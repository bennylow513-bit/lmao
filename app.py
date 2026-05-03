import json
import os
import re
import threading
import time
import traceback
from datetime import datetime
from difflib import SequenceMatcher
from typing import Dict, List
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
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
    return (
        str(number)
        .replace("+", "")
        .replace(" ", "")
        .replace("-", "")
        .strip()
    )


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
            studios.append(
                {
                    "name": name,
                    "address": address,
                }
            )

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
    options = [f"- {name}" for name in studio_names()]

    if include_not_specified:
        options.append("- Not specified")

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


def get_studio_address(outlet_name: str) -> str:
    for studio in STUDIOS:
        if studio["name"].lower() == outlet_name.lower():
            return studio["address"]

    return ""


# =========================
# ENV KEYS
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


def customer_service_link() -> str:
    number = clean_number(CUSTOMER_SERVICE_WHATSAPP_NUMBER)

    if not number or number.upper() == "TBC":
        return ""

    return f"https://wa.me/{number}"


def build_outlet_contact_reply(outlet: str) -> str:
    number = outlet_whatsapp_number(outlet)

    if not number or clean_number(number).upper() == "TBC":
        number = CUSTOMER_SERVICE_WHATSAPP_NUMBER

    clean = clean_number(number)

    if not clean or clean.upper() == "TBC":
        return ""

    return (
        f"{outlet} outlet contact:\n"
        f"+{clean}\n"
        f"https://wa.me/{clean}\n\n"
        f"Address:\n"
        f"{get_studio_address(outlet)}"
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
            "If you need any further assistance, please quote this Customer Service ID "
            "so our team can find your request quickly:\n"
            f"{chat_id}"
        )

    return reply


# =========================
# LANGUAGE
# =========================

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
# KNOWLEDGE REPLY
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
- Do not copy the English menu wording just because the knowledge file or fallback text is written in English.
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


def main_menu_text(chat_id: str, user_text: str) -> str:
    fallback = (
        "Namaste! Thank you for reaching out to Jal Yoga. 🙏\n\n"
        "To help us handle your request as quickly as possible, please let us know what you're looking for today:\n\n"
        "1. Schedule a Trial\n"
        "2. I’m a current member\n"
        "3. I’d like to find out more about Jal Yoga\n"
        "4. Corporate/Partnerships\n"
        "5. Staff Hub\n\n"
        "You can also type CUSTOMER SERVICE anytime to speak to our team.\n"
        "Reply STOP anytime to stop receiving follow-up messages."
    )

    return knowledge_reply(
        chat_id,
        user_text,
        (
            "Show the MAIN MENU from the knowledge file. "
            "Keep the exact menu number structure. "
            "The menu must include Schedule a Trial, current member, find out more about Jal Yoga, "
            "Corporate/Partnerships, and Staff Hub."
        ),
        fallback,
    )


def current_member_menu_text(chat_id: str, user_text: str) -> str:
    fallback = (
        "Welcome back! Hope your practice is going well. 🙏\n\n"
        "How can I help you with your membership today?\n\n"
        "1. Class Cancellation\n"
        "2. Membership Suspension\n"
        "3. I need help with my class booking\n"
        "4. I would like to refer a friend"
    )

    return knowledge_reply(
        chat_id,
        user_text,
        (
            "Show the CURRENT MEMBER menu from the knowledge file. "
            "Keep the exact menu number structure. "
            "The menu must include Class Cancellation, Membership Suspension, Class Booking Help, and Refer a Friend."
        ),
        fallback,
    )


def general_enquiry_menu_text(chat_id: str, user_text: str) -> str:
    fallback = (
        "General Enquiry 🙏\n\n"
        "What would you like to know more about?\n\n"
        "1. Studio Locations & Operating Hours\n"
        "2. Class Types\n"
        "3. Current Events & Retreat"
    )

    return knowledge_reply(
        chat_id,
        user_text,
        (
            "Show the GENERAL ENQUIRY menu from the knowledge file. "
            "Keep the exact menu number structure. "
            "The menu must include Studio Locations & Operating Hours, Class Types, and Current Events & Retreat."
        ),
        fallback,
    )


def ask_outlet_before_handoff_text(chat_id: str, user_text: str) -> str:
    fallback = (
        "Before I pass this to our Customer Service team, do you have a specific outlet for this enquiry?\n\n"
        "Please reply with one of these:\n"
        f"{studio_options_text(include_not_specified=True)}"
    )

    return knowledge_reply(
        chat_id,
        user_text,
        (
            "Ask the customer whether this Customer Service enquiry is about a specific outlet. "
            "Show all studio options from the knowledge file and include Not specified."
        ),
        fallback,
    )


# =========================
# LLM GENERAL ANSWER
# =========================

def ask_llm(chat_id: str, user_text: str) -> str:
    language = detect_user_language(chat_id, user_text)

    if not client:
        return (
            "I’m sorry — the AI answer service is not configured yet.\n"
            "Please type CUSTOMER SERVICE and our team will follow up."
        )

    history_text = "\n".join(
        f"{item['role'].upper()}: {item['content']}"
        for item in CHAT_HISTORY.get(chat_id, [])
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

    except Exception as e:
        print("OPENAI ERROR:", str(e), flush=True)
        traceback.print_exc()

        answer = "I’m sorry — something went wrong while checking the information.\n[HANDOFF]"

    if not answer:
        answer = "I’m sorry — I’m not fully sure based on the information I have.\n[HANDOFF]"

    add_history(chat_id, "user", user_text)
    add_history(chat_id, "assistant", strip_handoff_token(answer))

    return answer


# =========================
# ROUTER
# =========================

def parse_json_reply(text: str) -> Dict:
    try:
        return json.loads(text)

    except Exception:
        pass

    try:
        start = text.find("{")
        end = text.rfind("}") + 1

        if start >= 0 and end > start:
            return json.loads(text[start:end])

    except Exception:
        pass

    return {}


def route_message_with_llm(chat_id: str, user_text: str, mode: str = "normal") -> Dict:
    outlet_guess = detect_outlet_from_text(user_text)

    default_result = {
        "intent": "normal",
        "outlet": outlet_guess,
        "no_specific_outlet": False,
        "confidence": "low",
    }

    if not client:
        return default_result

    history_text = "\n".join(
        f"{item['role'].upper()}: {item['content']}"
        for item in CHAT_HISTORY.get(chat_id, [])[-6:]
    )

    instructions = f"""
You are a routing helper for a Jal Yoga Telegram bot.

Return JSON only. No markdown. No explanation.

Allowed outlets:
{studio_options_text(include_not_specified=True)}

Mode:
{mode}

Decide:
1. intent:
   - "outlet_contact" if user asks for outlet phone, WhatsApp, contact, number, call, hotline, or how to contact an outlet.
   - "handoff_outlet_answer" if mode is "handoff_outlet_answer" and user is answering which outlet the issue is about.
   - "normal" for everything else.

2. outlet:
   - Must be one of the allowed outlet names.
   - Use "Not specified" only if user clearly says no specific outlet, any outlet, not sure, do not know, idk, or it does not matter.
   - Use "" if unclear.

3. no_specific_outlet:
   - true only when user clearly says there is no specific outlet.
   - false otherwise.

4. confidence:
   - "high", "medium", or "low".
"""

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=instructions,
            input=f"Recent chat:\n{history_text}\n\nUser message:\n{user_text}",
        )

        data = parse_json_reply(response.output_text or "")

    except Exception as e:
        print("ROUTER ERROR:", str(e), flush=True)
        return default_result

    if not isinstance(data, dict):
        return default_result

    intent = data.get("intent", "normal")
    outlet = data.get("outlet", "")

    if outlet not in studio_names() + ["Not specified", ""]:
        outlet = outlet_guess

    if intent not in {"outlet_contact", "handoff_outlet_answer", "normal"}:
        intent = "normal"

    return {
        "intent": intent,
        "outlet": outlet,
        "no_specific_outlet": bool(data.get("no_specific_outlet", False)),
        "confidence": data.get("confidence", "low"),
    }


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
# CUSTOMER SERVICE HANDOFF
# =========================

def replace_summary_outlet(summary_text: str, outlet: str) -> str:
    lines = []
    replaced = False

    for line in summary_text.splitlines():
        if line.strip().lower().startswith("- outlet:"):
            lines.append(f"- Outlet: {outlet}")
            replaced = True

        else:
            lines.append(line)

    if not replaced:
        lines.append(f"- Outlet: {outlet}")

    return "\n".join(lines)


def send_customer_service_handoff_to_telegram(customer_chat_id: str, clean_answer: str, outlet: str) -> bool:
    target_chat_id = ""

    if outlet and outlet != "Not specified":
        target_chat_id = outlet_telegram_chat_id(outlet)

    if not target_chat_id:
        target_chat_id = CUSTOMER_SERVICE_TELEGRAM_CHAT_ID

    if not target_chat_id:
        print(
            f"CUSTOMER SERVICE HANDOFF SKIPPED: No Telegram chat ID for outlet={outlet}",
            flush=True,
        )
        return False

    message = (
        "New Customer Service Handoff 🙏\n\n"
        f"{clean_answer}\n\n"
        f"Customer Telegram Chat ID: {customer_chat_id}"
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

def parse_trial_booking_summary(reply: str) -> Dict[str, str]:
    booking = {
        "outlet": "",
        "name": "",
        "fitness_goal": "",
    }

    if "Trial Booking Summary:" not in reply:
        return booking

    for line in reply.splitlines():
        clean = line.strip()

        if clean.lower().startswith("- outlet:"):
            booking["outlet"] = clean.split(":", 1)[1].strip()

        elif clean.lower().startswith("- name:"):
            booking["name"] = clean.split(":", 1)[1].strip()

        elif clean.lower().startswith("- fitness goal:"):
            booking["fitness_goal"] = clean.split(":", 1)[1].strip()

    return booking


def send_trial_booking_to_outlet(customer_chat_id: str, reply: str) -> None:
    if "Trial Booking Summary:" not in reply:
        return

    booking = parse_trial_booking_summary(reply)
    outlet = booking.get("outlet", "") or detect_outlet_from_text(reply)

    if not outlet:
        print("TRIAL BOOKING SEND SKIPPED: No outlet detected", flush=True)
        return

    target_chat_id = outlet_telegram_chat_id(outlet)

    if not target_chat_id:
        print(
            f"TRIAL BOOKING SEND SKIPPED: Missing Telegram chat ID for outlet={outlet}",
            flush=True,
        )
        return

    message = (
        "New Trial Booking Received 🙏\n\n"
        f"Outlet: {outlet}\n"
        "Class: Trial Class\n"
        f"Name: {booking.get('name') or 'Not provided'}\n"
        f"Fitness Goal: {booking.get('fitness_goal') or 'Not provided'}\n\n"
        f"Customer Telegram Chat ID: {customer_chat_id}\n\n"
        "If you require further assistance, please use this ID when contacting Customer Service."
    )

    try:
        send_telegram_message(target_chat_id, message)

        TRIAL_BOOKINGS[customer_chat_id] = {
            "outlet": outlet,
            "name": booking.get("name", ""),
            "fitness_goal": booking.get("fitness_goal", ""),
        }

    except Exception as e:
        print("TRIAL BOOKING SEND ERROR:", str(e), flush=True)
        traceback.print_exc()


def send_trial_booking_update_to_outlet(customer_chat_id: str, booking: Dict[str, str], old_outlet: str = "") -> bool:
    outlet = booking.get("outlet", "")

    if not outlet:
        return False

    target_chat_id = outlet_telegram_chat_id(outlet)

    if not target_chat_id:
        print(
            f"TRIAL BOOKING UPDATE SKIPPED: Missing Telegram chat ID for outlet={outlet}",
            flush=True,
        )
        return False

    message = (
        "Updated Trial Booking Received 🔄\n\n"
        f"Outlet: {outlet}\n"
        f"Previous Outlet: {old_outlet or 'Not specified'}\n"
        "Class: Trial Class\n"
        f"Name: {booking.get('name') or 'Not provided'}\n"
        f"Fitness Goal: {booking.get('fitness_goal') or 'Not provided'}\n\n"
        f"Customer Telegram Chat ID: {customer_chat_id}\n\n"
        "If you require further assistance, please use this ID when contacting Customer Service."
    )

    try:
        send_telegram_message(target_chat_id, message)

        if old_outlet and old_outlet != outlet:
            old_chat_id = outlet_telegram_chat_id(old_outlet)

            if old_chat_id:
                old_message = (
                    "Trial Booking Location Changed ⚠️\n\n"
                    f"Customer has changed outlet from {old_outlet} to {outlet}.\n\n"
                    "Please do not follow up on the old outlet booking.\n\n"
                    f"Name: {booking.get('name') or 'Not provided'}\n"
                    f"Fitness Goal: {booking.get('fitness_goal') or 'Not provided'}\n"
                    f"Customer Telegram Chat ID: {customer_chat_id}"
                )

                send_telegram_message(old_chat_id, old_message)

        return True

    except Exception as e:
        print("TRIAL BOOKING UPDATE SEND ERROR:", str(e), flush=True)
        traceback.print_exc()
        return False


# =========================
# REFER A FRIEND
# =========================

def parse_refer_friend_summary(reply: str) -> Dict[str, str]:
    referral = {
        "friend_name": "",
        "friend_contact": "",
        "preferred_studio": "",
    }

    if "Refer-a-Friend Summary:" not in reply:
        return referral

    for line in reply.splitlines():
        clean = line.strip()

        if clean.lower().startswith("- friend name:"):
            referral["friend_name"] = clean.split(":", 1)[1].strip()

        elif clean.lower().startswith("- friend contact:"):
            referral["friend_contact"] = clean.split(":", 1)[1].strip()

        elif clean.lower().startswith("- preferred studio:"):
            referral["preferred_studio"] = clean.split(":", 1)[1].strip()

    return referral


def send_refer_friend_to_outlet(customer_chat_id: str, reply: str) -> None:
    if "Refer-a-Friend Summary:" not in reply:
        return

    referral = parse_refer_friend_summary(reply)
    outlet = referral.get("preferred_studio", "") or detect_outlet_from_text(reply)

    if not outlet:
        print("REFER FRIEND SEND SKIPPED: No outlet detected", flush=True)
        return

    target_chat_id = outlet_telegram_chat_id(outlet)

    if not target_chat_id:
        print(
            f"REFER FRIEND SEND SKIPPED: Missing Telegram chat ID for outlet={outlet}",
            flush=True,
        )
        return

    message = (
        "New Refer-a-Friend Received ✨\n\n"
        f"Preferred Studio: {outlet}\n"
        f"Friend Name: {referral.get('friend_name') or 'Not provided'}\n"
        f"Friend Contact: {referral.get('friend_contact') or 'Not provided'}\n\n"
        f"Referrer Telegram Chat ID: {customer_chat_id}\n\n"
        "If you require further assistance, please use this ID when contacting Customer Service."
    )

    try:
        send_telegram_message(target_chat_id, message)

    except Exception as e:
        print("REFER FRIEND SEND ERROR:", str(e), flush=True)
        traceback.print_exc()


# =========================
# TRIAL UPDATE EXTRACT
# =========================

def extract_updated_name(text: str) -> str:
    patterns = [
        r"(?:change|chnage|update|switch)\s+my\s+name\s+(?:to|into)\s+(.+?)(?:\s+and\s+|$)",
        r"(?:change|chnage|update|switch)\s+name\s+(?:to|into)\s+(.+?)(?:\s+and\s+|$)",
        r"(?:name)\s+(?:to|into)\s+(.+?)(?:\s+and\s+|$)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)

        if match:
            name = match.group(1).strip(" .,!?:;")
            return " ".join(word.capitalize() for word in name.split())

    return ""


def extract_updated_fitness_goal(text: str) -> str:
    patterns = [
        r"(?:change|chnage|update|switch)\s+(?:my\s+)?(?:fitness\s+goal|goal)\s+(?:to|into)\s+(.+?)(?:\s+and\s+|$)",
        r"(?:fitness\s+goal|goal)\s+(?:to|into)\s+(.+?)(?:\s+and\s+|$)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)

        if match:
            return match.group(1).strip(" .,!?:;")

    return ""



def extract_name_from_update_details(text: str, outlet: str) -> str:
    """
    Accepts any name when the user replies with update details, for example:
    - Kelvin, Bukit Timah
    - Ben Low, Kovan
    - Sarah Tan, Woodlands
    - Muhammad Amir, Katong
    """

    if not text:
        return ""

    # Split by comma, slash, semicolon, pipe, or the word "and"
    parts = [
        part.strip()
        for part in re.split(r"[,;/|]+|\band\b", text, flags=re.IGNORECASE)
        if part.strip()
    ]

    for part in parts:
        # Skip the part that is clearly an outlet
        if detect_outlet_from_text(part):
            continue

        cleaned = re.sub(
            r"\b(change|update|switch|my|the|name|outlet|studio|location|to|into)\b",
            " ",
            part,
            flags=re.IGNORECASE,
        )

        cleaned = re.sub(r"[^A-Za-z\s]", " ", cleaned)
        cleaned = " ".join(cleaned.split()).strip()

        if len(cleaned) >= 2:
            return " ".join(word.capitalize() for word in cleaned.split())

    # Fallback: remove outlet words from the full sentence
    cleaned = text

    if outlet:
        for alias in studio_aliases(outlet):
            cleaned = re.sub(
                rf"\b{re.escape(alias)}\b",
                " ",
                cleaned,
                flags=re.IGNORECASE,
            )

    cleaned = re.sub(
        r"\b(change|update|switch|my|the|name|outlet|studio|location|to|into|and)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )

    cleaned = re.sub(r"[^A-Za-z\s]", " ", cleaned)
    cleaned = " ".join(cleaned.split()).strip()

    if len(cleaned) >= 2:
        return " ".join(word.capitalize() for word in cleaned.split())

    return ""

def is_trial_update_request(text: str) -> bool:
    t = normalize(text)

    phrases = [
        "change my trial",
        "update my trial",
        "change trial",
        "update trial",
        "change booking",
        "update booking",
        "change my booking",
        "update my booking",
        "change outlet",
        "change location",
        "change studio",
        "switch outlet",
        "switch location",
        "switch studio",
        "change to",
        "move to",
        "change my name",
        "chnage my name",
        "update my name",
        "change name",
        "update name",
        "change my goal",
        "update my goal",
        "change goal",
        "update goal",
        "change my fitness goal",
        "update my fitness goal",
        "change fitness goal",
        "update fitness goal",
        "the name and the outlet",
        "name and outlet",
        "outlet and name",
        "change name and outlet",
        "change outlet and name",
        "update name and outlet",
        "update outlet and name",
        "change everything",
        "update everything",
    ]

    return any(phrase in t for phrase in phrases)


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

        return knowledge_reply(
            chat_id,
            text,
            (
                "The customer selected Schedule a Trial. "
                "Ask which studio they prefer. "
                "Show the studio options from the knowledge file."
            ),
            (
                "Sure — let’s schedule your trial class. 🙏\n\n"
                "Which studio would you prefer?\n\n"
                f"{studio_options_text()}"
            ),
        )

    if choice == "2":
        set_flow(chat_id, "current_member_menu")
        return current_member_menu_text(chat_id, text)

    if choice == "3":
        set_flow(chat_id, "general_enquiry_menu")
        return general_enquiry_menu_text(chat_id, text)

    if choice == "4":
        clean_answer = (
            "I’ll pass this to our Customer Service team.\n\n"
            "Summary:\n"
            "- Topic: Corporate / Partnership enquiry\n"
            "- Outlet: Not specified\n"
            "- Message: Customer selected Corporate / Partnerships"
        )

        PENDING_HANDOFFS[chat_id] = {
            "user_message": text,
            "clean_answer": clean_answer,
        }

        return ask_outlet_before_handoff_text(chat_id, text)

    if choice == "5":
        clean_answer = (
            "I’ll pass this to our Customer Service team.\n\n"
            "Summary:\n"
            "- Topic: Staff Hub enquiry\n"
            "- Outlet: Not specified\n"
            "- Message: Customer selected Staff Hub"
        )

        PENDING_HANDOFFS[chat_id] = {
            "user_message": text,
            "clean_answer": clean_answer,
        }

        return ask_outlet_before_handoff_text(chat_id, text)

    return ""


def handle_current_member_choice(chat_id: str, text: str) -> str:
    choice = normalize(text)

    if choice == "1":
        clear_flow(chat_id)

        return knowledge_reply(
            chat_id,
            text,
            (
                "The customer selected Current Member > Class Cancellation. "
                "Explain the Jal Yoga class cancellation policy from the knowledge file."
            ),
            (
                "You can cancel a booked class without penalty up to 2 hours before the class starts.\n\n"
                "After that:\n"
                "- Cancellations made less than 2 hours before class are late cancellations\n"
                "- No-shows are also counted as late cancellations\n"
                "- After 3 late cancellations, booking access may be suspended for 7 calendar days"
            ),
        )

    if choice == "2":
        clear_flow(chat_id)

        return knowledge_reply(
            chat_id,
            text,
            (
                "The customer selected Current Member > Membership Suspension. "
                "Ask whether this is for Medical Suspension or Non-Medical / Travel Suspension."
            ),
            "Sure — is this for Medical Suspension or Non-Medical / Travel Suspension?",
        )

    if choice == "3":
        clear_flow(chat_id)

        return knowledge_reply(
            chat_id,
            text,
            (
                "The customer selected Current Member > Class Booking Help. "
                "Ask what class booking issue they are facing. "
                "Give examples such as cannot book a class, class is full, schedule issue, or app booking issue."
            ),
            (
                "Sure — I can help with class booking questions.\n\n"
                "Please tell me what issue you’re facing, for example:\n"
                "- cannot book a class\n"
                "- class is full\n"
                "- need help checking a schedule\n"
                "- app booking issue"
            ),
        )

    if choice == "4":
        set_flow(chat_id, "refer_friend_name")

        return knowledge_reply(
            chat_id,
            text,
            "The customer selected Refer a Friend. Ask for the friend's full name.",
            "That’s wonderful — what is your friend’s full name?",
        )

    return ""


def handle_general_enquiry_choice(chat_id: str, text: str) -> str:
    choice = normalize(text)

    if choice == "1":
        clear_flow(chat_id)

        return knowledge_reply(
            chat_id,
            text,
            (
                "The customer selected General Enquiry > Studio Locations & Operating Hours. "
                "Provide Jal Yoga studio locations and operating hours from the knowledge file. "
                "Organise it clearly by outlet. "
                "If operating hours are not confirmed, say customers can contact Customer Service for the latest hours."
            ),
            (
                "Studio Locations & Operating Hours 🙏\n\n"
                f"{studio_options_text()}\n\n"
                "For the latest operating hours, please contact Customer Service."
            ),
        )

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
            "Jal Yoga offers beginner-friendly yoga, Pilates, barre, and wellness classes. Please contact Customer Service for more details.",
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

    return general_enquiry_menu_text(chat_id, text)


# =========================
# TRIAL / REFERRAL FLOW
# =========================

def handle_trial_flow(chat_id: str, text: str) -> str:
    flow = get_flow(chat_id)
    stage = get_flow_stage(chat_id)

    if stage == "trial_outlet":
        outlet = detect_outlet_from_text(text)

        if not outlet:
            return knowledge_reply(
                chat_id,
                text,
                (
                    "The customer is scheduling a trial but did not provide a valid studio. "
                    "Ask them to choose a studio from the studio list."
                ),
                f"Which studio would you prefer?\n\n{studio_options_text()}",
            )

        set_flow(chat_id, "trial_name", outlet=outlet)

        return knowledge_reply(
            chat_id,
            text,
            "The customer selected a trial studio. Ask for their full name.",
            "Got it. May I have your full name?",
        )

    if stage == "trial_name":
        name = text.strip()

        if len(name) < 2:
            return knowledge_reply(
                chat_id,
                text,
                "Ask the customer to provide their full name for the trial booking.",
                "Please share your full name.",
            )

        set_flow(
            chat_id,
            "trial_goal",
            outlet=flow.get("outlet", ""),
            name=name,
        )

        return knowledge_reply(
            chat_id,
            text,
            "The customer provided their name. Ask for their fitness goal for the trial.",
            f"Thanks, {name.title()} — what’s your fitness goal for the trial?",
        )

    if stage == "trial_goal":
        outlet = flow.get("outlet", "")
        name = flow.get("name", "")
        goal = text.strip()

        clear_flow(chat_id)

        reply = (
            "Trial Booking Summary:\n"
            f"- Outlet: {outlet}\n"
            "- Class: Trial Class\n"
            f"- Name: {name.title()}\n"
            f"- Fitness Goal: {goal}\n\n"
            f"Thank you! I've sent your details to the {outlet} team. "
            "Our Studio Manager will contact you within 24 hours to schedule your trial."
        )

        send_trial_booking_to_outlet(chat_id, reply)

        return add_customer_service_id_note(reply, chat_id)

    return ""


def handle_refer_friend_flow(chat_id: str, text: str) -> str:
    flow = get_flow(chat_id)
    stage = get_flow_stage(chat_id)

    if stage == "refer_friend_name":
        friend_name = text.strip()

        if len(friend_name) < 2:
            return knowledge_reply(
                chat_id,
                text,
                "Ask for the friend's full name.",
                "Please share your friend’s full name.",
            )

        set_flow(chat_id, "refer_friend_contact", friend_name=friend_name)

        return knowledge_reply(
            chat_id,
            text,
            "The customer provided their friend's name. Ask for the friend's contact number.",
            "Thanks — what is your friend’s contact number?",
        )

    if stage == "refer_friend_contact":
        set_flow(
            chat_id,
            "refer_friend_studio",
            friend_name=flow.get("friend_name", ""),
            friend_contact=text.strip(),
        )

        return knowledge_reply(
            chat_id,
            text,
            "The customer provided the friend's contact. Ask which studio the friend would prefer.",
            f"Which studio would your friend prefer?\n\n{studio_options_text()}",
        )

    if stage == "refer_friend_studio":
        outlet = detect_outlet_from_text(text)

        if not outlet:
            return knowledge_reply(
                chat_id,
                text,
                "The customer did not provide a valid preferred studio. Ask them to choose one studio.",
                f"Please choose one preferred studio:\n\n{studio_options_text()}",
            )

        reply = (
            "Refer-a-Friend Summary:\n"
            f"- Friend Name: {flow.get('friend_name', '')}\n"
            f"- Friend Contact: {flow.get('friend_contact', '')}\n"
            f"- Preferred Studio: {outlet}\n\n"
            "That’s amazing! We love meeting friends of our Jal Yoga community. ✨\n\n"
            "Thank you! Our team will reach out to them with a special invitation.\n\n"
            "Don’t forget to ask them to mention your name when they sign up so we can look after both of you."
        )

        clear_flow(chat_id)

        send_refer_friend_to_outlet(chat_id, reply)

        return add_customer_service_id_note(reply, chat_id)

    return ""


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
        set_flow(chat_id, "main_menu")
        mark_chat_active(chat_id)

        reply = main_menu_text(chat_id, text)
        return reply + "\n\nReply MENU to return to the main menu."

    if chat_id in OPT_OUT_USERS:
        return "You have opted out. Reply START if you want to chat with Jal Yoga again."

    mark_chat_active(chat_id)
    detect_user_language(chat_id, text)

    if contains_sensitive_keyword(text):
        return (
            "For your safety, please do not share NRIC, passport numbers, full card numbers, "
            "CVV, OTP, passwords, or bank details here.\n\n"
            "For account-specific or payment-related help, please type CUSTOMER SERVICE."
        )

    if is_reset_request(text):
        reset_history(chat_id)
        PENDING_HANDOFFS.pop(chat_id, None)
        clear_flow(chat_id)
        set_flow(chat_id, "main_menu")

        reply = main_menu_text(chat_id, text)

        add_history(chat_id, "user", text)
        add_history(chat_id, "assistant", reply)

        return reply + "\n\nReply MENU to return to the main menu."

    if "customer service" in norm or norm in {"cs", "agent", "human", "real person"}:
        clean_answer = (
            "I’ll pass this to our Customer Service team.\n\n"
            "Summary:\n"
            "- Topic: Customer Service enquiry\n"
            "- Outlet: Not specified\n"
            f"- Message: {text}"
        )

        PENDING_HANDOFFS[chat_id] = {
            "user_message": text,
            "clean_answer": clean_answer,
        }

        return ask_outlet_before_handoff_text(chat_id, text)

    stage = get_flow_stage(chat_id)

    if stage == "main_menu":
        reply = handle_main_menu_choice(chat_id, text)

        if reply:
            add_history(chat_id, "user", text)
            add_history(chat_id, "assistant", reply)

            return reply + "\n\nReply MENU to return to the main menu."

    if stage == "current_member_menu":
        reply = handle_current_member_choice(chat_id, text)

        if reply:
            add_history(chat_id, "user", text)
            add_history(chat_id, "assistant", reply)

            return reply + "\n\nReply MENU to return to the main menu."

    if stage == "general_enquiry_menu":
        reply = handle_general_enquiry_choice(chat_id, text)

        add_history(chat_id, "user", text)
        add_history(chat_id, "assistant", reply)

        return reply + "\n\nReply MENU to return to the main menu."

    if stage.startswith("trial_"):
        reply = handle_trial_flow(chat_id, text)

        if reply:
            add_history(chat_id, "user", text)
            add_history(chat_id, "assistant", reply)

            return reply + "\n\nReply MENU to return to the main menu."

    if stage.startswith("refer_friend_"):
        reply = handle_refer_friend_flow(chat_id, text)

        if reply:
            add_history(chat_id, "user", text)
            add_history(chat_id, "assistant", reply)

            return reply + "\n\nReply MENU to return to the main menu."

    new_outlet = detect_outlet_from_text(text)
    new_name = extract_updated_name(text)
    new_goal = extract_updated_fitness_goal(text)

    # If user first says "change name and outlet",
    # then replies "Ben Low, Bukit Timah",
    # this extracts "Ben Low" as the new name.
    if not new_name and get_flow_stage(chat_id) == "trial_update_details":
        new_name = extract_name_from_update_details(text, new_outlet)

    wants_trial_update = (
        chat_id in TRIAL_BOOKINGS
        and (
            is_trial_update_request(text)
            or bool(new_name)
            or bool(new_goal)
            or get_flow_stage(chat_id) == "trial_update_details"
        )
    )

    if wants_trial_update:
        old = TRIAL_BOOKINGS[chat_id]

        updated = {
            "outlet": new_outlet or old.get("outlet", ""),
            "name": new_name or old.get("name", ""),
            "fitness_goal": new_goal or old.get("fitness_goal", ""),
        }

        nothing_changed = (
            updated["outlet"] == old.get("outlet", "")
            and updated["name"] == old.get("name", "")
            and updated["fitness_goal"] == old.get("fitness_goal", "")
        )

        if nothing_changed:
            set_flow(chat_id, "trial_update_details")

            return knowledge_reply(
                chat_id,
                text,
                (
                    "The customer wants to update their trial booking but has not provided the new details clearly. "
                    "Ask them for the new name, outlet, or fitness goal. "
                    "Give examples using any customer name, such as: Sarah Tan, Bukit Timah."
                ),
                (
                    "Sure — what would you like to update for your trial booking?\n\n"
                    "You can reply like this:\n"
                    "- Sarah Tan, Bukit Timah\n"
                    "- Ben Low, Kovan\n"
                    "- change my name to Amanda Lee\n"
                    "- change to Woodlands\n"
                    "- change my fitness goal to weight loss\n"
                    "- change to Katong and change my name to Muhammad Amir"
                ),
            )

        sent = send_trial_booking_update_to_outlet(
            chat_id,
            updated,
            old_outlet=old.get("outlet", ""),
        )

        TRIAL_BOOKINGS[chat_id] = updated
        clear_flow(chat_id)

        if sent:
            reply = (
                "No problem — I’ve updated your trial booking.\n\n"
                "Updated Trial Booking Summary:\n"
                f"- Outlet: {updated.get('outlet') or 'Not provided'}\n"
                "- Class: Trial Class\n"
                f"- Name: {updated.get('name') or 'Not provided'}\n"
                f"- Fitness Goal: {updated.get('fitness_goal') or 'Not provided'}\n\n"
                f"I’ve sent the updated summary to the {updated.get('outlet')} team."
            )

            return add_customer_service_id_note(reply, chat_id) + "\n\nReply MENU to return to the main menu."

        return (
            "I’ve updated your trial booking in this chat, but I could not send it to the outlet group.\n\n"
            "Please check that the outlet Telegram chat ID is added correctly in Render.\n\n"
            "Reply MENU to return to the main menu."
        )

    if chat_id in PENDING_HANDOFFS:
        pending = PENDING_HANDOFFS.pop(chat_id)

        route = route_message_with_llm(chat_id, text, mode="handoff_outlet_answer")

        selected = route.get("outlet", "")

        if selected == "Not specified":
            selected = ""

        if not selected and not route.get("no_specific_outlet", False):
            PENDING_HANDOFFS[chat_id] = pending

            return knowledge_reply(
                chat_id,
                text,
                (
                    "The customer service handoff needs an outlet, but the user did not provide a clear outlet. "
                    "Ask which outlet this is about and show the studio list including Not specified."
                ),
                (
                    "Sorry, which outlet is this about?\n\n"
                    "Please reply with one of these:\n"
                    f"{studio_options_text(include_not_specified=True)}"
                ),
            )

        outlet = selected if selected else "Not specified"

        clean_answer = replace_summary_outlet(pending["clean_answer"], outlet)

        sent = send_customer_service_handoff_to_telegram(chat_id, clean_answer, outlet)

        team = f"{outlet} Customer Service team" if outlet != "Not specified" else "Customer Service team"

        if sent:
            return (
                f"{clean_answer}\n\n"
                f"I’ve sent this summary to our {team} on Telegram.\n\n"
                "Reply MENU to return to the main menu."
            )

        return (
            f"{clean_answer}\n\n"
            "Customer Service Telegram group is not configured yet.\n\n"
            "Reply MENU to return to the main menu."
        )

    if any(word in norm for word in ["trial", "free trial", "triel", "trail lesson", "trial lesson"]):
        set_flow(chat_id, "trial_outlet")

        reply = knowledge_reply(
            chat_id,
            text,
            (
                "The customer wants to schedule a trial class. "
                "Ask which studio they prefer and show the studio options."
            ),
            (
                "Sure — let’s schedule your trial class. 🙏\n\n"
                "Which studio would you prefer?\n\n"
                f"{studio_options_text()}"
            ),
        )

        return reply + "\n\nReply MENU to return to the main menu."

    if "refer" in norm and "friend" in norm:
        set_flow(chat_id, "refer_friend_name")

        reply = knowledge_reply(
            chat_id,
            text,
            "The customer wants to refer a friend. Ask for the friend's full name.",
            "That’s wonderful — what is your friend’s full name?",
        )

        return reply + "\n\nReply MENU to return to the main menu."

    route = route_message_with_llm(chat_id, text)

    if route.get("intent") == "outlet_contact":
        outlet = route.get("outlet", "")

        if outlet and outlet != "Not specified":
            reply = build_outlet_contact_reply(outlet)

            if reply:
                return reply + "\n\nReply MENU to return to the main menu."

        return knowledge_reply(
            chat_id,
            text,
            "Ask which outlet contact the customer wants. Show the studio options.",
            (
                "Which outlet contact would you like?\n\n"
                f"{studio_options_text()}"
            ),
        ) + "\n\nReply MENU to return to the main menu."

    answer = ask_llm(chat_id, text)

    if "[HANDOFF]" in answer:
        clean_answer = strip_handoff_token(answer).strip()
        outlet = detect_outlet_from_text(text + "\n" + clean_answer)

        if not outlet:
            PENDING_HANDOFFS[chat_id] = {
                "user_message": text,
                "clean_answer": clean_answer,
            }

            return ask_outlet_before_handoff_text(chat_id, text)

        sent = send_customer_service_handoff_to_telegram(chat_id, clean_answer, outlet)

        if sent:
            return (
                f"{clean_answer}\n\n"
                f"I’ve sent this summary to our {outlet} Customer Service team on Telegram.\n\n"
                "Reply MENU to return to the main menu."
            )

        return (
            f"{clean_answer}\n\n"
            "Customer Service Telegram group is not configured yet.\n\n"
            "Reply MENU to return to the main menu."
        )

    final_reply = strip_handoff_token(answer)

    send_trial_booking_to_outlet(chat_id, final_reply)
    send_refer_friend_to_outlet(chat_id, final_reply)

    return add_customer_service_id_note(final_reply, chat_id) + "\n\nReply MENU to return to the main menu."



# =========================
# FINAL TRANSLATION LAYER
# =========================

def translate_reply_if_needed(chat_id: str, user_text: str, reply: str) -> str:
    """
    Final safety layer to translate hardcoded/fallback replies.

    This fixes cases where the user types Chinese/Portuguese/Malay/etc,
    but the reply came from Python fallback text instead of the LLM.
    """
    language = detect_user_language(chat_id, user_text)

    if not language or language.lower() == "english":
        return reply

    if not client:
        return reply

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=(
                f"Translate this Jal Yoga bot reply into {language}. "
                "Keep the meaning exactly the same. "
                "Preserve menu numbers, outlet names, phone numbers, Telegram IDs, links, and formatting. "
                "Do not add new information. "
                "Do not remove MENU, STOP, Customer Service ID, [HANDOFF], or outlet names. "
                "If the reply is already in the correct language, return it unchanged."
            ),
            input=reply,
        )

        translated = (response.output_text or "").strip()

        if translated:
            return translated

    except Exception as e:
        print("TRANSLATION ERROR:", str(e), flush=True)
        traceback.print_exc()

    return reply


# =========================
# ROUTES
# =========================

@app.before_request
def start_background_tasks():
    start_inactivity_checker()


@app.route("/", methods=["GET"])
def home():
    telegram_link = "#"

    if TELEGRAM_BOT_USERNAME:
        telegram_link = f"https://t.me/{TELEGRAM_BOT_USERNAME.replace('@', '').strip()}"

    try:
        return render_template(
            "index.html",
            telegram_link=telegram_link,
            whatsapp_link=customer_service_link() or "#",
            studios=STUDIOS,
        )

    except Exception:
        return (
            "<h1>Jal Yoga Telegram Bot</h1>"
            "<p>Server is running.</p>"
            f'<p><a href="{telegram_link}">Open Telegram Bot</a></p>'
        )


@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "ok",
            "message": "healthy",
            "inactivity_checker_started": INACTIVITY_THREAD_STARTED,
            "active_inactivity_chats": len(INACTIVITY_STATE),
            "warning_seconds": INACTIVITY_WARNING_SECONDS,
            "close_seconds": INACTIVITY_CLOSE_SECONDS,
            "check_seconds": INACTIVITY_CHECK_SECONDS,
        }
    )


@app.route("/debug/inactivity", methods=["GET"])
def debug_inactivity():
    safe_state = {}

    for chat_id, state in INACTIVITY_STATE.items():
        safe_state[chat_id[-4:]] = {
            "seconds_since_last_user_message": int(
                time.time() - float(state.get("last_user_at", time.time()))
            ),
            "warning_sent": bool(state.get("warning_sent", False)),
            "closed": bool(state.get("closed", False)),
        }

    return jsonify(
        {
            "checker_started": INACTIVITY_THREAD_STARTED,
            "warning_seconds": INACTIVITY_WARNING_SECONDS,
            "close_seconds": INACTIVITY_CLOSE_SECONDS,
            "check_seconds": INACTIVITY_CHECK_SECONDS,
            "active_chat_count": len(INACTIVITY_STATE),
            "chats": safe_state,
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

        # Final translation layer for hardcoded and fallback replies
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
    return process_message(chat_id, user_text)


if __name__ == "__main__":
    start_inactivity_checker()

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    )