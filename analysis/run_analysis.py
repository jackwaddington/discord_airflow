"""
Full analysis suite for a single Discord server.

Usage:
    DB_HOST=localhost python3 analysis/run_analysis.py

Runs (in order):
    1. Weekly digest  — LLM narrative of the last 7 days
    2. Channel FAQ    — Q&A extracted from #the-discussion
    3. User profiles  — LLM profile of each active user
    4. Server health  — monthly join/leave trends (SQL, no LLM)

Output is saved to reports/theAgora-analysis.md
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Make sure Layer 2 and Layer 3 modules are importable
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "queries"))
sys.path.insert(0, str(ROOT / "analysis"))

from query_builder import QueryBuilder
from chunker import SmartChunker
from processor import DiscordLLMProcessor, _fill_template

# ── Config ────────────────────────────────────────────────────────────────────

AGORA_DISCORD_ID = "1474024565419147317"
DAYS = 30
OUTPUT_FILE = ROOT / "reports" / "theAgora-analysis.md"

DSN = (
    f"host={os.getenv('DB_HOST', 'localhost')} "
    f"port={os.getenv('DB_PORT', '5432')} "
    f"dbname={os.getenv('DB_NAME', 'discord_data')} "
    f"user={os.getenv('DB_USER', 'discord_user')} "
    f"password={os.getenv('DB_PASSWORD', 'discord')}"
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def section(title: str) -> str:
    return f"\n\n## {title}\n"

def hr() -> str:
    return "\n---\n"

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Connecting to database...")
    qb = QueryBuilder(DSN)

    # Find theAgora's internal server_id
    rows = qb._execute(
        "SELECT server_id, server_name FROM servers WHERE server_id = %s",
        (AGORA_DISCORD_ID,)
    )
    if not rows:
        print(f"ERROR: Server with discord_id {AGORA_DISCORD_ID} not found in DB.")
        sys.exit(1)

    server_id = rows[0]["server_id"]
    server_name = rows[0]["server_name"]
    print(f"Found: {server_name} (internal id={server_id})")

    print("Connecting to Ollama...")
    llm = DiscordLLMProcessor()
    if not llm.is_ready():
        print("ERROR: Ollama is not reachable. Run: ollama serve")
        sys.exit(1)

    lines = [
        f"# {server_name} — Full Analysis",
        f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · last {DAYS} days_",
        hr(),
    ]

    # ── 1. Weekly digest ──────────────────────────────────────────────────────
    print("\n[1/4] Weekly digest...")
    summary = qb.server_summary_data(server_id, days=DAYS)
    lines.append(section("Weekly Digest"))
    if summary:
        # Pull real messages to ground the LLM — prevents hallucination
        sample_rows = qb._execute(
            """
            SELECT m.created_at, u.current_username AS author, m.content
            FROM messages m
            JOIN users u ON m.user_id = u.user_id
            WHERE m.server_id = %s
              AND m.created_at > NOW() - (%s * INTERVAL '1 day')
              AND m.is_deleted = false
              AND m.content IS NOT NULL AND m.content != ''
            ORDER BY m.created_at DESC
            LIMIT 60
            """,
            (server_id, DAYS)
        )
        sample_text = SmartChunker.format_for_llm(sample_rows)
        template = llm._load_prompt("weekly_digest.txt")
        prompt = _fill_template(
            template,
            server_data=json.dumps(summary, indent=2, default=str),
            messages=sample_text,
        )
        digest = llm.client.generate(prompt).strip()
        lines.append(digest)
    else:
        lines.append("_No activity in this period._")

    # ── 2. Channel FAQ — #the-discussion ─────────────────────────────────────
    print("[2/4] Channel FAQ for #the-discussion...")
    channel_rows = qb._execute(
        """
        SELECT DISTINCT channel_id, channel_name
        FROM messages
        WHERE server_id = %s AND channel_name ILIKE '%%discussion%%'
        LIMIT 1
        """,
        (server_id,)
    )

    lines.append(section("Channel FAQ — #the-discussion"))
    if channel_rows:
        ch_id = channel_rows[0]["channel_id"]
        ch_name = channel_rows[0]["channel_name"]
        msgs = qb.channel_messages(server_id, ch_id, time_range_days=DAYS)
        if msgs:
            chunks = SmartChunker.chunk_and_format(msgs)
            print(f"  {len(msgs)} messages → {len(chunks)} chunk(s)")
            faq_parts = []
            for i, chunk in enumerate(chunks, 1):
                print(f"  Running LLM on chunk {i}/{len(chunks)}...")
                faq_parts.append(llm.channel_summary_faq(chunk, ch_name))
            lines.append("\n".join(faq_parts))
        else:
            lines.append("_No messages in this period._")
    else:
        lines.append("_Channel not found._")

    # ── 3. User profiles ──────────────────────────────────────────────────────
    print("[3/4] User profiles...")
    active_users = qb.recent_active_users(server_id, days=DAYS, limit=10)
    lines.append(section("User Profiles"))

    for user in active_users:
        username = user["current_username"]
        user_id_int = qb._execute(
            "SELECT user_id FROM users WHERE discord_id = %s",
            (user["discord_id"],)
        )
        if not user_id_int:
            continue
        uid = user_id_int[0]["user_id"]
        msgs = qb.user_message_context(uid, time_range_days=DAYS)
        if not msgs:
            continue

        print(f"  Profiling {username} ({len(msgs)} messages)...")
        chunks = SmartChunker.chunk_and_format(msgs)
        profile = llm.user_story(chunks[0], username=username)

        lines.append(f"\n### {username} ({user['message_count']} messages)\n")
        lines.append(profile)

    # ── 4. Server health ──────────────────────────────────────────────────────
    print("[4/4] Server health (join/leave trends)...")
    health = qb.server_health(server_id)
    lines.append(section("Server Health — Monthly Trends"))
    if health:
        lines.append("| Month | Joined | Left | Net |")
        lines.append("| ----- | ------ | ---- | --- |")
        for row in health[:12]:  # last 12 months
            lines.append(
                f"| {row['month']} | {row['new_members']} | {row['left_members']} | {int(row['net_change']):+d} |"
            )
    else:
        lines.append("_No membership data available._")

    # ── Save ──────────────────────────────────────────────────────────────────
    qb.close()
    llm.close()

    OUTPUT_FILE.parent.mkdir(exist_ok=True)
    OUTPUT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nDone. Report saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
