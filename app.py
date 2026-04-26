import json
import os
import re
import threading
import time
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

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "jal_yoga_verify_token")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v23.0")
PORT = int(os.getenv("PORT", "5000"))

PUBLIC_WHATSAPP_NUMBER = os.getenv("PUBLIC_WHATSAPP_NUMBER", "")

client = OpenAI(api_key=OPENAI_API_KEY)

CHAT_HISTORY: Dict[str, List[Dict[str, str]]] = {}
USER_ACTIVITY: Dict[str, Dict[str, Any]] = {}

INACTIVITY_SECONDS = 30  # change to 30 for testing
INACTIVITY_CHECK_INTERVAL = 30
INACTIVITY_MESSAGE = (
    "Hey, are you still there? 😊\n\n"
    "If you still need help, just reply here and I’ll continue."
)


def load_knowledge_text() -> str:
    try:
        with open("knowledge.txt", "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
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
    ],
    "suspension": [
        "suspension",
        "suspend membership",
        "pause membership",
        "freeze membership",
        "stop membership",
        "travel suspension",
        "medical suspension",
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
    ],
    "refer_friend": [
        "refer friend",
        "refer a friend",
        "friend referral",
    ],
    "corporate": [
        "corporate",
        "partnership",
        "corporate partnership",
        "corporate collab",
    ],
    "staff_hub": [
        "staff hub",
        "staff booking",
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
    "please", "pls", "pls.",
    "ah", "lah", "leh", "anot",
    "tell", "say", "know",
}


def normalize(text: str) -> str:
    return " ".join((text or "").strip().lower().replace("’", "'").split())


def extract_important_words(text: str) -> List[str]:
    words = normalize(text).split()
    return [word for word in words if word not in STOP_WORDS]


def now_singapore_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Singapore")).isoformat()


def closing_message() -> str:
    now_hour = datetime.now(ZoneInfo("Asia/Singapore")).hour
    if 7 <= now_hour < 18:
        return (
            "Is there anything else we can assist you with today?\n\n"
            "If not, we’ll close this ticket in a moment. Wishing you a wonderful and mindful day ahead! 🙏"
        )
    return (
        "Is there anything else we can assist you with today?\n\n"
        "If not, we’ll close this ticket for now. Wishing you a restful and peaceful evening ahead! ✨"
    )


def save_request(kind: str, phone: str, payload: Dict[str, Any]) -> None:
    record = {
        "kind": kind,
        "phone": phone,
        "payload": payload,
        "created_at_sg": now_singapore_iso(),
    }
    with open("requests_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def reset_history(phone: str) -> None:
    CHAT_HISTORY.pop(phone, None)


def add_history(phone: str, role: str, content: str) -> None:
    if phone not in CHAT_HISTORY:
        CHAT_HISTORY[phone] = []
    CHAT_HISTORY[phone].append({"role": role, "content": content})
    CHAT_HISTORY[phone] = CHAT_HISTORY[phone][-14:]


def mark_user_active(phone: str) -> None:
    USER_ACTIVITY[phone] = {
        "last_seen": time.time(),
        "reminder_sent": False,
    }


def best_fuzzy_match(text: str, choices: List[str], cutoff: float = 0.72) -> Optional[str]:
    matches = get_close_matches(text.lower(), [c.lower() for c in choices], n=1, cutoff=cutoff)
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
    candidates: List[str] = [t]
    candidates.extend(important_words)

    best: Optional[str] = None
    best_score = 0.0

    for candidate in candidates:
        match = get_close_matches(candidate, [s.lower() for s in KNOWN_STUDIOS], n=1, cutoff=0.6)
        if match:
            matched = match[0]

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

    all_phrases: List[str] = []
    phrase_to_intent: Dict[str, str] = {}

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

    hints: List[str] = []

    if studio and studio_score >= 0.8:
        hints.append(f"Detected studio: {studio}")

    if intent and intent_score >= 0.75:
        hints.append(f"Detected intent: {intent}")

    if not hints:
        return original

    return original + "\n\nSYSTEM HINTS:\n" + "\n".join(f"- {h}" for h in hints)


def is_menu_request(text: str) -> bool:
    return normalize(text) in {
        "menu",
        "start",
        "home",
        "main menu",
        "restart",
        "hi",
        "hello",
        "hey",
    }


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
        "complaint",
        "refund",
        "payment",
        "account",
        "manual review",
        "billing issue",
        "billing",
        "login issue",
        "login problem",
    ]
    return any(k in t for k in keywords)


def strip_handoff_token(text: str) -> str:
    return text.replace("[HANDOFF]", "").strip()


def ask_llm(phone: str, user_text: str, history_user_text: Optional[str] = None) -> str:
    if not OPENAI_API_KEY:
        return (
            "I’m sorry — the AI answer service is not configured yet.\n"
            "Please type CUSTOMER SERVICE and our team will follow up."
        )

    history = CHAT_HISTORY.get(phone, [])
    history_text = "\n".join(
        f"{item['role'].upper()}: {item['content']}" for item in history
    )

    instructions = f"""
You are Jal Yoga Singapore's WhatsApp assistant.

Use ONLY the knowledge below.

Core behavior:
- Handle as much of the conversation as possible naturally.
- Ask one question at a time.
- Continue multi-step flows based on recent chat context.
- Keep replies concise, warm, and professional.
- Do not invent prices, schedules, trainers, promotions, phone numbers, or any facts not shown in the knowledge.
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
    add_history(phone, "user", history_user_text or user_text)
    add_history(phone, "assistant", clean_answer)

    return answer


def send_whatsapp_message(to: str, message: str) -> None:
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message},
    }

    print("SEND URL:", url)
    print("SEND TO:", to)
    print("SEND PAYLOAD:", json.dumps(payload, indent=2, ensure_ascii=False))

    response = requests.post(url, headers=headers, json=payload, timeout=30)

    print("SEND STATUS:", response.status_code)
    print("SEND RESPONSE:", response.text)

    response.raise_for_status()


def inactivity_monitor(test_mode: bool = False, reminder_queue=None) -> None:
    while True:
        time.sleep(INACTIVITY_CHECK_INTERVAL)
        now = time.time()

        for phone, state in list(USER_ACTIVITY.items()):
            last_seen = state.get("last_seen", now)
            reminder_sent = state.get("reminder_sent", False)

            if not reminder_sent and (now - last_seen) >= INACTIVITY_SECONDS:
                try:
                    if test_mode and reminder_queue is not None and phone == "LOCAL_TEST":
                        reminder_queue.put(INACTIVITY_MESSAGE)
                    else:
                        send_whatsapp_message(phone, INACTIVITY_MESSAGE)

                    USER_ACTIVITY[phone]["reminder_sent"] = True

                    save_request(
                        "inactive_followup_sent",
                        phone,
                        {
                            "message": INACTIVITY_MESSAGE,
                            "sent_at": now_singapore_iso(),
                            "test_mode": test_mode,
                        },
                    )
                except Exception as e:
                    save_request(
                        "inactive_followup_error",
                        phone,
                        {
                            "error": str(e),
                            "sent_at": now_singapore_iso(),
                            "test_mode": test_mode,
                        },
                    )


def start_inactivity_thread(test_mode: bool = False, reminder_queue=None) -> None:
    if getattr(app, "_inactivity_thread_started", False):
        return

    thread = threading.Thread(
        target=inactivity_monitor,
        kwargs={
            "test_mode": test_mode,
            "reminder_queue": reminder_queue,
        },
        daemon=True,
    )
    thread.start()
    app._inactivity_thread_started = True


def extract_incoming_message(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    try:
        entry = payload.get("entry", [])
        if not entry:
            return None, None

        changes = entry[0].get("changes", [])
        if not changes:
            return None, None

        value = changes[0].get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return None, None

        msg = messages[0]
        sender = msg.get("from")
        msg_type = msg.get("type")

        if msg_type == "text":
            return sender, {
                "kind": "text",
                "text": msg.get("text", {}).get("body", ""),
            }

        if msg_type == "interactive":
            interactive = msg.get("interactive", {})
            interactive_type = interactive.get("type")

            if interactive_type == "button_reply":
                button_reply = interactive.get("button_reply", {})
                return sender, {
                    "kind": "interactive",
                    "reply_type": "button_reply",
                    "reply_id": button_reply.get("id", ""),
                    "title": button_reply.get("title", ""),
                }

            if interactive_type == "list_reply":
                list_reply = interactive.get("list_reply", {})
                return sender, {
                    "kind": "interactive",
                    "reply_type": "list_reply",
                    "reply_id": list_reply.get("id", ""),
                    "title": list_reply.get("title", ""),
                }

        return sender, None

    except Exception:
        return None, None


def unpack_user_input(incoming: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if incoming is None:
        return None, None, None

    if incoming.get("kind") == "text":
        raw_text = incoming.get("text", "")
        return raw_text, normalize(raw_text), None

    if incoming.get("kind") == "interactive":
        reply_id = incoming.get("reply_id", "")
        title = incoming.get("title", "")
        raw_text = title or reply_id
        return raw_text, normalize(raw_text), reply_id

    return None, None, None


def process_message(phone: str, incoming: Optional[Dict[str, Any]]) -> str:
    raw_text, _, _ = unpack_user_input(incoming)

    if incoming is not None and phone:
        mark_user_active(phone)

    if incoming is None:
        return (
            "I can currently handle text messages only.\n"
            "Please type your message, or type CUSTOMER SERVICE for manual help."
        )

    clean_text = raw_text or ""
    enriched_text = enrich_user_text_for_llm(clean_text)

    if is_menu_request(clean_text):
        reset_history(phone)
        answer = ask_llm(
            phone,
            "Show the Jal Yoga main menu exactly as written in the knowledge.",
            history_user_text=clean_text,
        )
        return strip_handoff_token(answer)

    if is_handoff_request(clean_text):
        save_request(
            "customer_service_handoff",
            phone,
            {"user_message": clean_text},
        )
        reset_history(phone)
        return (
            "Please let us know how we can help you!\n\n"
            "Our Customer Service team will review your message and get back to you as soon as possible."
        )

    answer = ask_llm(
        phone,
        enriched_text,
        history_user_text=clean_text,
    )

    if "[HANDOFF]" in answer:
        clean_answer = strip_handoff_token(answer)
        save_request(
            "customer_service_handoff",
            phone,
            {
                "user_message": clean_text,
                "llm_answer": clean_answer,
            },
        )
        reset_history(phone)

        if clean_answer:
            return clean_answer + "\n\nOur Customer Service team will review your message."
        return "Our Customer Service team will review your message."

    return strip_handoff_token(answer) + "\n\nReply MENU to return to the main menu."


def build_bot_reply(phone: str, user_text: str) -> str:
    incoming = {
        "kind": "text",
        "text": user_text,
    }
    return process_message(phone, incoming)


@app.route("/", methods=["GET"])
def home():
    start_inactivity_thread()
    return render_template(
        "index.html",
        studios=STUDIOS,
        public_whatsapp_number=PUBLIC_WHATSAPP_NUMBER,
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "Jal Yoga app is running."})


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    start_inactivity_thread()

    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200

    return "Verification failed", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    start_inactivity_thread()

    payload = request.get_json(silent=True) or {}
    phone, incoming = extract_incoming_message(payload)

    if not phone:
        return jsonify({"status": "ignored"}), 200

    try:
        reply = process_message(phone, incoming)
        send_whatsapp_message(phone, reply)
    except Exception as e:
        error_message = (
            "I’m sorry — something went wrong on our side.\n"
            "Please type CUSTOMER SERVICE and our team will follow up."
        )
        try:
            send_whatsapp_message(phone, error_message)
        except Exception:
            pass

        save_request(
            "server_error",
            phone,
            {
                "incoming": incoming,
                "error": str(e),
            },
        )

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    start_inactivity_thread()
    app.run(host="0.0.0.0", port=PORT, debug=True)