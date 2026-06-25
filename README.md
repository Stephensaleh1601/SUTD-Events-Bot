# SUTD Events Bot

A Telegram bot that watches a SUTD events channel/group, extracts event details, stores them in a local SQLite database, and notifies subscribers when new events matching their chosen categories are posted.

## How it works

- `bot.py` runs the user-facing Telegram bot (aiogram). `/start` shows a tappable inline-button picker of categories to follow (no slash commands to type or remember); `/select <category>` also works manually as a fallback. `/menu` shows every category as a checkbox (✅ subscribed / ⬜ not) — tapping one toggles that subscription on or off in place, no separate subscribe/unsubscribe commands needed. `/listings` shows inline buttons for the categories a user currently follows and lets them browse current listings. The bot also runs a background loop that purges events whose date has already passed (checked on startup, then every 24h), so stale postings don't linger. The bot is an admin member of the target group, so it picks up new group messages directly through its own aiogram handler — no separate login or listener process needed.
- `ingest.py` is a standalone CLI for backfilling older message history from the target chat. Since the Bot API can't fetch history older than when the bot joined, this script logs in as a regular Telegram user account via Telethon to pull past messages. It can also run a live Telethon listener as an alternative to the bot, but that's no longer necessary now that the bot is in the group. It also exposes `--dedupe` and `--expire` for on-demand database maintenance (see Running below).
- `dedupe.py` removes duplicate events: an exact-match pass (same title+date) plus an Agnes AI semantic pass that catches reworded/reposted duplicates the exact-match pass would miss.
- `db.py` initializes the SQLite schema (`events` and `user_preferences` tables).
- `agnes_ai.py` calls the [Agnes AI](https://agnes-ai.com) chat completions API (`agnes-2.0-flash` by default) to turn raw chat text into structured event fields (title, date, time, location, description, category — matched case-insensitively against the valid category list — and link). It returns an empty list when a message doesn't actually announce an event, so general chatter in the group doesn't get stored as a fake event. Both `bot.py` and `ingest.py` import this module rather than duplicating the API call.
- If a message includes a sign-up/registration URL, it's extracted as the event's `link` and shown as a "🔗 Sign up here" tap-through in both new-event notifications and `/listings`. Only `http(s)://` links are kept — anything else the model returns is dropped rather than stored.

## Setup

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your own credentials:

   ```bash
   cp .env.example .env
   ```

   To run `bot.py` you'll need:
   - `TELEGRAM_BOT_TOKEN` — create a bot via [@BotFather](https://t.me/BotFather), then add it to the target group as an admin (so it can read group messages).
   - `TARGET_CHAT` — the group's public `@username` the bot should read events from. The bot matches incoming messages by `chat.username`, so the group needs a public username.
   - `AGNES_API_KEY` — your API key from [agnes-ai.com](https://agnes-ai.com), used by `agnes_ai.py` to extract event data. Without it, group messages are still captured but no events will be extracted (a warning is logged).
   - `AGNES_MODEL` / `AGNES_API_BASE` — optional overrides; default to `agnes-2.0-flash` and `https://apihub.agnes-ai.com/v1`.

   To use `ingest.py`'s `--history`/`--listen` backfill modes, you'll additionally need these (not in `.env.example` — add them to your `.env` manually):
   - `TG_API_ID` / `TG_API_HASH` — from [my.telegram.org](https://my.telegram.org), used by Telethon to log in as a regular Telegram user account.
   - `TG_PHONE` — the phone number of the Telegram account used for that login.
   - `TG_2FA_PASSWORD` — only if that account has two-factor authentication enabled.

3. If you use `ingest.py`'s Telethon-based modes, the first run will prompt for a login code (and your 2FA password, if set) in the terminal to create a local `.session` file. This file is your login credential for that Telegram account — it's git-ignored and should never be committed or shared.

## Running

Start the bot (polling, including new group messages):

```bash
python bot.py
```

Or backfill/listen via the ingestion CLI (requires the Telethon env vars above):

```bash
python ingest.py --test              # verify access, print a few messages
python ingest.py --history --limit 50  # backfill recent message history
python ingest.py --listen            # listen for new messages only
```

Database maintenance (pure DB operations - no Telethon/Telegram credentials needed; the running bot also does both of these automatically):

```bash
python ingest.py --dedupe   # remove duplicate events (exact-match, then Agnes AI semantic pass)
python ingest.py --expire   # remove events whose date has already passed
```

## Security notes

- Never commit `.env`, `*.session` files, or the `*.db` file — they contain live credentials and user data. They're excluded via `.gitignore`.
- If you're picking up this repo from someone else, generate your own bot token, API ID/hash, and Agnes API key rather than reusing anyone else's.
