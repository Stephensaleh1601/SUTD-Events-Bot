import asyncio
import hashlib
import logging
import os
import sqlite3
import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import BotCommand, KeyboardButton, ReplyKeyboardMarkup
from telethon import TelegramClient, events
from telethon.tl.types import MessageService

from db import init_db

TOKEN = "8987900278:AAHuIiyzmqBFYF2T3IgwyS3KzG4sqR7NAUM"
DB_NAME = "events_hub.db"
TARGET_CHAT = "@sutdevents"
TG_API_ID = 9550531
TG_API_HASH = "538f9520f27e1f371a479b0b615a9baa"
TG_PHONE = "+6597286100"
TG_2FA_PASSWORD = "Xiaopeng1601!"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

VALID_CATEGORIES = [
    "Workshops", "Career talks", "Internship opportunities", 
    "Scholarships", "Competitions", "Volunteer opportunities", 
    "CCA sign-ups", "Fifth Row sign-ups", "Networking opportunities"
]

QUICK_COMMANDS_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="/select Workshops"), KeyboardButton(text="/select Career talks")],
        [KeyboardButton(text="/select Internship opportunities"), KeyboardButton(text="/select Scholarships")],
        [KeyboardButton(text="/select Competitions"), KeyboardButton(text="/select Volunteer opportunities")],
        [KeyboardButton(text="/select CCA sign-ups"), KeyboardButton(text="/select Fifth Row sign-ups")],
        [KeyboardButton(text="/select Networking opportunities")],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)


def generate_event_id(title: str, date: str, raw_text: str = "") -> str:
    """Generate a deterministic event ID from the event details and source text."""
    hasher = hashlib.sha256()
    source = f"{str(title).strip().lower()}_{str(date).strip()}_{str(raw_text).strip().lower()}"
    hasher.update(source.encode("utf-8"))
    return hasher.hexdigest()


def get_input(prompt: str, default: str = "") -> str:
    """Prompt the user for input and return a non-empty value."""
    value = input(prompt).strip()
    return value or default


async def login_with_telethon(api_id: int, api_hash: str, phone: str) -> TelegramClient:
    """Log in to Telegram with a user account and return a Telethon client."""
    session_name = "events_session_" + "".join(ch for ch in phone if ch.isalnum())
    session_path = os.path.abspath(f"{session_name}.session")
    if os.path.exists(session_path):
        try:
            os.remove(session_path)
        except OSError:
            pass
    client = TelegramClient(session_path, api_id, api_hash)

    try:
        await client.start(
            phone=phone,
            code_callback=lambda: get_input("Enter the Telegram login code: "),
            password=lambda: TG_2FA_PASSWORD or get_input("Enter your 2FA password (leave blank if none): "),
        )
        return client
    except Exception as exc:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise RuntimeError(
            "Telegram login failed. Please make sure you are using the correct phone number, that the code is entered once, and that your account allows sign-in from this environment."
        ) from exc


async def register_telethon_listener(target_chat: str):
    """Register a Telethon listener that captures new group messages and stores them as events."""
    api_id = int(os.getenv("TG_API_ID") or TG_API_ID)
    api_hash = os.getenv("TG_API_HASH") or TG_API_HASH
    phone = os.getenv("TG_PHONE") or TG_PHONE

    client = await login_with_telethon(api_id, api_hash, phone)
    entity = await client.get_entity(target_chat)

    @client.on(events.NewMessage(chats=[entity]))
    async def handle_new_group_message(event):
        if isinstance(event.message, MessageService):
            return

        text = event.raw_text or event.message.message or ""
        if not text:
            return

        print(f"\n📥 Telethon captured group message: {text[:1000]}")
        logging.info("Telethon captured group message from %s", event.chat_id)

        events_stored = await process_and_store_events([text])
        if events_stored:
            print(f"✅ Stored {events_stored} event(s) from Telethon message.")
        else:
            print("ℹ️ Telethon message captured but no new event was stored.")

    return client


def call_agnes_ai(raw_text: str):
    """Create a simple event payload from a group message for now."""
    return [
        {
            "title": "AI & Robotics Workshop 2026",
            "date": "2026-07-15",
            "time": "14:00 - 17:00",
            "location": "Campus Tech Lab Room 4",
            "description": raw_text[:200] + "..." if len(raw_text) > 200 else raw_text,
            "category": "Workshops",
        }
    ]


async def process_and_store_events(raw_data_sources):
    """Process incoming text and store it as an event in the local database."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    events_stored = 0
    for raw_text in raw_data_sources:
        extracted_events = call_agnes_ai(raw_text)
        for event in extracted_events:
            event_id = generate_event_id(event["title"], event["date"], raw_text)
            cursor.execute("SELECT id FROM events WHERE id = ?", (event_id,))
            if cursor.fetchone():
                continue

            cursor.execute(
                """
                INSERT INTO events (id, title, event_date, event_time, location, description, category)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event["title"],
                    event["date"],
                    event.get("time", ""),
                    event.get("location", ""),
                    event.get("description", ""),
                    event.get("category", "General"),
                ),
            )
            events_stored += 1
            logging.info("💾 Stored new event: %s", event["title"])
            await notify_subscribers(
                event.get("category", "General"),
                event["title"],
                event["date"],
                event.get("time", ""),
                event.get("location", ""),
                event.get("description", ""),
            )

    conn.commit()
    conn.close()
    return events_stored


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Start the bot and see the welcome message"),
        BotCommand(command="menu", description="View your saved event categories"),
        BotCommand(command="select", description="Follow a specific event category"),
    ])

@dp.message(Command("start"))
async def command_start_handler(message: types.Message):
    """Initializes onboarding sequences by allowing users to toggle their preferred interests."""
    await message.answer(
        f"Welcome to the Event Hub Bot, {message.from_user.first_name}!\n\n"
        "Please configure your preferred categories by using the commands below:\n"
        "• Use `/select <Category Name>` to follow an entry.\n"
        "• Use `/menu` to check matching event listings once configured.\n\n"
        "**Available options:**\n" + "\n".join([f"- {c}" for c in VALID_CATEGORIES]),
        reply_markup=QUICK_COMMANDS_KEYBOARD,
    )

@dp.message(Command("select"))
async def select_category(message: types.Message):
    """Saves user interest mappings directly to the local preference table."""
    category_input = message.text.replace("/select", "").strip()
    
    # Simple match check
    matched = next((c for c in VALID_CATEGORIES if c.lower() == category_input.lower()), None)
    if not matched:
        await message.answer("⚠️ Category not recognized. Please copy the exact name from the options menu list.")
        return
        
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO user_preferences (user_id, category) VALUES (?, ?)",
            (message.from_user.id, matched)
        )
        await db.commit()
        
    await message.answer(f"✅ Subscribed to updates under: **{matched}**! Type `/menu` to view dashboard.")

@dp.message(F.text.in_(["/start", "/menu"] + [f"/select {cat}" for cat in VALID_CATEGORIES]))
async def quick_command_handler(message: types.Message):
    """Handles the quick-access command buttons shown in the keyboard."""
    if message.text == "/start":
        await command_start_handler(message)
        return
    if message.text == "/menu":
        await show_menu(message)
        return
    if message.text.startswith("/select"):
        await select_category(message)

async def notify_subscribers(category: str, event_title: str, event_date: str, event_time: str, location: str, description: str) -> None:
    """Send a live update to all users who subscribed to the relevant category."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM user_preferences WHERE category = ?", (category,)) as cursor:
            subscribers = await cursor.fetchall()

    if not subscribers:
        return

    message_text = (
        f"📢 New event in {category}: *{event_title}*\n"
        f"📅 {event_date} | ⏰ {event_time}\n"
        f"📍 {location}\n"
        f"📝 {description}\n\n"
        "Please take a look!"
    )

    for (user_id,) in subscribers:
        try:
            await bot.send_message(chat_id=user_id, text=message_text)
        except Exception as exc:
            logging.warning("Failed to notify user %s: %s", user_id, exc)


@dp.message(Command("menu"))
async def show_menu(message: types.Message):
    """Constructs dynamic customized menus displaying only opted-in channels."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT category FROM user_preferences WHERE user_id = ?", (message.from_user.id,)) as cursor:
            rows = await cursor.fetchall()
            
    if not rows:
        await message.answer("You haven't customized your configuration profiles yet. Use `/select <category>` first.")
        return

    builder = InlineKeyboardBuilder()
    for row in rows:
        category_name = row[0]
        # Callback data pattern payload: "view:<category_name>"
        builder.button(text=category_name, callback_data=f"view:{category_name}")
    
    builder.adjust(1) # Renders one single full-width button per row
    await message.answer(
        "Select an event channel to review current notices:",
        reply_markup=builder.as_markup(),
    )

@dp.callback_query(F.data.startswith("view:"))
async def handle_category_view(callback_query: types.CallbackQuery):
    """Processes interactive selections to extract data updates corresponding to the query selection."""
    category = callback_query.data.split(":")[1]
    await callback_query.answer() # Immediately dismiss Telegram loading animations

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT title, event_date, event_time, location, description FROM events WHERE category = ? ORDER BY event_date ASC", 
            (category,)
        ) as cursor:
            events = await cursor.fetchall()

    if not events:
        await callback_query.message.answer(f"No current postings discovered for category: *{category}*.")
        return

    response_text = f"📋 **Current events cataloged under {category}:**\n\n"
    for item in events:
        response_text += (
            f"🔹 **{item[0]}**\n"
            f"📅 Date: {item[1]} | ⏰ Time: {item[2]}\n"
            f"📍 Location: {item[3]}\n"
            f"📝 {item[4]}\n"
            f"---------------------------\n"
        )
        
    await callback_query.message.answer(response_text)

async def main():
    await init_db()
    await set_bot_commands(bot)

    client = await register_telethon_listener(TARGET_CHAT)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        polling_task = asyncio.create_task(dp.start_polling(bot))
        await client.run_until_disconnected()
        polling_task.cancel()
        await asyncio.gather(polling_task, return_exceptions=True)
    finally:
        await client.disconnect()
        await dp.stop_polling()

if __name__ == "__main__":
    asyncio.run(main())