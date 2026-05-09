# Jal Yoga Singapore WhatsApp Bot

A Python-based WhatsApp chatbot for Jal Yoga Singapore using OpenAI and Flask.

This bot:
- replies to customer questions on WhatsApp
- uses `knowledge.txt` as the main knowledge source
- supports trial booking, member enquiries, studio locations, operating hours, policies, corporate enquiries, and staff hub requests
- hands difficult or account-specific cases to customer service

---

## Project Type

This is an **LLM-first Python chatbot**.

- **Python** handles:
  - WhatsApp webhook
  - sending and receiving messages
  - loading environment variables
  - chat history
  - handoff logging

- **LLM** handles:
  - most of the conversation
  - trial flow
  - studio and address questions
  - policy questions
  - member questions
  - refer-a-friend flow
  - corporate flow
  - staff hub flow

---

## Main Files

```bash
app.py
knowledge.txt
test_bot.py
requirements.txt
.env
templates/index.html