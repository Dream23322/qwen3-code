"""Token counting: tiktoken (cl100k_base) when available, chars/4 fallback.

Qwen3 uses a BPE tokenizer closely related to cl100k_base (the GPT-4 encoding).
Using tiktoken gives counts that are accurate to within a few percent, which is
much better than the naive chars/4 heuristic.

Install:  pip install tiktoken
"""

from __future__ import annotations

_enc = None
_TIKTOKEN_AVAILABLE: bool = False
_TIKTOKEN_CHECKED:   bool = False


def _get_encoder():
    """Lazy-initialise and cache the tiktoken encoder."""
    global _enc, _TIKTOKEN_AVAILABLE, _TIKTOKEN_CHECKED
    if _TIKTOKEN_CHECKED:
        return _enc
    _TIKTOKEN_CHECKED = True
    try:
        import tiktoken
        # cl100k_base is the GPT-4 encoding and is very close to Qwen3's BPE vocab.
        _enc = tiktoken.get_encoding("cl100k_base")
        _TIKTOKEN_AVAILABLE = True
    except Exception:
        _enc = None
        _TIKTOKEN_AVAILABLE = False
    return _enc


def tiktoken_available() -> bool:
    """Return True if tiktoken was imported and the encoder loaded successfully."""
    _get_encoder()
    return _TIKTOKEN_AVAILABLE


def count_tokens(text: str) -> int:
    """Return token count for *text*.

    Uses tiktoken cl100k_base when available; otherwise estimates as len/4.
    """
    if not text:
        return 0
    enc = _get_encoder()
    if enc is not None:
        try:
            # disallowed_special=() lets us encode any string without errors
            return len(enc.encode(text, disallowed_special=()))
        except Exception:
            pass
    return max(1, len(text) // 4)


def count_messages(messages: list[dict]) -> int:
    """Count total tokens for a list of chat messages.

    Each message carries ~4 tokens of overhead for the role/formatting wrapper,
    plus a 3-token reply primer at the end.  This matches OpenAI's counting
    convention and is a good approximation for Qwen3 chat templates.
    """
    total = 3  # reply primer
    for m in messages:
        total += 4  # per-message overhead (role + delimiters)
        total += count_tokens(m.get("content") or "")
    return total


def format_tokens(n: int, *, exact: bool | None = None) -> str:
    """Format a token count for display, e.g. '12.3k' or '~12.3k'.

    If *exact* is None, the prefix is decided by whether tiktoken is available.
    """
    if exact is None:
        exact = tiktoken_available()
    prefix = "" if exact else "~"
    if n >= 1000:
        return f"{prefix}{n / 1000:.1f}k"
    return f"{prefix}{n}"
