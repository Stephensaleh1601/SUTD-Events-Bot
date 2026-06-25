import asyncio
import logging
import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import BotCommand, KeyboardButton, ReplyKeyboardMarkup

from db import init_db

TOKEN = "8987900278:AAHuIiyzmqBFYF2T3IgwyS3KzG4sqR7NAUM"
DB_NAME = "events_hub.db"

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
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())