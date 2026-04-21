import json
import os
import re
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string, request
from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=False)

app = Flask(__name__)

# ----------------------------
# ENV
# ----------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4").strip()

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip()
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "").strip()
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "").strip()
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v23.0").strip()

BOT_NUMBER = os.getenv("BOT_NUMBER", "").strip()

OUTLET_CONTACTS = {
    "alexandra": os.getenv("OUTLET_ALEXANDRA", "").strip(),
    "katong": os.getenv("OUTLET_KATONG", "").strip(),
    "kovan": os.getenv("OUTLET_KOVAN", "").strip(),
    "upper bukit timah": os.getenv("OUTLET_UPPER_BUKIT_TIMAH", "").strip(),
    "woodlands": os.getenv("OUTLET_WOODLANDS", "").strip(),
}

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY is missing. Check your .env file.")

client = OpenAI(api_key=OPENAI_API_KEY)

# ----------------------------
# FILES
# ----------------------------
KNOWLEDGE_PATH = BASE_DIR / "data" / "knowledge_base.json"

# ----------------------------
# MEMORY
# ----------------------------
SESSIONS = {}
MAX_HISTORY = 8
TOP_K = 4
MIN_SCORE = 4

WELCOME_MESSAGE = """Namaste! Thank you for reaching out to Jal Yoga. 🙏

Please choose an option:
1. Schedule a Trial
2. I’m a current member
3. I’d like to find out more about Jal Yoga
4. Corporate / Partnerships
5. Staff Hub

You can also type your question directly.
You can also type: human
"""

OUTLET_PROMPT = """I’m unable to find that clearly in the Jal Yoga PDF knowledge.

Please choose your preferred outlet and I’ll connect you there:

1. Alexandra
2. Katong
3. Kovan
4. Upper Bukit Timah
5. Woodlands
"""

HOME_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Jal Yoga Demo</title>
    <style>
        body {
            margin: 0;
            font-family: Arial, sans-serif;
            background: #f8f4f1;
            color: #222;
        }
        .wrap {
            max-width: 1000px;
            margin: 0 auto;
            padding: 60px 20px;
        }
        .hero {
            text-align: center;
            padding: 40px 20px;
        }
        h1 {
            font-size: 42px;
            margin-bottom: 12px;
        }
        p {
            font-size: 18px;
            line-height: 1.6;
        }
        .btn {
            display: inline-block;
            background: #25D366;
            color: white;
            text-decoration: none;
            padding: 14px 24px;
            border-radius: 999px;
            font-weight: bold;
            margin-top: 20px;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
            gap: 16px;
            margin-top: 30px;
        }
        .card {
            background: white;
            border-radius: 16px;
            padding: 20px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.08);
        }
        .fab {
            position: fixed;
            right: 20px;
            bottom: 20px;
            width: 64px;
            height: 64px;
            border-radius: 50%;
            background: #25D366;
            color: white;
            text-decoration: none;
            display: flex;
            justify-content: center;
            align-items: center;
            font-size: 30px;
            box-shadow: 0 10px 24px rgba(0,0,0,0.2);
        }
        .small {
            color: #666;
            font-size: 14px;
            margin-top: 10px;
        }
    </style>
</head>
<body>
    <div class="wrap">
        <div class="hero">
            <h1>Jal Yoga</h1>
            <p>
                Welcome to our demo website. Chat with our WhatsApp assistant to ask about
                trial classes, member support, studio information, policies, and more.
            </p>
            <a class="btn" href="{{ bot_link }}" target="_blank">Chat on WhatsApp</a>
            <div class="small">Main bot number: {{ bot_number }}</div>
        </div>

        <div class="grid">
            <div class="card">
                <h3>Schedule a Trial</h3>
                <p>Ask about trial booking and studio visit details.</p>
            </div>
            <div class="card">
                <h3>Current Member</h3>
                <p>Get help with cancellation, suspension, booking, and member support.</p>
            </div>
            <div class="card">
                <h3>Find Out More</h3>
                <p>Ask about class types, schedules, policies, locations, and operating hours.</p>
            </div>
            <div class="card">
                <h3>Corporate / Partnerships</h3>
                <p>Ask about wellness partnerships and corporate enquiries.</p>
            </div>
            <div class="card">
                <h3>Staff Hub</h3>
                <p>Get help with staff-related booking flow questions.</p>
            </div>
        </div>
    </div>

    <a class="fab" href="{{ bot_link }}" target="_blank" title="Chat on WhatsApp">💬</a>
</body>
</html>
"""


def load_knowledge():
    if not KNOWLEDGE_PATH.exists():
        raise FileNotFoundError(f"Missing knowledge file: {KNOWLEDGE_PATH}")
    with KNOWLEDGE_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


KNOWLEDGE = load_knowledge()
SECTION_DOCS = KNOWLEDGE["sections"]
MENU_CONTEXT = {item["id"]: item["label"] for item in KNOWLEDGE["menu"]}


def normalize_number(number: str) -> str:
    digits = re.sub(r"\D", "", str(number))
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def display_number(number: str) -> str:
    digits = normalize_number(number)
    return f"+{digits}" if digits else str(number)


def wa_link(number: str, text: str = "") -> str:
    digits = normalize_number(number)
    if text:
        return f"https://wa.me/{digits}?text={quote(text)}"
    return f"https://wa.me/{digits}"


def get_session(phone: str):
    if phone not in SESSIONS:
        SESSIONS[phone] = {
            "history": [],
            "current_menu": None,
            "awaiting_outlet_handoff": False,
        }
    return SESSIONS[phone]


def reset_session(phone: str):
    SESSIONS[phone] = {
        "history": [],
        "current_menu": None,
        "awaiting_outlet_handoff": False,
    }


def add_history(phone: str, role: str, content: str):
    session = get_session(phone)
    session["history"].append({"role": role, "content": content})
    session["history"] = session["history"][-MAX_HISTORY:]


def is_human_request(text: str):
    text = text.lower()
    keywords = [
        "human",
        "agent",
        "staff",
        "customer service",
        "real person",
        "complaint",
        "refund",
        "not helpful",
        "talk to person",
        "talk to staff",
        "talk to human",
    ]
    return any(keyword in text for keyword in keywords)


def parse_outlet(text: str):
    t = text.strip().lower()
    mapping = {
        "1": "alexandra",
        "2": "katong",
        "3": "kovan",
        "4": "upper bukit timah",
        "5": "woodlands",
        "alexandra": "alexandra",
        "katong": "katong",
        "kovan": "kovan",
        "upper bukit timah": "upper bukit timah",
        "bukit timah": "upper bukit timah",
        "woodlands": "woodlands",
    }
    return mapping.get(t, "")


def handoff_message(outlet: str):
    number = OUTLET_CONTACTS.get(outlet, "")
    if not number:
        return "Sorry, I could not find that outlet contact number yet."

    message = f"Hello, I need help from Jal Yoga {outlet.title()}."
    link = wa_link(number, message)

    return (
        f"I’m connecting you to our {outlet.title()} team.\n\n"
        f"Contact Number: {display_number(number)}\n"
        f"WhatsApp Link: {link}\n\n"
        f"You can message them directly from the link above."
    )


def handle_menu_selection(phone: str, text: str):
    clean = text.strip()
    if clean in MENU_CONTEXT:
        session = get_session(phone)
        session["current_menu"] = clean
        label = MENU_CONTEXT[clean]
        return f"Got it — you selected {label}. Please type your question."
    return None


def tokenize(text: str):
    words = re.findall(r"[a-zA-Z0-9$]+", text.lower())
    stopwords = {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "is", "are",
        "i", "you", "me", "my", "we", "our", "your", "it", "this", "that", "with",
        "what", "how", "can", "do", "does", "about", "please", "would", "like", "tell",
        "find", "out", "help", "need", "know", "more"
    }
    return [word for word in words if word not in stopwords]


def score_section(user_message: str, section: dict, current_menu: str | None):
    query = user_message.lower().strip()
    tokens = tokenize(query)
    haystack = f"{section['title']} {' '.join(section.get('keywords', []))} {section['content']}".lower()

    score = 0
    for token in tokens:
        if token in haystack:
            score += 3

    for keyword in section.get("keywords", []):
        if keyword.lower() in query:
            score += 5

    if current_menu and section.get("category") == current_menu:
        score += 10

    if current_menu == "1" and any(x in query for x in ["trial", "studio", "fitness goal", "full name"]):
        score += 5
    if current_menu == "2" and any(x in query for x in ["cancel", "suspension", "booking", "member", "refer"]):
        score += 5
    if current_menu == "3" and any(x in query for x in ["location", "hours", "class", "schedule", "policy", "event", "retreat"]):
        score += 5
    if current_menu == "4" and any(x in query for x in ["corporate", "partnership", "company", "collaboration"]):
        score += 5
    if current_menu == "5" and any(x in query for x in ["staff", "room", "member name", "date", "time"]):
        score += 5

    return score


def retrieve_sections(user_message: str, current_menu: str | None):
    scored = []
    for section in SECTION_DOCS:
        scored.append((score_section(user_message, section, current_menu), section))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [section for score, section in scored[:TOP_K] if score >= MIN_SCORE]
    best = scored[0][0] if scored else 0
    return top, best


def build_grounding_context(user_message: str, current_menu: str | None):
    sections, best_score = retrieve_sections(user_message, current_menu)
    if not sections:
        return "", 0

    parts = []
    for section in sections:
        parts.append(f"[{section['title']}]\n{section['content']}")

    return "\n\n---\n\n".join(parts), best_score


def build_system_prompt(grounding_context: str) -> str:
    menu_text = ", ".join(f"{k}. {v}" for k, v in MENU_CONTEXT.items())
    outlets = ", ".join(outlet["name"] for outlet in KNOWLEDGE["outlets"])

    return f"""
You are Jal Yoga's WhatsApp assistant.

Use ONLY the grounded Jal Yoga knowledge below.
Do not invent facts.
If the answer is not clearly supported by the grounded knowledge, reply with exactly:
HANDOFF

Important business rules:
- The main menu options are: {menu_text}
- The outlets mentioned in the PDF are: {outlets}
- If the user asks something unsupported by the grounded knowledge, do not guess.
- Keep replies warm, clear, and simple.
- Keep replies under 120 words when possible.

Grounded knowledge:
{grounding_context}
""".strip()


def ask_openai(phone: str, user_message: str):
    session = get_session(phone)
    current_menu = session.get("current_menu")
    grounding_context, best_score = build_grounding_context(user_message, current_menu)

    if best_score < MIN_SCORE or not grounding_context:
        return "HANDOFF"

    recent_history = session["history"][-6:]
    lines = []

    if current_menu:
        lines.append(f"The current user-selected menu is: {MENU_CONTEXT[current_menu]}")

    for item in recent_history:
        role = item["role"].title()
        lines.append(f"{role}: {item['content']}")

    lines.append(f"User: {user_message}")
    conversation_text = "\n".join(lines)

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=build_system_prompt(grounding_context),
            input=conversation_text,
        )
        answer = (response.output_text or "").strip()
        return answer if answer else "HANDOFF"
    except Exception as exc:
        print("OpenAI error:", exc)
        return "HANDOFF"


def build_bot_reply(phone: str, user_message: str):
    clean_text = (user_message or "").strip()
    lower_text = clean_text.lower()

    if not clean_text:
        return "Please send a text message so I can help you."

    if lower_text in {"hi", "hello", "hey", "start", "menu", "reset"}:
        reset_session(phone)
        return WELCOME_MESSAGE

    session = get_session(phone)

    if session["awaiting_outlet_handoff"]:
        outlet = parse_outlet(clean_text)
        if not outlet:
            return OUTLET_PROMPT

        session["awaiting_outlet_handoff"] = False
        return handoff_message(outlet)

    if is_human_request(clean_text):
        session["awaiting_outlet_handoff"] = True
        return OUTLET_PROMPT

    menu_reply = handle_menu_selection(phone, clean_text)
    if menu_reply:
        return menu_reply

    add_history(phone, "user", clean_text)
    answer = ask_openai(phone, clean_text)

    if answer == "HANDOFF":
        session["awaiting_outlet_handoff"] = True
        return OUTLET_PROMPT

    add_history(phone, "assistant", answer)
    return answer


def send_whatsapp_text(to_number: str, body_text: str):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": body_text},
    }

    response = requests.post(url, headers=headers, json=payload, timeout=30)

    print("Meta response status:", response.status_code)
    print("Meta response body:", response.text)

    try:
        return response.json()
    except Exception:
        return {"status_code": response.status_code, "text": response.text}


def extract_incoming_text(payload: dict):
    try:
        entries = payload.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])

                if not messages:
                    continue

                for message in messages:
                    sender = message.get("from")
                    msg_type = message.get("type")

                    if msg_type == "text":
                        return sender, message["text"]["body"]

                    if msg_type == "interactive":
                        interactive = message.get("interactive", {})
                        interactive_type = interactive.get("type")

                        if interactive_type == "button_reply":
                            return sender, interactive["button_reply"]["title"]

                        if interactive_type == "list_reply":
                            return sender, interactive["list_reply"]["title"]

        return None, None

    except Exception as exc:
        print("Webhook parse error:", exc)
        return None, None


@app.route("/")
def home():
    link = wa_link(BOT_NUMBER, "Hi Jal Yoga")
    return render_template_string(
        HOME_HTML,
        bot_link=link,
        bot_number=display_number(BOT_NUMBER),
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/ask", methods=["POST"])
def ask():
    data = request.get_json(silent=True) or {}
    phone = data.get("phone", "LOCAL_TEST")
    message = data.get("message", "")

    reply = build_bot_reply(phone, message)
    return jsonify({"reply": reply}), 200


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200

    return "Verification failed", 403


@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():
    payload = request.get_json(silent=True) or {}

    sender, incoming_text = extract_incoming_text(payload)
    if not sender:
        return jsonify({"status": "ignored"}), 200

    print("Incoming sender:", sender)
    print("Incoming text:", incoming_text)

    reply = build_bot_reply(sender, incoming_text)
    print("Bot reply:", reply)

    result = send_whatsapp_text(sender, reply)
    print("WhatsApp send result:", result)

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)