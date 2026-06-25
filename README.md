# SUTD Events Bot

A Telegram bot that watches a SUTD events channel/group, extracts event details, stores them in a local SQLite database, and notifies subscribers when new events matching their chosen categories are posted.

## How it works

- `bot.py` runs the user-facing Telegram bot (aiogram). Users `/start`, `/select` a category to follow, and `/menu` to browse events. It also runs a Telethon listener that watches the target chat for new messages in real time.
- `ingest.py` is a standalone CLI for fetching message history from the target chat (backfill) or listening for new messages, independent of the bot process.
- `db.py` initializes the SQLite schema (`events` and `user_preferences` tables).
- Both `bot.py` and `ingest.py` call `call_agnes_ai()` to turn raw chat text into structured event fields (title, date, time, location, description, category). **This is currently a mock/placeholder** that returns hardcoded sample data — wire it up to your real Agnes AI (or any LLM) call before relying on it.

## Setup

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your own credentials:

   ```bash
   cp .env.example .env
   ```

   You'll need:
   - `TELEGRAM_BOT_TOKEN` — create a bot via [@BotFather](https://t.me/BotFather).
   - `TARGET_CHAT` — the channel/group the bot should read events from.
   - `TG_API_ID` / `TG_API_HASH` — from [my.telegram.org](https://my.telegram.org), used by Telethon to log in as a regular Telegram user account (required to read group history; bots alone can't do this).
   - `TG_PHONE` — the phone number of the Telegram account used for that login.
   - `TG_2FA_PASSWORD` — only if that account has two-factor authentication enabled.
   - `AGNES_API_KEY` — API key for whatever AI service you wire into `call_agnes_ai()`.

3. On first run, Telethon will prompt for a login code (and your 2FA password, if set) in the terminal to create a local `.session` file. This file is your login credential for that Telegram account — it's git-ignored and should never be committed or shared.

## Running

Start the bot (polling + live listener):

```bash
python bot.py
```

Or use the ingestion CLI directly:

```bash
python ingest.py --test              # verify access, print a few messages
python ingest.py --history --limit 50  # backfill recent message history
python ingest.py --listen            # listen for new messages only
```

## Security notes

- Never commit `.env`, `*.session` files, or the `*.db` file — they contain live credentials and user data. They're excluded via `.gitignore`.
- If you're picking up this repo from someone else, generate your own bot token, API ID/hash, and Agnes API key rather than reusing anyone else's.
