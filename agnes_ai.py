"""Shared helper for extracting structured event data from raw chat text via the Agnes AI API.

Used by both bot.py (live group message capture) and ingest.py (history backfill) so the
extraction logic and prompt only have to live in one place.

Docs: https://wiki.agnes-ai.com/en/docs/agnes-20-flash.md (OpenAI-compatible chat completions)
"""
import json
import logging
import os

import httpx

DEFAULT_API_BASE = "https://apihub.agnes-ai.com/v1"
DEFAULT_MODEL = "agnes-2.0-flash"

VALID_CATEGORIES = [
    "Workshops", "Career talks", "Internship opportunities",
    "Scholarships", "Competitions", "Volunteer opportunities",
    "CCA sign-ups", "Fifth Row sign-ups", "Networking opportunities",
]

SYSTEM_PROMPT = (
    "You extract structured event listings from raw messages posted in a university "
    "events Telegram group. Read the message and decide whether it announces one or more "
    "concrete events (workshops, talks, internships, scholarships, competitions, "
    "volunteering, CCA/club sign-ups, networking sessions, etc.).\n\n"
    "Respond with ONLY a JSON array and no other text. Each element must be an object with "
    "exactly these keys:\n"
    '- "title": short event name\n'
    '- "date": event date as YYYY-MM-DD\n'
    '- "time": event time, e.g. "14:00 - 17:00" (use "" if not stated)\n'
    '- "location": event location (use "" if not stated)\n'
    '- "description": a short 1-2 sentence summary of the message\n'
    '- "category": exactly one of: ' + ", ".join(VALID_CATEGORIES) + "\n\n"
    "If the message does not announce a real event with at least a title and a date "
    "(e.g. it's small talk, a question, a reply, or an update with nothing new), respond "
    "with an empty JSON array: []."
)

DEDUPE_SYSTEM_PROMPT = (
    "You are given a numbered list of event listings already stored in a university events "
    "database. Each line has an id, title, date, time, location, and description. Some entries "
    "may describe the exact same real-world event even though the wording, date format, or "
    "details differ slightly - for example one entry might be a reminder repost of another, or "
    "the title/date may have been extracted slightly differently from two separate messages "
    "about the same happening.\n\n"
    "Respond with ONLY a JSON array of arrays (no other text), where each inner array lists the "
    "ids of entries that all refer to the same real-world event. Only include groups of 2 or "
    "more ids - omit any entry that has no duplicate. If there are no duplicates at all, respond "
    "with an empty JSON array: []."
)


async def extract_events(raw_text: str) -> list[dict]:
    """Call the Agnes AI API to extract zero or more structured events from raw_text.

    Returns a list of event dicts (possibly empty) with keys: title, date, time,
    location, description, category. Never raises - on any failure it logs a
    warning and returns an empty list so a flaky AI response can't crash the caller.
    """
    api_key = os.getenv("AGNES_API_KEY", "")
    if not api_key:
        logging.warning("AGNES_API_KEY is not set; skipping AI extraction for this message.")
        return []

    api_base = os.getenv("AGNES_API_BASE", DEFAULT_API_BASE).rstrip("/")
    model = os.getenv("AGNES_MODEL", DEFAULT_MODEL)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": raw_text},
        ],
        "temperature": 0.2,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{api_base}/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logging.warning("Agnes AI request failed: %s", exc)
        return []

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        logging.warning("Unexpected Agnes AI response shape: %s (%s)", data, exc)
        return []

    return _parse_events(content)


def _parse_events(content: str) -> list[dict]:
    """Parse and validate the model's JSON response into a clean list of event dicts."""
    cleaned = (content or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logging.warning("Could not parse Agnes AI response as JSON: %s | raw: %s", exc, content[:300])
        return []

    if not isinstance(parsed, list):
        return []

    events = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        date = str(item.get("date", "")).strip()
        if not title or not date:
            continue
        # Case-insensitive match against the valid category list, so a minor casing
        # difference from the model doesn't bump an event into the invisible
        # "General" bucket (not a real category - no one can subscribe/view it).
        category_raw = str(item.get("category", "")).strip()
        category = next(
            (c for c in VALID_CATEGORIES if c.lower() == category_raw.lower()),
            "General",
        )
        events.append({
            "title": title,
            "date": date,
            "time": str(item.get("time", "")).strip(),
            "location": str(item.get("location", "")).strip(),
            "description": str(item.get("description", "")).strip(),
            "category": category,
        })
    return events


async def find_semantic_duplicate_groups(events: list[dict]) -> list[list[str]]:
    """Ask Agnes AI which stored events (by id) describe the same real-world event.

    events: a list of dicts with keys id, title, date, time, location, description -
    typically every row currently in the events table (or whatever's left after a
    cheaper exact-match pass). This is meant to catch duplicates that differ in
    wording/date format and so wouldn't share an id or an exact (title, date) key.

    Returns a list of id-groups, each with 2+ ids that the model judged to be the
    same event. Never raises - on any failure (missing key, bad response, etc.) it
    logs a warning and returns [], so a flaky AI response can never delete the
    wrong rows.
    """
    api_key = os.getenv("AGNES_API_KEY", "")
    if not api_key:
        logging.warning("AGNES_API_KEY is not set; skipping semantic dedup pass.")
        return []

    if len(events) < 2:
        return []

    api_base = os.getenv("AGNES_API_BASE", DEFAULT_API_BASE).rstrip("/")
    model = os.getenv("AGNES_MODEL", DEFAULT_MODEL)

    listing = "\n".join(
        f"{idx}. id={event['id']} title={event.get('title', '')!r} date={event.get('date', '')!r} "
        f"time={event.get('time', '')!r} location={event.get('location', '')!r} "
        f"description={event.get('description', '')!r}"
        for idx, event in enumerate(events, 1)
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": DEDUPE_SYSTEM_PROMPT},
            {"role": "user", "content": listing},
        ],
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{api_base}/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logging.warning("Agnes AI dedup request failed: %s", exc)
        return []

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        logging.warning("Unexpected Agnes AI response shape: %s (%s)", data, exc)
        return []

    cleaned = (content or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logging.warning("Could not parse Agnes AI dedup response as JSON: %s | raw: %s", exc, content[:300])
        return []

    if not isinstance(parsed, list):
        return []

    valid_ids = {str(event["id"]) for event in events}
    groups = []
    for group in parsed:
        if not isinstance(group, list):
            continue
        ids = [str(i) for i in group if str(i) in valid_ids]
        # De-dupe within the group itself and require at least 2 distinct ids
        ids = list(dict.fromkeys(ids))
        if len(ids) >= 2:
            groups.append(ids)
    return groups
