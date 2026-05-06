import json
import os
import re
import threading
import time
import traceback
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Dict, List
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, render_template
from openai import OpenAI

load_dotenv()

app = Flask(__name__)

# =========================
# ENV VARIABLES
# =========================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "")
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "")

CUSTOMER_SERVICE_WHATSAPP_NUMBER = os.getenv("CUSTOMER_SERVICE_WHATSAPP_NUMBER", "")
CUSTOMER_SERVICE_TELEGRAM_CHAT_ID = os.getenv("CUSTOMER_SERVICE_TELEGRAM_CHAT_ID", "")

PORT = int(os.getenv("PORT", "5000"))
OPT_OUT_FILE = os.getenv("OPT_OUT_FILE", "telegram_opt_out_users.json")
SCHEDULE_FILE = os.getenv("SCHEDULE_FILE", "schedule.json")

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


# =========================
# MEMORY
# =========================

CHAT_HISTORY: Dict[str, List[Dict[str, str]]] = {}
PENDING_HANDOFFS: Dict[str, Dict[str, str]] = {}
TRIAL_BOOKINGS: Dict[str, Dict[str, str]] = {}
FLOW_STATE: Dict[str, Dict[str, str]] = {}
INACTIVITY_STATE: Dict[str, Dict[str, object]] = {}
USER_LANGUAGE: Dict[str, str] = {}
LIVE_SUPPORT_CHATS: Dict[str, Dict[str, str]] = {}

INACTIVITY_WARNING_SECONDS = 600
INACTIVITY_CLOSE_SECONDS = 1200
INACTIVITY_CHECK_SECONDS = 30
INACTIVITY_THREAD_STARTED = False


# =========================
# WORD LISTS
# =========================

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

    for blocked in BLOCKED_WORDS:
        if blocked in words:
            return True

    return False


# =========================
# BASIC HELPERS
# =========================

def normalize(text: str) -> str:
    return " ".join((text or "").strip().lower().replace("’", "'").split())


def simple_text(text: str) -> str:
    text = normalize(text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def now_sg() -> str:
    return datetime.now(ZoneInfo("Asia/Singapore")).isoformat()


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


KNOWLEDGE_TEXT = load_knowledge_text()


# =========================
# STUDIOS
# =========================

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


def studio_names() -> List[str]:
    return [studio["name"] for studio in STUDIOS]


def studio_options_text(include_not_specified: bool = False) -> str:
    options = []

    for index, name in enumerate(studio_names(), start=1):
        options.append(f"{index}. {name}")

    if include_not_specified:
        options.append(f"{len(options) + 1}. Not specified")

    return "\n".join(options)


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

    padded = f" {clean} "

    for studio_name in studio_names():
        for alias in studio_aliases(studio_name):
            if f" {alias} " in padded:
                return studio_name

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
    for studio in STUDIOS:
        if studio["name"].lower() == outlet_name.lower():
            return studio["address"]

    return ""


# =========================
# CONTACT CONFIG
# =========================

def env_key_for_outlet_whatsapp(outlet_name: str) -> str:
    key = re.sub(r"[^A-Za-z0-9]+", "_", outlet_name.upper()).strip("_")
    return f"{key}_WHATSAPP_NUMBER"


def outlet_whatsapp_number(outlet_name: str) -> str:
    return os.getenv(env_key_for_outlet_whatsapp(outlet_name), "")


def env_key_for_outlet_telegram_chat(outlet_name: str) -> str:
    key = re.sub(r"[^A-Za-z0-9]+", "_", outlet_name.upper()).strip("_")
    return f"{key}_TELEGRAM_CHAT_ID"


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


# =========================
# HISTORY / FLOW
# =========================

def reset_history(chat_id: str) -> None:
    CHAT_HISTORY.pop(chat_id, None)


def add_history(chat_id: str, role: str, content: str) -> None:
    CHAT_HISTORY.setdefault(chat_id, []).append(
        {
            "role": role,
            "content": content,
        }
    )

    CHAT_HISTORY[chat_id] = CHAT_HISTORY[chat_id][-20:]


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

def repeat_current_flow_question(chat_id: str) -> str:
    stage = get_flow_stage(chat_id)
    flow = get_flow(chat_id)

    if stage == "main_menu":
        return main_menu_text()

    if stage == "current_member_menu":
        return current_member_menu_text()

    if stage == "general_enquiry_menu":
        return general_enquiry_menu_text()

    if stage == "trial_outlet":
        return (
            "Which studio would you prefer?\n\n"
            f"{studio_options_text()}"
        )

    if stage == "trial_name":
        return "May I have your full name?"

    if stage == "trial_goal":
        name = flow.get("name", "")

        if name:
            return f"Thanks, {name.title()} — what’s your fitness goal for the trial?"

        return "What’s your fitness goal for the trial?"

    if stage == "refer_friend_name":
        return "That’s wonderful — what is your friend’s full name?"

    if stage == "refer_friend_contact":
        return "Thanks — what is your friend’s contact number?"

    if stage == "refer_friend_studio":
        return (
            "Which studio would your friend prefer?\n\n"
            f"{studio_options_text()}"
        )

    if stage == "corporate_name":
        return "Sure — may I have your full name?"

    if stage == "corporate_email":
        return "Thanks. What is your email address?"

    if stage == "corporate_message":
        return (
            "Thank you. Please briefly tell us what your Corporate / Partnership enquiry is about.\n\n"
            "For example:\n"
            "- corporate wellness programme\n"
            "- company yoga class\n"
            "- partnership proposal\n"
            "- event collaboration"
        )

    if stage == "staff_name":
        return "Staff Hub 🙏\n\nMay I have the staff name?"

    if stage == "staff_studio":
        return (
            "Which studio is this related to?\n\n"
            f"{studio_options_text()}"
        )

    if stage == "staff_room":
        return "Which room is this related to?"

    if stage == "staff_member_booking_details":
        return (
            "Please share the member and booking details.\n\n"
            "For example:\n"
            "- Member name\n"
            "- Booking date and time\n"
            "- Class name\n"
            "- Issue or request"
        )

    if stage == "schedule_outlet":
        return (
            "Please choose an outlet by number or name:\n\n"
            f"{studio_options_text()}"
        )

    if stage == "contact_outlet":
        return (
            "Please choose an outlet by number or name:\n\n"
            f"{studio_options_text()}"
        )

    if stage == "pending_handoff_outlet":
        return ask_outlet_before_handoff_text()

    return main_menu_text()    


def add_menu_hint(reply: str) -> str:
    if "Reply MENU to return to the main menu." in reply:
        return reply

    return reply.rstrip() + "\n\nReply MENU to return to the main menu."


def finish_reply(chat_id: str, user_text: str, reply: str, add_menu: bool = True) -> str:
    final_reply = add_menu_hint(reply) if add_menu else reply

    add_history(chat_id, "user", user_text)
    add_history(chat_id, "assistant", final_reply)

    return final_reply


# =========================
# SAFETY / INTENT HELPERS
# =========================

def is_opt_out_request(text: str) -> bool:
    t = normalize(text)

    if t in OPT_OUT_WORDS:
        return True

    return any(phrase in t for phrase in OPT_OUT_WORDS if " " in phrase or "-" in phrase)


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


def is_customer_service_request(text: str) -> bool:
    t = normalize(text)
    clean = simple_text(text)
    raw = (text or "").strip().lower()

    if not raw:
        return False

    # Exact short commands.
    exact_words = {
        "cs",
        "support",
        "helpdesk",
        "客服",
        "人工",
        "人工客服",
        "真人",
        "真人客服",
        "客户服务",
        "客戶服務",
        "我要客服",
        "我要人工",
        "转人工",
        "轉人工",
        "联系人工",
        "聯繫人工",
        "hubungi admin",
        "admin",
        "khidmat pelanggan",
        "layanan pelanggan",
        "bantuan manusia",
        "சேவை",
        "உதவி",
    }

    if t in exact_words or raw in exact_words:
        return True

    # English / Singlish / typo-friendly phrases.
    phrases = [
        "customer service",
        "customer support",
        "customer care",
        "customer servide",
        "customer servis",
        "cust service",
        "cs team",
        "support team",
        "contact support",
        "contact customer service",
        "speak to customer service",
        "talk to customer service",
        "talk to someone",
        "speak to someone",
        "talk to a human",
        "speak to a human",
        "human agent",
        "real person",
        "live agent",
        "live chat",
        "need human",
        "need staff help",
        "talk to staff",
        "speak to staff",
        "connect me to staff",
        "connect me to customer service",
        "can i talk to someone",
        "can i speak to someone",
        "i want customer service",
        "i need customer service",
        "i want to talk to staff",
        "i need to talk to staff",
    ]

    if any(phrase in t for phrase in phrases):
        return True

    # Chinese phrases.
    chinese_phrases = [
        "客服",
        "人工客服",
        "真人客服",
        "客户服务",
        "客戶服務",
        "联系工作人员",
        "聯繫工作人員",
        "联系人工",
        "聯繫人工",
        "转人工",
        "轉人工",
        "找人",
        "我要找人",
        "我要人工",
        "我要客服",
        "我想找客服",
        "可以找客服吗",
        "可以找客服嗎",
        "我要和人说话",
        "我要和人說話",
    ]

    if any(phrase in raw for phrase in chinese_phrases):
        return True

    # Malay / Indonesian phrases.
    malay_phrases = [
        "khidmat pelanggan",
        "layanan pelanggan",
        "servis pelanggan",
        "customer servis",
        "bercakap dengan staff",
        "bercakap dengan manusia",
        "cakap dengan orang",
        "nak cakap dengan staff",
        "nak customer service",
        "saya mahu customer service",
        "saya nak bantuan manusia",
        "hubungi admin",
        "hubungi staf",
        "bantuan admin",
    ]

    if any(phrase in raw for phrase in malay_phrases):
        return True

    # Tamil phrases.
    tamil_phrases = [
        "வாடிக்கையாளர் சேவை",
        "வாடிக்கையாளர் ஆதரவு",
        "மனித உதவி",
        "ஒருவரிடம் பேச",
        "உதவி வேண்டும்",
        "staff உடன் பேச",
    ]

    if any(phrase in raw for phrase in tamil_phrases):
        return True

    # Fuzzy matching for short typo messages like "custumer servise".
    fuzzy_targets = [
        "customer service",
        "customer support",
        "customer care",
        "support team",
        "human agent",
        "real person",
        "live agent",
        "talk to someone",
        "speak to someone",
        "talk to staff",
        "speak to staff",
    ]

    words = clean.split()
    for target in fuzzy_targets:
        target_clean = simple_text(target)
        target_size = len(target_clean.split())

        if target_size == 0 or len(words) < target_size:
            continue

        for i in range(len(words) - target_size + 1):
            chunk = " ".join(words[i:i + target_size])
            score = SequenceMatcher(None, chunk, target_clean).ratio()

            if score >= 0.78:
                return True

    return False


def is_outlet_contact_request(text: str) -> bool:
    t = normalize(text)

    contact_words = [
        "contact",
        "phone",
        "number",
        "whatsapp",
        "call",
        "hotline",
    ]

    return any(word in t for word in contact_words)


# =========================
# LANGUAGE
# =========================
def detect_language_switch_request(text: str) -> str:
    t = normalize(text)
    clean = simple_text(text)
    raw = text.strip()

    english_words = {"english", "eng"}
    chinese_words = {"chinese", "中文", "华文", "華文", "mandarin"}
    malay_words = {"malay", "bahasa melayu", "melayu"}
    tamil_words = {"tamil", "தமிழ்"}

    switch_words = [
        "change",
        "switch",
        "translate",
        "reply",
        "speak",
        "use",
        "turn",
        "make",
        "convert",
        "chnage",
        "chage",
    ]

    # Direct exact requests
    if t in {
        "english",
        "eng",
        "speak english",
        "reply english",
        "reply in english",
        "use english",
        "change to english",
        "change into english",
        "change it to english",
        "change it into english",
        "english please",
    }:
        return "English"

    if t in {
        "chinese",
        "中文",
        "华文",
        "華文",
        "mandarin",
        "speak chinese",
        "reply chinese",
        "reply in chinese",
        "use chinese",
        "change to chinese",
        "change into chinese",
        "change it to chinese",
        "change it into chinese",
        "translate to chinese",
        "translate into chinese",
        "chinese please",
    }:
        return "Chinese"

    if t in {
        "malay",
        "bahasa melayu",
        "reply malay",
        "reply in malay",
        "use malay",
        "change to malay",
        "change into malay",
        "change it to malay",
        "change it into malay",
        "translate to malay",
        "malay please",
    }:
        return "Malay"

    if t in {
        "tamil",
        "தமிழ்",
        "reply tamil",
        "reply in tamil",
        "use tamil",
        "change to tamil",
        "change into tamil",
        "change it to tamil",
        "change it into tamil",
        "translate to tamil",
        "tamil please",
    }:
        return "Tamil"

    # Flexible typo-friendly detection
    has_switch_word = any(word in clean for word in switch_words)

    if has_switch_word:
        if any(word in clean for word in english_words):
            return "English"

        if any(word in clean for word in chinese_words) or any(word in raw for word in chinese_words):
            return "Chinese"

        if any(word in clean for word in malay_words):
            return "Malay"

        if any(word in clean for word in tamil_words) or any(word in raw for word in tamil_words):
            return "Tamil"

    return ""

def detect_user_language(chat_id: str, user_text: str) -> str:
    text = normalize(user_text)

    if text.isdigit():
        return USER_LANGUAGE.get(chat_id, "English")

    if text in {"salve", "ola", "olá", "oi", "bom dia", "boa tarde", "boa noite"}:
        USER_LANGUAGE[chat_id] = "Portuguese"
        return "Portuguese"

    if any(word in user_text for word in ["你好", "您好", "哈咯"]):
        USER_LANGUAGE[chat_id] = "Chinese"
        return "Chinese"

    if text in {"salam", "selamat pagi", "apa khabar"}:
        USER_LANGUAGE[chat_id] = "Malay"
        return "Malay"

    if any(word in user_text for word in ["வணக்கம்"]):
        USER_LANGUAGE[chat_id] = "Tamil"
        return "Tamil"

    if detect_outlet_from_text(user_text):
        return USER_LANGUAGE.get(chat_id, "English")

    if re.fullmatch(r"[A-Za-z][A-Za-z\s.'-]{1,60}", user_text.strip()) and len(user_text.strip().split()) <= 4:
        return USER_LANGUAGE.get(chat_id, "English")

    if len(text) <= 2:
        return USER_LANGUAGE.get(chat_id, "English")

    if not client:
        return USER_LANGUAGE.get(chat_id, "English")

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=(
                "Detect the language of the user's message. "
                "Return only the language name in English. "
                "Examples: English, Chinese, Malay, Tamil, Portuguese, Spanish, Japanese, Korean. "
                "If the message is only a name, number, outlet, or unclear, return Unknown."
            ),
            input=user_text,
        )

        language = (response.output_text or "").strip()

        if language and language.lower() != "unknown":
            USER_LANGUAGE[chat_id] = language
            return language

    except Exception as e:
        print("LANGUAGE DETECT ERROR:", str(e), flush=True)

    return USER_LANGUAGE.get(chat_id, "English")


# =========================
# LLM REPLIES
# =========================

def knowledge_reply(chat_id: str, user_text: str, task: str, fallback: str = "") -> str:
    if not client:
        return fallback or (
            "I’m sorry — the AI answer service is not configured yet.\n"
            "Please type CUSTOMER SERVICE and our team will follow up."
        )

    language = detect_user_language(chat_id, user_text)

    history_text = "\n".join(
        f"{item['role'].upper()}: {item['content']}"
        for item in CHAT_HISTORY.get(chat_id, [])[-8:]
    )

    instructions = f"""
You are Jal Yoga Singapore's Telegram customer-service assistant.

Use ONLY:
1. The knowledge file below.
2. The live customer-service config below.
3. The recent chat context below.

Language rule:
- Customer language: {language}
- Translate all customer-facing wording into the customer language where possible.
- If the customer message is only a number, keep replying in the stored customer language.
- Preserve outlet names, menu numbers, phone numbers, Telegram IDs, links, and formatting.
- Do not add information that is not in the knowledge file.

Core rules:
- Help only with Jal Yoga enquiries.
- Be warm, concise, professional, and helpful.
- Do not invent prices, promotions, schedules, trainers, live slots, outlet phone numbers, policies, or membership details.
- If information is not confirmed, say you are not fully sure and use [HANDOFF].
- Ask only one question at a time.
- Do not mention Meta, webhook, Python, OpenAI, code, or internal system details.

Live customer-service config:
{live_contact_config_text()}

Knowledge file:
{KNOWLEDGE_TEXT}

Recent chat:
{history_text}

Task:
{task}
"""

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
        print("KNOWLEDGE REPLY ERROR:", str(e), flush=True)
        traceback.print_exc()

    return fallback or "I’m sorry — I’m not fully sure based on the information I have.\n[HANDOFF]"


def ask_llm(chat_id: str, user_text: str) -> str:
    if not client:
        return (
            "I’m sorry — the AI answer service is not configured yet.\n"
            "Please type CUSTOMER SERVICE and our team will follow up."
        )

    language = detect_user_language(chat_id, user_text)

    history_text = "\n".join(
        f"{item['role'].upper()}: {item['content']}"
        for item in CHAT_HISTORY.get(chat_id, [])[-12:]
    )

    instructions = f"""
You are Jal Yoga Singapore's Telegram customer-service assistant.

Use ONLY:
1. The knowledge file below.
2. The live customer-service config below.
3. The recent chat context below.

Language:
- Customer language: {language}
- Reply in the customer's language where possible.
- Preserve outlet names, menu numbers, phone numbers, Telegram IDs, and links.

Core behaviour:
- Answer only Jal Yoga enquiries.
- Use only this knowledge file, recent chat context, and live contact config.
- Do not invent prices, promotions, schedules, trainers, live slots, outlet phone numbers, policies, or membership details.
- Ask one question at a time.
- Continue the current flow based on recent chat context.
- Use details the user already provided.
- Do not restart a flow unless the user says MENU, START, HOME, MAIN MENU, or RESTART.
- If information is not confirmed, say you are not fully sure and use [HANDOFF].

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

Knowledge file:
{KNOWLEDGE_TEXT}

Current time in Singapore:
{now_sg()}

Recent chat:
{history_text}
"""

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=instructions,
            input=user_text,
        )

        answer = (response.output_text or "").strip()

        if answer:
            return answer

    except Exception as e:
        print("OPENAI ERROR:", str(e), flush=True)
        traceback.print_exc()

    return "I’m sorry — I’m not fully sure based on the information I have.\n[HANDOFF]"


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


# =========================
# MENU TEXT
# =========================

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
        "What would you like to know more about?\n\n"
        "1. Studio Locations & Operating Hours\n"
        "2. Class Types\n"
        "3. Current Events & Retreat"
    )


def ask_outlet_before_handoff_text() -> str:
    return (
        "Before I pass this to our Customer Service team, do you have a specific outlet for this enquiry?\n\n"
        "Please reply with one of these:\n"
        f"{studio_options_text(include_not_specified=True)}"
    )


def start_customer_service_flow(chat_id: str, text: str) -> str:
    outlet = detect_outlet_choice(text)

    clean_answer = (
        "I’ll pass this to our Customer Service team.\n\n"
        "Summary:\n"
        "- Topic: Customer Service Request\n"
        f"- Outlet: {outlet or 'Not specified'}\n"
        f"- Message: {text}"
    )

    if not outlet:
        PENDING_HANDOFFS[chat_id] = {
            "user_message": text,
            "clean_answer": clean_answer,
        }
        set_flow(chat_id, "pending_handoff_outlet")
        return ask_outlet_before_handoff_text()

    clear_flow(chat_id)
    sent = send_customer_service_handoff_to_telegram(chat_id, clean_answer, outlet)

    if sent:
        return (
            f"{clean_answer}\n\n"
            f"I’ve sent this summary to our {outlet} Customer Service team on Telegram. 🙏\n\n"
            "You are now connected to Customer Service. You may reply here and our team will receive your message."
        )

    return (
        f"{clean_answer}\n\n"
        "Customer Service Telegram chat is not configured yet."
    )


# =========================
# TELEGRAM SEND
# =========================

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

    return True



# =========================
# LIVE CUSTOMER SERVICE CHAT
# =========================

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
    target_chat_id = ""

    if outlet and outlet != "Not specified":
        target_chat_id = outlet_telegram_chat_id(outlet)

    if not target_chat_id:
        target_chat_id = CUSTOMER_SERVICE_TELEGRAM_CHAT_ID

    return str(target_chat_id or "")


def open_live_support_chat(customer_chat_id: str, target_chat_id: str, outlet: str = "Not specified") -> None:
    if not target_chat_id:
        return

    LIVE_SUPPORT_CHATS[str(customer_chat_id)] = {
        "target_chat_id": str(target_chat_id),
        "outlet": outlet or "Not specified",
        "last_active_at": now_sg(),
    }


def support_reply_instructions(customer_chat_id: str) -> str:
    return (
        "To reply to this customer, type:\n"
        f"/reply {customer_chat_id} your message\n\n"
        "To close the live chat, type:\n"
        f"/close {customer_chat_id}"
    )


def handle_customer_service_command(chat_id: str, text: str) -> str:
    clean = text.strip()
    lower = clean.lower()

    if lower.startswith("/reply"):
        if not is_customer_service_chat(chat_id):
            return "Sorry, this command is only for Customer Service."

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

        if contains_blocked_word(message):
            return "Message blocked. Please use professional Customer Service language."

        send_telegram_message(
            customer_chat_id,
            (
                "Jal Yoga Customer Service 🙏\n\n"
                f"{message}\n\n"
                "You may reply here and our Customer Service team will receive your message."
            ),
        )

        live_chat = LIVE_SUPPORT_CHATS.get(customer_chat_id, {})

        open_live_support_chat(
            customer_chat_id,
            str(chat_id),
            live_chat.get("outlet", "Customer Service"),
        )

        return f"Sent to customer {customer_chat_id}."

    if lower.startswith("/close"):
        if not is_customer_service_chat(chat_id):
            return "Sorry, this command is only for Customer Service."

        parts = clean.split(maxsplit=1)

        if len(parts) < 2:
            return (
                "Please use this format:\n\n"
                "/close CUSTOMER_CHAT_ID\n\n"
                "Example:\n"
                "/close 123456789"
            )

        customer_chat_id = parts[1].strip()
        LIVE_SUPPORT_CHATS.pop(customer_chat_id, None)

        send_telegram_message(
            customer_chat_id,
            (
                "Customer Service has closed this chat for now. 🙏\n\n"
                "If you need help again, type CUSTOMER SERVICE anytime."
            ),
        )

        return f"Closed live chat with customer {customer_chat_id}."

    return ""


def forward_customer_message_to_support(customer_chat_id: str, text: str) -> bool:
    live_chat = LIVE_SUPPORT_CHATS.get(customer_chat_id, {})
    target_chat_id = live_chat.get("target_chat_id", "")

    if not target_chat_id:
        target_chat_id = CUSTOMER_SERVICE_TELEGRAM_CHAT_ID

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
        f"Reply using:\n/reply {customer_chat_id} your message\n\n"
        f"Close chat using:\n/close {customer_chat_id}"
    )

    send_telegram_message(target_chat_id, message)
    return True

# =========================
# CUSTOMER SERVICE HANDOFF
# =========================

def send_customer_service_handoff_to_telegram(customer_chat_id: str, clean_answer: str, outlet: str) -> bool:
    target_chat_id = get_support_target_chat_id(outlet)

    if not target_chat_id:
        print(
            f"CUSTOMER SERVICE HANDOFF SKIPPED: No Telegram chat ID for outlet={outlet}",
            flush=True,
        )
        return False

    open_live_support_chat(customer_chat_id, target_chat_id, outlet or "Not specified")

    message = (
        "New Customer Service Handoff 🙏\n\n"
        f"{clean_answer}\n\n"
        f"Customer Telegram Chat ID: {customer_chat_id}\n\n"
        "This customer is now connected to live Customer Service chat.\n\n"
        f"{support_reply_instructions(customer_chat_id)}"
    )

    try:
        send_telegram_message(target_chat_id, message)
        return True

    except Exception as e:
        print("CUSTOMER SERVICE HANDOFF SEND ERROR:", str(e), flush=True)
        traceback.print_exc()
        return False


# =========================
# TRIAL BOOKING
# =========================

def send_trial_booking_to_outlet(customer_chat_id: str, booking: Dict[str, str]) -> bool:
    outlet = booking.get("outlet", "")
    name = booking.get("name", "")
    fitness_goal = booking.get("fitness_goal", "")

    if not outlet:
        return False

    TRIAL_BOOKINGS[customer_chat_id] = {
        "outlet": outlet,
        "name": name,
        "fitness_goal": fitness_goal,
    }

    target_chat_id = get_support_target_chat_id(outlet)

    if not target_chat_id:
        print("TRIAL BOOKING SEND SKIPPED: No target chat ID", flush=True)
        return False

    open_live_support_chat(customer_chat_id, target_chat_id, outlet)

    message = (
        "New Trial Booking Received 🙏\n\n"
        f"Outlet: {outlet}\n"
        "Class: Trial Class\n"
        f"Name: {name or 'Not provided'}\n"
        f"Fitness Goal: {fitness_goal or 'Not provided'}\n\n"
        f"Customer Telegram Chat ID: {customer_chat_id}\n\n"
        "This customer is now connected to live Customer Service chat.\n\n"
        f"{support_reply_instructions(customer_chat_id)}"
    )

    try:
        send_telegram_message(target_chat_id, message)
        return True
    except Exception as e:
        print("TRIAL BOOKING SEND ERROR:", str(e), flush=True)
        traceback.print_exc()
        return False


# =========================
# REFER FRIEND
# =========================

def send_refer_friend_to_outlet(customer_chat_id: str, referral: Dict[str, str]) -> bool:
    outlet = referral.get("preferred_studio", "")
    friend_name = referral.get("friend_name", "")
    friend_contact = referral.get("friend_contact", "")

    if not outlet:
        return False

    target_chat_id = get_support_target_chat_id(outlet)

    if not target_chat_id:
        print("REFER FRIEND SEND SKIPPED: No target chat ID", flush=True)
        return False

    open_live_support_chat(customer_chat_id, target_chat_id, outlet)

    message = (
        "New Refer-a-Friend Received ✨\n\n"
        f"Preferred Studio: {outlet}\n"
        f"Friend Name: {friend_name or 'Not provided'}\n"
        f"Friend Contact: {friend_contact or 'Not provided'}\n\n"
        f"Referrer Telegram Chat ID: {customer_chat_id}\n\n"
        "This customer is now connected to live Customer Service chat.\n\n"
        f"{support_reply_instructions(customer_chat_id)}"
    )

    try:
        send_telegram_message(target_chat_id, message)
        return True
    except Exception as e:
        print("REFER FRIEND SEND ERROR:", str(e), flush=True)
        traceback.print_exc()
        return False


# =========================
# LIVE SCHEDULE FROM JSON
# =========================

DEFAULT_SCHEDULE = {
    "updated": "2026-05-04",
    "studios": {
        "Alexandra": [
            {"day": "Monday", "time": "7:00 PM", "class": "Yoga", "trainer": "TBC", "slots": "TBC"},
            {"day": "Wednesday", "time": "6:00 PM", "class": "Pilates", "trainer": "TBC", "slots": "TBC"},
            {"day": "Saturday", "time": "10:00 AM", "class": "Trial Class", "trainer": "TBC", "slots": "TBC"},
        ],
        "Katong": [
            {"day": "Tuesday", "time": "7:30 PM", "class": "Yoga", "trainer": "TBC", "slots": "TBC"},
            {"day": "Thursday", "time": "6:30 PM", "class": "Barre", "trainer": "TBC", "slots": "TBC"},
            {"day": "Sunday", "time": "11:00 AM", "class": "Trial Class", "trainer": "TBC", "slots": "TBC"},
        ],
        "Kovan": [
            {"day": "Monday", "time": "6:30 PM", "class": "Pilates", "trainer": "TBC", "slots": "TBC"},
            {"day": "Friday", "time": "7:00 PM", "class": "Yoga", "trainer": "TBC", "slots": "TBC"},
            {"day": "Saturday", "time": "9:00 AM", "class": "Trial Class", "trainer": "TBC", "slots": "TBC"},
        ],
        "Upper Bukit Timah": [
            {"day": "Tuesday", "time": "6:00 PM", "class": "Yoga", "trainer": "TBC", "slots": "TBC"},
            {"day": "Thursday", "time": "7:30 PM", "class": "Pilates", "trainer": "TBC", "slots": "TBC"},
            {"day": "Sunday", "time": "10:30 AM", "class": "Trial Class", "trainer": "TBC", "slots": "TBC"},
        ],
        "Woodlands": [
            {"day": "Wednesday", "time": "7:00 PM", "class": "Barre", "trainer": "TBC", "slots": "TBC"},
            {"day": "Friday", "time": "6:00 PM", "class": "Yoga", "trainer": "TBC", "slots": "TBC"},
            {"day": "Saturday", "time": "11:30 AM", "class": "Trial Class", "trainer": "TBC", "slots": "TBC"},
        ],
    },
}


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


def is_schedule_request(text: str) -> bool:
    t = normalize(text)
    clean = simple_text(text)

    if not clean and not text.strip():
        return False

    non_english_schedule_words = [
        "jadual",
        "时间表",
        "時間表",
        "課表",
        "课程表",
    ]

    if any(word in t or word in text for word in non_english_schedule_words):
        return True

    keywords = [
        "schedule",
        "schdule",
        "sched",
        "timetable",
        "time table",
        "class schedule",
        "class timetable",
        "class timing",
        "class timings",
        "class time",
        "class times",
        "what class",
        "what classes",
        "classes today",
        "today class",
        "today classes",
        "today schedule",
        "tomorrow schedule",
        "available class",
        "available classes",
        "available slot",
        "available slots",
        "slot",
        "slots",
        "timing",
        "timings",
        "timeslot",
        "timeslots",
    ]

    if any(keyword in t for keyword in keywords):
        return True

    context_patterns = [
        r"\b(class|classes|lesson|lessons|yoga|pilates|barre|trial)\b.*\b(time|timing|timings|schedule|timetable|available|availability|slot|slots)\b",
        r"\b(time|timing|timings|schedule|timetable|available|availability|slot|slots)\b.*\b(class|classes|lesson|lessons|yoga|pilates|barre|trial)\b",
    ]

    if any(re.search(pattern, clean, flags=re.IGNORECASE) for pattern in context_patterns):
        return True

    fuzzy_schedule_phrases = [
        "schedule",
        "schdule",
        "schedle",
        "schedual",
        "secdule",
        "scedule",
        "scehdule",
        "shedule",
        "skedule",
        "timetable",
        "timetabel",
        "timetble",
        "time table",
        "class schedule",
        "class timing",
        "class timetable",
    ]

    if fuzzy_phrase_match(text, fuzzy_schedule_phrases, threshold=0.76):
        return True

    return False


def load_schedule_data() -> dict:
    try:
        with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict) and data.get("studios"):
            return data

    except FileNotFoundError:
        print("SCHEDULE FILE NOT FOUND. Using default schedule.", flush=True)

    except Exception as e:
        print("SCHEDULE LOAD ERROR:", str(e), flush=True)
        traceback.print_exc()

    return DEFAULT_SCHEDULE


def requested_day_from_text(text: str) -> str:
    t = normalize(text)
    now = datetime.now(ZoneInfo("Asia/Singapore"))

    if "today" in t:
        return now.strftime("%A")

    if "tomorrow" in t:
        return (now + timedelta(days=1)).strftime("%A")

    days = [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]

    for day in days:
        if day in t:
            return day.title()

    return ""


def format_one_outlet_schedule(outlet: str, classes: list, day_filter: str = "") -> str:
    address = get_studio_address(outlet)

    lines = [f"{outlet} Schedule"]

    if address:
        lines.append(f"Address: {address}")

    lines.append("")

    if day_filter:
        classes = [
            item for item in classes
            if str(item.get("day", "")).lower() == day_filter.lower()
        ]

    if not classes:
        if day_filter:
            lines.append(f"No classes are listed for {day_filter} right now.")
        else:
            lines.append("No classes are listed for this outlet right now.")

        return "\n".join(lines)

    for item in classes:
        day = item.get("day", "TBC")
        time_text = item.get("time", "TBC")
        class_name = item.get("class", "TBC")
        trainer = item.get("trainer", "TBC")
        slots = str(item.get("slots", "TBC"))

        line = f"- {day}, {time_text}: {class_name}"

        if trainer and trainer != "TBC":
            line += f" with {trainer}"

        if slots and slots != "TBC":
            line += f" ({slots} slots left)"

        lines.append(line)

    return "\n".join(lines)


def live_schedule_reply(chat_id: str, user_text: str, forced_outlet: str = "") -> str:
    schedule_data = load_schedule_data()
    studios_data = schedule_data.get("studios", {})
    updated = schedule_data.get("updated", "TBC")

    requested_outlet = forced_outlet or detect_outlet_choice(user_text)
    day_filter = requested_day_from_text(user_text)

    if requested_outlet:
        classes = studios_data.get(requested_outlet, [])

        title_day = f" for {day_filter}" if day_filter else ""

        return (
            f"Here is the latest schedule I have for {requested_outlet}{title_day}. 🙏\n"
            f"Last updated: {updated}\n\n"
            f"{format_one_outlet_schedule(requested_outlet, classes, day_filter)}"
        )

    reply_lines = [
        "Here is the latest Jal Yoga class schedule I have. 🙏",
        f"Last updated: {updated}",
        "",
    ]

    if day_filter:
        reply_lines[0] = f"Here is the latest Jal Yoga class schedule I have for {day_filter}. 🙏"

    for outlet in studio_names():
        classes = studios_data.get(outlet, [])
        reply_lines.append(format_one_outlet_schedule(outlet, classes, day_filter))
        reply_lines.append("")

    reply_lines.append("Please reply with an outlet number or outlet name if you want to see only one outlet:")
    reply_lines.append(studio_options_text())

    return "\n".join(reply_lines).strip()


# =========================
# INACTIVITY
# =========================

def mark_chat_active(chat_id: str) -> None:
    INACTIVITY_STATE[chat_id] = {
        "last_user_at": time.time(),
        "warning_sent": False,
        "closed": False,
    }


def clear_inactivity_state(chat_id: str) -> None:
    INACTIVITY_STATE.pop(chat_id, None)


def inactivity_checker_loop() -> None:
    while True:
        time.sleep(INACTIVITY_CHECK_SECONDS)

        now = time.time()

        for chat_id, state in list(INACTIVITY_STATE.items()):
            try:
                if chat_id in OPT_OUT_USERS:
                    clear_inactivity_state(chat_id)
                    continue

                idle_seconds = now - float(state.get("last_user_at", now))
                warning_sent = bool(state.get("warning_sent", False))

                if not warning_sent and idle_seconds >= INACTIVITY_WARNING_SECONDS:
                    send_telegram_message(
                        chat_id,
                        "Just checking in — do you still need help? "
                        "Reply here to continue, or type STOP to stop receiving follow-up messages.",
                    )

                    state["warning_sent"] = True

                elif warning_sent and idle_seconds >= INACTIVITY_CLOSE_SECONDS:
                    send_telegram_message(
                        chat_id,
                        "We’ll close this chat for now. "
                        "If you need help again, reply START or MENU anytime. 🙏",
                    )

                    reset_history(chat_id)
                    PENDING_HANDOFFS.pop(chat_id, None)
                    TRIAL_BOOKINGS.pop(chat_id, None)
                    clear_flow(chat_id)
                    clear_inactivity_state(chat_id)

            except Exception as e:
                print("INACTIVITY CHECK ERROR:", str(e), flush=True)
                traceback.print_exc()


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


# =========================
# MENU HANDLERS
# =========================

def handle_main_menu_choice(chat_id: str, text: str) -> str:
    choice = normalize(text)

    if choice == "1":
        set_flow(chat_id, "trial_outlet")

        return (
            "Sure — let’s schedule your trial class. 🙏\n\n"
            "Which studio would you prefer?\n\n"
            f"{studio_options_text()}"
        )

    if choice == "2":
        set_flow(chat_id, "current_member_menu")
        return current_member_menu_text()

    if choice == "3":
        set_flow(chat_id, "general_enquiry_menu")
        return general_enquiry_menu_text()

    if choice == "4":
        set_flow(chat_id, "corporate_name")
        return "Sure — may I have your full name?"

    if choice == "5":
        set_flow(chat_id, "staff_name")
        return "Staff Hub 🙏\n\nMay I have the staff name?"

    return ""


def handle_current_member_choice(chat_id: str, text: str) -> str:
    choice = normalize(text)

    if choice == "1":
        set_flow(chat_id, "member_cancel_details")

        return (
            "Class Cancellation Policy 🙏\n\n"
            "You can cancel a booked class without penalty up to 2 hours before the class starts.\n\n"
            "After that:\n"
            "- Cancellations made less than 2 hours before class are late cancellations\n"
            "- No-shows are also counted as late cancellations\n"
            "- After 3 late cancellations, booking access may be suspended for 7 calendar days\n\n"
            "If you want Customer Service to help with a specific booked class, please reply with:\n"
            "- Outlet\n"
            "- Class name\n"
            "- Date and time\n\n"
            "After you send the details, I’ll connect you to Customer Service here."
        )

    if choice == "2":
        set_flow(chat_id, "member_suspension_details")

        return (
            "Sure — I’ll connect you to Customer Service for membership suspension. 🙏\n\n"
            "Please reply with:\n"
            "- Medical Suspension or Non-Medical / Travel Suspension\n"
            "- Your preferred outlet, if any\n"
            "- Any important details"
        )

    if choice == "3":
        set_flow(chat_id, "member_booking_issue_details")

        return (
            "Sure — I’ll connect you to Customer Service for class booking help. 🙏\n\n"
            "Please tell me what issue you’re facing, for example:\n"
            "- cannot book a class\n"
            "- class is full\n"
            "- need help checking a schedule\n"
            "- app booking issue\n\n"
            "Please include the outlet if you know it."
        )

    if choice == "4":
        set_flow(chat_id, "refer_friend_name")
        return "That’s wonderful — what is your friend’s full name?"

    return current_member_menu_text()



def handle_member_service_flow(chat_id: str, text: str) -> str:
    stage = get_flow_stage(chat_id)
    outlet = detect_outlet_choice(text)

    if stage == "member_cancel_details":
        topic = "Class Cancellation"
        prompt_if_empty = "Please share the outlet, class name, date, and time for the class you want help with."
    elif stage == "member_suspension_details":
        topic = "Membership Suspension"
        prompt_if_empty = "Please share whether this is medical or non-medical/travel suspension, plus your preferred outlet if any."
    elif stage == "member_booking_issue_details":
        topic = "Class Booking Help"
        prompt_if_empty = "Please share the booking issue and outlet if you know it."
    else:
        return ""

    details = text.strip()

    if len(details) < 2:
        return prompt_if_empty

    clean_answer = (
        "I’ll pass this to our Customer Service team.\n\n"
        "Summary:\n"
        f"- Topic: {topic}\n"
        f"- Outlet: {outlet or 'Not specified'}\n"
        f"- Message: {details}"
    )

    if not outlet:
        PENDING_HANDOFFS[chat_id] = {
            "user_message": details,
            "clean_answer": clean_answer,
        }
        set_flow(chat_id, "pending_handoff_outlet")
        return ask_outlet_before_handoff_text()

    clear_flow(chat_id)
    sent = send_customer_service_handoff_to_telegram(chat_id, clean_answer, outlet)

    if sent:
        return (
            f"{clean_answer}\n\n"
            f"I’ve sent this to our {outlet} Customer Service team. 🙏\n\n"
            "You are now connected to Customer Service. You may reply here and our team will receive your message."
        )

    return f"{clean_answer}\n\nCustomer Service Telegram chat is not configured yet."


def handle_general_enquiry_choice(chat_id: str, text: str) -> str:
    choice = normalize(text)

    if choice == "1":
        clear_flow(chat_id)

        lines = ["Studio Locations 🙏", ""]

        for index, studio in enumerate(STUDIOS, start=1):
            lines.append(f"{index}. {studio['name']}")
            lines.append(studio["address"])
            lines.append("")

        lines.append("For the latest operating hours, please contact Customer Service.")

        return "\n".join(lines).strip()

    if choice == "2":
        clear_flow(chat_id)

        return knowledge_reply(
            chat_id,
            text,
            (
                "The customer selected General Enquiry > Class Types. "
                "Explain Jal Yoga class types from the knowledge file. "
                "Keep it beginner-friendly and concise."
            ),
            (
                "Jal Yoga offers beginner-friendly Yoga, Pilates, Barre, and wellness classes.\n\n"
                "Please type your question and I’ll help based on the information I have."
            ),
        )

    if choice == "3":
        clear_flow(chat_id)

        return knowledge_reply(
            chat_id,
            text,
            (
                "The customer selected General Enquiry > Current Events & Retreat. "
                "Share current Jal Yoga events and retreats from the knowledge file. "
                "If there are no confirmed events or retreats, say customers can contact Customer Service for the latest updates."
            ),
            "For the latest events and retreats, please contact Customer Service.",
        )

    return general_enquiry_menu_text()


# =========================
# EXTRA OUTLET FLOW HANDLERS
# =========================

def handle_schedule_outlet_flow(chat_id: str, text: str) -> str:
    outlet = detect_outlet_choice(text)

    if not outlet:
        return (
            "Please choose an outlet by number or name:\n\n"
            f"{studio_options_text()}"
        )

    clear_flow(chat_id)
    return live_schedule_reply(chat_id, text, forced_outlet=outlet)


def handle_contact_outlet_flow(chat_id: str, text: str) -> str:
    outlet = detect_outlet_choice(text)

    if not outlet:
        return (
            "Please choose an outlet by number or name:\n\n"
            f"{studio_options_text()}"
        )

    clear_flow(chat_id)
    return build_outlet_contact_reply(outlet)


# =========================
# FLOW HANDLERS
# =========================

def handle_trial_flow(chat_id: str, text: str) -> str:
    flow = get_flow(chat_id)
    stage = get_flow_stage(chat_id)

    if stage == "trial_outlet":
        outlet = detect_outlet_choice(text)

        if not outlet:
            return (
                "Which studio would you prefer?\n\n"
                f"{studio_options_text()}"
            )

        set_flow(chat_id, "trial_name", outlet=outlet)

        return (
            f"Got it — {outlet}. 🙏\n\n"
            "May I have your full name?"
        )

    if stage == "trial_name":
        if is_meaning_question(text):
            return (
                "I mean: please type your full name for the trial booking.\n\n"
                "For example: Ben Tan"
            )

        name = text.strip()

        if len(name) < 2:
            return "Please share your full name."

        set_flow(
            chat_id,
            "trial_goal",
            outlet=flow.get("outlet", ""),
            name=name,
        )

        return f"Thanks, {name.title()} — what’s your fitness goal for the trial?"

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

        send_trial_booking_to_outlet(chat_id, booking)

        reply = (
            "Trial Booking Summary:\n"
            f"- Outlet: {outlet}\n"
            "- Class: Trial Class\n"
            f"- Name: {name.title()}\n"
            f"- Fitness Goal: {goal}\n\n"
            f"Thank you! I've sent your details to the {outlet} team. 🙏\n\n"
            "You are now connected to Customer Service. "
            "You may reply here if you want to ask anything, and our team will receive your message."
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

        set_flow(chat_id, "refer_friend_contact", friend_name=friend_name)

        return "Thanks — what is your friend’s contact number?"

    if stage == "refer_friend_contact":
        friend_contact = text.strip()

        if len(friend_contact) < 3:
            return "Please share your friend’s contact number."

        set_flow(
            chat_id,
            "refer_friend_studio",
            friend_name=flow.get("friend_name", ""),
            friend_contact=friend_contact,
        )

        return (
            "Which studio would your friend prefer?\n\n"
            f"{studio_options_text()}"
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
            "You are now connected to Customer Service. You may reply here if you want to ask anything."
        )

        return add_customer_service_id_note(reply, chat_id)

    return ""


def handle_corporate_flow(chat_id: str, text: str) -> str:
    flow = get_flow(chat_id)
    stage = get_flow_stage(chat_id)

    # GLOBAL SCHEDULE SHORTCUT
    # This makes schedule work anytime, even if user spells it wrongly.
    # Examples: schedule, secdule, schedual, scedule, timetable, class timing.
    # It will show the timetable instead of giving the website link.
    if is_schedule_request(text):
        requested_outlet = detect_outlet_choice(text)

        if requested_outlet:
            clear_flow(chat_id)
            reply = live_schedule_reply(chat_id, text, forced_outlet=requested_outlet)
            return finish_reply(chat_id, text, reply)

        set_flow(chat_id, "schedule_outlet")
        reply = live_schedule_reply(chat_id, text)
        return finish_reply(chat_id, text, reply)

    if stage == "corporate_name":
        name = text.strip()

        if len(name) < 2:
            return "Please share your full name."

        set_flow(chat_id, "corporate_email", name=name)

        return "Thanks. What is your email address?"

    if stage == "corporate_email":
        email = text.strip()

        if "@" not in email or "." not in email:
            return "Please share a valid email address."

        set_flow(
            chat_id,
            "corporate_message",
            name=flow.get("name", ""),
            email=email,
        )

        return (
            "Thank you. Please briefly tell us what your Corporate / Partnership enquiry is about.\n\n"
            "For example:\n"
            "- corporate wellness programme\n"
            "- company yoga class\n"
            "- partnership proposal\n"
            "- event collaboration"
        )

    if stage == "corporate_message":
        name = flow.get("name", "")
        email = flow.get("email", "")
        message = text.strip()

        if len(message) < 2:
            return "Please briefly tell us what your Corporate / Partnership enquiry is about."

        clear_flow(chat_id)

        clean_answer = (
            "I’ll pass this to our Customer Service team.\n\n"
            "Corporate / Partnership Summary:\n"
            f"- Name: {name}\n"
            f"- Email: {email}\n"
            f"- Message: {message}"
        )

        sent = send_customer_service_handoff_to_telegram(
            chat_id,
            clean_answer,
            "Not specified",
        )

        if sent:
            return (
                f"{clean_answer}\n\n"
                "Thank you! I’ve sent this to our Customer Service team. "
                "They will follow up with you soon."
            )

        return (
            f"{clean_answer}\n\n"
            "Customer Service Telegram group is not configured yet."
        )

    return ""


def handle_staff_hub_flow(chat_id: str, text: str) -> str:
    flow = get_flow(chat_id)
    stage = get_flow_stage(chat_id)

    if stage == "staff_name":
        staff_name = text.strip()

        if len(staff_name) < 2:
            return "Please share the staff name."

        set_flow(chat_id, "staff_studio", staff_name=staff_name)

        return (
            "Which studio is this related to?\n\n"
            f"{studio_options_text()}"
        )

    if stage == "staff_studio":
        outlet = detect_outlet_choice(text)

        if not outlet:
            return (
                "Please choose a valid studio:\n\n"
                f"{studio_options_text()}"
            )

        set_flow(
            chat_id,
            "staff_room",
            staff_name=flow.get("staff_name", ""),
            outlet=outlet,
        )

        return "Which room is this related to?"

    if stage == "staff_room":
        room = text.strip()

        if len(room) < 1:
            return "Please share the room."

        set_flow(
            chat_id,
            "staff_member_booking_details",
            staff_name=flow.get("staff_name", ""),
            outlet=flow.get("outlet", ""),
            room=room,
        )

        return (
            "Please share the member and booking details.\n\n"
            "For example:\n"
            "- Member name\n"
            "- Booking date and time\n"
            "- Class name\n"
            "- Issue or request"
        )

    if stage == "staff_member_booking_details":
        staff_name = flow.get("staff_name", "")
        outlet = flow.get("outlet", "")
        room = flow.get("room", "")
        details = text.strip()

        if len(details) < 2:
            return "Please share the member and booking details."

        clear_flow(chat_id)

        clean_answer = (
            "I’ll pass this to our Customer Service team.\n\n"
            "Staff Hub Summary:\n"
            f"- Staff Name: {staff_name}\n"
            f"- Studio: {outlet}\n"
            f"- Room: {room}\n"
            f"- Member and Booking Details: {details}"
        )

        sent = send_customer_service_handoff_to_telegram(
            chat_id,
            clean_answer,
            outlet,
        )

        if sent:
            return (
                f"{clean_answer}\n\n"
                f"Thank you! I’ve sent this to the {outlet} team."
            )

        return (
            f"{clean_answer}\n\n"
            "Customer Service Telegram group is not configured yet."
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

    sent = send_customer_service_handoff_to_telegram(chat_id, clean_answer, outlet)

    if sent:
        if outlet != "Not specified":
            return (
                f"{clean_answer}\n\n"
                f"I’ve sent this summary to our {outlet} Customer Service team on Telegram."
            )

        return (
            f"{clean_answer}\n\n"
            "I’ve sent this summary to our Customer Service team on Telegram."
        )

    return (
        f"{clean_answer}\n\n"
        "Customer Service Telegram group is not configured yet."
    )


# =========================
# FINAL TRANSLATION LAYER
# =========================

def translate_reply_if_needed(chat_id: str, user_text: str, reply: str) -> str:
    language = USER_LANGUAGE.get(chat_id, "English")

    if language.lower() in {"english", "unknown"}:
        return reply

    if not client:
        return reply

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=(
                f"Translate the assistant reply into {language}. "
                "Preserve outlet names, menu numbers, phone numbers, Telegram IDs, URLs, emojis, and formatting. "
                "Do not add new information."
            ),
            input=reply,
        )

        translated = (response.output_text or "").strip()

        if translated:
            return translated

    except Exception as e:
        print("TRANSLATION ERROR:", str(e), flush=True)

    return reply


# =========================
# PROCESS MESSAGE
# =========================

def process_message(chat_id: str, user_text: str) -> str:
    text = user_text.strip()
    norm = normalize(text)

    if not text:
        return "Please type your message, or type MENU to see the options."

    if is_opt_out_request(text):
        OPT_OUT_USERS.add(chat_id)
        save_opt_out_users()
        reset_history(chat_id)
        PENDING_HANDOFFS.pop(chat_id, None)
        TRIAL_BOOKINGS.pop(chat_id, None)
        clear_flow(chat_id)
        LIVE_SUPPORT_CHATS.pop(chat_id, None)
        clear_inactivity_state(chat_id)

        return (
            "Noted — you have been unsubscribed and will not receive follow-up messages.\n"
            "If you need help later, reply START."
        )

    if is_opt_in_request(text) and chat_id in OPT_OUT_USERS:
        OPT_OUT_USERS.discard(chat_id)
        save_opt_out_users()
        reset_history(chat_id)
        PENDING_HANDOFFS.pop(chat_id, None)
        LIVE_SUPPORT_CHATS.pop(chat_id, None)
        set_flow(chat_id, "main_menu")
        mark_chat_active(chat_id)

        return finish_reply(chat_id, text, main_menu_text())

    if chat_id in OPT_OUT_USERS:
        return "You have opted out. Reply START if you want to chat with Jal Yoga again."

        mark_chat_active(chat_id)

    customer_service_command_reply = handle_customer_service_command(chat_id, text)

    if customer_service_command_reply:
        return finish_reply(chat_id, text, customer_service_command_reply, add_menu=False)

    language_switch = detect_language_switch_request(text)

    if language_switch:
        USER_LANGUAGE[chat_id] = language_switch

        last_assistant_reply = ""

        for item in reversed(CHAT_HISTORY.get(chat_id, [])):
            if item.get("role") == "assistant":
                last_assistant_reply = item.get("content", "")
                break

        if last_assistant_reply:
            reply = (
                f"Okay, I’ll reply in {language_switch} from now on. 🙏\n\n"
                "Here is my previous reply translated:\n\n"
                f"{last_assistant_reply}"
            )
        else:
            reply = (
                f"Okay, I’ll reply in {language_switch} from now on. 🙏\n\n"
                f"{repeat_current_flow_question(chat_id)}"
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

    # LIVE CUSTOMER SERVICE CHAT HAS PRIORITY
    # If the customer is already connected to Customer Service, normal messages
    # should go to Customer Service instead of restarting the bot.
    # Only MENU / START / RESTART exits live chat and returns to the main menu.
    if chat_id in LIVE_SUPPORT_CHATS:
        if norm in {"menu", "main menu", "restart", "start", "/start", "home"}:
            LIVE_SUPPORT_CHATS.pop(chat_id, None)
            reset_history(chat_id)
            PENDING_HANDOFFS.pop(chat_id, None)
            clear_flow(chat_id)
            set_flow(chat_id, "main_menu")

            return finish_reply(chat_id, text, main_menu_text())

        if norm in {"end chat", "close chat", "stop live chat", "exit live chat"}:
            LIVE_SUPPORT_CHATS.pop(chat_id, None)

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
        reset_history(chat_id)
        PENDING_HANDOFFS.pop(chat_id, None)
        LIVE_SUPPORT_CHATS.pop(chat_id, None)
        clear_flow(chat_id)
        set_flow(chat_id, "main_menu")

        return finish_reply(chat_id, text, main_menu_text())

    # CUSTOMER SERVICE SHORTCUT
    # User can ask for customer service anytime, in any supported language,
    # even while they are inside another flow like trial booking, schedule, or member help.
    if is_customer_service_request(text):
        reply = start_customer_service_flow(chat_id, text)
        return finish_reply(chat_id, text, reply)
    
    stage = get_flow_stage(chat_id)

    if stage.startswith("member_"):
        reply = handle_member_service_flow(chat_id, text)

        if reply:
            return finish_reply(chat_id, text, reply)

    if stage == "schedule_outlet":
        reply = handle_schedule_outlet_flow(chat_id, text)
        return finish_reply(chat_id, text, reply)

    if stage == "contact_outlet":
        reply = handle_contact_outlet_flow(chat_id, text)
        return finish_reply(chat_id, text, reply)

    if stage == "pending_handoff_outlet":
        reply = handle_pending_handoff_outlet(chat_id, text)
        return finish_reply(chat_id, text, reply)

    if stage == "main_menu":
        reply = handle_main_menu_choice(chat_id, text)

        if reply:
            return finish_reply(chat_id, text, reply)

    if stage == "current_member_menu":
        reply = handle_current_member_choice(chat_id, text)
        return finish_reply(chat_id, text, reply)

    if stage == "general_enquiry_menu":
        reply = handle_general_enquiry_choice(chat_id, text)
        return finish_reply(chat_id, text, reply)

    if stage.startswith("trial_"):
        reply = handle_trial_flow(chat_id, text)

        if reply:
            return finish_reply(chat_id, text, reply)

    if stage.startswith("refer_friend_"):
        reply = handle_refer_friend_flow(chat_id, text)

        if reply:
            return finish_reply(chat_id, text, reply)

    if stage.startswith("corporate_"):
        reply = handle_corporate_flow(chat_id, text)

        if reply:
            return finish_reply(chat_id, text, reply)

    if stage.startswith("staff_"):
        reply = handle_staff_hub_flow(chat_id, text)

        if reply:
            return finish_reply(chat_id, text, reply)

    if is_schedule_request(text):
        requested_outlet = detect_outlet_choice(text)

        if requested_outlet:
            reply = live_schedule_reply(chat_id, text, forced_outlet=requested_outlet)
            return finish_reply(chat_id, text, reply)

        set_flow(chat_id, "schedule_outlet")
        reply = live_schedule_reply(chat_id, text)
        return finish_reply(chat_id, text, reply)

    if norm in {"1", "2", "3", "4", "5"}:
        set_flow(chat_id, "main_menu")
        reply = handle_main_menu_choice(chat_id, text)

        if reply:
            return finish_reply(chat_id, text, reply)

    if is_class_cancellation_request(text):
        reply = (
            "Class Cancellation Policy 🙏\n\n"
            "You can cancel a booked class without penalty up to 2 hours before the class starts.\n\n"
            "After that:\n"
            "- Cancellations made less than 2 hours before class are late cancellations\n"
            "- No-shows are also counted as late cancellations\n"
            "- After 3 late cancellations, booking access may be suspended for 7 calendar days\n\n"
            "To cancel a specific booked class, please reply with:\n"
            "- Outlet\n"
            "- Class name\n"
            "- Date and time"
        )

        return finish_reply(chat_id, text, reply)

    if any(word in norm for word in ["trial", "free trial", "triel", "trail lesson", "trial lesson"]):
        set_flow(chat_id, "trial_outlet")

        reply = (
            "Sure — let’s schedule your trial class. 🙏\n\n"
            "Which studio would you prefer?\n\n"
            f"{studio_options_text()}"
        )

        return finish_reply(chat_id, text, reply)

    if "refer" in norm and "friend" in norm:
        set_flow(chat_id, "refer_friend_name")

        reply = "That’s wonderful — what is your friend’s full name?"

        return finish_reply(chat_id, text, reply)

    if "corporate" in norm or "partnership" in norm or "partnerships" in norm:
        set_flow(chat_id, "corporate_name")

        reply = "Sure — may I have your full name?"

        return finish_reply(chat_id, text, reply)

    if "staff hub" in norm:
        set_flow(chat_id, "staff_name")

        reply = "Staff Hub 🙏\n\nMay I have the staff name?"

        return finish_reply(chat_id, text, reply)

    if is_outlet_contact_request(text):
        outlet = detect_outlet_choice(text)

        if outlet:
            reply = build_outlet_contact_reply(outlet)
            return finish_reply(chat_id, text, reply)

        set_flow(chat_id, "contact_outlet")

        reply = (
            "Which outlet contact would you like?\n\n"
            f"{studio_options_text()}"
        )

        return finish_reply(chat_id, text, reply)

    answer = ask_llm(chat_id, text)

    if "[HANDOFF]" in answer:
        clean_answer = strip_handoff_token(answer).strip()
        outlet = detect_outlet_choice(text + "\n" + clean_answer)

        if not outlet:
            PENDING_HANDOFFS[chat_id] = {
                "user_message": text,
                "clean_answer": clean_answer,
            }
            set_flow(chat_id, "pending_handoff_outlet")

            return finish_reply(chat_id, text, ask_outlet_before_handoff_text())

        sent = send_customer_service_handoff_to_telegram(chat_id, clean_answer, outlet)

        if sent:
            reply = (
                f"{clean_answer}\n\n"
                f"I’ve sent this summary to our {outlet} Customer Service team on Telegram."
            )
        else:
            reply = (
                f"{clean_answer}\n\n"
                "Customer Service Telegram group is not configured yet."
            )

        return finish_reply(chat_id, text, reply)

    final_reply = add_customer_service_id_note(strip_handoff_token(answer), chat_id)

    return finish_reply(chat_id, text, final_reply)


# =========================
# FLASK ROUTES
# =========================

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
            "schedule_file": SCHEDULE_FILE,
        }
    )


@app.route("/debug/outlets", methods=["GET"])
def debug_outlets():
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


@app.route("/debug/schedule", methods=["GET"])
def debug_schedule():
    return jsonify(load_schedule_data())


@app.route("/debug/trial-bookings", methods=["GET"])
def debug_trial_bookings():
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

    if chat_type in {"group", "supergroup", "channel"}:
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
    reply = process_message(chat_id, user_text)
    return translate_reply_if_needed(chat_id, user_text, reply)


start_inactivity_checker()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    )