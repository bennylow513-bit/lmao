import json
import os
import traceback
from datetime import datetime
from typing import Any, Dict, List
from urllib.parse import quote
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

STAFF_TELEGRAM_CHAT_ID = os.getenv("STAFF_TELEGRAM_CHAT_ID", "")

CUSTOMER_SERVICE_WHATSAPP_NUMBER = os.getenv("CUSTOMER_SERVICE_WHATSAPP_NUMBER", "")

ALEXANDRA_WHATSAPP_NUMBER = os.getenv("ALEXANDRA_WHATSAPP_NUMBER", "")
KATONG_WHATSAPP_NUMBER = os.getenv("KATONG_WHATSAPP_NUMBER", "")
KOVAN_WHATSAPP_NUMBER = os.getenv("KOVAN_WHATSAPP_NUMBER", "")
UPPER_BUKIT_TIMAH_WHATSAPP_NUMBER = os.getenv("UPPER_BUKIT_TIMAH_WHATSAPP_NUMBER", "")
WOODLANDS_WHATSAPP_NUMBER = os.getenv("WOODLANDS_WHATSAPP_NUMBER", "")

PORT = int(os.getenv("PORT", "5000"))

client = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# MEMORY
# =========================

CHAT_HISTORY: Dict[str, List[Dict[str, str]]] = {}

OPT_OUT_FILE = os.getenv("OPT_OUT_FILE", "telegram_opt_out_users.json")


def load_opt_out_users() -> set:
    try:
        with open(OPT_OUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return set(str(item) for item in data)

    except Exception:
        pass

    return set()


OPT_OUT_USERS = load_opt_out_users()


def save_opt_out_users() -> None:
    with open(OPT_OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(OPT_OUT_USERS), f, ensure_ascii=False, indent=2)


# =========================
# KNOWLEDGE
# =========================

def load_knowledge_text() -> str:
    try:
        with open("knowledge.txt", "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


KNOWLEDGE_TEXT = load_knowledge_text()


def parse_studios(text: str) -> List[Dict[str, str]]:
    studio_names = [
        "Alexandra",
        "Katong",
        "Kovan",
        "Upper Bukit Timah",
        "Woodlands",
    ]

    studios: List[Dict[str, str]] = []

    for line in text.splitlines():
        line = line.strip()

        if not line.startswith("- "):
            continue

        clean_line = line[2:].strip()

        for studio_name in studio_names:
            prefix = studio_name + ":"

            if clean_line.lower().startswith(prefix.lower()):
                address = clean_line.split(":", 1)[1].strip()

                if not any(s["name"] == studio_name for s in studios):
                    studios.append(
                        {
                            "name": studio_name,
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


# =========================
# BASIC SAFETY
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


def normalize(text: str) -> str:
    return " ".join((text or "").strip().lower().replace("’", "'").split())


def now_singapore_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Singapore")).isoformat()


def reset_history(chat_id: str) -> None:
    CHAT_HISTORY.pop(chat_id, None)


def add_history(chat_id: str, role: str, content: str) -> None:
    if chat_id not in CHAT_HISTORY:
        CHAT_HISTORY[chat_id] = []

    CHAT_HISTORY[chat_id].append(
        {
            "role": role,
            "content": content,
        }
    )

    CHAT_HISTORY[chat_id] = CHAT_HISTORY[chat_id][-20:]


def save_request(kind: str, chat_id: str, payload: Dict[str, Any]) -> None:
    record = {
        "kind": kind,
        "chat_id": chat_id,
        "payload": payload,
        "created_at_sg": now_singapore_iso(),
    }

    with open("requests_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


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


def clean_number(number: str) -> str:
    return number.replace("+", "").replace(" ", "").strip()


def strip_handoff_token(text: str) -> str:
    return text.replace("[HANDOFF]", "").strip()


# =========================
# WHATSAPP HELPERS
# =========================

def customer_service_link() -> str:
    number = clean_number(CUSTOMER_SERVICE_WHATSAPP_NUMBER)

    if not number:
        return ""

    return f"https://wa.me/{number}"


def outlet_whatsapp_numbers() -> Dict[str, str]:
    return {
        "Alexandra": ALEXANDRA_WHATSAPP_NUMBER,
        "Katong": KATONG_WHATSAPP_NUMBER,
        "Kovan": KOVAN_WHATSAPP_NUMBER,
        "Upper Bukit Timah": UPPER_BUKIT_TIMAH_WHATSAPP_NUMBER,
        "Woodlands": WOODLANDS_WHATSAPP_NUMBER,
    }


def selected_customer_service_number(outlet: str) -> str:
    outlet = (outlet or "").strip()
    outlet_numbers = outlet_whatsapp_numbers()

    if outlet in outlet_numbers and outlet_numbers[outlet]:
        return outlet_numbers[outlet]

    return CUSTOMER_SERVICE_WHATSAPP_NUMBER


def build_prefilled_whatsapp_link(number: str, message: str) -> str:
    clean = clean_number(number)

    if not clean:
        return ""

    encoded_message = quote(message, safe="")
    return f"https://wa.me/{clean}?text={encoded_message}"


def customer_service_prefilled_link(bot_data: Dict[str, Any], user_text: str) -> str:
    outlet = bot_data.get("outlet", "Not specified")
    topic = bot_data.get("topic", "General enquiry")
    summary_message = bot_data.get("summary_message", user_text)

    selected_number = selected_customer_service_number(outlet)

    prefilled_message = (
        "Hello Jal Yoga Customer Service,\n\n"
        "I need help with this enquiry:\n\n"
        "Summary:\n"
        f"- Topic: {topic}\n"
        f"- Outlet: {outlet}\n"
        f"- Message: {summary_message}\n\n"
        "Thank you."
    )

    return build_prefilled_whatsapp_link(selected_number, prefilled_message)


def live_contact_config_text() -> str:
    return f"""
LIVE CUSTOMER SERVICE CONFIG FROM RENDER

Main Customer Service WhatsApp:
- {CUSTOMER_SERVICE_WHATSAPP_NUMBER or "TBC"}

Outlet WhatsApp Numbers:
- Alexandra: {ALEXANDRA_WHATSAPP_NUMBER or "TBC"}
- Katong: {KATONG_WHATSAPP_NUMBER or "TBC"}
- Kovan: {KOVAN_WHATSAPP_NUMBER or "TBC"}
- Upper Bukit Timah: {UPPER_BUKIT_TIMAH_WHATSAPP_NUMBER or "TBC"}
- Woodlands: {WOODLANDS_WHATSAPP_NUMBER or "TBC"}

Rules:
- You may use these numbers only if they are not TBC.
- If an outlet number is TBC, do not invent it.
- If main Customer Service is available, use the main Customer Service link for handoff.
- The app will create the WhatsApp prefilled summary link after your response.
"""


# =========================
# STAFF TELEGRAM NOTIFICATION
# =========================

def send_staff_telegram_message(message: str) -> bool:
    if not STAFF_TELEGRAM_CHAT_ID:
        print("Missing STAFF_TELEGRAM_CHAT_ID")
        return False

    if not TELEGRAM_BOT_TOKEN:
        print("Missing TELEGRAM_BOT_TOKEN")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": STAFF_TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }

    response = requests.post(url, json=payload, timeout=30)

    print("STAFF TELEGRAM SEND STATUS:", response.status_code)
    print("STAFF TELEGRAM SEND RESPONSE:", response.text)

    response.raise_for_status()
    return True


# =========================
# LLM STRUCTURED OUTPUT
# =========================

def bot_reply_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "reply_text": {
                "type": "string",
                "description": "The exact Telegram message to send to the user. Do not include the WhatsApp Customer Service link.",
            },
            "needs_handoff": {
                "type": "boolean",
                "description": "True if Customer Service handoff is needed.",
            },
            "topic": {
                "type": "string",
                "description": "Short topic for Customer Service summary.",
            },
            "outlet": {
                "type": "string",
                "enum": [
                    "Not specified",
                    "Alexandra",
                    "Katong",
                    "Kovan",
                    "Upper Bukit Timah",
                    "Woodlands",
                ],
                "description": "Detected outlet if clearly mentioned. Otherwise Not specified.",
            },
            "summary_message": {
                "type": "string",
                "description": "Short summary of the user's request for Customer Service.",
            },
            "notify_staff": {
                "type": "boolean",
                "description": "True when a completed request should be automatically sent to the staff Telegram group.",
            },
            "staff_message": {
                "type": "string",
                "description": "The exact message to send to the staff Telegram group. Empty if notify_staff is false.",
            },
        },
        "required": [
            "reply_text",
            "needs_handoff",
            "topic",
            "outlet",
            "summary_message",
            "notify_staff",
            "staff_message",
        ],
    }


def fallback_bot_data(user_text: str) -> Dict[str, Any]:
    return {
        "reply_text": (
            "I’m sorry — I’m not fully sure based on the information I have.\n\n"
            "I’ll pass this to our Customer Service team.\n\n"
            "Summary:\n"
            "- Topic: General enquiry\n"
            "- Outlet: Not specified\n"
            f"- Message: {user_text}"
        ),
        "needs_handoff": True,
        "topic": "General enquiry",
        "outlet": "Not specified",
        "summary_message": user_text,
        "notify_staff": False,
        "staff_message": "",
    }


def parse_bot_json(raw_text: str, user_text: str) -> Dict[str, Any]:
    try:
        data = json.loads(raw_text)
    except Exception:
        return fallback_bot_data(user_text)

    if not isinstance(data, dict):
        return fallback_bot_data(user_text)

    required = {
        "reply_text",
        "needs_handoff",
        "topic",
        "outlet",
        "summary_message",
        "notify_staff",
        "staff_message",
    }

    if not required.issubset(set(data.keys())):
        return fallback_bot_data(user_text)

    data["reply_text"] = str(data.get("reply_text", "")).strip()
    data["topic"] = str(data.get("topic", "General enquiry")).strip()
    data["outlet"] = str(data.get("outlet", "Not specified")).strip()
    data["summary_message"] = str(data.get("summary_message", user_text)).strip()
    data["needs_handoff"] = bool(data.get("needs_handoff", False))
    data["notify_staff"] = bool(data.get("notify_staff", False))
    data["staff_message"] = str(data.get("staff_message", "")).strip()

    valid_outlets = {
        "Not specified",
        "Alexandra",
        "Katong",
        "Kovan",
        "Upper Bukit Timah",
        "Woodlands",
    }

    if data["outlet"] not in valid_outlets:
        data["outlet"] = "Not specified"

    if not data["reply_text"]:
        return fallback_bot_data(user_text)

    if not data["notify_staff"]:
        data["staff_message"] = ""

    return data


def ensure_handoff_summary(bot_data: Dict[str, Any], user_text: str) -> Dict[str, Any]:
    reply_text = strip_handoff_token(bot_data.get("reply_text", "")).strip()

    if "Summary:" not in reply_text:
        reply_text = (
            "I’ll pass this to our Customer Service team.\n\n"
            "Summary:\n"
            f"- Topic: {bot_data.get('topic', 'General enquiry')}\n"
            f"- Outlet: {bot_data.get('outlet', 'Not specified')}\n"
            f"- Message: {bot_data.get('summary_message', user_text)}"
        )

    bot_data["reply_text"] = reply_text
    return bot_data


# =========================
# LLM BRAIN
# =========================

def ask_llm(chat_id: str, user_text: str) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        return {
            "reply_text": (
                "I’m sorry — the AI answer service is not configured yet.\n\n"
                "I’ll pass this to our Customer Service team.\n\n"
                "Summary:\n"
                "- Topic: AI configuration issue\n"
                "- Outlet: Not specified\n"
                f"- Message: {user_text}"
            ),
            "needs_handoff": True,
            "topic": "AI configuration issue",
            "outlet": "Not specified",
            "summary_message": user_text,
            "notify_staff": False,
            "staff_message": "",
        }

    history = CHAT_HISTORY.get(chat_id, [])

    history_text = "\n".join(
        f"{item['role'].upper()}: {item['content']}" for item in history
    )

    instructions = f"""
You are Jal Yoga Singapore's Telegram customer-service assistant.

You must return JSON only using the required schema.

Use ONLY:
1. The knowledge file below.
2. The live customer-service config below.
3. The recent chat context below.

You are LLM-first:
- You decide the user intent naturally.
- You decide the current flow naturally from recent chat context.
- You decide the topic naturally.
- You decide the outlet naturally.
- You decide if handoff is needed.
- You decide if staff should be notified.
- Do not depend only on exact keywords.
- Understand typos, Singlish, casual phrasing, short forms, and different languages.
- Continue flows based on recent chat context.
- Ask only ONE question at a time.
- Do not restart a flow unless the user says MENU, START, HOME, MAIN MENU, or RESTART.

Hard rules:
- You are not a general-purpose chatbot.
- Only help with Jal Yoga enquiries.
- Be warm, concise, professional, and helpful.
- If replying in English, use British English.
- If the user uses another language, reply in the same language where possible.
- Do not send marketing, promotions, pressure selling, or unrelated content.
- Do not invent prices, schedules, trainers, promotions, outlet numbers, WhatsApp numbers, membership packages, or policy details.
- If information is not clearly confirmed, set needs_handoff to true.
- Never ask for NRIC, passport number, full card number, CVV, OTP, passwords, bank details, or medical documents through the bot.

Handoff:
Set needs_handoff to true when:
- User wants human / agent / real person / customer service / CS.
- Complaint.
- Refund.
- Payment or billing.
- Account or login issue.
- Manual review.
- Membership pricing/details not confirmed.
- Membership cancellation / termination / permanent stop.
- Any answer is not clearly in the knowledge.

When needs_handoff is true:
- reply_text must be short.
- Do not include a WhatsApp link.
- Use this style:

I’ll pass this to our Customer Service team.

Summary:
- Topic: <topic>
- Outlet: <outlet or Not specified>
- Message: <user message>

The app will add the WhatsApp link with the summary prefilled.

Staff notification:
- Set notify_staff to true when a completed request should be sent to the staff Telegram group.
- Set notify_staff to true for completed trial booking.
- Set notify_staff to true for completed refer-a-friend request.
- Set notify_staff to true for completed corporate/partnership request.
- Set notify_staff to true for completed staff hub request.
- Set notify_staff to true when booking-help details have been collected for staff review.
- Set notify_staff to true when the user replies PROCEED after a suspension explanation.
- Otherwise set notify_staff to false and staff_message to an empty string.

For completed trial booking, staff_message must look like:

New Trial Booking
- Outlet: <outlet>
- Class: Trial Class
- Name: <name>
- Fitness Goal: <fitness goal>
- User Telegram Chat ID: {chat_id}

For completed refer-a-friend request, staff_message must look like:

New Refer-a-Friend Request
- Friend Name: <friend name>
- Friend Contact: <friend contact>
- Preferred Studio: <studio>
- User Telegram Chat ID: {chat_id}

For completed corporate request, staff_message must look like:

New Corporate / Partnership Request
- Full Name: <name>
- Work Email: <email>
- Company Name: <company or Not provided>
- User Telegram Chat ID: {chat_id}

For completed staff hub request, staff_message must look like:

New Staff Hub Request
- Member Name: <name>
- Date & Time: <date and time>
- Studio Location & Room: <studio and room>
- User Telegram Chat ID: {chat_id}

For completed booking-help request, staff_message must look like:

New Booking Help Request
- Issue: <booking issue details>
- User Telegram Chat ID: {chat_id}

For suspension PROCEED request, staff_message must look like:

New Membership Suspension Request
- Suspension Type: <Medical or Non-Medical / Travel>
- Outlet: <outlet or Not specified>
- User Telegram Chat ID: {chat_id}

Only say "your details have been sent to our team" if notify_staff is true.

Outlet contact number:
- If user asks for outlet phone number, contact number, call number, or WhatsApp number:
  - If number is confirmed in live config, answer directly.
  - Keep it short.
  - Use this format:

<Outlet> outlet contact:
<phone number>
<WhatsApp link>

Address:
<outlet address>

- Do not include "Outlet Contact Summary".
- Do not repeat the number twice.
- Do not add a long explanation.
- If outlet number is TBC but main Customer Service exists, give main Customer Service.
- If no confirmed number is available, set needs_handoff to true.
- Do not invent phone numbers.

Suspension:
- If user chooses Membership Suspension from the current member menu, first ask:
  "Sure — is this for Medical Suspension or Non-Medical / Travel Suspension?"
- Do not explain full suspension policy until user chooses the type.
- Medical/doctor/injury means Medical Suspension.
- Travel/non-medical/overseas/holiday means Non-Medical / Travel Suspension.
- If user replies PROCEED after suspension info, say:
  "Thank you for your submission! Our Customer Care team will review your request and get back to you within 48 hours."
- For PROCEED after suspension info, set notify_staff to true.

Trial:
- Ask one question at a time: studio, full name, fitness goal, summary.
- Fitness goal can be words, numbers, decimals, or mixed text and numbers.
- Do not reject numeric fitness goals.
- When all trial details are collected, show Trial Booking Summary and set notify_staff to true.

Current member:
- Option 2 from main menu means current member.
- Option 2 inside current member menu means membership suspension.
- Use recent chat context to know which menu the user is in.

Menu:
- hi, hello, hey, start, /start, menu, main menu, home, restart means show main menu.

Output JSON fields:
- reply_text: final message to user, without WhatsApp handoff link.
- needs_handoff: true or false.
- topic: short topic for summary.
- outlet: one of the allowed outlet enum values.
- summary_message: short version of user request.
- notify_staff: true or false.
- staff_message: exact message to send to staff group, or empty string.

{live_contact_config_text()}

USER TELEGRAM CHAT ID:
{chat_id}

KNOWLEDGE FILE:
{KNOWLEDGE_TEXT}

CURRENT TIME IN SINGAPORE:
{now_singapore_iso()}

RECENT CHAT:
{history_text}
"""

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            reasoning={"effort": "low"},
            instructions=instructions,
            input=user_text,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "jal_yoga_bot_reply",
                    "schema": bot_reply_schema(),
                    "strict": True,
                }
            },
        )

        raw_text = (response.output_text or "").strip()
        bot_data = parse_bot_json(raw_text, user_text)

    except Exception as e:
        print("OPENAI ERROR:", str(e))
        traceback.print_exc()
        bot_data = fallback_bot_data(user_text)

    if bot_data.get("needs_handoff"):
        bot_data = ensure_handoff_summary(bot_data, user_text)

    add_history(chat_id, "user", user_text)
    add_history(chat_id, "assistant", bot_data["reply_text"])

    return bot_data


# =========================
# MAIN MESSAGE PROCESSOR
# =========================

def process_message(chat_id: str, user_text: str) -> str:
    clean_text = user_text.strip()

    if not clean_text:
        return "Please type your message, or type MENU to see the options."

    if is_opt_out_request(clean_text):
        OPT_OUT_USERS.add(chat_id)
        save_opt_out_users()
        reset_history(chat_id)

        save_request(
            "user_opted_out",
            chat_id,
            {
                "user_message": clean_text,
            },
        )

        return (
            "Noted — you have been unsubscribed and will not receive follow-up messages.\n"
            "If you need help later, reply START."
        )

    if is_opt_in_request(clean_text) and chat_id in OPT_OUT_USERS:
        OPT_OUT_USERS.discard(chat_id)
        save_opt_out_users()
        reset_history(chat_id)

        save_request(
            "user_opted_in",
            chat_id,
            {
                "user_message": clean_text,
            },
        )

        return "Welcome back — you are subscribed again. Type MENU to see Jal Yoga options."

    if chat_id in OPT_OUT_USERS:
        return "You have opted out. Reply START if you want to chat with Jal Yoga again."

    if contains_sensitive_keyword(clean_text):
        save_request(
            "sensitive_info_blocked",
            chat_id,
            {
                "preview": clean_text[:120],
            },
        )

        return (
            "For your safety, please do not share NRIC, passport numbers, full card numbers, "
            "CVV, OTP, passwords, or bank details here.\n\n"
            "For account-specific or payment-related help, please type CUSTOMER SERVICE."
        )

    if is_reset_request(clean_text):
        reset_history(chat_id)

    bot_data = ask_llm(chat_id, clean_text)

    reply_text = strip_handoff_token(bot_data.get("reply_text", "")).strip()

    if bot_data.get("notify_staff") and bot_data.get("staff_message"):
        try:
            send_staff_telegram_message(bot_data["staff_message"])
        except Exception as e:
            print("FAILED TO SEND STAFF MESSAGE:", str(e))
            traceback.print_exc()

            reply_text += (
                "\n\nNote: I could not send this to the staff group automatically. "
                "Please type CUSTOMER SERVICE if you need help."
            )

    if bot_data.get("needs_handoff"):
        save_request(
            "customer_service_handoff",
            chat_id,
            {
                "user_message": clean_text,
                "bot_data": bot_data,
            },
        )

        prefilled_link = customer_service_prefilled_link(bot_data, clean_text)

        if prefilled_link:
            return (
                f"{reply_text}\n\n"
                f"Tap here to send this summary to Customer Service:\n"
                f"{prefilled_link}\n\n"
                f"Reply MENU to return to the main menu."
            )

        return (
            f"{reply_text}\n\n"
            "Our Customer Service team will review your message and get back to you.\n\n"
            "Reply MENU to return to the main menu."
        )

    return f"{reply_text}\n\nReply MENU to return to the main menu."


# =========================
# TELEGRAM SEND MESSAGE
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
        print("Missing TELEGRAM_BOT_TOKEN")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    for chunk in split_long_message(message):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }

        response = requests.post(url, json=payload, timeout=30)

        print("TELEGRAM SEND STATUS:", response.status_code)
        print("TELEGRAM SEND RESPONSE:", response.text)

        response.raise_for_status()

    return True


# =========================
# ROUTES
# =========================

@app.route("/", methods=["GET"])
def home():
    telegram_link = "#"

    if TELEGRAM_BOT_USERNAME:
        username = TELEGRAM_BOT_USERNAME.replace("@", "").strip()
        telegram_link = f"https://t.me/{username}"

    whatsapp_link = customer_service_link() or "#"

    return render_template(
        "index.html",
        telegram_link=telegram_link,
        whatsapp_link=whatsapp_link,
        studios=STUDIOS,
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "ok",
            "message": "healthy",
        }
    )


@app.route("/telegram/webhook", methods=["GET"])
def telegram_webhook_test():
    return jsonify(
        {
            "status": "ok",
            "message": "Telegram webhook route exists. Telegram will use POST here.",
        }
    )


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    if TELEGRAM_SECRET_TOKEN:
        incoming_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            "",
        )

        if incoming_secret != TELEGRAM_SECRET_TOKEN:
            return jsonify({"status": "forbidden"}), 403

    update = request.get_json(silent=True) or {}

    message = update.get("message") or update.get("edited_message")

    if not message:
        return jsonify({"status": "ignored", "reason": "no message"}), 200

    chat = message.get("chat", {})
    chat_id = str(chat.get("id", ""))
    chat_type = chat.get("type", "")

    if not chat_id:
        return jsonify({"status": "ignored", "reason": "no chat id"}), 200

    if (
        STAFF_TELEGRAM_CHAT_ID
        and chat_id == STAFF_TELEGRAM_CHAT_ID
        and chat_type in {"group", "supergroup"}
    ):
        return jsonify({"status": "ignored", "reason": "staff group message"}), 200

    user_text = message.get("text", "")

    if not user_text:
        return jsonify({"status": "ignored", "reason": "non-text message"}), 200

    try:
        reply = process_message(chat_id, user_text)
        send_telegram_message(chat_id, reply)

    except Exception as e:
        print("ERROR:", str(e))
        traceback.print_exc()

        save_request(
            "server_error",
            chat_id,
            {
                "user_text": user_text,
                "error": str(e),
            },
        )

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
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    )