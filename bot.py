import asyncio
import hashlib
import html
import logging
import os
import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import BotCommand

from dotenv import load_dotenv

from db import init_db
from agnes_ai import VALID_CATEGORIES, extract_events

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_NAME = os.getenv("DB_NAME", "events_hub.db")
TARGET_CHAT = os.getenv("TARGET_CHAT", "@your_telegram_channel")

if not TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill in your own credentials."
    )

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Display-only emoji per category. Purely cosmetic - never used for matching,
# callback_data, or DB lookups, so it can't affect bot behavior.
CATEGORY_EMOJI = {
    "Workshops": "🛠️",
    "Career talks": "💼",
    "Internship opportunities": "🧑‍💻",
    "Scholarships": "🎓",
    "Competitions": "🏆",
    "Volunteer opportunities": "🤝",
    "CCA sign-ups": "🎭",
    "Fifth Row sign-ups": "🎬",
    "Networking opportunities": "🌐",
}


def category_label(category: str) -> str:
    """Return a category name prefixed with its display emoji, for UI text only."""
    return f"{CATEGORY_EMOJI.get(category, '📌')} {category}"


def build_category_picker_keyboard():
    """Build an inline keyboard with one tappable button per category.

    Tapping a button just picks that category directly (callback_data
    "select:<category>") - no slash command text is ever shown or sent.
    """
    builder = InlineKeyboardBuilder()
    for category in VALID_CATEGORIES:
        builder.button(text=category_label(category), callback_data=f"select:{category}")
    builder.adjust(2)
    return builder.as_markup()


async def subscribe_user(user_id: int, category: str) -> None:
    """Record that a user wants updates for the given category."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO user_preferences (user_id, category) VALUES (?, ?)",
            (user_id, category),
        )
        await db.commit()


def generate_event_id(title: str, date: str, raw_text: str = "") -> str:
    """Generate a deterministic event ID from the event details and source text."""
    hasher = hashlib.sha256()
    source = f"{str(title).strip().lower()}_{str(date).strip()}_{str(raw_text).strip().lower()}"
    hasher.update(source.encode("utf-8"))
    return hasher.hexdigest()


async def process_and_store_events(raw_data_sources):
    """Process incoming text and store it as an event in the local database."""
    events_stored = 0
    async with aiosqlite.connect(DB_NAME) as db:
        for raw_text in raw_data_sources:
            extracted_events = await extract_events(raw_text)
            for event in extracted_events:
                event_id = generate_event_id(event["title"], event["date"], raw_text)

                async with db.execute("SELECT id FROM events WHERE id = ?", (event_id,)) as cursor:
                    if await cursor.fetchone():
                        continue

                await db.execute(
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

        await db.commit()

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
    first_name = html.escape(message.from_user.first_name or "there")
    await message.answer(
        f"👋 <b>Welcome to the SUTD Event Hub Bot, {first_name}!</b>\n\n"
        "I'll notify you the moment a new event matching your interests is posted.\n\n"
        "<b>Tap a category below to follow it:</b>",
        reply_markup=build_category_picker_keyboard(),
    )
    await message.answer("Once you're following something, use <code>/menu</code> any time to view its listings.")

@dp.message(Command("select"))
async def select_category(message: types.Message):
    """Saves user interest mappings directly to the local preference table."""
    category_input = message.text.replace("/select", "").strip()

    if not category_input:
        await message.answer(
            "Tap a category to follow it:",
            reply_markup=build_category_picker_keyboard(),
        )
        return

    # Simple match check
    matched = next((c for c in VALID_CATEGORIES if c.lower() == category_input.lower()), None)
    if not matched:
        await message.answer("⚠️ <b>Category not recognized.</b>\nPlease copy the exact name from the options menu list.")
        return

    await subscribe_user(message.from_user.id, matched)
    await message.answer(
        f"✅ <b>Subscribed!</b> You'll now get updates for {category_label(matched)}.\n"
        "Type <code>/menu</code> to view your dashboard."
    )

@dp.callback_query(F.data.startswith("select:"))
async def handle_category_select(callback_query: types.CallbackQuery):
    """Handles a tap on one of the category-picker buttons - the actual selection action."""
    category = callback_query.data.split(":", 1)[1]
    await callback_query.answer() # Immediately dismiss Telegram loading animations

    if category not in VALID_CATEGORIES:
        await callback_query.message.answer("⚠️ Unrecognized category.")
        return

    await subscribe_user(callback_query.from_user.id, category)
    await callback_query.message.answer(
        f"✅ <b>Subscribed!</b> You'll now get updates for {category_label(category)}.\n"
        "Type <code>/menu</code> to view your dashboard."
    )

async def notify_subscribers(category: str, event_title: str, event_date: str, event_time: str, location: str, description: str) -> None:
    """Send a live update to all users who subscribed to the relevant category."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM user_preferences WHERE category = ?", (category,)) as cursor:
            subscribers = await cursor.fetchall()

    if not subscribers:
        return

    safe_title = html.escape(event_title)
    safe_date = html.escape(event_date)
    safe_time = html.escape(event_time) if event_time else "TBC"
    safe_location = html.escape(location) if location else "TBC"
    safe_description = html.escape(description) if description else "—"

    message_text = (
        f"📢 <b>New listing — {category_label(category)}</b>\n\n"
        f"<b>{safe_title}</b>\n"
        f"📅 {safe_date}   ⏰ {safe_time}\n"
        f"📍 {safe_location}\n\n"
        f"{safe_description}"
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
        await message.answer(
            "You haven't followed any categories yet.\nUse <code>/select &lt;category&gt;</code> first."
        )
        return

    builder = InlineKeyboardBuilder()
    for row in rows:
        category_name = row[0]
        # Callback data pattern payload: "view:<category_name>" - label is display-only.
        builder.button(text=category_label(category_name), callback_data=f"view:{category_name}")

    builder.adjust(1) # Renders one single full-width button per row
    await message.answer(
        "📋 <b>Your followed categories</b>\nTap one to see current listings:",
        reply_markup=builder.as_markup(),
    )

@dp.callback_query(F.data.startswith("view:"))
async def handle_category_view(callback_query: types.CallbackQuery):
    """Processes interactive selections to extract data updates corresponding to the query selection."""
    category = callback_query.data.split(":", 1)[1]
    await callback_query.answer() # Immediately dismiss Telegram loading animations

    if category not in VALID_CATEGORIES:
        await callback_query.message.answer("⚠️ Unrecognized category.")
        return

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT title, event_date, event_time, location, description FROM events WHERE category = ? ORDER BY event_date ASC",
            (category,)
        ) as cursor:
            events = await cursor.fetchall()

    if not events:
        await callback_query.message.answer(f"No current postings for {category_label(category)} yet.")
        return

    lines = [f"<b>{category_label(category)} — current listings</b>\n"]
    for title, event_date, event_time, location, description in events:
        lines.append(
            f"🔹 <b>{html.escape(title)}</b>\n"
            f"📅 {html.escape(event_date)}   ⏰ {html.escape(event_time) if event_time else 'TBC'}\n"
            f"📍 {html.escape(location) if location else 'TBC'}\n"
            f"{html.escape(description) if description else '—'}\n"
            "──────────────────"
        )

    await callback_query.message.answer("\n".join(lines))


def is_target_group_message(message: types.Message) -> bool:
    """Return True if the message was posted in the configured target group."""
    target_username = TARGET_CHAT.lstrip("@").strip().lower()
    return bool(target_username) and (message.chat.username or "").lower() == target_username


@dp.message(is_target_group_message)
async def handle_group_message(message: types.Message):
    """Capture new text messages posted in the target events group and store them as events."""
    text = message.text or message.caption or ""
    if not text:
        return

    logging.info("📥 Captured group message from chat %s", message.chat.id)
    events_stored = await process_and_store_events([text])
    if events_stored:
        logging.info("✅ Stored %d event(s) from group message.", events_stored)
    else:
        logging.info("ℹ️ Group message captured but no new event was stored.")


async def main():
    await init_db()
    await set_bot_commands(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())