from app import build_bot_reply


PHONE = "LOCAL_TEST"


def print_divider():
    print("-" * 60)


def print_bot(reply: str):
    print()
    print("Jal Yoga Bot:")
    print(reply)
    print_divider()


def main():
    print_divider()
    print("Jal Yoga Local Test Bot")
    print("Type 'exit' or 'quit' to stop")
    print("Type 'menu' anytime to restart the conversation")
    print_divider()

    while True:
        user_text = input("You: ").strip()

        if not user_text:
            print("Please type something.")
            continue

        if user_text.lower() in {"exit", "quit"}:
            print("Bye.")
            break

        try:
            reply = build_bot_reply(PHONE, user_text)
            print_bot(reply)
        except Exception as e:
            print()
            print("Error:")
            print(str(e))
            print_divider()


if __name__ == "__main__":
    main()