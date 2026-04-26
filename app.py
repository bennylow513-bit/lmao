import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union
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

USER_STATES: Dict[str, Dict[str, Any]] = {}

MAIN_MENU_STATE = {
    "flow": "main_menu",
    "step": "waiting_choice",
    "data": {},
}


def load_knowledge_text() -> str:
    try:
        with open("knowledge.txt", "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


KNOWLEDGE_TEXT = load_knowledge_text()


def extract_section(title: str, text: str) -> str:
    pattern = rf"(?ms)^{re.escape(title)}:\s*\n(.*?)(?=^[A-Za-z][^\n]*:\s*$|\Z)"
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def extract_bullets(section_text: str) -> List[str]:
    items = []
    for line in section_text.splitlines():
        line = line.strip()
        if line.startswith("- "):
            items.append(line[2:].strip())
    return items


def parse_menu_items(text: str) -> List[str]:
    section = extract_section("Main menu", text)
    items = []
    for line in section.splitlines():
        line = line.strip()
        m = re.match(r"^\d+\.\s*(.+)$", line)
        if m:
            items.append(m.group(1).strip())
    return items


def parse_studios(text: str) -> List[Dict[str, str]]:
    section = extract_section("Studios", text)
    studios = []
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


def parse_operating_hours(text: str) -> List[str]:
    section = extract_section("Operating hours", text)
    return extract_bullets(section)


def parse_member_topics(text: str) -> List[str]:
    section = extract_section("Current member topics", text)
    return extract_bullets(section)


MENU_ITEMS = parse_menu_items(KNOWLEDGE_TEXT)
STUDIOS = parse_studios(KNOWLEDGE_TEXT)
OPERATING_HOURS = parse_operating_hours(KNOWLEDGE_TEXT)
MEMBER_TOPICS = parse_member_topics(KNOWLEDGE_TEXT)

TRIAL_STUDIOS = [s["name"] for s in STUDIOS]

if not TRIAL_STUDIOS:
    TRIAL_STUDIOS = [
        "Alexandra",
        "Katong",
        "Kovan",
        "Upper Bukit Timah",
        "Woodlands",
    ]

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


def normalize(text: str) -> str:
    return " ".join((text or "").strip().lower().replace("’", "'").split())


def now_singapore_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Singapore")).isoformat()


def closing_message() -> str:
    now_hour = datetime.now(ZoneInfo("Asia/Singapore")).hour
    if 7 <= now_hour < 18:
        return (
            "Is there anything else we can assist you with today?\n"
            "If not, we’ll close this ticket in a moment. "
            "Wishing you a wonderful and mindful day ahead! 🙏"
        )
    return (
        "Is there anything else we can assist you with today?\n"
        "If not, we’ll close this ticket for now. "
        "Wishing you a restful and peaceful evening ahead! ✨"
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


def reset_user(phone: str) -> None:
    USER_STATES.pop(phone, None)


def set_state(phone: str, flow: str, step: str, data: Optional[Dict[str, Any]] = None) -> None:
    USER_STATES[phone] = {
        "flow": flow,
        "step": step,
        "data": data or {},
    }


def get_state(phone: str) -> Dict[str, Any]:
    if phone in USER_STATES:
        return USER_STATES[phone]
    return {
        "flow": MAIN_MENU_STATE["flow"],
        "step": MAIN_MENU_STATE["step"],
        "data": {},
    }


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


YES_WORDS = {
    "yes",
    "y",
    "sure",
    "ok",
    "okay",
    "okie",
    "yes please",
    "yep",
    "yeah",
    "can",
    "please",
}

NO_WORDS = {
    "no",
    "n",
    "nope",
    "nah",
    "no thanks",
    "not now",
}


def is_yes(text: str) -> bool:
    return normalize(text) in YES_WORDS


def is_no(text: str) -> bool:
    return normalize(text) in NO_WORDS


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
    ]
    return any(k in t for k in keywords)


def match_studio(text: str, allowed_studios: List[str]) -> Optional[str]:
    t = normalize(text)
    for studio in allowed_studios:
        if t == normalize(studio):
            return studio
    return None


def valid_email(text: str) -> bool:
    return re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", text or "") is not None


def build_buttons_message(
    body: str,
    buttons: List[Tuple[str, str]],
    footer: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "kind": "buttons",
        "body": body,
        "buttons": [{"id": btn_id, "title": title} for btn_id, title in buttons],
        "footer": footer or "",
    }


def build_list_message(
    body: str,
    button_text: str,
    rows: List[Tuple[str, str, str]],
    section_title: str = "Options",
    footer: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "kind": "list",
        "body": body,
        "button_text": button_text,
        "section_title": section_title,
        "rows": [
            {
                "id": row_id,
                "title": title,
                "description": description,
            }
            for row_id, title, description in rows
        ],
        "footer": footer or "",
    }


def build_main_menu_message() -> Dict[str, Any]:
    return build_list_message(
        body=(
            "Namaste! Thank you for reaching out to Jal Yoga. 🙏\n\n"
            "Please choose what you need today:"
        ),
        button_text="Open Menu",
        rows=[
            ("menu_trial", "Schedule a Trial", "Book a trial class"),
            ("menu_member", "Current Member", "Membership support"),
            ("menu_general", "General Enquiry", "Studios, hours, policies"),
            ("menu_corporate", "Corporate", "Partnerships and wellness"),
            ("menu_staff", "Staff Hub", "Internal staff booking"),
        ],
        section_title="Main Menu",
        footer="You can also type CUSTOMER SERVICE anytime.",
    )


def build_trial_studio_message() -> Dict[str, Any]:
    rows = []
    for studio in TRIAL_STUDIOS:
        row_id = "studio_" + normalize(studio).replace(" ", "_")
        rows.append((row_id, studio, "Trial class outlet"))

    return build_list_message(
        body="We’d love to have you! First, which studio would you like to visit?",
        button_text="Choose Studio",
        rows=rows,
        section_title="Studios",
    )


def build_member_menu_message() -> Dict[str, Any]:
    return build_list_message(
        body=(
            "Welcome back! Hope your practice is going well. 🙏\n\n"
            "How can I help you with your membership today?"
        ),
        button_text="Choose Option",
        rows=[
            ("member_cancel", "Class Cancellation", "Cancellation policy"),
            ("member_suspend", "Membership Suspension", "Medical or non-medical"),
            ("member_booking_help", "Booking Help", "Help with class booking"),
            ("member_refer_friend", "Refer a Friend", "Invite a friend"),
        ],
        section_title="Member Services",
        footer="You can also type MENU anytime to go back.",
    )


def build_general_menu_message() -> Dict[str, Any]:
    return build_list_message(
        body="General Enquiry\n\nChoose a topic below, or type your question directly.",
        button_text="View Topics",
        rows=[
            ("general_studios", "Studios", "Studio locations"),
            ("general_hours", "Operating Hours", "Opening hours"),
            ("general_policies", "Policies", "Booking and cancellation"),
            ("general_question", "Ask a Question", "Type your question"),
        ],
        section_title="General Topics",
        footer="You can also type your own question.",
    )


def build_suspension_buttons_message() -> Dict[str, Any]:
    return build_buttons_message(
        body="Sure! Is your request for a Medical Suspension or Non-Medical Suspension?",
        buttons=[
            ("suspension_medical", "Medical"),
            ("suspension_non_medical", "Non-Medical"),
        ],
        footer="You can also type MENU to go back.",
    )


def main_menu_text() -> Dict[str, Any]:
    return build_main_menu_message()


def member_menu_text() -> Dict[str, Any]:
    return build_member_menu_message()


def general_menu_text() -> Dict[str, Any]:
    return build_general_menu_message()


def studios_text() -> str:
    if not STUDIOS:
        return (
            "I’m sorry — I’m not fully sure based on the information I have. "
            "I’ll pass this to our Customer Service team."
        )

    lines = ["Our studios are:"]
    for studio in STUDIOS:
        lines.append(f"- {studio['name']}: {studio['address']}")
    return "\n".join(lines)


def hours_text() -> str:
    if not OPERATING_HOURS:
        return (
            "I’m sorry — I’m not fully sure based on the information I have. "
            "I’ll pass this to our Customer Service team."
        )

    lines = ["Our operating hours are:"]
    for item in OPERATING_HOURS:
        lines.append(f"- {item}")
    return "\n".join(lines)


def build_location_picker_message() -> Dict[str, Any]:
    rows = []
    for studio in STUDIOS:
        row_id = "location_" + normalize(studio["name"]).replace(" ", "_")
        rows.append((row_id, studio["name"], "View address"))

    return build_list_message(
        body="Sure! Please choose the studio location you want:",
        button_text="Choose Location",
        rows=rows,
        section_title="Studio Locations",
        footer="You can also type the studio name.",
    )


def studio_address_text(studio_name: str) -> str:
    for studio in STUDIOS:
        if normalize(studio["name"]) == normalize(studio_name):
            return f"{studio['name']}:\n{studio['address']}"
    return (
        "I’m sorry — I’m not fully sure based on the information I have. "
        "I’ll pass this to our Customer Service team."
    )


def studio_location_summary_text(studio_name: str) -> str:
    for studio in STUDIOS:
        if normalize(studio["name"]) == normalize(studio_name):
            return (
                "Here is your summary:\n\n"
                f"Request: Studio location\n"
                f"Selected studio: {studio['name']}\n"
                f"Address: {studio['address']}\n\n"
                f"{closing_message()}\n"
                "Reply MENU to return to the main menu."
            )

    return (
        "I’m sorry — I’m not fully sure based on the information I have. "
        "I’ll pass this to our Customer Service team."
    )


def ask_llm(question: str) -> str:
    if not OPENAI_API_KEY:
        return (
            "I’m sorry — the AI answer service is not configured yet.\n"
            "Please type CUSTOMER SERVICE and our team will follow up."
        )

    instructions = f"""
You are Jal Yoga Singapore's WhatsApp assistant.

Rules:
1. Answer ONLY using the knowledge below.
2. Keep replies concise, warm, and professional.
3. Ask one question at a time.
4. Do not invent prices, classes, schedules, trainers, or phone numbers.
5. If the user wants a real human, or the issue is complaint, refund, payment, account-specific, manual review, or you are unsure, hand off to customer service.
6. If the answer is not clearly in the knowledge, reply exactly:
I’m sorry — I’m not fully sure based on the information I have. I’ll pass this to our Customer Service team.
7. If you want to offer studio addresses, ask exactly:
Would you also like the studio locations?
8. Do not ask other yes/no follow-up questions unless the app can handle them.

KNOWLEDGE:
{KNOWLEDGE_TEXT}
"""

    response = client.responses.create(
        model=OPENAI_MODEL,
        reasoning={"effort": "low"},
        instructions=instructions,
        input=question,
    )

    answer = (response.output_text or "").strip()
    if not answer:
        answer = (
            "I’m sorry — I’m not fully sure based on the information I have. "
            "I’ll pass this to our Customer Service team."
        )
    return answer


def maybe_escalate_after_llm(phone: str, user_text: str, answer: str) -> None:
    if "i'll pass this to our customer service team" in normalize(answer):
        save_request(
            "llm_handoff",
            phone,
            {
                "user_message": user_text,
                "llm_answer": answer,
            },
        )


def finalize_llm_answer(phone: str, user_text: str, answer: str) -> str:
    maybe_escalate_after_llm(phone, user_text, answer)

    normalized_answer = normalize(answer)

    if "would you also like the studio locations?" in normalized_answer:
        set_state(phone, "location_follow_up", "waiting_yes_no", {})
        return answer

    reset_user(phone)
    return answer + "\n\nReply MENU to return to the main menu."


def send_whatsapp_message(to: str, message: Union[str, Dict[str, Any]]) -> None:
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    if isinstance(message, str):
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": message},
        }

    elif message["kind"] == "buttons":
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": message["body"]},
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {
                                "id": btn["id"],
                                "title": btn["title"],
                            },
                        }
                        for btn in message["buttons"]
                    ]
                },
            },
        }
        if message.get("footer"):
            payload["interactive"]["footer"] = {"text": message["footer"]}

    elif message["kind"] == "list":
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": message["body"]},
                "action": {
                    "button": message["button_text"],
                    "sections": [
                        {
                            "title": message["section_title"],
                            "rows": message["rows"],
                        }
                    ],
                },
            },
        }
        if message.get("footer"):
            payload["interactive"]["footer"] = {"text": message["footer"]}

    else:
        raise ValueError("Unsupported message kind")

    print("SEND URL:", url)
    print("SEND TO:", to)
    print("SEND PAYLOAD:", json.dumps(payload, indent=2, ensure_ascii=False))

    response = requests.post(url, headers=headers, json=payload, timeout=30)

    print("SEND STATUS:", response.status_code)
    print("SEND RESPONSE:", response.text)

    response.raise_for_status()


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


def process_message(phone: str, incoming: Optional[Dict[str, Any]]) -> Union[str, Dict[str, Any]]:
    raw_text, t, reply_id = unpack_user_input(incoming)
    first_contact = phone not in USER_STATES

    if incoming is None:
        return (
            "I can currently handle text messages only.\n"
            "Please type your message, or type CUSTOMER SERVICE for manual help."
        )

    if is_menu_request(raw_text or ""):
        set_state(phone, "main_menu", "waiting_choice", {})
        return main_menu_text()

    if is_handoff_request(raw_text or ""):
        save_request(
            "customer_service_handoff",
            phone,
            {
                "user_message": raw_text or "",
                "state": get_state(phone),
            },
        )
        reset_user(phone)
        return (
            "Please let us know how we can help you!\n\n"
            "Simply type your enquiry below. While our response may not be immediate, "
            "our Customer Service team will review your message and get back to you right here "
            "as soon as possible."
        )

    if first_contact:
        set_state(phone, "main_menu", "waiting_choice", {})

    state = get_state(phone)
    flow = state["flow"]
    step = state["step"]
    data = state["data"]

    if flow == "location_follow_up" and step == "waiting_yes_no":
        if is_yes(raw_text or ""):
            set_state(phone, "location_follow_up", "choose_location", {})
            return build_location_picker_message()

        if is_no(raw_text or ""):
            reset_user(phone)
            return "No worries. Reply MENU anytime if you need anything else."

        return "Please reply YES or NO."

    if flow == "location_follow_up" and step == "choose_location":
        studio_map = {
            "location_" + normalize(studio["name"]).replace(" ", "_"): studio["name"]
            for studio in STUDIOS
        }

        studio = studio_map.get(reply_id) if reply_id else match_studio(raw_text or "", TRIAL_STUDIOS)

        if not studio:
            return build_location_picker_message()

        address_text = studio_address_text(studio)
        summary_text = studio_location_summary_text(studio)

        reset_user(phone)
        return address_text + "\n\n" + summary_text

    if flow == "main_menu" and step == "waiting_choice":
        if reply_id == "menu_trial" or t in {
            "1",
            "schedule a trial",
            "trial",
            "book trial",
            "book a trial",
            "schedule trial",
        }:
            set_state(phone, "trial", "ask_studio", {})
            return build_trial_studio_message()

        if reply_id == "menu_member" or t in {
            "2",
            "i'm a current member",
            "im a current member",
            "current member",
            "member",
        }:
            set_state(phone, "member_menu", "waiting_choice", {})
            return member_menu_text()

        if reply_id == "menu_general" or t in {
            "3",
            "i'd like to find out more about jal yoga",
            "id like to find out more about jal yoga",
            "general enquiry",
            "general inquiry",
            "find out more",
        }:
            set_state(phone, "general_menu", "waiting_choice", {})
            return general_menu_text()

        if reply_id == "menu_corporate" or t in {
            "4",
            "corporate",
            "corporate/partnerships",
            "partnerships",
            "corporate partnerships",
        }:
            set_state(phone, "corporate", "ask_name", {})
            return (
                "We’re always excited to explore new collaborations and wellness opportunities! 🤝\n\n"
                "May I have your Full Name?"
            )

        if reply_id == "menu_staff" or t in {"5", "staff hub", "staff"}:
            set_state(phone, "staff_hub", "ask_member_name", {})
            return "Hey Team! Please share the Member Name first."

        answer = ask_llm(raw_text or "")
        return finalize_llm_answer(phone, raw_text or "", answer)

    if flow == "trial":
        if step == "ask_studio":
            studio_map = {
                "studio_" + normalize(studio).replace(" ", "_"): studio
                for studio in TRIAL_STUDIOS
            }

            studio = studio_map.get(reply_id) if reply_id else match_studio(raw_text or "", TRIAL_STUDIOS)

            if not studio:
                return build_trial_studio_message()

            data["studio"] = studio
            set_state(phone, "trial", "ask_name", data)
            return "Got it. May I have your Full Name?"

        if step == "ask_name":
            data["name"] = raw_text or ""
            set_state(phone, "trial", "ask_goal", data)
            return "And finally, what is your Fitness Goal?"

        if step == "ask_goal":
            data["fitness_goal"] = raw_text or ""
            save_request("trial_booking", phone, data.copy())

            summary = (
                f"Thank you! I've sent your details to the {data['studio']} team. "
                f"Our Studio Manager will contact you within 24 hours to schedule your trial.\n\n"
                f"Outlet: {data['studio']}\n"
                f"Class: Trial Class\n"
                f"Name: {data['name']}\n"
                f"Fitness Goal: {data['fitness_goal']}\n\n"
                f"{closing_message()}\n"
                f"Reply MENU to return to the main menu."
            )

            reset_user(phone)
            return summary

    if flow == "member_menu" and step == "waiting_choice":
        if reply_id == "member_cancel" or t in {
            "1",
            "class cancellation",
            "cancel",
            "cancellation",
        }:
            answer = ask_llm(
                "Explain the class cancellation policy in a short WhatsApp reply."
            )
            maybe_escalate_after_llm(phone, raw_text or "", answer)
            reset_user(phone)
            return answer + "\n\nReply MENU to return to the main menu."

        if reply_id == "member_suspend" or t in {
            "2",
            "membership suspension",
            "suspension",
            "suspend",
        }:
            set_state(phone, "suspension", "ask_type", {})
            return build_suspension_buttons_message()

        if reply_id == "member_booking_help" or t in {
            "3",
            "class booking",
            "booking help",
            "help with booking",
            "help with class booking",
        }:
            set_state(phone, "booking_help", "ask_details", {})
            return "Please share your booking issue and our team will review it."

        if reply_id == "member_refer_friend" or t in {
            "4",
            "refer a friend",
            "refer friend",
        }:
            set_state(phone, "refer_friend", "ask_friend_name", {})
            return "That’s amazing! Please share your friend’s name first."

        return member_menu_text()

    if flow == "suspension":
        if step == "ask_type":
            if reply_id == "suspension_medical" or t == "medical":
                data["type"] = "Medical"
                set_state(phone, "suspension", "wait_proceed", data)
                answer = ask_llm(
                    "Explain the medical suspension policy in a short WhatsApp reply."
                )
                maybe_escalate_after_llm(phone, raw_text or "", answer)
                return (
                    answer
                    + "\n\nIf you would like our Customer Service team to follow up, reply PROCEED."
                    + "\nReply MENU to go back."
                )

            if reply_id == "suspension_non_medical" or t in {"non-medical", "non medical", "travel"}:
                data["type"] = "Non-Medical"
                set_state(phone, "suspension", "wait_proceed", data)
                answer = ask_llm(
                    "Explain the non-medical suspension policy in a short WhatsApp reply."
                )
                maybe_escalate_after_llm(phone, raw_text or "", answer)
                return (
                    answer
                    + "\n\nIf you would like our Customer Service team to follow up, reply PROCEED."
                    + "\nReply MENU to go back."
                )

            return build_suspension_buttons_message()

        if step == "wait_proceed":
            if t == "proceed":
                save_request(
                    "membership_suspension",
                    phone,
                    {"suspension_type": data.get("type", "Unknown")},
                )
                reset_user(phone)
                return (
                    "Thank you for your submission! Our Customer Service team will review your request and get back to you.\n\n"
                    f"{closing_message()}\n"
                    "Reply MENU to return to the main menu."
                )
            return "Please reply PROCEED if you would like our Customer Service team to follow up, or type MENU to go back."

    if flow == "booking_help" and step == "ask_details":
        save_request("class_booking_help", phone, {"details": raw_text or ""})
        reset_user(phone)
        return (
            "Thank you! Our Customer Service team will review your response and get back to you shortly.\n\n"
            f"{closing_message()}\n"
            "Reply MENU to return to the main menu."
        )

    if flow == "refer_friend":
        if step == "ask_friend_name":
            data["friend_name"] = raw_text or ""
            set_state(phone, "refer_friend", "ask_friend_contact", data)
            return "Please share your friend’s contact number."

        if step == "ask_friend_contact":
            data["friend_contact"] = raw_text or ""
            save_request("refer_friend", phone, data.copy())
            reset_user(phone)
            return (
                "Thank you! Our team will review the referral and follow up accordingly.\n\n"
                f"{closing_message()}\n"
                "Reply MENU to return to the main menu."
            )

    if flow == "general_menu" and step == "waiting_choice":
        if reply_id == "general_studios" or t in {"1", "studios", "locations", "studio"}:
            reset_user(phone)
            return studios_text() + "\n\nReply MENU to return to the main menu."

        if reply_id == "general_hours" or t in {"2", "hours", "operating hours"}:
            reset_user(phone)
            return hours_text() + "\n\nReply MENU to return to the main menu."

        if reply_id == "general_policies" or t in {"3", "policies", "policy"}:
            answer = ask_llm(
                "Summarize the booking, cancellation, and suspension policies in a short WhatsApp reply."
            )
            maybe_escalate_after_llm(phone, raw_text or "", answer)
            reset_user(phone)
            return answer + "\n\nReply MENU to return to the main menu."

        if reply_id == "general_question" or t == "4":
            set_state(phone, "general_question", "ask_question", {})
            return "Sure — please type your question."

        answer = ask_llm(raw_text or "")
        return finalize_llm_answer(phone, raw_text or "", answer)

    if flow == "general_question" and step == "ask_question":
        answer = ask_llm(raw_text or "")
        return finalize_llm_answer(phone, raw_text or "", answer)

    if flow == "corporate":
        if step == "ask_name":
            data["name"] = raw_text or ""
            set_state(phone, "corporate", "ask_email", data)
            return "Thank you! Please share your Work Email Address."

        if step == "ask_email":
            if not valid_email(raw_text or ""):
                return "Please enter a valid work email address."
            data["email"] = raw_text or ""
            set_state(phone, "corporate", "ask_company", data)
            return "Please share your Company Name. If you prefer, you can reply SKIP."

        if step == "ask_company":
            data["company"] = "" if t == "skip" else (raw_text or "")
            save_request("corporate_partnership", phone, data.copy())
            reset_user(phone)
            return (
                "Thank you for sharing those details!\n"
                "Your information has been forwarded to our Partnerships Team. "
                "We will review your request and get back to you via email.\n\n"
                f"{closing_message()}\n"
                "Reply MENU to return to the main menu."
            )

    if flow == "staff_hub":
        if step == "ask_member_name":
            data["member_name"] = raw_text or ""
            set_state(phone, "staff_hub", "ask_date_time", data)
            return "Please share the Date & Time."

        if step == "ask_date_time":
            data["date_time"] = raw_text or ""
            set_state(phone, "staff_hub", "ask_location_room", data)
            return "Please share the Studio Location & Room."

        if step == "ask_location_room":
            data["location_room"] = raw_text or ""
            save_request("staff_hub_booking", phone, data.copy())
            reset_user(phone)
            return (
                "Thanks! We’ve sent your booking request to the studio. "
                "We’ll double-check the schedule and update you shortly.\n\n"
                f"{closing_message()}\n"
                "Reply MENU to return to the main menu."
            )

    answer = ask_llm(raw_text or "")
    return finalize_llm_answer(phone, raw_text or "", answer)


def build_bot_reply(phone: str, user_text: str) -> Union[str, Dict[str, Any]]:
    incoming = {
        "kind": "text",
        "text": user_text,
    }
    return process_message(phone, incoming)


@app.route("/", methods=["GET"])
def home():
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
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200

    return "Verification failed", 403


@app.route("/webhook", methods=["POST"])
def webhook():
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
    app.run(host="0.0.0.0", port=PORT, debug=True)