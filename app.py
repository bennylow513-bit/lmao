import json
import os
import re
from datetime import datetime
from difflib import get_close_matches
from typing import Any, Dict, List, Optional, Tuple
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
# KEYWORDS
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

JAL_YOGA_GENERAL_KEYWORDS = {
    "jal",
    "yoga",
    "pilates",
    "barre",
    "trial",
    "class",
    "classes",
    "schedule",
    "studio",
    "studios",
    "outlet",
    "outlets",
    "location",
    "address",
    "membership",
    "member",
    "package",
    "price",
    "pricing",
    "booking",
    "book",
    "cancel",
    "cancellation",
    "suspend",
    "suspension",
    "pause",
    "freeze",
    "refer",
    "friend",
    "corporate",
    "partnership",
    "staff",
    "hours",
    "open",
    "close",
    "trainer",
    "service",
    "human",
    "agent",
}

KNOWN_INTENTS: Dict[str, List[str]] = {
    "trial": [
        "trial",
        "free trial",
        "trial lesson",
        "trial class",
        "book trial",
        "schedule trial",
        "try class",
        "first time class",
        "trail lesson",
        "triel",
        "free lesson",
        "got trial",
    ],
    "suspension": [
        "suspension",
        "suspend membership",
        "pause membership",
        "freeze membership",
        "stop membership",
        "travel suspension",
        "medical suspension",
        "suspen",
    ],
    "cancellation": [
        "cancel class",
        "class cancellation",
        "cancel booking",
        "late cancellation",
    ],
    "booking_help": [
        "booking help",
        "cannot book",
        "cant book",
        "can't book",
        "booking issue",
        "class booking",
        "cannot book class",
        "cant bok",
    ],
    "refer_friend": [
        "refer friend",
        "refer a friend",
        "friend referral",
        "reffer friend",
        "refer a fren",
    ],
    "corporate": [
        "corporate",
        "partnership",
        "corporate partnership",
        "corporate collab",
        "parternship",
    ],
    "staff_hub": [
        "staff hub",
        "staff booking",
        "staf hub",
    ],
    "locations": [
        "location",
        "locations",
        "address",
        "addresses",
        "studio",
        "studios",
        "outlet",
        "outlets",
        "where is",
        "where ah",
    ],
    "hours": [
        "hours",
        "opening hours",
        "operating hours",
        "what time open",
        "what time close",
    ],
}

STOP_WORDS = {
    "is", "was", "are", "am", "be", "been",
    "do", "does", "did",
    "can", "could", "will", "would", "should",
    "a", "an", "the",
    "to", "for", "of", "in", "on", "at",
    "i", "you", "me", "my", "your", "our",
    "what", "where", "when", "how", "why",
    "please", "pls",
    "ah", "lah", "leh", "anot",
    "tell", "say", "know",
}


# =========================
# KNOWLEDGE
# =========================

def load_knowledge_text() -> str:
    for filename in ("knowledge.txt", "knowledge(6).txt", "knowledge(1).txt"):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            continue

    return ""


KNOWLEDGE_TEXT = load_knowledge_text()


def extract_section(title: str, text: str) -> str:
    pattern = rf"(?ms)^{re.escape(title)}\s*\n(.*?)(?=^[A-Z][A-Z /&()'\-]+$|\Z)"
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def extract_bullets(section_text: str) -> List[str]:
    items: List[str] = []

    for line in section_text.splitlines():
        line = line.strip()

        if line.startswith("- "):
            items.append(line[2:].strip())

    return items


def parse_studios(text: str) -> List[Dict[str, str]]:
    section = extract_section("STUDIOS", text)
    studios: List[Dict[str, str]] = []

    for item in extract_bullets(section):
        if ":" in item:
            name, address = item.split(":", 1)
            studios.append(
                {
                    "name": name.strip(),
                    "address": address.strip(),
                }
            )

    return studios


STUDIOS = parse_studios(KNOWLEDGE_TEXT)

if not STUDIOS:
    STUDIOS = [
        {"name": "Alexandra", "address": "456 Alexandra Rd, #02-03, Singapore 119962"},
        {"name": "Katong", "address": "131 E Coast Rd, #03-01, Singapore 428816"},
        {"name": "Kovan", "address": "1F Yio Chu Kang Rd, Singapore 545512"},
        {"name": "Upper Bukit Timah", "address": "816 Upper Bukit Timah Road, Singapore 678149"},
        {"name": "Woodlands", "address": "8 Woodlands Sq, #04-12/13 Wood Square, Solo 2, Singapore 737713"},
    ]

KNOWN_STUDIOS = [studio["name"] for studio in STUDIOS]


# =========================
# HELPERS
# =========================

def normalize(text: str) -> str:
    return " ".join((text or "").strip().lower().replace("’", "'").split())


def extract_important_words(text: str) -> List[str]:
    words = normalize(text).split()
    return [word for word in words if word not in STOP_WORDS]


def now_singapore_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Singapore")).isoformat()


def save_request(kind: str, chat_id: str, payload: Dict[str, Any]) -> None:
    record = {
        "kind": kind,
        "chat_id": chat_id,
        "payload": payload,
        "created_at_sg": now_singapore_iso(),
    }

    with open("requests_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


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

    CHAT_HISTORY[chat_id] = CHAT_HISTORY[chat_id][-14:]


def best_fuzzy_match(text: str, choices: List[str], cutoff: float = 0.72) -> Optional[str]:
    matches = get_close_matches(
        text.lower(),
        [choice.lower() for choice in choices],
        n=1,
        cutoff=cutoff,
    )

    if not matches:
        return None

    matched_lower = matches[0]

    for choice in choices:
        if choice.lower() == matched_lower:
            return choice

    return None


def detect_studio_from_text(text: str) -> Tuple[Optional[str], float]:
    t = normalize(text)

    for studio in KNOWN_STUDIOS:
        if studio.lower() in t:
            return studio, 1.0

    important_words = extract_important_words(t)

    candidates = [t]
    candidates.extend(important_words)

    best = None
    best_score = 0.0

    for candidate in candidates:
        matches = get_close_matches(
            candidate,
            [studio.lower() for studio in KNOWN_STUDIOS],
            n=1,
            cutoff=0.6,
        )

        if matches:
            matched = matches[0]

            if candidate == matched:
                score = 1.0
            elif len(candidate) >= 4:
                score = 0.82
            else:
                score = 0.70

            for studio in KNOWN_STUDIOS:
                if studio.lower() == matched and score > best_score:
                    best = studio
                    best_score = score

    return best, best_score


def detect_intent_from_text(text: str) -> Tuple[Optional[str], float]:
    t = normalize(text)

    for intent, phrases in KNOWN_INTENTS.items():
        for phrase in phrases:
            if phrase in t:
                return intent, 1.0

    important_words = extract_important_words(t)
    filtered_text = " ".join(important_words)

    all_phrases = []
    phrase_to_intent = {}

    for intent, phrases in KNOWN_INTENTS.items():
        for phrase in phrases:
            all_phrases.append(phrase)
            phrase_to_intent[phrase] = intent

    if filtered_text:
        match = best_fuzzy_match(filtered_text, all_phrases, cutoff=0.58)

        if match:
            return phrase_to_intent[match], 0.78

    for word in important_words:
        match = best_fuzzy_match(word, all_phrases, cutoff=0.72)

        if match:
            return phrase_to_intent[match], 0.72

    return None, 0.0


def enrich_user_text_for_llm(text: str) -> str:
    original = text or ""

    studio, studio_score = detect_studio_from_text(original)
    intent, intent_score = detect_intent_from_text(original)

    hints = []

    if studio and studio_score >= 0.8:
        hints.append(f"Detected studio: {studio}")

    if intent and intent_score >= 0.75:
        hints.append(f"Detected intent: {intent}")

    if not hints:
        return original

    return original + "\n\nSYSTEM HINTS:\n" + "\n".join(
        f"- {hint}" for hint in hints
    )


def is_menu_request(text: str) -> bool:
    return normalize(text) in {
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


def is_opt_out_request(text: str) -> bool:
    t = normalize(text)

    if t in OPT_OUT_WORDS:
        return True

    long_phrases = [word for word in OPT_OUT_WORDS if " " in word or "-" in word]
    return any(phrase in t for phrase in long_phrases)


def is_opt_in_request(text: str) -> bool:
    return normalize(text) in OPT_IN_WORDS


def contains_sensitive_keyword(text: str) -> bool:
    t = normalize(text)
    return any(keyword in t for keyword in SENSITIVE_KEYWORDS)


def is_handoff_request(text: str) -> bool:
    t = normalize(text)

    keywords = [
        "customer service",
        "agent",
        "human",
        "staff",
        "representative",
        "speak to someone",
        "speak to a person",
        "talk to human",
        "talk to real person",
        "real person",
        "complaint",
        "refund",
        "payment",
        "account",
        "manual review",
        "billing issue",
        "billing",
        "login issue",
        "login problem",
        "cs",
    ]

    return any(keyword in t for keyword in keywords)


def is_jal_yoga_related(chat_id: str, text: str) -> bool:
    if CHAT_HISTORY.get(chat_id):
        return True

    t = normalize(text)

    if not t:
        return True

    if is_menu_request(t) or is_handoff_request(t):
        return True

    if t in {
        "yes",
        "no",
        "ok",
        "okay",
        "sure",
        "proceed",
        "skip",
        "thanks",
        "thank you",
    }:
        return True

    studio, studio_score = detect_studio_from_text(t)

    if studio and studio_score >= 0.72:
        return True

    intent, intent_score = detect_intent_from_text(t)

    if intent and intent_score >= 0.72:
        return True

    words = set(t.split())
    return bool(words & JAL_YOGA_GENERAL_KEYWORDS)


def strip_handoff_token(text: str) -> str:
    return text.replace("[HANDOFF]", "").strip()


def customer_service_reply() -> str:
    if CUSTOMER_SERVICE_WHATSAPP_NUMBER:
        number = CUSTOMER_SERVICE_WHATSAPP_NUMBER.replace("+", "").replace(" ", "")

        return (
            "You can speak to our Customer Service team on WhatsApp here:\n"
            f"https://wa.me/{number}\n\n"
            "Please send them your enquiry and they will assist you."
        )

    return (
        "Please let us know how we can help you!\n\n"
        "Simply type your enquiry below. While our response may not be immediate, "
        "our Customer Service team will review your message and get back to you as soon as possible."
    )


# =========================
# OPENAI BOT BRAIN
# =========================

def ask_llm(chat_id: str, user_text: str, history_user_text: Optional[str] = None) -> str:
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
You are Jal Yoga Singapore's Telegram assistant.

Use ONLY the knowledge below.

Core behavior:
- You are a Jal Yoga customer-service assistant, not a general-purpose chatbot.
- Handle as much of the Jal Yoga conversation as possible naturally.
- Ask one question at a time.
- Continue multi-step flows based on recent chat context.
- Keep replies concise, warm, and professional.
- Do not send marketing, promotions, pressure selling, or unrelated content.
- Do not invent prices, schedules, trainers, promotions, phone numbers, or any facts not shown in the knowledge.
- Never ask for NRIC, passport number, full payment card number, CVV, OTP, passwords, or bank details.
- If the user asks for medical diagnosis, legal advice, financial advice, or anything unrelated to Jal Yoga, politely say you can only help with Jal Yoga enquiries.
- If the answer is not clearly in the knowledge, or the issue is complaint, refund, payment, account-specific, billing-specific, login-specific, manual review, or the user wants a real human, include exactly this token on a new line:
[HANDOFF]

Important behavior rules:
- If the user is only asking for information, explain the policy only.
- Do not treat a question as a submitted request.
- Only treat it as a real request if the user clearly says they want to proceed, want help to proceed, want to submit it now, or reply PROCEED.
- For suspension questions:
  - if asking only, explain the suspension policy only
  - then end with: "If you would like our Customer Service team to help you proceed, please reply PROCEED."
  - only after the user clearly wants to proceed should you say the request will be reviewed
- For cancellation questions:
  - if asking only, explain how cancellation works
  - if the user needs manual help, missed the timing, or has an app/account issue, use [HANDOFF]
- If the user gives multiple needed details in one message, use them and continue to the next missing step.
- Do not restart a flow unless the user says MENU, START, HOME, MAIN MENU, or RESTART.
- Understand common typos, casual phrasing, short forms, and Singapore-style phrasing.
- Use any SYSTEM HINTS if provided, but only if they make sense with the user's message.
- If a likely studio name is unclear, ask for confirmation briefly instead of guessing.
- If the user message contains typos but the meaning is still clear, answer the intended meaning naturally.
- If the user message is unclear but close to a known Jal Yoga topic, ask a short clarification question instead of guessing wrongly.

Conversation behavior:
- If the user asks for the menu, show the Jal Yoga main menu from the knowledge.
- If the user asks about a trial, free trial, trial class, trial lesson, or similar typo, follow the trial flow in the knowledge.
- If the user asks about studios, outlets, locations, or addresses:
  - If one studio is named, give that studio's address directly.
  - If they ask for all studios or outlets, list all studios with addresses.
  - If they ask about location but do not specify which studio, ask which studio they mean.
- If the user asks about operating hours, answer directly from the knowledge.
- If the user is a current member, help using the knowledge for cancellation, suspension, booking help, and refer-a-friend.
- If the user asks about corporate or partnerships, follow the corporate flow in the knowledge.
- If the user asks about staff hub, follow the staff hub flow in the knowledge.
- When a flow is completed, use the appropriate closing style shown in the knowledge.
- If the user asks for a different language, reply in the same language that the user has used.
- If the user speaks in English, reply in British English.
- If the user is rude or insulting, stay calm and professional. Do not insult the user back. If needed, offer customer service handoff.
- End main menu and follow-up style replies with: "Reply STOP anytime to stop receiving follow-up messages."

Telegram-specific behavior:
- You are replying inside Telegram, not WhatsApp.
- Do not mention Meta, webhook, or WhatsApp Cloud API to customers.
- If the user wants a real human, use [HANDOFF].
- If giving customer service handoff, do not invent phone numbers. The app will add the customer service link separately.

KNOWLEDGE:
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

    clean_answer = strip_handoff_token(answer)

    add_history(chat_id, "user", history_user_text or user_text)
    add_history(chat_id, "assistant", clean_answer)

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

        save_request("user_opted_out", chat_id, {"user_message": clean_text})

        return (
            "Noted — you have been unsubscribed and will not receive follow-up messages.\n"
            "If you need help later, reply START."
        )

    if is_opt_in_request(clean_text) and chat_id in OPT_OUT_USERS:
        OPT_OUT_USERS.discard(chat_id)
        save_opt_out_users()

        save_request("user_opted_in", chat_id, {"user_message": clean_text})

        return "Welcome back — you are subscribed again. Type MENU to see Jal Yoga options."

    if chat_id in OPT_OUT_USERS:
        return "You have opted out. Reply START if you want to chat with Jal Yoga again."

    if contains_sensitive_keyword(clean_text):
        save_request("sensitive_info_blocked", chat_id, {"preview": clean_text[:120]})

        return (
            "For your safety, please do not share NRIC, passport numbers, card numbers, CVV, OTP, "
            "passwords, or bank details here.\n\n"
            "For account-specific or payment-related help, please type CUSTOMER SERVICE."
        )

    if not is_jal_yoga_related(chat_id, clean_text):
        return (
            "I can help with Jal Yoga enquiries such as trial classes, schedules, outlets, prices, "
            "memberships, bookings, and customer support.\n\n"
            "Type MENU to see the options, or CUSTOMER SERVICE to speak to our team."
        )

    enriched_text = enrich_user_text_for_llm(clean_text)

    if is_menu_request(clean_text):
        reset_history(chat_id)

        answer = ask_llm(
            chat_id,
            "Show the Jal Yoga main menu exactly as written in the knowledge. Add this line at the end: Reply STOP anytime to stop receiving follow-up messages.",
            history_user_text=clean_text,
        )

        return strip_handoff_token(answer)

    if is_handoff_request(clean_text):
        reset_history(chat_id)

        save_request("customer_service_handoff", chat_id, {"user_message": clean_text})

        return customer_service_reply()

    answer = ask_llm(chat_id, enriched_text, history_user_text=clean_text)

    if "[HANDOFF]" in answer:
        clean_answer = strip_handoff_token(answer)
        reset_history(chat_id)

        save_request(
            "customer_service_handoff",
            chat_id,
            {
                "user_message": clean_text,
                "llm_answer": clean_answer,
            },
        )

        if clean_answer:
            return clean_answer + "\n\n" + customer_service_reply()

        return customer_service_reply()

    return strip_handoff_token(answer) + "\n\nReply MENU to return to the main menu."


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
            "disable_web_page_preview": False,
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

    whatsapp_link = "#"

    if CUSTOMER_SERVICE_WHATSAPP_NUMBER:
        number = CUSTOMER_SERVICE_WHATSAPP_NUMBER.replace("+", "").replace(" ", "")
        whatsapp_link = f"https://wa.me/{number}"

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