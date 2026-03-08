# Layer 1: Setup Guide

Layer 1 collects Discord message history into PostgreSQL using [DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter). It runs in two steps: **export** (fetch messages from Discord to JSON) and **import** (load JSON into the database).

---

## Prerequisites

- Python 3.10+
- PostgreSQL running with schema initialised — see [setup-postgresql.md](setup-postgresql.md)
- Your personal Discord token — see [setup-discord-token.md](setup-discord-token.md)
- The IDs of the Discord servers you want to export

DiscordChatExporter is downloaded automatically. No .NET required.

---

## Installation

```bash
cd layer1-collector
pip install -r requirements.txt
```

---

## Configuration

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml`:

```yaml
discord:
  token: ${DISCORD_TOKEN}   # or paste token directly (keep file out of git)
  guilds:
    - 123456789012345678    # right-click server → Copy Server ID

exporter:
  output_directory: ./exports
  after_date: ""            # leave blank for full history, or e.g. "2024-01-01"

database:
  host: localhost
  port: 5432
  name: discord_data
  user: discord_user
  password: ${DB_PASSWORD}
```

**Getting server IDs:**

1. In Discord: User Settings → Advanced → enable **Developer Mode**
2. Right-click any server → **Copy Server ID**

---

## Running Manually

### Step 1: Export

```bash
python exporter.py
```

This downloads DiscordChatExporter (first run only), then exports all channels in your configured guilds to `./exports/<guild_id>/`. One JSON file per channel.

```bash
# Export a single guild
python exporter.py --guild 123456789012345678

# Export only recent messages
python exporter.py --after 2024-01-01

# Export a single channel
python exporter.py --channel 111111111111111111
```

### Step 2: Import

```bash
python importer.py --input ./exports
```

Reads all JSON files under `./exports/` and inserts them into PostgreSQL. Running it twice is safe — existing messages are updated, not duplicated.

```bash
# Preview without writing to DB
python importer.py --input ./exports --dry-run

# Import a single guild's files
python importer.py --input ./exports/123456789012345678

# Import one file
python importer.py --input ./exports/123456789012345678/general.json
```

---

## Scheduling (Automated Updates)

Run both steps together with the provided script:

```bash
deployment/scripts/run_exporter.sh
```

### macOS (launchd — every 6 hours)

Create `~/Library/LaunchAgents/com.discord.exporter.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.discord.exporter</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/path/to/deployment/scripts/run_exporter.sh</string>
    </array>
    <key>StartInterval</key>
    <integer>21600</integer>
    <key>StandardOutPath</key>
    <string>/path/to/layer1-collector/logs/exporter.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/layer1-collector/logs/exporter.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.discord.exporter.plist
```

### Linux (cron — every 6 hours)

```bash
crontab -e

# Add:
0 */6 * * * /path/to/deployment/scripts/run_exporter.sh >> /path/to/layer1-collector/logs/exporter.log 2>&1
```

---

## Verification

After importing, check the database:

```bash
psql -h localhost -U discord_user -d discord_data
```

```sql
-- Total messages
SELECT COUNT(*) FROM messages;

-- Messages per server
SELECT s.server_name, COUNT(m.message_id) AS messages
FROM servers s
JOIN messages m USING (server_id)
GROUP BY s.server_name
ORDER BY messages DESC;

-- Most recent message
SELECT created_at, content
FROM messages
ORDER BY created_at DESC
LIMIT 5;
```

---

## Troubleshooting

### "DiscordChatExporter failed: 401 Unauthorized"

Your token has expired or is invalid. Re-extract it from your browser — see [setup-discord-token.md](setup-discord-token.md).

### "No JSON files found"

The export may have produced no output if the guild ID is wrong or your account isn't a member of that server.

### macOS: "cannot be opened because it is from an unidentified developer"

The exporter script handles this automatically with `xattr -d com.apple.quarantine`. If the error persists, run:

```bash
xattr -d com.apple.quarantine layer1-collector/bin/DiscordChatExporter.Cli
```

### "Could not connect to PostgreSQL"

Check that PostgreSQL is running and your `config.yaml` (or `.env`) has the correct host/port/credentials:

```bash
psql -h localhost -U discord_user -d discord_data -c "SELECT 1"
```

---

## What Gets Imported

| Data          | Imported? | Notes                                          |
| ------------- | --------- | ---------------------------------------------- |
| Messages      | Yes       | Including edits                                |
| Threads       | Yes       | Stored with `thread_id` set                    |
| Attachments   | Partial   | Filename noted in content; file not downloaded |
| Reactions     | No        | DCE only exports counts, not per-user data     |
| DMs           | No        | Only guild channels are supported              |
| Voice channels| No        | No message history                             |
