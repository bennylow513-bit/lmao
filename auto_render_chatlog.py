import json
import time
import urllib.parse
import urllib.request
from datetime import datetime

# CHANGE THESE 2 ONLY
RENDER_URL = "https://yoga-og5l.onrender.com"
DEBUG_ROUTE_TOKEN = "12345"

# How often it refreshes
REFRESH_SECONDS = 5

# Output files inside VS Code
JSON_OUTPUT_FILE = "render_chat_logs.json"
TXT_OUTPUT_FILE = "render_chat_logs.txt"


def download_chatlog():
    url = (
        f"{RENDER_URL.rstrip('/')}/debug/chat-log-file"
        f"?token={urllib.parse.quote(DEBUG_ROUTE_TOKEN)}"
        f"&limit=300"
    )

    with urllib.request.urlopen(url, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))

    # Save full JSON
    with open(JSON_OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # Save readable TXT
    entries = data.get("entries", [])

    with open(TXT_OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("RENDER CHATLOG AUTO UPDATE\n")
        f.write(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 60 + "\n\n")

        if not entries:
            f.write("No chat messages yet.\n")
        else:
            for entry in entries:
                time_sg = entry.get("time_sg", "")
                chat_id = entry.get("chat_id", "")
                direction = entry.get("direction", "")
                role = entry.get("role", "")
                message = entry.get("message", "")

                f.write(f"[{time_sg}]\n")
                f.write(f"Chat ID: {chat_id}\n")
                f.write(f"{direction.upper()} | {role}: {message}\n")
                f.write("-" * 60 + "\n")

    print(f"Updated {TXT_OUTPUT_FILE} at {datetime.now().strftime('%H:%M:%S')}")


print("Auto Render chatlog started.")
print("Press CTRL + C to stop.\n")

while True:
    try:
        download_chatlog()
    except Exception as e:
        print("Error downloading chatlog:", e)

    time.sleep(REFRESH_SECONDS)