"""
Discord Data Exporter - Layer 1

Downloads DiscordChatExporter CLI (if not already installed) and runs it
to export channel/server history as JSON files for later import.

Usage:
    python exporter.py                     # uses config.yaml
    python exporter.py --config my.yaml   # custom config path
    python exporter.py --guild 123456789  # override: export one guild
    python exporter.py --after 2024-01-01 # only messages after this date

DiscordChatExporter is maintained by Tyrrrz:
    https://github.com/Tyrrrz/DiscordChatExporter

Configuration via config.yaml (see config.example.yaml) or .env:
    DISCORD_TOKEN - your personal Discord token (NOT a bot token)

Getting your personal token: see docs/setup-discord-token.md
"""

import argparse
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Optional

import requests
import yaml
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ─── DiscordChatExporter Download ─────────────────────────────────────────────

DCE_GITHUB_REPO = "Tyrrrz/DiscordChatExporter"
DCE_API_URL = f"https://api.github.com/repos/{DCE_GITHUB_REPO}/releases/latest"
BIN_DIR = Path(__file__).parent / "bin"


def _platform_asset_name() -> str:
    """
    Return the DCE release asset filename for the current platform.

    DCE ships self-contained binaries so .NET is not required.
    """
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
        return f"DiscordChatExporter.Cli.osx-{arch}.zip"
    elif system == "linux":
        return "DiscordChatExporter.Cli.linux-x64.zip"
    elif system == "windows":
        return "DiscordChatExporter.Cli.win-x64.zip"
    else:
        raise RuntimeError(
            f"Unsupported platform: {system}. "
            "Download DiscordChatExporter manually from "
            "https://github.com/Tyrrrz/DiscordChatExporter/releases"
        )


def _find_cli() -> Optional[Path]:
    """
    Find DiscordChatExporter.Cli binary.

    Checks (in order):
    1. ./bin/DiscordChatExporter.Cli  (local install)
    2. PATH                            (system install or dotnet global tool)
    """
    local = BIN_DIR / "DiscordChatExporter.Cli"
    if local.exists():
        return local

    found = shutil.which("DiscordChatExporter.Cli")
    if found:
        return Path(found)

    return None


def ensure_cli() -> Path:
    """
    Return path to DiscordChatExporter.Cli, downloading it if necessary.

    Downloads the latest release binary for the current platform into
    layer1-collector/bin/ and makes it executable.

    Raises:
        RuntimeError: if download or extraction fails.
    """
    existing = _find_cli()
    if existing:
        logger.debug(f"DiscordChatExporter.Cli found at {existing}")
        return existing

    logger.info("DiscordChatExporter.Cli not found — downloading latest release...")
    BIN_DIR.mkdir(exist_ok=True)

    # Fetch latest release metadata from GitHub
    try:
        resp = requests.get(DCE_API_URL, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not fetch DCE release info from GitHub: {exc}\n"
            "Check your internet connection, or download manually from "
            "https://github.com/Tyrrrz/DiscordChatExporter/releases"
        )

    release = resp.json()
    version = release.get("tag_name", "unknown")
    asset_name = _platform_asset_name()

    asset = next(
        (a for a in release.get("assets", []) if a["name"] == asset_name),
        None,
    )
    if not asset:
        available = [a["name"] for a in release.get("assets", [])]
        raise RuntimeError(
            f"Release {version} does not contain {asset_name}.\n"
            f"Available assets: {available}"
        )

    # Download the zip
    logger.info(f"Downloading {asset_name} ({version})...")
    zip_path = BIN_DIR / asset_name
    try:
        with requests.get(
            asset["browser_download_url"], stream=True, timeout=120
        ) as r:
            r.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
    except requests.RequestException as exc:
        raise RuntimeError(f"Download failed: {exc}")

    # Extract
    logger.info("Extracting...")
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(BIN_DIR)
    zip_path.unlink()

    cli = BIN_DIR / "DiscordChatExporter.Cli"
    if not cli.exists():
        # Some releases put it in a subdirectory — search for it
        found = list(BIN_DIR.rglob("DiscordChatExporter.Cli"))
        if not found:
            raise RuntimeError(
                f"Could not find DiscordChatExporter.Cli after extracting {asset_name}. "
                f"Contents of {BIN_DIR}: {list(BIN_DIR.iterdir())}"
            )
        cli = found[0]

    # Make executable on Unix
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # macOS: remove quarantine flag that blocks unsigned binaries
    if platform.system() == "Darwin":
        try:
            subprocess.run(
                ["xattr", "-d", "com.apple.quarantine", str(cli)],
                capture_output=True,  # ignore errors — flag may not be set
            )
        except FileNotFoundError:
            pass  # xattr not available (rare)

    logger.info(f"DiscordChatExporter.Cli ready at {cli}")
    return cli


# ─── Export Functions ─────────────────────────────────────────────────────────


def export_guild(
    cli: Path,
    token: str,
    guild_id: str,
    output_dir: Path,
    after: Optional[str] = None,
    before: Optional[str] = None,
) -> list[Path]:
    """
    Export all text channels in a guild to JSON files.

    One JSON file is created per channel. DCE names them:
        "{GuildName} - {ChannelName} [{ChannelId}].json"

    Args:
        cli:        Path to DiscordChatExporter.Cli binary.
        token:      Personal Discord token.
        guild_id:   Discord server (guild) ID.
        output_dir: Directory to write JSON files into.
        after:      Only export messages after this date (ISO 8601 or Discord snowflake).
        before:     Only export messages before this date.

    Returns:
        List of paths to the generated JSON files.

    Raises:
        RuntimeError: if DCE exits with a non-zero status.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    files_before = set(output_dir.glob("*.json"))

    cmd = [
        str(cli), "exportguild",
        "--token", token,
        "--guild", guild_id,
        "--format", "Json",
        "--output", str(output_dir),
        "--media", "false",      # skip downloading attachments
        "--reuse-media", "false",
    ]
    if after:
        cmd += ["--after", after]
    if before:
        cmd += ["--before", before]

    logger.info(f"Exporting guild {guild_id} → {output_dir}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(
            f"DiscordChatExporter failed for guild {guild_id} "
            f"(exit {result.returncode}):\n{result.stderr or result.stdout}"
        )

    new_files = [f for f in output_dir.glob("*.json") if f not in files_before]
    logger.info(f"Guild {guild_id}: exported {len(new_files)} channel file(s)")
    return new_files


def export_channel(
    cli: Path,
    token: str,
    channel_id: str,
    output_dir: Path,
    after: Optional[str] = None,
    before: Optional[str] = None,
) -> Optional[Path]:
    """
    Export a single channel to a JSON file.

    Args:
        cli:        Path to DiscordChatExporter.Cli binary.
        token:      Personal Discord token.
        channel_id: Discord channel ID.
        output_dir: Directory to write the JSON file into.
        after:      Only export messages after this date.
        before:     Only export messages before this date.

    Returns:
        Path to the generated JSON file, or None if nothing was exported.

    Raises:
        RuntimeError: if DCE exits with a non-zero status.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    files_before = set(output_dir.glob("*.json"))

    cmd = [
        str(cli), "export",
        "--token", token,
        "--channel", channel_id,
        "--format", "Json",
        "--output", str(output_dir),
        "--media", "false",
    ]
    if after:
        cmd += ["--after", after]
    if before:
        cmd += ["--before", before]

    logger.info(f"Exporting channel {channel_id} → {output_dir}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(
            f"DiscordChatExporter failed for channel {channel_id} "
            f"(exit {result.returncode}):\n{result.stderr or result.stdout}"
        )

    new_files = [f for f in output_dir.glob("*.json") if f not in files_before]
    if not new_files:
        logger.warning(f"Channel {channel_id}: no output file created (channel may be empty)")
        return None

    return new_files[0]


# ─── Config Loader ────────────────────────────────────────────────────────────


def load_config(config_path: str = "config.yaml") -> dict:
    """
    Load YAML config, substituting ${VAR} and ${VAR:default} env var references.

    Uses environment variables (and .env file) as the source of truth for
    secrets. The config file itself can be committed — secrets stay in .env.
    """
    with open(config_path, encoding="utf-8") as f:
        content = f.read()

    def _replace(match: re.Match) -> str:
        var = match.group(1)
        default = match.group(2) or ""
        return os.environ.get(var, default)

    content = re.sub(r"\$\{(\w+)(?::([^}]*))?\}", _replace, content)
    return yaml.safe_load(content)


# ─── Entry Point ─────────────────────────────────────────────────────────────


def main() -> None:
    """Export Discord data to JSON based on config.yaml."""
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Export Discord data using DiscordChatExporter"
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    parser.add_argument("--guild", help="Export a single guild ID (overrides config)")
    parser.add_argument("--channel", help="Export a single channel ID (overrides config)")
    parser.add_argument(
        "--after",
        help="Only export messages after this date (e.g. 2024-01-01)",
    )
    parser.add_argument(
        "--before",
        help="Only export messages before this date",
    )
    args = parser.parse_args()

    # Load config
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        logger.error(
            f"Config file not found: {args.config}\n"
            "Copy config.example.yaml to config.yaml and fill in your values."
        )
        sys.exit(1)

    discord_cfg = config.get("discord", {})
    token = discord_cfg.get("token") or os.getenv("DISCORD_TOKEN")
    if not token:
        logger.error(
            "Discord token not set. Add DISCORD_TOKEN to .env "
            "or set discord.token in config.yaml.\n"
            "See docs/setup-discord-token.md for how to get your token."
        )
        sys.exit(1)

    output_dir = Path(
        config.get("exporter", {}).get("output_directory", "./exports")
    )
    after = args.after or config.get("exporter", {}).get("after_date") or None
    before = args.before or None

    # Ensure DCE is available
    cli = ensure_cli()

    exported_files: list[Path] = []

    # CLI overrides
    if args.guild:
        files = export_guild(cli, token, args.guild, output_dir / args.guild, after, before)
        exported_files.extend(files)
    elif args.channel:
        f = export_channel(cli, token, args.channel, output_dir / "channels", after, before)
        if f:
            exported_files.append(f)
    else:
        # Use config
        for guild_id in discord_cfg.get("guilds", []):
            files = export_guild(
                cli, token, str(guild_id), output_dir / str(guild_id), after, before
            )
            exported_files.extend(files)

        for channel_id in discord_cfg.get("channels", []):
            f = export_channel(
                cli, token, str(channel_id), output_dir / "channels", after, before
            )
            if f:
                exported_files.append(f)

    if not exported_files:
        logger.warning("No files exported. Check your guild/channel IDs and token.")
        sys.exit(1)

    logger.info(f"\nExport complete: {len(exported_files)} file(s) ready to import.")
    for f in exported_files:
        size_kb = f.stat().st_size // 1024
        logger.info(f"  {f.name} ({size_kb} KB)")

    logger.info(
        f"\nNext step: python importer.py --input {output_dir}"
    )


if __name__ == "__main__":
    main()
