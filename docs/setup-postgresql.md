# PostgreSQL Setup Guide

## Option 1: Docker (Recommended)

### On Proxmox

```bash
# SSH into Proxmox
ssh root@proxmox-ip

# Create data directory
mkdir -p /mnt/discord-db-data

# Go to repo directory
cd discord-intelligence/deployment

# Start PostgreSQL
docker-compose up -d postgres

# Verify it's running
docker ps | grep discord-db
```

### On Local Machine

```bash
cd deployment
docker-compose up -d postgres
```

## Option 2: Local Installation

### macOS (Homebrew)

```bash
brew install postgresql@16
brew services start postgresql@16
```

### Linux (Ubuntu/Debian)

```bash
sudo apt install postgresql postgresql-contrib
sudo systemctl start postgresql
```

## Verify Connection

```bash
psql -h localhost -U discord_user -d discord_data
```

You should see a `discord_data=#` prompt.

Type `\q` to exit.
