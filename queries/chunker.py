"""
Smart Message Chunker - Layer 2

Splits large message sets into chunks that fit within an LLM's context window,
and formats message dicts into readable text for LLM prompts.

The two core problems this solves:

1. SPLITTING: Mistral 7B has a ~32k token context window. A server with
   months of activity can have hundreds of thousands of messages. We must
   break those into batches that fit, then run the LLM over each batch
   and optionally merge results.

2. FORMATTING: Raw database rows (dicts with timestamps, IDs, etc.) need to
   become clean, readable conversation text before being inserted into a prompt.

Usage:
    from chunker import SmartChunker

    messages = qb.channel_messages(server_id, channel_id, time_range_days=30)

    # Chunk and format in one step
    formatted_chunks = SmartChunker.chunk_and_format(messages)
    for chunk_text in formatted_chunks:
        result = llm.channel_summary_faq(chunk_text, channel_name)

    # Or do it manually
    chunks = SmartChunker.chunk_messages(messages, max_tokens=8000)
    for chunk in chunks:
        text = SmartChunker.format_for_llm(chunk)
        # ... pass text to LLM
"""

from datetime import datetime
from typing import Optional


class SmartChunker:
    """
    Utility class for preparing message data for LLM prompts.

    All methods are class methods / static methods — no state needed.
    Instantiate if you want custom defaults, or call the class directly.
    """

    # Mistral 7B: ~32k token context. We cap message content at 8k to
    # leave headroom for the system prompt, task instructions, and output.
    DEFAULT_MAX_TOKENS: int = 8_000

    # Conservative token estimate: 1 word ≈ 1.3 tokens.
    # Accounts for punctuation, whitespace tokens, and subword splitting
    # of technical terms like "malloc", "segfault", "async/await".
    TOKENS_PER_WORD: float = 1.3

    @classmethod
    def estimate_tokens(cls, text: str) -> int:
        """
        Estimate the token count of a string.

        Uses word count × TOKENS_PER_WORD as a fast approximation.
        Accurate enough for chunking without a full tokenizer library.

        Args:
            text: Any string.

        Returns:
            Estimated token count (always >= 0).
        """
        if not text:
            return 0
        return max(1, int(len(text.split()) * cls.TOKENS_PER_WORD))

    @classmethod
    def chunk_messages(
        cls,
        messages: list[dict],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> list[list[dict]]:
        """
        Split a list of message dicts into token-budget chunks.

        Messages are kept whole — we never split a single message across
        chunks. If a single message exceeds max_tokens on its own, it gets
        its own chunk rather than being silently dropped.

        Args:
            messages:   List of message dicts (from QueryBuilder methods).
            max_tokens: Token budget per chunk. Default is 8k.

        Returns:
            List of chunks. Each chunk is a list[dict] in the original order.
            Returns [[]] (one empty chunk) if messages is empty.
        """
        if not messages:
            return []

        chunks: list[list[dict]] = []
        current_chunk: list[dict] = []
        current_tokens: int = 0

        for msg in messages:
            content = msg.get("content") or ""
            msg_tokens = cls.estimate_tokens(content)

            # If adding this message would overflow AND we already have
            # content in the chunk, flush and start fresh.
            if current_tokens + msg_tokens > max_tokens and current_chunk:
                chunks.append(current_chunk)
                current_chunk = [msg]
                current_tokens = msg_tokens
            else:
                current_chunk.append(msg)
                current_tokens += msg_tokens

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    @staticmethod
    def format_for_llm(
        messages: list[dict],
        include_timestamps: bool = True,
    ) -> str:
        """
        Convert a list of message dicts into clean text for LLM prompts.

        Output example (with timestamps):
            [2024-01-15 14:32] alice: Hello, how do I use malloc?
            [2024-01-15 14:33] bob: malloc(size) allocates size bytes on the heap.

        Output example (without timestamps):
            alice: Hello, how do I use malloc?
            bob: malloc(size) allocates size bytes on the heap.

        Messages with empty content (attachment-only posts) are skipped.

        Args:
            messages:           List of message dicts.
            include_timestamps: Prepend [YYYY-MM-DD HH:MM] to each line.

        Returns:
            Multi-line string, one message per line. Empty string if no
            messages have non-empty content.
        """
        lines: list[str] = []

        for msg in messages:
            # Normalise author field — QueryBuilder returns different key names
            # depending on which method produced the rows.
            author = (
                msg.get("author")
                or msg.get("current_username")
                or "unknown"
            )
            content = (msg.get("content") or "").strip()

            if not content:
                continue  # Skip attachment-only or empty messages

            if include_timestamps and msg.get("created_at"):
                ts = msg["created_at"]
                ts_str = (
                    ts.strftime("%Y-%m-%d %H:%M")
                    if isinstance(ts, datetime)
                    else str(ts)
                )
                lines.append(f"[{ts_str}] {author}: {content}")
            else:
                lines.append(f"{author}: {content}")

        return "\n".join(lines)

    @classmethod
    def chunk_and_format(
        cls,
        messages: list[dict],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        include_timestamps: bool = True,
    ) -> list[str]:
        """
        Chunk messages and format each chunk as a string in one step.

        This is the most common entry point for Layer 3:

            chunks = SmartChunker.chunk_and_format(messages)
            for chunk_text in chunks:
                result = llm.channel_summary_faq(chunk_text, channel_name)

        Args:
            messages:           List of message dicts from QueryBuilder.
            max_tokens:         Token budget per chunk.
            include_timestamps: Whether to include [YYYY-MM-DD HH:MM] prefix.

        Returns:
            List of formatted strings, one per chunk.
        """
        chunks = cls.chunk_messages(messages, max_tokens)
        return [
            cls.format_for_llm(chunk, include_timestamps)
            for chunk in chunks
        ]

    @classmethod
    def stats(cls, messages: list[dict]) -> dict:
        """
        Return quick statistics about a message set without chunking it.

        Useful for deciding whether to chunk at all, or for logging.

        Returns:
            Dict with: total_messages, total_tokens, estimated_chunks,
            avg_tokens_per_message
        """
        if not messages:
            return {
                "total_messages": 0,
                "total_tokens": 0,
                "estimated_chunks": 0,
                "avg_tokens_per_message": 0,
            }

        total_tokens = sum(
            cls.estimate_tokens(msg.get("content") or "")
            for msg in messages
        )
        estimated_chunks = max(
            1, -(-total_tokens // cls.DEFAULT_MAX_TOKENS)  # ceiling division
        )
        return {
            "total_messages": len(messages),
            "total_tokens": total_tokens,
            "estimated_chunks": estimated_chunks,
            "avg_tokens_per_message": round(total_tokens / len(messages), 1),
        }
