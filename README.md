# discord_airflow

Automated Discord community intelligence using Apache Airflow. Collects message history from Discord servers, computes weekly activity statistics, and (optionally) generates LLM summaries — all orchestrated as a scheduled pipeline with a web UI for monitoring.

The LLM aspect is interesting - a bit like a human it is quite accurate most of the time. However, when your friend starts talking like they are an authority on the proposed Helsinki-Tallinn tunnel... 🤔 That's were we need to fact-check. 

---

## Architecture

The project is structured in three layers, each independent and testable on its own:

```
┌─────────────────────────────────────────────────────┐
│  Layer 1 — Collect                                  │
│  collectors/exporter.py  →  collectors/importer.py  │
│                                                     │
│  DiscordChatExporter CLI fetches message history    │
│  from Discord using a personal token and saves      │
│  JSON files. The importer loads those into          │
│  PostgreSQL — idempotently, safe to re-run.         │
└────────────────────┬────────────────────────────────┘
                     │ PostgreSQL (discord_data)
┌────────────────────▼────────────────────────────────┐
│  Layer 2 — Query                                    │
│  queries/query_builder.py  +  queries/chunker.py    │
│                                                     │
│  Parameterised SQL queries over the message store:  │
│  activity stats, top users, cross-server members,   │
│  message search, channel history. Pure SQL —        │
│  always reliable, no hallucination risk.            │
└────────────────────┬────────────────────────────────┘
                     │ structured data
┌────────────────────▼────────────────────────────────┐
│  Layer 3 — Analyse                                  │
│  analysis/processor.py  +  analysis/prompts/        │
│                                                     │
│  Feeds Layer 2 output to a local Ollama LLM         │
│  (Mistral by default) to generate written           │
│  summaries, user profiles, and digests.             │
│  Verify LLM output against raw data —               │
│  treat narratives as a starting point, not          │
│  ground truth.                                      │
└─────────────────────────────────────────────────────┘
```

Apache Airflow sits above all three layers — it schedules when each runs, manages dependencies between tasks, retries on failure, and provides a web UI to see run history and logs.

---

## What the DAG does

`dags/discord_weekly.py` runs every Sunday at 09:00 and produces a markdown report in `reports/`:

```
compute_stats  →  save_report
```

**compute_stats** — queries PostgreSQL for the past 7 days:
- Total messages, unique posters, active channels per server
- Top 5 contributors per server
- Top 5 most active channels
- New users seen for the first time

**save_report** — writes `reports/YYYY-MM-DD.md`

Trigger manually anytime via the Airflow UI. Pass `{"days": 14}` in the run config to change the look-back window.

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) — runs Airflow
- PostgreSQL — your existing `discord_data` database (does not need to be in Docker)
- Discord data already imported — see [Layer 1 setup](#layer-1-setup)

---

## Quick start

```bash
# 1. Clone and enter the repo
git clone https://github.com/yourname/discord_airflow
cd discord_airflow

# 2. Copy and fill in your credentials
cp .env.example .env
# Edit .env: add DB_PASSWORD, DISCORD_TOKEN

# 3. Copy and fill in your server config
cp config.example.yaml config.yaml
# Edit config.yaml: add your Discord server IDs

# 4. Initialise Airflow (first time only — creates DB tables and admin user)
docker compose up airflow-init

# 5. Start Airflow
docker compose up -d

# 6. Open the UI
open http://localhost:8080
# Login: airflow / airflow
```

Find `discord_weekly` in the DAG list, enable it, and click **Trigger DAG** to run immediately.

---

## Layer 1 setup

Before Airflow can query anything, you need Discord data in PostgreSQL.

```bash
# Create the schema (first time only)
psql -h localhost -U discord_user -d discord_data -f db_schema.sql

# Export from Discord
python3 collectors/exporter.py --after 2024-01-01

# Import into PostgreSQL
python3 collectors/importer.py --input collectors/exports
```

See [docs/layer1-setup.md](docs/layer1-setup.md) for full details including scheduling the export.

---

## Configuration

| Variable | Description |
| -------- | ----------- |
| `DISCORD_TOKEN` | Personal Discord token (not a bot token) |
| `DB_HOST` | PostgreSQL host (`host.docker.internal` for local Mac) |
| `DB_NAME` | Database name (default: `discord_data`) |
| `DB_USER` | Database user |
| `DB_PASSWORD` | Database password |
| `OLLAMA_HOST` | Ollama server URL (for LLM tasks) |
| `OLLAMA_MODEL` | Model name (default: `mistral`) |

---

## Adding more tasks

The DAG is designed to grow. Planned additions:

- `compute_connectors` — users active across multiple servers
- `compute_highlights` — most reacted messages of the week
- `compute_health` — monthly membership join/leave trends
- `llm_digest` — LLM narrative summary per server (requires Ollama)

Each becomes a new `PythonOperator` in `dags/discord_weekly.py`, feeding into `save_report`.

---

## Deployment

Runs locally with Docker Compose for development. Designed to deploy to Kubernetes (k3s or similar) using the [official Airflow Helm chart](https://airflow.apache.org/docs/helm-chart/).
