"""'/context' subcommands: display, clear, clean."""

import json
import re

from rich.panel import Panel
from rich.markup import escape as _esc

from qwen3_code.theme import console, SAKURA, SAKURA_DEEP, SAKURA_DARK, SAKURA_MUTED

# Estimated context window in tokens (rough: 1 token ≈ 4 chars).
# Matches qwen3's default 128 k context.
_CTX_LIMIT_TOKENS: int = 128_000


# ---------------------------------------------------------------------------
# Usage bar
# ---------------------------------------------------------------------------

def ctx_usage_bar(
    messages: list[dict],
    limit: int = _CTX_LIMIT_TOKENS,
    bar_width: int = 38,
) -> str:
    """Return a Rich-markup string with a coloured usage bar.

    Example:  [green][=========------------------------------][/green]  [green]~12.3k/128k tokens[/green]
    """
    total_chars = sum(len(m.get("content") or "") for m in messages)
    est_tokens  = max(0, total_chars // 4)
    pct         = min(1.0, est_tokens / limit)
    filled      = int(pct * bar_width)
    empty       = bar_width - filled

    bar = "=" * filled + "-" * empty

    if pct < 0.60:   colour = "green"
    elif pct < 0.85: colour = "yellow"
    else:            colour = "red"

    used_str  = f"{est_tokens / 1000:.1f}k"
    limit_str = f"{limit // 1000}k"
    return (
        f"[{colour}][[/{colour}][{colour}]{bar}[/{colour}][{colour}]][/{colour}]"
        f"  [{colour}]~{used_str}/{limit_str} tokens[/{colour}]"
    )


# ---------------------------------------------------------------------------
# /context display
# ---------------------------------------------------------------------------

def ctx_display(messages: list[dict]) -> None:
    non_system = [(i, m) for i, m in enumerate(messages) if m["role"] != "system"]
    total_chars = sum(len(m.get("content") or "") for m in messages)
    est_tokens  = max(0, total_chars // 4)

    lines: list[str] = [ctx_usage_bar(messages)]
    lines.append("")

    if not non_system:
        lines.append("[dim]No messages in context.[/dim]")
    else:
        for i, m in non_system:
            role    = m["role"]
            content = m.get("content") or ""
            chars   = len(content)
            toks    = chars // 4
            preview = content.replace("\n", " ")[:90]
            if len(content) > 90:
                preview += "\u2026"

            role_colour = "cyan" if role == "user" else SAKURA
            lines.append(
                f"  [{role_colour}][{i:>3}] {role:<9}[/{role_colour}]"
                f"  [dim]{_esc(preview)}[/dim]"
                f"  [dim]({toks}t)[/dim]"
            )

    lines.append("")
    lines.append(f"[dim]Total: ~{est_tokens / 1000:.1f}k tokens across {len(non_system)} message(s)[/dim]")

    console.print(Panel("\n".join(lines), title="Context", border_style=SAKURA_DEEP))


# ---------------------------------------------------------------------------
# /context clear
# ---------------------------------------------------------------------------

def ctx_clear(messages: list[dict]) -> None:
    before = sum(1 for m in messages if m["role"] != "system")
    messages[:] = [m for m in messages if m["role"] == "system"]
    console.print(f"[info]Cleared {before} message(s). System prompt preserved.[/info]")


# ---------------------------------------------------------------------------
# /context clean
# ---------------------------------------------------------------------------

def ctx_clean(messages: list[dict], state: dict) -> None:
    """Use the AI to identify and remove low-value messages."""
    non_system = [(i, m) for i, m in enumerate(messages) if m["role"] != "system"]

    if len(non_system) < 4:
        console.print("[info]Not enough messages to clean (need at least 4).[/info]")
        return

    # Show current state first
    ctx_display(messages)

    # Build numbered summary for the AI
    summary: list[str] = []
    for i, m in non_system:
        chars   = len(m.get("content") or "")
        preview = (m.get("content") or "").replace("\n", " ")[:150]
        summary.append(f"[{i}] {m['role']} (~{chars // 4}t): {preview}")

    prompt = (
        "You are a context manager for an AI coding assistant. "
        "Below is a numbered list of messages in the conversation history. "
        "Identify which message indices are SAFE TO REMOVE because they are:\n"
        "  - Old file contents that have since been rewritten\n"
        "  - Intermediate planning steps that were already executed\n"
        "  - Redundant or repeated information\n"
        "  - Command outputs that have already been acted on\n"
        "  - Low-value filler messages\n\n"
        "PRESERVE: important user requests, key decisions, recent messages (last 6), "
        "and any context still relevant to the current task.\n\n"
        "Reply with ONLY a JSON array of integer indices to remove, e.g. [2, 5, 8]\n"
        "If nothing should be removed, reply with []\n\n"
        "Messages:\n" + "\n".join(summary)
    )

    import ollama
    from qwen3_code.settings import _model

    console.print("[dim]Asking AI to analyse context\u2026[/dim]")
    try:
        resp = ollama.chat(
            model=_model(),
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        raw = resp["message"]["content"].strip()
    except Exception as exc:
        console.print(f"[error]Model error: {exc}[/error]")
        return

    # Extract JSON array (may be embedded in prose)
    mat = re.search(r"\[[\d,\s]*\]", raw)
    if not mat:
        console.print(f"[error]Could not parse AI response: {_esc(raw[:200])}[/error]")
        return

    try:
        indices: set[int] = set(json.loads(mat.group()))
    except Exception:
        console.print("[error]Invalid JSON from AI.[/error]")
        return

    # Guard: only allow non-system indices
    valid = {i for i, _ in non_system}
    # Never remove the last 6 non-system messages
    last_six = {i for i, _ in non_system[-6:]}
    indices  = indices & valid - last_six

    if not indices:
        console.print("[info]AI found nothing to remove \u2014 context looks clean.[/info]")
        return

    # Show preview
    preview_lines = ["[bold]AI suggests removing:[/bold]"]
    saved_tokens  = 0
    for idx in sorted(indices):
        m_obj   = messages[idx]
        chars   = len(m_obj.get("content") or "")
        toks    = chars // 4
        saved_tokens += toks
        prev    = (m_obj.get("content") or "").replace("\n", " ")[:80]
        preview_lines.append(
            f"  [dim][{idx}] {m_obj['role']} (~{toks}t): {_esc(prev)}\u2026[/dim]"
        )
    preview_lines.append(f"\n  [green]Will free ~{saved_tokens / 1000:.1f}k tokens[/green]")
    console.print(Panel("\n".join(preview_lines), title="Context clean", border_style=SAKURA_DARK))

    try:
        answer = input("Remove these messages? [y/N] ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        console.print("[info]Cancelled.[/info]")
        return
    if answer not in ("y", "yes"):
        console.print("[info]Cancelled.[/info]")
        return

    messages[:] = [m for i, m in enumerate(messages) if i not in indices]

    from qwen3_code.session import save_session
    save_session(state["cwd"], messages)
    console.print(f"[info]Removed {len(indices)} message(s), freed ~{saved_tokens / 1000:.1f}k tokens.[/info]")
    ctx_display(messages)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def handle_context(arg: str, messages: list[dict], state: dict) -> None:
    sub = arg.strip().lower()
    if sub in ("", "display"):
        ctx_display(messages)
    elif sub == "clear":
        ctx_clear(messages)
    elif sub == "clean":
        ctx_clean(messages, state)
    else:
        console.print("[error]Usage: /context [display|clear|clean][/error]")
