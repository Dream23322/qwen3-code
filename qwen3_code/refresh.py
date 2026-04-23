"""'/refresh' command: reload tracked files and prune stale context."""

import os
import re
from pathlib import Path

from rich.panel import Panel

from qwen3_code.theme import console, SAKURA_DEEP
from qwen3_code.vc import all_tracked_files, _load_vc, vc_commit
from qwen3_code.session import save_session

# ---------------------------------------------------------------------------
# Patterns that match /read-injected context blocks
# ---------------------------------------------------------------------------

_READ_BLOCK_RE = re.compile(
    r"Here is the content of `(?P<path>[^`]+)`:\s*\n\n```[^\n]*\n(?P<body>.*?)\n?```",
    re.DOTALL,
)


def _strip_file_blocks(content: str, gone_set: set[str]) -> str:
    """Remove context blocks for files in *gone_set* from a message string."""

    def _remove_single(m: re.Match) -> str:
        fp = m.group("path")
        if fp in gone_set or str(Path(fp).resolve()) in gone_set:
            return ""
        return m.group(0)

    content = _READ_BLOCK_RE.sub(_remove_single, content)

    _SUB_RE = re.compile(r"### (?P<rel>[^\n]+)\n```[^\n]*\n.*?```", re.DOTALL)

    def _fix_bulk(m: re.Match) -> str:
        full  = m.group(0)
        nl2   = full.find("\n\n")
        if nl2 == -1:
            return full
        intro = full[: nl2 + 2]
        rest  = full[nl2 + 2:]
        kept: list[str] = []
        for sub in _SUB_RE.finditer(rest):
            rel  = sub.group("rel").strip()
            gone = any(g.endswith(os.sep + rel) or g.endswith("/" + rel) or Path(g).name == rel
                       for g in gone_set)
            if not gone:
                kept.append(sub.group(0))
        if not kept:
            return ""
        new_intro = re.sub(r"Here are all \d+ file\(s\)", f"Here are all {len(kept)} file(s)", intro)
        return new_intro + "\n\n".join(kept)

    content = re.sub(
        r"Here are all \d+ file\(s\) from `[^`]+`:\n\n(?:### [^\n]+\n```[^\n]*\n.*?```\n?)+",
        _fix_bulk, content, flags=re.DOTALL,
    )
    return content.strip()


def _file_in_context(fp: str, messages: list[dict], state: dict) -> bool:
    """Return True if *fp* is currently referenced in any message or pending context."""
    needle_abs  = str(Path(fp).resolve())
    needle_name = Path(fp).name
    haystack = (
        [m["content"] for m in messages if m.get("content")]
        + state.get("pending_context", [])
    )
    for text in haystack:
        if fp in text or needle_abs in text or needle_name in text:
            return True
    return False


# ---------------------------------------------------------------------------
# Public handler
# ---------------------------------------------------------------------------

def handle_refresh(messages: list[dict], state: dict) -> None:
    cwd     = state["cwd"]
    tracked = all_tracked_files()
    local   = [fp for fp in tracked if fp.startswith(cwd)]

    if not local:
        console.print("[info]No tracked files under current directory. Use /read to load files.[/info]")
        return

    refreshed: list[tuple[str, str]] = []
    unchanged: list[str]             = []
    gone:      list[str]             = []

    for fp in local:
        if not Path(fp).exists():
            gone.append(fp)
            continue
        try:
            new_content = Path(fp).read_text(encoding="utf-8")
        except Exception:
            gone.append(fp)
            continue

        old = ""
        idx = _load_vc(fp)
        hid = idx.get("head")
        if hid and hid in idx["commits"]:
            try:
                old = Path(idx["commits"][hid]["snapshot"]).read_text(encoding="utf-8")
            except Exception:
                pass

        if new_content != old:
            refreshed.append((fp, new_content))
        else:
            unchanged.append(fp)

    gone_set = set(gone)

    # Only report gone files that are actually still referenced in context.
    # Files already pruned on a previous /refresh won't appear in messages
    # any more, so there's nothing new to report for them.
    gone_reportable = [fp for fp in gone if _file_in_context(fp, messages, state)]

    # ---- Prune messages ---------------------------------------------------
    pruned = 0
    new_msgs: list[dict] = []
    for msg in messages:
        if msg["role"] not in ("user", "assistant"):
            new_msgs.append(msg)
            continue
        cleaned = _strip_file_blocks(msg["content"], gone_set) if gone else msg["content"]
        if not cleaned.strip():
            pruned += 1
            continue
        if cleaned != msg["content"]:
            pruned += 1
            new_msgs.append({**msg, "content": cleaned})
        else:
            new_msgs.append(msg)
    messages[:] = new_msgs

    # ---- Prune pending_context -------------------------------------------
    state["pending_context"] = [
        c for c in state.get("pending_context", [])
        if (_strip_file_blocks(c, gone_set) if gone else c).strip()
    ]

    # ---- Inject refreshed content ----------------------------------------
    for fp, content in refreshed:
        state.setdefault("pending_context", []).append(
            f"Here is the content of `{fp}`:\n\n```\n{content}\n```"
        )
        vc_commit(fp, content, "Refresh snapshot")

    # ---- Report ----------------------------------------------------------
    lines: list[str] = []
    if refreshed:
        lines.append("[bold green]Updated[/bold green] (reloaded):")
        for fp, _ in refreshed:
            lines.append(f"  [green]+[/green] {fp}")
    if unchanged:
        lines.append("[bold]Unchanged[/bold]:")
        for fp in unchanged:
            lines.append(f"  [dim]\u2013[/dim] {fp}")
    if gone_reportable:
        lines.append("[bold red]Gone[/bold red] (removed from context):")
        for fp in gone_reportable:
            lines.append(f"  [red]\u00d7[/red] {fp}")
    elif gone and not gone_reportable:
        # Files are missing on disk but context was already clean — say nothing.
        pass
    if pruned:
        lines.append(f"\n[dim]Pruned {pruned} stale message(s).[/dim]")
    if not lines:
        lines.append("[info]Everything is up to date.[/info]")

    console.print(Panel("\n".join(lines), title="/refresh", border_style=SAKURA_DEEP))
    save_session(cwd, messages)
