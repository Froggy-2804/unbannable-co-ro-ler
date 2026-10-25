"""Token-budget guarding using strictly o200k_harmony tokenizer.

Provides conservative token estimation and budget checks reserving output tokens,
message framing, and reasoning budget without claiming exact Ollama template token counts.
"""

from __future__ import annotations

from typing import Any

REQUIRED_ENCODING = "o200k_harmony"

DEFAULT_FRAMING_RESERVE = 512
DEFAULT_THINKING_RESERVE = 1024


class TokenizerPrerequisiteError(RuntimeError):
    """Raised when tiktoken is unavailable or the required o200k_harmony encoding cannot be loaded."""


_cached_encoding: Any = None


def get_encoding() -> Any:
    """Retrieve strictly o200k_harmony tiktoken encoding with clear prerequisite failure if unavailable."""
    global _cached_encoding
    if _cached_encoding is not None:
        return _cached_encoding

    try:
        import tiktoken
    except ImportError as exc:
        raise TokenizerPrerequisiteError(
            "Token budget guard requires 'tiktoken>=0.12' with 'o200k_harmony' encoding. "
            "Please install analysis dependencies: pip install '.[analysis]'"
        ) from exc

    try:
        _cached_encoding = tiktoken.get_encoding(REQUIRED_ENCODING)
        return _cached_encoding
    except Exception as exc:
        raise TokenizerPrerequisiteError(
            f"Required encoding '{REQUIRED_ENCODING}' could not be loaded from tiktoken: {exc}. "
            "Please upgrade tiktoken to a compatible version: pip install --upgrade 'tiktoken>=0.12'"
        ) from exc


def estimate_tokens(text: str, encoding: Any = None) -> int:
    """Estimate token count for text using strictly o200k_harmony encoding.

    Note: This provides a heuristic conservative budget bound and does not claim
    exact Ollama backend template token counts. Encode errors are never silently bypassed.
    """
    if not text:
        return 0
    enc = encoding if encoding is not None else get_encoding()
    try:
        return len(enc.encode(text, disallowed_special=()))
    except Exception as exc:
        raise TokenizerPrerequisiteError(
            f"Failed to encode text with {REQUIRED_ENCODING}: {exc}"
        ) from exc


def estimate_chat_tokens(
    messages: list[dict[str, str]],
    encoding: Any = None,
) -> int:
    """Estimate total input tokens for chat messages including message framing overhead."""
    enc = encoding if encoding is not None else get_encoding()
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        # Reserve ~8 tokens per message for role tags and framing delimiter tokens
        total += estimate_tokens(content, encoding=enc) + 8
    # Reserve ~16 tokens for overall chat conversation envelope
    return total + 16


def check_context_budget(
    messages: list[dict[str, str]],
    num_ctx: int,
    num_predict: int,
    think: str | None = None,
    framing_reserve: int = DEFAULT_FRAMING_RESERVE,
    thinking_reserve: int = DEFAULT_THINKING_RESERVE,
    encoding: Any = None,
) -> tuple[bool, int, int]:
    """Check if chat messages fit within the available context window budget.

    Reserves:
      - num_predict: reserved output tokens
      - framing_reserve: safety margin for system instructions, schema, and chat framing (estimate, not exact-token promise)
      - thinking_reserve: extra reasoning token budget when think is enabled

    Returns:
        (fits, estimated_tokens, max_allowed_input_tokens)
    """
    reserved = num_predict + framing_reserve
    if think is not None and str(think).strip().lower() not in ("", "none", "off", "0", "false"):
        reserved += thinking_reserve

    max_input_tokens = max(0, num_ctx - reserved)
    estimated_tokens = estimate_chat_tokens(messages, encoding=encoding)
    fits = (estimated_tokens <= max_input_tokens) and (max_input_tokens > 0)
    return fits, estimated_tokens, max_input_tokens
