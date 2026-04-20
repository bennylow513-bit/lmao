from app import build_bot_reply

PHONE = "LOCAL_TEST"

print("Local Jal Yoga test bot is ready.")
print("Type 'exit' to stop.")
print()

while True:
    user_text = input("You: ").strip()
    if user_text.lower() in {"exit", "quit"}:
        print("Bye.")
        break

    reply = build_bot_reply(PHONE, user_text)
    print(f"Bot: {reply}")
    print()