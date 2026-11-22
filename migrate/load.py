"""One-off: load every Discord store on the Mac into the CNPG `discord` database, then prove nothing is missing.

    DATABASE_URL=postgresql://discord:...@192.168.0.83:5463/discord python migrate/load.py ~/github

    DATABASE_URL=... python migrate/load.py ~/github --exports discord_airflow/exports/<run>/<guild>   (a new collect run only)

Safe to re-run (raw rows are upserts, legacy copies are dropped and recopied). Exits non-zero if the
reconciliation finds anything missing. What goes where:
  raw.export_file       one row per DiscordChatExporter JSON file (header + sha256), incl. a salvaged truncated one
  raw.message_version   every distinct version of every message, full JSON, keyed (message_id, sha256 of the JSON)
  legacy_*              verbatim copies of the old SQLite and Postgres stores (SQLite columns as text)
  discord.*             views: latest version of each message, channels, reactions
Original files (and a pg_dump of the Mac DB) go to Garage via migrate/archive.sh, not here.
"""
import collections, hashlib, json, os, pathlib, sqlite3, sys
import psycopg

ROOT = pathlib.Path(sys.argv[1]).expanduser()
EXPORT_DIRS = ["dischord_analysis/discord-export", "discord_intelligence/layer1-collector/exports", "discord_airflow/exports"]
SQLITES = {"legacy_hive_sqlite": "dischord_analysis/processed/discord.db",
           "legacy_channel_analyser": "dischord_channel_analyser/data/discord.db"}
LEGACY_PG = os.environ.get("LEGACY_PG", "postgresql://discord_user@localhost/discord_data")  # -> legacy_discord_data

DDL = """
create schema if not exists raw;
create table if not exists raw.export_file (
  source_path text primary key, sha256 text not null, bytes bigint not null,
  guild jsonb, channel jsonb, date_range jsonb, exported_at timestamptz, message_count int,
  messages_read int not null, parse_error text);
create table if not exists raw.message_version (
  message_id bigint not null, raw_sha256 text not null, raw jsonb not null,
  guild_id bigint, channel_id bigint, exported_at timestamptz, source_path text not null,
  primary key (message_id, raw_sha256));
create schema if not exists discord;
create or replace view discord.messages as
  select distinct on (message_id) message_id, guild_id, channel_id,
    (raw->>'timestamp')::timestamptz as sent_at, (raw->>'timestampEdited')::timestamptz as edited_at,
    (raw->'author'->>'id')::bigint as author_id, raw->'author'->>'name' as author_name,
    raw->>'content' as content, raw->>'type' as type, (raw->'reference'->>'messageId')::bigint as reply_to, raw
  from raw.message_version order by message_id, exported_at desc nulls last, raw_sha256;
create or replace view discord.channels as
  select distinct on ((channel->>'id')::bigint) (channel->>'id')::bigint as channel_id, (guild->>'id')::bigint as guild_id,
    guild->>'name' as guild_name, channel->>'category' as category, channel->>'name' as name, channel->>'type' as type
  from raw.export_file where channel is not null order by (channel->>'id')::bigint, exported_at desc nulls last;
create or replace view discord.reactions as
  select m.message_id, r->'emoji'->>'name' as emoji, (r->>'count')::int as count
  from discord.messages m, jsonb_array_elements(m.raw->'reactions') r;
"""


def read_export(text):
    """Parse a DiscordChatExporter file. A truncated file keeps its header and every complete message."""
    try:
        return json.loads(text), None
    except json.JSONDecodeError as e:
        err = f"truncated, salvaged: {e}"
    cut = text.index('"messages"')
    doc = json.loads(text[:cut].rstrip().rstrip(",") + "}")
    dec, i, msgs = json.JSONDecoder(), text.index("[", cut) + 1, []
    while True:
        while i < len(text) and text[i] in " \t\r\n,":
            i += 1
        try:
            m, i = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            break
        msgs.append(m)
    doc["messages"] = msgs
    return doc, err


def load_exports(db, dirs=EXPORT_DIRS):
    files, versions, json_ids = [], {}, set()
    for d in dirs:
        for p in sorted((ROOT / d).rglob("*.json")):
            b = p.read_bytes()
            doc, err = read_export(b.decode("utf-8"))
            rel, exp = str(p.relative_to(ROOT)), doc.get("exportedAt")
            files.append((rel, hashlib.sha256(b).hexdigest(), len(b), json.dumps(doc.get("guild")), json.dumps(doc.get("channel")),
                          json.dumps(doc.get("dateRange")), exp, doc.get("messageCount"), len(doc["messages"]), err))
            for m in doc["messages"]:
                s = json.dumps(m, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                key = (int(m["id"]), hashlib.sha256(s.encode()).hexdigest())
                json_ids.add(key[0])
                if key not in versions or (exp or "") > (versions[key][3] or ""):  # keep the newest export as source
                    versions[key] = (s, doc["guild"]["id"], doc["channel"]["id"], exp, rel)
    with db.cursor() as c:
        c.execute("create temp table f (like raw.export_file) on commit drop")
        with c.copy("copy f from stdin") as cp:
            for r in files:
                cp.write_row(r)
        c.execute("insert into raw.export_file select * from f on conflict (source_path) do update set "
                  "sha256=excluded.sha256, bytes=excluded.bytes, guild=excluded.guild, channel=excluded.channel, "
                  "date_range=excluded.date_range, exported_at=excluded.exported_at, message_count=excluded.message_count, "
                  "messages_read=excluded.messages_read, parse_error=excluded.parse_error")
        c.execute("create temp table v (like raw.message_version) on commit drop")
        with c.copy("copy v from stdin") as cp:
            for (mid, sha), (s, g, ch, exp, rel) in versions.items():
                cp.write_row((mid, sha, s, g, ch, exp, rel))
        c.execute("insert into raw.message_version select * from v on conflict (message_id, raw_sha256) do update set "
                  "exported_at=excluded.exported_at, source_path=excluded.source_path "
                  "where excluded.exported_at > raw.message_version.exported_at")
    db.commit()
    print(f"exports: {len(files)} files, {len(versions)} distinct message versions, {len(json_ids)} messages")
    return files, versions, json_ids


def copy_sqlite(db, schema, path):
    """Copy every table verbatim. Columns become text: SQLite is untyped, text keeps every value exactly."""
    src, ids = sqlite3.connect(f"file:{path}?mode=ro", uri=True), set()
    with db.cursor() as c:
        c.execute(f'drop schema if exists "{schema}" cascade; create schema "{schema}"')
        for (t,) in src.execute("select name from sqlite_master where type='table' order by name"):
            cols = [r[1] for r in src.execute(f'pragma table_info("{t}")')]
            c.execute(f'create table "{schema}"."{t}" ({", ".join(f"{q(x)} text" for x in cols)})')
            rows = [tuple(None if v is None else str(v) for v in r) for r in src.execute(f'select * from "{t}"')]
            assert not any(isinstance(v, bytes) for r in src.execute(f'select * from "{t}" limit 1000') for v in r), t
            with c.copy(f'copy "{schema}"."{t}" from stdin') as cp:
                for r in rows:
                    cp.write_row(r)
            c.execute(f'select * from "{schema}"."{t}"')
            ok = collections.Counter(rows) == collections.Counter(c.fetchall())
            print(f"  {schema}.{t}: {len(rows)} rows {'OK' if ok else 'MISMATCH'}")
            if not ok:
                raise SystemExit(f"{schema}.{t} copy does not match source")
            if t == "messages":
                ids |= {int(r[0]) for r in src.execute("select message_id from messages")}
    db.commit()
    return ids


def copy_pg(db, schema, url):
    """Copy every public table with its column types; COPY is byte-exact. Checked by an md5 over all rows."""
    ids = set()
    with psycopg.connect(url) as src, src.cursor() as s, db.cursor() as c:
        c.execute(f'drop schema if exists "{schema}" cascade; create schema "{schema}"')
        s.execute("select table_name from information_schema.tables where table_schema='public' and table_type='BASE TABLE' order by 1")
        for (t,) in s.fetchall():
            s.execute("select attname, format_type(atttypid, atttypmod) from pg_attribute "
                      "where attrelid = %s::regclass and attnum > 0 and not attisdropped order by attnum", (f'public."{t}"',))
            c.execute(f'create table "{schema}"."{t}" ({", ".join(f"{q(n)} {ty}" for n, ty in s.fetchall())})')
            with s.copy(f'copy public."{t}" to stdout') as out, c.copy(f'copy "{schema}"."{t}" from stdin') as inp:
                for chunk in out:
                    inp.write(chunk)
            check = "select count(*), md5(coalesce(string_agg(row_to_json(x)::text, E'\\n' order by row_to_json(x)::text collate \"C\"), '')) from {} x"
            # json + byte order: row text (x::text) quotes values by the OS's isspace(), which differs Mac vs Linux on emoji bytes
            s.execute(check.format(f'public."{t}"'))
            c.execute(check.format(f'"{schema}"."{t}"'))
            a, b = s.fetchone(), c.fetchone()
            print(f"  {schema}.{t}: {a[0]} rows {'OK' if a == b else 'MISMATCH'}")
            if a != b:
                raise SystemExit(f"{schema}.{t} copy does not match source")
        s.execute("select message_id from public.messages")
        ids = {r[0] for r in s.fetchall()}
    db.commit()
    return ids


def q(name):
    return '"' + name.replace('"', '""') + '"'


def load_new(dirs):
    """--exports mode: load only these export folders (a fresh collect run) and check every message landed."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as db:
        db.execute(DDL)
        db.commit()
        files, versions, json_ids = load_exports(db, dirs)
        db_ids = {r[0] for r in db.execute("select message_id from raw.message_version where message_id = any(%s)", (list(json_ids),))}
        missing = json_ids - db_ids
        print(f"reconcile: {len(json_ids)} messages in these files, {len(missing)} missing")
        if missing:
            raise SystemExit("RECONCILIATION FAILED")


def main():
    if "--exports" in sys.argv:
        return load_new(sys.argv[sys.argv.index("--exports") + 1:])
    with psycopg.connect(os.environ["DATABASE_URL"]) as db:
        db.execute(DDL)
        db.commit()
        files, versions, json_ids = load_exports(db)
        legacy = set()
        for schema, path in SQLITES.items():
            legacy |= copy_sqlite(db, schema, ROOT / path)
        legacy |= copy_pg(db, "legacy_discord_data", LEGACY_PG)

        # Reconcile: every message id from every store, every distinct version and every file must be in the DB.
        db_ids = {r[0] for r in db.execute("select distinct message_id from raw.message_version")}
        db_versions = db.execute("select count(*) from raw.message_version").fetchone()[0]
        db_files = db.execute("select count(*) from raw.export_file").fetchone()[0]
        missing = (json_ids | legacy) - db_ids
        short = [f[0] for f in files if f[7] is not None and f[8] != f[7] and f[9] is None]
        print(f"reconcile: {len(json_ids | legacy)} message ids across all stores, {len(db_ids)} in raw.message_version, "
              f"{len(missing)} missing; {db_versions}/{len(versions)} versions; {db_files}/{len(files)} files")
        for f in files:
            if f[9]:
                print(f"  note: {f[0]}: {f[9]} ({f[8]} of {f[7]} messages kept)")
        if short:
            print(f"  files whose messageCount != messages read: {short}")
        if missing or db_versions < len(versions) or db_files < len(files) or short:
            raise SystemExit("RECONCILIATION FAILED")
        print("reconcile: OK, nothing missing")


if __name__ == "__main__":
    main()
