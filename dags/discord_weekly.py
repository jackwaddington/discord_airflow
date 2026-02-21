"""
Discord Weekly Stats DAG

Runs every Sunday at 09:00 and produces a markdown stats report
covering the previous 7 days across all Discord servers.

Tasks:
    compute_stats  — queries PostgreSQL for activity metrics
    save_report    — writes results to reports/YYYY-MM-DD.md

No LLM involved — these are pure SQL numbers, always reliable.

Trigger manually in the Airflow UI, or wait for Sunday 09:00.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow import DAG
from airflow.operators.python import PythonOperator

# ── Path setup ────────────────────────────────────────────────────────────────
# Airflow mounts our project folders as volumes — add them to sys.path
# so we can import query_builder from within tasks.
AIRFLOW_HOME = Path("/opt/airflow")
sys.path.insert(0, str(AIRFLOW_HOME / "queries"))

# ── DAG default args ──────────────────────────────────────────────────────────
default_args = {
    "owner": "airflow",
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

# ── Task functions ─────────────────────────────────────────────────────────────


def compute_stats(**context) -> dict:
    """
    Query PostgreSQL for weekly activity stats across all servers.

    Returns a dict pushed to XCom so save_report can pick it up.
    Each key is a server name; value is a stats dict.
    """
    import psycopg2
    import psycopg2.extras

    dsn = (
        f"host={os.getenv('DB_HOST', 'host.docker.internal')} "
        f"port={os.getenv('DB_PORT', '5432')} "
        f"dbname={os.getenv('DB_NAME', 'discord_data')} "
        f"user={os.getenv('DB_USER', 'discord_user')} "
        f"password={os.getenv('DB_PASSWORD', '')}"
    )

    days = int(context["dag_run"].conf.get("days", 7))

    conn = psycopg2.connect(dsn)
    conn.autocommit = True

    def q(sql, params=()):
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    # Overall stats per server
    server_stats = q(
        """
        SELECT
            s.server_id,
            s.server_name,
            COUNT(DISTINCT m.message_id)    AS total_messages,
            COUNT(DISTINCT m.user_id)       AS unique_posters,
            COUNT(DISTINCT m.channel_id)    AS active_channels,
            COUNT(DISTINCT
                CASE WHEN m.thread_id IS NOT NULL
                THEN m.message_id END)      AS thread_messages
        FROM servers s
        LEFT JOIN messages m
            ON s.server_id = m.server_id
            AND m.created_at > NOW() - (%s * INTERVAL '1 day')
            AND m.is_deleted = false
        GROUP BY s.server_id, s.server_name
        ORDER BY total_messages DESC
        """,
        (days,),
    )

    results = {}
    for srv in server_stats:
        sid = srv["server_id"]
        name = srv["server_name"]

        # Top 5 users
        top_users = q(
            """
            SELECT u.current_username, COUNT(*) AS messages
            FROM messages m
            JOIN users u ON m.user_id = u.user_id
            WHERE m.server_id = %s
              AND m.created_at > NOW() - (%s * INTERVAL '1 day')
              AND m.is_deleted = false
            GROUP BY u.user_id, u.current_username
            ORDER BY messages DESC
            LIMIT 5
            """,
            (sid, days),
        )

        # Top 5 channels
        top_channels = q(
            """
            SELECT channel_name, COUNT(*) AS messages
            FROM messages
            WHERE server_id = %s
              AND created_at > NOW() - (%s * INTERVAL '1 day')
              AND is_deleted = false
              AND thread_id IS NULL
            GROUP BY channel_name
            ORDER BY messages DESC
            LIMIT 5
            """,
            (sid, days),
        )

        # New users seen for first time in this window
        new_users = q(
            """
            SELECT COUNT(DISTINCT u.user_id) AS count
            FROM users u
            WHERE u.created_at > NOW() - (%s * INTERVAL '1 day')
              AND EXISTS (
                  SELECT 1 FROM messages m
                  WHERE m.user_id = u.user_id AND m.server_id = %s
              )
            """,
            (days, sid),
        )

        results[name] = {
            "server_id": sid,
            "days": days,
            "total_messages": srv["total_messages"] or 0,
            "unique_posters": srv["unique_posters"] or 0,
            "active_channels": srv["active_channels"] or 0,
            "thread_messages": srv["thread_messages"] or 0,
            "new_users": new_users[0]["count"] if new_users else 0,
            "top_users": [{"username": u["current_username"], "messages": u["messages"]} for u in top_users],
            "top_channels": [{"channel": c["channel_name"], "messages": c["messages"]} for c in top_channels],
        }

    conn.close()

    # Push to XCom so save_report can access it
    context["ti"].xcom_push(key="stats", value=results)
    print(f"Stats computed for {len(results)} servers over {days} days")
    return results


def save_report(**context) -> None:
    """
    Pull stats from XCom and write a markdown report to reports/.
    """
    stats = context["ti"].xcom_pull(task_ids="compute_stats", key="stats")
    if not stats:
        raise ValueError("No stats received from compute_stats task")

    run_date = context["ds"]  # YYYY-MM-DD of the DAG run
    days = next(iter(stats.values()), {}).get("days", 7)

    lines = [
        f"# Discord Weekly Stats — {run_date} (last {days} days)\n",
        "_Generated by discord_airflow · pure SQL, no LLM_\n",
        "---\n",
    ]

    for server_name, s in sorted(stats.items(), key=lambda x: -x[1]["total_messages"]):
        lines.append(f"\n## {server_name}\n")

        if s["total_messages"] == 0:
            lines.append("_No activity this period._\n")
            continue

        lines.append(
            f"| Metric | Value |\n"
            f"| ------ | ----- |\n"
            f"| Messages | {s['total_messages']} |\n"
            f"| Unique posters | {s['unique_posters']} |\n"
            f"| Active channels | {s['active_channels']} |\n"
            f"| Thread messages | {s['thread_messages']} |\n"
            f"| New users | {s['new_users']} |\n"
        )

        if s["top_users"]:
            lines.append("\n**Top contributors:**\n")
            for u in s["top_users"]:
                lines.append(f"- **{u['username']}** — {u['messages']} messages")
            lines.append("")

        if s["top_channels"]:
            lines.append("\n**Most active channels:**\n")
            for c in s["top_channels"]:
                lines.append(f"- #{c['channel']} — {c['messages']} messages")
            lines.append("")

    report = "\n".join(lines)

    output_dir = Path("/opt/airflow/reports")
    output_dir.mkdir(exist_ok=True)
    output_file = output_dir / f"{run_date}.md"
    output_file.write_text(report, encoding="utf-8")

    print(f"Report saved to {output_file}")
    print(f"Total servers: {len(stats)}")


# ── DAG definition ─────────────────────────────────────────────────────────────

with DAG(
    dag_id="discord_weekly",
    description="Weekly Discord activity stats — pure SQL, no LLM",
    default_args=default_args,
    start_date=datetime(2026, 1, 1),
    schedule="0 9 * * 0",   # Every Sunday at 09:00
    catchup=False,
    tags=["discord", "stats"],
    params={"days": 7},     # Override via UI: Trigger DAG → Conf → {"days": 14}
) as dag:

    t1 = PythonOperator(
        task_id="compute_stats",
        python_callable=compute_stats,
    )

    t2 = PythonOperator(
        task_id="save_report",
        python_callable=save_report,
    )

    # compute_stats must finish before save_report starts
    t1 >> t2
