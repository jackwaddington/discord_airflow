"""
Keyword-based event categoriser for Hive Helsinki events.

Reads exports/hive_events.csv (which may already have LLM categories),
re-categorises every event using keyword rules, and saves the result.

Rules are explicit and auditable — you can see exactly why each event
got its label. Run this after hive_analysis.py.

Usage:
    .venv/bin/python3 analysis/categorise_events.py
"""

import csv
import re
from collections import Counter
from pathlib import Path

EXPORTS = Path(__file__).parent.parent / "exports"

# ─── Keyword rules (ordered — first match wins) ───────────────────────────────
# Each rule: (category, [keywords])
# Matching is case-insensitive against the event title.

RULES = [
    ("induction", [
        "piscine", "orientation", "onboarding", "induction", "evaluation",
        "exam", "pedago", "42", "bocal", "selection", "admission",
        "get_next_line", "libft", "ft_printf", "born2beroot", "push_swap",
    ]),
    ("sports", [
        "volleyball", "volley", "football", "soccer", "basketball", "tennis",
        "running", "runner", "climbing", "swim", "yoga", "stretching",
        "mindfulness", "martial", "fighting", "badminton", "frisbee",
        "beach", "sport", "workout", "fitness", "gym", "hike", "hiking",
        "cycling", "padel",
    ]),
    ("wellness", [
        "meditation", "mental health", "wellbeing", "well-being", "hetki",
        "mindfulness", "breathing", "relaxation", "stress", "burnout",
        "sleep", "nutrition", "health",
    ]),
    ("gaming", [
        "game night", "game of the month", "magic the gathering", "mtg",
        "mafia", "werewolf", "werewolves", "board game", "tabletop",
        "dungeons", "d&d", "dnd", "rpg", "video game", "esport",
        "fighting game", "supercell", "minecraft",
    ]),
    ("creative", [
        "3d print", "3d-print", "electronics", "soldering", "hardware",
        "music", "concert", "photography", "film", "kino", "cinema",
        "art", "drawing", "painting", "craft", "maker", "diy",
    ]),
    ("tech", [
        "workshop", "hackathon", "sprint", "demo", "tech talk", "tech career",
        "ai", "machine learning", "ml", "llm", "python", "javascript",
        "rust", "golang", "fastapi", "api", "web", "cloud", "linux",
        "cybersecurity", "security", "devops", "kubernetes", "docker",
        "software", "developer", "engineer", "coding", "programming",
        "startup", "product", "ux", "ui", "design", "data science",
        "excursion", "company visit", "office tour", "visit to",
        "career", "job", "internship", "summer job", "recruitment",
        "cv", "resume", "interview", "linkedin", "portfolio",
    ]),
    ("learning", [
        "book club", "language cafe", "kieli kahvila", "study",
        "lecture", "seminar", "talk", "presentation", "panel",
        "public speaking", "writing", "communication", "english",
        "finnish", "french", "spanish", "german",
    ]),
    ("social", [
        "gala", "party", "brunch", "lunch", "dinner", "breakfast",
        "coffee", "drinks", "sitsit", "afterwork", "after work",
        "networking", "mixer", "social", "gathering", "meetup",
        "meet up", "hangout", "hang out", "picnic", "barbecue", "bbq",
        "market", "fair", "xmas", "christmas", "halloween", "easter",
        "pikkujoulu", "sauna", "karaoke", "trivia", "quiz",
        "beecoming", "volunteer", "alumni",
    ]),
]


def categorise(title: str) -> tuple[str, str]:
    """
    Return (category, matched_keyword) for an event title.
    Falls back to 'other' if no rule matches.
    """
    lower = title.lower()
    for category, keywords in RULES:
        for kw in keywords:
            if kw in lower:
                return category, kw
    return "other", ""


def main() -> None:
    in_path = EXPORTS / "hive_events.csv"
    out_path = EXPORTS / "hive_events.csv"

    events = list(csv.DictReader(open(in_path, encoding="utf-8")))
    print(f"Re-categorising {len(events)} events with keyword rules...")

    matched = 0
    for e in events:
        cat, kw = categorise(e["title"])
        e["category"] = cat
        e["matched_keyword"] = kw
        if cat != "other":
            matched += 1

    print(f"Matched: {matched}/{len(events)} ({100*matched//len(events)}%)")
    print(f"Unmatched (other): {len(events)-matched}")

    # Show category breakdown
    cats = Counter(e["category"] for e in events)
    print("\nEvents by category:")
    for cat, n in sorted(cats.items(), key=lambda x: -x[1]):
        print(f"  {cat:<25} {n:3d}")

    # Show sample 'other' titles so we can improve rules
    others = [e["title"] for e in events if e["category"] == "other"]
    print(f"\nSample 'other' titles (first 30):")
    for t in others[:30]:
        print(f"  {t}")

    # Save with matched_keyword column for auditability
    fieldnames = ["title", "date", "category", "matched_keyword", "source_channel", "raw_text"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(events, key=lambda e: e.get("date") or ""))

    print(f"\nSaved → {out_path}")


if __name__ == "__main__":
    main()
