"""'/context' subcommands: display, clear, clean."""

import json
import re

from rich.panel import Panel
from rich.markup import escape as _esc

from qwen3_code.theme import console, SAKURA, SAKURA_DEEP, SAKURA_DARK, SAKURA_MUTED
from qwen3_code.tokens import count_tokens, count_messages, format_tokens, tiktoken_available


def _ctx_limit() -> int:
    from qwen3_code.settings import _context_window
    return _context_window()


# ---------------------------------------------------------------------------
# Compact label helpers  (1..10, +1..+10, ++1..)
# ---------------------------------------------------------------------------

def _item_label(zero_idx: int) -> str:
    """Convert 0-based list position to compact display label."""
    if zero_idx < 10:
        return str(zero_idx + 1)
    elif zero_idx < 20:
        return f"+{zero_idx - 9}"
    else:
        return f"++{zero_idx - 19}"


def _parse_label(label: str, total: int) -> int | None:
    """Parse a compact label back to a 0-based index.  Returns None if invalid."""
    label = label.strip()
    try:
        if label.startswith("++"):
            zero_idx = 19 + int(label[2:]) - 1
        elif label.startswith("+"):
            zero_idx = 9 + int(label[1:]) - 1
        else:
            zero_idx = int(label) - 1
        return zero_idx if 0 <= zero_idx < total else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Usage bar
# ---------------------------------------------------------------------------

def ctx_usage_bar(
    messages: list[dict],
    pending: list[str] | None = None,
    bar_width: int = 38,
) -> str:
    limit  = _ctx_limit()
    used   = count_messages(messages)
    if pending:
        used += sum(count_tokens(p) for p in pending)

    pct    = min(1.0, used / limit)
    filled = int(pct * bar_width)
    empty  = bar_width - filled
    bar    = "=" * filled + "-" * empty

    if pct < 0.60:   colour = "green"
    elif pct < 0.85: colour = "yellow"
    else:            colour = "red"

    used_str  = format_tokens(used)
    limit_str = format_tokens(limit, exact=True)
    return (
        f"[{colour}][[/{colour}][{colour}]{bar}[/{colour}][{colour}]][/{colour}]"
        f"  [{colour}]{used_str}/{limit_str} tokens[/{colour}]"
    )


# ---------------------------------------------------------------------------
# /context display
# ---------------------------------------------------------------------------

def ctx_display(messages: list[dict], state: dict | None = None) -> None:
    pending    = (state or {}).get("pending_context", [])
    non_system = [(i, m) for i, m in enumerate(messages) if m["role"] != "system"]
    used       = count_messages(messages)
    used      += sum(count_tokens(p) for p in pending)
    limit      = _ctx_limit()
    exact      = tiktoken_available()

    lines: list[str] = [ctx_usage_bar(messages, pending)]
    engine_note = "tiktoken cl100k_base" if exact else "estimated (install tiktoken for accuracy)"
    lines.append(f"[dim]  Token engine : {engine_note}[/dim]")
    lines.append(f"[dim]  Change limit : /settings context_window <tokens>  (current: {limit:,})[/dim]")

    if pending:
        lines.append("")
        lines.append(f"[bold yellow]\u23f3 Staged ({len(pending)} item(s)) \u2014 will be sent with your next message:[/bold yellow]")
        for idx, p in enumerate(pending):
            preview = p.replace("\n", " ")[:100]
            if len(p) > 100:
                preview += "\u2026"
            toks = count_tokens(p)
            lines.append(
                f"  [yellow][staged {idx}][/yellow]  [dim]{_esc(preview)}[/dim]"
                f"  [dim]({format_tokens(toks)})[/dim]"
            )

    lines.append("")
    if not non_system:
        lines.append("[dim]No sent messages in context.[/dim]")
    else:
        lines.append("[dim]Sent messages:[/dim]")
        for i, m in non_system:
            role    = m["role"]
            content = m.get("content") or ""
            toks    = count_tokens(content) + 4
            preview = content.replace("\n", " ")[:90]
            if len(content) > 90:
                preview += "\u2026"
            role_colour = "cyan" if role == "user" else SAKURA
            lines.append(
                f"  [{role_colour}][{i:>3}] {role:<9}[/{role_colour}]"
                f"  [dim]{_esc(preview)}[/dim]"
                f"  [dim]({format_tokens(toks)})[/dim]"
            )

    lines.append("")
    lines.append(
        f"[dim]Total: {format_tokens(used)} tokens"
        f" ({len(non_system)} sent, {len(pending)} staged)[/dim]"
    )
    console.print(Panel("\n".join(lines), title="Context", border_style=SAKURA_DEEP))


# ---------------------------------------------------------------------------
# /context clear
# ---------------------------------------------------------------------------

def ctx_clear(messages: list[dict], state: dict | None = None) -> None:
    before = sum(1 for m in messages if m["role"] != "system")
    messages[:] = [m for m in messages if m["role"] == "system"]
    staged = 0
    if state and state.get("pending_context"):
        staged = len(state["pending_context"])
        state["pending_context"] = []
    console.print(f"[info]Cleared {before} sent message(s) and {staged} staged item(s). System prompt preserved.[/info]")


# ---------------------------------------------------------------------------
# Custom selection helpers
# ---------------------------------------------------------------------------

def _show_removable_list(removable: list[tuple[int, dict]]) -> None:
    """Print the removable messages with compact labels."""
    n = len(removable)
    legend_parts: list[str] = ["1\u20139"]
    if n > 10: legend_parts.append("+1\u2026+10  (items 11\u201320)")
    if n > 20: legend_parts.append("++1\u2026  (items 21+)")
    legend = "  ".join(legend_parts)

    lines = [f"[dim]Label scheme: {legend}[/dim]", ""]
    for pos, (msg_idx, m) in enumerate(removable):
        label  = _item_label(pos)
        role   = m["role"]
        toks   = count_tokens(m.get("content") or "") + 4
        preview = (m.get("content") or "").replace("\n", " ")[:80]
        if len(m.get("content") or "") > 80:
            preview += "\u2026"
        role_colour = "cyan" if role == "user" else SAKURA
        lines.append(
            f"  [bold]{label:<5}[/bold]"
            f"  [{role_colour}]{role:<9}[/{role_colour}]"
            f"  [dim]{_esc(preview)}[/dim]"
            f"  [dim]({format_tokens(toks)})[/dim]"
        )
    console.print(Panel("\n".join(lines), title="Removable messages", border_style=SAKURA_MUTED))


def _collect_custom_indices(removable: list[tuple[int, dict]]) -> set[int] | None:
    """Ask the user to type labels interactively.  Returns a set of *message* indices,
    or None if cancelled."""
    n = len(removable)
    console.print(
        "[info]Enter labels to remove (one per line). "
        "Blank line or [bold]done[/bold] when finished. [bold]cancel[/bold] to abort.[/info]"
    )

    chosen_positions: list[int] = []
    while True:
        try:
            raw = input("  label> ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("[info]Cancelled.[/info]")
            return None
        if raw in ("", "done"):
            break
        if raw == "cancel":
            console.print("[info]Cancelled.[/info]")
            return None
        pos = _parse_label(raw, n)
        if pos is None:
            console.print(f"[error]  '{raw}' is not a valid label (1\u2013{_item_label(n - 1)})[/error]")
            continue
        if pos not in chosen_positions:
            chosen_positions.append(pos)
            label = _item_label(pos)
            m     = removable[pos][1]
            prev  = (m.get("content") or "").replace("\n", " ")[:60]
            console.print(f"  [green]\u2713 {label}[/green]  [dim]{_esc(prev)}[/dim]")

    if not chosen_positions:
        console.print("[info]No labels entered \u2014 nothing removed.[/info]")
        return None

    return {removable[pos][0] for pos in chosen_positions}


# ---------------------------------------------------------------------------
# /context clean
# ---------------------------------------------------------------------------

def ctx_clean(messages: list[dict], state: dict) -> None:
    non_system = [(i, m) for i, m in enumerate(messages) if m["role"] != "system"]

    if len(non_system) < 4:
        console.print("[info]Not enough messages to clean (need at least 4).[/info]")
        return

    ctx_display(messages, state)

    # --- Ask AI for suggestions ---
    summary: list[str] = []
    for i, m in non_system:
        toks    = count_tokens(m.get("content") or "") + 4
        preview = (m.get("content") or "").replace("\n", " ")[:150]
        summary.append(f"[{i}] {m['role']} ({format_tokens(toks)}): {preview}")

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

    mat = re.search(r"\[[\d,\s]*\]", raw)
    if not mat:
        console.print(f"[error]Could not parse AI response: {_esc(raw[:200])}[/error]")
        return

    try:
        ai_indices: set[int] = set(json.loads(mat.group()))
    except Exception:
        console.print("[error]Invalid JSON from AI.[/error]")
        return

    valid    = {i for i, _ in non_system}
    last_six = {i for i, _ in non_system[-6:]}
    ai_indices = ai_indices & valid - last_six

    # --- Show AI suggestion panel ---
    if not ai_indices:
        console.print("[info]AI found nothing to remove \u2014 context looks clean.[/info]")
        # Still offer custom mode so user can manually remove things
        try:
            answer = input("Open custom selection anyway? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return
        if answer not in ("y", "yes"):
            return
        answer = "c"
    else:
        ai_preview_lines = ["[bold]AI suggests removing:[/bold]"]
        saved_ai = 0
        # Build ordered list matching removable ordering (by position in non_system)
        for idx in sorted(ai_indices):
            m_obj        = messages[idx]
            toks         = count_tokens(m_obj.get("content") or "") + 4
            saved_ai    += toks
            prev         = (m_obj.get("content") or "").replace("\n", " ")[:80]
            ai_preview_lines.append(
                f"  [dim][msg {idx}] {m_obj['role']} ({format_tokens(toks)}): {_esc(prev)}\u2026[/dim]"
            )
        ai_preview_lines.append(
            f"\n  [green]Will free {format_tokens(saved_ai)} tokens[/green]"
        )
        console.print(Panel(
            "\n".join(ai_preview_lines),
            title="Context clean \u2014 AI suggestion",
            border_style=SAKURA_DARK,
        ))

        try:
            answer = input(
                "[y] Accept  [n] Cancel  [c] Custom selection  > "
            ).strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print("[info]Cancelled.[/info]")
            return

    # --- Branch on answer ---
    if answer in ("n", ""):
        console.print("[info]Cancelled.[/info]")
        return

    if answer in ("y", "yes") and ai_indices:
        indices_to_remove = ai_indices

    elif answer in ("c", "custom"):
        # Build removable list: all non-system messages except last 6
        removable: list[tuple[int, dict]] = [
            (i, m) for i, m in non_system if i not in last_six
        ]
        if not removable:
            console.print("[info]No messages available to remove (last 6 are protected).[/info]")
            return
        _show_removable_list(removable)
        result = _collect_custom_indices(removable)
        if result is None:
            return
        indices_to_remove = result

    else:
        console.print("[error]Unrecognised choice.[/error]")
        return

    if not indices_to_remove:
        console.print("[info]Nothing to remove.[/info]")
        return

    # --- Final summary + confirm ---
    final_lines = ["[bold]Will remove:[/bold]"]
    saved_total = 0
    for idx in sorted(indices_to_remove):
        m_obj        = messages[idx]
        toks         = count_tokens(m_obj.get("content") or "") + 4
        saved_total += toks
        prev         = (m_obj.get("content") or "").replace("\n", " ")[:80]
        final_lines.append(
            f"  [dim][msg {idx}] {m_obj['role']} ({format_tokens(toks)}): {_esc(prev)}\u2026[/dim]"
        )
    final_lines.append(f"\n  [green]Will free {format_tokens(saved_total)} tokens[/green]")
    console.print(Panel("\n".join(final_lines), title="Confirm removal", border_style=SAKURA_DARK))

    try:
        confirm = input("Confirm? [y/N] ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        console.print("[info]Cancelled.[/info]")
        return
    if confirm not in ("y", "yes"):
        console.print("[info]Cancelled.[/info]")
        return

    messages[:] = [m for i, m in enumerate(messages) if i not in indices_to_remove]

    from qwen3_code.session import save_session
    save_session(state["cwd"], messages)
    console.print(
        f"[info]Removed {len(indices_to_remove)} message(s), freed {format_tokens(saved_total)} tokens.[/info]"
    )
    ctx_display(messages, state)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def handle_context(arg: str, messages: list[dict], state: dict) -> None:
    sub = arg.strip().lower()
    if sub in ("", "display"):
        ctx_display(messages, state)
    elif sub == "clear":
        ctx_clear(messages, state)
    elif sub == "clean":
        ctx_clean(messages, state)
    else:
        console.print("[error]Usage: /context [display|clear|clean][/error]")
