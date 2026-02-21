"""
Query Builder - Layer 2

Extracts data from PostgreSQL for downstream LLM processing.

All query methods return plain list[dict] so results are easy to
serialize to JSON and hand off to Layer 3.

Usage:
    from query_builder import from_env

    qb = from_env()

    # Who's in 3+ servers?
    connectors = qb.users_across_servers(min_servers=3)

    # What has alice been talking about?
    messages = qb.user_message_context(user_id=42, time_range_days=90)

    # Summarize a channel
    channel = qb.channel_messages(server_id=123, channel_id=456, time_range_days=30)

    qb.close()

Configuration via .env (same as Layer 1):
    DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD
"""

import logging
import os
from typing import Optional

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class QueryBuilder:
    """
    Typed, parameterized query methods for the Discord Intelligence database.

    Uses RealDictCursor so every row comes back as a plain dict —
    no magic ORM objects, easy JSON serialization, easy LLM formatting.

    The connection is opened in autocommit mode because all queries here
    are read-only SELECTs and we don't need transaction control.
    """

    def __init__(self, dsn: str) -> None:
        """
        Connect to PostgreSQL.

        Args:
            dsn: psycopg2 connection string, e.g.
                 "host=localhost port=5432 dbname=discord_data user=..."
        """
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True

    def _execute(self, sql: str, params: tuple = ()) -> list[dict]:
        """
        Run a parameterized query and return all rows as plain dicts.

        All public query methods go through this — it's the single place
        we touch the cursor, making it easy to swap backends later.
        """
        with self.conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    # ─── Public Query Methods ─────────────────────────────────────────────────

    def users_across_servers(self, min_servers: int = 3) -> list[dict]:
        """
        Return users who are active in at least min_servers servers.

        Useful for finding community connectors, cross-server regulars,
        or people who might be worth profiling across communities.

        Returns rows with: discord_id, current_username, server_count, servers[]
        """
        return self._execute(
            """
            SELECT
                u.discord_id,
                u.current_username,
                COUNT(DISTINCT sm.server_id)                        AS server_count,
                ARRAY_AGG(DISTINCT s.server_name ORDER BY s.server_name) AS servers
            FROM users u
            JOIN server_members sm ON u.user_id = sm.user_id
            JOIN servers s         ON sm.server_id = s.server_id
            WHERE sm.is_active = true
            GROUP BY u.user_id, u.discord_id, u.current_username
            HAVING COUNT(DISTINCT sm.server_id) >= %s
            ORDER BY server_count DESC
            """,
            (min_servers,),
        )

    def user_message_context(
        self,
        user_id: int,
        time_range_days: Optional[int] = None,
    ) -> list[dict]:
        """
        Get all messages from a user with server/channel context.

        Results are ordered newest-first so the most recent activity
        is at the top if the caller truncates for context window limits.

        Args:
            user_id:         Internal user_id (from the users table).
            time_range_days: Limit to messages in the last N days.
                             None fetches full history (can be large).

        Returns rows with:
            message_id, created_at, server_name, channel_name,
            content, is_deleted, reaction_count
        """
        sql = """
            SELECT
                m.message_id,
                m.created_at,
                s.server_name,
                m.channel_name,
                m.content,
                m.is_deleted,
                COUNT(r.id) AS reaction_count
            FROM messages m
            JOIN servers s      ON m.server_id = s.server_id
            LEFT JOIN reactions r ON m.message_id = r.message_id
            WHERE m.user_id = %s
              AND m.is_deleted = false
        """
        params: list = [user_id]

        if time_range_days is not None:
            # Use interval arithmetic with a parameter — never interpolate
            # time values directly into SQL strings.
            sql += " AND m.created_at > NOW() - (%s * INTERVAL '1 day')"
            params.append(time_range_days)

        sql += """
            GROUP BY m.message_id, s.server_name
            ORDER BY m.created_at DESC
        """
        return self._execute(sql, tuple(params))

    def channel_messages(
        self,
        server_id: int,
        channel_id: int,
        time_range_days: int = 30,
    ) -> list[dict]:
        """
        Get non-deleted messages from a channel within a time window.

        Ordered oldest-first (natural reading order) so the LLM sees
        conversation flow in chronological sequence.

        Returns rows with:
            message_id, created_at, author, content,
            reply_to_message_id, thread_id
        """
        return self._execute(
            """
            SELECT
                m.message_id,
                m.created_at,
                u.current_username  AS author,
                m.content,
                m.reply_to_message_id,
                m.thread_id
            FROM messages m
            JOIN users u ON m.user_id = u.user_id
            WHERE m.server_id = %s
              AND m.channel_id = %s
              AND m.created_at > NOW() - (%s * INTERVAL '1 day')
              AND m.is_deleted = false
            ORDER BY m.created_at ASC
            """,
            (server_id, channel_id, time_range_days),
        )

    def server_health(self, server_id: int) -> list[dict]:
        """
        Return monthly join/leave counts for a server.

        Each row is one calendar month with new_members, left_members,
        and net_change — useful for spotting growth trends or churn spikes.

        Returns rows with: month, new_members, left_members, net_change
        """
        return self._execute(
            """
            WITH monthly_joins AS (
                SELECT
                    DATE_TRUNC('month', joined_at)::DATE AS month,
                    COUNT(DISTINCT user_id)              AS new_members,
                    0                                    AS left_members
                FROM server_members
                WHERE server_id = %s
                  AND joined_at IS NOT NULL
                GROUP BY DATE_TRUNC('month', joined_at)
            ),
            monthly_churn AS (
                SELECT
                    DATE_TRUNC('month', left_at)::DATE AS month,
                    0                                  AS new_members,
                    COUNT(DISTINCT user_id)             AS left_members
                FROM server_members
                WHERE server_id = %s
                  AND left_at IS NOT NULL
                GROUP BY DATE_TRUNC('month', left_at)
            ),
            combined AS (
                SELECT * FROM monthly_joins
                UNION ALL
                SELECT * FROM monthly_churn
            )
            SELECT
                month,
                SUM(new_members)                        AS new_members,
                SUM(left_members)                       AS left_members,
                SUM(new_members) - SUM(left_members)    AS net_change
            FROM combined
            GROUP BY month
            ORDER BY month DESC
            """,
            (server_id, server_id),
        )

    def server_summary_data(
        self, server_id: int, days: int = 7
    ) -> Optional[dict]:
        """
        High-level activity stats for a server over the last N days.

        Returns a single dict ready to be passed to the weekly digest
        LLM prompt. Returns None if the server has no recent activity.

        Keys: server_name, total_messages, active_users,
              active_channels, top_users[], days
        """
        rows = self._execute(
            """
            SELECT
                s.server_name,
                COUNT(DISTINCT m.message_id) AS total_messages,
                COUNT(DISTINCT m.user_id)    AS active_users,
                COUNT(DISTINCT m.channel_id) AS active_channels
            FROM messages m
            JOIN servers s ON m.server_id = s.server_id
            WHERE m.server_id = %s
              AND m.created_at > NOW() - (%s * INTERVAL '1 day')
              AND m.is_deleted = false
            GROUP BY s.server_id, s.server_name
            """,
            (server_id, days),
        )

        if not rows:
            logger.info(f"server_summary_data: no recent messages for server {server_id}")
            return None

        summary = rows[0]

        # Top contributors — separate query keeps the main one readable
        top_users = self._execute(
            """
            SELECT
                u.current_username,
                COUNT(m.message_id)                                  AS message_count,
                ARRAY_AGG(DISTINCT m.channel_name ORDER BY m.channel_name) AS channels
            FROM messages m
            JOIN users u ON m.user_id = u.user_id
            WHERE m.server_id = %s
              AND m.created_at > NOW() - (%s * INTERVAL '1 day')
              AND m.is_deleted = false
            GROUP BY u.user_id, u.current_username
            ORDER BY message_count DESC
            LIMIT 10
            """,
            (server_id, days),
        )

        summary["top_users"] = top_users
        summary["days"] = days
        return summary

    def recent_active_users(
        self,
        server_id: int,
        days: int = 30,
        limit: int = 20,
    ) -> list[dict]:
        """
        Return the most active users in a server over the last N days.

        Good for a quick "who's driving this community?" check, or for
        identifying who to profile with the user_story LLM prompt.

        Returns rows with:
            discord_id, current_username, message_count,
            last_active, channels_used
        """
        return self._execute(
            """
            SELECT
                u.discord_id,
                u.current_username,
                COUNT(m.message_id)          AS message_count,
                MAX(m.created_at)            AS last_active,
                COUNT(DISTINCT m.channel_id) AS channels_used
            FROM messages m
            JOIN users u ON m.user_id = u.user_id
            WHERE m.server_id = %s
              AND m.created_at > NOW() - (%s * INTERVAL '1 day')
              AND m.is_deleted = false
            GROUP BY u.user_id, u.discord_id, u.current_username
            ORDER BY message_count DESC
            LIMIT %s
            """,
            (server_id, days, limit),
        )

    def search_messages(
        self,
        query: str,
        server_id: Optional[int] = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        Case-insensitive substring search across message content.

        Args:
            query:     Search term — matched with ILIKE (case-insensitive).
            server_id: Limit to one server. None searches all servers.
            limit:     Maximum rows returned.

        Note:
            ILIKE is fine for moderate data volumes. For millions of
            messages, add a GIN index with tsvector for full-text search.

        Returns rows with:
            message_id, created_at, server_name, channel_name, author, content
        """
        sql = """
            SELECT
                m.message_id,
                m.created_at,
                s.server_name,
                m.channel_name,
                u.current_username AS author,
                m.content
            FROM messages m
            JOIN servers s ON m.server_id = s.server_id
            JOIN users u   ON m.user_id = u.user_id
            WHERE m.content ILIKE %s
              AND m.is_deleted = false
        """
        params: list = [f"%{query}%"]

        if server_id is not None:
            sql += " AND m.server_id = %s"
            params.append(server_id)

        sql += " ORDER BY m.created_at DESC LIMIT %s"
        params.append(limit)

        return self._execute(sql, tuple(params))

    def find_user(self, username: str) -> list[dict]:
        """
        Find users by username (case-insensitive, partial match).

        Args:
            username: Name to search for — matched with ILIKE.

        Returns rows with: user_id, discord_id, current_username
        """
        return self._execute(
            """
            SELECT user_id, discord_id, current_username
            FROM users
            WHERE current_username ILIKE %s
            ORDER BY current_username
            """,
            (f"%{username}%",),
        )

    def all_servers(self) -> list[dict]:
        """
        Return all servers in the database.

        Returns rows with: server_id, server_name, discord_id
        """
        return self._execute(
            """
            SELECT server_id, server_name
            FROM servers
            ORDER BY server_name
            """
        )

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()


# ─── Convenience Constructor ──────────────────────────────────────────────────


def from_env() -> QueryBuilder:
    """
    Build a QueryBuilder from environment variables.

    Reads DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD from the
    environment (or .env file via python-dotenv).
    """
    dsn = (
        f"host={os.getenv('DB_HOST', 'localhost')} "
        f"port={os.getenv('DB_PORT', '5432')} "
        f"dbname={os.getenv('DB_NAME', 'discord_data')} "
        f"user={os.getenv('DB_USER', 'discord_user')} "
        f"password={os.getenv('DB_PASSWORD', '')}"
    )
    return QueryBuilder(dsn)
