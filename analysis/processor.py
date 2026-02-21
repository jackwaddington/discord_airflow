"""
LLM Processor - Layer 3

Runs Discord data analysis through a local Ollama instance via REST API.

Usage:
    from processor import DiscordLLMProcessor

    llm = DiscordLLMProcessor()           # reads OLLAMA_HOST / OLLAMA_MODEL from .env
    # or
    llm = DiscordLLMProcessor(host="http://gpu-machine:11434", model="mistral")

    # Analyse a user's history
    story = llm.user_story(formatted_messages, username="alice")

    # Summarise a channel
    faq = llm.channel_summary_faq(formatted_messages, channel="general")

    # Weekly digest
    digest = llm.weekly_digest(server_summary_dict)

    llm.close()

Configuration via .env:
    OLLAMA_HOST   - URL of the Ollama instance (default: http://localhost:11434)
    OLLAMA_MODEL  - Model to use (default: mistral)

If Ollama is on a different machine, SSH-tunnel it first:
    ssh -L 11434:localhost:11434 user@gpu-machine
Then set OLLAMA_HOST=http://localhost:11434 as normal.

All analysis methods accept pre-formatted text strings from SmartChunker,
not raw database rows. See layer2-query/chunker.py for formatting.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Prompt templates live next to this file in the prompts/ subdirectory
PROMPTS_DIR = Path(__file__).parent / "prompts"


# ─── Ollama REST Client ───────────────────────────────────────────────────────


class OllamaClient:
    """
    Thin wrapper around the Ollama REST API.

    Uses requests.Session for connection pooling — one client instance
    can handle multiple generate() calls efficiently.

    Raises clear, actionable errors rather than raw HTTP exceptions so
    callers know exactly what went wrong and how to fix it.
    """

    def __init__(self, host: str, model: str) -> None:
        """
        Args:
            host:  Base URL of the Ollama server, e.g. "http://localhost:11434".
            model: Model name as known to Ollama, e.g. "mistral" or "mistral:7b".
        """
        self.host = host.rstrip("/")
        self.model = model
        self.session = requests.Session()

    def generate(self, prompt: str, timeout: int = 180) -> str:
        """
        Send a prompt to Ollama and return the generated text.

        Args:
            prompt:  The full prompt string to send.
            timeout: Seconds to wait for a response. Larger models or long
                     prompts may need 3-5 minutes on slower hardware.

        Returns:
            The model's response as a plain string.

        Raises:
            TimeoutError:    Model did not respond in time.
            ConnectionError: Could not reach Ollama.
            RuntimeError:    Ollama returned an API error.
        """
        url = f"{self.host}/api/generate"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }

        try:
            resp = self.session.post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
            return resp.json()["response"]

        except requests.Timeout:
            raise TimeoutError(
                f"Ollama did not respond within {timeout}s. "
                "Try a longer timeout or a smaller model."
            )
        except requests.ConnectionError:
            raise ConnectionError(
                f"Could not connect to Ollama at {self.host}. "
                "Is it running? Start it with: ollama serve"
            )
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"Ollama API error ({exc.response.status_code}): "
                f"{exc.response.text}"
            )

    def is_available(self) -> bool:
        """
        Return True if Ollama is reachable and the configured model is loaded.

        Safe to call as a health check before running a long analysis.
        """
        try:
            resp = self.session.get(f"{self.host}/api/tags", timeout=5)
            resp.raise_for_status()
            models = resp.json().get("models", [])
            # Ollama tags models as "mistral:latest" — match on base name only
            loaded = {m.get("name", "").split(":")[0] for m in models}
            return self.model.split(":")[0] in loaded
        except Exception:
            return False

    def close(self) -> None:
        """Close the underlying requests session."""
        self.session.close()


# ─── Template Filling ─────────────────────────────────────────────────────────


def _fill_template(template: str, **kwargs: str) -> str:
    """
    Fill named placeholders in a prompt template string.

    Uses plain str.replace() rather than str.format() so that curly braces
    in the data (C structs, Python dicts, code snippets posted to Discord)
    are never misinterpreted as format placeholders.

    Example:
        template = "User {username} wrote:\\n{messages}"
        _fill_template(template, username="alice", messages="struct s { int x; };")
        # Safe — the braces in the message content are not interpreted.

    Args:
        template: Prompt template with {placeholder} markers.
        **kwargs: Values to substitute, in any order.

    Returns:
        Filled prompt string ready to send to the LLM.
    """
    result = template
    for key, value in kwargs.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result


# ─── LLM Processor ────────────────────────────────────────────────────────────


class DiscordLLMProcessor:
    """
    Runs analysis on Discord data using a local Ollama LLM.

    Each public method corresponds to one analysis type:
      - user_story          → narrative about a user's programming journey
      - channel_summary_faq → FAQ extracted from channel discussions
      - factcheck           → technical claim verification
      - weekly_digest       → server activity summary

    All methods accept pre-formatted text from SmartChunker.format_for_llm().
    For large message sets, chunk first and call the method once per chunk.

    The optional `client` parameter lets you inject a mock OllamaClient
    for testing without needing a live Ollama instance.
    """

    def __init__(
        self,
        host: Optional[str] = None,
        model: Optional[str] = None,
        client: Optional[OllamaClient] = None,
    ) -> None:
        """
        Args:
            host:   Ollama base URL. Falls back to OLLAMA_HOST env var,
                    then "http://localhost:11434".
            model:  Model name. Falls back to OLLAMA_MODEL env var,
                    then "mistral".
            client: Optional pre-built OllamaClient (useful for testing).
        """
        if client is not None:
            self.client = client
        else:
            _host = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")
            _model = model or os.getenv("OLLAMA_MODEL", "mistral")
            self.client = OllamaClient(host=_host, model=_model)

        # Cache loaded templates in memory — they're small and don't change
        self._prompt_cache: dict[str, str] = {}

    def _load_prompt(self, filename: str) -> str:
        """Load a prompt template from prompts/ (cached after first load)."""
        if filename not in self._prompt_cache:
            path = PROMPTS_DIR / filename
            if not path.exists():
                raise FileNotFoundError(
                    f"Prompt template not found: {path}\n"
                    f"Expected directory: {PROMPTS_DIR}"
                )
            self._prompt_cache[filename] = path.read_text(encoding="utf-8")
        return self._prompt_cache[filename]

    def _run(self, prompt_file: str, **kwargs: str) -> str:
        """Load a template, fill variables, run the LLM, return stripped output."""
        template = self._load_prompt(prompt_file)
        prompt = _fill_template(template, **kwargs)
        logger.debug(f"Running '{prompt_file}': {len(prompt)} chars input")
        response = self.client.generate(prompt)
        logger.debug(f"Response: {len(response)} chars")
        return response.strip()

    # ─── Public Analysis Methods ──────────────────────────────────────────────

    def user_story(self, messages_text: str, username: str) -> str:
        """
        Generate a narrative about a user's programming journey.

        Best results with 30-90 days of messages from a single user.
        For large histories, pass only the most recent chunk.

        Args:
            messages_text: Formatted text from SmartChunker.format_for_llm().
            username:      The user's Discord display name.

        Returns:
            2-3 paragraph narrative capturing the user's voice and growth.
        """
        return self._run(
            "user_story.txt",
            username=username,
            messages=messages_text,
        )

    def channel_summary_faq(self, messages_text: str, channel: str) -> str:
        """
        Extract a FAQ from a channel's recent discussions.

        Works best on help/support/questions channels.

        Args:
            messages_text: Formatted text from SmartChunker.format_for_llm().
            channel:       Channel name without the # prefix.

        Returns:
            Q&A formatted FAQ covering common questions and answers.
        """
        return self._run(
            "channel_summary_faq.txt",
            channel=channel,
            messages=messages_text,
        )

    def factcheck(self, messages_text: str, topic: str) -> str:
        """
        Fact-check technical claims in a set of messages.

        Useful for catching misinformation in help channels (wrong advice
        about memory management, incorrect syntax, etc.).

        Args:
            messages_text: Formatted text from SmartChunker.format_for_llm().
            topic:         The technical topic (e.g. "memory management in C").

        Returns:
            Annotated list of claims with verdicts. Uncertain items are
            labelled "uncertain" rather than fabricated.
        """
        return self._run(
            "factcheck.txt",
            topic=topic,
            messages=messages_text,
        )

    def weekly_digest(self, server_data: "dict | str") -> str:
        """
        Generate a weekly digest for a server.

        Args:
            server_data: Output from QueryBuilder.server_summary_data() (dict),
                         or a pre-formatted string. Dicts are serialized to
                         indented JSON before being inserted into the prompt.

        Returns:
            1-2 page digest suitable for posting to a #weekly-digest channel.
        """
        if isinstance(server_data, dict):
            data_str = json.dumps(server_data, indent=2, default=str)
        else:
            data_str = str(server_data)

        return self._run("weekly_digest.txt", server_data=data_str)

    # ─── Utility ─────────────────────────────────────────────────────────────

    def is_ready(self) -> bool:
        """
        Return True if Ollama is reachable and the model is loaded.

        Preflight check before running long analyses:

            if not llm.is_ready():
                print("Run: ollama serve && ollama pull mistral")
                return
        """
        return self.client.is_available()

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self.client.close()


# ─── Convenience Constructor ──────────────────────────────────────────────────


def from_env() -> DiscordLLMProcessor:
    """
    Build a DiscordLLMProcessor from environment variables.

    Reads OLLAMA_HOST and OLLAMA_MODEL from the environment (or .env file).
    """
    return DiscordLLMProcessor()
