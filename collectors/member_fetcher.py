"""
Discord Member Fetcher (Gateway op 14 / LAZY_REQUEST)

Fetches member rosters via the Discord WebSocket Gateway using op 14,
the same mechanism Discord's own client uses to load the member sidebar.
Works for any server you're in — no MANAGE_GUILD permission needed.

The REST /guilds/{id}/members endpoint requires MANAGE_GUILD (403 for
regular members). Gateway op 8 (REQUEST_GUILD_MEMBERS) is bot-only.
Op 14 is the only path available to normal user tokens.

Limitation: members are returned sorted by presence then role. Offline
lurkers appear at the end; we page through all ranges to capture them,
but very large servers (10k+) may time out before all chunks arrive.

Usage:
    python member_fetcher.py                     # all guilds in config.yaml
    python member_fetcher.py --guild 123456789   # one guild only
    python member_fetcher.py --config my.yaml

Uses your personal Discord token (same one in config.yaml).
Note: automating with a personal token is against Discord ToS; use for
personal data only and do not distribute or commercialise.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time

import psycopg2
import websockets
import yaml
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
RANGE_SIZE  = 100   # Discord member list page size
MAX_RANGES  = 50    # cap at 5 000 members per guild per op14 request

# Gateway opcodes
OP_DISPATCH      = 0
OP_HEARTBEAT     = 1
OP_IDENTIFY      = 2
OP_LAZY_REQUEST  = 14
OP_HELLO         = 10
OP_HEARTBEAT_ACK = 11


# ─── Database ─────────────────────────────────────────────────────────────────


class Database:
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

    def upsert_member(self, server_id: int, user_id: int) -> None:
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

    def get_channel_ids(self, guild_ids: list[int]) -> dict[int, int]:
        """Return one channel_id per guild (the most active channel we know about)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT server_id, channel_id
                FROM (
                    SELECT server_id, channel_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY server_id
                               ORDER BY COUNT(*) DESC
                           ) AS rn
                    FROM messages
                    WHERE server_id = ANY(%s)
                    GROUP BY server_id, channel_id
                ) ranked
                WHERE rn = 1
                """,
                (guild_ids,),
            )
            return {row[0]: row[1] for row in cur.fetchall()}

    def close(self) -> None:
        self.conn.close()


# ─── Username formatting ──────────────────────────────────────────────────────


def _format_username(member: dict) -> str:
    user          = member.get("user", {})
    nick          = member.get("nick") or ""
    global_name   = user.get("global_name") or ""
    username      = user.get("username", "unknown")
    discriminator = user.get("discriminator", "0")
    display = nick or global_name or username
    if discriminator and discriminator not in ("0", "0000"):
        return f"{display}#{discriminator}"
    return display


def _members_from_items(items: list) -> list[dict]:
    """Extract member dicts from a GUILD_MEMBER_LIST_UPDATE items array."""
    return [item["member"] for item in items if "member" in item]


# ─── Gateway client ───────────────────────────────────────────────────────────


class GatewayMemberFetcher:
    """
    Uses op 14 (LAZY_REQUEST) to load the member sidebar for each guild,
    paging through all ranges until member_count is satisfied.
    """

    def __init__(self, token: str, guild_channels: dict[int, int]) -> None:
        """
        guild_channels: {guild_id: channel_id} — one known channel per guild.
        """
        self.token          = token
        self.guild_channels = guild_channels
        self.results: dict[int, list[dict]] = {g: [] for g in guild_channels}
        self._member_counts: dict[int, int] = {}
        self._heartbeat_interval = 41.25
        self._ws = None

    async def run(self) -> dict[int, list[dict]]:
        # One fresh Gateway connection per guild — avoids 4002 payload errors
        # that occur when Discord closes a session that processed too many guilds.
        for guild_id, channel_id in self.guild_channels.items():
            await self._run_one(guild_id, channel_id)
            await asyncio.sleep(5)  # give Discord time before next session
        return self.results

    async def _run_one(self, guild_id: int, channel_id: int) -> None:
        async with websockets.connect(
            GATEWAY_URL,
            max_size=16 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            await self._handle_hello(await self._recv())
            hb_task = asyncio.create_task(self._heartbeat_loop())
            await self._identify()
            ready = await self._wait_for_ready()
            if not ready:
                logger.warning(f"  Guild {guild_id}: READY timed out, skipping")
                hb_task.cancel()
                return
            await self._fetch_guild(guild_id, channel_id)
            hb_task.cancel()

    async def _recv(self) -> dict:
        return json.loads(await self._ws.recv())

    async def _send(self, payload: dict) -> None:
        await self._ws.send(json.dumps(payload))

    async def _handle_hello(self, msg: dict) -> None:
        assert msg["op"] == OP_HELLO
        self._heartbeat_interval = msg["d"]["heartbeat_interval"] / 1000

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            try:
                await self._send({"op": OP_HEARTBEAT, "d": None})
            except Exception:
                return

    async def _identify(self) -> None:
        await self._send({
            "op": OP_IDENTIFY,
            "d": {
                "token": self.token,
                "intents": 0,
                "properties": {
                    "$os":      "linux",
                    "$browser": "discord_airflow",
                    "$device":  "discord_airflow",
                },
            },
        })

    async def _wait_for_ready(self) -> bool:
        """Wait for READY. Returns False if it times out (rate-limited or closed)."""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(self._recv(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                return False
            if msg.get("op") == OP_DISPATCH and msg.get("t") == "READY":
                logger.info("Gateway: READY")
                return True
        return False

    async def _lazy_request(self, guild_id: int, channel_id: int, ranges: list) -> None:
        # Minimal payload — extra fields (typing/threads/activities) trigger 4002
        await self._send({
            "op": OP_LAZY_REQUEST,
            "d": {
                "guild_id": str(guild_id),
                "channels": {str(channel_id): ranges},
            },
        })

    async def _fetch_guild(self, guild_id: int, channel_id: int) -> None:
        # First request — range [0, 99] — also tells us member_count
        await self._lazy_request(guild_id, channel_id, [[0, RANGE_SIZE - 1]])
        member_count = await self._await_update(guild_id)
        if member_count == 0:
            logger.warning(f"  Guild {guild_id}: no data returned from op14")
            return

        # Remaining ranges — one at a time to avoid 4002 decode errors
        total_ranges = min(
            (member_count + RANGE_SIZE - 1) // RANGE_SIZE,
            MAX_RANGES,
        )
        for i in range(1, total_ranges):
            start = i * RANGE_SIZE
            await self._lazy_request(
                guild_id, channel_id, [[start, start + RANGE_SIZE - 1]]
            )
            await self._await_update(guild_id)
            await asyncio.sleep(0.5)

        logger.info(
            f"  Guild {guild_id}: {len(self.results[guild_id])}/{member_count} members"
        )

    async def _await_update(self, guild_id: int) -> int:
        """Wait for one GUILD_MEMBER_LIST_UPDATE for this guild. Returns member_count."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(self._recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            count = self._handle_list_update(msg, guild_id)
            if count is not None:
                return count
        return 0

    def _handle_list_update(self, msg: dict, guild_id: int) -> int | None:
        """Process a GUILD_MEMBER_LIST_UPDATE. Returns member_count on first call, else None."""
        if msg.get("op") != OP_DISPATCH or msg.get("t") != "GUILD_MEMBER_LIST_UPDATE":
            return None
        d = msg["d"]
        if int(d.get("guild_id", 0)) != guild_id:
            return None

        for op in d.get("ops", []):
            if op.get("op") == "SYNC":
                members = _members_from_items(op.get("items", []))
                self.results[guild_id].extend(members)

        return d.get("member_count", 0)


# ─── Config ───────────────────────────────────────────────────────────────────


def _load_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()

        def _replace(m: re.Match) -> str:
            var, default = m.group(1), m.group(2) or ""
            return os.environ.get(var, default)

        content = re.sub(r"\$\{(\w+)(?::([^}]*))?\}", _replace, content)
        return yaml.safe_load(content) or {}
    except FileNotFoundError:
        logger.warning(f"Config file {path} not found — using environment variables.")
        return {}


def _build_dsn(config: dict) -> str:
    db = config.get("database", {})
    return (
        f"host={db.get('host', os.getenv('DB_HOST', 'localhost'))} "
        f"port={db.get('port', os.getenv('DB_PORT', '5432'))} "
        f"dbname={db.get('name', os.getenv('DB_NAME', 'discord_data'))} "
        f"user={db.get('user', os.getenv('DB_USER', 'discord_user'))} "
        f"password={db.get('password', os.getenv('DB_PASSWORD', ''))}"
    )


# ─── Entry Point ─────────────────────────────────────────────────────────────


async def _main(args: argparse.Namespace) -> None:
    config = _load_config(args.config)
    token  = config.get("discord", {}).get("token") or os.getenv("DISCORD_TOKEN", "")
    if not token:
        logger.error("No Discord token found in config or DISCORD_TOKEN env var.")
        sys.exit(1)

    guild_ids: list[int] = (
        [args.guild]
        if args.guild
        else [int(g) for g in config.get("discord", {}).get("guilds", [])]
    )
    if not guild_ids:
        logger.error("No guilds configured.")
        sys.exit(1)

    try:
        db = Database(_build_dsn(config))
    except psycopg2.OperationalError as exc:
        logger.error(f"Could not connect to PostgreSQL: {exc}")
        sys.exit(1)

    # Get one known channel per guild from our message history
    guild_channels = db.get_channel_ids(guild_ids)
    missing = [g for g in guild_ids if g not in guild_channels]
    if missing:
        logger.warning(
            f"No channel data in DB for guilds {missing} — "
            "import messages first or they will be skipped."
        )
    guild_channels = {g: c for g, c in guild_channels.items() if g in guild_ids}

    if not guild_channels:
        logger.error("No guilds with known channels to fetch.")
        sys.exit(1)

    logger.info(f"Fetching members for {len(guild_channels)} guild(s) via Gateway op14...")
    fetcher = GatewayMemberFetcher(token, guild_channels)
    results = await fetcher.run()

    # Upsert server names (use guild ID as name placeholder if unknown)
    total_upserted = 0
    for guild_id, members in results.items():
        db.upsert_server(guild_id, str(guild_id))
        count = 0
        for member in members:
            user = member.get("user", {})
            if not user.get("id") or user.get("bot"):
                continue
            discord_id = int(user["id"])
            username   = _format_username(member)
            user_id    = db.upsert_user(discord_id, username)
            db.upsert_member(guild_id, user_id)
            count += 1
        total_upserted += count
        logger.info(f"  Guild {guild_id}: {count} members upserted to DB")

    db.close()
    logger.info(f"\nDone — {total_upserted} member records across {len(guild_channels)} guild(s)")


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Fetch Discord guild member rosters via Gateway op14"
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--guild", type=int, help="Fetch one guild only")
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
