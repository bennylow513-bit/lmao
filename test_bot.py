import queue
import threading
import sys

from app import build_bot_reply, start_inactivity_thread


PHONE = "LOCAL_TEST"
REMINDER_QUEUE = queue.Queue()


def print_divider():
    print("-" * 60, flush=True)


def print_bot(reply: str):
    print()
    print("Jal Yoga Bot:", flush=True)
    print(reply, flush=True)
    print_divider()


def reminder_listener():
    while True:
        message = REMINDER_QUEUE.get()
        if message is None:
            break

        sys.stdout.write("\n")
        sys.stdout.write("Jal Yoga Bot (Reminder):\n")
        sys.stdout.write(message + "\n")
        sys.stdout.write("-" * 60 + "\n")
        sys.stdout.flush()


def main():
    start_inactivity_thread(test_mode=True, reminder_queue=REMINDER_QUEUE)

    listener = threading.Thread(target=reminder_listener, daemon=True)
    listener.start()

    print_divider()
    print("Jal Yoga Local Test Bot", flush=True)
    print("Type 'exit' or 'quit' to stop", flush=True)
    print("Type 'menu' anytime to restart the conversation", flush=True)
    print_divider()

    while True:
        user_text = input("You: ").strip()

        if not user_text:
            print("Please type something.", flush=True)
            continue

        if user_text.lower() in {"exit", "quit"}:
            REMINDER_QUEUE.put(None)
            print("Bye.", flush=True)
            break

        try:
            reply = build_bot_reply(PHONE, user_text)
            print_bot(reply)
        except Exception as e:
            print()
            print("Error:", flush=True)
            print(str(e), flush=True)
            print_divider()


if __name__ == "__main__":
    main()