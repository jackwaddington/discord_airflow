# Discord Bot Token Setup

## Create a Discord Application

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click "New Application"
3. Name it: "Discord Archive"
4. Accept the terms
5. Click "Create"

## Create a Bot

1. Go to the "Bot" section (left sidebar)
2. Click "Add Bot"
3. Under "TOKEN", click "Copy" (this is your DISCORD_TOKEN)
4. **IMPORTANT:** Keep this secret! Never commit to GitHub!

## Set Bot Permissions

1. Go to "OAuth2" section (left sidebar)
2. Click "URL Generator"
3. Under "SCOPES", select: `bot`
4. Under "PERMISSIONS", select:
   - `View Channels`
   - `Read Message History`
5. Copy the generated URL

## Add Bot to Your Servers

1. Paste the URL from step above into your browser
2. Select each server you want to monitor
3. Click "Authorize"
4. Verify the bot appears in each server

## Add Token to .env

```bash
cp .env.example .env
# Edit .env and set:
DISCORD_TOKEN=your_token_from_step_3
```

**Never commit .env to GitHub!**
