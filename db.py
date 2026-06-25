import aiosqlite

DB_NAME = "events_hub.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # Table to hold processed events
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                title TEXT,
                event_date TEXT,
                event_time TEXT,
                location TEXT,
                description TEXT,
                category TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Table to store individual user preferences
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id INTEGER,
                category TEXT,
                PRIMARY KEY (user_id, category)
            )
        """)
        await db.commit()

if __name__ == "__main__":
    import asyncio
    asyncio.run(init_db())