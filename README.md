# Jal Yoga Singapore — Telegram AI Chatbot

A 24/7 AI-powered Telegram customer-service bot for Jal Yoga Singapore. It handles trial bookings, membership enquiries, studio information, live two-way Customer Service chat, and multilingual support — all inside Telegram.

---

## Table of Contents

- [Project Overview](#project-overview)
- [Features](#features)
- [Project Structure](#project-structure)
- [Requirements](#requirements)
- [Installation](#installation)
- [Environment Variables](#environment-variables)
- [Run Locally](#run-locally)
- [Telegram Webhook Setup](#telegram-webhook-setup)
- [Deploy to Render](#deploy-to-render)
- [Routes](#routes)
- [Helper Scripts](#helper-scripts)
- [Common GitHub Commands](#common-github-commands)
- [Troubleshooting](#troubleshooting)
- [Security Notes](#security-notes)

---

## Project Overview

Built with:

- **Python 3.10+**
- **Flask** — web server and webhook handler
- **Telegram Bot API** — messaging interface
- **OpenAI** — AI replies
- **Google Sheets** *(optional)* — chatlog sync (primary storage)
- **Render** — cloud deployment
- **`knowledge.txt`** — editable knowledge base
- **Jal Yoga website** — fetched at startup as supplementary knowledge

---

## Features

### Main Menu

Users trigger the menu by typing `hi`, `hello`, `start`, `/start`, `menu`, and several other reset words (including common greetings in other languages).

```
Namaste! Thank you for reaching out to Jal Yoga. 🙏

1. Schedule a Trial
2. I'm a current member
3. I'd like to find out more about Jal Yoga
4. Corporate/Partnerships
5. Staff Hub

You can also type CUSTOMER SERVICE anytime, in any language,
to speak to our team.
Reply STOP anytime to stop receiving follow-up messages.
```

---

### Trial Booking Flow

Guides the user through a step-by-step trial booking:

1. Preferred outlet (or location-based recommendation)
2. Full name
3. Fitness goal

Produces a booking summary and notifies the relevant outlet team. Users who have already booked can also request to change their trial outlet, and the previous outlet is notified of the change.

---

### Current Member Flow

Supports enquiries for existing members:

```
1. Class Cancellation
2. Membership Suspension
3. I need help with my class booking
4. I would like to refer a friend
```

Referral details are forwarded to the relevant outlet.

---

### General Enquiry Flow

Answers questions about studio locations and hours, class types, facilities, schedules, events, and retreats — sourced from `knowledge.txt` and the Jal Yoga website knowledge fetched at startup.

---

### Staff Hub

Menu option **5** is a hub for staff-related requests. Among other things, it can list instructor names and then answer follow-up questions about a specific instructor.

> *Review note:* Confirm the exact Staff Hub behaviour and any access restrictions before publishing this section.

---

### Customer Service Handoff & Live Chat

The bot has two levels of Customer Service support:

**1. Handoff summary** — when the bot is unsure or a user asks for a human, it creates a structured summary and routes it to the correct Customer Service channel (main or outlet-specific).

```
I'll pass this to our Customer Service team.

Summary:
- Topic: Membership cancellation
- Outlet: Katong
- Message: I want to cancel my membership
```

**2. Live two-way chat** — once a handoff opens, the bot relays messages directly between the customer and Customer Service staff:

- Customer messages are forwarded to the assigned CS chat.
- CS staff reply simply by replying in their Telegram chat, or with the backup command `/reply <chat_id> <message>`.
- Either side can end the session; staff can close with `/close` or by typing `close`.
- Messages containing blocked/offensive words are filtered and not relayed.

---

### Nearest Outlet Recommendation

Users can type their location, postal code, MRT station, or area, and the bot recommends the likely nearest outlet. Outlet-matching rules can be customised via `knowledge.txt`.

---

### Live Class Schedule (Mindbody)

The bot can fetch live class schedule information for outlets directly from Mindbody schedule widgets, so users can ask about upcoming classes.

> *Review note:* Confirm which outlets have working Mindbody widgets configured before relying on this in production.

---

### Multilingual Support

The bot detects the user's language and replies accordingly. Supported languages include English, Chinese, Malay, Tamil, Thai, Japanese, Korean, Portuguese, Spanish, and French. Singlish and common typos are handled gracefully.

---

### Opt-Out / Opt-In

Users can stop follow-up messages by sending `STOP` (and similar phrases like `unsubscribe`, `remove me`). They can opt back in with `START` or `subscribe`. Opt-out status is stored in the file named by `OPT_OUT_FILE`.

---

### Inactivity Handling

If a conversation goes quiet, a background checker sends a warning after `INACTIVITY_WARNING_SECONDS` and closes the session after `INACTIVITY_CLOSE_SECONDS`. The checker runs automatically.

---

### Chatlog System

All conversations are logged. Storage destinations:

- **Google Sheet** — primary destination when configured (see [Environment Variables](#environment-variables))
- **Local files** — only when `CHATLOG_LOCAL_ENABLED=true`:
  - `chat_logs.jsonl` — flat log file
  - `chatlogs/` — per-customer log files (by chat ID)

Sensitive content is lightly redacted before storage: email addresses, OTP/password/CVV phrases, and long numeric strings (9+ digits) are masked.

Example log entry:

```json
{
  "time_sg": "2026-05-10T12:00:00+08:00",
  "chat_id": "123456789",
  "direction": "incoming",
  "role": "customer",
  "message": "hi",
  "meta": { "platform": "telegram" }
}
```

---

## Project Structure

```
.
├── app.py                     # Main application
├── knowledge.txt              # Editable knowledge base
├── requirements.txt
├── README.md
├── .env                       # Local secrets (do not commit)
├── .gitignore
├── chat_logs.jsonl            # Flat chatlog (local logging only; do not commit)
├── chatlogs/                  # Per-customer chatlogs (local logging only; do not commit)
└── templates/                 # Flask HTML templates
```

Optional helper scripts:

```
├── auto_render_chatlog.py     # Auto-downloads Render chatlog into VS Code
├── auto_knowledge.py          # Watches uploads/ and imports content to knowledge.txt
└── uploads/                   # Drop files here for auto_knowledge.py
```

---

## Requirements

- Python 3.10 or above
- Telegram Bot token (from [@BotFather](https://t.me/BotFather))
- OpenAI API key
- GitHub account
- Render account (for deployment)

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/YOUR-USERNAME/YOUR-REPO-NAME.git
cd YOUR-REPO-NAME
```

### 2. Create a virtual environment

**Windows:**
```bash
python -m venv venv
venv\Scripts\activate
```

**Mac / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

Recommended `requirements.txt`:

```
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
gspread
google-auth
```

---

## Environment Variables

Create a `.env` file in the project root. **Never commit this file.**

```env
# OpenAI
OPENAI_API_KEY=your_openai_api_key_here
OPENAI_MODEL=gpt-5.4-mini

# Telegram
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_SECRET_TOKEN=your_random_secret_token_here
TELEGRAM_BOT_USERNAME=your_bot_username_here

# Customer Service contacts
CUSTOMER_SERVICE_WHATSAPP_NUMBER=6590000000
CUSTOMER_SERVICE_TELEGRAM_CHAT_ID=your_cs_telegram_chat_id_here

# Debug
DEBUG_ROUTE_TOKEN=your_private_debug_token_here

# App
PORT=5000
OPT_OUT_FILE=telegram_opt_out_users.json
FLASK_DEBUG=false

# Chatlogs
CHATLOG_ENABLED=true
CHATLOG_LOCAL_ENABLED=false
CHATLOG_DIR=chatlogs
CHATLOG_FILE=chat_logs.jsonl
CHATLOG_MAX_VIEW_LINES=300

# Inactivity timeout
INACTIVITY_WARNING_SECONDS=300
INACTIVITY_CLOSE_SECONDS=600
INACTIVITY_CHECK_SECONDS=30
```

> **Notes:**
> - `OPENAI_MODEL` defaults to `gpt-5.4-mini` if unset. Set it to any model your API key can access.
> - `CHATLOG_LOCAL_ENABLED` defaults to `false` — local `.jsonl` and `chatlogs/` files are written only when this is `true`. The Google Sheet is the primary destination.
> - The inactivity checker starts automatically; there is no separate enable/disable variable.

### Outlet-specific contacts *(optional)*

If each outlet has its own Customer Service channel:

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

If outlet-specific values are left blank, the bot falls back to the main Customer Service contact.

### Google Sheets integration *(optional)*

To sync chatlogs to a Google Sheet, set `GOOGLE_SHEET_ID` and **one** of the credential options:

```env
GOOGLE_SHEET_ID=your_google_sheet_id_here
GOOGLE_SHEET_WORKSHEET=Chat Logs

# Provide ONE of the following credential sources:
GOOGLE_SERVICE_ACCOUNT_JSON=                 # raw service account JSON
GOOGLE_SERVICE_ACCOUNT_JSON_BASE64=          # base64-encoded service account JSON
GOOGLE_SERVICE_ACCOUNT_FILE=                 # path to a service account JSON file
# (GOOGLE_APPLICATION_CREDENTIALS is also accepted as a file path)
```

The service account must have **Editor** access to the sheet.

---

## Run Locally

```bash
python app.py
```

Then open `http://localhost:5000` to confirm the server is running, or `http://localhost:5000/health` for a status check.

> **Note:** Telegram webhooks require a public HTTPS URL. For local testing, use [ngrok](https://ngrok.com/) or deploy to Render.

---

## Telegram Webhook Setup

After deploying to Render, register your webhook by opening this URL in a browser (replace the placeholders):

```
https://api.telegram.org/botYOUR_TELEGRAM_BOT_TOKEN/setWebhook?url=https://YOUR-RENDER-APP.onrender.com/telegram/webhook&secret_token=YOUR_TELEGRAM_SECRET_TOKEN
```

You should receive `{"ok":true}` if the webhook was set successfully.

> Keep your bot token private. Do not share or commit it.

---

## Deploy to Render

### 1. Push to GitHub

```bash
git add .
git commit -m "Deploy Jal Yoga chatbot"
git push
```

### 2. Create a Render Web Service

1. Go to [render.com](https://render.com) and click **New → Web Service**
2. Connect your GitHub repository
3. Select your branch
4. Set the environment to **Python**

### 3. Build command

```bash
pip install -r requirements.txt
```

### 4. Start command

```bash
gunicorn app:app
```

### 5. Add environment variables

In Render: **Dashboard → Your Service → Environment**

Add all variables from the [Environment Variables](#environment-variables) section above, then redeploy.

---

## Routes

### Public routes

| Route | Method | Description |
|---|---|---|
| `/` | GET | Basic status page |
| `/health` | GET | Health/status check |
| `/telegram/webhook` | GET | Confirms the webhook route exists |
| `/telegram/webhook` | POST | Telegram webhook (used by Telegram) |

### Debug routes

All debug routes require your `DEBUG_ROUTE_TOKEN`.

Base URL:
```
https://YOUR-RENDER-APP.onrender.com/debug/ROUTE?token=YOUR_DEBUG_ROUTE_TOKEN
```

| Route | Description |
|---|---|
| `/debug/chat-log-file` | View the main chatlog file |
| `/debug/chatlogs` | List all per-customer chatlogs |
| `/debug/chatlog?chat_id=ID` | View one customer's chatlog |
| `/debug/test-chatlog` | Write a test chatlog entry |
| `/debug/outlets` | Check outlet contact config |
| `/debug/trial-bookings` | View trial booking summaries |

> *Changed from previous README:* there is no `/debug/config` route. Use `/health` for status, and `/debug/outlets` for outlet configuration.

---

## Helper Scripts

### `auto_render_chatlog.py`

Downloads the Render chatlog into VS Code every 5 seconds. Useful for monitoring live conversations without SSH access.

Update the two values at the top of the file, then run:

```bash
python auto_render_chatlog.py
```

Output files: `render_chat_log.json` and `render_chat_log.txt` (refresh every 5 seconds).

---

### `auto_knowledge.py`

Watches the `uploads/` folder and automatically extracts text from supported files into `knowledge.txt`.

Supported formats: `.pdf`, `.txt`, `.md`, `.docx`, `.csv`, `.xlsx`, `.pptx`, `.json`

```bash
python auto_knowledge.py
```

Drop files into `uploads/` and the content will be appended to `knowledge.txt`. Always review and clean the imported content before using it in production.

---

## Common GitHub Commands

```bash
# Check status
git status

# Stage, commit, and push changes
git add .
git commit -m "Update chatbot files"
git push
```

If `.env` was accidentally committed:

```bash
git rm --cached .env
git add .gitignore
git commit -m "Remove .env from tracking"
git push
```

Then immediately rotate any exposed API keys or tokens.

---

## Troubleshooting

### Bot does not reply

1. Confirm the Render service is running (no crashes in logs)
2. Verify the Telegram webhook is set and returns `{"ok":true}`
3. Check that `TELEGRAM_BOT_TOKEN` and `TELEGRAM_SECRET_TOKEN` match exactly
4. Look for `INCOMING TELEGRAM UPDATE` logs in Render

### OpenAI does not reply

1. Confirm `OPENAI_API_KEY` is set and valid
2. Confirm `OPENAI_MODEL` matches a model your key has access to
3. Redeploy Render after changing environment variables

### Chatlog not updating

1. Confirm `CHATLOG_ENABLED=true`
2. For local files, confirm `CHATLOG_LOCAL_ENABLED=true`
3. For Google Sheets, confirm `GOOGLE_SHEET_ID` and a credential variable are set, and the service account has Editor access
4. Open `/debug/test-chatlog?token=YOUR_TOKEN` to force a test entry
5. Check `/debug/chat-log-file?token=YOUR_TOKEN`

### Live Customer Service chat not relaying

1. Confirm `CUSTOMER_SERVICE_TELEGRAM_CHAT_ID` (or the relevant outlet chat ID) is set
2. Confirm CS staff are replying within the correct Telegram chat
3. Note that messages with blocked words are intentionally not relayed

### `requirements.txt` missing on Render

```bash
git add requirements.txt
git commit -m "Add requirements.txt"
git push
```

### Yellow squiggles in VS Code

```bash
pip install requests python-dotenv flask openai watchdog pymupdf python-docx openpyxl python-pptx gspread google-auth
```

Then press `Ctrl+Shift+P` → **Python: Select Interpreter** → pick your `venv`.

---

## Security Notes

**Never commit these files to GitHub:**

```
.env
chat_logs.jsonl
chatlogs/
render_chat_log.json
render_chat_log.txt
telegram_opt_out_users.json
processed_files.json
uploads/
```

**Keep these secret:**

- OpenAI API key
- Telegram bot token and secret token
- Debug route token
- Customer Service chat IDs
- Google service account credentials

The bot is designed never to request sensitive information (NRIC, card numbers, CVV, OTP, passwords, bank details, or medical documents) through the chat. It also redacts emails, OTP/password phrases, and long numbers from chatlogs, and filters offensive/blocked words from the live Customer Service relay.

---

## Recommended `.gitignore`

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