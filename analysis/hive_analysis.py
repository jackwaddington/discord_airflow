"""
Hive Helsinki Analysis
======================

Two analyses:
  1. Weekly active users   — how many unique people post each week
  2. Event catalogue       — every event ever announced, with date and category

Usage:
    DB_HOST=localhost .venv/bin/python3 analysis/hive_analysis.py

Outputs (all gitignored under exports/):
    exports/hive_weekly_users.csv   — week_start, active_users, total_messages
    exports/hive_events.csv         — title, date, category, source_channel, raw_text
"""

import csv
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "queries"))
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("DB_HOST", "localhost")

from query_builder import from_env as qb_from_env
from processor import DiscordLLMProcessor

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

HIVE_ID = 768384038007603221

EXPORTS = Path(__file__).parent.parent / "exports"
EXPORTS.mkdir(exist_ok=True)

# ─── Event-bearing channels ───────────────────────────────────────────────────

# Main announcement channels — use bold-title extraction + LLM categorisation
EVENT_CHANNELS = [
    "events-at-hive",
    "📣｜announcements",
    "👯｜student-gatherings",
    "🎪｜social-event-tips",
    "🔥｜tech-career-events",
]

# Sports channels — casual coordination format, pre-labelled as "sports"
SPORTS_CHANNELS = [
    "🏐｜beach-volley",
    "⚽｜hive-football",
    "🧗｜climbing",
    "🏀｜bf-basketball",
    "🏃🏼｜hive-runner",
    "🎾｜tennis",
]

# First line of each sports message typically is the event name (emoji + title)
SPORTS_TITLE_RE = re.compile(r"^([\U0001F300-\U0001FFFF\u2600-\u27BF][\S ]{3,})", re.MULTILINE)

# ─── Regex patterns to extract structured events ──────────────────────────────

# Matches **Bold Title** or __Underline Title__ (Discord markdown)
TITLE_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")

# Matches dates in various formats the announcements use:
#   "Wednesday, February 11, 2026 at 15:00"
#   "18th, March, 15:00-16:30"
#   "Tuesday 25.2 at 17:00"
#   "2025-06-10"
DATE_PATTERNS = [
    # Full: "Wednesday, February 11, 2026 at 15:00"
    re.compile(
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
        r"(\w+ \d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})"
        r"(?:\s+at\s+\d{1,2}:\d{2})?",
        re.IGNORECASE,
    ),
    # "18th March 2026" / "18th, March, 2026"
    re.compile(r"(\d{1,2}(?:st|nd|rd|th)?,?\s+\w+,?\s+\d{4})", re.IGNORECASE),
    # "25.2.2026" — require a 4-digit year so we don't match times (13.00)
    re.compile(r"(\d{1,2}\.\d{1,2}\.\d{4})"),
    # ISO "2026-02-11"
    re.compile(r"(\d{4}-\d{2}-\d{2})"),
    # Emoji-prefixed: "📍 Wednesday, February 18, 2026 at 13:00"
    re.compile(
        r"📍\s*(?:\w+,?\s+)?(\w+ \d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})",
        re.IGNORECASE,
    ),
]


def extract_events_from_message(content: str, channel: str) -> list[dict]:
    """
    Extract one or more events from a single message.

    Returns a list of dicts with: title, date, source_channel, raw_text.
    """
    if not content or len(content.strip()) < 10:
        return []

    events = []

    # Find all bold/underlined titles
    titles = []
    for m in TITLE_RE.finditer(content):
        t = m.group(1) or m.group(2)
        t = t.strip()
        # Filter out formatting-only matches (very short, or just emoji)
        if len(t) > 4 and not t.startswith("@"):
            titles.append(t)

    if not titles:
        return []

    # Find the first date in the message
    date_str = ""
    for pat in DATE_PATTERNS:
        m = pat.search(content)
        if m:
            date_str = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
            date_str = date_str.strip().strip(",")
            break

    # One event per title found
    for title in titles:
        events.append(
            {
                "title": title,
                "date": date_str,
                "source_channel": channel,
                "raw_text": content[:500],
            }
        )

    return events


PHONE_RE = re.compile(r'^\+?\d[\d\s\-]{7,}$')

SPORTS_REPLY_RE = re.compile(
    r"^(count me in|i('m| am) in|me too|i'll come|i will|yes|no|"
    r"sure|sounds good|cool|great|ok|okay|thanks|thank you|"
    r"@|https?://discord\.com|^\+\d|^\[|are there|is it|when do|"
    r"courts? free|here we|is there still|can you|hey!?$)",
    re.IGNORECASE,
)


def extract_sports_events(content: str, channel: str) -> list[dict]:
    """
    Extract sports events from casual coordination messages.

    Only grabs organising posts — messages with a time/location/date.
    Filters out replies (short conversational messages, RSVPs, etc.)

    Sports channels use emoji bullets instead of bold markdown, e.g.:
      🏐Friday Volleyball
      ⌚️18:30
      📍Töölölahti
    """
    if not content or len(content.strip()) < 15:
        return []

    # Must contain a time (⌚, HH:MM or "at 18:00") or location (📍) or date
    # to be considered an organising post, not just a reply
    has_time = bool(re.search(r'[⌚🕐🕑🕒🕓🕔🕕🕖🕗🕘🕙🕚🕛]|\d{1,2}:\d{2}|at \d{1,2}', content))
    has_location = bool(re.search(r'📍|kisahalli|töölö|hive|helsinki|address|location', content, re.IGNORECASE))
    has_date = any(pat.search(content) for pat in DATE_PATTERNS) or bool(
        re.search(r'monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|tmw', content, re.IGNORECASE)
    )

    if not (has_time or has_location or has_date):
        return []

    # First non-empty line is usually the event title
    first_line = content.strip().splitlines()[0].strip()
    if len(first_line) < 4:
        return []

    # Reject obvious replies and phone numbers
    if SPORTS_REPLY_RE.match(first_line) or PHONE_RE.match(first_line):
        return []

    # Extract date string if present
    date_str = ""
    for pat in DATE_PATTERNS:
        m = pat.search(content)
        if m:
            date_str = (m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)).strip().strip(",")
            break

    return [{
        "title": first_line,
        "date": date_str,
        "category": "sports",
        "source_channel": channel,
        "raw_text": content[:500],
    }]


# Noise patterns — bold text that looks like a section header, not an event title
NOISE_RE = re.compile(
    r"^(Agenda|Schedule|When\?|Where\?|What|Why\?|How\?|Note:|TBC|TBA|"
    r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|"
    r"Practicalities|Overview|binding|speakers?:|about the|in total of|"
    r"if you|we got|it is|[A-Z\s]{2,}:?\s*$)",
    re.IGNORECASE,
)


def deduplicate_events(events: list[dict]) -> list[dict]:
    """Remove near-duplicate events (same title, keep earliest)."""
    seen: dict[str, dict] = {}
    for e in events:
        key = e["title"].lower().strip()
        if key not in seen:
            seen[key] = e
    return list(seen.values())


def categorise_events(events: list[dict], llm: DiscordLLMProcessor) -> list[dict]:
    """
    Send events to the LLM in batches for categorisation.

    Returns the same list with a 'category' field added to each event.
    """
    BATCH_SIZE = 30

    results = []
    for i in range(0, len(events), BATCH_SIZE):
        batch = events[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (len(events) + BATCH_SIZE - 1) // BATCH_SIZE
        log.info(f"  Categorising batch {batch_num}/{total_batches} ({len(batch)} events)")

        # Format batch as a numbered list for the prompt
        lines = []
        for j, e in enumerate(batch, 1):
            lines.append(f'{j}. Title: {e["title"]} | Date: {e["date"] or "unknown"}')
        events_text = "\n".join(lines)

        try:
            raw = llm._run("event_categorise.txt", events=events_text)

            # Strip markdown fences if the LLM added them
            raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
            raw = re.sub(r"\s*```$", "", raw)

            parsed = json.loads(raw)

            # Map categories back by position
            for j, item in enumerate(parsed):
                if j < len(batch):
                    batch[j]["category"] = item.get("category", "other")

        except (json.JSONDecodeError, Exception) as exc:
            log.warning(f"  Categorisation failed for batch {batch_num}: {exc}")
            for e in batch:
                e.setdefault("category", "other")

        results.extend(batch)

    return results


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    qb = qb_from_env()
    llm = DiscordLLMProcessor()

    # ── 1. Weekly active users ────────────────────────────────────────────────
    log.info("Querying weekly active users for Hive Helsinki...")
    weekly = qb.weekly_active_users(HIVE_ID)
    out_path = EXPORTS / "hive_weekly_users.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["week_start", "active_users", "total_messages"])
        writer.writeheader()
        writer.writerows(weekly)
    log.info(f"  Saved {len(weekly)} weeks → {out_path}")

    # Quick summary
    if weekly:
        recent = weekly[:8]  # last 8 weeks
        log.info("  Recent weekly active users:")
        for w in recent:
            log.info(f"    {w['week_start']}  {w['active_users']:3d} users  {w['total_messages']:4d} msgs")

    # ── 2. Event extraction ───────────────────────────────────────────────────
    log.info("\nExtracting events from Hive channels...")
    all_events: list[dict] = []

    for channel in EVENT_CHANNELS:
        rows = qb.channel_all_messages(HIVE_ID, channel, limit=5000)
        log.info(f"  {channel}: {len(rows)} messages")
        for row in rows:
            extracted = extract_events_from_message(row["content"], channel)
            # Filter out section-header noise
            extracted = [e for e in extracted if not NOISE_RE.match(e["title"])]
            all_events.extend(extracted)

    for channel in SPORTS_CHANNELS:
        rows = qb.channel_all_messages(HIVE_ID, channel, limit=2000)
        log.info(f"  {channel}: {len(rows)} messages")
        for row in rows:
            extracted = extract_sports_events(row["content"], channel)
            all_events.extend(extracted)

    log.info(f"\n  Raw events extracted: {len(all_events)}")
    all_events = deduplicate_events(all_events)
    log.info(f"  After deduplication:  {len(all_events)}")

    # ── 3. LLM categorisation ─────────────────────────────────────────────────
    # Sports events are pre-labelled; only send unlabelled ones to the LLM
    to_categorise = [e for e in all_events if "category" not in e]
    pre_labelled = [e for e in all_events if "category" in e]

    if llm.is_ready():
        log.info(f"\nCategorising {len(to_categorise)} events with LLM ({len(pre_labelled)} pre-labelled)...")
        categorised = categorise_events(to_categorise, llm)
        all_events = pre_labelled + categorised
    else:
        log.warning("Ollama not available — skipping categorisation, all marked 'other'")
        for e in to_categorise:
            e["category"] = "other"
        all_events = pre_labelled + to_categorise

    # ── 4. Save events CSV ────────────────────────────────────────────────────
    out_path = EXPORTS / "hive_events.csv"
    fieldnames = ["title", "date", "category", "source_channel", "raw_text"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(all_events, key=lambda e: e.get("date") or ""))
    log.info(f"\nSaved {len(all_events)} events → {out_path}")

    # Category summary
    from collections import Counter
    cats = Counter(e.get("category", "other") for e in all_events)
    log.info("\nEvents by category:")
    for cat, n in sorted(cats.items(), key=lambda x: -x[1]):
        log.info(f"  {cat:<12} {n:3d}")

    qb.close()
    llm.close()


if __name__ == "__main__":
    main()
