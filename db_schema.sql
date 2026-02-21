-- Discord Intelligence Platform - PostgreSQL Schema
-- Layer 1: Data Collection & Storage
--
-- Run once to initialize the database:
--   psql -h localhost -U discord_user -d discord_data -f db_schema.sql


-- ─── Servers ──────────────────────────────────────────────────────────────────
-- One row per Discord server (guild) we monitor.

CREATE TABLE IF NOT EXISTS servers (
    server_id       BIGINT PRIMARY KEY,
    server_name     VARCHAR(255),
    created_at      TIMESTAMP,                          -- When the server was created on Discord
    monitored_from  TIMESTAMP DEFAULT NOW(),            -- When we started watching it
    active          BOOLEAN DEFAULT true
);


-- ─── Users ────────────────────────────────────────────────────────────────────
-- discord_id is immutable and globally unique per Discord account.
-- user_id is our internal surrogate key used for all foreign keys.

CREATE TABLE IF NOT EXISTS users (
    user_id          SERIAL PRIMARY KEY,
    discord_id       BIGINT UNIQUE NOT NULL,
    current_username VARCHAR(255),
    created_at       TIMESTAMP DEFAULT NOW(),
    last_seen        TIMESTAMP DEFAULT NOW()
);


-- ─── Username History ─────────────────────────────────────────────────────────
-- Discord users can rename themselves. We keep an audit trail instead of
-- overwriting so we can answer "who was 'oldname' in 2023?".

CREATE TABLE IF NOT EXISTS username_history (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER REFERENCES users(user_id) ON DELETE CASCADE,
    username     VARCHAR(255),                         -- The new name they changed TO
    changed_from VARCHAR(255),                         -- The name they changed FROM
    changed_at   TIMESTAMP DEFAULT NOW()
);


-- ─── Messages ─────────────────────────────────────────────────────────────────
-- Core data table. We never hard-delete rows — soft-deletes only.
-- This gives us an audit trail and lets us answer "what was deleted?" queries.

CREATE TABLE IF NOT EXISTS messages (
    message_id           BIGINT PRIMARY KEY,
    server_id            BIGINT REFERENCES servers(server_id),
    channel_id           BIGINT,
    channel_name         VARCHAR(255),
    user_id              INTEGER REFERENCES users(user_id),
    content              TEXT,
    created_at           TIMESTAMP,
    edited_at            TIMESTAMP,                    -- NULL if never edited
    deleted_at           TIMESTAMP,                    -- NULL if not deleted
    is_deleted           BOOLEAN DEFAULT false,
    reply_to_message_id  BIGINT,                       -- NULL if not a reply
    thread_id            BIGINT                        -- NULL if not in a thread
);


-- ─── Reactions ────────────────────────────────────────────────────────────────
-- Reactions are useful for sentiment analysis and identifying high-signal messages.
-- One row per (message, emoji, user) combination.

CREATE TABLE IF NOT EXISTS reactions (
    id          SERIAL PRIMARY KEY,
    message_id  BIGINT REFERENCES messages(message_id) ON DELETE CASCADE,
    emoji       VARCHAR(100),
    user_id     INTEGER REFERENCES users(user_id),
    added_at    TIMESTAMP DEFAULT NOW(),
    UNIQUE (message_id, emoji, user_id)
);


-- ─── Server Members ───────────────────────────────────────────────────────────
-- Tracks which users are (or were) in which servers.
-- left_at + is_active=false = user left the server.

CREATE TABLE IF NOT EXISTS server_members (
    id          SERIAL PRIMARY KEY,
    server_id   BIGINT REFERENCES servers(server_id),
    user_id     INTEGER REFERENCES users(user_id),
    joined_at   TIMESTAMP DEFAULT NOW(),
    left_at     TIMESTAMP,
    is_active   BOOLEAN DEFAULT true,
    UNIQUE (server_id, user_id)
);


-- ─── Analysis Cache ───────────────────────────────────────────────────────────
-- Pre-computed query results to avoid re-running expensive aggregations.
-- Layer 2 checks here before hitting the main tables.

CREATE TABLE IF NOT EXISTS analysis_cache (
    id          SERIAL PRIMARY KEY,
    query_type  VARCHAR(100),                          -- e.g. 'server_summary', 'user_story'
    parameters  JSONB,                                 -- Query parameters (server_id, date range, etc.)
    result      JSONB,
    created_at  TIMESTAMP DEFAULT NOW(),
    expires_at  TIMESTAMP                              -- NULL = never expires
);


-- ─── Indexes ──────────────────────────────────────────────────────────────────
-- Critical for performance with large message volumes (millions of rows).

-- Messages: most queries filter by server + time or user + time
CREATE INDEX IF NOT EXISTS idx_messages_server_created  ON messages (server_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_user_created    ON messages (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_channel_created ON messages (channel_id, created_at DESC);

-- Partial index: most queries exclude deleted messages, so index only live ones
CREATE INDEX IF NOT EXISTS idx_messages_live            ON messages (server_id, created_at DESC)
    WHERE is_deleted = false;

-- Users: almost always looked up by discord_id first
CREATE INDEX IF NOT EXISTS idx_users_discord_id         ON users (discord_id);

-- Server members: filtered by server + active status in membership queries
CREATE INDEX IF NOT EXISTS idx_server_members_server    ON server_members (server_id, is_active);

-- Cache: lookup by type + parameters
CREATE INDEX IF NOT EXISTS idx_analysis_cache_type      ON analysis_cache (query_type, created_at DESC);
