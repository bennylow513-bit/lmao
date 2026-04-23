from app import build_bot_reply


PHONE = "LOCAL_TEST"


def print_reply(reply):
    if isinstance(reply, str):
        print(f"Bot: {reply}")
        return

    if isinstance(reply, dict):
        kind = reply.get("kind")

        if kind == "buttons":
            print(f"Bot: {reply.get('body', '')}")
            buttons = reply.get("buttons", [])
            if buttons:
                print("Buttons:")
                for btn in buttons:
                    print(f"- {btn.get('title', '')}  [id: {btn.get('id', '')}]")
            if reply.get("footer"):
                print(reply["footer"])
            return

        if kind == "list":
            print(f"Bot: {reply.get('body', '')}")
            rows = reply.get("rows", [])
            if rows:
                print("Options:")
                for row in rows:
                    print(
                        f"- {row.get('title', '')} | "
                        f"{row.get('description', '')} "
                        f"[id: {row.get('id', '')}]"
                    )
            if reply.get("footer"):
                print(reply["footer"])
            return

    print(f"Bot: {reply}")


print("Local Jal Yoga test bot is ready.")
print("Type 'exit' to stop.")
print("Type things like: hi, trial, current member, general enquiry, corporate, staff hub")
print()

while True:
    user_text = input("You: ").strip()

    if user_text.lower() in {"exit", "quit"}:
        print("Bye.")
        break

    reply = build_bot_reply(PHONE, user_text)
    print_reply(reply)
    print()