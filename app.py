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
MESSAGE_SEND_LOG: Dict[str, List[float]] = {}

# WhatsApp / Meta safety settings.
# Keep follow-ups low-volume and only inside the 24-hour customer-service window.
CUSTOMER_SERVICE_WINDOW_SECONDS = 24 * 60 * 60
INACTIVITY_ENABLED = os.getenv("INACTIVITY_ENABLED", "true").lower() == "true"
INACTIVITY_SECONDS = int(os.getenv("INACTIVITY_SECONDS", "600"))  # 10 minutes by default
INACTIVITY_CHECK_INTERVAL = int(os.getenv("INACTIVITY_CHECK_INTERVAL", "60"))
OUTBOUND_RATE_LIMIT_COUNT = int(os.getenv("OUTBOUND_RATE_LIMIT_COUNT", "8"))
OUTBOUND_RATE_LIMIT_WINDOW = int(os.getenv("OUTBOUND_RATE_LIMIT_WINDOW", "60"))
LOG_FULL_MESSAGES = os.getenv("LOG_FULL_MESSAGES", "false").lower() == "true"
OPT_OUT_FILE = os.getenv("OPT_OUT_FILE", "opt_out_users.json")

INACTIVITY_MESSAGE = (
    "Hi, do you still need help with your Jal Yoga enquiry? 😊\n\n"
    "If yes, just reply here and I’ll continue. Reply STOP anytime to stop receiving follow-up messages."
)

OPT_OUT_CONFIRMATION = (
    "Noted — you have been unsubscribed and will not receive follow-up messages. "
    "If you need help later, reply START."
)

OPT_IN_CONFIRMATION = (
    "Welcome back — you are subscribed again. Type MENU to see Jal Yoga options."
)

OPT_OUT_WORDS = {
    "stop", "unsubscribe", "opt out", "opt-out", "remove me", "no more messages",
    "do not message me", "dont message me", "don't message me", "cancel messages"
}

OPT_IN_WORDS = {"start", "subscribe", "opt in", "opt-in"}

SENSITIVE_KEYWORDS = [
    "nric", "ic number", "passport number", "credit card", "debit card",
    "card number", "cvv", "otp", "one time password", "password",
    "bank account", "bank number"
]

JAL_YOGA_GENERAL_KEYWORDS = {
    "jal", "yoga", "pilates", "barre", "trial", "class", "classes", "schedule",
    "studio", "studios", "outlet", "outlets", "location", "address", "membership",
    "member", "package", "price", "pricing", "booking", "book", "cancel", "cancellation",
    "suspend", "suspension", "pause", "freeze", "refer", "friend", "corporate",
    "partnership", "staff", "hours", "open", "close", "trainer", "customer service"
}


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


def load_knowledge_text() -> str:
    # Render should use knowledge.txt. The fallback helps when testing with uploaded files.
    for filename in ("knowledge.txt", "knowledge(1).txt"):
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


def save_opt_out_users() -> None:
    with open(OPT_OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(OPT_OUT_USERS), f, ensure_ascii=False, indent=2)


def set_user_opt_out(phone: str, opted_out: bool) -> None:
    if not phone:
        return
    if opted_out:
        OPT_OUT_USERS.add(phone)
    else:
        OPT_OUT_USERS.discard(phone)
    save_opt_out_users()


def is_user_opted_out(phone: str) -> bool:
    return bool(phone and phone in OPT_OUT_USERS)


def is_opt_out_request(text: str) -> bool:
    t = normalize(text)

    # Important: "stop membership" should mean membership suspension, not WhatsApp opt-out.
    if t in OPT_OUT_WORDS:
        return True

    long_phrases = [word for word in OPT_OUT_WORDS if " " in word or "-" in word]
    return any(phrase in t for phrase in long_phrases)


def is_opt_in_request(text: str) -> bool:
    t = normalize(text)
    return t in OPT_IN_WORDS


def contains_sensitive_keyword(text: str) -> bool:
    t = normalize(text)
    return any(keyword in t for keyword in SENSITIVE_KEYWORDS)


def is_jal_yoga_related(phone: str, text: str) -> bool:
    # During an active flow, short replies such as names, goals, PROCEED, or studio choices are allowed.
    if CHAT_HISTORY.get(phone):
        return True

    t = normalize(text)
    if not t:
        return True

    if is_menu_request(t) or is_handoff_request(t):
        return True

    if t in {"yes", "no", "ok", "okay", "sure", "proceed", "skip", "thanks", "thank you"}:
        return True

    studio, studio_score = detect_studio_from_text(t)
    if studio and studio_score >= 0.72:
        return True

    intent, intent_score = detect_intent_from_text(t)
    if intent and intent_score >= 0.72:
        return True

    words = set(t.split())
    return bool(words & JAL_YOGA_GENERAL_KEYWORDS)


def mask_phone(phone: str) -> str:
    if not phone or len(phone) <= 4:
        return "****"
    return "*" * max(len(phone) - 4, 0) + phone[-4:]


def is_inside_customer_service_window(phone: str) -> bool:
    if phone == "LOCAL_TEST":
        return True
    state = USER_ACTIVITY.get(phone, {})
    last_user_message_time = state.get("last_user_message_time")
    if not last_user_message_time:
        return False
    return (time.time() - float(last_user_message_time)) <= CUSTOMER_SERVICE_WINDOW_SECONDS


def is_rate_limited(phone: str) -> bool:
    now = time.time()
    timestamps = MESSAGE_SEND_LOG.setdefault(phone, [])
    MESSAGE_SEND_LOG[phone] = [
        ts for ts in timestamps if now - ts <= OUTBOUND_RATE_LIMIT_WINDOW
    ]

    if len(MESSAGE_SEND_LOG[phone]) >= OUTBOUND_RATE_LIMIT_COUNT:
        return True

    MESSAGE_SEND_LOG[phone].append(now)
    return False


def can_send_free_text(phone: str) -> Tuple[bool, str]:
    if not phone:
        return False, "Missing phone number."

    if is_user_opted_out(phone):
        return False, "User opted out."

    if not is_inside_customer_service_window(phone):
        return False, "Outside the 24-hour customer-service window. Use an approved Message Template instead."

    if is_rate_limited(phone):
        return False, "Outbound rate limit reached."

    return True, "OK"


def is_compliance_confirmation(message: str) -> bool:
    t = normalize(message)
    return "unsubscribed" in t or "subscribed again" in t


def reset_history(phone: str) -> None:
    CHAT_HISTORY.pop(phone, None)


def add_history(phone: str, role: str, content: str) -> None:
    if phone not in CHAT_HISTORY:
        CHAT_HISTORY[phone] = []
    CHAT_HISTORY[phone].append({"role": role, "content": content})
    CHAT_HISTORY[phone] = CHAT_HISTORY[phone][-14:]


def mark_user_active(phone: str) -> None:
    if not phone:
        return

    now = time.time()
    USER_ACTIVITY[phone] = {
        "last_seen": now,
        "last_user_message_time": now,
        "last_seen_sg": now_singapore_iso(),
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


def send_whatsapp_message(to: str, message: str, force: bool = False) -> bool:
    if not message or not str(message).strip():
        return False

    if not force:
        ok, reason = can_send_free_text(to)
        if not ok:
            save_request(
                "message_blocked_by_compliance",
                to,
                {
                    "reason": reason,
                    "message_preview": str(message)[:120],
                },
            )
            print("MESSAGE BLOCKED:", reason, "TO:", mask_phone(to))
            return False

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
    print("SEND TO:", mask_phone(to))
    if LOG_FULL_MESSAGES:
        print("SEND PAYLOAD:", json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("SEND MESSAGE PREVIEW:", str(message)[:120])

    response = requests.post(url, headers=headers, json=payload, timeout=30)

    print("SEND STATUS:", response.status_code)
    print("SEND RESPONSE:", response.text)

    response.raise_for_status()
    return True


def inactivity_monitor(test_mode: bool = False, reminder_queue=None) -> None:
    while True:
        time.sleep(INACTIVITY_CHECK_INTERVAL)
        now = time.time()

        for phone, state in list(USER_ACTIVITY.items()):
            last_seen = state.get("last_seen", now)
            reminder_sent = state.get("reminder_sent", False)

            if reminder_sent or (now - last_seen) < INACTIVITY_SECONDS:
                continue

            if is_user_opted_out(phone):
                save_request(
                    "inactive_followup_skipped",
                    phone,
                    {"reason": "User opted out.", "sent_at": now_singapore_iso(), "test_mode": test_mode},
                )
                USER_ACTIVITY[phone]["reminder_sent"] = True
                continue

            if not is_inside_customer_service_window(phone):
                save_request(
                    "inactive_followup_skipped",
                    phone,
                    {
                        "reason": "Outside 24-hour customer-service window. Use approved template instead.",
                        "sent_at": now_singapore_iso(),
                        "test_mode": test_mode,
                    },
                )
                USER_ACTIVITY[phone]["reminder_sent"] = True
                continue

            try:
                if test_mode and reminder_queue is not None and phone == "LOCAL_TEST":
                    reminder_queue.put(INACTIVITY_MESSAGE)
                    sent = True
                else:
                    sent = send_whatsapp_message(phone, INACTIVITY_MESSAGE)

                USER_ACTIVITY[phone]["reminder_sent"] = True

                save_request(
                    "inactive_followup_sent" if sent else "inactive_followup_not_sent",
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
    if not INACTIVITY_ENABLED and not test_mode:
        return

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
            "Please type your message, or type CUSTOMER SERVICE for manual help.\n\n"
            "Reply STOP anytime to stop receiving follow-up messages."
        )

    clean_text = raw_text or ""

    if is_opt_out_request(clean_text):
        set_user_opt_out(phone, True)
        reset_history(phone)
        save_request("user_opted_out", phone, {"user_message": clean_text})
        return OPT_OUT_CONFIRMATION

    if is_opt_in_request(clean_text) and is_user_opted_out(phone):
        set_user_opt_out(phone, False)
        save_request("user_opted_in", phone, {"user_message": clean_text})
        return OPT_IN_CONFIRMATION

    if is_user_opted_out(phone):
        return (
            "You have opted out of follow-up messages. "
            "If you want to chat with Jal Yoga again, please reply START."
        )

    if contains_sensitive_keyword(clean_text):
        save_request("sensitive_info_blocked", phone, {"user_message_preview": clean_text[:120]})
        return (
            "For your safety, please do not share NRIC, passport numbers, card numbers, CVV, OTP, "
            "passwords, or bank details here.\n\n"
            "For account-specific or payment-related help, please type CUSTOMER SERVICE."
        )

    if not is_jal_yoga_related(phone, clean_text):
        return (
            "I can help with Jal Yoga enquiries such as trial classes, schedules, outlets, prices, "
            "memberships, bookings, and customer support.\n\n"
            "Type MENU to see the options, or CUSTOMER SERVICE to speak to our team.\n\n"
            "Reply STOP anytime to stop receiving follow-up messages."
        )

    enriched_text = enrich_user_text_for_llm(clean_text)

    if is_menu_request(clean_text):
        reset_history(phone)
        answer = ask_llm(
            phone,
            "Show the Jal Yoga main menu exactly as written in the knowledge. Add this line at the end: Reply STOP anytime to stop receiving follow-up messages.",
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
            "Simply type your enquiry below. While our response may not be immediate, "
            "our Customer Service team will review your message and get back to you as soon as possible.\n\n"
            "Reply STOP anytime to stop receiving follow-up messages."
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
            return clean_answer + "\n\nOur Customer Service team will review your message.\n\nReply STOP anytime to stop receiving follow-up messages."
        return "Our Customer Service team will review your message.\n\nReply STOP anytime to stop receiving follow-up messages."

    return strip_handoff_token(answer) + "\n\nReply MENU to return to the main menu. Reply STOP anytime to stop receiving follow-up messages."


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
        if reply:
            send_whatsapp_message(
                phone,
                reply,
                force=is_compliance_confirmation(reply),
            )
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
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    )