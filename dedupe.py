"""Deduplication helpers for the `events` table.

Two passes, meant to be run together (see ingest.py's `--dedupe` flag):

1. Exact-match (`dedupe_exact`): groups events by a normalized (title, date) key and
   collapses each group down to its earliest-inserted row. Pure code, no API calls -
   cheap, deterministic, and catches reposts/test-data duplicates outright. Going
   forward this case shouldn't recur in practice, since bot.py's generate_event_id
   already hashes on the same (title, date) key and the existing-id check skips
   re-inserting a row that already exists - this pass exists mainly to clean up
   legacy rows that were inserted before that was consistent across bot.py/ingest.py.

2. Semantic (`dedupe_semantic`): for whatever survives pass 1, asks Agnes AI whether
   any remaining entries describe the same real-world event despite different
   wording, date formatting, or extraction variance (e.g. a reminder repost that
   got parsed into a slightly different title/date). This is the pass that can
   actually catch duplicates exact-matching can't, since it doesn't rely on the
   text matching at all. It costs an API call, so it's meant to be run on demand
   (e.g. periodically), not on every single message.

Both passes always keep the earliest-inserted row in each duplicate group and
delete the rest, so older/first-seen data wins.
"""
import logging

import aiosqlite

from agnes_ai import find_semantic_duplicate_groups

DEFAULT_DB_NAME = "events_hub.db"


async def _fetch_all_events(db_name: str):
    """Return every row in `events`, ordered earliest-inserted first."""
    async with aiosqlite.connect(db_name) as db:
        async with db.execute(
            "SELECT id, title, event_date, event_time, location, description, category, timestamp "
            "FROM events ORDER BY timestamp ASC, id ASC"
        ) as cursor:
            return await cursor.fetchall()


async def _delete_ids(db_name: str, ids: list[str]) -> None:
    if not ids:
        return
    async with aiosqlite.connect(db_name) as db:
        await db.executemany("DELETE FROM events WHERE id = ?", [(i,) for i in ids])
        await db.commit()


async def dedupe_exact(db_name: str = DEFAULT_DB_NAME) -> int:
    """Collapse events sharing the same normalized (title, date) to one row each.

    Keeps the earliest-inserted row per group (rows are fetched in timestamp
    order, so the first row seen for a key is the one kept). Returns the
    number of rows removed.
    """
    rows = await _fetch_all_events(db_name)

    groups: dict[tuple[str, str], list] = {}
    for row in rows:
        key = (str(row[1]).strip().lower(), str(row[2]).strip())
        groups.setdefault(key, []).append(row)

    to_delete: list[str] = []
    for key, group in groups.items():
        if len(group) <= 1:
            continue
        survivor, dupes = group[0], group[1:]
        to_delete.extend(row[0] for row in dupes)
        logging.info(
            "🧹 Exact-dedup: keeping %s (title=%r date=%r), removing %d duplicate(s)",
            survivor[0], key[0], key[1], len(dupes),
        )

    await _delete_ids(db_name, to_delete)
    return len(to_delete)


async def dedupe_semantic(db_name: str = DEFAULT_DB_NAME) -> int:
    """Ask Agnes AI to spot duplicates that differ in wording/date format.

    Keeps the earliest-inserted row in each AI-confirmed group. Returns the
    number of rows removed. A no-op (returns 0) if AGNES_API_KEY isn't set or
    the AI call/response is unusable - never deletes anything on a guess.
    """
    rows = await _fetch_all_events(db_name)
    if len(rows) < 2:
        return 0

    order = {row[0]: idx for idx, row in enumerate(rows)}  # earliest-inserted -> lowest index
    events = [
        {
            "id": row[0],
            "title": row[1],
            "date": row[2],
            "time": row[3],
            "location": row[4],
            "description": row[5],
        }
        for row in rows
    ]

    groups = await find_semantic_duplicate_groups(events)

    to_delete: list[str] = []
    for group in groups:
        ids = sorted({i for i in group if i in order}, key=lambda i: order[i])
        if len(ids) < 2:
            continue
        survivor, dupes = ids[0], ids[1:]
        to_delete.extend(dupes)
        logging.info("🧹 Semantic-dedup (Agnes AI): keeping %s, removing %d duplicate(s)", survivor, len(dupes))

    await _delete_ids(db_name, to_delete)
    return len(to_delete)
