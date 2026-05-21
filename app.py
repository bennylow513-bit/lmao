import base64
import json
import os
import re
import threading
import time
import traceback
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from html import unescape
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, render_template
from openai import OpenAI

try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:
    gspread = None
    Credentials = None

load_dotenv()

app = Flask(__name__)


@app.before_request
def ensure_inactivity_thread():
    start_inactivity_checker()


# ENV VARIABLES

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "")
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "")

CUSTOMER_SERVICE_WHATSAPP_NUMBER = os.getenv("CUSTOMER_SERVICE_WHATSAPP_NUMBER", "")
CUSTOMER_SERVICE_TELEGRAM_CHAT_ID = os.getenv("CUSTOMER_SERVICE_TELEGRAM_CHAT_ID", "")
DEBUG_ROUTE_TOKEN = os.getenv("DEBUG_ROUTE_TOKEN", "")

PORT = int(os.getenv("PORT", "5000"))
OPT_OUT_FILE = os.getenv("OPT_OUT_FILE", "telegram_opt_out_users.json")

# Chatlog settings
CHATLOG_ENABLED = os.getenv("CHATLOG_ENABLED", "true").lower() not in {"0", "false", "no", "off"}
CHATLOG_LOCAL_ENABLED = os.getenv("CHATLOG_LOCAL_ENABLED", "false").lower() not in {"0", "false", "no", "off"}
CHATLOG_DIR = os.getenv("CHATLOG_DIR", "chatlogs")
CHATLOG_FILE = os.getenv("CHATLOG_FILE", "chat_logs.jsonl")
CHATLOG_MAX_VIEW_LINES = int(os.getenv("CHATLOG_MAX_VIEW_LINES", "300"))

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
GOOGLE_SHEET_WORKSHEET = os.getenv("GOOGLE_SHEET_WORKSHEET", "Chat Logs").strip() or "Chat Logs"
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_SERVICE_ACCOUNT_JSON_BASE64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64", "")
GOOGLE_SERVICE_ACCOUNT_FILE = (
    os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
).strip()

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

AI_NOT_CONFIGURED_REPLY = (
    "I’m sorry — the AI answer service is not configured yet.\n"
    "Please type CUSTOMER SERVICE and our team will follow up."
)
AI_NOT_SURE_HANDOFF_REPLY = "I’m sorry — I’m not fully sure based on the information I have.\n[HANDOFF]"


# MEMORY

CHAT_HISTORY: Dict[str, List[Dict[str, str]]] = {}
PENDING_HANDOFFS: Dict[str, Dict[str, str]] = {}
TRIAL_BOOKINGS: Dict[str, Dict[str, str]] = {}
FLOW_STATE: Dict[str, Dict[str, str]] = {}
OUTLET_CONTEXT: Dict[str, str] = {}
INACTIVITY_STATE: Dict[str, Dict[str, object]] = {}
USER_LANGUAGE: Dict[str, str] = {}
LIVE_SUPPORT_CHATS: Dict[str, Dict[str, str]] = {}
SUPPORT_ACTIVE_CUSTOMER: Dict[str, str] = {}
CHATLOG_CHAT_META: Dict[str, Dict[str, str]] = {}
# Tracks when the bot just listed instructor names and is waiting
# for the user to reply with a specific instructor name.
STAFF_LIST_PENDING: Dict[str, bool] = {}

INACTIVITY_WARNING_SECONDS = int(os.getenv("INACTIVITY_WARNING_SECONDS", "300"))
INACTIVITY_CLOSE_SECONDS = int(os.getenv("INACTIVITY_CLOSE_SECONDS", "600"))
INACTIVITY_CHECK_SECONDS = int(os.getenv("INACTIVITY_CHECK_SECONDS", "30"))
INACTIVITY_THREAD_STARTED = False
INACTIVITY_REMINDER_QUEUE = None
CHATLOG_LOCK = threading.Lock()
CHATLOG_SHEET_HEADERS = ["timestamp_sg", "chat_id", "chat_type", "username", "directions", "role", "message"]
CHATLOG_SHEET = None
GOOGLE_SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# WORD LISTS

OPT_OUT_WORDS = {
    "stop",
    "unsubscribe",
    "opt out",
    "opt-out",
    "remove me",
    "no more messages",
    "do not message me",
    "dont message me",
    "don't message me",
    "cancel messages",
}

OPT_IN_WORDS = {
    "start",
    "/start",
    "subscribe",
    "opt in",
    "opt-in",
}

RESET_WORDS = {
    "menu",
    "/menu",
    "start",
    "/start",
    "home",
    "main menu",
    "restart",
    "hi",
    "hello",
    "hey",
    "salve",
    "ola",
    "olá",
    "oi",
    "你好",
    "您好",
    "哈咯",
    "salam",
    "selamat pagi",
    "apa khabar",
    "வணக்கம்",
}

SENSITIVE_KEYWORDS = [
    "nric",
    "ic number",
    "passport number",
    "credit card",
    "debit card",
    "card number",
    "cvv",
    "otp",
    "one time password",
    "password",
    "bank account",
    "bank number",
]

# Words that should never be sent through the live Customer Service chat.
# Add more words here if needed.
BLOCKED_WORDS = [
    "nigger",
    "nigga",
    "chink",
    "kike",
    "faggot",
    "retard",
]


def contains_blocked_word(text: str) -> bool:
    clean = simple_text(text)
    words = set(clean.split())
    return any(blocked in words for blocked in BLOCKED_WORDS)


# BASIC HELPERS

def normalize(text: str) -> str:
    return " ".join((text or "").strip().lower().replace("’", "'").split())


def simple_text(text: str) -> str:
    text = normalize(text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def phrase_list(text: str) -> List[str]:
    return [item.strip() for item in text.strip().split("|") if item.strip()]


def now_sg() -> str:
    return datetime.now(ZoneInfo("Asia/Singapore")).isoformat()


# CHATLOG HELPERS

def safe_chatlog_id(chat_id: str) -> str:
    """Make chat_id safe to use as a filename."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(chat_id)).strip("_")
    return safe or "unknown_chat"


def redact_chatlog_text(text: str) -> str:
    """Light redaction so sensitive info is not stored too plainly."""
    clean = text or ""

    # Emails
    clean = re.sub(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        "[email redacted]",
        clean,
    )

    # OTP-style phrases
    clean = re.sub(
        r"\b(otp|one time password|password|cvv)\s*[:=-]?\s*\S+",
        r"\1: [redacted]",
        clean,
        flags=re.IGNORECASE,
    )

    # Long card/account/ID-like numbers. Keep normal short menu choices untouched.
    clean = re.sub(r"\b\d{9,}\b", "[long number redacted]", clean)

    return clean


def chatlog_google_sheet_configured() -> bool:
    return bool(
        GOOGLE_SHEET_ID
        and (
            GOOGLE_SERVICE_ACCOUNT_JSON.strip()
            or GOOGLE_SERVICE_ACCOUNT_JSON_BASE64.strip()
            or GOOGLE_SERVICE_ACCOUNT_FILE
        )
    )


def chatlog_storage_label() -> str:
    destinations = []

    if chatlog_google_sheet_configured():
        destinations.append("google_sheet")

    if CHATLOG_LOCAL_ENABLED:
        destinations.append("local_file")

    return "+".join(destinations) or "none"


def load_google_service_account_info() -> Optional[Dict[str, Any]]:
    if GOOGLE_SERVICE_ACCOUNT_JSON.strip():
        return json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)

    if GOOGLE_SERVICE_ACCOUNT_JSON_BASE64.strip():
        decoded = base64.b64decode(GOOGLE_SERVICE_ACCOUNT_JSON_BASE64).decode("utf-8")
        return json.loads(decoded)

    return None


def build_google_credentials():
    if Credentials is None:
        raise RuntimeError("Install google-auth and gspread to write chat logs to Google Sheets.")

    service_account_info = load_google_service_account_info()

    if service_account_info:
        return Credentials.from_service_account_info(
            service_account_info,
            scopes=GOOGLE_SHEETS_SCOPES,
        )

    if GOOGLE_SERVICE_ACCOUNT_FILE:
        return Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE,
            scopes=GOOGLE_SHEETS_SCOPES,
        )

    raise RuntimeError("Google Sheets service account credentials are not configured.")


def get_chatlog_sheet():
    global CHATLOG_SHEET

    if not chatlog_google_sheet_configured():
        return None

    if gspread is None:
        raise RuntimeError("Install gspread to write chat logs to Google Sheets.")

    if CHATLOG_SHEET is not None:
        return CHATLOG_SHEET

    client = gspread.authorize(build_google_credentials())
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)

    try:
        worksheet = spreadsheet.worksheet(GOOGLE_SHEET_WORKSHEET)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=GOOGLE_SHEET_WORKSHEET,
            rows=1000,
            cols=len(CHATLOG_SHEET_HEADERS),
        )

    existing_headers = worksheet.row_values(1)

    if existing_headers[: len(CHATLOG_SHEET_HEADERS)] != CHATLOG_SHEET_HEADERS:
        worksheet.update(values=[CHATLOG_SHEET_HEADERS], range_name="A1:G1")

    CHATLOG_SHEET = worksheet
    return CHATLOG_SHEET


def build_chatlog_row(
    chat_id: str,
    direction: str,
    role: str,
    message: str,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    chat_id = str(chat_id)
    meta = meta or {}
    saved_meta = CHATLOG_CHAT_META.get(chat_id, {})
    chat_type = str(meta.get("chat_type") or saved_meta.get("chat_type", ""))
    username = str(
        meta.get("username")
        or meta.get("telegram_username")
        or meta.get("telegram_first_name")
        or saved_meta.get("username", "")
    )
    timestamp_sg = now_sg()

    if chat_type or username:
        CHATLOG_CHAT_META[chat_id] = {
            "chat_type": chat_type,
            "username": username,
        }

    return {
        "timestamp_sg": timestamp_sg,
        "time_sg": timestamp_sg,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "username": username,
        "directions": direction,
        "direction": direction,
        "role": role,
        "message": redact_chatlog_text(message),
        "meta": meta,
    }


def chatlog_row_values(row: Dict[str, Any]) -> List[str]:
    return [
        row.get("timestamp_sg", "") or row.get("time_sg", ""),
        row.get("chat_id", ""),
        row.get("chat_type", ""),
        row.get("username", ""),
        row.get("directions", "") or row.get("direction", ""),
        row.get("role", ""),
        row.get("message", ""),
    ]


def append_google_sheet_chatlog(row: Dict[str, Any]) -> None:
    worksheet = get_chatlog_sheet()

    if worksheet is None:
        return

    worksheet.append_row(chatlog_row_values(row), value_input_option="RAW")


def write_local_chatlog(row: Dict[str, Any]) -> None:
    os.makedirs(CHATLOG_DIR, exist_ok=True)
    safe_id = safe_chatlog_id(row.get("chat_id", ""))
    per_chat_path = os.path.join(CHATLOG_DIR, f"{safe_id}.jsonl")
    line = json.dumps(row, ensure_ascii=False) + "\n"

    if CHATLOG_FILE:
        chatlog_file_dir = os.path.dirname(CHATLOG_FILE)

        if chatlog_file_dir:
            os.makedirs(chatlog_file_dir, exist_ok=True)

        with open(CHATLOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)

    with open(per_chat_path, "a", encoding="utf-8") as f:
        f.write(line)


def write_chatlog(
    chat_id: str,
    direction: str,
    role: str,
    message: str,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one message/event into the configured chat log destination."""
    if not CHATLOG_ENABLED:
        return

    try:
        row = build_chatlog_row(chat_id, direction, role, message, meta)

        with CHATLOG_LOCK:
            if chatlog_google_sheet_configured():
                try:
                    append_google_sheet_chatlog(row)
                except Exception as e:
                    print("GOOGLE SHEET CHATLOG WRITE ERROR:", str(e), flush=True)

            if CHATLOG_LOCAL_ENABLED:
                try:
                    write_local_chatlog(row)
                except Exception as e:
                    print("LOCAL CHATLOG WRITE ERROR:", str(e), flush=True)

    except Exception as e:
        print("CHATLOG WRITE ERROR:", str(e), flush=True)


def read_local_chatlog_entries(chat_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    safe_id = safe_chatlog_id(chat_id)
    file_path = os.path.join(CHATLOG_DIR, f"{safe_id}.jsonl")

    if not os.path.exists(file_path):
        return []

    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    entries: List[Dict[str, Any]] = []

    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except Exception:
            continue

    return entries


def read_local_chatlog_file_entries(limit: int = 100) -> List[Dict[str, Any]]:
    if not CHATLOG_FILE or not os.path.exists(CHATLOG_FILE):
        return []

    with open(CHATLOG_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    entries: List[Dict[str, Any]] = []

    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except Exception:
            continue

    return entries


def parse_chatlog_sheet_row(headers: List[str], values: List[str]) -> Dict[str, Any]:
    raw = {
        header: values[index] if index < len(values) else ""
        for index, header in enumerate(headers)
    }
    meta_text = raw.get("meta_json", "")
    timestamp_sg = raw.get("timestamp_sg", "") or raw.get("time_sg", "")
    directions = raw.get("directions", "") or raw.get("direction", "")

    try:
        meta = json.loads(meta_text) if meta_text else {}
    except Exception:
        meta = {"raw": meta_text}

    return {
        "timestamp_sg": timestamp_sg,
        "time_sg": timestamp_sg,
        "chat_id": raw.get("chat_id", ""),
        "chat_type": raw.get("chat_type", ""),
        "username": raw.get("username", ""),
        "directions": directions,
        "direction": directions,
        "role": raw.get("role", ""),
        "message": raw.get("message", ""),
        "meta": meta,
    }


def read_google_sheet_chatlog_entries(limit: int = 100, chat_id: str = "") -> List[Dict[str, Any]]:
    try:
        with CHATLOG_LOCK:
            worksheet = get_chatlog_sheet()

            if worksheet is None:
                return []

            rows = worksheet.get_all_values()

    except Exception as e:
        print("CHATLOG READ ERROR:", str(e), flush=True)
        return []

    if len(rows) <= 1:
        return []

    headers = rows[0]
    entries = [
        parse_chatlog_sheet_row(headers, values)
        for values in rows[1:]
        if any(str(value).strip() for value in values)
    ]

    if chat_id:
        chat_id = str(chat_id)
        entries = [entry for entry in entries if str(entry.get("chat_id", "")) == chat_id]

    if limit > 0:
        entries = entries[-limit:]

    return entries


def read_chatlog_entries(chat_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    if chatlog_google_sheet_configured():
        return read_google_sheet_chatlog_entries(limit=limit, chat_id=chat_id)

    return read_local_chatlog_entries(chat_id, limit=limit)


def read_chatlog_file_entries(limit: int = 100) -> List[Dict[str, Any]]:
    if chatlog_google_sheet_configured():
        return read_google_sheet_chatlog_entries(limit=limit)

    return read_local_chatlog_file_entries(limit=limit)


def clean_number(number: str) -> str:
    return str(number).replace("+", "").replace(" ", "").replace("-", "").strip()


def load_opt_out_users() -> set:
    try:
        with open(OPT_OUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return set(str(x) for x in data)

    except Exception:
        pass

    return set()


OPT_OUT_USERS = load_opt_out_users()


def save_opt_out_users() -> None:
    with open(OPT_OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(OPT_OUT_USERS), f, ensure_ascii=False, indent=2)


def load_knowledge_text() -> str:
    try:
        with open("knowledge.txt", "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


# Website knowledge is fetched at startup so the LLM can answer General Enquiry
# questions using Jal Yoga's website while the rest of the chatbot stays unchanged.
WEBSITE_KNOWLEDGE_URLS = [
    "https://www.jalyoga.com.sg/",
    "https://www.jalyoga.com.sg/our-studios/",
    "https://www.jalyoga.com.sg/our-instructors/",
    "https://www.jalyoga.com.sg/jal-schedule/",
    "https://www.jalyoga.com.sg/memberships/",
    "https://www.jalyoga.com.sg/yoga-classes/",
    "https://www.jalyoga.com.sg/barre-classes/",
    "https://www.jalyoga.com.sg/mat-pilates-classes/",
    "https://www.jalyoga.com.sg/reformer-pilates-classes/",
    "https://www.jalyoga.com.sg/infrared-heat/",
    "https://www.jalyoga.com.sg/corporate-classes/",
    "https://www.jalyoga.com.sg/face-yoga-workshop/",
    "https://www.jalyoga.com.sg/me-face-yoga/",
    "https://www.jalyoga.com.sg/nepal-yoga-retreat/",
    "https://www.jalyoga.com.sg/yoga-personal-training/",
    "https://www.jalyoga.com.sg/reformer-pilates-personal-training/",
    "https://www.jalyoga.com.sg/pilates-teacher-training-course/",
    "https://www.jalyoga.com.sg/hatha-teacher-training-course/",
    "https://www.jalyoga.com.sg/barre-teacher-training-course/",
    "https://www.jalyoga.com.sg/sound-bath-teacher-training-course/",
    "https://www.jalyoga.com.sg/me-face-yoga-teacher-training-course/",
]


INSTRUCTOR_PROFILE_PATH_RE = re.compile(r"^/our-instructor/[^/]+/?$", re.IGNORECASE)


def jal_yoga_host(netloc: str) -> str:
    host = (netloc or "").lower()
    return host[4:] if host.startswith("www.") else host


def canonical_website_url(url: str) -> str:
    parsed = urlparse(url)

    if not parsed.scheme or not parsed.netloc:
        return ""

    path = parsed.path or "/"

    if path != "/" and not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
        path += "/"

    return f"{parsed.scheme}://{parsed.netloc.lower()}{path}"


def discover_instructor_profile_urls(page_url: str, html: str) -> List[str]:
    urls = []
    seen = set()

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        hrefs = [tag.get("href", "") for tag in soup.find_all("a", href=True)]
    except Exception:
        hrefs = re.findall(r"""href=["']([^"']+)["']""", html or "", flags=re.IGNORECASE)

    for href in hrefs:
        absolute_url = urljoin(page_url, href)
        parsed = urlparse(absolute_url)

        if jal_yoga_host(parsed.netloc) != "jalyoga.com.sg":
            continue

        if not INSTRUCTOR_PROFILE_PATH_RE.match(parsed.path or ""):
            continue

        clean_url = canonical_website_url(absolute_url)

        if clean_url and clean_url not in seen:
            seen.add(clean_url)
            urls.append(clean_url)

    return urls


def html_to_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()

        text = soup.get_text("\n")

    except Exception:
        text = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "\n", text)

    lines = []

    for line in text.splitlines():
        clean = re.sub(r"\s+", " ", line).strip()

        if clean:
            lines.append(clean)

    return "\n".join(lines)


def fetch_website_knowledge() -> str:
    parts = []
    urls_to_fetch = list(WEBSITE_KNOWLEDGE_URLS)
    fetched_urls = set()

    index = 0

    while index < len(urls_to_fetch):
        url = urls_to_fetch[index]
        index += 1

        canonical_url = canonical_website_url(url)

        if not canonical_url or canonical_url in fetched_urls:
            continue

        fetched_urls.add(canonical_url)

        try:
            response = requests.get(
                canonical_url,
                timeout=12,
                headers={
                    "User-Agent": "Mozilla/5.0 JalYogaTelegramAssistant/1.0"
                },
            )

            if response.status_code != 200:
                print(f"WEBSITE SKIPPED {canonical_url}: {response.status_code}", flush=True)
                continue

            for linked_url in discover_instructor_profile_urls(canonical_url, response.text):
                if linked_url not in fetched_urls and linked_url not in urls_to_fetch:
                    urls_to_fetch.append(linked_url)

            page_text = html_to_text(response.text)
            page_label = canonical_url.rstrip("/").split("/")[-1] or "home"
            page_label = page_label.replace("-", " ").title()

            if page_text:
                parts.append(
                    f"\n\n==================================================\n"
                    f"JAL YOGA WEBSITE CONTENT: {page_label}\n"
                    f"==================================================\n"
                    f"{page_text[:12000]}"
                )

        except Exception as e:
            print(f"WEBSITE KNOWLEDGE FETCH ERROR for {canonical_url}: {e}", flush=True)

    return "\n".join(parts).strip()


# Knowledge source.
# The Jal Yoga website is the PRIMARY source for company facts.
# knowledge.txt is a SUPPLEMENT: it only fills in the few details the web
# crawler cannot pick up (conversation flows, studio policies, suspension
# fees, refer-a-friend, handoff wording, etc.).
LOCAL_KNOWLEDGE_TEXT = """
==================================================
JAL YOGA OPERATING HOURS
==================================================
Please refer to the Jal Yoga website content sections for each studio's specific operating hours and schedules.
"""

# Website content first (primary source).
WEBSITE_TEXT = fetch_website_knowledge() + "\n\n" + LOCAL_KNOWLEDGE_TEXT

# knowledge.txt loaded separately as a supplement.
SUPPLEMENT_KNOWLEDGE_TEXT = load_knowledge_text()

# KNOWLEDGE_TEXT = website (primary) + knowledge.txt (supplement).
# Website stays first so it wins for anything it already covers.
if SUPPLEMENT_KNOWLEDGE_TEXT:
    KNOWLEDGE_TEXT = (
        WEBSITE_TEXT
        + "\n\n"
        + "==================================================\n"
        + "SUPPLEMENTARY KNOWLEDGE (use only for details NOT in the website content above)\n"
        + "==================================================\n"
        + SUPPLEMENT_KNOWLEDGE_TEXT
    )
else:
    KNOWLEDGE_TEXT = WEBSITE_TEXT


# STUDIOS

def parse_studios(text: str) -> List[Dict[str, str]]:
    studios: List[Dict[str, str]] = []
    inside = False

    for line in text.splitlines():
        clean = line.strip()

        if clean.upper().startswith("2. STUDIOS"):
            inside = True
            continue

        if inside and clean.startswith("===") and studios:
            break

        if not inside:
            continue

        if not clean.startswith("- "):
            continue

        item = clean[2:].strip()

        if ":" not in item:
            continue

        name, address = item.split(":", 1)
        name = name.strip()
        address = address.strip()

        if not name or not address:
            continue

        if "singapore" not in address.lower():
            continue

        if not any(s["name"].lower() == name.lower() for s in studios):
            studios.append({"name": name, "address": address})

    return studios


STUDIOS = parse_studios(KNOWLEDGE_TEXT)

if not STUDIOS:
    STUDIOS = [
        {
            "name": "Alexandra",
            "address": "456 Alexandra Rd, #02-03, Singapore 119962",
        },
        {
            "name": "Katong",
            "address": "131 E Coast Rd, #03-01, Singapore 428816",
        },
        {
            "name": "Kovan",
            "address": "1F Yio Chu Kang Rd, Singapore 545512",
        },
        {
            "name": "Upper Bukit Timah",
            "address": "816 Upper Bukit Timah Road, Singapore 678149",
        },
        {
            "name": "Woodlands",
            "address": "8 Woodlands Sq, #04-12/13 Wood Square, Solo 2, Singapore 737713",
        },
    ]


NEAREST_OUTLET_AREA_RULES: List[Dict[str, str]] = []
NEAREST_OUTLET_POSTAL_RULES: Dict[str, str] = {}


def studio_names() -> List[str]:
    return [studio["name"] for studio in STUDIOS]


def studio_options_text(include_not_specified: bool = False) -> str:
    options = [f"{index}. {name}" for index, name in enumerate(studio_names(), start=1)]

    if include_not_specified:
        options.append(f"{len(options) + 1}. Not specified")

    return "\n".join(options)


def studio_prompt(question: str, include_not_specified: bool = False) -> str:
    return f"{question}\n\n{studio_options_text(include_not_specified)}"


TRIAL_STUDIO_QUESTION = (
    "We’d love to help with a trial class. Which studio would you like to visit: "
    "Alexandra, Katong, Kovan, Upper Bukit Timah, or Woodlands?"
)


MINDBODY_SCHEDULE_WIDGETS = {
    "Alexandra": {"widget_id": "211772", "location_id": "1"},
    "Katong": {"widget_id": "211771", "location_id": "3"},
    "Kovan": {"widget_id": "203543", "location_id": "6"},
    "Upper Bukit Timah": {"widget_id": "211773", "location_id": "2"},
    "Woodlands": {"widget_id": "211775", "location_id": "5"},
}


def studio_aliases(studio_name: str) -> List[str]:
    clean = simple_text(studio_name)
    words = clean.split()

    aliases = {clean}

    if len(words) > 1:
        aliases.add("".join(word[0] for word in words if word))

    for word in words:
        if len(word) >= 4:
            aliases.add(word)

    if studio_name.lower() == "upper bukit timah":
        aliases.update({"bukit timah", "upper bt", "bt", "ubt"})

    return list(aliases)


def detect_outlet_from_text(text: str) -> str:
    clean = simple_text(text)

    if not clean:
        return ""

    # 1. FAST PATH: Check basic English/hardcoded aliases first (saves API cost & time)
    padded = f" {clean} "

    for studio_name in studio_names():
        for alias in studio_aliases(studio_name):
            if f" {alias} " in padded:
                return studio_name

    # 2. THE AI WAY: Ask OpenAI to translate and map the studio
    if client:
        valid_studios = ", ".join(studio_names())
        instructions = (
            "You are an assistant that extracts the Jal Yoga studio name from a user's message. "
            f"The valid studios are: {valid_studios}. "
            "The user might use ANY language (e.g., Chinese, Malay, Thai, Tamil, etc.) or bad spelling. "
            "Return ONLY the exact English name of the matching studio from the valid list. "
            "If the user does not mention a studio, or you are unsure, reply EXACTLY with 'UNKNOWN'."
        )
        
        ai_result = openai_text_reply(
            instructions, 
            text, 
            fallback="UNKNOWN", 
            error_label="AI OUTLET DETECT ERROR",
            show_traceback=False
        ).strip()
        
        # Check if the AI returned a valid studio name
        for studio_name in studio_names():
            if studio_name.lower() == ai_result.lower():
                return studio_name

    # 3. FALLBACK: Old fuzzy matching just in case OpenAI is temporarily down
    words = clean.split()
    chunks = []

    for size in range(1, 4):
        for i in range(len(words) - size + 1):
            chunks.append(" ".join(words[i:i + size]))

    best_studio = ""
    best_score = 0.0

    for studio_name in studio_names():
        for alias in studio_aliases(studio_name):
            for chunk in chunks:
                score = SequenceMatcher(None, chunk, alias).ratio()

                if score > best_score:
                    best_score = score
                    best_studio = studio_name

    if best_score >= 0.78:
        return best_studio

    return ""


def detect_outlet_choice(text: str, include_not_specified: bool = False) -> str:
    norm = normalize(text)
    names = studio_names()

    if norm.isdigit():
        number = int(norm)

        if 1 <= number <= len(names):
            return names[number - 1]

        if include_not_specified and number == len(names) + 1:
            return "Not specified"

    outlet = detect_outlet_from_text(text)

    if outlet:
        return outlet

    if include_not_specified:
        if norm in {
            "not specified",
            "no",
            "no specific outlet",
            "any",
            "any outlet",
            "not sure",
            "idk",
            "does not matter",
            "doesn't matter",
            "no outlet",
        }:
            return "Not specified"

    return ""


def get_studio_address(outlet_name: str) -> str:
    return next(
        (studio["address"] for studio in STUDIOS if studio["name"].lower() == outlet_name.lower()),
        "",
    )


def remember_outlet_context(chat_id: str, outlet: str) -> None:
    if outlet and outlet in studio_names():
        OUTLET_CONTEXT[chat_id] = outlet


def get_flow_outlet(chat_id: str) -> str:
    flow = get_flow(chat_id)
    return (
        flow.get("outlet", "")
        or flow.get("recommended_outlet", "")
        or OUTLET_CONTEXT.get(chat_id, "")
    )


def clean_location_candidate(text: str) -> str:
    candidate = normalize(text)
    candidate = re.sub(r"https?://\S+", " ", candidate)
    candidate = re.sub(r"\b(singapore|sg)\b", " ", candidate)
    candidate = re.sub(
        r"\b(nearest|closest|nearer|closer|nearby|outlet|outlets|studio|studios|branch|branches|"
        r"jal yoga|recommend|recommended|recomend|reccomend|reccomed|suggest|pick|choose|which|what|where|"
        r"is|are|the|to|from|around|near|at|in|my|me|current|location|locate|"
        r"home|house|residence|address|postal|code|help|find|based|on|please|pls|can|you|i|im|i'm|stay|staying|live|"
        r"living|am|for|would|like)\b",
        " ",
        candidate,
    )
    candidate = re.sub(r"[^a-z0-9\s]", " ", candidate)
    return " ".join(candidate.split())


def location_candidates_from_text(text: str) -> List[str]:
    norm = normalize(text)
    candidates: List[str] = []
    postal_match = re.search(r"\b\d{6}\b", norm)

    if postal_match:
        candidates.append(postal_match.group(0))

    patterns = [
        r"(?:nearest|closest|nearer|closer|nearby).*(?:to|from|at|in|around|near)\s+(.+)$",
        # Notice we added 'stay', 'staying', 'live', 'living' right here!
        r"(?:i am|i'm|im|i stay|i'm staying|im staying|i live|i'm living|im living|stay|staying|live|living)\s+(?:at|in|near|around)?\s*(.+)$",
        r"(?:my location is|location is|address is|i am at|i'm at|im at)\s+(.+)$",
        r"(?:near|around|at|in|from)\s+(.+)$",
    ]

    for pattern in patterns:
        match = re.search(pattern, norm, flags=re.IGNORECASE)

        if match:
            candidate = clean_location_candidate(match.group(1))

            if candidate:
                candidates.append(candidate)

    fallback = clean_location_candidate(norm)

    if fallback:
        candidates.append(fallback)

    unique_candidates: List[str] = []

    for candidate in candidates:
        if candidate and candidate not in unique_candidates:
            unique_candidates.append(candidate)

    return unique_candidates


def clean_area_label(label: str) -> str:
    label = re.sub(r"\bnearby\b", "", label, flags=re.IGNORECASE)
    label = re.sub(r"\bareas?\b", "", label, flags=re.IGNORECASE)
    label = re.sub(r"\s+", " ", label)
    return label.strip(" .")


def split_area_labels(area_text: str) -> List[str]:
    cleaned = area_text.strip().rstrip(".")
    cleaned = re.sub(r"\s+and\s+", ", ", cleaned, flags=re.IGNORECASE)
    labels = []

    for raw_group in cleaned.split(","):
        for raw_label in raw_group.split("/"):
            label = clean_area_label(raw_label)

            if label:
                labels.append(label)

    return labels


def expand_postal_prefixes(prefix_text: str) -> List[str]:
    prefixes: List[str] = []

    for raw_token in prefix_text.split(","):
        token = raw_token.strip()
        range_match = re.fullmatch(r"(\d{2})\s*-\s*(\d{2})", token)

        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))

            if start <= end:
                prefixes.extend(f"{number:02d}" for number in range(start, end + 1))

            continue

        if re.fullmatch(r"\d{2}", token):
            prefixes.append(token)

    return prefixes


def parse_nearest_outlet_area_rules(text: str) -> List[Dict[str, str]]:
    rules: List[Dict[str, str]] = []
    inside = False

    for line in text.splitlines():
        clean = line.strip()
        lower = clean.lower()

        if lower.startswith("nearest outlet guide:"):
            inside = True
            continue

        if inside and not clean and rules:
            break

        if not inside or not clean.startswith("- "):
            continue

        match = re.match(r"^- (?P<outlet>.+?) is usually best for (?P<areas>.+)$", clean)

        if not match:
            continue

        outlet = detect_outlet_from_text(match.group("outlet"))

        if not outlet:
            continue

        for label in split_area_labels(match.group("areas")):
            key = simple_text(label)

            if key:
                rules.append({"outlet": outlet, "label": label, "key": key})

    return rules


def parse_nearest_outlet_postal_rules(text: str) -> Dict[str, str]:
    rules: Dict[str, str] = {}
    inside = False

    for line in text.splitlines():
        clean = line.strip()
        lower = clean.lower()

        if lower.startswith("nearest outlet postal prefix guide:"):
            inside = True
            continue

        if inside and not clean and rules:
            break

        if not inside or not clean.startswith("- ") or ":" not in clean:
            continue

        outlet_text, prefix_text = clean[2:].split(":", 1)
        outlet = detect_outlet_from_text(outlet_text)

        if not outlet:
            continue

        for prefix in expand_postal_prefixes(prefix_text):
            rules[prefix] = outlet

    return rules


DEFAULT_NEAREST_OUTLET_AREAS: Dict[str, List[str]] = {
    "Alexandra": [
        "Alexandra",
        "Anchorpoint",
        "Bras Basah",
        "Bugis",
        "Buona Vista",
        "Chinatown",
        "City Hall",
        "Clarke Quay",
        "Commonwealth",
        "Dempsey",
        "Dhoby Ghaut",
        "Esplanade",
        "Great World",
        "HarbourFront",
        "Havelock",
        "Holland Village",
        "Kent Ridge",
        "Labrador Park",
        "Mapletree Business City",
        "Marina Bay",
        "MBS",
        "NUS",
        "Newton",
        "Novena",
        "one-north",
        "Orchard",
        "Outram Park",
        "Pasir Panjang",
        "Promenade",
        "Queenstown",
        "Raffles Place",
        "Redhill",
        "River Valley",
        "Robertson Quay",
        "Sentosa",
        "Somerset",
        "Suntec",
        "Tanglin",
        "Telok Blangah",
        "Tanjong Pagar",
        "Tiong Bahru",
        "VivoCity",
    ],
    "Katong": [
        "Aljunied",
        "Bayshore",
        "Bedok",
        "Bedok Mall",
        "Bedok Reservoir",
        "CBP",
        "Changi",
        "Changi Business Park",
        "Dakota",
        "East Coast",
        "Eunos",
        "Expo",
        "Geylang",
        "Joo Chiat",
        "Kaki Bukit",
        "Kallang",
        "Katong",
        "Katong Shopping Centre",
        "Katong V",
        "Kembangan",
        "Marine Parade",
        "Marine Terrace",
        "Mountbatten",
        "Parkway Parade",
        "Paya Lebar",
        "PLQ",
        "Pasir Ris",
        "Siglap",
        "Siglap Centre",
        "Simei",
        "Stadium",
        "Tampines",
        "Tanah Merah",
        "Tanjong Katong",
        "Tanjong Rhu",
        "Ubi",
    ],
    "Kovan": [
        "AMK",
        "Ang Mo Kio",
        "Bartley",
        "Bidadari",
        "Bishan",
        "Boon Keng",
        "Braddell",
        "Bright Hill",
        "Buangkok",
        "Caldecott",
        "Compass One",
        "Farrer Park",
        "HDB Hub",
        "Hougang",
        "Junction 8",
        "Kovan",
        "Lorong Chuan",
        "MacPherson",
        "Marymount",
        "NEX",
        "Potong Pasir",
        "Punggol",
        "Seletar",
        "Sengkang",
        "Serangoon",
        "Tai Seng",
        "Thomson Plaza",
        "Toa Payoh",
        "TPY",
        "Upper Serangoon",
        "Upper Thomson",
        "Waterway Point",
        "Woodleigh",
        "YCK",
        "Yio Chu Kang",
    ],
    "Upper Bukit Timah": [
        "Beauty World",
        "Boon Lay",
        "Bukit Batok",
        "Bukit Gombak",
        "Bukit Panjang",
        "Bukit Timah",
        "Cashew",
        "Chinese Garden",
        "Choa Chu Kang",
        "CCK",
        "Clementi",
        "Clementi Mall",
        "Dairy Farm",
        "Dover",
        "Gek Poh",
        "Gul Circle",
        "Hillview",
        "IMM",
        "Jem",
        "Joo Koon",
        "Jurong",
        "Jurong East",
        "King Albert Park",
        "Lakeside",
        "Lot One",
        "Nanyang",
        "Ngee Ann Poly",
        "NTU",
        "Bukit Panjang Plaza",
        "Pioneer",
        "Rail Mall",
        "SIM",
        "Sixth Avenue",
        "Taman Jurong",
        "Tengah",
        "Teck Whye",
        "Tuas",
        "Tuas Link",
        "West Coast",
        "Westgate",
        "Yew Tee",
    ],
    "Woodlands": [
        "Admiralty",
        "Canberra",
        "Causeway Point",
        "Khatib",
        "Marsiling",
        "Mandai",
        "Northpoint",
        "Sembawang",
        "Senoko",
        "Springleaf",
        "Woodlands",
        "Woodlands Square",
        "Yishun",
    ],
}


def default_nearest_outlet_area_rules() -> List[Dict[str, str]]:
    rules: List[Dict[str, str]] = []
    names = set(studio_names())

    for outlet, labels in DEFAULT_NEAREST_OUTLET_AREAS.items():
        if outlet not in names:
            continue

        for label in labels:
            key = simple_text(label)

            if key:
                rules.append({"outlet": outlet, "label": label, "key": key})

    return rules


def merge_nearest_outlet_area_rules(
    knowledge_rules: List[Dict[str, str]],
    default_rules: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    merged = list(knowledge_rules)
    seen_keys = {rule.get("key", "") for rule in merged}

    for rule in default_rules:
        key = rule.get("key", "")

        if key and key not in seen_keys:
            merged.append(rule)
            seen_keys.add(key)

    return merged


NEAREST_OUTLET_AREA_RULES = merge_nearest_outlet_area_rules(
    parse_nearest_outlet_area_rules(KNOWLEDGE_TEXT),
    default_nearest_outlet_area_rules(),
)
NEAREST_OUTLET_POSTAL_RULES = parse_nearest_outlet_postal_rules(KNOWLEDGE_TEXT)


def text_chunks(clean: str, max_size: int = 4) -> List[str]:
    words = clean.split()
    chunks = []

    for size in range(1, min(max_size, len(words)) + 1):
        for index in range(len(words) - size + 1):
            chunks.append(" ".join(words[index:index + size]))

    return chunks


def area_location_match(text: str) -> Optional[Dict[str, str]]:
    clean = simple_text(text)

    if not clean:
        return None

    padded = f" {clean} "
    chunks = text_chunks(clean)
    best_rule: Optional[Dict[str, str]] = None
    best_score = 0.0

    for rule in NEAREST_OUTLET_AREA_RULES:
        key = rule.get("key", "")

        if not key:
            continue

        score = 0.0

        if clean == key or f" {key} " in padded:
            score = 1.0 + (len(key) / 1000)
        elif len(key) >= 5:
            score = max((SequenceMatcher(None, chunk, key).ratio() for chunk in chunks), default=0.0)

            if score < 0.88:
                score = 0.0

        if score > best_score:
            best_score = score
            best_rule = rule

    if not best_rule:
        return None

    return {
        "label": best_rule["label"],
        "outlet": best_rule["outlet"],
        "source": "knowledge_area",
    }


def postal_location_match(text: str) -> Optional[Dict[str, str]]:
    match = re.search(r"\b(\d{2})\d{4}\b", text)

    if not match:
        return None

    prefix = match.group(1)
    outlet = NEAREST_OUTLET_POSTAL_RULES.get(prefix)

    if not outlet:
        return None

    return {
        "label": f"Singapore {match.group(0)}",
        "outlet": outlet,
        "source": "knowledge_postal_prefix",
    }


def resolve_user_location(text: str) -> Optional[Dict[str, str]]:
    candidates = location_candidates_from_text(text)

    for candidate in candidates:
        resolved = postal_location_match(candidate)

        if resolved:
            return resolved

    for candidate in candidates:
        resolved = area_location_match(candidate)

        if resolved:
            return resolved

    return None


def nearest_outlet_recommendation(text: str) -> Optional[Dict[str, Any]]:
    location = resolve_user_location(text)

    if not location:
        return None

    outlet = location.get("outlet", "")

    if not outlet:
        return None

    return {
        "location": location,
        "ranked_studios": [
            {
                "name": outlet,
                "address": get_studio_address(outlet),
            }
        ],
    }


def is_nearest_outlet_request(text: str) -> bool:
    clean = simple_text(text)

    if not clean:
        return False

    # Notice we added "stay at", "live at", and "staying at" to the end of this list!
    location_phrases = ["i stay", "i live", "im at", "i am at", "my location", "im staying", "living in", "stay at", "live at", "staying at"]
    if any(phrase in clean for phrase in location_phrases):
        return True

    # 2. Add "go" and "visit" to context words
    has_outlet_context = any(
        word in clean
        for word in [
            "outlet", "outlets", "studio", "studios", "branch", "branches", 
            "jal yoga", "trial", "trail", "class", "go", "visit"
        ]
    )

    if any(word in clean for word in ["nearest", "closest", "nearer", "closer", "nearby"]) and has_outlet_context:
        return True

    if re.search(r"\b(outlet|outlets|studio|studios|branch|branches)\b.*\b(near|around)\b", clean):
        return True

    if re.search(r"\b(near|around)\b.*\b(outlet|outlets|studio|studios|branch|branches)\b", clean):
        return True

    if "near me" in clean and has_outlet_context:
        return True

    recommendation_words = [
        "recommend",
        "recommended",
        "recomend",
        "reccomend",
        "reccomed",
        "reccomened", # Typo included
        "suggest",
        "pick",
        "choose",
    ]

    if any(word in clean for word in recommendation_words) and has_outlet_context:
        return True

    if any(word in clean for word in recommendation_words) and any(
        word in clean for word in ["near", "nearest", "closest", "home", "house", "location"]
    ):
        return True

    if has_outlet_context and any(phrase in clean for phrase in ["should i go", "should i visit", "which one", "where do i go", "where to go", "where should i"]):
        return True

    return False


def is_all_outlets_request(text: str) -> bool:
    clean = simple_text(text)

    if not clean or is_nearest_outlet_request(text):
        return False

    if any(word in clean for word in ["contact", "phone", "whatsapp", "call", "hotline"]):
        return False

    outlet_terms = {
        "outlet",
        "outlets",
        "studio",
        "studios",
        "branch",
        "branches",
        "location",
        "locations",
    }
    has_outlet_term = bool(set(clean.split()) & outlet_terms)

    if has_outlet_term and any(phrase in clean for phrase in ["how many", "number of", "count of"]):
        return True

    outlet_words = r"(outlet|outlets|studio|studios|branch|branches|location|locations)"

    patterns = [
        rf"\b(how many|number of|count of)\b.*\b{outlet_words}\b",
        rf"\b(list|show|share|send|see)\b.*\b(all )?{outlet_words}\b",
        rf"\ball\b.*\b{outlet_words}\b",
        rf"\b(studio locations|outlet locations)\b",
        rf"\b(what|which|where)\b.*\b{outlet_words}\b.*\b(have|available|are|located)\b",
    ]

    if clean in {"outlet", "outlets", "studio", "studios", "locations", "studio locations", "outlet locations"}:
        return True

    return any(re.search(pattern, clean, flags=re.IGNORECASE) for pattern in patterns)


# CONTACT CONFIG

def env_key_for_outlet(outlet_name: str, suffix: str) -> str:
    key = re.sub(r"[^A-Za-z0-9]+", "_", outlet_name.upper()).strip("_")
    return f"{key}_{suffix}"


def env_key_for_outlet_whatsapp(outlet_name: str) -> str:
    return env_key_for_outlet(outlet_name, "WHATSAPP_NUMBER")


def outlet_whatsapp_number(outlet_name: str) -> str:
    return os.getenv(env_key_for_outlet_whatsapp(outlet_name), "")


def env_key_for_outlet_telegram_chat(outlet_name: str) -> str:
    return env_key_for_outlet(outlet_name, "TELEGRAM_CHAT_ID")


def outlet_telegram_chat_id(outlet_name: str) -> str:
    return os.getenv(env_key_for_outlet_telegram_chat(outlet_name), "")


def build_outlet_contact_reply(outlet: str) -> str:
    number = outlet_whatsapp_number(outlet)

    if not number or clean_number(number).upper() == "TBC":
        number = CUSTOMER_SERVICE_WHATSAPP_NUMBER

    clean = clean_number(number)

    if not clean or clean.upper() == "TBC":
        return (
            f"{outlet} outlet contact is not configured yet.\n\n"
            f"Address:\n{get_studio_address(outlet)}"
        )

    return (
        f"{outlet} outlet contact:\n"
        f"+{clean}\n"
        f"https://wa.me/{clean}\n\n"
        f"Address:\n{get_studio_address(outlet)}"
    )


def build_customer_service_contact_reply(outlet: str = "") -> str:
    number = outlet_whatsapp_number(outlet) if outlet else ""

    if not number or clean_number(number).upper() == "TBC":
        number = CUSTOMER_SERVICE_WHATSAPP_NUMBER

    clean = clean_number(number)

    if not clean or clean.upper() == "TBC":
        if outlet:
            return (
                f"{outlet} Customer Service contact is not configured yet.\n\n"
                f"Address:\n{get_studio_address(outlet)}"
            )

        return "Customer Service contact is not configured yet."

    title = f"{outlet} Customer Service contact" if outlet else "Jal Yoga Customer Service contact"
    lines = [
        f"{title}:",
        f"+{clean}",
        f"https://wa.me/{clean}",
    ]

    if outlet:
        lines.extend(["", "Address:", get_studio_address(outlet)])

    return "\n".join(lines)


def live_contact_config_text() -> str:
    outlet_lines = []

    for studio in STUDIOS:
        name = studio["name"]
        number = outlet_whatsapp_number(name) or "TBC"
        telegram_chat_id = outlet_telegram_chat_id(name) or "TBC"

        outlet_lines.append(
            f"- {name}: WhatsApp={number}, Telegram Chat ID={telegram_chat_id}"
        )

    return f"""
LIVE CUSTOMER SERVICE CONFIG FROM RENDER

Main Customer Service WhatsApp:
- {CUSTOMER_SERVICE_WHATSAPP_NUMBER or "TBC"}

Fallback Customer Service Telegram Chat ID:
- {CUSTOMER_SERVICE_TELEGRAM_CHAT_ID or "TBC"}

Outlet Contacts:
{chr(10).join(outlet_lines)}
"""


# HISTORY / FLOW

def reset_history(chat_id: str) -> None:
    CHAT_HISTORY.pop(chat_id, None)


def add_history(chat_id: str, role: str, content: str) -> None:
    CHAT_HISTORY.setdefault(chat_id, []).append({"role": role, "content": content})
    CHAT_HISTORY[chat_id] = CHAT_HISTORY[chat_id][-20:]


def recent_history_text(chat_id: str, limit: int) -> str:
    return "\n".join(
        f"{item['role'].upper()}: {item['content']}"
        for item in CHAT_HISTORY.get(chat_id, [])[-limit:]
    )


def set_flow(chat_id: str, stage_name: str, **data: str) -> None:
    FLOW_STATE[chat_id] = {
        "stage": stage_name,
        **data,
    }


def get_flow(chat_id: str) -> Dict[str, str]:
    return FLOW_STATE.get(chat_id, {})


def get_flow_stage(chat_id: str) -> str:
    return FLOW_STATE.get(chat_id, {}).get("stage", "")


def clear_flow(chat_id: str) -> None:
    FLOW_STATE.pop(chat_id, None)


FLOW_QUESTION_BUILDERS = {}


def repeat_current_flow_question(chat_id: str) -> str:
    builder = FLOW_QUESTION_BUILDERS.get(get_flow_stage(chat_id))
    return builder(get_flow(chat_id)) if builder else main_menu_text()


def queue_pending_handoff(chat_id: str, user_message: str, clean_answer: str) -> str:
    PENDING_HANDOFFS[chat_id] = {
        "user_message": user_message,
        "clean_answer": clean_answer,
    }
    set_flow(chat_id, "pending_handoff_outlet")
    return ask_outlet_before_handoff_text()


def summary_text(title: str, fields: Dict[str, str]) -> str:
    lines = [
        "I’ll pass this to our Customer Service team.",
        "",
        title,
    ]
    lines.extend(f"- {label}: {value}" for label, value in fields.items())
    return "\n".join(lines)


def customer_service_summary(topic: str, outlet: str, message: str) -> str:
    return summary_text(
        "Summary:",
        {
            "Topic": topic,
            "Outlet": outlet or "Not specified",
            "Message": message,
        },
    )


def support_handoff_text(clean_answer: str) -> str:
    lines = (clean_answer or "").splitlines()

    while lines and not lines[0].strip():
        lines.pop(0)

    return "\n".join(lines).strip()


def customer_live_chat_close_hint() -> str:
    return "To close this chat, type CLOSE CHAT, END CHAT, or DONE."


def add_live_support_close_hint(text: str) -> str:
    clean = (text or "").strip()

    if not clean:
        return customer_live_chat_close_hint()

    if "close this chat" in clean.lower():
        return clean

    return f"{clean}\n\n{customer_live_chat_close_hint()}"


def send_handoff_result(chat_id: str, clean_answer: str, outlet: str, success_text: str, failure_text: str) -> str:
    sent = send_customer_service_handoff_to_telegram(chat_id, clean_answer, outlet)
    result_text = add_live_support_close_hint(success_text) if sent else failure_text
    return f"{clean_answer}\n\n{result_text}"


def reset_chat_state(
    chat_id: str,
    *,
    include_trial: bool = False,
    include_inactivity: bool = False,
) -> None:
    reset_history(chat_id)
    PENDING_HANDOFFS.pop(chat_id, None)
    clear_flow(chat_id)
    OUTLET_CONTEXT.pop(chat_id, None)
    STAFF_LIST_PENDING.pop(chat_id, None)
    close_live_support_chat(chat_id)

    if include_trial:
        TRIAL_BOOKINGS.pop(chat_id, None)

    if include_inactivity:
        clear_inactivity_state(chat_id)


def add_menu_hint(reply: str) -> str:
    if "Reply MENU to return to the main menu." in reply:
        return reply

    return reply.rstrip() + "\n\nReply MENU to return to the main menu."


JAL_YOGA_WEBSITE_URL_RE = re.compile(r"https?://(?:www\.)?jalyoga\.com\.sg/\S*", re.IGNORECASE)


def remove_jal_yoga_website_urls(reply: str) -> str:
    lines = []

    for line in reply.splitlines():
        cleaned = JAL_YOGA_WEBSITE_URL_RE.sub("", line).rstrip()

        if line.strip() and not cleaned.strip():
            continue

        lines.append(cleaned)

    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def finish_reply(chat_id: str, user_text: str, reply: str, add_menu: bool = True) -> str:
    reply = remove_jal_yoga_website_urls(reply)
    
    # NEW: Automatically remove any markdown bolding stars from the AI's reply
    reply = reply.replace("**", "")
    
    final_reply = add_menu_hint(reply) if add_menu else reply

    add_history(chat_id, "user", user_text)
    add_history(chat_id, "assistant", final_reply)

    return final_reply


def start_flow_reply(chat_id: str, user_text: str, stage: str, reply: str) -> str:
    set_flow(chat_id, stage)
    return finish_reply(chat_id, user_text, reply)


def next_flow_reply(chat_id: str, stage: str, reply: str, **data: str) -> str:
    set_flow(chat_id, stage, **data)
    return reply


# SAFETY / INTENT HELPERS

def is_opt_out_request(text: str) -> bool:
    t = normalize(text)
    return t in OPT_OUT_WORDS or any(phrase in t for phrase in OPT_OUT_WORDS if " " in phrase or "-" in phrase)


def is_opt_in_request(text: str) -> bool:
    return normalize(text) in OPT_IN_WORDS


def is_reset_request(text: str) -> bool:
    return normalize(text) in RESET_WORDS


def contains_sensitive_keyword(text: str) -> bool:
    t = normalize(text)
    return any(keyword in t for keyword in SENSITIVE_KEYWORDS)


def is_meaning_question(text: str) -> bool:
    t = normalize(text)
    raw = (text or "").strip()

    phrases = [
        "what mean",
        "what does this mean",
        "what does it mean",
        "what do you mean",
        "meaning",
        "什么意思",
        "什麼意思",
        "什么 意思",
        "apa maksud",
        "maksudnya",
        "maksud",
    ]

    return any(phrase in t or phrase in raw for phrase in phrases)


def is_class_cancellation_request(text: str) -> bool:
    t = normalize(text)

    patterns = [
        r"\b(cancel|cancle|cancell|cancelled|canceled|cancelling|canceling)\b.*\b(class|booking|session|lesson)\b",
        r"\b(class|booking|session|lesson)\b.*\b(cancel|cancle|cancell|cancelled|canceled|cancelling|canceling)\b",
        r"\bclass cancellation\b",
        r"\bcancel class\b",
        r"\bcancel my class\b",
        r"\bcancel my booking\b",
        r"\blate cancellation\b",
        r"\blate cancel\b",
        r"\bno show\b",
        r"\bno-show\b",
        r"\bmissed my class\b",
        r"\bi cannot attend\b",
        r"\bi can't attend\b",
        r"\bi cant attend\b",
        r"\bi wana cancel\b",
        r"\bi wanna cancel\b",
        r"\bi want cancel\b",
        r"\bwant to cancel\b",
        r"\bneed to cancel\b",
    ]

    return any(re.search(pattern, t, flags=re.IGNORECASE) for pattern in patterns)


CUSTOMER_SERVICE_EXACT_WORDS = set(phrase_list(
    "cs|support|helpdesk|客服|人工|人工客服|真人|真人客服|客户服务|客戶服務|我要客服|我要人工|转人工|轉人工|联系人工|聯繫人工|hubungi admin|admin|khidmat pelanggan|layanan pelanggan|bantuan manusia|சேவை|உதவி"
))
CUSTOMER_SERVICE_TEXT_PHRASES = phrase_list(
    "customer service|customer support|customer care|customer servide|customer servis|cust service|cs team|support team|contact support|contact customer service|speak to customer service|talk to customer service|talk to someone|speak to someone|talk to a human|speak to a human|human agent|real person|live agent|live chat|need human|need staff help|talk to staff|speak to staff|connect me to staff|connect me to customer service|can i talk to someone|can i speak to someone|i want customer service|i need customer service|i want to talk to staff|i need to talk to staff"
)
CUSTOMER_SERVICE_RAW_PHRASES = phrase_list(
    "客服|人工客服|真人客服|客户服务|客戶服務|联系工作人员|聯繫工作人員|联系人工|聯繫人工|转人工|轉人工|找人|我要找人|我要人工|我要客服|我想找客服|可以找客服吗|可以找客服嗎|我要和人说话|我要和人說話|khidmat pelanggan|layanan pelanggan|servis pelanggan|customer servis|bercakap dengan staff|bercakap dengan manusia|cakap dengan orang|nak cakap dengan staff|nak customer service|saya mahu customer service|saya nak bantuan manusia|hubungi admin|hubungi staf|bantuan admin|வாடிக்கையாளர் சேவை|வாடிக்கையாளர் ஆதரவு|மனித உதவி|ஒருவரிடம் பேச|உதவி வேண்டும்|staff உடன் பேச"
)
CUSTOMER_SERVICE_FUZZY_TARGETS = phrase_list(
    "customer service|customer support|customer care|support team|human agent|real person|live agent|talk to someone|speak to someone|talk to staff|speak to staff"
)


def is_customer_service_request(text: str) -> bool:
    t = normalize(text)
    clean = simple_text(text)
    raw = (text or "").strip().lower()

    if not raw:
        return False

    if t in CUSTOMER_SERVICE_EXACT_WORDS or raw in CUSTOMER_SERVICE_EXACT_WORDS:
        return True

    if any(phrase in t for phrase in CUSTOMER_SERVICE_TEXT_PHRASES):
        return True

    if any(phrase in raw for phrase in CUSTOMER_SERVICE_RAW_PHRASES):
        return True

    return fuzzy_phrase_match(clean, CUSTOMER_SERVICE_FUZZY_TARGETS, threshold=0.78)


def is_customer_service_contact_request(text: str) -> bool:
    clean = simple_text(text)

    if not clean or not is_customer_service_request(text):
        return False

    contact_detail_words = {"phone", "number", "whatsapp", "hotline", "call"}

    if any(word in clean.split() for word in contact_detail_words):
        return True

    is_follow_up = any(phrase in clean for phrase in ["what about", "how about"])
    handoff_words = {"talk", "speak", "connect", "human", "agent", "staff", "live"}

    return is_follow_up and not any(word in clean.split() for word in handoff_words)


def is_outlet_contact_request(text: str) -> bool:
    clean = simple_text(text)

    contact_words = {
        "contact",
        "phone",
        "number",
        "whatsapp",
        "call",
        "hotline",
    }

    # By splitting the text into individual words first, 
    # the bot won't accidentally trigger on words like "called" or "recall".
    return any(word in clean.split() for word in contact_words)


# LANGUAGE
LANGUAGE_SWITCH_WORDS = phrase_list("change|switch|translate|reply|speak|use|turn|make|convert|show|display|back|chnage|chage")
LANGUAGE_SWITCH_EXACT = {
    "English": set(phrase_list("english|eng|back english|back to english|switch back to english|speak english|reply english|reply in english|use english|change to english|change into english|change it to english|change it into english|english please")),
    "Chinese": set(phrase_list("chinese|中文|华文|華文|mandarin|speak chinese|reply chinese|reply in chinese|use chinese|change to chinese|change into chinese|change it to chinese|change it into chinese|translate to chinese|translate into chinese|chinese please")),
    "Malay": set(phrase_list("malay|bahasa melayu|reply malay|reply in malay|use malay|change to malay|change into malay|change it to malay|change it into malay|translate to malay|malay please")),
    "Tamil": set(phrase_list("tamil|தமிழ்|reply tamil|reply in tamil|use tamil|change to tamil|change into tamil|change it to tamil|change it into tamil|translate to tamil|tamil please")),
    "Thai": set(phrase_list("thai|ภาษาไทย|speak thai|reply thai|reply in thai|use thai|change to thai|change into thai|change it to thai|change it into thai|translate to thai|translate into thai|show in thai|show this in thai|can show this in thai|thai please")),
}
LANGUAGE_SWITCH_KEYWORDS = {
    "English": (phrase_list("english|eng"), []),
    "Chinese": (phrase_list("chinese|中文|华文|華文|mandarin"), phrase_list("chinese|中文|华文|華文|mandarin")),
    "Malay": (phrase_list("malay|bahasa melayu|melayu"), []),
    "Tamil": (phrase_list("tamil|தமிழ்"), phrase_list("tamil|தமிழ்")),
    "Thai": (phrase_list("thai"), phrase_list("ภาษาไทย")),
}
LANGUAGE_NAME_ALIASES = {
    "arabic": "Arabic",
    "bahasa indonesia": "Indonesian",
    "bahasa melayu": "Malay",
    "chinese": "Chinese",
    "english": "English",
    "eng": "English",
    "filipino": "Filipino",
    "french": "French",
    "german": "German",
    "hindi": "Hindi",
    "indonesian": "Indonesian",
    "italian": "Italian",
    "japanese": "Japanese",
    "korean": "Korean",
    "malay": "Malay",
    "mandarin": "Chinese",
    "portuguese": "Portuguese",
    "russian": "Russian",
    "spanish": "Spanish",
    "tagalog": "Filipino",
    "tamil": "Tamil",
    "thai": "Thai",
    "vietnamese": "Vietnamese",
}
LANGUAGE_SCRIPT_PATTERNS = [
    ("Japanese", r"[\u3040-\u30ff]"),
    ("Korean", r"[\uac00-\ud7af]"),
    ("Thai", r"[\u0e00-\u0e7f]"),
    ("Tamil", r"[\u0b80-\u0bff]"),
    ("Hindi", r"[\u0900-\u097f]"),
    ("Arabic", r"[\u0600-\u06ff]"),
    ("Chinese", r"[\u4e00-\u9fff]"),
]
LANGUAGE_PHRASE_HINTS = {
    "English": phrase_list("hi|hello|hey|thank you|thanks|good morning|good afternoon|good evening|can i|i want|i need|what is|how much|schedule|trial|membership|customer service"),
    "Portuguese": phrase_list("salve|ola|olá|oi|bom dia|boa tarde|boa noite|obrigado|obrigada"),
    "Spanish": phrase_list("hola|buenos dias|buenas tardes|buenas noches|gracias|por favor"),
    "French": phrase_list("bonjour|bonsoir|merci|s il vous plait|s'il vous plait|s'il te plait"),
    "German": phrase_list("guten morgen|guten tag|danke|bitte"),
    "Vietnamese": phrase_list("xin chao|xin chào|cam on|cảm ơn"),
    "Indonesian": phrase_list("halo|terima kasih|selamat siang|selamat malam"),
    "Malay": phrase_list("salam|selamat pagi|apa khabar"),
}
LANGUAGE_NEUTRAL_STAGES = {
    "trial_name",
    "refer_friend_name",
    "refer_friend_contact",
    "corporate_name",
    "corporate_email",
    "staff_name",
    "staff_room",
}


def detect_language_switch_request(text: str) -> str:
    t = normalize(text)
    clean = simple_text(text)
    raw = text.strip()
    padded_clean = f" {clean} "

    for language, exact_words in LANGUAGE_SWITCH_EXACT.items():
        if t in exact_words:
            return language

    # Natural follow-ups like "back to english" should be treated as a language switch.
    for alias, language in LANGUAGE_NAME_ALIASES.items():
        if clean in {f"back to {alias}", f"switch back to {alias}", f"reply back in {alias}"}:
            return language

    if any(word in clean.split() for word in LANGUAGE_SWITCH_WORDS):
        for language, (clean_words, raw_words) in LANGUAGE_SWITCH_KEYWORDS.items():
            if any(word in clean for word in clean_words) or any(word in raw for word in raw_words):
                return language

        for alias, language in LANGUAGE_NAME_ALIASES.items():
            if f" {alias} " in padded_clean:
                return language

    return ""


def detect_language_from_script(text: str) -> str:
    for language, pattern in LANGUAGE_SCRIPT_PATTERNS:
        if re.search(pattern, text or ""):
            return language

    return ""


def detect_language_from_phrase(text: str) -> str:
    norm = normalize(text)
    clean = simple_text(text)
    padded_norm = f" {norm} "
    padded_clean = f" {clean} "

    for language, phrases in LANGUAGE_PHRASE_HINTS.items():
        for phrase in phrases:
            phrase_norm = normalize(phrase)
            phrase_clean = simple_text(phrase)

            if norm == phrase_norm or phrase_norm and f" {phrase_norm} " in padded_norm:
                return language

            if clean == phrase_clean or phrase_clean and f" {phrase_clean} " in padded_clean:
                return language

    return ""


def remember_user_language(chat_id: str, language: str) -> str:
    USER_LANGUAGE[chat_id] = language
    return language


def should_keep_current_language(chat_id: str, user_text: str) -> bool:
    text = (user_text or "").strip()
    norm = normalize(text)

    if not text or norm.isdigit():
        return True

    if (
        norm in RESET_WORDS
        or norm in OPT_IN_WORDS
        or norm in OPT_OUT_WORDS
        or norm in SUPPORT_CLOSE_WORDS
        or is_customer_close_text(text)
    ):
        return True

    if is_support_command_text(norm) or detect_outlet_from_text(text):
        return True

    if get_flow_stage(chat_id) in LANGUAGE_NEUTRAL_STAGES:
        return True

    return False


def detect_user_language(chat_id: str, user_text: str) -> str:
    text = normalize(user_text)
    current_language = USER_LANGUAGE.get(chat_id, "English")

    script_language = detect_language_from_script(user_text)
    if script_language:
        return remember_user_language(chat_id, script_language)

    phrase_language = detect_language_from_phrase(user_text)
    if phrase_language:
        return remember_user_language(chat_id, phrase_language)

    if should_keep_current_language(chat_id, user_text) or len(text) <= 2:
        return current_language

    if not client:
        return current_language

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=(
                "Detect the dominant language of the user's message. "
                "Return only the language name in English. "
                "Examples: English, Chinese, Malay, Tamil, Thai, Portuguese, Spanish, French, Japanese, Korean. "
                "If the message is only a name, number, outlet, email, phone number, command, menu choice, or unclear, return Unknown."
            ),
            input=user_text,
        )

        language = (response.output_text or "").strip()

        if language and language.lower() != "unknown":
            return remember_user_language(chat_id, language)

    except Exception as e:
        print("LANGUAGE DETECT ERROR:", str(e), flush=True)

    return current_language


# LLM REPLIES

def openai_text_reply(instructions: str, user_text: str, fallback: str, error_label: str, show_traceback: bool = True) -> str:
    if not client:
        return fallback

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=instructions,
            input=user_text,
        )

        reply = (response.output_text or "").strip()
        if reply:
            return reply

    except Exception as e:
        print(f"{error_label}:", str(e), flush=True)

        if show_traceback:
            traceback.print_exc()

    return fallback


def knowledge_reply(chat_id: str, user_text: str, task: str, fallback: str = "") -> str:
    if not client:
        return fallback or AI_NOT_CONFIGURED_REPLY

    language = detect_user_language(chat_id, user_text)
    history_text = recent_history_text(chat_id, 8)

    instructions = f"""
You are Jal Yoga Singapore's Telegram customer-service assistant.

Use ONLY:
1. The Jal Yoga website content below.
2. The live customer-service config below.
3. The recent chat context below.

Language rule:
- Customer language: {language}
- Translate all customer-facing wording into the customer language where possible.
- If the customer message is only a number, keep replying in the stored customer language.
- Preserve menu numbers, phone numbers, Telegram IDs, WhatsApp links, and formatting.
- Do not add information that is not in the Jal Yoga website content.

Core rules:
- Help only with Jal Yoga enquiries.
- Speak naturally and confidently. NEVER use phrases like "The website says" or "According to the text". Just state the facts directly.
- Format neatly: Use line breaks between different topics. ALWAYS use short bullet points when listing items.
- Answer ONLY what the customer specifically asks for. Keep answers short and direct.
- Do not add extra details like address, facilities, amenities, or parking unless explicitly requested.
- Do not invent prices, promotions, schedules, trainers, live slots, outlet phone numbers, policies, staff information, or membership details.
- Do not give Jal Yoga website URLs to customers.
- If the exact answer is not clearly available, say you are not fully sure and use [HANDOFF].
- Ask only one question at a time.
- Do not mention Meta, webhook, Python, OpenAI, code, or internal system details.

Source priority:
- The Jal Yoga website content is the PRIMARY source. Always use it first.
- The supplementary knowledge section is a backup. Use it ONLY for details that are not covered by the website content (such as conversation flows, studio policies, suspension fees, refer-a-friend, and handoff wording).
- If the website and the supplement disagree on a fact, trust the website content.

Live customer-service config:
{live_contact_config_text()}

Jal Yoga website content:
{KNOWLEDGE_TEXT}

Recent chat:
{history_text}

Task:
{task}
"""

    return openai_text_reply(
        instructions,
        user_text,
        fallback or AI_NOT_SURE_HANDOFF_REPLY,
        "KNOWLEDGE REPLY ERROR",
    )

def ask_llm(chat_id: str, user_text: str) -> str:
    if not client:
        return AI_NOT_CONFIGURED_REPLY

    language = detect_user_language(chat_id, user_text)
    history_text = recent_history_text(chat_id, 12)

    instructions = f"""
You are Jal Yoga Singapore's Telegram customer-service assistant.

Use ONLY:
1. The Jal Yoga website content below.
2. The live customer-service config below.
3. The recent chat context below.

Source priority:
- The Jal Yoga website content is the PRIMARY source. Always use it first.
- The supplementary knowledge section is a backup. Use it ONLY for details not covered by the website content (such as conversation flows, studio policies, suspension fees, refer-a-friend, and handoff wording).
- If the website and the supplement disagree on a fact, trust the website content.

Language:
- Customer language: {language}
- Reply in the customer's language where possible.
- Preserve menu numbers, phone numbers, Telegram IDs, WhatsApp links, and formatting.

Core rules:
- Help only with Jal Yoga enquiries.
- Speak naturally and confidently. NEVER use phrases like "The website says" or "According to the text". Just state the facts directly.
- Format neatly: Use line breaks between different topics. ALWAYS use short bullet points when listing items.
- Answer ONLY what the customer specifically asks for. Keep answers short and direct.
- Do not add extra details like address, facilities, amenities, or parking unless explicitly requested.
- Do not invent prices, promotions, schedules, trainers, live slots, outlet phone numbers, policies, staff information, or membership details.
- Do not give Jal Yoga website URLs to customers.
- If the exact answer is not clearly available, say you are not fully sure and use [HANDOFF].
- Ask only one question at a time.
- Do not mention Meta, webhook, Python, OpenAI, code, or internal system details.
Customer Service handoff format:

I’ll pass this to our Customer Service team.

Summary:
- Topic: <topic>
- Outlet: <outlet or Not specified>
- Message: <user message>

[HANDOFF]

Do not mention:
- Meta
- webhook
- OpenAI
- Python
- code
- internal system details

Live config:
{live_contact_config_text()}

Jal Yoga website content:
{KNOWLEDGE_TEXT}

Current time in Singapore:
{now_sg()}

Recent chat:
{history_text}
"""

    return openai_text_reply(instructions, user_text, AI_NOT_SURE_HANDOFF_REPLY, "OPENAI ERROR")

def strip_handoff_token(text: str) -> str:
    return text.replace("[HANDOFF]", "").strip()


def add_customer_service_id_note(reply: str, chat_id: str) -> str:
    triggers = [
        "Trial Booking Summary:",
        "Updated Trial Booking Summary:",
        "Refer-a-Friend Summary:",
        "Corporate / Partnership Summary:",
        "Corporate/Partnership Summary:",
        "Staff Hub Summary:",
    ]

    if any(trigger in reply for trigger in triggers):
        return (
            f"{reply}\n\n"
            "If you need further assistance, please quote this Customer Service ID:\n"
            f"{chat_id}"
        )

    return reply


# MENU TEXT

def main_menu_text() -> str:
    return (
        "Namaste! Thank you for reaching out to Jal Yoga. 🙏\n\n"
        "To help us handle your request as quickly as possible, please let us know what you're looking for today:\n\n"
        "1. Schedule a Trial\n"
        "2. I’m a current member\n"
        "3. I’d like to find out more about Jal Yoga\n"
        "4. Corporate/Partnerships\n"
        "5. Staff Hub\n\n"
        "You can also type CUSTOMER SERVICE anytime, in any language, to speak to our team.\n"
        "Reply STOP anytime to stop receiving follow-up messages."
    )


def current_member_menu_text() -> str:
    return (
        "Welcome back! Hope your practice is going well. 🙏\n\n"
        "How can I help you with your membership today?\n\n"
        "1. Class Cancellation\n"
        "2. Membership Suspension\n"
        "3. I need help with my class booking\n"
        "4. I would like to refer a friend"
    )


def general_enquiry_menu_text() -> str:
    return (
        "General Enquiry 🙏\n\n"
        "You may ask me anything about Jal Yoga, such as:\n"
        "- class types\n"
        "- studio locations\n"
        "- operating hours\n"
        "- trial classes\n"
        "- memberships\n"
        "- facilities\n"
        "- schedules\n"
        "- events or retreats\n\n"
        "What would you like to know?"
    )


def ask_outlet_before_handoff_text() -> str:
    return (
        "Before I pass this to our Customer Service team, do you have a specific outlet for this enquiry?\n\n"
        "Please reply with one of these:\n"
        f"{studio_options_text(include_not_specified=True)}"
    )


def trial_outlet_question() -> str:
    return TRIAL_STUDIO_QUESTION


def trial_start_text() -> str:
    return (
        "Sure — let’s schedule your trial class. 🙏\n\n"
        f"{trial_outlet_question()}"
    )


def trial_change_outlet_question() -> str:
    return studio_prompt("Which studio would you like to change the trial booking to?")


def nearest_outlet_location_question() -> str:
    return (
        "Sure, please type your location, postal code, MRT station, or area in Singapore, "
        "and I will suggest the nearest Jal Yoga outlet."
    )


def nearest_outlet_action_prompt(outlet: str) -> str:
    return (
        "Would you like to:\n"
        f"1. Schedule a trial class at {outlet}\n"
        f"2. Book a regular class at {outlet}"
    )


def format_nearest_outlet_reply(recommendation: Dict[str, Any]) -> str:
    location = recommendation["location"]
    top = recommendation["ranked_studios"][0]
    outlet = top["name"]

    lines = [
        (
            f"Based on {location['label']}, the nearest Jal Yoga outlet is likely "
            f"{outlet}."
        ),
        "",
        "Address:",
        top["address"],
    ]

    lines.extend(["", nearest_outlet_action_prompt(outlet)])
    return "\n".join(lines)


def format_trial_nearest_outlet_reply(recommendation: Dict[str, Any]) -> str:
    location = recommendation["location"]
    top = recommendation["ranked_studios"][0]
    outlet = top["name"]

    return (
        f"Based on {location['label']}, {outlet} looks nearest. "
        f"I will use {outlet} for your trial.\n\n"
        "May I have your full name?"
    )


def regular_class_booking_guidance(outlet: str) -> str:
    return (
        f"For regular classes at {outlet}, please book through the Jal Yoga App up to 3 days in advance.\n\n"
        "If you are new, I recommend starting with a trial class first.\n\n"
        "Reply 1 and I will schedule a trial here, or type CUSTOMER SERVICE if you need help with a booking."
    )


def friend_studio_question() -> str:
    return studio_prompt("Which studio would your friend prefer?")


def outlet_number_question() -> str:
    return studio_prompt("Please choose an outlet by number or name:")


def corporate_message_question() -> str:
    return (
        "Thank you. Please briefly tell us what your Corporate / Partnership enquiry is about.\n\n"
        "For example:\n"
        "- corporate wellness programme\n"
        "- company yoga class\n"
        "- partnership proposal\n"
        "- event collaboration"
    )


def staff_booking_details_question() -> str:
    return (
        "Please share the member and booking details.\n\n"
        "For example:\n"
        "- Member name\n"
        "- Booking date and time\n"
        "- Class name\n"
        "- Issue or request"
    )


def trial_goal_question(flow: Dict[str, str]) -> str:
    name = flow.get("name", "")

    if name:
        return f"Thanks, {name.title()} — what’s your fitness goal for the trial?"

    return "What’s your fitness goal for the trial?"


def class_cancellation_policy(next_step: str) -> str:
    return (
        "Class Cancellation Policy 🙏\n\n"
        "You can cancel a booked class without penalty up to 2 hours before the class starts.\n\n"
        "After that:\n"
        "- Cancellations made less than 2 hours before class are late cancellations\n"
        "- No-shows are also counted as late cancellations\n"
        "- After 3 late cancellations, booking access may be suspended for 7 calendar days\n\n"
        f"{next_step}"
    )


def membership_suspension_policy(next_step: str) -> str:
    return (
        "Membership Suspension 🙏\n\n"
        "Here is a quick overview before we proceed:\n\n"
        "Medical Suspension:\n"
        "- The extension fee can be waived with valid documentation from a certified physician\n"
        "- Processed in blocks of one month\n"
        "- Your membership expiry is adjusted once the documentation is verified\n\n"
        "Travel / Non-Medical Suspension:\n"
        "- An extension fee of S$50 per month applies\n"
        "- Processed in one-month blocks, up to a maximum of 3 months\n"
        "- Your membership expiry is extended once the fee is processed\n\n"
        f"{next_step}"
    )


def studio_locations_text() -> str:
    outlet_label = "outlet" if len(STUDIOS) == 1 else "outlets"
    lines = [f"We have {len(STUDIOS)} {outlet_label}:", ""]

    for index, studio in enumerate(STUDIOS, start=1):
        lines.extend([f"{index}. {studio['name']}", studio["address"], ""])

    lines.append("For the latest operating hours, please contact Customer Service.")
    return "\n".join(lines).strip()


FLOW_QUESTION_BUILDERS.update(
    {
        "main_menu": lambda _flow: main_menu_text(),
        "current_member_menu": lambda _flow: current_member_menu_text(),
        "general_enquiry_menu": lambda _flow: general_enquiry_menu_text(),
        "trial_outlet": lambda _flow: trial_outlet_question(),
        "trial_name": lambda _flow: "May I have your full name?",
        "trial_goal": trial_goal_question,
        "trial_change_outlet": lambda _flow: trial_change_outlet_question(),
        "nearest_outlet_location": lambda _flow: nearest_outlet_location_question(),
        "nearest_outlet_action": lambda flow: nearest_outlet_action_prompt(flow.get("recommended_outlet", "the recommended outlet")),
        "refer_friend_name": lambda _flow: "That’s wonderful — what is your friend’s full name?",
        "refer_friend_contact": lambda _flow: "Thanks — what is your friend’s contact number?",
        "refer_friend_studio": lambda _flow: friend_studio_question(),
        "corporate_name": lambda _flow: "Sure — may I have your full name?",
        "corporate_email": lambda _flow: "Thanks. What is your email address?",
        "corporate_message": lambda _flow: corporate_message_question(),
        "staff_name": lambda _flow: "Staff Hub 🙏\n\nMay I have the staff name?",
        "staff_studio": lambda _flow: studio_prompt("Which studio is this related to?"),
        "staff_room": lambda _flow: "Which room is this related to?",
        "staff_member_booking_details": lambda _flow: staff_booking_details_question(),
        "contact_outlet": lambda _flow: outlet_number_question(),
        "pending_handoff_outlet": lambda _flow: ask_outlet_before_handoff_text(),
        "event_outlet": lambda _flow: studio_prompt("Which studio would you like to check for events and workshops?"),
    }
)


MAIN_MENU_CHOICES = {
    "1": ("trial_outlet", trial_start_text),
    "2": ("current_member_menu", current_member_menu_text),
    "3": ("general_enquiry_menu", general_enquiry_menu_text),
    "4": ("corporate_name", lambda: "Sure — may I have your full name?"),
    "5": ("staff_name", lambda: "Staff Hub 🙏\n\nMay I have the staff name?"),
}


CURRENT_MEMBER_CHOICES = {
    "1": (
        "member_cancel_details",
        lambda: class_cancellation_policy(
            "If you want Customer Service to help with a specific booked class, please reply with:\n"
            "- Outlet\n"
            "- Class name\n"
            "- Date and time\n\n"
            "After you send the details, I’ll connect you to Customer Service here."
        ),
    ),
    "2": (
        "member_suspension_details",
        lambda: membership_suspension_policy(
            "To continue, please reply with:\n"
            "- Medical Suspension or Non-Medical / Travel Suspension\n"
            "- Your preferred outlet, if any\n"
            "- Any important details\n\n"
            "After you send the details, I’ll connect you to Customer Service here."
        ),
    ),
    "3": (
        "member_booking_issue_details",
        lambda: (
            "Sure — I’ll connect you to Customer Service for class booking help. 🙏\n\n"
            "Please tell me what issue you’re facing, for example:\n"
            "- cannot book a class\n"
            "- class is full\n"
            "- need help checking a schedule\n"
            "- app booking issue\n\n"
            "Please include the outlet if you know it."
        ),
    ),
    "4": (
        "refer_friend_name",
        lambda: "That’s wonderful — what is your friend’s full name?",
    ),
}


MEMBER_SERVICE_STAGES = {
    "member_cancel_details": (
        "Class Cancellation",
        "Please share the outlet, class name, date, and time for the class you want help with.",
    ),
    "member_suspension_details": (
        "Membership Suspension",
        "Please share whether this is medical or non-medical/travel suspension, plus your preferred outlet if any.",
    ),
    "member_booking_issue_details": (
        "Class Booking Help",
        "Please share the booking issue and outlet if you know it.",
    ),
}


GENERAL_ENQUIRY_KNOWLEDGE_CHOICES = {
    "2": (
        "The customer selected General Enquiry > Class Types. "
        "Explain Jal Yoga class types from the knowledge file. "
        "Keep it beginner-friendly and concise.",
        (
            "Jal Yoga offers beginner-friendly Yoga, Pilates, Barre, and wellness classes.\n\n"
            "Please type your question and I’ll help based on the information I have."
        ),
    ),
    "3": (
        "The customer selected General Enquiry > Current Events & Retreat. "
        "Share current Jal Yoga events and retreats from the knowledge file. "
        "If there are no confirmed events or retreats, say customers can contact Customer Service for the latest updates.",
        "For the latest events and retreats, please contact Customer Service.",
    ),
}


def start_customer_service_flow(chat_id: str, text: str) -> str:
    outlet = detect_outlet_choice(text) or get_flow_outlet(chat_id)
    clean_answer = customer_service_summary("Customer Service Request", outlet, text)

    if not outlet:
        return queue_pending_handoff(chat_id, text, clean_answer)

    remember_outlet_context(chat_id, outlet)
    clear_flow(chat_id)
    return send_handoff_result(
        chat_id,
        clean_answer,
        outlet,
        (
            f"I’ve sent this summary to our {outlet} Customer Service team on Telegram. 🙏\n\n"
            "You are now connected to Customer Service. You may reply here and our team will receive your message."
        ),
        "Customer Service Telegram chat is not configured yet.",
    )


# TELEGRAM SEND

def split_long_message(text: str, limit: int = 3900) -> List[str]:
    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""

    for line in text.splitlines():
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current += "\n" + line if current else line

    if current:
        chunks.append(current)

    return chunks


def send_telegram_message(chat_id: str, message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        print("Missing TELEGRAM_BOT_TOKEN", flush=True)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    for chunk in split_long_message(message):
        response = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            },
            timeout=30,
        )

        print("TELEGRAM SEND STATUS:", response.status_code, flush=True)
        print("TELEGRAM SEND RESPONSE:", response.text, flush=True)

        response.raise_for_status()

        write_chatlog(
            chat_id,
            "outgoing",
            "bot",
            chunk,
            {"platform": "telegram"},
        )

    return True



# LIVE CUSTOMER SERVICE CHAT

def get_customer_service_chat_ids() -> set:
    chat_ids = set()

    if CUSTOMER_SERVICE_TELEGRAM_CHAT_ID:
        chat_ids.add(str(CUSTOMER_SERVICE_TELEGRAM_CHAT_ID))

    for studio in STUDIOS:
        outlet_chat_id = outlet_telegram_chat_id(studio["name"])

        if outlet_chat_id:
            chat_ids.add(str(outlet_chat_id))

    return chat_ids


def is_customer_service_chat(chat_id: str) -> bool:
    return str(chat_id) in get_customer_service_chat_ids()


def get_support_target_chat_id(outlet: str = "") -> str:
    target_chat_id = outlet_telegram_chat_id(outlet) if outlet and outlet != "Not specified" else ""
    return str(target_chat_id or CUSTOMER_SERVICE_TELEGRAM_CHAT_ID or "")


def open_live_support_chat(customer_chat_id: str, target_chat_id: str, outlet: str = "Not specified") -> None:
    if not target_chat_id:
        return

    customer_chat_id = str(customer_chat_id)
    target_chat_id = str(target_chat_id)

    LIVE_SUPPORT_CHATS[customer_chat_id] = {
        "target_chat_id": target_chat_id,
        "outlet": outlet or "Not specified",
        "last_active_at": now_sg(),
    }

    # Make this customer the active customer for this Customer Service chat.
    # This allows Customer Service to just type normally to reply.
    SUPPORT_ACTIVE_CUSTOMER[target_chat_id] = customer_chat_id


def support_reply_instructions(customer_chat_id: str) -> str:
    return (
        "Reply here to message this customer.\n"
        f"Backup: /reply {customer_chat_id} your message\n"
        "Close: close"
    )


def is_support_command_text(lower: str) -> bool:
    return lower.startswith(("/reply", "/close"))


def support_reply_result(chat_id: str, customer_chat_id: str, clean: str, lower: str) -> str:
    if lower in SUPPORT_CLOSE_WORDS:
        return close_customer_chat_from_support(chat_id, customer_chat_id)

    return send_support_message_to_customer(chat_id, customer_chat_id, clean)


def extract_customer_chat_id_from_support_text(text: str) -> str:
    patterns = [
        r"Customer Telegram Chat ID:\s*([-\d]+)",
        r"Referrer Telegram Chat ID:\s*([-\d]+)",
        r"/reply\s+([-\d]+)",
        r"/close\s+([-\d]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.IGNORECASE)

        if match:
            return match.group(1).strip()

    return ""


def close_live_support_chat(customer_chat_id: str) -> None:
    customer_chat_id = str(customer_chat_id)
    live_chat = LIVE_SUPPORT_CHATS.pop(customer_chat_id, {})
    target_chat_id = str(live_chat.get("target_chat_id", ""))

    if target_chat_id and SUPPORT_ACTIVE_CUSTOMER.get(target_chat_id) == customer_chat_id:
        SUPPORT_ACTIVE_CUSTOMER.pop(target_chat_id, None)


def close_live_support_chat_from_customer(customer_chat_id: str) -> None:
    customer_chat_id = str(customer_chat_id)
    live_chat = LIVE_SUPPORT_CHATS.get(customer_chat_id, {})
    target_chat_id = str(live_chat.get("target_chat_id", ""))

    close_live_support_chat(customer_chat_id)

    if not target_chat_id:
        return

    try:
        send_telegram_message(
            target_chat_id,
            f"Customer {customer_chat_id} has closed the live Customer Service chat.",
        )
    except Exception as e:
        print("CUSTOMER LIVE CHAT CLOSE NOTICE ERROR:", str(e), flush=True)


SUPPORT_CLOSE_WORDS = {"close", "close chat", "done", "resolved", "end chat"}
CUSTOMER_CLOSE_WORDS = {
    "close",
    "close chat",
    "close conversation",
    "close customer service",
    "close live chat",
    "close support",
    "close support chat",
    "done",
    "end",
    "resolved",
    "end chat",
    "end conversation",
    "end customer service",
    "end live chat",
    "end support",
    "end support chat",
    "finish",
    "finish chat",
    "finished",
    "all done",
    "all good",
    "im done",
    "i'm done",
    "i am done",
    "we are done",
    "ok all good",
    "okay all good",
    "thats all",
    "that's all",
    "that is all",
    "thats it",
    "that's it",
    "that is it",
    "that should be all",
    "nothing else",
    "nothing more",
    "no more questions",
    "no further questions",
    "no need",
    "no need already",
    "no thanks",
    "no thank you",
    "ok thanks",
    "okay thanks",
    "ok thank you",
    "okay thank you",
    "thanks thats it",
    "thanks that's it",
    "thanks that is it",
    "thank you thats all",
    "thank you that's all",
    "thank you that is all",
    "thank you thats it",
    "thank you that's it",
    "thank you that is it",
    "leave chat",
    "quit chat",
    "cancel chat",
    "cancel live chat",
    "stop live chat",
    "stop support chat",
    "exit live chat",
    "exit support chat",
    "bye",
    "bye bye",
    "goodbye",
    "good bye",
    "see you",
    "see ya",
    "cya",
    "talk to you later",
    "thank you bye",
    "thanks bye",
    "thank you goodbye",
    "thanks goodbye",
    "appreciate it bye",
    "ok bye",
    "okay bye",
    "ok thank you bye",
    "okay thank you bye",
    "thanks thats all",
    "thanks that's all",
}


def is_customer_close_text(text: str) -> bool:
    norm = normalize(text)
    clean = simple_text(text)
    return norm in CUSTOMER_CLOSE_WORDS or clean in CUSTOMER_CLOSE_WORDS


def customer_service_reply_text(message: str) -> str:
    return (
        "Jal Yoga Customer Service 🙏\n\n"
        f"{message}\n\n"
        f"{add_live_support_close_hint('You may reply here and our Customer Service team will receive your message.')}"
    )


def customer_service_closed_text() -> str:
    return (
        "Customer Service has closed this chat for now. 🙏\n\n"
        "If you need help again, type CUSTOMER SERVICE anytime."
    )


def close_customer_chat_from_support(support_chat_id: str, customer_chat_id: str) -> str:
    close_live_support_chat(customer_chat_id)
    SUPPORT_ACTIVE_CUSTOMER.pop(str(support_chat_id), None)
    send_telegram_message(customer_chat_id, customer_service_closed_text())
    return f"Closed live chat with customer {customer_chat_id}."


def send_support_message_to_customer(support_chat_id: str, customer_chat_id: str, message: str) -> str:
    customer_chat_id = str(customer_chat_id).strip()
    clean_message = message.strip()

    if not customer_chat_id or not clean_message:
        return "Please provide the customer chat ID and message."

    if contains_blocked_word(clean_message):
        return "Message blocked. Please use professional Customer Service language."

    send_telegram_message(customer_chat_id, customer_service_reply_text(clean_message))

    live_chat = LIVE_SUPPORT_CHATS.get(customer_chat_id, {})

    open_live_support_chat(
        customer_chat_id,
        str(support_chat_id),
        live_chat.get("outlet", "Customer Service"),
    )

    return f"Sent to customer {customer_chat_id}."


def handle_customer_service_reply_to_message(chat_id: str, text: str, reply_to_text: str) -> str:
    if not is_customer_service_chat(chat_id):
        return ""

    if not reply_to_text:
        return ""

    clean = text.strip()
    lower = clean.lower()

    if not clean:
        return ""

    # Let command handler handle these.
    if is_support_command_text(lower):
        return ""

    customer_chat_id = extract_customer_chat_id_from_support_text(reply_to_text)

    if not customer_chat_id:
        return ""

    return support_reply_result(chat_id, customer_chat_id, clean, lower)


def handle_customer_service_command(chat_id: str, text: str) -> str:
    clean = text.strip()
    lower = clean.lower()

    if not is_support_command_text(lower):
        return ""

    if not is_customer_service_chat(chat_id):
        return "Sorry, this command is only for Customer Service."

    if lower.startswith("/reply"):
        parts = clean.split(maxsplit=2)

        if len(parts) < 3:
            return (
                "Please use this format:\n\n"
                "/reply CUSTOMER_CHAT_ID your message\n\n"
                "Example:\n"
                "/reply 123456789 Hi, this is Jal Yoga Customer Service. How can I help?"
            )

        customer_chat_id = parts[1].strip()
        message = parts[2].strip()

        if not customer_chat_id or not message:
            return "Please use this format:\n\n/reply CUSTOMER_CHAT_ID your message"

        return send_support_message_to_customer(chat_id, customer_chat_id, message)

    if lower.startswith("/close"):
        parts = clean.split(maxsplit=1)

        if len(parts) < 2:
            return (
                "Please use this format:\n\n"
                "/close CUSTOMER_CHAT_ID\n\n"
                "Example:\n"
                "/close 123456789"
            )

        return close_customer_chat_from_support(chat_id, parts[1].strip())

    return ""


def handle_support_active_reply(chat_id: str, text: str) -> str:
    """Allow Customer Service to reply by just typing normally.

    The bot remembers the latest/active customer for each Customer Service chat.
    This is easier than typing /reply CUSTOMER_CHAT_ID message every time.
    """
    if not is_customer_service_chat(chat_id):
        return ""

    clean = text.strip()
    lower = clean.lower()

    if not clean:
        return ""

    # Let command handler handle these.
    if is_support_command_text(lower):
        return ""

    customer_chat_id = SUPPORT_ACTIVE_CUSTOMER.get(str(chat_id), "")

    if not customer_chat_id:
        return (
            "No active customer selected yet.\n\n"
            "Please wait for a handoff message, or use:\n"
            "/reply CUSTOMER_CHAT_ID your message"
        )

    return support_reply_result(chat_id, customer_chat_id, clean, lower)


def forward_customer_message_to_support(customer_chat_id: str, text: str) -> bool:
    live_chat = LIVE_SUPPORT_CHATS.get(customer_chat_id, {})
    target_chat_id = live_chat.get("target_chat_id", "") or CUSTOMER_SERVICE_TELEGRAM_CHAT_ID

    if not target_chat_id:
        return False

    open_live_support_chat(
        customer_chat_id,
        str(target_chat_id),
        live_chat.get("outlet", "Customer Service"),
    )

    message = (
        "Customer replied in live chat 🙏\n\n"
        f"Customer Telegram Chat ID: {customer_chat_id}\n\n"
        f"Message:\n{text}\n\n"
        f"{support_reply_instructions(customer_chat_id)}"
    )

    send_telegram_message(target_chat_id, message)
    return True


def send_support_notification(
    customer_chat_id: str,
    target_chat_id: str,
    outlet: str,
    message: str,
    error_label: str,
) -> bool:
    open_live_support_chat(customer_chat_id, target_chat_id, outlet)

    try:
        send_telegram_message(target_chat_id, message)
        return True
    except Exception as e:
        print(f"{error_label}:", str(e), flush=True)
        traceback.print_exc()
        return False


def send_outlet_support_notification(
    customer_chat_id: str,
    outlet: str,
    message: str,
    skip_label: str,
    error_label: str,
) -> bool:
    if not outlet:
        return False

    target_chat_id = get_support_target_chat_id(outlet)

    if not target_chat_id:
        print(f"{skip_label}: No target chat ID", flush=True)
        return False

    return send_support_notification(customer_chat_id, target_chat_id, outlet, message, error_label)


# CUSTOMER SERVICE HANDOFF

def send_customer_service_handoff_to_telegram(customer_chat_id: str, clean_answer: str, outlet: str) -> bool:
    target_chat_id = get_support_target_chat_id(outlet)

    if not target_chat_id:
        print(
            f"CUSTOMER SERVICE HANDOFF SKIPPED: No Telegram chat ID for outlet={outlet}",
            flush=True,
        )
        return False

    handoff_text = support_handoff_text(clean_answer)

    message = (
        "New Customer Service Handoff 🙏\n\n"
        f"{handoff_text}\n\n"
        f"Customer Telegram Chat ID: {customer_chat_id}\n\n"
        "This customer is now connected to live Customer Service chat.\n\n"
        f"{support_reply_instructions(customer_chat_id)}"
    )

    return send_support_notification(
        customer_chat_id,
        target_chat_id,
        outlet or "Not specified",
        message,
        "CUSTOMER SERVICE HANDOFF SEND ERROR",
    )


# TRIAL BOOKING

TRIAL_OUTLET_CHANGE_WORDS = phrase_list(
    "change|switch|move|transfer|instead|prefer|preferred|rather|chnage|chage"
)
TRIAL_OUTLET_CHANGE_CONTEXT_WORDS = phrase_list(
    "trial|trail|triel|outlet|studio"
)


def is_trial_outlet_change_request(chat_id: str, text: str) -> bool:
    has_trial_context = bool(TRIAL_BOOKINGS.get(chat_id)) or get_flow_stage(chat_id).startswith("trial_")

    if not has_trial_context:
        return False

    clean = simple_text(text)

    if not clean:
        return False

    has_change_word = any(word in clean for word in TRIAL_OUTLET_CHANGE_WORDS)
    has_context_word = any(word in clean for word in TRIAL_OUTLET_CHANGE_CONTEXT_WORDS)
    has_outlet = bool(detect_outlet_choice(text))

    if has_outlet:
        return has_change_word

    return has_change_word and has_context_word


def send_trial_change_notice_to_previous_outlet(
    customer_chat_id: str,
    previous_outlet: str,
    new_outlet: str,
    name: str,
) -> None:
    if not previous_outlet or previous_outlet == new_outlet:
        return

    previous_target_chat_id = outlet_telegram_chat_id(previous_outlet)

    if not previous_target_chat_id:
        return

    message = (
        "Trial Booking Outlet Changed\n\n"
        f"Customer Telegram Chat ID: {customer_chat_id}\n\n"
        "Summary:\n"
        f"- Previous Outlet: {previous_outlet}\n"
        f"- New Outlet: {new_outlet}\n"
        f"- Name: {name or 'Not provided'}\n\n"
        "Please do not proceed with the previous outlet booking unless Customer Service confirms otherwise."
    )

    try:
        send_telegram_message(previous_target_chat_id, message)
    except Exception as e:
        print("TRIAL BOOKING PREVIOUS OUTLET NOTICE ERROR:", str(e), flush=True)
        traceback.print_exc()


def send_trial_booking_to_outlet(customer_chat_id: str, booking: Dict[str, str]) -> bool:
    outlet = booking.get("outlet", "")
    name = booking.get("name", "")
    fitness_goal = booking.get("fitness_goal", "")
    previous_outlet = booking.get("previous_outlet", "")
    is_update = bool(previous_outlet and previous_outlet != outlet)

    if not outlet:
        return False

    TRIAL_BOOKINGS[customer_chat_id] = {
        "outlet": outlet,
        "name": name,
        "fitness_goal": fitness_goal,
    }

    title = "Updated Trial Booking Summary" if is_update else "New Trial Booking Summary"
    outlet_lines = (
        f"- Previous Outlet: {previous_outlet}\n"
        f"- New Outlet: {outlet}\n"
        if is_update
        else f"- Outlet: {outlet}\n"
    )
    status_text = (
        "This customer has changed their trial booking outlet.\n\n"
        if is_update
        else "This customer has submitted a trial booking request.\n\n"
    )

    message = (
        f"{title} 🙏\n\n"
        "Summary:\n"
        f"{outlet_lines}"
        "- Class: Trial Class\n"
        f"- Name: {name or 'Not provided'}\n"
        f"- Fitness Goal: {fitness_goal or 'Not provided'}\n\n"
        f"Customer Telegram Chat ID: {customer_chat_id}\n\n"
        f"{status_text}"
        f"{support_reply_instructions(customer_chat_id)}"
    )

    target_chat_id = outlet_telegram_chat_id(outlet)

    if not target_chat_id:
        print("TRIAL BOOKING SEND SKIPPED: No outlet Telegram chat ID", flush=True)
        return False

    return send_support_notification(
        customer_chat_id,
        target_chat_id,
        outlet,
        message,
        "TRIAL BOOKING SEND ERROR",
    )


def update_active_trial_outlet(chat_id: str, outlet: str) -> str:
    flow = get_flow(chat_id)
    stage = get_flow_stage(chat_id)
    data = {key: value for key, value in flow.items() if key != "stage"}
    data["outlet"] = outlet

    if stage == "trial_goal":
        set_flow(chat_id, stage, **data)
        return (
            f"I've updated your preferred studio to {outlet}. 🙏\n\n"
            f"{trial_goal_question(data)}"
        )

    set_flow(chat_id, "trial_name", outlet=outlet)
    return (
        f"I've updated your preferred studio to {outlet}. 🙏\n\n"
        "May I have your full name?"
    )


def apply_completed_trial_outlet_change(chat_id: str, outlet: str) -> str:
    booking = TRIAL_BOOKINGS.get(chat_id, {})
    previous_outlet = booking.get("outlet", "")
    name = booking.get("name", "")
    fitness_goal = booking.get("fitness_goal", "")

    if previous_outlet == outlet:
        return (
            "Your trial booking is already set to this outlet.\n\n"
            "Is there anything else we can assist you with today?"
        )

    close_live_support_chat(chat_id)

    updated_booking = {
        "outlet": outlet,
        "name": name,
        "fitness_goal": fitness_goal,
        "previous_outlet": previous_outlet,
    }

    sent = send_trial_booking_to_outlet(chat_id, updated_booking)

    if sent:
        send_trial_change_notice_to_previous_outlet(chat_id, previous_outlet, outlet, name)
        follow_up = (
            f"I've updated your trial booking from {previous_outlet} to {outlet} and sent the updated summary "
            f"to the {outlet} team. Our Studio Manager will contact you within 24 hours to schedule your trial."
        )
    else:
        follow_up = (
            f"I've updated your preferred outlet to {outlet}, but the {outlet} Telegram chat is not configured yet.\n\n"
            "Please type CUSTOMER SERVICE so our team can assist with the booking."
        )

    reply = (
        "Updated Trial Booking Summary:\n"
        f"- Previous Outlet: {previous_outlet or 'Not provided'}\n"
        f"- New Outlet: {outlet}\n"
        "- Class: Trial Class\n"
        f"- Name: {name or 'Not provided'}\n"
        f"- Fitness Goal: {fitness_goal or 'Not provided'}\n\n"
        f"{follow_up}"
    )

    return add_customer_service_id_note(reply, chat_id)


def handle_trial_outlet_change_request(chat_id: str, text: str) -> str:
    stage = get_flow_stage(chat_id)
    flow = get_flow(chat_id)
    outlet = detect_outlet_choice(text)

    if stage == "trial_change_outlet":
        if not outlet:
            return trial_change_outlet_question()

        return_stage = flow.get("return_stage", "")

        if return_stage.startswith("trial_") and not TRIAL_BOOKINGS.get(chat_id):
            restore_data = {
                key: value
                for key, value in flow.items()
                if key not in {"stage", "return_stage"}
            }
            set_flow(chat_id, return_stage, **restore_data)
            return update_active_trial_outlet(chat_id, outlet)

        clear_flow(chat_id)
        return apply_completed_trial_outlet_change(chat_id, outlet)

    if not is_trial_outlet_change_request(chat_id, text):
        return ""

    if not outlet:
        if stage.startswith("trial_") and not TRIAL_BOOKINGS.get(chat_id):
            flow_data = {key: value for key, value in flow.items() if key != "stage"}
            set_flow(chat_id, "trial_change_outlet", return_stage=stage, **flow_data)
        else:
            set_flow(chat_id, "trial_change_outlet")

        return trial_change_outlet_question()

    if stage.startswith("trial_") and not TRIAL_BOOKINGS.get(chat_id):
        return update_active_trial_outlet(chat_id, outlet)

    return apply_completed_trial_outlet_change(chat_id, outlet)


# REFER FRIEND

def send_refer_friend_to_outlet(customer_chat_id: str, referral: Dict[str, str]) -> bool:
    outlet = referral.get("preferred_studio", "")
    friend_name = referral.get("friend_name", "")
    friend_contact = referral.get("friend_contact", "")

    message = (
        "New Refer-a-Friend Received ✨\n\n"
        f"Preferred Studio: {outlet}\n"
        f"Friend Name: {friend_name or 'Not provided'}\n"
        f"Friend Contact: {friend_contact or 'Not provided'}\n\n"
        f"Referrer Telegram Chat ID: {customer_chat_id}\n\n"
        "Live chat connected.\n\n"
        f"{support_reply_instructions(customer_chat_id)}"
    )

    return send_outlet_support_notification(
        customer_chat_id,
        outlet,
        message,
        "REFER FRIEND SEND SKIPPED",
        "REFER FRIEND SEND ERROR",
    )


# FUZZY PHRASE MATCHING


def fuzzy_phrase_match(text: str, phrases: List[str], threshold: float = 0.76) -> bool:
    clean = simple_text(text)

    if not clean:
        return False

    words = clean.split()
    padded = f" {clean} "

    for phrase in phrases:
        target = simple_text(phrase)

        if not target:
            continue

        if f" {target} " in padded:
            return True

        target_words = target.split()
        size = len(target_words)

        if size == 0 or len(words) < size:
            continue

        for i in range(len(words) - size + 1):
            chunk = " ".join(words[i:i + size])
            score = SequenceMatcher(None, chunk, target).ratio()

            if score >= threshold:
                return True

    return False


CLASS_SCHEDULE_NON_ENGLISH_WORDS = phrase_list("jadual|时间表|時間表|課表|课程表")
CLASS_SCHEDULE_KEYWORDS = phrase_list(
    "schedule|schdule|sched|timetable|time table|class schedule|class timetable|class timing|class timings|class time|class times|classes today|today class|today classes|today schedule|tomorrow schedule|available slot|available slots|timeslot|timeslots"
)
CLASS_SCHEDULE_CONTEXT_PATTERNS = [
    # Removed 'available' and 'availability' to prevent false positives on general queries
    r"\b(class|classes|lesson|lessons|yoga|pilates|barre|trial)\b.*\b(time|timing|timings|schedule|timetable|slot|slots)\b",
    r"\b(time|timing|timings|schedule|timetable|slot|slots)\b.*\b(class|classes|lesson|lessons|yoga|pilates|barre|trial)\b",
]
CLASS_SCHEDULE_FUZZY_PHRASES = phrase_list(
    "schedule|schdule|schedle|schedual|secdule|scedule|scehdule|shedule|skedule|timetable|timetabel|timetble|time table|class schedule|class timing|class timetable"
)


def is_class_schedule_request(text: str) -> bool:
    normalized = normalize(text)
    clean = simple_text(text)

    if not clean and not text.strip():
        return False

    return (
        any(word in normalized or word in text for word in CLASS_SCHEDULE_NON_ENGLISH_WORDS)
        or any(keyword in normalized for keyword in CLASS_SCHEDULE_KEYWORDS)
        or any(re.search(pattern, clean, flags=re.IGNORECASE) for pattern in CLASS_SCHEDULE_CONTEXT_PATTERNS)
        or fuzzy_phrase_match(text, CLASS_SCHEDULE_FUZZY_PHRASES, threshold=0.76)
    )



CLASS_TYPE_KEYWORDS = phrase_list(
    "what yoga|what yoga class|what yoga classes|what yoga do you have|what yoga do u have|what yoga do u guys have|"
    "yoga classes|yoga class types|types of yoga|kind of yoga|kinds of yoga|"
    "what class|what classes|what classes do you have|what classes do u have|what classes do u guys have|"
    "class types|types of class|types of classes|kind of class|kinds of classes|"
    "what pilates|pilates classes|pilates class types|what barre|barre classes|what reformer|reformer classes"
)

CLASS_TYPE_CONTEXT_PATTERNS = [
    r"\b(what|which)\b.*\b(yoga|pilates|barre|reformer|class|classes)\b.*\b(have|offer|available|provide)\b",
    r"\b(types?|kinds?)\b.*\b(yoga|pilates|barre|reformer|class|classes)\b",
    r"\b(yoga|pilates|barre|reformer|class|classes)\b.*\b(types?|kinds?)\b",
]
def is_membership_request(text: str) -> bool:
    clean = simple_text(text)
    words = set(clean.split())
    member_words = {"membership", "memberships", "package", "packages", "price", "prices", "pricing"}
    return bool(words & member_words)


def is_class_type_request(text: str) -> bool:
    normalized = normalize(text)
    clean = simple_text(text)

    if not clean and not text.strip():
        return False

    # Timing/schedule questions should still go to schedule logic.
    if is_class_schedule_request(text):
        return False

    return (
        any(keyword in normalized for keyword in CLASS_TYPE_KEYWORDS)
        or any(re.search(pattern, clean, flags=re.IGNORECASE) for pattern in CLASS_TYPE_CONTEXT_PATTERNS)
    )


STAFF_INFO_KEYWORDS = phrase_list(
    "instructor|instructors|teacher|teachers|trainer|trainers|staff|coach|coaches|"
    "credentials|credential|qualification|qualifications|certified|certification|certifications|"
    "highlights|highlight|profile|bio|biography|experience|background|"
    "who is|who are|tell me about|tell me more about|"
    "more information about instructor|more info about instructor|"
    "more information about teacher|more info about teacher|"
    "more information about trainer|more info about trainer|"
    "more information about staff|more info about staff|"
    "know more about|info about|information about|details about|"
    "any info on|any information on|info on|information on|"
    "do you have|list of teachers|list of instructors|list of trainers|"
    "yoga teacher|yoga teachers|yoga instructor|yoga instructors|"
    "pilates teacher|pilates teachers|pilates instructor|pilates instructors|"
    "barre teacher|barre teachers|barre instructor|barre instructors|"
    "who teaches|who is teaching|who runs|who leads"
)


STAFF_HANDOFF_PHRASES = phrase_list(
    "talk to staff|speak to staff|connect me to staff|need staff help|staff help|"
    "talk to an instructor|speak to an instructor|talk to a teacher|speak to a teacher|"
    "talk to a trainer|speak to a trainer|contact staff|contact instructor|contact teacher|"
    "contact trainer|message staff|message instructor|call staff"
)

def is_staff_handoff_request(text: str) -> bool:
    clean = simple_text(text)

    if not clean:
        return False

    words = set(clean.split())
    staff_words = {
        "instructor", "instructors", "teacher", "teachers",
        "trainer", "trainers", "staff", "coach", "coaches",
        "satff", "staf", "instuctor", "instructer", "techer", "traner"
    }

    if not words & staff_words:
        return False

    if any(phrase in clean for phrase in STAFF_HANDOFF_PHRASES):
        return True

    contact_words = {
        "talk", "speak", "connect", "contact", "message", "call",
        "whatsapp", "human", "agent", "live", "support", "helpdesk"
    }
    info_words = {
        "about", "profile", "bio", "biography", "info", "information",
        "know", "who", "credential", "credentials", "qualification",
        "qualifications", "experience", "background", "highlight",
        "highlights", "called", "named"
    }

    return bool(words & contact_words and not words & info_words)


def is_staff_info_request(text: str) -> bool:
    clean = simple_text(text)
    normalized = normalize(text)

    if not clean:
        return False

    # FIX: If the message mentions any class, course, training, or workshop keywords, 
    # do NOT let it get intercepted by the staff profile handler.
    course_keywords = {"yoga", "pilates", "barre", "reformer", "training", "course", "workshop", "retreat"}
    if is_class_type_request(text) or any(word in normalized for word in course_keywords):
        return False

    staff_words = {
        "instructor", "instructors", "teacher", "teachers",
        "trainer", "trainers", "staff", "coach", "coaches",
        "satff", "staf", "instuctor", "instructer", "techer", "traner"
    }

    credential_words = {
        "credential", "credentials", "qualification", "qualifications",
        "certified", "certification", "certifications", "highlight",
        "highlights", "profile", "bio", "biography", "experience", "background"
    }
    info_words = {"who", "what", "more", "info", "information", "about", "know", "profile"}
    words = set(clean.split())

    if words & staff_words:
        return True

    if any(keyword in normalized for keyword in STAFF_INFO_KEYWORDS):
        return True

    # Handles messages like: "can i know more about Amen" after a schedule answer.
    if any(phrase in normalized for phrase in ["know more about", "more info about", "more information about"]):
        return True

    return bool((words & info_words) and (words & (staff_words | credential_words)))


def is_generic_staff_query(text: str) -> bool:
    """
    True when the user is asking about staff/instructors generally without naming
    a specific person. e.g. 'instructor?', 'do you have teachers?', 'tell me about your trainers'.
    """
    clean = simple_text(text)
    if not clean:
        return False

    staff_words = {
        "instructor", "instructors", "teacher", "teachers",
        "trainer", "trainers", "staff", "coach", "coaches",
        "satff", "staf", "instuctor", "instructer", "techer", "traner"
    }
    # ... (rest of the function stays exactly the same)
    words = set(clean.split())

    # Must mention staff in some form
    if not (words & staff_words):
        # phrases like 'who teaches', 'do you have teachers'
        if not any(p in clean for p in [
            "who teaches", "who is teaching", "who runs", "who leads",
            "list of teachers", "list of instructors", "list of trainers"
        ]):
            return False

    # Short generic queries (3 words or fewer) like "instructor?", "teachers"
    if len(words) <= 3:
        return True

    # Longer phrasings that are still generic (no specific name guessable)
    generic_phrases = [
        "do you have", "tell me about your", "tell me more about your",
        "list of", "list your", "who are your", "what teachers",
        "what instructors", "what trainers", "any teachers", "any instructors",
        "any trainers", "how many teachers", "how many instructors",
        "how many trainers", "the teachers", "the instructors",
        "the trainers", "your teachers", "your instructors", "your trainers",
        "your staff", "your coaches", "more about your"
    ]

    return any(phrase in clean for phrase in generic_phrases)


def is_short_name_reply(text: str) -> bool:
    """
    Detects when the user replies with just a name (e.g. 'Ravi', 'Sarah Yang').
    Used after we listed instructor names to follow up about a specific person.
    """
    clean = (text or "").strip()
    if not clean:
        return False

    # Must be 1-3 short words, mostly alphabetic
    parts = clean.split()
    if not (1 <= len(parts) <= 3):
        return False

    # Reject pure numbers, common command words
    if normalize(clean) in RESET_WORDS | OPT_OUT_WORDS | OPT_IN_WORDS:
        return False
    if normalize(clean).isdigit():
        return False

    # Each word should be mostly letters
    for p in parts:
        letters = sum(1 for c in p if c.isalpha())
        if letters < max(2, len(p) - 1):
            return False

    return True

def staff_info_reply(chat_id: str, text: str) -> str:
    try:
        return knowledge_reply(
            chat_id,
            text,
            (
                "The customer is asking for information about an instructor or teacher. "
                "Use ONLY the Jal Yoga website content and recent chat context. "
                "IMPORTANT: Check the recent chat history first. If the customer asks who is teaching a specific class or course that was just mentioned, give them the exact instructor's name and details based on the website content. "
                "If they ask a general question (like 'who are your teachers?') AND the chat history does not mention a specific class, list all the instructors found in the website content. "
                "Format the list like this: put each instructor's name on its own line starting with '- ' (a dash and a space). "
                "On the following line, write ONE short description as a single indented sub-bullet starting with three spaces, a dash and a space ('   - '). "
                "Keep the description brief — roughly five to ten words summarising what that instructor specialises in. "
                "If an instructor has many specialties, summarise them into one short phrase instead of listing them all. "
                "ALWAYS use exactly ONE sub-bullet per instructor, even if the customer asks to split by class type or specialty — in that case still summarise everything they teach into that single short sub-bullet. "
                "Do NOT create a separate sub-bullet for each class type, and do NOT create more than two levels of bullets. "
                "Do NOT group the instructors under headings such as 'Men' and 'Women', or any other category, UNLESS the customer has clearly and explicitly asked you to group them that way (for example 'split the list into male and female'). If they have not explicitly asked, keep it as one single flat list. "
                "Put one blank line between each instructor so the list is easy to read. For example:\n"
                "- Aneesh\n"
                "   - Breath awareness and posture adjustment specialist\n"
                "\n"
                "- April\n"
                "   - Alignment and inclusive classes for all levels\n"
                "Base the description on the real website content, do not invent it. "
                "End your reply by adding the exact tag [WAIT_FOR_NAME] followed by asking 'Which staff member would you like to know more about?' (translate the question into the customer's language, but DO NOT translate the [WAIT_FOR_NAME] tag). "
                "If they name a specific instructor, share their confirmed credentials and highlights. "
                "Do not invent details. If you are not sure, say you are not fully sure and use [HANDOFF]."
            ),
            (
                "I’m sorry — I’m not fully sure based on the website information I have.\n"
                "[HANDOFF]"
            ),
        )
    except Exception as e:
        print("STAFF INFO REPLY ERROR:", str(e), flush=True)
        return "I’m sorry — I’m not fully sure based on the website information I have.\n[HANDOFF]"


def handle_staff_info_request(chat_id: str, text: str) -> str:
    answer = staff_info_reply(chat_id, text)

    if "[HANDOFF]" in answer or "not fully sure" in answer.lower():
        STAFF_LIST_PENDING.pop(chat_id, None)
        clean_answer = strip_handoff_token(answer).strip()
        return queue_pending_handoff(chat_id, text, clean_answer)

    clean_answer = strip_handoff_token(answer).strip()

    if "[WAIT_FOR_NAME]" in clean_answer:
        STAFF_LIST_PENDING[chat_id] = True
        clean_answer = clean_answer.replace("[WAIT_FOR_NAME]", "").strip()
    else:
        STAFF_LIST_PENDING.pop(chat_id, None)

    return clean_answer


MONTH_NAME_TO_NUMBER = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def build_schedule_date(day: int, month: int, year: Optional[int] = None) -> Optional[datetime]:
    """Build a Singapore date. If year is missing, choose the next matching date."""
    today = datetime.now(ZoneInfo("Asia/Singapore")).date()

    if year is None:
        year = today.year

    if year < 100:
        year += 2000

    try:
        chosen = datetime(year, month, day, tzinfo=ZoneInfo("Asia/Singapore")).date()
    except ValueError:
        return None

    # For messages like "15 May" without a year, use next year's date if this year's already passed.
    if chosen < today and year == today.year:
        try:
            chosen = datetime(today.year + 1, month, day, tzinfo=ZoneInfo("Asia/Singapore")).date()
        except ValueError:
            return None

    return datetime.combine(chosen, datetime.min.time())


def explicit_schedule_dates(text: str) -> List[datetime]:
    """Parse direct date requests like '15 May', 'May 15', '15/05', or '15-05-2026'."""
    clean = normalize(text)
    dates: List[datetime] = []

    month_words = "|".join(sorted(MONTH_NAME_TO_NUMBER.keys(), key=len, reverse=True))

    patterns = [
        # 15 May, 15th May 2026
        rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{month_words})(?:\s+(?P<year>\d{{2,4}}))?\b",
        # May 15, May 15th 2026
        rf"\b(?P<month>{month_words})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:\s+(?P<year>\d{{2,4}}))?\b",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, clean, flags=re.IGNORECASE):
            day = int(match.group("day"))
            month = MONTH_NAME_TO_NUMBER[match.group("month").lower()]
            year = int(match.group("year")) if match.groupdict().get("year") else None
            parsed = build_schedule_date(day, month, year)

            if parsed and parsed not in dates:
                dates.append(parsed)

    # Numeric dates. In Singapore, treat 15/05 as day/month.
    for match in re.finditer(r"\b(?P<day>\d{1,2})[/-](?P<month>\d{1,2})(?:[/-](?P<year>\d{2,4}))?\b", clean):
        day = int(match.group("day"))
        month = int(match.group("month"))
        year = int(match.group("year")) if match.group("year") else None
        parsed = build_schedule_date(day, month, year)

        if parsed and parsed not in dates:
            dates.append(parsed)

    return dates


def has_schedule_date_request(text: str) -> bool:
    clean = simple_text(text)

    if explicit_schedule_dates(text):
        return True

    if any(word in clean.split() for word in ["today", "tomorrow", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]):
        return True

    return False


def is_schedule_followup_request(chat_id: str, text: str) -> bool:
    """Allow short follow-ups after a schedule reply, e.g. 'can i see 15 May'."""
    flow = get_flow(chat_id)
    clean = simple_text(text)

    if flow.get("last_topic") != "schedule":
        return False

    if not clean:
        return False

    if detect_outlet_choice(text):
        return False

    if has_schedule_date_request(text):
        return True

    followup_words = {"next", "more", "another", "other", "show", "see", "view", "later", "after"}
    return bool(set(clean.split()) & followup_words)


def requested_schedule_dates(text: str, lookahead_days: int = 5) -> List[datetime]:
    explicit_dates = explicit_schedule_dates(text)

    if explicit_dates:
        return explicit_dates

    t = normalize(text)
    today = datetime.now(ZoneInfo("Asia/Singapore")).date()

    if "today" in t:
        return [datetime.combine(today, datetime.min.time())]

    if "tomorrow" in t:
        return [datetime.combine(today + timedelta(days=1), datetime.min.time())]

    weekdays = [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]

    for index, day in enumerate(weekdays):
        if day in t:
            delta = (index - today.weekday()) % 7
            return [datetime.combine(today + timedelta(days=delta), datetime.min.time())]

    return [
        datetime.combine(today + timedelta(days=offset), datetime.min.time())
        for offset in range(lookahead_days)
    ]


def clean_schedule_text(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text or "")).strip()


def parse_mindbody_schedule_html(html: str) -> List[Dict[str, str]]:
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        rows = []

        for day_block in soup.select(".bw-widget__day"):
            date_text = clean_schedule_text(day_block.select_one(".bw-widget__date").get_text(" ") if day_block.select_one(".bw-widget__date") else "")

            for session in day_block.select(".bw-session"):
                start = clean_schedule_text(session.select_one(".hc_starttime").get_text(" ") if session.select_one(".hc_starttime") else "")
                end = clean_schedule_text(session.select_one(".hc_endtime").get_text(" ") if session.select_one(".hc_endtime") else "")
                class_name = clean_schedule_text(session.select_one(".bw-session__name").get_text(" ") if session.select_one(".bw-session__name") else "")
                trainer = clean_schedule_text(session.select_one(".bw-session__staff").get_text(" ") if session.select_one(".bw-session__staff") else "")
                location = clean_schedule_text(session.select_one(".bw-session__location").get_text(" ") if session.select_one(".bw-session__location") else "")

                if not class_name or not start:
                    continue

                rows.append(
                    {
                        "date": date_text,
                        "start": start,
                        "end": end,
                        "class": class_name,
                        "trainer": trainer,
                        "location": location,
                    }
                )

        return rows

    except Exception as e:
        print("MINDBODY SCHEDULE PARSE ERROR:", str(e), flush=True)
        traceback.print_exc()
        return []


def fetch_mindbody_schedule_rows(outlet: str, date_value: datetime) -> List[Dict[str, str]]:
    widget = MINDBODY_SCHEDULE_WIDGETS.get(outlet)

    if not widget:
        return []

    url = f"https://widgets.mindbodyonline.com/widgets/schedules/{widget['widget_id']}/load_markup"
    params = {
        "options[start_date]": date_value.strftime("%Y-%m-%d"),
        "options[location]": widget["location_id"],
        "options[trainer]": "",
        "options[type_group]": "",
        "options[visit_type]": "",
        "preview": "",
    }

    try:
        response = requests.get(
            url,
            params=params,
            timeout=12,
            headers={
                "User-Agent": "Mozilla/5.0 JalYogaTelegramAssistant/1.0",
                "Referer": "https://www.jalyoga.com.sg/jal-schedule/",
            },
        )

        if response.status_code != 200:
            print(f"MINDBODY SCHEDULE SKIPPED {outlet}: {response.status_code}", flush=True)
            return []

        data = response.json()
        return parse_mindbody_schedule_html(data.get("class_sessions", ""))

    except Exception as e:
        print(f"MINDBODY SCHEDULE FETCH ERROR for {outlet}:", str(e), flush=True)
        traceback.print_exc()
        return []


def collect_mindbody_schedule(outlets: List[str], dates: List[datetime], max_per_outlet: int = 3) -> Dict[str, List[Dict[str, str]]]:
    schedule = {}

    for outlet in outlets:
        rows = []

        for date_value in dates:
            rows.extend(fetch_mindbody_schedule_rows(outlet, date_value))

            if len(rows) >= max_per_outlet:
                break

        schedule[outlet] = rows[:max_per_outlet]

    return schedule


def format_schedule_row(row: Dict[str, str]) -> str:
    time_text = f"{row['start']} - {row['end']}" if row.get("end") else row.get("start", "")
    line = f"- {time_text}: {row.get('class', 'Class')}"

    if row.get("trainer"):
        line += f" with {row['trainer']}"

    return line


def live_class_schedule_reply(text: str) -> str:
    requested_outlet = detect_outlet_choice(text)
    outlets = [requested_outlet] if requested_outlet else studio_names()
    dates = requested_schedule_dates(text)
    max_per_outlet = 6 if requested_outlet else 3
    schedule = collect_mindbody_schedule(outlets, dates, max_per_outlet=max_per_outlet)

    if requested_outlet:
        rows = schedule.get(requested_outlet, [])

        if rows:
            lines = [f"Here are upcoming classes I found for {requested_outlet}:", ""]
            current_date = ""

            for row in rows:
                if row.get("date") and row["date"] != current_date:
                    current_date = row["date"]
                    lines.append(current_date)

                lines.append(format_schedule_row(row))

            return append_trial_studio_question("\n".join(lines))

        return append_trial_studio_question(
            f"I don’t see any listed classes for {requested_outlet} in the next few days from the live schedule."
        )

    lines = ["Here are upcoming classes I found from the live schedule:", ""]
    has_rows = False

    for outlet in outlets:
        rows = schedule.get(outlet, [])

        if not rows:
            continue

        has_rows = True
        lines.append(f"{outlet}")
        current_date = ""

        for row in rows:
            if row.get("date") and row["date"] != current_date:
                current_date = row["date"]
                lines.append(current_date)

            lines.append(format_schedule_row(row))

        lines.append("")

    if not has_rows:
        return append_trial_studio_question(
            "I don’t see any listed classes in the next few days from the live schedule."
        )

    return append_trial_studio_question("\n".join(lines).strip())



# INACTIVITY

def mark_chat_active(chat_id: str) -> None:
    INACTIVITY_STATE[chat_id] = {
        "last_user_at": time.time(),
        "warning_sent": False,
        "closed": False,
    }


def clear_inactivity_state(chat_id: str) -> None:
    INACTIVITY_STATE.pop(chat_id, None)


def send_inactivity_message(chat_id: str, message: str) -> None:
    if INACTIVITY_REMINDER_QUEUE is not None:
        INACTIVITY_REMINDER_QUEUE.put(message)
        return

    send_telegram_message(chat_id, message)


def inactivity_checker_loop() -> None:
    while True:
        time.sleep(INACTIVITY_CHECK_SECONDS)

        now = time.time()

        for chat_id, state in list(INACTIVITY_STATE.items()):
            try:
                if chat_id in OPT_OUT_USERS:
                    clear_inactivity_state(chat_id)
                    continue

                idle_seconds = now - float(str(state.get("last_user_at", now)))
                warning_sent = bool(state.get("warning_sent", False))

                if not warning_sent and idle_seconds >= INACTIVITY_WARNING_SECONDS:
                    send_inactivity_message(
                        chat_id,
                        "Just checking in — do you still need help? "
                        "Reply here to continue, or type STOP to stop receiving follow-up messages.",
                    )

                    state["warning_sent"] = True

                elif warning_sent and idle_seconds >= INACTIVITY_CLOSE_SECONDS:
                    send_inactivity_message(
                        chat_id,
                        "We’ll close this chat for now. "
                        "If you need help again, reply START or MENU anytime. 🙏",
                    )

                    # NEW: Check if they are in a live chat and notify the staff
                    live_chat = LIVE_SUPPORT_CHATS.get(chat_id, {})
                    target_chat_id = live_chat.get("target_chat_id", "")
                    
                    if target_chat_id:
                        try:
                            send_telegram_message(
                                str(target_chat_id),
                                f"System Note: Customer {chat_id} was disconnected due to inactivity (10 minutes).",
                            )
                        except Exception:
                            pass

                    reset_chat_state(chat_id, include_trial=True, include_inactivity=True)

            except Exception as e:
                print("INACTIVITY CHECK ERROR:", str(e), flush=True)
                traceback.print_exc()


def start_inactivity_thread(test_mode: bool = False, reminder_queue=None) -> None:
    global INACTIVITY_WARNING_SECONDS
    global INACTIVITY_CLOSE_SECONDS
    global INACTIVITY_CHECK_SECONDS
    global INACTIVITY_REMINDER_QUEUE

    if test_mode:
        INACTIVITY_WARNING_SECONDS, INACTIVITY_CLOSE_SECONDS, INACTIVITY_CHECK_SECONDS = (10, 20, 1)

    INACTIVITY_REMINDER_QUEUE = reminder_queue if test_mode else None

    start_inactivity_checker()


def start_inactivity_checker() -> None:
    global INACTIVITY_THREAD_STARTED

    if INACTIVITY_THREAD_STARTED:
        return

    INACTIVITY_THREAD_STARTED = True

    thread = threading.Thread(
        target=inactivity_checker_loop,
        daemon=True,
    )

    thread.start()


# MENU HANDLERS

def handle_menu_choice(chat_id: str, text: str, choices: dict, fallback: str = "") -> str:
    menu_choice = choices.get(normalize(text))

    if not menu_choice:
        return fallback

    stage, reply_factory = menu_choice
    set_flow(chat_id, stage)
    return reply_factory()


def handle_main_menu_choice(chat_id: str, text: str) -> str:
    return handle_menu_choice(chat_id, text, MAIN_MENU_CHOICES)


def handle_current_member_choice(chat_id: str, text: str) -> str:
    reply = handle_menu_choice(chat_id, text, CURRENT_MEMBER_CHOICES)

    if reply:
        return reply

    clear_flow(chat_id)
    return ""


def handle_member_service_flow(chat_id: str, text: str) -> str:
    stage = get_flow_stage(chat_id)
    outlet = detect_outlet_choice(text) or get_flow_outlet(chat_id)

    service_stage = MEMBER_SERVICE_STAGES.get(stage)

    if not service_stage:
        return ""

    topic, prompt_if_empty = service_stage
    details = text.strip()

    if len(details) < 2:
        return prompt_if_empty

    clean_answer = customer_service_summary(topic, outlet, details)

    if not outlet:
        return queue_pending_handoff(chat_id, details, clean_answer)

    clear_flow(chat_id)
    return send_handoff_result(
        chat_id,
        clean_answer,
        outlet,
        (
            f"I’ve sent this to our {outlet} Customer Service team. 🙏\n\n"
            "You are now connected to Customer Service. You may reply here and our team will receive your message."
        ),
        "Customer Service Telegram chat is not configured yet.",
    )


def handle_general_enquiry_choice(chat_id: str, text: str) -> str:
    choice = normalize(text)

    # Keep the old numbered shortcuts working for customers who still type 1/2/3.
    if choice == "1":
        clear_flow(chat_id)
        return studio_locations_text()

    if choice == "2":
        clear_flow(chat_id)
        return ask_llm(chat_id, "What class types does Jal Yoga offer?")

    if choice == "3":
        clear_flow(chat_id)
        return ask_llm(chat_id, "What current events or retreats does Jal Yoga offer?")

    # Main change: once the user is inside General Enquiry, any normal message
    # goes to the LLM, which can answer from knowledge.txt + Jal Yoga website text.
    clear_flow(chat_id)
    return ask_llm(chat_id, text)


def append_trial_studio_question(reply: str, fallback: str = "") -> str:
    clean_reply = remove_jal_yoga_website_urls(strip_handoff_token(reply)).strip()

    if (
        "[HANDOFF]" in reply
        or "I’ll pass this to our Customer Service team" in clean_reply
        or "I'll pass this to our Customer Service team" in clean_reply
    ):
        clean_reply = fallback

    if not clean_reply:
        clean_reply = "I’ll help with the class schedule based on the latest Jal Yoga information I have."

    if TRIAL_STUDIO_QUESTION in clean_reply:
        return clean_reply

    return f"{clean_reply}\n\n{TRIAL_STUDIO_QUESTION}"


def handle_class_schedule_request(chat_id: str, text: str) -> str:
    # Normal schedule requests: "schedule", "classes today", "class timing", etc.
    # Follow-up schedule requests: after showing schedule, allow short messages like
    # "can i see 15 May" or "show tomorrow" even if they do not say "schedule" again.
    if not is_class_schedule_request(text) and not is_schedule_followup_request(chat_id, text):
        return ""

    set_flow(chat_id, "trial_outlet", last_topic="schedule")
    return live_class_schedule_reply(text)


# EXTRA OUTLET FLOW HANDLERS

def handle_outlet_choice_flow(chat_id: str, text: str, reply_factory, optional: bool = False) -> str:
    outlet = detect_outlet_choice(text)

    if not outlet:
        if optional:
            clear_flow(chat_id)
            return ""

        return outlet_number_question()

    remember_outlet_context(chat_id, outlet)
    clear_flow(chat_id)
    return reply_factory(outlet)

def handle_contact_outlet_flow(chat_id: str, text: str) -> str:
    return handle_outlet_choice_flow(chat_id, text, build_outlet_contact_reply, optional=True)


def store_nearest_outlet_action(chat_id: str, recommendation: Dict[str, Any]) -> None:
    top = recommendation["ranked_studios"][0]
    remember_outlet_context(chat_id, top["name"])
    set_flow(
        chat_id,
        "nearest_outlet_action",
        recommended_outlet=top["name"],
        outlet=top["name"],
        location_label=str(recommendation["location"]["label"]),
    )


def handle_nearest_outlet_request(chat_id: str, text: str) -> str:
    recommendation = nearest_outlet_recommendation(text)

    if not recommendation:
        set_flow(chat_id, "nearest_outlet_location")
        return nearest_outlet_location_question()

    store_nearest_outlet_action(chat_id, recommendation)
    return format_nearest_outlet_reply(recommendation)


def handle_nearest_outlet_location_flow(chat_id: str, text: str) -> str:
    recommendation = nearest_outlet_recommendation(text)

    if not recommendation:
        return (
            "Sorry, I could not recognise that location. Please type a Singapore postal code, "
            "MRT station, mall, or area, for example Tampines, Serangoon, or 520123."
        )

    store_nearest_outlet_action(chat_id, recommendation)
    return format_nearest_outlet_reply(recommendation)


def wants_trial_from_nearest_action(text: str) -> bool:
    clean = simple_text(text)

    return (
        clean in {"1", "trial", "trial class", "trail", "trail class", "triel", "free trial"}
        or "trial" in clean
        or "trail" in clean
        or "triel" in clean
    )


def wants_regular_class_booking(text: str) -> bool:
    clean = simple_text(text)

    return (
        clean in {"2", "book", "booking", "book class", "book a class", "regular class", "class"}
        or ("book" in clean and "class" in clean)
        or ("booking" in clean and "class" in clean)
    )


def handle_nearest_outlet_action_flow(chat_id: str, text: str) -> str:
    flow = get_flow(chat_id)
    outlet = flow.get("recommended_outlet", "") or flow.get("outlet", "")

    if is_all_outlets_request(text):
        clear_flow(chat_id)
        return studio_locations_text()

    if not outlet:
        set_flow(chat_id, "nearest_outlet_location")
        return nearest_outlet_location_question()

    if wants_trial_from_nearest_action(text):
        return next_flow_reply(
            chat_id,
            "trial_name",
            f"Great - let's schedule your trial class at {outlet}.\n\nMay I have your full name?",
            outlet=outlet,
        )

    if wants_regular_class_booking(text):
        return regular_class_booking_guidance(outlet)

    if is_nearest_outlet_request(text):
        return handle_nearest_outlet_request(chat_id, text)

    recommendation = nearest_outlet_recommendation(text)

    if recommendation:
        store_nearest_outlet_action(chat_id, recommendation)
        return format_nearest_outlet_reply(recommendation)

    chosen_outlet = detect_outlet_choice(text)

    if chosen_outlet:
        set_flow(
            chat_id,
            "nearest_outlet_action",
            recommended_outlet=chosen_outlet,
            outlet=chosen_outlet,
            location_label=flow.get("location_label", ""),
        )
        return (
            f"Got it - {chosen_outlet}.\n\n"
            f"{nearest_outlet_action_prompt(chosen_outlet)}"
        )

    return nearest_outlet_action_prompt(outlet)


# FLOW HANDLERS

def handle_trial_flow(chat_id: str, text: str) -> str:
    flow = get_flow(chat_id)
    stage = get_flow_stage(chat_id)

    if stage == "trial_outlet":
        outlet = detect_outlet_choice(text)

        if outlet:
            return next_flow_reply(
                chat_id,
                "trial_name",
                f"Got it — {outlet}. 🙏\n\n"
                "May I have your full name?",
                outlet=outlet,
            )

        if is_class_type_request(text):
            return answer_flow_question_then_continue(chat_id, text)

        recommendation = nearest_outlet_recommendation(text)

        if recommendation:
            top = recommendation["ranked_studios"][0]
            outlet = top["name"]
            return next_flow_reply(
                chat_id,
                "trial_name",
                format_trial_nearest_outlet_reply(recommendation),
                outlet=outlet,
            )

        return trial_outlet_question()

    if stage == "trial_name":
        if is_meaning_question(text):
            return (
                "I mean: please type your full name for the trial booking.\n\n"
                "For example: Ben Tan"
            )

        name = text.strip()

        if len(name) < 2:
            return "Please share your full name."

        return next_flow_reply(
            chat_id,
            "trial_goal",
            f"Thanks, {name.title()} — what’s your fitness goal for the trial?",
            outlet=flow.get("outlet", ""),
            name=name,
        )

    if stage == "trial_goal":
        if is_meaning_question(text):
            return (
                "Fitness goal means what you want to improve from the trial class.\n\n"
                "For example:\n"
                "- flexibility\n"
                "- weight loss\n"
                "- strength\n"
                "- back pain relief\n"
                "- stress relief"
            )

        outlet = flow.get("outlet", "")
        name = flow.get("name", "")
        goal = text.strip()

        if len(goal) < 2:
            return "Please share your fitness goal."

        clear_flow(chat_id)

        booking = {
            "outlet": outlet,
            "name": name.title(),
            "fitness_goal": goal,
        }

        sent = send_trial_booking_to_outlet(chat_id, booking)

        if sent:
            follow_up = (
                f"Thank you! I've sent your details to the {outlet} team. "
                "Our Studio Manager will contact you within 24 hours to schedule your trial.\n\n"
                "Is there anything else we can assist you with today?\n\n"
                "If not, we'll close this ticket in a moment. Wishing you a wonderful and mindful day ahead! 🙏"
            )
        else:
            follow_up = (
                "Thank you! I've captured your trial details, but the studio Telegram chat is not configured yet.\n\n"
                "Please type CUSTOMER SERVICE so our team can assist with the booking."
            )

        reply = (
            "Trial Booking Summary:\n"
            f"- Outlet: {outlet}\n"
            "- Class: Trial Class\n"
            f"- Name: {name.title()}\n"
            f"- Fitness Goal: {goal}\n\n"
            f"{follow_up}"
        )

        return add_customer_service_id_note(reply, chat_id)

    return ""


def handle_refer_friend_flow(chat_id: str, text: str) -> str:
    flow = get_flow(chat_id)
    stage = get_flow_stage(chat_id)

    if stage == "refer_friend_name":
        friend_name = text.strip()

        if len(friend_name) < 2:
            return "Please share your friend’s full name."

        return next_flow_reply(
            chat_id,
            "refer_friend_contact",
            "Thanks — what is your friend’s contact number?",
            friend_name=friend_name,
        )

    if stage == "refer_friend_contact":
        friend_contact = text.strip()

        if len(friend_contact) < 3:
            return "Please share your friend’s contact number."

        return next_flow_reply(
            chat_id,
            "refer_friend_studio",
            friend_studio_question(),
            friend_name=flow.get("friend_name", ""),
            friend_contact=friend_contact,
        )

    if stage == "refer_friend_studio":
        outlet = detect_outlet_choice(text)

        if not outlet:
            return (
                "Please choose one of these studios:\n\n"
                f"{studio_options_text()}"
            )

        referral = {
            "friend_name": flow.get("friend_name", ""),
            "friend_contact": flow.get("friend_contact", ""),
            "preferred_studio": outlet,
        }

        clear_flow(chat_id)

        send_refer_friend_to_outlet(chat_id, referral)

        reply = (
            "Refer-a-Friend Summary:\n"
            f"- Friend Name: {referral['friend_name']}\n"
            f"- Friend Contact: {referral['friend_contact']}\n"
            f"- Preferred Studio: {outlet}\n\n"
            "That’s amazing! We love meeting friends of our Jal Yoga community. ✨\n\n"
            "Thank you! Our team will reach out to them with a special invitation.\n\n"
            "You are now connected to Customer Service.\n"
            f"{add_live_support_close_hint('You may reply here if you want to ask anything.')}"
        )

        return add_customer_service_id_note(reply, chat_id)

    return ""


def handle_corporate_flow(chat_id: str, text: str) -> str:
    flow = get_flow(chat_id)
    stage = get_flow_stage(chat_id)

    if stage == "corporate_name":
        name = text.strip()

        if len(name) < 2:
            return "Please share your full name."

        return next_flow_reply(
            chat_id,
            "corporate_email",
            "Thanks. What is your email address?",
            name=name,
        )

    if stage == "corporate_email":
        email = text.strip()

        if "@" not in email or "." not in email:
            return "Please share a valid email address."

        return next_flow_reply(
            chat_id,
            "corporate_message",
            corporate_message_question(),
            name=flow.get("name", ""),
            email=email,
        )

    if stage == "corporate_message":
        name = flow.get("name", "")
        email = flow.get("email", "")
        message = text.strip()

        if len(message) < 2:
            return "Please briefly tell us what your Corporate / Partnership enquiry is about."

        clear_flow(chat_id)

        clean_answer = summary_text(
            "Corporate / Partnership Summary:",
            {
                "Name": name,
                "Email": email,
                "Message": message,
            },
        )

        return send_handoff_result(
            chat_id,
            clean_answer,
            "Not specified",
            "Thank you! I’ve sent this to our Customer Service team. They will follow up with you soon.",
            "Customer Service Telegram group is not configured yet.",
        )

    return ""
def is_event_request(text: str) -> bool:
    clean = simple_text(text)
    words = set(clean.split())
    event_words = {"event", "events", "workshop", "workshops", "retreat", "retreats"}
    return bool(words & event_words)

def handle_event_flow(chat_id: str, text: str) -> str:
    flow = get_flow(chat_id)
    stage = get_flow_stage(chat_id)

    if stage == "event_outlet":
        outlet = detect_outlet_choice(text)

        if not outlet:
            return (
                "Please choose a valid studio:\n\n"
                f"{studio_options_text()}"
            )

        clear_flow(chat_id)
        remember_outlet_context(chat_id, outlet)

        return knowledge_reply(
            chat_id,
            text,
            (
                f"The customer wants to know about events, retreats, or workshops at the {outlet} studio. "
                f"Search the website content ONLY for workshops/events happening at {outlet}. "
                "List them neatly using short bullet points. You MUST include a short description of what the workshop is about. "
                f"If there are no events explicitly listed for {outlet}, politely say you don't see any scheduled there right now."
            ),
        )
    return ""

def handle_staff_hub_flow(chat_id: str, text: str) -> str:
    flow = get_flow(chat_id)
    stage = get_flow_stage(chat_id)

    if stage == "staff_name":
        staff_name = text.strip()

        if len(staff_name) < 2:
            return "Please share the staff name."

        return next_flow_reply(
            chat_id,
            "staff_studio",
            studio_prompt("Which studio is this related to?"),
            staff_name=staff_name,
        )

    if stage == "staff_studio":
        outlet = detect_outlet_choice(text)

        if not outlet:
            return (
                "Please choose a valid studio:\n\n"
                f"{studio_options_text()}"
            )

        return next_flow_reply(
            chat_id,
            "staff_room",
            "Which room is this related to?",
            staff_name=flow.get("staff_name", ""),
            outlet=outlet,
        )

    if stage == "staff_room":
        room = text.strip()

        if len(room) < 1:
            return "Please share the room."

        return next_flow_reply(
            chat_id,
            "staff_member_booking_details",
            staff_booking_details_question(),
            staff_name=flow.get("staff_name", ""),
            outlet=flow.get("outlet", ""),
            room=room,
        )

    if stage == "staff_member_booking_details":
        staff_name = flow.get("staff_name", "")
        outlet = flow.get("outlet", "")
        room = flow.get("room", "")
        details = text.strip()

        if len(details) < 2:
            return "Please share the member and booking details."

        clear_flow(chat_id)

        clean_answer = summary_text(
            "Staff Hub Summary:",
            {
                "Staff Name": staff_name,
                "Studio": outlet,
                "Room": room,
                "Member and Booking Details": details,
            },
        )

        return send_handoff_result(
            chat_id,
            clean_answer,
            outlet,
            f"Thank you! I’ve sent this to the {outlet} team.",
            "Customer Service Telegram group is not configured yet.",
        )

    return ""


def handle_pending_handoff_outlet(chat_id: str, text: str) -> str:
    outlet = detect_outlet_choice(text, include_not_specified=True)

    if not outlet:
        return ask_outlet_before_handoff_text()

    pending = PENDING_HANDOFFS.pop(chat_id, {})
    clear_flow(chat_id)

    clean_answer = pending.get(
        "clean_answer",
        "I’ll pass this to our Customer Service team.",
    )

    if "- Outlet:" in clean_answer:
        clean_answer = re.sub(r"- Outlet:.*", f"- Outlet: {outlet}", clean_answer)
    else:
        clean_answer += f"\n- Outlet: {outlet}"

    success = (
        f"I’ve sent this summary to our {outlet} Customer Service team on Telegram."
        if outlet != "Not specified"
        else "I’ve sent this summary to our Customer Service team on Telegram."
    )

    return send_handoff_result(
        chat_id,
        clean_answer,
        outlet,
        success,
        "Customer Service Telegram group is not configured yet.",
    )


# FINAL TRANSLATION LAYER

def translate_reply_if_needed(chat_id: str, user_text: str, reply: str) -> str:
    language = USER_LANGUAGE.get(chat_id, "English")

    if language.lower() in {"english", "unknown"}:
        return reply
    return openai_text_reply(
        (
            f"Translate the assistant reply into {language}. "
            "Translate every user-facing sentence fully. "
            "Preserve menu numbers, phone numbers, Telegram IDs, URLs, emojis, and formatting. "
            "Do not add new information."
        ),
        reply,
        reply,
        "TRANSLATION ERROR",
        show_traceback=False,
    )



def translate_text_to_language(text: str, language: str) -> str:
    """Translate text to the requested language, even when switching back to English."""
    if not text.strip() or language.lower() in {"unknown"}:
        return text

    return openai_text_reply(
        (
            f"Translate the assistant reply into {language}. "
            "Translate every user-facing sentence fully. "
            "Preserve class names, menu numbers, phone numbers, Telegram IDs, URLs, emojis, dates, times, and formatting. "
            "Do not add new information."
        ),
        text,
        text,
        "LANGUAGE SWITCH TRANSLATION ERROR",
        show_traceback=False,
    )


def is_flow_question_interrupt(chat_id: str, text: str) -> bool:
    clean = simple_text(text)
    normalized = normalize(text)
    
    jal_terms = {
        "yoga", "pilates", "barre", "reformer", "class", "classes",
        "membership", "memberships", "price", "prices", "package", "packages",
        "trial", "schedule", "timetable", "outlet", "studio", "studios",
        "teacher", "trainer", "instructor", "staff", "hot", "infrared",
        "credential", "credentials", "qualification", "qualifications",
        "profile", "bio", "experience", "highlight", "highlights",
        # Add these new terms so the bot recognizes them as valid questions!
        "course", "courses", "workshop", "workshops", "retreat", "retreats","event", "events"   
    }
    if not clean:
        return False

    # Do not interrupt normal short flow answers like "ben".
   # Look for this block around line 1032 and add "membership" and "package" to the list:
    if len(clean.split()) <= 2 and not any(
        word in clean
        for word in [
            "schedule", "timetable", "yoga", "pilates", "barre", "reformer",
            "membership", "memberships", "package", "packages", "price", "prices", "pricing",
            "outlet", "studio", "teacher", "trainer", "instructor", "staff",
        ]
    ):
        return False

    if normalized.isdigit():
        return False

    if normalized in RESET_WORDS or normalized in OPT_IN_WORDS or normalized in OPT_OUT_WORDS:
        return False

    if detect_outlet_choice(text, include_not_specified=True):
        return False

    if is_customer_service_request(text) or is_customer_service_contact_request(text):
        return False

    # FIX: Explicitly ignore nearest-outlet requests so your custom logic can run!
# Add this right before the question_words check (around line 1049):
    if is_membership_request(text):
        return True

    if is_nearest_outlet_request(text):
        return False

    if is_all_outlets_request(text):
        return True

    if is_staff_info_request(text):
        return True

    if is_class_schedule_request(text) or is_schedule_followup_request(chat_id, text) or is_class_type_request(text):
        return True

    question_words = {
        "what", "which", "where", "when", "who", "why", "how",
        "can", "do", "does", "is", "are",
    }

    jal_terms = {
        "yoga", "pilates", "barre", "reformer", "class", "classes",
        "membership", "memberships", "price", "prices", "package", "packages",
        "trial", "schedule", "timetable", "outlet", "studio", "studios",
        "teacher", "trainer", "instructor", "staff", "hot", "infrared",
        "credential", "credentials", "qualification", "qualifications",
        "profile", "bio", "experience", "highlight", "highlights",
    }

    words = set(clean.split())

    return bool(words & question_words and words & jal_terms)

def answer_flow_question_then_continue(chat_id: str, text: str) -> str:
    """
    Answer the customer's website question, then repeat the active flow question.
    This prevents all flows from getting stuck when users ask questions mid-flow.
    """
    try:
        staff_request = is_staff_info_request(text)
        
        # FIX: Route any message with course keywords to the class description logic
        course_keywords = {"yoga", "pilates", "barre", "reformer", "training", "course", "workshop", "retreat"}
        class_type_request = is_class_type_request(text) or any(word in normalize(text) for word in course_keywords)
        
        schedule_request = is_class_schedule_request(text) or is_schedule_followup_request(chat_id, text)
        membership_request = is_membership_request(text)
    except Exception as e:
        print("FLOW INTERRUPT DETECT ERROR:", str(e), flush=True)
        traceback.print_exc()
        return repeat_current_flow_question(chat_id)

    if staff_request:
        raw_answer = staff_info_reply(chat_id, text)
        generic = is_generic_staff_query(text)

        needs_handoff = "[HANDOFF]" in raw_answer or "not fully sure" in raw_answer.lower()
        answer = strip_handoff_token(raw_answer).strip()

        if needs_handoff:
            STAFF_LIST_PENDING.pop(chat_id, None)
            return queue_pending_handoff(chat_id, text, answer)

        if generic:
            # The bot just listed instructor names. Wait for a specific name reply.
            STAFF_LIST_PENDING[chat_id] = True
            # Don't pile the full main menu under the instructor list.
            return answer

        STAFF_LIST_PENDING.pop(chat_id, None)
        # Specific instructor reply — still gentle continuation, no full menu dump.
        return answer

    elif class_type_request:
        raw_answer = knowledge_reply(
            chat_id,
            text,
            (
                "The customer is asking what class types Jal Yoga has. "
                "Answer using ONLY the Jal Yoga website content. "
                "For yoga questions, use the Yoga Classes page content. "
                "For pilates, barre, or reformer questions, use the matching website page content. "
                "Mention specific class examples only if they appear in the website content. "
                "Keep it concise and customer-friendly. "
                "Do not give website URLs."
            ),
        )
    elif membership_request: # Add this block
        raw_answer = knowledge_reply(
            chat_id,
            text,
            (
                "The customer is asking about membership types, packages, or pricing. "
                "Look through the website content for general membership information. "
                "State what options are available clearly (e.g., unlimited passes, class packs) if found. "
                "Since exact pricing requires a consultation, politely explain that package prices vary "
                "based on commitment tiers and invite them to leave their details or speak to customer service for the latest rates."
            ),
        )    
    elif schedule_request:
        return live_class_schedule_reply(text)
    else:
        raw_answer = knowledge_reply(
            chat_id,
            text,
            (
                "The customer asked a general Jal Yoga question while they are already inside another form flow. "
                "Answer directly and concisely. NEVER use phrases like 'The website says' or 'According to the website'. "
                "Format neatly. If the information is missing, say you are not fully sure and use [HANDOFF]. "
                "Do not give website URLs."
            ),
        )

    needs_handoff = "[HANDOFF]" in raw_answer or "not fully sure" in raw_answer.lower()
    answer = strip_handoff_token(raw_answer).strip()

    # If the answer was uncertain, route to Customer Service.
    if needs_handoff:
        return queue_pending_handoff(chat_id, text, answer)

    stage = get_flow_stage(chat_id)
    
    # If the user is just sitting at a menu, we don't need a continuation prompt.
    if stage in {"main_menu", "current_member_menu", "general_enquiry_menu"}:
        return answer

   # We intentionally DO NOT use clear_flow(chat_id) here.
    # The bot will quietly remember exactly where the user left off.
    return (
        f"{answer}\n\n"
        "(Whenever you're ready, just reply to my previous question to continue your request, or type MENU to start over.)"
    )


def handle_active_flow_stage(chat_id: str, text: str) -> str:
    stage = get_flow_stage(chat_id)

    # Allow website-question interruptions in ALL stages, including detail-filling!
    interrupt_allowed_stages = {
        "trial_outlet",
        "trial_name",
        "trial_goal",
        "trial_change_outlet",
        "nearest_outlet_location",
        "nearest_outlet_action",
        "refer_friend_name",
        "refer_friend_contact",
        "refer_friend_studio",
        "corporate_name",
        "corporate_email",
        "corporate_message",
        "staff_name",
        "staff_studio",
        "staff_room",
        "staff_member_booking_details",
        "member_cancel_details",
        "member_suspension_details",
        "member_booking_issue_details",
        "contact_outlet",
        "pending_handoff_outlet",
        "main_menu",
        "current_member_menu",
        "general_enquiry_menu",
        "event_outlet",
    }

    if stage in interrupt_allowed_stages and is_flow_question_interrupt(chat_id, text):
        return answer_flow_question_then_continue(chat_id, text)

    if is_all_outlets_request(text):
        clear_flow(chat_id)
        return studio_locations_text()

    if (
        is_nearest_outlet_request(text)
        and stage not in {"nearest_outlet_location", "nearest_outlet_action"}
        and not stage.startswith("trial_")
    ):
        return handle_nearest_outlet_request(chat_id, text)

    if stage.startswith("member_"):
        return handle_member_service_flow(chat_id, text)

    stage_handlers = {
        "contact_outlet": handle_contact_outlet_flow,
        "nearest_outlet_location": handle_nearest_outlet_location_flow,
        "nearest_outlet_action": handle_nearest_outlet_action_flow,
        "pending_handoff_outlet": handle_pending_handoff_outlet,
        "main_menu": handle_main_menu_choice,
        "current_member_menu": handle_current_member_choice,
        "general_enquiry_menu": handle_general_enquiry_choice,
    }
    handler = stage_handlers.get(stage)

    if handler:
        return handler(chat_id, text)

    for prefix, flow_handler in (
        ("trial_", handle_trial_flow),
        ("refer_friend_", handle_refer_friend_flow),
        ("corporate_", handle_corporate_flow),
        ("staff_", handle_staff_hub_flow),
        ("event_", handle_event_flow),
    ):
        if stage.startswith(prefix):
            return flow_handler(chat_id, text)

    return ""


# PROCESS MESSAGE

def process_message(chat_id: str, user_text: str) -> str:
    text = user_text.strip()
    norm = normalize(text)

    if not text:
        return "Please type your message, or type MENU to see the options."

    mentioned_outlet = detect_outlet_from_text(text)

    if mentioned_outlet:
        remember_outlet_context(chat_id, mentioned_outlet)

    if is_opt_out_request(text):
        OPT_OUT_USERS.add(chat_id)
        save_opt_out_users()
        reset_chat_state(chat_id, include_trial=True, include_inactivity=True)

        return (
            "Noted — you have been unsubscribed and will not receive follow-up messages.\n"
            "If you need help later, reply START."
        )

    if is_opt_in_request(text) and chat_id in OPT_OUT_USERS:
        OPT_OUT_USERS.discard(chat_id)
        save_opt_out_users()
        reset_chat_state(chat_id)
        mark_chat_active(chat_id)

        return start_flow_reply(chat_id, text, "main_menu", main_menu_text())

    if chat_id in OPT_OUT_USERS:
        return "You have opted out. Reply START if you want to chat with Jal Yoga again."

    mark_chat_active(chat_id)

    # Customer Service account commands, e.g. /reply and /close.
    customer_service_command_reply = handle_customer_service_command(chat_id, text)

    if customer_service_command_reply:
        return finish_reply(chat_id, text, customer_service_command_reply, add_menu=False)

    # Easy Customer Service reply mode.
    # After a handoff, Customer Service can just type normally to reply to the active customer.
    support_active_reply = handle_support_active_reply(chat_id, text)

    if support_active_reply:
        return finish_reply(chat_id, text, support_active_reply, add_menu=False)

    language_switch = detect_language_switch_request(text)

    if language_switch:
        remember_user_language(chat_id, language_switch)

        last_assistant_reply = next(
            (item.get("content", "") for item in reversed(CHAT_HISTORY.get(chat_id, [])) if item.get("role") == "assistant"),
            "",
        )

        if last_assistant_reply:
            translated_previous = translate_text_to_language(last_assistant_reply, language_switch)
            reply = (
                f"Okay, I’ll reply in {language_switch} from now on. 🙏\n\n"
                "Here is my previous reply translated:\n\n"
                f"{translated_previous}"
            )
        else:
            translated_question = translate_text_to_language(repeat_current_flow_question(chat_id), language_switch)
            reply = (
                f"Okay, I’ll reply in {language_switch} from now on. 🙏\n\n"
                f"{translated_question}"
            )

        return finish_reply(chat_id, text, reply)

    detect_user_language(chat_id, text)

    if contains_sensitive_keyword(text):
        return finish_reply(
            chat_id,
            text,
            (
                "For your safety, please do not share NRIC, passport numbers, full card numbers, "
                "CVV, OTP, passwords, or bank details here.\n\n"
                "For account-specific or payment-related help, please type CUSTOMER SERVICE."
            ),
        )

    trial_outlet_change_reply = handle_trial_outlet_change_request(chat_id, text)

    if trial_outlet_change_reply:
        return finish_reply(chat_id, text, trial_outlet_change_reply)

    # LIVE CUSTOMER SERVICE CHAT HAS PRIORITY.
    # If the customer is connected to Customer Service, normal messages should go to support,
    # instead of resetting the bot when they type words like hi/hello.
    if chat_id in LIVE_SUPPORT_CHATS:
        if norm in {"menu", "main menu", "restart", "start", "/start", "home"}:
            reset_chat_state(chat_id)

            return start_flow_reply(chat_id, text, "main_menu", main_menu_text())

        if is_customer_close_text(text):
            close_live_support_chat_from_customer(chat_id)

            return finish_reply(
                chat_id,
                text,
                (
                    "Live Customer Service chat has been closed. 🙏\n\n"
                    "You can type MENU to return to the main menu."
                ),
            )

        if contains_blocked_word(text):
            return finish_reply(
                chat_id,
                text,
                "Please keep the conversation respectful. Your message was not sent to Customer Service.",
            )

        sent_to_support = forward_customer_message_to_support(chat_id, text)

        if sent_to_support:
            return finish_reply(
                chat_id,
                text,
                (
                    "I’ve sent your message to Customer Service. 🙏\n\n"
                    "Please wait for their reply here."
                ),
            )

    if is_reset_request(text):
        reset_chat_state(chat_id)

        return start_flow_reply(chat_id, text, "main_menu", main_menu_text())
    
    if any(word in norm for word in ["operating hours", "opening hours", "opening times", "what time do you open"]):
        outlet = detect_outlet_choice(text)
        if outlet:
            raw_answer = knowledge_reply(
                chat_id,
                text,
                (
                    f"The customer wants to know the operating hours for the {outlet} studio. "
                    f"Search the website content carefully for {outlet}'s opening or operating hours. "
                    f"State the timings clearly. If the exact timings for this studio are not listed on the website, "
                    f"politely inform them and tell them they can type CUSTOMER SERVICE to check with the team."
                )
            )
            return finish_reply(chat_id, text, strip_handoff_token(raw_answer))
        else:
            return start_flow_reply(
                chat_id,
                text,
                "general_enquiry_menu",
                studio_prompt("Which studio's operating hours would you like to check?")
            )

    if is_customer_service_contact_request(text):
        outlet = detect_outlet_choice(text) or get_flow_outlet(chat_id)

        if outlet:
            remember_outlet_context(chat_id, outlet)

        return finish_reply(chat_id, text, build_customer_service_contact_reply(outlet))

    # STAFF LIST FOLLOW-UP.
    # If the bot just showed a list of instructor names and the user
    # replies with a short name like "Ravi" or "Sarah Yang", treat it as
    # asking about that specific instructor instead of falling through to
    # the LLM (which would hand them off to Customer Service).
    if (
        STAFF_LIST_PENDING.get(chat_id)
        and is_short_name_reply(text)
        and not is_reset_request(text)
        and not is_customer_service_request(text)
        and not detect_outlet_choice(text)
    ):
        return finish_reply(chat_id, text, handle_staff_info_request(chat_id, text))

    if "staff hub" not in norm and is_staff_info_request(text):
        return finish_reply(chat_id, text, handle_staff_info_request(chat_id, text))

    # CUSTOMER SERVICE SHORTCUT.
    # User can ask for customer service anytime, in any supported language,
    # even while they are inside another flow like trial booking, schedule, or member help.
    if is_customer_service_request(text):
        reply = start_customer_service_flow(chat_id, text)
        return finish_reply(chat_id, text, reply)

    # GENERAL ENQUIRY SHORTCUT.
    # If the customer types "general enquiry" / "general inquiry" directly,
    # move them into the LLM-powered General Enquiry flow.
    if norm in {
        "general enquiry",
        "general enquiries",
        "general inquiry",
        "general inquiries",
        "general question",
        "general questions",
        "i have a question",
        "i want to ask a question",
    }:
        return start_flow_reply(chat_id, text, "general_enquiry_menu", general_enquiry_menu_text())

    class_schedule_reply = handle_class_schedule_request(chat_id, text)

    if class_schedule_reply:
        return finish_reply(chat_id, text, class_schedule_reply)
    
    # --- NEW: Catch trial requests early so the LLM doesn't tell them to fill out a website form ---
    current_stage = get_flow_stage(chat_id)
    if not current_stage.startswith("trial_"):
        trial_phrases = [
            "book a trial", "schedule a trial", "want a trial", "free trial", 
            "any trial", "trial class", "trail lesson", "trial lesson"
        ]
        if any(phrase in norm for phrase in trial_phrases) or norm in ["trial", "trail", "triel"] or ("trial" in norm and "available" in norm):
            return start_flow_reply(chat_id, text, "trial_outlet", trial_start_text())
    # -----------------------------------------------------------------------------------------------

    flow_reply = handle_active_flow_stage(chat_id, text)

    if flow_reply:
        return finish_reply(chat_id, text, flow_reply)

    if is_nearest_outlet_request(text):
        reply = handle_nearest_outlet_request(chat_id, text)
        return finish_reply(chat_id, text, reply)

    if is_all_outlets_request(text):
        clear_flow(chat_id)
        return finish_reply(chat_id, text, studio_locations_text())

    if is_class_cancellation_request(text):
        reply = class_cancellation_policy(
            "To cancel a specific booked class, please reply with:\n"
            "- Outlet\n"
            "- Class name\n"
            "- Date and time"
        )

        return finish_reply(chat_id, text, reply)


    if "refer" in norm and "friend" in norm:
        return start_flow_reply(
            chat_id,
            text,
            "refer_friend_name",
            "That’s wonderful — what is your friend’s full name?",
        )

    if "corporate" in norm or "partnership" in norm or "partnerships" in norm:
        return start_flow_reply(chat_id, text, "corporate_name", "Sure — may I have your full name?")

    if "staff hub" in norm:
        return start_flow_reply(chat_id, text, "staff_name", "Staff Hub 🙏\n\nMay I have the staff name?")

    if is_outlet_contact_request(text):
        outlet = detect_outlet_choice(text)

        if outlet:
            reply = build_outlet_contact_reply(outlet)
            return finish_reply(chat_id, text, reply)

        return start_flow_reply(
            chat_id,
            text,
            "contact_outlet",
            studio_prompt("Which outlet contact would you like?"),
        )

    if is_staff_info_request(text):
        return finish_reply(chat_id, text, handle_staff_info_request(chat_id, text))

    if is_event_request(text):
        outlet = detect_outlet_choice(text)
        if outlet:
            # If they already typed the outlet (e.g., "what workshops at katong"), answer immediately!
            raw_answer = knowledge_reply(
                chat_id,
                text,
                (
                    f"The customer wants to know about events, retreats, or workshops at the {outlet} studio. "
                    f"Search the website content ONLY for workshops/events happening at {outlet}. "
                    "List them neatly using short bullet points. You MUST include a short description of what the workshop is about. "
                    f"If there are no events explicitly listed for {outlet}, say you don't see any scheduled there right now."
                )
            )
            return finish_reply(chat_id, text, strip_handoff_token(raw_answer))
        else:
            # If they didn't specify an outlet, start the flow to ask them
            return start_flow_reply(
                chat_id,
                text,
                "event_outlet",
                studio_prompt("Which studio would you like to check for events and workshops?")
            )
    if is_membership_request(text):
        raw_answer = knowledge_reply(
            chat_id,
            text,
            (
                "The customer is asking about membership types, packages, or pricing. "
                "Briefly explain the available options (Yoga, Pilates, Barre, Reformer options) from the website text. "
                "Explain that rates depend on options and packages, and invite them to type CUSTOMER SERVICE if they want exact price quotes."
            )
        )
        return finish_reply(chat_id, text, strip_handoff_token(raw_answer))
    
    answer = ask_llm(chat_id, text)

    if "[HANDOFF]" in answer:
        clean_answer = strip_handoff_token(answer).strip()
        outlet = detect_outlet_choice(text + "\n" + clean_answer)

        if not outlet:
            reply = queue_pending_handoff(chat_id, text, clean_answer)
            return finish_reply(chat_id, text, reply)

        reply = send_handoff_result(
            chat_id,
            clean_answer,
            outlet,
            f"I’ve sent this summary to our {outlet} Customer Service team on Telegram.",
            "Customer Service Telegram group is not configured yet.",
        )

        return finish_reply(chat_id, text, reply)

    final_reply = add_customer_service_id_note(strip_handoff_token(answer), chat_id)

    return finish_reply(chat_id, text, final_reply)



# FLASK ROUTES

@app.route("/", methods=["GET"])
def home():
    return render_template("index.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "healthy",
            "openai_configured": bool(OPENAI_API_KEY),
            "telegram_configured": bool(TELEGRAM_BOT_TOKEN),
            "customer_service_telegram_configured": bool(CUSTOMER_SERVICE_TELEGRAM_CHAT_ID),
            "inactivity_checker_started": INACTIVITY_THREAD_STARTED,
            "active_inactivity_chats": len(INACTIVITY_STATE),
            "inactivity_warning_seconds": INACTIVITY_WARNING_SECONDS,
            "inactivity_close_seconds": INACTIVITY_CLOSE_SECONDS,
            "inactivity_check_seconds": INACTIVITY_CHECK_SECONDS,
            "chatlog_enabled": CHATLOG_ENABLED,
            "chatlog_storage": chatlog_storage_label(),
            "chatlog_local_enabled": CHATLOG_LOCAL_ENABLED,
            "chatlog_google_sheet_configured": chatlog_google_sheet_configured(),
            "google_sheet_worksheet": GOOGLE_SHEET_WORKSHEET,
            "chatlog_file": os.path.abspath(CHATLOG_FILE) if CHATLOG_FILE else "",
            "chatlog_file_exists": bool(CHATLOG_FILE and os.path.exists(CHATLOG_FILE)),
            "chatlog_dir": os.path.abspath(CHATLOG_DIR),
            "chatlog_dir_exists": os.path.isdir(CHATLOG_DIR),
        }
    )


def debug_routes_enabled() -> bool:
    if os.getenv("FLASK_DEBUG", "false").lower() == "true":
        return True

    supplied_token = request.headers.get("X-Debug-Token", "") or request.args.get("token", "")
    return bool(DEBUG_ROUTE_TOKEN and supplied_token == DEBUG_ROUTE_TOKEN)


def require_debug_route_access():
    if debug_routes_enabled():
        return None

    return jsonify({"status": "forbidden"}), 403


@app.route("/debug/outlets", methods=["GET"])
def debug_outlets():
    forbidden = require_debug_route_access()

    if forbidden:
        return forbidden

    outlet_data = {}

    for studio in STUDIOS:
        name = studio["name"]
        chat_id = outlet_telegram_chat_id(name)

        outlet_data[name] = {
            "address": studio["address"],
            "telegram_chat_id_configured": bool(chat_id),
            "telegram_chat_id_last_4": chat_id[-4:] if chat_id else "",
            "env_key": env_key_for_outlet_telegram_chat(name),
        }

    return jsonify(
        {
            "status": "ok",
            "fallback_customer_service_configured": bool(CUSTOMER_SERVICE_TELEGRAM_CHAT_ID),
            "outlets": outlet_data,
        }
    )


@app.route("/debug/trial-bookings", methods=["GET"])
def debug_trial_bookings():
    forbidden = require_debug_route_access()

    if forbidden:
        return forbidden

    return jsonify(
        {
            "status": "ok",
            "trial_booking_count": len(TRIAL_BOOKINGS),
            "trial_bookings": {
                chat_id[-4:]: booking
                for chat_id, booking in TRIAL_BOOKINGS.items()
            },
        }
    )


@app.route("/debug/chatlogs", methods=["GET"])
def debug_chatlogs():
    forbidden = require_debug_route_access()

    if forbidden:
        return forbidden

    logs = []

    if chatlog_google_sheet_configured():
        logs_by_chat: Dict[str, Dict[str, Any]] = {}

        for entry in read_google_sheet_chatlog_entries(limit=0):
            chat_id = str(entry.get("chat_id", "")).strip()

            if not chat_id:
                continue

            log = logs_by_chat.setdefault(
                chat_id,
                {
                    "chat_id": chat_id,
                    "count": 0,
                    "modified_sg": "",
                    "view_url": (
                        f"/debug/chatlog?chat_id={chat_id}&token={request.args.get('token', '')}"
                        if request.args.get("token", "")
                        else f"/debug/chatlog?chat_id={chat_id}"
                    ),
                },
            )
            log["count"] += 1
            log["modified_sg"] = entry.get("time_sg", "")

        logs = list(logs_by_chat.values())
    elif os.path.isdir(CHATLOG_DIR):
        for filename in sorted(os.listdir(CHATLOG_DIR)):
            if not filename.endswith(".jsonl"):
                continue

            file_path = os.path.join(CHATLOG_DIR, filename)
            stat = os.stat(file_path)
            chat_id = filename[:-6]
            logs.append(
                {
                    "chat_id": chat_id,
                    "filename": filename,
                    "size_bytes": stat.st_size,
                    "modified_sg": datetime.fromtimestamp(
                        stat.st_mtime,
                        ZoneInfo("Asia/Singapore"),
                    ).isoformat(),
                    "view_url": (
                        f"/debug/chatlog?chat_id={chat_id}&token={request.args.get('token', '')}"
                        if request.args.get("token", "")
                        else f"/debug/chatlog?chat_id={chat_id}"
                    ),
                }
            )

    logs.sort(key=lambda item: item["modified_sg"], reverse=True)

    return jsonify(
        {
            "status": "ok",
            "chatlog_enabled": CHATLOG_ENABLED,
            "chatlog_storage": chatlog_storage_label(),
            "google_sheet_configured": chatlog_google_sheet_configured(),
            "google_sheet_worksheet": GOOGLE_SHEET_WORKSHEET,
            "chatlog_dir": CHATLOG_DIR,
            "count": len(logs),
            "logs": logs,
        }
    )


@app.route("/debug/chatlog", methods=["GET"])
def debug_chatlog():
    forbidden = require_debug_route_access()

    if forbidden:
        return forbidden

    chat_id = request.args.get("chat_id", "").strip()

    if not chat_id:
        return jsonify({"status": "error", "message": "Missing chat_id"}), 400

    try:
        limit = int(request.args.get("limit", str(CHATLOG_MAX_VIEW_LINES)))
    except ValueError:
        limit = CHATLOG_MAX_VIEW_LINES

    limit = max(1, min(limit, CHATLOG_MAX_VIEW_LINES))
    entries = read_chatlog_entries(chat_id, limit=limit)

    return jsonify(
        {
            "status": "ok",
            "chat_id": chat_id,
            "limit": limit,
            "count": len(entries),
            "entries": entries,
        }
    )


@app.route("/debug/chat-log-file", methods=["GET"])
def debug_chat_log_file():
    forbidden = require_debug_route_access()

    if forbidden:
        return forbidden

    try:
        limit = int(request.args.get("limit", str(CHATLOG_MAX_VIEW_LINES)))
    except ValueError:
        limit = CHATLOG_MAX_VIEW_LINES

    limit = max(1, min(limit, CHATLOG_MAX_VIEW_LINES))
    entries = read_chatlog_file_entries(limit=limit)

    return jsonify(
        {
            "status": "ok",
            "chatlog_storage": chatlog_storage_label(),
            "google_sheet_configured": chatlog_google_sheet_configured(),
            "google_sheet_worksheet": GOOGLE_SHEET_WORKSHEET,
            "chatlog_file": CHATLOG_FILE,
            "chatlog_file_exists": bool(CHATLOG_FILE and os.path.exists(CHATLOG_FILE)),
            "limit": limit,
            "count": len(entries),
            "entries": entries,
        }
    )


@app.route("/debug/test-chatlog", methods=["GET"])
def debug_test_chatlog():
    forbidden = require_debug_route_access()

    if forbidden:
        return forbidden

    write_chatlog(
        "DEBUG_TEST",
        "incoming",
        "debug_user",
        "debug test message",
        {"platform": "debug"},
    )
    write_chatlog(
        "DEBUG_TEST",
        "outgoing",
        "debug_bot",
        "debug test reply",
        {"platform": "debug"},
    )

    return jsonify(
        {
            "status": "ok",
            "message": f"Test chatlog written to {chatlog_storage_label()}.",
            "chatlog_storage": chatlog_storage_label(),
            "google_sheet_configured": chatlog_google_sheet_configured(),
            "google_sheet_worksheet": GOOGLE_SHEET_WORKSHEET,
            "chatlog_file": CHATLOG_FILE,
            "chatlog_file_exists": bool(CHATLOG_FILE and os.path.exists(CHATLOG_FILE)),
            "file_view_path": "/debug/chat-log-file",
            "chat_view_path": "/debug/chatlog?chat_id=DEBUG_TEST",
        }
    )


@app.route("/telegram/webhook", methods=["GET"])
def telegram_webhook_test():
    return jsonify(
        {
            "status": "ok",
            "message": "Telegram webhook route exists. Telegram will use POST here.",
            "inactivity_checker_started": INACTIVITY_THREAD_STARTED,
        }
    )


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    if TELEGRAM_SECRET_TOKEN:
        incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")

        if incoming_secret != TELEGRAM_SECRET_TOKEN:
            return jsonify({"status": "forbidden"}), 403

    update = request.get_json(silent=True) or {}

    message = (
        update.get("message")
        or update.get("edited_message")
        or update.get("channel_post")
        or update.get("edited_channel_post")
    )

    if not message:
        return jsonify({"status": "ignored", "reason": "no message"}), 200

    chat = message.get("chat", {})
    chat_id = str(chat.get("id", ""))
    chat_type = chat.get("type", "")

    if not chat_id:
        return jsonify({"status": "ignored", "reason": "no chat id"}), 200

    text = message.get("text", "")

    print(
        f"INCOMING TELEGRAM UPDATE | chat_id={chat_id} | chat_type={chat_type} | text={text}",
        flush=True,
    )

    from_user = message.get("from", {})
    write_chatlog(
        chat_id,
        "incoming",
        "customer_service" if is_customer_service_chat(chat_id) else "customer",
        text,
        {
            "platform": "telegram",
            "chat_type": chat_type,
            "message_id": message.get("message_id"),
            "telegram_username": from_user.get("username", ""),
            "telegram_first_name": from_user.get("first_name", ""),
        },
    )

    if chat_type in {"group", "supergroup", "channel"} and not is_customer_service_chat(chat_id):
        return jsonify(
            {
                "status": "ignored",
                "reason": "group_or_channel_message_logged",
                "chat_id": chat_id,
                "chat_type": chat_type,
            }
        ), 200

    if not text:
        send_telegram_message(
            chat_id,
            "I can currently handle text messages only. Please type your message, or type MENU.",
        )

        return jsonify({"status": "ok"}), 200

    try:
        reply_to_message = message.get("reply_to_message") or {}
        reply_to_text = reply_to_message.get("text", "")

        customer_service_reply = handle_customer_service_reply_to_message(
            chat_id,
            text,
            reply_to_text,
        )

        if customer_service_reply:
            send_telegram_message(chat_id, customer_service_reply)
            return jsonify({"status": "ok"}), 200

        reply = process_message(chat_id, text)
        reply = translate_reply_if_needed(chat_id, text, reply)

        send_telegram_message(chat_id, reply)

    except Exception as e:
        print("ERROR:", str(e), flush=True)
        traceback.print_exc()

        try:
            send_telegram_message(
                chat_id,
                "I’m sorry — something went wrong on our side. Please type CUSTOMER SERVICE.",
            )
        except Exception:
            pass

    return jsonify({"status": "ok"}), 200


def build_bot_reply(chat_id: str, user_text: str) -> str:
    write_chatlog(
        chat_id,
        "incoming",
        "local_user",
        user_text,
        {"platform": "local_test"},
    )

    reply = process_message(chat_id, user_text)
    final_reply = translate_reply_if_needed(chat_id, user_text, reply)

    write_chatlog(
        chat_id,
        "outgoing",
        "local_bot",
        final_reply,
        {"platform": "local_test"},
    )

    return final_reply


if __name__ == "__main__":
    # Render passes the port as an environment variable, fallback to 5000 for local testing
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    )