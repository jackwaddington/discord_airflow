#!/usr/bin/env bash
# One-off: archive every Discord file git doesn't hold (exports, SQLite, CSVs, reports) plus a pg_dump of the Mac
# discord_data DB into Garage bucket `discord`, then download it back and verify every checksum.
#   migrate/archive.sh ~/github
# Needs the Garage key in the Keychain: service garage-discord, accounts key_id and secret.
# Left out on purpose: .venv, caches, the DiscordChatExporter binary, .claude, and .env / config.yaml (Discord token).
set -euo pipefail
ROOT=$(cd "${1:?usage: archive.sh <dir holding the discord repos>}" && pwd)
NAME=discord-originals-$(date +%F)
WORK=$(mktemp -d); OUT=$WORK/$NAME; mkdir -p "$OUT" "$WORK/back" "$WORK/x"
REPOS="dischord_channel_analyser dischord_analysis dischord_voices discord_intelligence dischord_digest discord_airflow discord_philosopher"
SKIP='(^|/)(\.venv|venv|__pycache__|bin|\.claude)/|\.DS_Store$|\.pyc$|\.Rhistory$|(^|/)\.env$|(^|/)config\.yaml$'

for r in $REPOS; do
  (cd "$ROOT/$r" && { git ls-files -z -o --exclude-standard; git ls-files -z -o -i --exclude-standard; } | tr '\0' '\n') \
    | { grep -v -E "$SKIP" || true; } | sed "s|^|$r/|"  # a repo with nothing to archive is fine
done | sort -u > "$WORK/list"
[ -s "$WORK/list" ] || { echo "nothing to archive"; exit 1; }
(cd "$ROOT" && tr '\n' '\0' < "$WORK/list" | xargs -0 shasum -a 256) > "$OUT/MANIFEST.sha256"
(cd "$ROOT" && tar -czf "$OUT/files.tar.gz" -T "$WORK/list")
pg_dump -Fc -h localhost -U discord_user discord_data > "$OUT/discord_data.pgdump"
(cd "$OUT" && shasum -a 256 files.tar.gz MANIFEST.sha256 discord_data.pgdump > CHECKSUMS.sha256)
echo "built $NAME: $(wc -l < "$WORK/list" | tr -d ' ') files, $(du -sh "$OUT" | cut -f1)"

export AWS_ACCESS_KEY_ID=$(security find-generic-password -s garage-discord -a key_id -w)
export AWS_SECRET_ACCESS_KEY=$(security find-generic-password -s garage-discord -a secret -w)
export AWS_CONFIG_FILE=$WORK/aws.conf  # path-style addressing for Garage, without touching ~/.aws
printf '[default]\nregion = garage\ns3 =\n  addressing_style = path\n' > "$AWS_CONFIG_FILE"
S3="aws --endpoint-url http://192.168.0.83:3900 s3"
$S3 cp --recursive --only-show-errors "$OUT" "s3://discord/originals/$NAME/"

# Verify from what Garage gives back, not from the local copy.
$S3 cp --recursive --only-show-errors "s3://discord/originals/$NAME/" "$WORK/back/"
(cd "$WORK/back" && shasum -a 256 -c --quiet CHECKSUMS.sha256)
tar -xzf "$WORK/back/files.tar.gz" -C "$WORK/x"
(cd "$WORK/x" && shasum -a 256 -c --quiet "$WORK/back/MANIFEST.sha256")
[ "$(wc -l < "$WORK/back/MANIFEST.sha256")" -eq "$(wc -l < "$WORK/list")" ]
pg_restore -l "$WORK/back/discord_data.pgdump" > /dev/null
echo "verified s3://discord/originals/$NAME/: every file's sha256 matches after a round trip through Garage"
rm -rf "$WORK"
