# Jal Yoga Telegram AI Chatbot

A Telegram AI customer-service chatbot for Jal Yoga Singapore.

This bot helps customers with trial bookings, membership enquiries, class cancellation questions, studio information, class schedule questions, customer-service handoff, multilingual replies, and chatlog tracking.

---

## Project Overview

This project is built with:

- Python
- Flask
- Telegram Bot API
- OpenAI
- Render
- GitHub
- `knowledge.txt` as the editable knowledge base

The bot is designed to provide 24/7 automated replies for common Jal Yoga enquiries, while still allowing users to speak to Customer Service when needed.

---

## Main Features

### Telegram Chatbot

The bot can reply to users through Telegram using a webhook.

Users can type:

```text
hi
```

The bot will show the main menu:

```text
1. Schedule a Trial
2. I’m a current member
3. I’d like to find out more about Jal Yoga
4. Corporate/Partnerships
5. Staff Hub
```

---

### AI Replies Using OpenAI

The bot uses OpenAI to answer customer questions based on:

1. `knowledge.txt`
2. Render environment variables
3. Recent chat context

The bot is instructed not to invent prices, promotions, schedules, trainers, policies, or outlet information.

---

### Trial Booking Flow

The bot can guide users through a trial booking flow.

It can ask for:

- Preferred outlet
- Full name
- Fitness goal

Then it creates a trial booking summary for Customer Service.

---

### Current Member Flow

The bot supports current member enquiries such as:

- Class cancellation
- Membership suspension
- Class booking help
- Refer a friend

---

### General Enquiry Flow

The bot can help users ask about:

- Studio locations and operating hours
- Class types
- Events and retreats

---

### Customer Service Handoff

When the bot is unsure or the user asks to speak to Customer Service, it can create a handoff summary.

Example:

```text
I’ll pass this to our Customer Service team.

Summary:
- Topic: Customer Service Request
- Outlet: Katong
- Message: I need help with my booking
```

The bot can route the handoff to:

- Main Customer Service Telegram chat
- Outlet-specific Telegram chat IDs

---

### Multilingual Support

The bot can detect and reply in different languages where possible.

Supported examples:

- English
- Chinese
- Malay
- Tamil
- Thai
- Portuguese
- Spanish
- French
- Japanese
- Korean

---

### Chatlog System

The bot saves Telegram conversations into chatlog files.

Main chatlog file:

```text
chat_logs.jsonl
```

Per-customer chatlog folder:

```text
chatlogs/
```

Example chatlog entry:

```json
{
  "time_sg": "2026-05-10T12:00:00+08:00",
  "chat_id": "123456789",
  "direction": "incoming",
  "role": "customer",
  "message": "hi",
  "meta": {
    "platform": "telegram"
  }
}
```

---

## Project Structure

```text
.
├── app.py
├── knowledge.txt
├── schedule.json
├── requirements.txt
├── README.md
├── .env
├── .gitignore
├── chat_logs.jsonl
├── chatlogs/
└── templates/
```

Optional helper files:

```text
.
├── auto_render_chatlog.py
├── auto_knowledge.py
└── uploads/
```

---

## File Explanation

### `app.py`

Main chatbot application.

It handles:

- Flask web server
- Telegram webhook
- OpenAI replies
- Customer menu flow
- Trial booking flow
- Current member flow
- Customer Service handoff
- Chatlog saving
- Debug routes
- Schedule loading
- Knowledge file loading

---

### `knowledge.txt`

This is the bot’s editable knowledge base.

Put Jal Yoga information here, such as:

- Studio locations
- Trial class details
- Membership policies
- Class types
- Prices
- Promotions
- Events
- Cancellation policies
- Customer Service information

The bot should use this file as the main source of truth.

---

### `schedule.json`

Optional class schedule file.

Use this file if you want the bot to answer schedule questions from structured data.

---

### `.env`

Stores private keys and tokens for local development.

Do not upload this file to GitHub.

---

### `requirements.txt`

Stores the Python packages needed to run the bot.

---

## Requirements

You need:

- Python 3.10 or above
- Telegram bot token
- OpenAI API key
- GitHub account
- Render account

---

## Installation

### 1. Download or clone the project

```bash
git clone https://github.com/YOUR-USERNAME/YOUR-REPO-NAME.git
cd YOUR-REPO-NAME
```

---

### 2. Create a virtual environment

Windows:

```bash
python -m venv venv
venv\Scripts\activate
```

Mac/Linux:

```bash
python3 -m venv venv
source venv/bin/activate
```

---

### 3. Install required packages

```bash
pip install -r requirements.txt
```

---

## Recommended `requirements.txt`

Create a file named:

```text
requirements.txt
```

Paste this inside:

```txt
Flask
requests
python-dotenv
openai
gunicorn
watchdog
pymupdf
python-docx
openpyxl
python-pptx
```

---

## Environment Variables

Create a file named:

```text
.env
```

Paste this inside and replace the values:

```env
OPENAI_API_KEY=your_openai_api_key_here
OPENAI_MODEL=gpt-5.4-mini

TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_SECRET_TOKEN=your_random_secret_token_here
TELEGRAM_BOT_USERNAME=your_bot_username_here

CUSTOMER_SERVICE_WHATSAPP_NUMBER=6590000000
CUSTOMER_SERVICE_TELEGRAM_CHAT_ID=your_customer_service_telegram_chat_id_here

DEBUG_ROUTE_TOKEN=your_private_debug_token_here

PORT=5000
OPT_OUT_FILE=telegram_opt_out_users.json
SCHEDULE_FILE=schedule.json

CHATLOG_ENABLED=true
CHATLOG_DIR=chatlogs
CHATLOG_FILE=chat_logs.jsonl
CHATLOG_MAX_VIEW_LINES=300
```

---

## Outlet-Specific Environment Variables

Use these if each outlet has its own Telegram Customer Service chat.

```env
ALEXANDRA_TELEGRAM_CHAT_ID=
KATONG_TELEGRAM_CHAT_ID=
KOVAN_TELEGRAM_CHAT_ID=
UPPER_BUKIT_TIMAH_TELEGRAM_CHAT_ID=
WOODLANDS_TELEGRAM_CHAT_ID=
```

Use these if each outlet has its own WhatsApp number.

```env
ALEXANDRA_WHATSAPP_NUMBER=
KATONG_WHATSAPP_NUMBER=
KOVAN_WHATSAPP_NUMBER=
UPPER_BUKIT_TIMAH_WHATSAPP_NUMBER=
WOODLANDS_WHATSAPP_NUMBER=
```

If outlet-specific values are empty, the bot will use the main Customer Service contact.

---

## Run Locally

Run this in VS Code terminal:

```bash
python app.py
```

Then open:

```text
http://localhost:5000
```

---

## Telegram Webhook Setup

After deploying to Render, set your Telegram webhook.

Use this link format:

```text
https://api.telegram.org/botYOUR_TELEGRAM_BOT_TOKEN/setWebhook?url=https://YOUR-RENDER-APP.onrender.com/telegram/webhook&secret_token=YOUR_TELEGRAM_SECRET_TOKEN
```

Example:

```text
https://api.telegram.org/bot123456:ABCDEF/setWebhook?url=https://jalyoga-bot.onrender.com/telegram/webhook&secret_token=mysecret123
```

Do not share your real Telegram bot token.

---

## Render Deployment

### 1. Push project to GitHub

```bash
git add .
git commit -m "Upload Jal Yoga Telegram chatbot"
git push
```

---

### 2. Create Render Web Service

On Render:

1. Click **New**
2. Click **Web Service**
3. Connect your GitHub repository
4. Choose your branch
5. Choose Python environment

---

### 3. Build Command

Use this:

```bash
pip install -r requirements.txt
```

---

### 4. Start Command

Use this:

```bash
gunicorn app:app
```

---

### 5. Add Environment Variables on Render

Go to:

```text
Render Dashboard > Your Service > Environment
```

Add these:

```env
OPENAI_API_KEY=
OPENAI_MODEL=gpt-5.4-mini

TELEGRAM_BOT_TOKEN=
TELEGRAM_SECRET_TOKEN=
TELEGRAM_BOT_USERNAME=

CUSTOMER_SERVICE_WHATSAPP_NUMBER=
CUSTOMER_SERVICE_TELEGRAM_CHAT_ID=

DEBUG_ROUTE_TOKEN=

PORT=5000
OPT_OUT_FILE=telegram_opt_out_users.json
SCHEDULE_FILE=schedule.json

CHATLOG_ENABLED=true
CHATLOG_DIR=chatlogs
CHATLOG_FILE=chat_logs.jsonl
CHATLOG_MAX_VIEW_LINES=300
```

Optional outlet variables:

```env
ALEXANDRA_TELEGRAM_CHAT_ID=
KATONG_TELEGRAM_CHAT_ID=
KOVAN_TELEGRAM_CHAT_ID=
UPPER_BUKIT_TIMAH_TELEGRAM_CHAT_ID=
WOODLANDS_TELEGRAM_CHAT_ID=

ALEXANDRA_WHATSAPP_NUMBER=
KATONG_WHATSAPP_NUMBER=
KOVAN_WHATSAPP_NUMBER=
UPPER_BUKIT_TIMAH_WHATSAPP_NUMBER=
WOODLANDS_WHATSAPP_NUMBER=
```

After adding environment variables, redeploy the Render service.

---

## Debug Routes

All debug routes should use your `DEBUG_ROUTE_TOKEN`.

Format:

```text
https://YOUR-RENDER-APP.onrender.com/debug/ROUTE?token=YOUR_DEBUG_ROUTE_TOKEN
```

---

### View chatlog file

```text
https://YOUR-RENDER-APP.onrender.com/debug/chat-log-file?token=YOUR_DEBUG_ROUTE_TOKEN
```

---

### View all chatlogs

```text
https://YOUR-RENDER-APP.onrender.com/debug/chatlogs?token=YOUR_DEBUG_ROUTE_TOKEN
```

---

### View one customer chatlog

```text
https://YOUR-RENDER-APP.onrender.com/debug/chatlog?chat_id=CUSTOMER_CHAT_ID&token=YOUR_DEBUG_ROUTE_TOKEN
```

---

### Test chatlog writing

```text
https://YOUR-RENDER-APP.onrender.com/debug/test-chatlog?token=YOUR_DEBUG_ROUTE_TOKEN
```

---

### Check config

```text
https://YOUR-RENDER-APP.onrender.com/debug/config?token=YOUR_DEBUG_ROUTE_TOKEN
```

---

### Check outlets

```text
https://YOUR-RENDER-APP.onrender.com/debug/outlets?token=YOUR_DEBUG_ROUTE_TOKEN
```

---

### Check schedule

```text
https://YOUR-RENDER-APP.onrender.com/debug/schedule?token=YOUR_DEBUG_ROUTE_TOKEN
```

---

### Check trial bookings

```text
https://YOUR-RENDER-APP.onrender.com/debug/trial-bookings?token=YOUR_DEBUG_ROUTE_TOKEN
```

---

## Chatlog Auto Update in VS Code

When the bot runs on Render, the chatlog is saved on Render, not directly inside VS Code.

To auto-download the Render chatlog into VS Code, create a file named:

```text
auto_render_chatlog.py
```

Paste this:

```python
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime

# Change these 2 values
RENDER_URL = "https://YOUR-RENDER-APP.onrender.com"
DEBUG_ROUTE_TOKEN = "YOUR_DEBUG_ROUTE_TOKEN"

REFRESH_SECONDS = 5

JSON_OUTPUT_FILE = "render_chat_log.json"
TXT_OUTPUT_FILE = "render_chat_log.txt"


def download_chatlog():
    url = (
        f"{RENDER_URL.rstrip('/')}/debug/chat-log-file"
        f"?token={urllib.parse.quote(DEBUG_ROUTE_TOKEN)}"
        f"&limit=300"
    )

    with urllib.request.urlopen(url, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))

    with open(JSON_OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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
```

Run it:

```bash
python auto_render_chatlog.py
```

Then open:

```text
render_chat_log.txt
```

Now the file will refresh every 5 seconds.

---

## Auto Knowledge Importer

Optional helper file:

```text
auto_knowledge.py
```

This file watches the `uploads/` folder and automatically adds supported file content into `knowledge.txt`.

Supported file types:

```text
.pdf
.txt
.md
.docx
.csv
.xlsx
.pptx
.json
```

Run it:

```bash
python auto_knowledge.py
```

Then drop files into:

```text
uploads/
```

The extracted text will be added into:

```text
knowledge.txt
```

Always check and clean the imported content before using it in the final bot.

---

## Recommended `.gitignore`

Create a file named:

```text
.gitignore
```

Paste this:

```gitignore
.env

__pycache__/
*.pyc
venv/
.venv/

chatlogs/
chat_logs.jsonl
chat_logs.txt
render_chat_log.json
render_chat_log.txt
render_chat_logs.json
render_chat_logs.txt

telegram_opt_out_users.json
processed_files.json
uploads/

.DS_Store
```

Do not upload `.env`, chatlogs, or private tokens to GitHub.

---

## Common GitHub Commands

Check current status:

```bash
git status
```

Add all changes:

```bash
git add .
```

Save changes:

```bash
git commit -m "Update Jal Yoga chatbot"
```

Push to GitHub:

```bash
git push
```

---

## Updating GitHub After Changes

Use this every time you update your code:

```bash
git add .
git commit -m "Update chatbot files"
git push
```

---

## Remove `.env` from GitHub Tracking

If `.env` was accidentally added to Git:

```bash
git rm --cached .env
git add .gitignore
git commit -m "Remove env file from tracking"
git push
```

After that, reset your exposed API keys and tokens.

---

## Example Bot Conversation

User:

```text
hi
```

Bot:

```text
Namaste! Thank you for reaching out to Jal Yoga. 🙏

To help us handle your request as quickly as possible, please let us know what you're looking for today:

1. Schedule a Trial
2. I’m a current member
3. I’d like to find out more about Jal Yoga
4. Corporate/Partnerships
5. Staff Hub
```

User:

```text
1
```

Bot:

```text
Sure — let’s schedule your trial class. 🙏

Which studio would you prefer?

1. Alexandra
2. Katong
3. Kovan
4. Upper Bukit Timah
5. Woodlands
```

User:

```text
Katong
```

Bot:

```text
May I have your full name?
```

User:

```text
Ben Ong
```

Bot:

```text
Thanks, Ben Ong — what’s your fitness goal for the trial?
```

---

## Troubleshooting

### Bot does not reply

Check:

1. Render service is running
2. Telegram webhook is set correctly
3. `TELEGRAM_BOT_TOKEN` is correct
4. `TELEGRAM_SECRET_TOKEN` matches the webhook secret
5. Render logs show incoming Telegram updates

---

### OpenAI does not reply

Check:

1. `OPENAI_API_KEY` is set correctly
2. `OPENAI_MODEL` is set correctly
3. Render environment variables are saved
4. Render service was redeployed
5. OpenAI API key has access

---

### Chatlog does not update

Check:

1. `CHATLOG_ENABLED=true`
2. The bot is receiving Telegram messages
3. Open this route:

```text
/debug/test-chatlog?token=YOUR_DEBUG_ROUTE_TOKEN
```

Then open:

```text
/debug/chat-log-file?token=YOUR_DEBUG_ROUTE_TOKEN
```

---

### Render says `requirements.txt` missing

Make sure this file exists:

```text
requirements.txt
```

Then push it to GitHub:

```bash
git add requirements.txt
git commit -m "Add requirements file"
git push
```

---

### Yellow squiggly lines in VS Code

Install missing packages:

```bash
pip install requests python-dotenv flask openai watchdog pymupdf python-docx openpyxl python-pptx
```

Then select the correct Python interpreter:

```text
CTRL + SHIFT + P
Python: Select Interpreter
```

---

## Security Notes

Never upload these files to GitHub:

```text
.env
chat_logs.jsonl
chat_logs.txt
chatlogs/
render_chat_log.json
render_chat_log.txt
telegram_opt_out_users.json
processed_files.json
uploads/
```

Keep these secret:

- OpenAI API key
- Telegram bot token
- Telegram secret token
- Debug route token
- Customer Service chat IDs

---

## Project Goal

The goal of this project is to provide Jal Yoga customers with a 24/7 Telegram AI assistant that can answer common enquiries, guide users through structured flows, support trial bookings, and hand off complex questions to Customer Service.

---

## Current Status

Current version includes:

- Telegram webhook
- OpenAI customer-service replies
- `knowledge.txt` support
- Trial booking flow
- Current member flow
- General enquiry flow
- Customer Service handoff
- Multilingual reply support
- Chatlog saving
- Render deployment support
- Debug routes for testing

---

## Author

Created for a Jal Yoga chatbot project.
