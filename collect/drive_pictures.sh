#!/usr/bin/env bash
# Copy the pictures and videos behind every Google Drive link posted in the collected Discord messages into the
# cold-tier S3 bucket discord-drive (Garage on RPI4B-2G-01), then check every file's hash against Drive.
#   collect/drive_pictures.sh          (re-runnable: rclone skips what is already there)
# Layout: folders/<drive folder id>/<path in that folder>, files/<file name> for single-file links.
# Needs: rclone remote `gdrive:` (read-only, Jack's Google account), the bucket key in the Keychain
# (service garage-cold-discord-drive), and k3s-01 access for the discord DB password.
set -euo pipefail
MEDIA='*.{jpg,jpeg,png,heic,heif,gif,webp,dng,raw,cr2,nef,arw,mp4,mov,m4v,avi,mkv,webm}'

export RCLONE_CONFIG_COLD_TYPE=s3 RCLONE_CONFIG_COLD_PROVIDER=Other RCLONE_CONFIG_COLD_REGION=garage \
  RCLONE_CONFIG_COLD_ENDPOINT=http://192.168.0.209:3900 RCLONE_CONFIG_COLD_FORCE_PATH_STYLE=true
RCLONE_CONFIG_COLD_ACCESS_KEY_ID=$(security find-generic-password -s garage-cold-discord-drive -a key_id -w)
RCLONE_CONFIG_COLD_SECRET_ACCESS_KEY=$(security find-generic-password -s garage-cold-discord-drive -a secret -w)
export RCLONE_CONFIG_COLD_ACCESS_KEY_ID RCLONE_CONFIG_COLD_SECRET_ACCESS_KEY
R="rclone --ignore-case --transfers 2 --tpslimit 5 --stats-one-line --stats 0"  # gentle on Google

PGPASSWORD=$(KUBECONFIG=${KUBECONFIG:-~/.kube/k3s-01} kubectl get secret discord-app -n databases -o jsonpath='{.data.password}' | base64 -d)
export PGPASSWORD
LINKS=$(psql -h 192.168.0.83 -p 5463 -U discord -d discord -At -F ' ' -c "
  select distinct case when url ~ '/folders/|folderview' then 'folder' else 'file' end,
    coalesce(substring(url from '/folders/([A-Za-z0-9_-]{10,})'), substring(url from '/file/d/([A-Za-z0-9_-]{10,})'),
             substring(url from '[?&]id=([A-Za-z0-9_-]{10,})'))
  from (select (regexp_matches(content, 'https?://drive\.google\.com/[^\s>)\]]+', 'g'))[1] url from discord.messages) l")

fail=0
while read -r kind id; do
  [ -n "$id" ] || continue
  if [ "$kind" = folder ]; then
    n=$(rclone lsf -R --files-only --ignore-case --include "$MEDIA" gdrive: --drive-root-folder-id "$id" 2>/dev/null | wc -l | tr -d ' ')
    [ "$n" -gt 0 ] || { echo "folder $id: no pictures (empty, or no access)"; continue; }
    $R copy gdrive: "cold:discord-drive/folders/$id" --drive-root-folder-id "$id" --include "$MEDIA"
    if $R check gdrive: "cold:discord-drive/folders/$id" --drive-root-folder-id "$id" --include "$MEDIA" --one-way 2>/dev/null; then
      echo "folder $id: $n files, all hashes match"
    else
      echo "folder $id: CHECK FAILED"; fail=1
    fi
  else
    $R backend copyid gdrive: "$id" cold:discord-drive/files/ 2>/dev/null && echo "file $id: copied" || echo "file $id: no access"
  fi
done <<< "$LINKS"
exit $fail
