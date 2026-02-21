"""
Discord Data Importer - Layer 1

Reads JSON files exported by DiscordChatExporter and inserts the data
into PostgreSQL, handling duplicates gracefully.

Usage:
    python importer.py --input ./exports/           # import all JSON files
    python importer.py --input ./exports/123456789  # import one guild's files
    python importer.py --input file.json            # import a single file
    python importer.py --dry-run --input ./exports/ # preview without inserting

The importer is idempotent — running it twice on the same files is safe.
Existing messages are updated (content edits are preserved), not duplicated.

DiscordChatExporter JSON schema notes:
  - Each file covers one channel (or thread).
  - Reactions only include counts, not individual users — the reactions
    table is skipped. Reaction data is stored in messages.reaction_data (JSONB)
    if that column exists, otherwise ignored.
  - Thread channels have type "GuildPublicThread", "GuildPrivateThread", etc.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import psycopg2
import psycopg2.extras
import yaml
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Channel types that indicate the "channel" is actually a thread
THREAD_TYPES = {
    "GuildPublicThread",
    "GuildPrivateThread",
    "GuildNewsThread",
}


# ─── Database ─────────────────────────────────────────────────────────────────


class Database:
    """Minimal DB wrapper for the importer — mirrors the one in poller.py."""

    def __init__(self, dsn: str) -> None:
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = False

    def upsert_server(self, server_id: int, server_name: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO servers (server_id, server_name, monitored_from)
                VALUES (%s, %s, NOW())
                ON CONFLICT (server_id) DO UPDATE
                    SET server_name = EXCLUDED.server_name
                """,
                (server_id, server_name),
            )
        self.conn.commit()

    def upsert_user(self, discord_id: int, username: str) -> int:
        """Insert or update user; track username changes. Returns internal user_id."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, current_username FROM users WHERE discord_id = %s",
                (discord_id,),
            )
            row = cur.fetchone()

            if row is None:
                cur.execute(
                    """
                    INSERT INTO users (discord_id, current_username, created_at, last_seen)
                    VALUES (%s, %s, NOW(), NOW())
                    RETURNING user_id
                    """,
                    (discord_id, username),
                )
                user_id = cur.fetchone()[0]
            else:
                user_id, current_username = row
                cur.execute(
                    "UPDATE users SET last_seen = NOW() WHERE user_id = %s",
                    (user_id,),
                )
                if current_username != username:
                    cur.execute(
                        """
                        INSERT INTO username_history (user_id, username, changed_from, changed_at)
                        VALUES (%s, %s, %s, NOW())
                        """,
                        (user_id, username, current_username),
                    )
                    cur.execute(
                        "UPDATE users SET current_username = %s WHERE user_id = %s",
                        (username, user_id),
                    )

        self.conn.commit()
        return user_id

    def upsert_server_member(self, server_id: int, user_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO server_members (server_id, user_id, joined_at, is_active)
                VALUES (%s, %s, NOW(), true)
                ON CONFLICT (server_id, user_id) DO UPDATE
                    SET is_active = true
                """,
                (server_id, user_id),
            )
        self.conn.commit()

    def upsert_message(
        self,
        message_id: int,
        server_id: int,
        channel_id: int,
        channel_name: str,
        user_id: int,
        content: str,
        created_at: datetime,
        edited_at: Optional[datetime],
        reply_to_message_id: Optional[int],
        thread_id: Optional[int],
    ) -> bool:
        """
        Insert or update a message.

        Returns True if this was a new message, False if it already existed.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT message_id FROM messages WHERE message_id = %s",
                (message_id,),
            )
            is_new = cur.fetchone() is None

            cur.execute(
                """
                INSERT INTO messages (
                    message_id, server_id, channel_id, channel_name,
                    user_id, content, created_at, edited_at,
                    reply_to_message_id, thread_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (message_id) DO UPDATE SET
                    content   = EXCLUDED.content,
                    edited_at = EXCLUDED.edited_at,
                    is_deleted = false
                """,
                (
                    message_id, server_id, channel_id, channel_name,
                    user_id, content, created_at, edited_at,
                    reply_to_message_id, thread_id,
                ),
            )
        self.conn.commit()
        return is_new

    def message_count(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM messages")
            return cur.fetchone()[0]

    def close(self) -> None:
        self.conn.close()


# ─── DCE JSON Parsing ─────────────────────────────────────────────────────────


def _parse_timestamp(ts: Optional[str]) -> Optional[datetime]:
    """
    Parse a DCE ISO 8601 timestamp string to a timezone-aware datetime.

    DCE uses: "2024-01-15T14:32:00.000+00:00"
    Returns UTC datetime, or None if ts is None/empty.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(tzinfo=None)  # store as naive UTC
    except (ValueError, TypeError):
        logger.warning(f"Could not parse timestamp: {ts!r}")
        return None


def _format_username(author: dict) -> str:
    """
    Build a display username from a DCE author object.

    Modern Discord accounts have discriminator "0000" (no tag).
    Legacy accounts have a 4-digit discriminator like "alice#1234".
    """
    name = author.get("name", "unknown")
    # Use nickname if available (server-specific display name)
    nickname = author.get("nickname", "")
    display = nickname if nickname else name
    discriminator = author.get("discriminator", "0000")
    if discriminator and discriminator != "0000" and discriminator != "0":
        return f"{display}#{discriminator}"
    return display


def _parse_discord_id(raw: Any) -> Optional[int]:
    """Safely convert a Discord ID (may be string or int in JSON) to int."""
    if raw is None:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def import_file(
    json_path: Path,
    db: Database,
    dry_run: bool = False,
) -> dict:
    """
    Parse one DiscordChatExporter JSON file and insert into the database.

    Args:
        json_path: Path to the exported JSON file.
        db:        Database instance.
        dry_run:   If True, parse and count but don't write anything.

    Returns:
        Stats dict: {"file", "new", "updated", "skipped", "errors"}
    """
    stats = {"file": json_path.name, "new": 0, "updated": 0, "skipped": 0, "errors": 0}

    logger.info(f"Importing: {json_path.name}")

    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(f"  Could not read {json_path}: {exc}")
        stats["errors"] += 1
        return stats

    # ── Server ────────────────────────────────────────────────────────────────
    guild = data.get("guild", {})
    server_id = _parse_discord_id(guild.get("id"))
    server_name = guild.get("name", "Unknown Server")

    if server_id is None:
        logger.error(f"  No guild.id in {json_path.name}, skipping.")
        stats["errors"] += 1
        return stats

    # ── Channel ───────────────────────────────────────────────────────────────
    channel = data.get("channel", {})
    channel_id = _parse_discord_id(channel.get("id"))
    channel_name = channel.get("name", "unknown")
    channel_type = channel.get("type", "")

    if channel_id is None:
        logger.error(f"  No channel.id in {json_path.name}, skipping.")
        stats["errors"] += 1
        return stats

    # Threads are exported as their own channels — set thread_id accordingly
    # so queries can distinguish thread messages from regular channel messages.
    is_thread = channel_type in THREAD_TYPES
    thread_id = channel_id if is_thread else None

    if dry_run:
        message_count = len(data.get("messages", []))
        logger.info(
            f"  [dry-run] {server_name} #{channel_name}: "
            f"{message_count} message(s), thread={is_thread}"
        )
        stats["new"] = message_count
        return stats

    # ── Upsert server ─────────────────────────────────────────────────────────
    db.upsert_server(server_id, server_name)

    # ── Messages ──────────────────────────────────────────────────────────────
    messages = data.get("messages", [])

    for msg in messages:
        try:
            message_id = _parse_discord_id(msg.get("id"))
            if message_id is None:
                stats["skipped"] += 1
                continue

            # Author
            author = msg.get("author", {})
            author_discord_id = _parse_discord_id(author.get("id"))
            if author_discord_id is None:
                stats["skipped"] += 1
                continue

            username = _format_username(author)
            user_id = db.upsert_user(author_discord_id, username)
            db.upsert_server_member(server_id, user_id)

            # Timestamps
            created_at = _parse_timestamp(msg.get("timestamp"))
            if created_at is None:
                stats["skipped"] += 1
                continue
            edited_at = _parse_timestamp(msg.get("timestampEdited"))

            # Content — combine text content + note if attachments exist
            content = msg.get("content", "") or ""
            attachments = msg.get("attachments", [])
            if attachments and not content:
                # Attachment-only message — note what was attached
                names = [a.get("fileName", "attachment") for a in attachments]
                content = f"[{', '.join(names)}]"

            # Reply reference
            reference = msg.get("reference", {}) or {}
            reply_to = _parse_discord_id(reference.get("messageId"))

            # Insert
            is_new = db.upsert_message(
                message_id=message_id,
                server_id=server_id,
                channel_id=channel_id,
                channel_name=channel_name,
                user_id=user_id,
                content=content,
                created_at=created_at,
                edited_at=edited_at,
                reply_to_message_id=reply_to,
                thread_id=thread_id,
            )

            if is_new:
                stats["new"] += 1
            else:
                stats["updated"] += 1

        except Exception as exc:
            logger.warning(f"  Error importing message {msg.get('id')}: {exc}")
            stats["errors"] += 1

    logger.info(
        f"  {server_name} #{channel_name}: "
        f"{stats['new']} new, {stats['updated']} updated, "
        f"{stats['skipped']} skipped, {stats['errors']} errors"
    )
    return stats


# ─── Config & DSN ─────────────────────────────────────────────────────────────


def _build_dsn(config: dict) -> str:
    db = config.get("database", {})
    return (
        f"host={db.get('host', os.getenv('DB_HOST', 'localhost'))} "
        f"port={db.get('port', os.getenv('DB_PORT', '5432'))} "
        f"dbname={db.get('name', os.getenv('DB_NAME', 'discord_data'))} "
        f"user={db.get('user', os.getenv('DB_USER', 'discord_user'))} "
        f"password={db.get('password', os.getenv('DB_PASSWORD', ''))}"
    )


def _collect_json_files(input_path: Path) -> list[Path]:
    """Return all .json files under input_path (recursive if directory)."""
    if input_path.is_file():
        return [input_path]
    elif input_path.is_dir():
        files = sorted(input_path.rglob("*.json"))
        if not files:
            logger.warning(f"No JSON files found in {input_path}")
        return files
    else:
        logger.error(f"Input path does not exist: {input_path}")
        return []


# ─── Entry Point ─────────────────────────────────────────────────────────────


def main() -> None:
    """Import exported JSON files into PostgreSQL."""
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Import DiscordChatExporter JSON files into PostgreSQL"
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to a JSON file or directory of JSON files",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config YAML (for DB connection)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse files and report counts without writing to the database",
    )
    args = parser.parse_args()

    # Load config for DB connection
    config: dict = {}
    try:
        with open(args.config, encoding="utf-8") as f:
            import re
            content = f.read()

            def _replace(m: re.Match) -> str:
                var = m.group(1)
                default = m.group(2) or ""
                return os.environ.get(var, default)

            content = re.sub(r"\$\{(\w+)(?::([^}]*))?\}", _replace, content)
            config = yaml.safe_load(content) or {}
    except FileNotFoundError:
        logger.warning(
            f"Config file {args.config} not found — using environment variables for DB connection."
        )

    json_files = _collect_json_files(Path(args.input))
    if not json_files:
        sys.exit(1)

    logger.info(f"Found {len(json_files)} file(s) to import")

    if args.dry_run:
        logger.info("[dry-run mode — no database writes]")
        db = None
    else:
        try:
            db = Database(_build_dsn(config))
        except psycopg2.OperationalError as exc:
            logger.error(
                f"Could not connect to PostgreSQL: {exc}\n"
                "Check your database config in config.yaml or .env."
            )
            sys.exit(1)

    # Import each file
    totals = {"new": 0, "updated": 0, "skipped": 0, "errors": 0}
    for json_file in json_files:
        stats = import_file(json_file, db, dry_run=args.dry_run)
        for k in totals:
            totals[k] += stats.get(k, 0)

    if db:
        total_in_db = db.message_count()
        db.close()
    else:
        total_in_db = 0

    logger.info(
        f"\n{'[dry-run] ' if args.dry_run else ''}Import complete:\n"
        f"  Files processed : {len(json_files)}\n"
        f"  New messages    : {totals['new']}\n"
        f"  Updated messages: {totals['updated']}\n"
        f"  Skipped         : {totals['skipped']}\n"
        f"  Errors          : {totals['errors']}\n"
        + (f"  Total in DB     : {total_in_db}" if not args.dry_run else "")
    )

    if totals["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
