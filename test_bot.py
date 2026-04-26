from app import build_bot_reply


PHONE = "LOCAL_TEST"


def bubble_left(text: str):
    print()
    print("┌─ Jal Yoga Bot " + "─" * 42)
    for line in text.splitlines():
        print("│ " + line)
    print("└" + "─" * 56)
    print()


def bubble_right(text: str):
    print()
    print(" " * 20 + "You")
    for line in text.splitlines():
        print(" " * 20 + line)
    print()


def main():
    print("=" * 60)
    print("Jal Yoga WhatsApp Bot Local Tester")
    print("Type 'exit' or 'quit' to stop")
    print("Type 'menu' to restart")
    print("=" * 60)

    while True:
        user_text = input("You: ").strip()

        if not user_text:
            print("Please type something.")
            continue

        if user_text.lower() in {"exit", "quit"}:
            print("Bye.")
            break

        try:
            bubble_right(user_text)
            reply = build_bot_reply(PHONE, user_text)
            bubble_left(reply)
        except Exception as e:
            print()
            print("Error:", str(e))
            print()


if __name__ == "__main__":
    main()