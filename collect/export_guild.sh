#!/usr/bin/env bash
# Export one server's full history (every channel, every thread incl. archived forum posts) with Jack's personal
# token from .env, then load it into CNPG `discord` and check every exported message landed. Run from the Mac,
# one server at a time:   collect/export_guild.sh <guild id>      (ids: collectors/bin/DiscordChatExporter.Cli guilds)
# Needs: KUBECONFIG-reachable k3s-01 (for the DB password) and a python with psycopg (PYTHON=..., migrate/requirements.txt).
# shortcut: full history on every run; switch to per-channel --after <last message id> once every server has its first full run.
set -euo pipefail
cd "$(dirname "$0")/.."
G=${1:?usage: export_guild.sh <guild id>}
OUT=exports/$(date +%F)-full/$G
rc=0
DISCORD_TOKEN=$(grep '^DISCORD_TOKEN=' .env | cut -d= -f2- | tr -d "\"'") \
  collectors/bin/DiscordChatExporter.Cli exportguild -g "$G" -f Json -o "$OUT/" --include-threads All || rc=$?
[ -d "$OUT" ] || { echo "nothing exported for $G (exit $rc)"; exit 1; }

P=$(KUBECONFIG=${KUBECONFIG:-~/.kube/k3s-01} kubectl get secret discord-app -n databases -o jsonpath='{.data.password}' | base64 -d)
DATABASE_URL="postgresql://discord:$P@192.168.0.83:5463/discord" "${PYTHON:-python3}" migrate/load.py .. --exports "discord_airflow/$OUT"
[ "$rc" -eq 0 ] || echo "WARNING: the exporter exited $rc (some channels failed); everything it did export is loaded"
exit "$rc"
