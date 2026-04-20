import os
import re
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string, request
from openai import OpenAI

load_dotenv()

app = Flask(__name__)

# ----------------------------
# ENV
# ----------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4")

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v23.0")

BOT_NUMBER = os.getenv("BOT_NUMBER", "")

CS_NUMBERS = {
    "north": os.getenv("CS_NORTH", ""),
    "south": os.getenv("CS_SOUTH", ""),
    "east": os.getenv("CS_EAST", ""),
    "west": os.getenv("CS_WEST", ""),
    "centre": os.getenv("CS_CENTRE", ""),
}
print("OPENAI_API_KEY found:", bool(OPENAI_API_KEY))
print("OPENAI_API_KEY starts with:", OPENAI_API_KEY[:5] if OPENAI_API_KEY else "EMPTY")
client = OpenAI(api_key=OPENAI_API_KEY)


# ----------------------------
# FILE PATHS
# ----------------------------
KNOWLEDGE_PATH = Path("data/jal_yoga_faq.txt")

# ----------------------------
# MEMORY
# ----------------------------
SESSIONS = {}
MAX_HISTORY = 8

WELCOME_MESSAGE = """Namaste! Thank you for reaching out to Jal Yoga. 🙏

Please choose an option:
1. Schedule a Trial
2. I’m a current member
3. I’d like to find out more about Jal Yoga
4. Corporate / Partnerships
5. Staff Hub

You can also type: human
"""

AREA_PROMPT = """Please choose your area so I can send you the correct customer service contact:

1. North
2. South
3. East
4. West
5. Centre
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
                <h3>Book a Trial</h3>
                <p>Ask about trial classes and get guided step by step.</p>
            </div>
            <div class="card">
                <h3>Member Support</h3>
                <p>Get help with cancellation, suspension, and class booking issues.</p>
            </div>
            <div class="card">
                <h3>General Enquiry</h3>
                <p>Ask about studio locations, operating hours, class types, and policies.</p>
            </div>
            <div class="card">
                <h3>Talk to Human</h3>
                <p>If the bot cannot help, it will pass you to the correct area team.</p>
            </div>
        </div>
    </div>

    <a class="fab" href="{{ bot_link }}" target="_blank" title="Chat on WhatsApp">💬</a>
</body>
</html>
"""


def load_knowledge() -> str:
    if not KNOWLEDGE_PATH.exists():
        raise FileNotFoundError(
            f"Knowledge file not found: {KNOWLEDGE_PATH}. "
            f"Run convert_pdf.py first."
        )
    return KNOWLEDGE_PATH.read_text(encoding="utf-8").strip()


KNOWLEDGE_TEXT = load_knowledge()


def build_system_prompt() -> str:
    return f"""
You are Jal Yoga's WhatsApp assistant.

Use only the knowledge below.
Do not invent facts.
If the answer is not clearly inside the knowledge, reply with exactly:
HANDOFF

If the user asks for:
- a human
- agent
- customer service
- complaint
- refund
- difficult case
reply with exactly:
HANDOFF

Style rules:
- friendly
- simple English
- warm
- concise
- helpful
- keep replies under 120 words when possible

JAL YOGA KNOWLEDGE:
{KNOWLEDGE_TEXT}
""".strip()


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


def get_session(phone: str) -> dict:
    if phone not in SESSIONS:
        SESSIONS[phone] = {
            "history": [],
            "awaiting_area": False,
        }
    return SESSIONS[phone]


def reset_session(phone: str) -> None:
    SESSIONS[phone] = {
        "history": [],
        "awaiting_area": False,
    }


def add_history(phone: str, role: str, content: str) -> None:
    session = get_session(phone)
    session["history"].append({"role": role, "content": content})
    session["history"] = session["history"][-MAX_HISTORY:]


def build_chat_input(phone: str, user_message: str) -> str:
    session = get_session(phone)
    lines = []

    for item in session["history"]:
        role = item["role"].title()
        lines.append(f"{role}: {item['content']}")

    lines.append(f"User: {user_message}")
    return "\n".join(lines)


def is_human_request(text: str) -> bool:
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
        "someone call me",
        "talk to person",
        "talk to staff",
    ]
    return any(keyword in text for keyword in keywords)


def parse_area(text: str) -> str:
    text = text.strip().lower()
    mapping = {
        "1": "north",
        "2": "south",
        "3": "east",
        "4": "west",
        "5": "centre",
        "north": "north",
        "south": "south",
        "east": "east",
        "west": "west",
        "centre": "centre",
        "center": "centre",
    }
    return mapping.get(text, "")


def handoff_message(area: str) -> str:
    number = CS_NUMBERS.get(area, "")
    if not number:
        return "Sorry, I could not find that customer service number yet."

    link = wa_link(number, "Hello, I need help from Jal Yoga customer service.")
    return (
        f"I’m connecting you to our {area.title()} customer service team.\n\n"
        f"Contact Number: {display_number(number)}\n"
        f"WhatsApp Link: {link}\n\n"
        f"You can message them directly from the link above."
    )


def ask_openai(phone: str, user_message: str) -> str:
    conversation_text = build_chat_input(phone, user_message)

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=build_system_prompt(),
            input=conversation_text,
        )
        answer = (response.output_text or "").strip()
        return answer if answer else "HANDOFF"
    except Exception as exc:
        print("OpenAI error:", exc)
        return "HANDOFF"


def build_bot_reply(phone: str, user_message: str) -> str:
    clean_text = (user_message or "").strip()
    lower_text = clean_text.lower()

    if not clean_text:
        return "Please send a text message so I can help you."

    if lower_text in {"hi", "hello", "hey", "start", "menu", "reset"}:
        reset_session(phone)
        return WELCOME_MESSAGE

    session = get_session(phone)

    if session["awaiting_area"]:
        area = parse_area(clean_text)
        if not area:
            return AREA_PROMPT

        session["awaiting_area"] = False
        return handoff_message(area)

    if is_human_request(clean_text):
        session["awaiting_area"] = True
        return AREA_PROMPT

    add_history(phone, "user", clean_text)
    answer = ask_openai(phone, clean_text)

    if answer == "HANDOFF":
        session["awaiting_area"] = True
        return AREA_PROMPT

    add_history(phone, "assistant", answer)
    return answer


def send_whatsapp_text(to_number: str, body_text: str) -> dict:
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