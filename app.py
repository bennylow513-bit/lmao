import json
import os
import traceback
from datetime import datetime
from typing import Dict, List
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

# Stores handoff summaries while waiting for the user to choose outlet
PENDING_HANDOFFS: Dict[str, Dict[str, str]] = {}

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
# SMALL SAFETY HELPERS
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


def save_request(kind: str, chat_id: str, payload: Dict) -> None:
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


def strip_handoff_token(text: str) -> str:
    return text.replace("[HANDOFF]", "").strip()


def clean_number(number: str) -> str:
    return (
        str(number)
        .replace("+", "")
        .replace(" ", "")
        .replace("-", "")
        .strip()
    )


def customer_service_link() -> str:
    number = clean_number(CUSTOMER_SERVICE_WHATSAPP_NUMBER)

    if not number or number.upper() == "TBC":
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


def detect_outlet_from_text(text: str) -> str:
    t = normalize(text)

    outlet_aliases = {
        "upper bukit timah": "Upper Bukit Timah",
        "bukit timah": "Upper Bukit Timah",
        "bukit timmah": "Upper Bukit Timah",
        "ubt": "Upper Bukit Timah",
        "alexandra": "Alexandra",
        "alex": "Alexandra",
        "katong": "Katong",
        "katon": "Katong",
        "kovan": "Kovan",
        "koven": "Kovan",
        "woodlands": "Woodlands",
        "woodland": "Woodlands",
    }

    for alias, outlet in outlet_aliases.items():
        if alias in t:
            return outlet

    return ""


def build_prefilled_whatsapp_link(number: str, message: str) -> str:
    clean = clean_number(number)

    if not clean or clean.upper() == "TBC":
        return ""

    encoded_message = quote(message, safe="")
    return f"https://wa.me/{clean}?text={encoded_message}"


def customer_service_prefilled_link(user_text: str, summary_text: str) -> str:
    detected_outlet = detect_outlet_from_text(user_text + "\n" + summary_text)

    selected_number = ""

    if detected_outlet:
        selected_number = outlet_whatsapp_numbers().get(detected_outlet, "")

    if not selected_number:
        selected_number = CUSTOMER_SERVICE_WHATSAPP_NUMBER

    prefilled_message = (
        "Hello Jal Yoga Customer Service,\n\n"
        "I need help with this enquiry:\n\n"
        f"{summary_text}\n\n"
        "Thank you."
    )

    return build_prefilled_whatsapp_link(selected_number, prefilled_message)


# =========================
# OUTLET CONTACT FORMAT
# =========================

def get_studio_address(outlet_name: str) -> str:
    for studio in STUDIOS:
        if studio["name"].lower() == outlet_name.lower():
            return studio["address"]

    return ""


def is_outlet_contact_request(text: str) -> bool:
    t = normalize(text)
    tokens = set(t.split())

    contact_words = [
        "contact",
        "contact number",
        "phone",
        "phone number",
        "number",
        "whatsapp",
        "whatsapp number",
        "call",
        "telephone",
        "hotline",
        "wa number",
    ]

    if "wa" in tokens:
        return True

    return any(word in t for word in contact_words)


def build_outlet_contact_reply(user_text: str) -> str:
    outlet = detect_outlet_from_text(user_text)

    if not outlet:
        return ""

    if not is_outlet_contact_request(user_text):
        return ""

    number = outlet_whatsapp_numbers().get(outlet, "")

    if not number or clean_number(number).upper() == "TBC":
        number = CUSTOMER_SERVICE_WHATSAPP_NUMBER

    clean = clean_number(number)

    if not clean or clean.upper() == "TBC":
        return ""

    address = get_studio_address(outlet)
    whatsapp_link = f"https://wa.me/{clean}"

    return (
        f"{outlet} outlet contact:\n"
        f"+{clean}\n"
        f"{whatsapp_link}\n\n"
        f"Address:\n"
        f"{address}"
    )


def replace_summary_outlet(summary_text: str, outlet: str) -> str:
    lines = summary_text.splitlines()
    new_lines = []

    outlet_replaced = False

    for line in lines:
        if line.strip().lower().startswith("- outlet:"):
            new_lines.append(f"- Outlet: {outlet}")
            outlet_replaced = True
        else:
            new_lines.append(line)

    if not outlet_replaced:
        new_lines.append(f"- Outlet: {outlet}")

    return "\n".join(new_lines)


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
- For handoff, the app will create a WhatsApp link with the summary prefilled.
"""


# =========================
# LLM BRAIN
# =========================

def ask_llm(chat_id: str, user_text: str) -> str:
    if not OPENAI_API_KEY:
        return (
            "I’m sorry — the AI answer service is not configured yet.\n"
            "Please type CUSTOMER SERVICE and our team will follow up."
        )

    history = CHAT_HISTORY.get(chat_id, [])

    history_text = "\n".join(
        f"{item['role'].upper()}: {item['content']}" for item in history
    )

    instructions = f"""
You are Jal Yoga Singapore's Telegram customer-service assistant.

Use ONLY:
1. The knowledge file below.
2. The live customer-service config below.
3. The recent chat context below.

You are LLM-first:
- Decide the user's intent naturally.
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
- If information is not clearly confirmed, say you are not fully sure and use [HANDOFF].
- Never ask for NRIC, passport number, full card number, CVV, OTP, passwords, bank details, or medical documents through the bot.

CUSTOMER SERVICE HANDOFF

Hand off when:
- User wants human / agent / real person / customer service / CS
- Complaint
- Refund
- Payment or billing
- Account or login issue
- Manual review
- Membership pricing/details not confirmed
- Membership cancellation / termination / permanent stop
- Any answer is not clearly in the knowledge

Use this short structure:

I’ll pass this to our Customer Service team.

Summary:
- Topic: <topic>
- Outlet: <outlet or Not specified>
- Message: <user message>

[HANDOFF]

Do not add long explanations before the summary.
Do not say "Please let us know how we can help you" unless the user directly asks for Customer Service.
Do not repeat the same handoff message twice.

Important:
- If [HANDOFF] is used and the outlet is unknown, write "- Outlet: Not specified".
- The app will ask the user for a specific outlet before sending the WhatsApp Customer Service link.

The app will add a WhatsApp link after [HANDOFF].
The WhatsApp link will open with the summary already typed.
The user still needs to press Send manually.

Important difference:
- Class cancellation means cancelling a booked class.
- Membership suspension means temporary pause, freeze, hold, or stop for a while.
- Membership cancellation means ending membership permanently.
- Do NOT treat membership cancellation as suspension.
- If user says cancel, terminate, end, or quit membership, use [HANDOFF].
- If user says pause, freeze, hold, suspend, temporary stop, travel freeze, medical freeze, going overseas, or cannot attend for a while, explain suspension policy.

Suspension behaviour:
- If user chooses Membership Suspension from the current member menu, first ask:
  "Sure — is this for Medical Suspension or Non-Medical / Travel Suspension?"
- Do not explain the full suspension policy until the user chooses the type.
- If the user clearly says medical, doctor, physician, injury, doctor memo, or recovering from injury, explain Medical Suspension.
- If the user clearly says travel, non-medical, overseas, holiday, going overseas, or cannot attend for a while, explain Non-Medical / Travel Suspension.
- If the user is only asking about suspension, explain the correct suspension policy only.
- End with:
  "If you would like our Customer Service team to help you proceed, please reply PROCEED."
- If the user clearly wants to proceed or replies PROCEED after suspension info, say:
  "Thank you for your submission! Our Customer Care team will review your request and get back to you within 48 hours."

Trial flow:
- If user asks about trial, free trial, trial lesson, trail lesson, triel, beginner trial, or got trial anot, start trial flow.
- Ask one question at a time:
  1. Preferred studio
  2. Full Name
  3. Fitness Goal
  4. Show summary
- If user gives multiple details in one message, use them and ask only for the next missing detail.
- Fitness Goal can be words, numbers, or decimals, for example flexibility, weight loss, 55, 55.5, lose 5kg.
- Do not reject numeric fitness goals.
- Final trial summary format:
  Trial Booking Summary:
  - Outlet: <studio>
  - Class: Trial Class
  - Name: <name>
  - Fitness Goal: <goal>

  Thank you! I've sent your details to the <studio> team. Our Studio Manager will contact you within 24 hours to schedule your trial.

Current member flow:
- If user chooses current member or option 2 from main menu, show the current member menu from the knowledge.
- If they choose option 1 inside current member menu, explain class cancellation only.
- If they choose option 2 inside current member menu, ask:
  "Sure — is this for Medical Suspension or Non-Medical / Travel Suspension?"
- If they choose option 3 inside current member menu, follow booking help.
- If they choose option 4 inside current member menu, follow refer-a-friend.
- If they ask membership cancellation, use [HANDOFF].

Outlet and contact number:
- If user asks for an outlet address, provide the address from the knowledge.
- If user asks for an outlet number, phone number, contact number, or WhatsApp contact:
  - Use the live customer-service config if a number is confirmed.
  - Keep the reply short.
  - Use this format only:

<Outlet> outlet contact:
<phone number>
<WhatsApp link>

Address:
<outlet address>

- Do not include "Outlet Contact Summary".
- Do not repeat the phone number twice.
- Do not add a long explanation.
- If outlet number is TBC but main Customer Service exists, give main Customer Service.
- If no number is confirmed, use [HANDOFF].
- Do not invent numbers.

Menu:
If user asks for menu, start, /start, home, main menu, restart, hi, hello, or hey, show the Jal Yoga main menu from the knowledge.

Output:
- Reply directly to the customer.
- Do not explain your reasoning.
- Keep replies short unless a policy explanation is needed.
- Do not include [HANDOFF] unless handoff is needed.

{live_contact_config_text()}

KNOWLEDGE FILE:
{KNOWLEDGE_TEXT}

CURRENT TIME IN SINGAPORE:
{now_singapore_iso()}

RECENT CHAT:
{history_text}
"""

    response = client.responses.create(
        model=OPENAI_MODEL,
        reasoning={"effort": "low"},
        instructions=instructions,
        input=user_text,
    )

    answer = (response.output_text or "").strip()

    if not answer:
        answer = (
            "I’m sorry — I’m not fully sure based on the information I have.\n"
            "[HANDOFF]"
        )

    add_history(chat_id, "user", user_text)
    add_history(chat_id, "assistant", strip_handoff_token(answer))

    return answer


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
        PENDING_HANDOFFS.pop(chat_id, None)

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
        PENDING_HANDOFFS.pop(chat_id, None)

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
        PENDING_HANDOFFS.pop(chat_id, None)

    # =========================
    # HANDLE PENDING HANDOFF OUTLET QUESTION
    # =========================

    if chat_id in PENDING_HANDOFFS:
        pending = PENDING_HANDOFFS.pop(chat_id)

        selected_outlet = detect_outlet_from_text(clean_text)

        no_outlet_words = {
            "no",
            "nope",
            "none",
            "not sure",
            "not specified",
            "any",
            "any outlet",
            "no specific outlet",
            "dont know",
            "don't know",
            "idk",
            "unsure",
        }

        if not selected_outlet and normalize(clean_text) not in no_outlet_words:
            PENDING_HANDOFFS[chat_id] = pending

            return (
                "Sorry, which outlet is this about?\n\n"
                "Please reply with one of these:\n"
                "- Alexandra\n"
                "- Katong\n"
                "- Kovan\n"
                "- Upper Bukit Timah\n"
                "- Woodlands\n"
                "- Not specified"
            )

        outlet_for_summary = selected_outlet if selected_outlet else "Not specified"

        clean_answer = replace_summary_outlet(
            pending["clean_answer"],
            outlet_for_summary,
        )

        save_request(
            "customer_service_handoff",
            chat_id,
            {
                "user_message": pending["user_message"],
                "selected_outlet": outlet_for_summary,
                "llm_answer": clean_answer,
            },
        )

        prefilled_link = customer_service_prefilled_link(
            outlet_for_summary + "\n" + pending["user_message"],
            clean_answer,
        )

        if prefilled_link:
            return (
                f"{clean_answer}\n\n"
                f"Tap here to send this summary to Customer Service:\n"
                f"{prefilled_link}\n\n"
                f"Reply MENU to return to the main menu."
            )

        return (
            f"{clean_answer}\n\n"
            "Our Customer Service team will review your message and get back to you.\n\n"
            "Reply MENU to return to the main menu."
        )

    # =========================
    # DIRECT OUTLET CONTACT REPLY
    # =========================

    outlet_contact_reply = build_outlet_contact_reply(clean_text)

    if outlet_contact_reply:
        return outlet_contact_reply + "\n\nReply MENU to return to the main menu."

    # =========================
    # ASK LLM
    # =========================

    answer = ask_llm(chat_id, clean_text)

    if "[HANDOFF]" in answer:
        clean_answer = strip_handoff_token(answer).strip()
        detected_outlet = detect_outlet_from_text(clean_text + "\n" + clean_answer)

        # NEW:
        # If Customer Service handoff is needed but no outlet is mentioned,
        # ask the user for outlet first.
        if not detected_outlet:
            PENDING_HANDOFFS[chat_id] = {
                "user_message": clean_text,
                "clean_answer": clean_answer,
            }

            return (
                "Before I pass this to our Customer Service team, "
                "do you have a specific outlet for this enquiry?\n\n"
                "Please reply with one of these:\n"
                "- Alexandra\n"
                "- Katong\n"
                "- Kovan\n"
                "- Upper Bukit Timah\n"
                "- Woodlands\n"
                "- Not specified"
            )

        save_request(
            "customer_service_handoff",
            chat_id,
            {
                "user_message": clean_text,
                "llm_answer": clean_answer,
            },
        )

        prefilled_link = customer_service_prefilled_link(clean_text, clean_answer)

        if prefilled_link:
            return (
                f"{clean_answer}\n\n"
                f"Tap here to send this summary to Customer Service:\n"
                f"{prefilled_link}\n\n"
                f"Reply MENU to return to the main menu."
            )

        return (
            f"{clean_answer}\n\n"
            "Our Customer Service team will review your message and get back to you.\n\n"
            "Reply MENU to return to the main menu."
        )

    return strip_handoff_token(answer).strip() + "\n\nReply MENU to return to the main menu."


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

    if not chat_id:
        return jsonify({"status": "ignored", "reason": "no chat id"}), 200

    user_text = message.get("text", "")

    if not user_text:
        send_telegram_message(
            chat_id,
            "I can currently handle text messages only. Please type your message, or type MENU.",
        )

        return jsonify({"status": "ok"}), 200

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