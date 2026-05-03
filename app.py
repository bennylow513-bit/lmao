import json
import os
import re
import threading
import time
import traceback
from datetime import datetime
from difflib import SequenceMatcher
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

PORT = int(os.getenv("PORT", "5000"))

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


# =========================
# MEMORY
# =========================

CHAT_HISTORY: Dict[str, List[Dict[str, str]]] = {}

# Stores handoff summaries while waiting for the user to choose outlet
PENDING_HANDOFFS: Dict[str, Dict[str, str]] = {}

# Stores inactivity timer state for each Telegram chat
INACTIVITY_STATE: Dict[str, Dict[str, object]] = {}

# =========================
# INACTIVITY TIMING
# =========================
# 10 minutes = 600 seconds
# 20 minutes total = reminder after 10 min, close after another 10 min

INACTIVITY_WARNING_SECONDS = 600
INACTIVITY_CLOSE_SECONDS = 1200
INACTIVITY_CHECK_SECONDS = 30

INACTIVITY_THREAD_STARTED = False

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
    """
    Reads studio names and addresses from section 2. STUDIOS in knowledge.txt.

    Expected format:
    - Katong: 131 E Coast Rd, #03-01, Singapore 428816
    """
    studios: List[Dict[str, str]] = []
    inside_studio_section = False

    for line in text.splitlines():
        clean_line = line.strip()

        if clean_line.upper().startswith("2. STUDIOS"):
            inside_studio_section = True
            continue

        if inside_studio_section and clean_line.startswith("===") and studios:
            break

        if not inside_studio_section:
            continue

        if not clean_line.startswith("- "):
            continue

        item = clean_line[2:].strip()

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

# Backup only if knowledge.txt cannot be read properly
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


def simple_text(text: str) -> str:
    text = normalize(text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


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


def mark_chat_active(chat_id: str) -> None:
    """
    Called whenever the user sends a message.
    This resets the inactivity timer.
    """
    INACTIVITY_STATE[chat_id] = {
        "last_user_at": time.time(),
        "warning_sent": False,
        "closed": False,
    }

    print(
        f"INACTIVITY TIMER RESET for chat_id={chat_id}. "
        f"Active chats={len(INACTIVITY_STATE)}",
        flush=True,
    )


def clear_inactivity_state(chat_id: str) -> None:
    INACTIVITY_STATE.pop(chat_id, None)

    print(
        f"INACTIVITY STATE CLEARED for chat_id={chat_id}. "
        f"Active chats={len(INACTIVITY_STATE)}",
        flush=True,
    )


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


# =========================
# STUDIO / OUTLET HELPERS
# =========================

def studio_names() -> List[str]:
    return [studio["name"] for studio in STUDIOS]


def studio_options_text(include_not_specified: bool = False) -> str:
    options = [f"- {name}" for name in studio_names()]

    if include_not_specified:
        options.append("- Not specified")

    return "\n".join(options)


def studio_aliases(studio_name: str) -> List[str]:
    clean_name = simple_text(studio_name)
    words = clean_name.split()

    aliases = {clean_name}

    if len(words) > 1:
        initials = "".join(word[0] for word in words if word)
        aliases.add(initials)

    for word in words:
        if len(word) >= 4:
            aliases.add(word)

    return list(aliases)


def detect_outlet_from_text(text: str) -> str:
    """
    Less hardcoded outlet detection.
    Uses studio names from knowledge.txt and fuzzy matching for typos.
    """
    clean = simple_text(text)

    if not clean:
        return ""

    padded_clean = f" {clean} "

    for studio_name in studio_names():
        for alias in studio_aliases(studio_name):
            if f" {alias} " in padded_clean:
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


def env_key_for_outlet(outlet_name: str) -> str:
    """
    Example:
    Upper Bukit Timah -> UPPER_BUKIT_TIMAH_WHATSAPP_NUMBER
    """
    key = re.sub(r"[^A-Za-z0-9]+", "_", outlet_name.upper()).strip("_")
    return f"{key}_WHATSAPP_NUMBER"


def outlet_whatsapp_number(outlet_name: str) -> str:
    return os.getenv(env_key_for_outlet(outlet_name), "")


def env_key_for_outlet_telegram_chat(outlet_name: str) -> str:
    """
    Example:
    Upper Bukit Timah -> UPPER_BUKIT_TIMAH_TELEGRAM_CHAT_ID
    Katong -> KATONG_TELEGRAM_CHAT_ID
    """
    key = re.sub(r"[^A-Za-z0-9]+", "_", outlet_name.upper()).strip("_")
    return f"{key}_TELEGRAM_CHAT_ID"


def outlet_telegram_chat_id(outlet_name: str) -> str:
    return os.getenv(env_key_for_outlet_telegram_chat(outlet_name), "")


def outlet_whatsapp_numbers() -> Dict[str, str]:
    return {
        studio["name"]: outlet_whatsapp_number(studio["name"])
        for studio in STUDIOS
    }


def get_studio_address(outlet_name: str) -> str:
    for studio in STUDIOS:
        if studio["name"].lower() == outlet_name.lower():
            return studio["address"]

    return ""


# =========================
# WHATSAPP LINK HELPERS
# =========================

def customer_service_link() -> str:
    number = clean_number(CUSTOMER_SERVICE_WHATSAPP_NUMBER)

    if not number or number.upper() == "TBC":
        return ""

    return f"https://wa.me/{number}"


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
        selected_number = outlet_whatsapp_number(detected_outlet)

    if not selected_number:
        selected_number = CUSTOMER_SERVICE_WHATSAPP_NUMBER

    prefilled_message = (
        "Hello Jal Yoga Customer Service,\n\n"
        "I need help with this enquiry:\n\n"
        f"{summary_text}\n\n"
        "Thank you."
    )

    return build_prefilled_whatsapp_link(selected_number, prefilled_message)


def build_outlet_contact_reply(outlet: str) -> str:
    if not outlet or outlet == "Not specified":
        return ""

    number = outlet_whatsapp_number(outlet)

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
    outlet_lines = []

    for studio in STUDIOS:
        name = studio["name"]
        number = outlet_whatsapp_number(name) or "TBC"
        telegram_chat_id = outlet_telegram_chat_id(name) or "TBC"
        outlet_lines.append(f"- {name}: WhatsApp={number}, Telegram Chat ID={telegram_chat_id}")

    outlet_text = "\n".join(outlet_lines)

    return f"""
LIVE CUSTOMER SERVICE CONFIG FROM RENDER

Main Customer Service WhatsApp:
- {CUSTOMER_SERVICE_WHATSAPP_NUMBER or "TBC"}

Outlet Contacts:
{outlet_text}

Rules:
- You may use these numbers only if they are not TBC.
- If an outlet number is TBC, do not invent it.
- If main Customer Service is available, use the main Customer Service link for handoff.
- For trial bookings, the app may send the summary to the outlet Telegram group if the outlet Telegram chat ID is configured.
"""


# =========================
# LLM ROUTER
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
    """
    Small LLM router.
    Purpose:
    - Detect outlet contact requests
    - Detect outlet answer while pending Customer Service handoff
    - Reduce hardcoded keyword lists
    """
    outlet_guess = detect_outlet_from_text(user_text)

    default_result = {
        "intent": "normal",
        "outlet": outlet_guess,
        "no_specific_outlet": False,
        "confidence": "low",
    }

    if not client:
        return default_result

    history = CHAT_HISTORY.get(chat_id, [])

    history_text = "\n".join(
        f"{item['role'].upper()}: {item['content']}" for item in history[-6:]
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

Understand typos and casual Singapore phrasing.

Examples:
- "katong contact" -> {{"intent":"outlet_contact","outlet":"Katong","no_specific_outlet":false,"confidence":"high"}}
- "koven number" -> {{"intent":"outlet_contact","outlet":"Kovan","no_specific_outlet":false,"confidence":"high"}}
- "ubt whatsapp" -> {{"intent":"outlet_contact","outlet":"Upper Bukit Timah","no_specific_outlet":false,"confidence":"high"}}
- "no specific outlet" -> {{"intent":"handoff_outlet_answer","outlet":"Not specified","no_specific_outlet":true,"confidence":"high"}}
- "any outlet also can" -> {{"intent":"handoff_outlet_answer","outlet":"Not specified","no_specific_outlet":true,"confidence":"high"}}
"""

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            reasoning={"effort": "low"},
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
    no_specific_outlet = bool(data.get("no_specific_outlet", False))
    confidence = data.get("confidence", "low")

    allowed_outlets = studio_names() + ["Not specified", ""]

    if outlet not in allowed_outlets:
        outlet = outlet_guess

    if intent not in {"outlet_contact", "handoff_outlet_answer", "normal"}:
        intent = "normal"

    if confidence not in {"high", "medium", "low"}:
        confidence = "low"

    return {
        "intent": intent,
        "outlet": outlet,
        "no_specific_outlet": no_specific_outlet,
        "confidence": confidence,
    }


# =========================
# LLM BRAIN
# =========================

def ask_llm(chat_id: str, user_text: str) -> str:
    if not client:
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

Important:
- If [HANDOFF] is used and the outlet is unknown, write "- Outlet: Not specified".
- The app will ask the user for a specific outlet before sending the WhatsApp Customer Service link.
- Do not repeat the same handoff message twice.

Trial flow:
- If user asks about trial, free trial, trial lesson, trail lesson, triel, beginner trial, or got trial anot, start trial flow.
- Ask one question at a time:
  1. Preferred studio
  2. Full Name
  3. Fitness Goal
  4. Show summary
- Studio options:
{studio_options_text()}
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

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            reasoning={"effort": "low"},
            instructions=instructions,
            input=user_text,
        )

        answer = (response.output_text or "").strip()

    except Exception as e:
        print("OPENAI ERROR:", str(e), flush=True)
        traceback.print_exc()

        answer = (
            "I’m sorry — something went wrong while checking the information.\n"
            "[HANDOFF]"
        )

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
        clear_inactivity_state(chat_id)

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
        mark_chat_active(chat_id)

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

    # Reset the inactivity timer whenever the user sends a valid message
    mark_chat_active(chat_id)

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

        route = route_message_with_llm(
            chat_id,
            clean_text,
            mode="handoff_outlet_answer",
        )

        selected_outlet = route.get("outlet", "")

        if selected_outlet == "Not specified":
            selected_outlet = ""

        if not selected_outlet and not route.get("no_specific_outlet", False):
            PENDING_HANDOFFS[chat_id] = pending

            return (
                "Sorry, which outlet is this about?\n\n"
                "Please reply with one of these:\n"
                f"{studio_options_text(include_not_specified=True)}"
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
    # LLM ROUTING BEFORE MAIN ANSWER
    # =========================

    route = route_message_with_llm(chat_id, clean_text)

    # Outlet contact request, e.g. "katong contact"
    if route.get("intent") == "outlet_contact":
        outlet = route.get("outlet", "")

        if outlet and outlet != "Not specified":
            outlet_contact_reply = build_outlet_contact_reply(outlet)

            if outlet_contact_reply:
                return outlet_contact_reply + "\n\nReply MENU to return to the main menu."

        return (
            "Which outlet contact would you like?\n\n"
            f"{studio_options_text(include_not_specified=False)}\n\n"
            "Reply MENU to return to the main menu."
        )

    # =========================
    # ASK MAIN LLM
    # =========================

    answer = ask_llm(chat_id, clean_text)

    if "[HANDOFF]" in answer:
        clean_answer = strip_handoff_token(answer).strip()
        detected_outlet = detect_outlet_from_text(clean_text + "\n" + clean_answer)

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
                f"{studio_options_text(include_not_specified=True)}"
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

    final_reply = strip_handoff_token(answer).strip()

    # Send trial booking summary to the correct outlet Telegram group if this is a trial booking
    send_trial_booking_to_outlet(chat_id, final_reply)

    return final_reply + "\n\nReply MENU to return to the main menu."


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
        print("Missing TELEGRAM_BOT_TOKEN", flush=True)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    for chunk in split_long_message(message):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }

        response = requests.post(url, json=payload, timeout=30)

        print("TELEGRAM SEND STATUS:", response.status_code, flush=True)
        print("TELEGRAM SEND RESPONSE:", response.text, flush=True)

        response.raise_for_status()

    return True


# =========================
# SEND TRIAL BOOKING TO OUTLET GROUP
# =========================

def send_trial_booking_to_outlet(customer_chat_id: str, customer_reply: str) -> None:
    """
    If the bot reply contains a Trial Booking Summary,
    send it to the correct outlet Telegram group.
    """

    if "Trial Booking Summary:" not in customer_reply:
        return

    outlet = ""
    name = ""
    fitness_goal = ""

    for line in customer_reply.splitlines():
        clean_line = line.strip()

        if clean_line.lower().startswith("- outlet:"):
            outlet = clean_line.split(":", 1)[1].strip()

        elif clean_line.lower().startswith("- name:"):
            name = clean_line.split(":", 1)[1].strip()

        elif clean_line.lower().startswith("- fitness goal:"):
            fitness_goal = clean_line.split(":", 1)[1].strip()

    if not outlet:
        outlet = detect_outlet_from_text(customer_reply)

    if not outlet:
        print("TRIAL BOOKING SEND SKIPPED: No outlet detected", flush=True)
        return

    outlet_chat_id = outlet_telegram_chat_id(outlet)

    if not outlet_chat_id:
        print(
            f"TRIAL BOOKING SEND SKIPPED: Missing Telegram chat ID for outlet={outlet}",
            flush=True,
        )
        return

    outlet_message = (
        "New Trial Booking Received 🙏\n\n"
        f"Outlet: {outlet}\n"
        "Class: Trial Class\n"
        f"Name: {name or 'Not provided'}\n"
        f"Fitness Goal: {fitness_goal or 'Not provided'}\n\n"
        f"Customer Telegram Chat ID: {customer_chat_id}\n\n"
        "Please contact the customer within 24 hours."
    )

    try:
        send_telegram_message(outlet_chat_id, outlet_message)

        save_request(
            "trial_booking_sent_to_outlet",
            customer_chat_id,
            {
                "outlet": outlet,
                "outlet_chat_id": outlet_chat_id,
                "name": name,
                "fitness_goal": fitness_goal,
            },
        )

        print(
            f"TRIAL BOOKING SENT to outlet={outlet}, chat_id={outlet_chat_id}",
            flush=True,
        )

    except Exception as e:
        print(
            f"TRIAL BOOKING SEND ERROR for outlet={outlet}: {str(e)}",
            flush=True,
        )
        traceback.print_exc()


# =========================
# INACTIVITY CHECKER
# =========================

def inactivity_checker_loop() -> None:
    """
    Background loop for project/demo inactivity follow-up.

    Behaviour:
    - After no reply for INACTIVITY_WARNING_SECONDS, send reminder.
    - After no reply for INACTIVITY_CLOSE_SECONDS total, close chat.
    """
    while True:
        time.sleep(INACTIVITY_CHECK_SECONDS)

        now = time.time()

        print(
            f"INACTIVITY CHECK RUNNING | active_chats={len(INACTIVITY_STATE)}",
            flush=True,
        )

        for chat_id, state in list(INACTIVITY_STATE.items()):
            try:
                if chat_id in OPT_OUT_USERS:
                    clear_inactivity_state(chat_id)
                    continue

                last_user_at = float(state.get("last_user_at", now))
                warning_sent = bool(state.get("warning_sent", False))
                closed = bool(state.get("closed", False))

                if closed:
                    clear_inactivity_state(chat_id)
                    continue

                idle_seconds = now - last_user_at

                print(
                    f"CHECK chat_id={chat_id} | idle={int(idle_seconds)}s "
                    f"| warning_sent={warning_sent}",
                    flush=True,
                )

                # 1. Send reminder after the warning time
                if not warning_sent and idle_seconds >= INACTIVITY_WARNING_SECONDS:
                    send_telegram_message(
                        chat_id,
                        "Just checking in — do you still need help? "
                        "Reply here to continue, or type STOP to stop receiving follow-up messages.",
                    )

                    state["warning_sent"] = True

                    save_request(
                        "inactivity_warning_sent",
                        chat_id,
                        {
                            "idle_seconds": int(idle_seconds),
                        },
                    )

                    print(
                        f"INACTIVITY WARNING SENT to chat_id={chat_id}",
                        flush=True,
                    )

                # 2. Auto-close after the close time
                elif warning_sent and idle_seconds >= INACTIVITY_CLOSE_SECONDS:
                    send_telegram_message(
                        chat_id,
                        "We’ll close this chat for now. "
                        "If you need help again, reply START or MENU anytime. 🙏",
                    )

                    reset_history(chat_id)
                    PENDING_HANDOFFS.pop(chat_id, None)

                    state["closed"] = True

                    save_request(
                        "chat_auto_closed",
                        chat_id,
                        {
                            "idle_seconds": int(idle_seconds),
                        },
                    )

                    print(
                        f"CHAT AUTO CLOSED for chat_id={chat_id}",
                        flush=True,
                    )

                    clear_inactivity_state(chat_id)

            except Exception as e:
                print("INACTIVITY CHECK ERROR:", str(e), flush=True)
                traceback.print_exc()


def start_inactivity_checker() -> None:
    global INACTIVITY_THREAD_STARTED

    if INACTIVITY_THREAD_STARTED:
        return

    INACTIVITY_THREAD_STARTED = True

    print(
        "INACTIVITY CHECKER STARTED "
        f"| warning={INACTIVITY_WARNING_SECONDS}s "
        f"| close={INACTIVITY_CLOSE_SECONDS}s "
        f"| check_every={INACTIVITY_CHECK_SECONDS}s",
        flush=True,
    )

    thread = threading.Thread(
        target=inactivity_checker_loop,
        daemon=True,
    )

    thread.start()


# =========================
# START BACKGROUND CHECKER SAFELY
# =========================

@app.before_request
def start_background_tasks():
    start_inactivity_checker()


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
        outlet_data[name] = {
            "address": studio["address"],
            "telegram_chat_id_configured": bool(outlet_telegram_chat_id(name)),
            "telegram_chat_id_last_4": outlet_telegram_chat_id(name)[-4:]
            if outlet_telegram_chat_id(name)
            else "",
            "env_key": env_key_for_outlet_telegram_chat(name),
        }

    return jsonify(
        {
            "status": "ok",
            "outlets": outlet_data,
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
        incoming_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            "",
        )

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

    user_text = message.get("text", "")

    print(
        f"INCOMING TELEGRAM UPDATE | chat_id={chat_id} | chat_type={chat_type} | text={user_text}",
        flush=True,
    )

    # Important:
    # Outlet groups are for receiving booking summaries.
    # We log their chat IDs, but we do not let the bot reply to staff group messages.
    if chat_type in {"group", "supergroup", "channel"}:
        return jsonify(
            {
                "status": "ignored",
                "reason": "group_or_channel_message_logged",
                "chat_id": chat_id,
                "chat_type": chat_type,
            }
        ), 200

    if not user_text:
        send_telegram_message(
            chat_id,
            "I can currently handle text messages only. Please type your message, or type MENU.",
        )

        return jsonify({"status": "ok"}), 200

    try:
        print(
            f"INCOMING CUSTOMER MESSAGE | chat_id={chat_id} | text={user_text}",
            flush=True,
        )

        reply = process_message(chat_id, user_text)
        send_telegram_message(chat_id, reply)

    except Exception as e:
        print("ERROR:", str(e), flush=True)
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
    start_inactivity_checker()

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    )