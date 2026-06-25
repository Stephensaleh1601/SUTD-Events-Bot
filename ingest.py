import argparse
import asyncio
import hashlib
import sqlite3
import datetime
import os
import logging
from aiogram import Bot, Dispatcher, types
from telethon import TelegramClient
from telethon.tl.types import MessageService
from dotenv import load_dotenv

from bot import notify_subscribers

load_dotenv()

# ==========================================
# 1. CONFIGURATION
# ==========================================
# Telegram Bot Token (from @BotFather) - set in .env
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# The target chat. Can be a public username (e.g., 'sutd_events'),
# a Chat ID (integer), or a username prefixed with '@'.
# Note: The bot MUST be a member of this chat to read messages.
TARGET_CHAT = os.getenv("TARGET_CHAT", "@your_telegram_channel")

# Telegram user-account credentials - set in .env (see .env.example)
TG_API_ID = os.getenv("TG_API_ID", "0")
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_PHONE = os.getenv("TG_PHONE", "")
TG_2FA_PASSWORD = os.getenv("TG_2FA_PASSWORD", "")

# Agnes AI & DB Config - set in .env
AGNES_API_KEY = os.getenv("AGNES_API_KEY", "")
DB_NAME = os.getenv("DB_NAME", "events_hub.db")

if not BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill in your own credentials."
    )

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# 2. HELPER FUNCTIONS
# ==========================================
def generate_event_id(title, date):
    """Generate a unique ID for an event based on title and date."""
    hasher = hashlib.sha256()
    hasher.update(f"{str(title).strip().lower()}_{str(date).strip()}".encode('utf-8'))
    return hasher.hexdigest()

def call_agnes_ai(raw_text):
    """
    Placeholder for your actual Agnes AI API POST request.
    
    TODO: Replace this with your real Agnes AI API call.
    Currently returns mock data to test the database flow.
    
    Expected return format:
    [
        {
            "title": "Event Title",
            "date": "YYYY-MM-DD",
            "time": "HH:MM - HH:MM",
            "location": "Event Location",
            "description": "Full event description",
            "category": "Category Name"
        },
        ...
    ]
    """
    # --- MOCK DATA FOR TESTING ---
    # Replace this block with your real API call
    return [
        {
            "title": "AI & Robotics Workshop 2026",
            "date": "2026-07-15",
            "time": "14:00 - 17:00",
            "location": "Campus Tech Lab Room 4",
            "description": raw_text[:200] + "..." if len(raw_text) > 200 else raw_text,
            "category": "Workshops"
        }
    ]
    # -----------------------------

def process_and_store_events(raw_data_sources):
    """Process raw text data through Agnes AI and store events in SQLite."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    events_stored = 0
    for raw_text in raw_data_sources:
        extracted_events = call_agnes_ai(raw_text)
        
        for event in extracted_events:
            event_id = generate_event_id(event['title'], event['date'])
            
            # Skip duplicates
            cursor.execute("SELECT id FROM events WHERE id = ?", (event_id,))
            if cursor.fetchone():
                continue
                
            cursor.execute("""
                INSERT INTO events (id, title, event_date, event_time, location, description, category)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                event_id,
                event['title'],
                event['date'],
                event.get('time', ''),
                event.get('location', ''),
                event.get('description', ''),
                event.get('category', 'General')
            ))
            events_stored += 1
            logger.info(f"💾 Stored new event: {event['title']}")
            asyncio.create_task(notify_subscribers(
                event.get('category', 'General'),
                event['title'],
                event['date'],
                event.get('time', ''),
                event.get('location', ''),
                event.get('description', ''),
            ))
            
    conn.commit()
    conn.close()
    return events_stored

# ==========================================
# 3. TELEGRAM MESSAGE FETCHER (using aiogram)
# ==========================================
async def verify_chat_access(bot: Bot, target_chat: str):
    """Check that the bot can see the target chat and print its details."""
    try:
        chat = await bot.get_chat(chat_id=target_chat)
        title = chat.title or chat.username or chat.id
        logger.info(f"✅ Bot can access chat: {title}")
        print(f"✅ Bot can access chat: {title}")
        return chat
    except Exception as e:
        logger.error(f"❌ Cannot access chat '{target_chat}': {e}")
        print(f"❌ Cannot access chat '{target_chat}': {e}")
        return None


def is_target_chat(message: types.Message, target_chat: str) -> bool:
    """Return True when the message comes from the configured target chat."""
    target_chat = str(target_chat).strip()
    if not target_chat:
        return False

    if target_chat.startswith("@"):
        return (message.chat.username or "").lower() == target_chat[1:].lower()

    try:
        return str(message.chat.id) == target_chat
    except Exception:
        return False


def register_group_listener(dp: Dispatcher, target_chat: str):
    """Register a handler that captures incoming messages from the target group."""

    @dp.message()
    async def handle_group_message(message: types.Message):
        if not is_target_chat(message, target_chat):
            return

        text = message.text or message.caption or ""
        if not text:
            return

        print(f"\n📥 Message from group chat: {text[:1000]}")
        logger.info("Captured group message from %s", message.chat.id)

        # Send the incoming group message into the same processing pipeline used by the ingestion flow.
        events_stored = process_and_store_events([text])
        if events_stored:
            print(f"✅ Stored {events_stored} event(s) from incoming group message.")
        else:
            print("ℹ️ Message captured but no new event was stored.")

    return handle_group_message

def get_input(prompt: str, default: str = "") -> str:
    """Prompt the user for input and return a non-empty value."""
    value = input(prompt).strip()
    return value or default


def normalize_target_chat(target_chat):
    """Convert numeric chat IDs to integers so Telethon resolves them correctly."""
    value = str(target_chat).strip()
    if not value:
        return None
    if value.startswith("@"):
        return value
    try:
        return int(value)
    except ValueError:
        return value


async def login_with_telethon(api_id: int, api_hash: str, phone: str):
    """Log in to Telegram with a user account and return a Telethon client."""
    session_name = "events_session_" + "".join(ch for ch in phone if ch.isalnum())
    client = TelegramClient(session_name, api_id, api_hash)

    try:
        await client.start(
            phone=phone,
            code_callback=lambda: get_input("Enter the Telegram login code: "),
            password=lambda: TG_2FA_PASSWORD or get_input("Enter your 2FA password (leave blank if none): "),
        )
        return client
    except Exception as exc:
        await client.disconnect()
        raise RuntimeError(
            "Telegram login failed. Please make sure you are using the correct phone number, \
            that the code is entered once, and that your account allows sign-in from this environment."
        ) from exc

async def fetch_telegram_messages(limit=50, offset_id=0, target_chat=TARGET_CHAT):
    """Fetch existing messages from the target chat using a logged-in user account via Telethon."""
    api_id = os.getenv("TG_API_ID") or TG_API_ID
    api_hash = os.getenv("TG_API_HASH") or TG_API_HASH
    phone = os.getenv("TG_PHONE") or TG_PHONE

    try:
        api_id_int = int(api_id)
    except ValueError:
        logger.error("API ID must be an integer.")
        return []

    normalized_chat = normalize_target_chat(target_chat)
    logger.info(f"📡 Logging in with Telethon and fetching up to {limit} messages from chat '{normalized_chat}'...")
    client = None
    try:
        client = await login_with_telethon(api_id_int, api_hash, phone)
        entity = await client.get_entity(normalized_chat)
        messages = await client.get_messages(entity, limit=limit, offset_id=offset_id)

        scraped_texts = []
        for msg in messages:
            if isinstance(msg, MessageService):
                continue
            text = msg.text or msg.caption or ""
            if text:
                scraped_texts.append(text)

        logger.info(f"✅ Retrieved {len(scraped_texts)} text/caption messages.")
        return scraped_texts
    except Exception as e:
        logger.error(f"❌ Error fetching messages from chat '{target_chat}': {e}")
        return []
    finally:
        if client:
            await client.disconnect()

async def fetch_first_messages(limit=5, target_chat=TARGET_CHAT):
    """Test mode: verify access and print the first few messages using a user account."""
    logger.info(f"🧪 Test mode: checking access to '{target_chat}' using Telethon...")
    messages = await fetch_telegram_messages(limit=limit, target_chat=target_chat)
    if not messages:
        print("ℹ️ No messages were retrieved. The login may have failed or the chat may be inaccessible.")
        return []

    for index, text in enumerate(messages, 1):
        print(f"\n[{index}/{len(messages)}]")
        print(text[:1000])

    return messages

# ==========================================
# 4. MAIN ENTRY POINT
# ==========================================
async def main():
    parser = argparse.ArgumentParser(description="Telegram event ingestion utility")
    parser.add_argument("--test", action="store_true", help="Verify access to the target chat and print a few messages")
    parser.add_argument("--history", action="store_true", help="Use a user account to fetch existing messages from the target group")
    parser.add_argument("--listen", action="store_true", help="Listen for new messages from the target group")
    parser.add_argument("--limit", type=int, default=50, help="Number of messages to fetch (default: 50)")
    parser.add_argument("--chat", default=TARGET_CHAT, help="Override the target chat ID/username")
    parser.add_argument("--chat-value", dest="chat_value", default=None, help="Alternative way to pass the target chat when the @ symbol is problematic")
    parser.add_argument("--offset", type=int, default=0, help="Message ID offset for pagination")
    args = parser.parse_args()

    bot = Bot(token=BOT_TOKEN)
    chat_target = args.chat_value or args.chat

    if args.test:
        await fetch_first_messages(limit=min(args.limit, 10), target_chat=chat_target)
        return

    if args.history:
        print("Starting history sync from Telegram...")
        raw_messages = await fetch_telegram_messages(limit=min(args.limit, 200), target_chat=chat_target)
        if not raw_messages:
            print("No messages found to process.")
            return
        print(f"Passing {len(raw_messages)} messages to Agnes AI...")
        events_stored = process_and_store_events(raw_messages)
        print(f"History sync complete! 🚀 {events_stored} new events stored.")
        return

    if args.listen:
        dp = Dispatcher()
        register_group_listener(dp, chat_target)
        await verify_chat_access(bot, chat_target)
        print(f"Listening for new messages in chat '{chat_target}'... Press Ctrl+C to stop.")
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
        return

    print("Starting data ingestion cycle...")
    
    # 1. Scrape Telegram
    raw_messages = await fetch_telegram_messages(
        limit=min(args.limit, 100),
        offset_id=args.offset,
        target_chat=chat_target
    )
    
    if not raw_messages:
        print("No new messages found to process.")
        return

    # 2. Process through AI and Database
    print(f"Passing {len(raw_messages)} messages to Agnes AI...")
    events_stored = process_and_store_events(raw_messages)
    print(f"Ingestion cycle complete! 🚀 {events_stored} new events stored.")

if __name__ == "__main__":
    asyncio.run(main())