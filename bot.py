import asyncio
import hashlib
import html
import logging
import os
from datetime import date, datetime

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


def build_subscription_checkbox_keyboard(subscribed: set):
    """Build a checkbox-style inline keyboard for every category.

    Each button is prefixed with ✅ (subscribed) or ⬜ (not subscribed) based on
    `subscribed`, and tapping it toggles that one category on/off (callback_data
    "toggle:<category>") - this is what /menu uses to manage subscriptions.
    """
    builder = InlineKeyboardBuilder()
    for category in VALID_CATEGORIES:
        mark = "✅" if category in subscribed else "⬜"
        builder.button(text=f"{mark} {category_label(category)}", callback_data=f"toggle:{category}")
    builder.adjust(2)
    return builder.as_markup()


async def get_user_categories(user_id: int) -> set:
    """Return the set of categories a user currently subscribes to."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT category FROM user_preferences WHERE user_id = ?", (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def subscribe_user(user_id: int, category: str) -> None:
    """Record that a user wants updates for the given category."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO user_preferences (user_id, category) VALUES (?, ?)",
            (user_id, category),
        )
        await db.commit()


async def unsubscribe_user(user_id: int, category: str) -> None:
    """Remove a user's subscription to the given category."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "DELETE FROM user_preferences WHERE user_id = ? AND category = ?",
            (user_id, category),
        )
        await db.commit()


def generate_event_id(title: str, date: str) -> str:
    """Generate a deterministic event ID from the event's title and date.

    Deliberately based on title+date only (not the raw source text), so the
    same real-world event reposted/reworded - or captured once live via
    bot.py and once via ingest.py's history backfill - always hashes to the
    same ID and can't be double-stored. This is the single source of truth
    for ID generation; ingest.py imports it rather than keeping its own copy.
    """
    hasher = hashlib.sha256()
    source = f"{str(title).strip().lower()}_{str(date).strip()}"
    hasher.update(source.encode("utf-8"))
    return hasher.hexdigest()


async def process_and_store_events(raw_data_sources):
    """Process incoming text and store it as an event in the local database."""
    events_stored = 0
    async with aiosqlite.connect(DB_NAME) as db:
        for raw_text in raw_data_sources:
            extracted_events = await extract_events(raw_text)
            for event in extracted_events:
                event_id = generate_event_id(event["title"], event["date"])

                async with db.execute("SELECT id FROM events WHERE id = ?", (event_id,)) as cursor:
                    if await cursor.fetchone():
                        continue

                await db.execute(
                    """
                    INSERT INTO events (id, title, event_date, event_time, location, description, category, link)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event["title"],
                        event["date"],
                        event.get("time", ""),
                        event.get("location", ""),
                        event.get("description", ""),
                        event.get("category", "General"),
                        event.get("link", ""),
                    ),
                )
                events_stored += 1
                logging.info("💾 Stored new event: %s [%s]", event["title"], event.get("category", "General"))
                await notify_subscribers(
                    event.get("category", "General"),
                    event["title"],
                    event["date"],
                    event.get("time", ""),
                    event.get("location", ""),
                    event.get("description", ""),
                    event.get("link", ""),
                )

        await db.commit()

    return events_stored


def _parse_event_date(date_str: str):
    """Parse an event's date string into a date object, or None if it can't be read.

    Agnes AI is prompted to always return YYYY-MM-DD, so that's tried first; a couple
    of fallback formats are tried too in case of older/odd data. Unparseable dates are
    left alone by callers rather than guessed at - never delete on uncertainty.
    """
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


async def delete_expired_events(db_name: str = None) -> int:
    """Delete events whose date has already passed (i.e. it's now the day after, or later).

    A past event/sign-up isn't useful to show subscribers or keep around, so this is
    run on bot startup and on a recurring loop (see expire_events_loop) to keep the
    database and /menu listings from accumulating stale entries. Returns the number
    of rows removed. Rows with an unparseable date are left in place rather than
    risking deleting something live.
    """
    db_name = db_name or DB_NAME
    today = date.today()
    async with aiosqlite.connect(db_name) as db:
        async with db.execute("SELECT id, event_date FROM events") as cursor:
            rows = await cursor.fetchall()

        expired_ids = []
        for event_id, event_date in rows:
            parsed = _parse_event_date(event_date)
            if parsed is not None and parsed < today:
                expired_ids.append(event_id)

        if expired_ids:
            await db.executemany("DELETE FROM events WHERE id = ?", [(i,) for i in expired_ids])
            await db.commit()

    return len(expired_ids)


async def expire_events_loop(interval_seconds: int = 24 * 60 * 60) -> None:
    """Background task: periodically purge events whose date has passed.

    Runs immediately on startup (to catch anything that expired while the bot was
    offline), then once every `interval_seconds` (default 24h) for as long as the
    bot keeps running.
    """
    while True:
        try:
            removed = await delete_expired_events()
            if removed:
                logging.info("🗑️ Removed %d expired event(s).", removed)
        except Exception as exc:
            logging.warning("Expired-event cleanup failed: %s", exc)
        await asyncio.sleep(interval_seconds)


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Start the bot and see the welcome message"),
        BotCommand(command="menu", description="Subscribe/unsubscribe to event categories"),
        BotCommand(command="listings", description="Browse current listings for categories you follow"),
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
    await message.answer(
        "Use <code>/menu</code> any time to subscribe or unsubscribe, "
        "and <code>/listings</code> to see what's currently posted."
    )

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
        "Use <code>/menu</code> to manage your subscriptions, or <code>/listings</code> to see what's posted."
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
        "Use <code>/menu</code> to manage your subscriptions, or <code>/listings</code> to see what's posted."
    )

async def notify_subscribers(category: str, event_title: str, event_date: str, event_time: str, location: str, description: str, link: str = "") -> None:
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
    if link:
        # html.escape (quote=True by default) also escapes " and ' so the link
        # can't break out of the href="..." attribute it's placed in.
        message_text += f"\n\n🔗 <a href=\"{html.escape(link)}\">Sign up here</a>"

    for (user_id,) in subscribers:
        try:
            await bot.send_message(chat_id=user_id, text=message_text)
        except Exception as exc:
            logging.warning("Failed to notify user %s: %s", user_id, exc)


@dp.message(Command("menu"))
async def show_menu(message: types.Message):
    """Show every category as a checkbox the user can tap to subscribe/unsubscribe."""
    subscribed = await get_user_categories(message.from_user.id)
    await message.answer(
        "📋 <b>Manage your categories</b>\nTap a category to subscribe or unsubscribe:",
        reply_markup=build_subscription_checkbox_keyboard(subscribed),
    )

@dp.callback_query(F.data.startswith("toggle:"))
async def handle_category_toggle(callback_query: types.CallbackQuery):
    """Handles a tap on a /menu checkbox - toggles that subscription and updates the checkbox in place."""
    category = callback_query.data.split(":", 1)[1]

    if category not in VALID_CATEGORIES:
        await callback_query.answer("⚠️ Unrecognized category.", show_alert=True)
        return

    user_id = callback_query.from_user.id
    subscribed = await get_user_categories(user_id)

    if category in subscribed:
        await unsubscribe_user(user_id, category)
        subscribed.discard(category)
        await callback_query.answer(f"Unsubscribed from {category}")
    else:
        await subscribe_user(user_id, category)
        subscribed.add(category)
        await callback_query.answer(f"Subscribed to {category}")

    await callback_query.message.edit_reply_markup(
        reply_markup=build_subscription_checkbox_keyboard(subscribed)
    )

@dp.message(Command("listings"))
async def show_listings(message: types.Message):
    """Show inline buttons for the categories a user follows, to browse current listings."""
    subscribed = await get_user_categories(message.from_user.id)

    if not subscribed:
        await message.answer(
            "You haven't subscribed to any categories yet.\nUse <code>/menu</code> to pick some."
        )
        return

    builder = InlineKeyboardBuilder()
    for category_name in sorted(subscribed):
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
            "SELECT title, event_date, event_time, location, description, link FROM events WHERE category = ? ORDER BY event_date ASC",
            (category,)
        ) as cursor:
            events = await cursor.fetchall()

    if not events:
        await callback_query.message.answer(f"No current postings for {category_label(category)} yet.")
        return

    lines = [f"<b>{category_label(category)} — current listings</b>\n"]
    for title, event_date, event_time, location, description, link in events:
        entry = (
            f"🔹 <b>{html.escape(title)}</b>\n"
            f"📅 {html.escape(event_date)}   ⏰ {html.escape(event_time) if event_time else 'TBC'}\n"
            f"📍 {html.escape(location) if location else 'TBC'}\n"
            f"{html.escape(description) if description else '—'}"
        )
        if link:
            entry += f"\n🔗 <a href=\"{html.escape(link)}\">Sign up here</a>"
        entry += "\n──────────────────"
        lines.append(entry)

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
    asyncio.create_task(expire_events_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())