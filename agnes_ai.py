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
        category = item.get("category", "")
        if category not in VALID_CATEGORIES:
            category = "General"
        events.append({
            "title": title,
            "date": date,
            "time": str(item.get("time", "")).strip(),
            "location": str(item.get("location", "")).strip(),
            "description": str(item.get("description", "")).strip(),
            "category": category,
        })
    return events
