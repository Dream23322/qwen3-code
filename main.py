#!/usr/bin/env python3
"""
qwen3-code: A simple Claude Code-style TUI powered by Ollama + huihui_ai/qwen3-coder-abliterated:30b
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    import ollama
except ImportError:
    print("[error] ollama package not found. Run: pip install ollama")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.theme import Theme
    from rich.text import Text
except ImportError:
    print("[error] rich package not found. Run: pip install rich")
    sys.exit(1)

try:
    from prompt_toolkit import prompt as _pt_prompt
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import InMemoryHistory as _PTHistory
    _PT_AVAILABLE: bool = True
except ImportError:
    _PT_AVAILABLE = False

# -- Constants -----------------------------------------------------------------

MODEL: str              = "huihui_ai/qwen3-coder-abliterated:30b"
VC_DIR: Path            = Path.home() / ".local" / "share" / "qwen3-code" / "vc"
SESSION_DIR: Path       = Path.home() / ".local" / "share" / "qwen3-code" / "sessions"
STREAM_MAX_LINES: int   = 10   # rolling window height during streaming

_PARTIAL_REPROMPT: str = (
    "Your last response contained a partial file (it included truncation markers like "
    "\"...\", \"# rest of\", or similar). "
    "You MUST provide the COMPLETE file content from top to bottom using the "
    "<!-- WRITE: path --> format. Do not omit any section."
)

SYSTEM_PROMPT: str = textwrap.dedent("""\
    You are an expert software engineer assistant embedded in a terminal.
    You help the user understand, write, debug, and refactor code.
    When showing code, always wrap it in fenced code blocks with the correct language tag.
    Be concise and direct. Prefer targeted, minimal changes.
    If asked to run a shell command, explain what it does first.

    RUNNING COMMANDS - when you want to run a shell command as part of helping
    the user (e.g. installing a package, running tests, building the project),
    emit the command using this EXACT marker format ANYWHERE in your response:

    <!-- RUN: <shell command here> -->

    The tool will show the command to the user and ask for confirmation before
    running it. The output will be fed back to you automatically so you can
    continue helping. Only emit one RUN marker per response. Never emit a RUN
    marker for commands the user did not implicitly or explicitly request.

    FILE EDITING - when the user asks you to edit or rewrite a file, respond with
    the complete new file content using this EXACT format (the marker line is
    required so the tool can auto-save it):

    <!-- WRITE: path/to/file -->

    Always provide the COMPLETE file from top to bottom, never partial diffs or
    truncated sections. The tool will back up the original before writing so the
    user can /undo at any time.
""").strip()

# -- Sakura palette ------------------------------------------------------------

SAKURA: str       = "#FFB7C5"
SAKURA_DEEP: str  = "#FF69B4"
SAKURA_MUTED: str = "#FFCDD6"
SAKURA_DARK: str  = "#C2185B"

# -- UI setup ------------------------------------------------------------------

custom_theme: Theme = Theme({
    "user":      f"bold {SAKURA_DEEP}",
    "assistant": f"bold {SAKURA}",
    "system":    f"dim {SAKURA_MUTED}",
    "error":     f"bold {SAKURA_DARK}",
    "info":      f"dim {SAKURA_MUTED}",
})

console: Console = Console(theme=custom_theme)

# -- Help table builder --------------------------------------------------------

def _help_table() -> Table:
    t = Table(show_header=False, box=None, padding=(0, 1), expand=True)
    t.add_column("cmd",  no_wrap=True, min_width=26)
    t.add_column("desc", justify="right")

    D = "[dim]"
    E = "[/dim]"

    rows: list[tuple[str, str]] = [
        ("[bold]General[/bold]", ""),
        ("/cd [dir]",             "change working directory"),
        ("/read <file>",          f"load file into context  {D}(saves baseline snapshot){E}"),
        ("/read -a",              f"load ALL files recursively  {D}(saves baseline snapshots){E}"),
        ("/run <cmd>",            "run a shell command"),
        ("/clear",                "clear conversation history"),
        ("/check <target>",       f"AI code review  {D}ALL | file | file:func{E}"),
        ("/stackview <type>",     f"inspect state  {D}fh / fhf / sessions / env{E}"),
        ("/history",              "show message history"),
        ("/help",                 "show this help"),
        ("/quit",                 "exit"),
        ("", ""),
        ("[bold]Version control[/bold]", f"{D}git-like, tree-based{E}"),
        ("/undo [file]",          "move HEAD to parent commit"),
        ("/redo [file] [id]",     f"move HEAD to child  {D}(menu if branched){E}"),
        ("/checkout <id> [file]", "check out any commit by ID"),
        ("/commit <file> [msg]",  "manually commit current file state"),
        ("/log [file]",           "show commit tree"),
        ("/files",                "list all tracked files"),
        ("", ""),
        ("[bold]Workflow[/bold]", ""),
        ("/read file.py",         f"{D}baseline snapshot created{E}"),
        ("ask AI to edit",        f"{D}AI writes \u2192 diff vs baseline \u2192 AI commit msg{E}"),
    ]

    for cmd_text, desc_text in rows:
        t.add_row(f"  {cmd_text}", desc_text)

    return t


# -- Path helpers --------------------------------------------------------------

def _short_cwd(cwd: str) -> str:
    parts: list[str] = Path(cwd).parts
    if len(parts) <= 2:
        return cwd
    return os.path.join(parts[-2], parts[-1])


# -- Session persistence -------------------------------------------------------

def _session_path(cwd: str) -> Path:
    safe: str = re.sub(r"[^\w.\-]", "_", cwd)
    return SESSION_DIR / f"{safe}.json"


def save_session(cwd: str, messages: list[dict]) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    non_system: list[dict] = [m for m in messages if m.get("role") != "system"]
    data: dict = {
        "cwd":      cwd,
        "saved_at": datetime.now().isoformat(),
        "messages": non_system,
    }
    _session_path(cwd).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_session(cwd: str) -> list[dict]:
    base: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    path: Path = _session_path(cwd)
    if not path.exists():
        return base
    try:
        data: dict        = json.loads(path.read_text(encoding="utf-8"))
        saved: list[dict] = data.get("messages", [])
        saved_at: str     = data.get("saved_at", "unknown")
        count: int        = len(saved)
        console.print(Panel(
            f"[info]Resumed session for [bold]{cwd}[/bold]\n"
            f"{count} message(s) from {saved_at}[/info]",
            title="Session loaded",
            border_style=SAKURA,
        ))
        return base + saved
    except Exception as exc:
        console.print(f"[error]Could not load session: {exc}[/error]")
        return base


# ==============================================================================
# Git-like version control
# ==============================================================================

_VC_CACHE: dict[str, dict] = {}   # filepath -> loaded VC index


def _vc_slot(filepath: str) -> Path:
    safe: str = re.sub(r"[^\w.\-]", "_", str(Path(filepath).resolve()))[-48:]
    slot: Path = VC_DIR / safe
    slot.mkdir(parents=True, exist_ok=True)
    return slot


def _vc_index_path(filepath: str) -> Path:
    return _vc_slot(filepath) / "index.json"


def _load_vc(filepath: str) -> dict:
    if filepath in _VC_CACHE:
        return _VC_CACHE[filepath]
    p: Path = _vc_index_path(filepath)
    if p.exists():
        try:
            data: dict = json.loads(p.read_text(encoding="utf-8"))
            _VC_CACHE[filepath] = data
            return data
        except Exception:
            pass
    idx: dict = {"filepath": filepath, "commits": {}, "head": None, "root": None}
    _VC_CACHE[filepath] = idx
    return idx


def _save_vc(filepath: str) -> None:
    idx: dict = _load_vc(filepath)
    _vc_index_path(filepath).write_text(json.dumps(idx, indent=2), encoding="utf-8")


def _vc_snapshot(filepath: str, content: str) -> Path:
    slot: Path = _vc_slot(filepath)
    ts: str    = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    bak: Path  = slot / f"{ts}.bak"
    bak.write_text(content, encoding="utf-8")
    return bak


def _make_commit_id(filepath: str, content: str, ts: str) -> str:
    raw: str = f"{filepath}:{ts}:{len(content)}:{content[:128]}"
    return hashlib.sha1(raw.encode()).hexdigest()[:7]


def _generate_commit_message(filepath: str, old_content: str, new_content: str) -> str:
    fname: str           = Path(filepath).name
    old_lines: list[str] = old_content.splitlines()
    new_lines: list[str] = new_content.splitlines()
    diff_lines: list[str] = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=2))
    added:   int = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++ "))
    removed: int = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("--- "))

    if not diff_lines:
        return f"No-op ({fname})"

    snippet: str = "\n".join(diff_lines[:40])
    prompt: str  = (
        f"Write a concise git commit message (imperative mood, max 60 chars) "
        f"for this change to `{fname}`. "
        f"Reply with ONLY the commit message text, no quotes, no explanation.\n\n"
        f"```diff\n{snippet}\n```"
    )
    console.print(f"[info]Generating commit message...[/info]", end="")
    try:
        resp = ollama.chat(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        msg: str = resp["message"]["content"].strip().split("\n")[0]
        msg = msg.strip("\"'`")[:72]
        console.print(f" [bold]{msg}[/bold]")
        return msg or f"Update {fname}"
    except Exception:
        fallback: str = f"Update {fname} (+{added}/-{removed} lines)"
        console.print(f" [dim]{fallback}[/dim]")
        return fallback


def _vc_commit(
    filepath: str,
    new_content: str,
    message: str | None = None,
) -> str:
    idx: dict = _load_vc(filepath)
    ts: str   = datetime.now().isoformat()
    cid: str  = _make_commit_id(filepath, new_content, ts)

    snap: Path = _vc_snapshot(filepath, new_content)

    parent_id: str | None = idx.get("head")
    if message is None:
        if parent_id and parent_id in idx["commits"]:
            try:
                old_content: str = Path(idx["commits"][parent_id]["snapshot"]).read_text(encoding="utf-8")
            except Exception:
                old_content = ""
        else:
            old_content = ""
        message = _generate_commit_message(filepath, old_content, new_content)

    commit: dict = {
        "id":        cid,
        "message":   message,
        "timestamp": ts,
        "parent_id": parent_id,
        "snapshot":  str(snap),
        "children":  [],
    }

    if parent_id and parent_id in idx["commits"]:
        if cid not in idx["commits"][parent_id]["children"]:
            idx["commits"][parent_id]["children"].append(cid)

    idx["commits"][cid] = commit
    idx["head"]          = cid
    if not idx.get("root") or not parent_id:
        idx["root"] = cid

    _save_vc(filepath)
    return cid


def _vc_baseline(filepath: str) -> None:
    resolved: str = str(Path(filepath).resolve())
    idx: dict = _load_vc(resolved)
    if idx.get("head"):
        return
    try:
        content: str = Path(resolved).read_text(encoding="utf-8")
    except Exception:
        return
    _vc_commit(resolved, content, "Baseline (pre-edit)")
    console.print(f"[info]Baseline snapshot saved for [bold]{Path(resolved).name}[/bold][/info]")


def write_file_with_vc(
    filepath: str,
    new_content: str,
    commit_message: str | None = None,
) -> None:
    resolved: str = str(Path(filepath).resolve())

    if Path(resolved).exists():
        idx_check: dict = _load_vc(resolved)
        if not idx_check.get("head"):
            _vc_baseline(resolved)

    path: Path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_content, encoding="utf-8")

    cid: str  = _vc_commit(resolved, new_content, commit_message)
    idx: dict = _load_vc(resolved)
    msg: str  = idx["commits"][cid]["message"]
    lines: int = len(new_content.splitlines())

    console.print(Panel(
        f"[info]Wrote [bold]{lines}[/bold] lines to [bold]{filepath}[/bold]\n"
        f"Commit [bold cyan]{cid}[/bold cyan]  {msg}[/info]",
        title="File written", border_style=SAKURA_DEEP,
    ))


# -- VC navigation commands ----------------------------------------------------

def _resolve_commit(filepath: str, id_prefix: str) -> str | None:
    idx: dict = _load_vc(filepath)
    commits: dict = idx.get("commits", {})
    if id_prefix in commits:
        return id_prefix
    matches: list[str] = [cid for cid in commits if cid.startswith(id_prefix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        console.print(f"[error]Ambiguous prefix '{id_prefix}': {matches}[/error]")
    else:
        console.print(f"[error]Commit '{id_prefix}' not found in {Path(filepath).name}[/error]")
    return None


def do_undo(filepath: str) -> None:
    filepath = str(Path(filepath).resolve())
    idx: dict = _load_vc(filepath)
    head_id: str | None = idx.get("head")
    if not head_id or head_id not in idx["commits"]:
        console.print(f"[error]No commits for {filepath}[/error]")
        return
    parent_id: str | None = idx["commits"][head_id].get("parent_id")
    if not parent_id:
        console.print(f"[error]Already at root commit for {filepath}.[/error]")
        return
    parent: dict = idx["commits"][parent_id]
    try:
        content: str = Path(parent["snapshot"]).read_text(encoding="utf-8")
    except Exception as exc:
        console.print(f"[error]Snapshot missing: {exc}[/error]")
        return
    Path(filepath).write_text(content, encoding="utf-8")
    idx["head"] = parent_id
    _save_vc(filepath)
    console.print(Panel(
        f"[info]HEAD is now [bold cyan]{parent_id}[/bold cyan]\n"
        f"{parent['message']}  [dim]{parent['timestamp'][:19]}[/dim]\n\n"
        f"Tip: new writes here will create a branch.[/info]",
        title="Undo", border_style=SAKURA,
    ))


def do_redo(filepath: str, target_id: str | None = None) -> None:
    filepath = str(Path(filepath).resolve())
    idx: dict = _load_vc(filepath)
    head_id: str | None = idx.get("head")
    if not head_id or head_id not in idx["commits"]:
        console.print(f"[error]No commits for {filepath}[/error]")
        return
    children: list[str] = [
        c for c in idx["commits"][head_id].get("children", [])
        if c in idx["commits"]
    ]
    if not children:
        console.print(f"[error]Already at tip of this branch for {filepath}.[/error]")
        return
    if target_id:
        full_id: str | None = _resolve_commit(filepath, target_id)
        if not full_id or full_id not in children:
            console.print(f"[error]Commit {target_id} is not a direct child of HEAD {head_id}.[/error]")
            return
        child_id: str = full_id
    elif len(children) == 1:
        child_id = children[0]
    else:
        rows: list[str] = []
        for cid in children:
            c: dict = idx["commits"][cid]
            rows.append(f"  [bold cyan]{cid}[/bold cyan]  {c['message']}  [dim]{c['timestamp'][:19]}[/dim]")
        console.print(Panel(
            "[info]Multiple branches from HEAD. Choose one:\n\n"
            + "\n".join(rows)
            + "\n\nUse [bold]/redo " + Path(filepath).name + " <id>[/bold] to select.[/info]",
            title="Branch", border_style=SAKURA_DEEP,
        ))
        return
    child: dict = idx["commits"][child_id]
    try:
        content = Path(child["snapshot"]).read_text(encoding="utf-8")
    except Exception as exc:
        console.print(f"[error]Snapshot missing: {exc}[/error]")
        return
    Path(filepath).write_text(content, encoding="utf-8")
    idx["head"] = child_id
    _save_vc(filepath)
    console.print(Panel(
        f"[info]HEAD is now [bold cyan]{child_id}[/bold cyan]\n"
        f"{child['message']}  [dim]{child['timestamp'][:19]}[/dim][/info]",
        title="Redo", border_style=SAKURA,
    ))


def do_checkout(filepath: str, id_prefix: str) -> None:
    filepath = str(Path(filepath).resolve())
    full_id: str | None = _resolve_commit(filepath, id_prefix)
    if not full_id:
        return
    idx: dict    = _load_vc(filepath)
    commit: dict = idx["commits"][full_id]
    try:
        content: str = Path(commit["snapshot"]).read_text(encoding="utf-8")
    except Exception as exc:
        console.print(f"[error]Snapshot missing: {exc}[/error]")
        return
    Path(filepath).write_text(content, encoding="utf-8")
    idx["head"] = full_id
    _save_vc(filepath)
    console.print(Panel(
        f"[info]Checked out [bold cyan]{full_id}[/bold cyan]\n"
        f"{commit['message']}  [dim]{commit['timestamp'][:19]}[/dim]\n\n"
        f"Tip: new writes from here will branch.[/info]",
        title="Checkout", border_style=SAKURA,
    ))


def do_manual_commit(filepath: str, message: str) -> None:
    filepath = str(Path(filepath).resolve())
    path: Path = Path(filepath)
    if not path.exists():
        console.print(f"[error]File not found: {filepath}[/error]")
        return
    content: str = path.read_text(encoding="utf-8")
    cid: str     = _vc_commit(filepath, content, message or None)
    idx: dict    = _load_vc(filepath)
    msg: str     = idx["commits"][cid]["message"]
    console.print(Panel(
        f"[info]Committed [bold]{filepath}[/bold]\n"
        f"[bold cyan]{cid}[/bold cyan]  {msg}[/info]",
        title="Commit", border_style=SAKURA_DEEP,
    ))


def show_log(filepath: str) -> None:
    filepath = str(Path(filepath).resolve())
    idx: dict     = _load_vc(filepath)
    commits: dict = idx.get("commits", {})
    head_id: str | None = idx.get("head")
    root_id: str | None = idx.get("root")

    if not commits:
        console.print(f"[info]No commits recorded for {filepath}.[/info]")
        return

    lines: list[str] = []

    def _walk(cid: str, prefix: str, is_last: bool) -> None:
        c: dict = commits.get(cid, {})
        if not c:
            return
        connector: str  = "\u2514\u2500\u2500 " if is_last else "\u251c\u2500\u2500 "
        head_tag: str   = "  [bold yellow](HEAD)[/bold yellow]" if cid == head_id else ""
        root_tag: str   = "  [dim](root)[/dim]" if cid == root_id and cid != head_id else ""
        branch_tag: str = ""
        if len(commits.get(cid, {}).get("children", [])) > 1 or (
            c.get("parent_id") and
            len(commits.get(c["parent_id"], {}).get("children", [])) > 1
        ):
            branch_tag = "  [dim cyan](branch)[/dim cyan]"
        lines.append(
            f"{prefix}{connector}"
            f"[bold cyan]{cid}[/bold cyan]"
            f"{head_tag}{root_tag}{branch_tag}  "
            f"{c.get('message', '?')}  "
            f"[dim]{c.get('timestamp', '')[:19]}[/dim]"
        )
        child_prefix: str   = prefix + ("    " if is_last else "\u2502   ")
        children: list[str] = [ch for ch in c.get("children", []) if ch in commits]
        for i, child_cid in enumerate(children):
            _walk(child_cid, child_prefix, i == len(children) - 1)

    if root_id and root_id in commits:
        _walk(root_id, "", True)
    else:
        for c in sorted(commits.values(), key=lambda x: x.get("timestamp", "")):
            head_tag = "  (HEAD)" if c["id"] == head_id else ""
            lines.append(f"[bold cyan]{c['id']}[/bold cyan]{head_tag}  {c['message']}  [dim]{c['timestamp'][:19]}[/dim]")

    console.print(Panel(
        "\n".join(lines),
        title=f"Commit tree: {Path(filepath).name}",
        border_style=SAKURA,
    ))


def _all_tracked_files() -> list[str]:
    result: list[str] = []
    if not VC_DIR.exists():
        return result
    for slot in sorted(VC_DIR.iterdir()):
        idx_path: Path = slot / "index.json"
        if idx_path.exists():
            try:
                data: dict = json.loads(idx_path.read_text(encoding="utf-8"))
                fp: str    = data.get("filepath", "")
                if fp:
                    result.append(fp)
            except Exception:
                pass
    return result


# -- General helpers -----------------------------------------------------------

def read_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception as exc:
        return f"[could not read file: {exc}]"


def run_command(cmd: str) -> str:
    try:
        result: subprocess.CompletedProcess = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30,
        )
        return (result.stdout + result.stderr).strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return "[command timed out after 30 s]"
    except Exception as exc:
        return f"[command error: {exc}]"


def build_context_snippet(cwd: str) -> str:
    try:
        files: list[str] = [
            f.name for f in Path(cwd).iterdir()
            if f.is_file() and not f.name.startswith(".")
        ][:20]
    except Exception:
        files = []
    return "\n".join([f"Working directory: {cwd}", f"Visible files: {', '.join(files) if files else 'none'}"])


def resolve_path(arg: str, cwd: str) -> str:
    p: Path = Path(arg)
    return str(p) if p.is_absolute() else str(Path(cwd) / p)


# -- Partial-write detection ---------------------------------------------------

_PARTIAL_PATTERNS: list[re.Pattern] = [
    re.compile(r"^\s*\.{3}\s*$",                       re.MULTILINE),
    re.compile(r"^\s*#\s*\.{3}\s*$",                   re.MULTILINE),
    re.compile(r"^\s*//\s*\.{3}\s*$",                  re.MULTILINE),
    re.compile(r"#\s*(rest|remainder|remaining)\s+of",  re.IGNORECASE),
    re.compile(r"#\s*\.\.\.\.*",                        re.IGNORECASE),
    re.compile(r"//\s*\.\.\.\.*",                       re.IGNORECASE),
    re.compile(r"\[\s*previous\s+(code|content)",       re.IGNORECASE),
    re.compile(r"\[\s*rest\s+of\s+(the\s+)?code",       re.IGNORECASE),
    re.compile(r"# same as before",                     re.IGNORECASE),
    re.compile(r"# unchanged",                          re.IGNORECASE),
    re.compile(r"# \(omitted\)",                        re.IGNORECASE),
]

_WRITE_PATTERN: re.Pattern = re.compile(
    r"<!--\s*WRITE:\s*(?P<path>[^\s>]+)\s*-->\s*```(?:\w+)?\n(?P<code>.*?)```",
    re.DOTALL,
)


def _reply_has_partial_write(reply: str) -> bool:
    for match in _WRITE_PATTERN.finditer(reply):
        code: str = match.group("code")
        for pat in _PARTIAL_PATTERNS:
            if pat.search(code):
                return True
    return False


def apply_file_writes(reply: str) -> None:
    for match in _WRITE_PATTERN.finditer(reply):
        write_file_with_vc(match.group("path").strip(), match.group("code"))


_RUN_PATTERN: re.Pattern = re.compile(r"<!--\s*RUN:\s*(?P<cmd>[^>]+?)\s*-->", re.DOTALL)


def apply_command_runs(reply: str, cwd: str, messages: list[dict]) -> None:
    for match in _RUN_PATTERN.finditer(reply):
        cmd: str = match.group("cmd").strip()
        if not cmd:
            continue
        console.print(Panel(
            f"[bold {SAKURA_DEEP}]The assistant wants to run:[/bold {SAKURA_DEEP}]\n  [bold]{cmd}[/bold]\n\n[info]Run in: {cwd}[/info]",
            title="Permission required", border_style=SAKURA_DARK,
        ))
        try:
            answer: str = input("Allow? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print("[info]Skipped.[/info]")
            continue
        if answer not in ("y", "yes"):
            console.print("[info]Command skipped.[/info]")
            messages.append({"role": "user", "content": f"[Command `{cmd}` was denied by the user.]"})
            continue
        console.print(f"[info]Running: {cmd}[/info]")
        output: str = run_command(cmd)
        console.print(Panel(output, title=f"$ {cmd}", border_style=SAKURA_MUTED))
        messages.append({"role": "user", "content": f"[Command `{cmd}` was run. Output:]\n```\n{output}\n```"})


# -- Tab-completion ------------------------------------------------------------

_SLASH_COMMANDS: list[str] = [
    "/cd", "/read", "/run", "/undo", "/redo", "/files",
    "/clear", "/check", "/stackview", "/history", "/help",
    "/commit", "/log", "/checkout",
    "/quit", "/exit", "/q",
]

_CMD_SUBARGS: dict[str, list[str]] = {
    "/stackview": ["fh", "fhf", "sessions", "sess", "stack", "env", "environment", "help"],
    "/check": ["ALL"],
    "/read":  ["-a"],
}

_FILE_COMMANDS: set[str] = {"/read", "/check", "/undo", "/redo", "/files", "/cd", "/commit", "/log", "/checkout"}


def _fuzzy_match(query: str, candidate: str) -> bool:
    q: str = query.lower()
    c: str = candidate.lower()
    qi: int = 0
    for ch in c:
        if qi < len(q) and ch == q[qi]:
            qi += 1
    return qi == len(q)


if _PT_AVAILABLE:
    class _SlashCompleter(Completer):  # type: ignore[misc]
        def __init__(self, cwd_getter):
            self._cwd = cwd_getter

        def get_completions(self, document, complete_event):
            text: str = document.text_before_cursor
            if not text.startswith("/"):
                return
            parts: list[str] = text.split(maxsplit=1)
            typed_cmd: str   = parts[0]
            is_exact: bool   = typed_cmd.lower() in {c.lower() for c in _SLASH_COMMANDS}
            if len(parts) == 1 or not is_exact:
                for cmd in _SLASH_COMMANDS:
                    if cmd.startswith(typed_cmd) or _fuzzy_match(typed_cmd, cmd):
                        yield Completion(cmd, start_position=-len(text.rstrip()))
                return
            cmd: str        = typed_cmd.lower()
            arg_so_far: str = parts[1]
            if cmd in _CMD_SUBARGS:
                for sub in _CMD_SUBARGS[cmd]:
                    if sub.lower().startswith(arg_so_far.lower()) or (arg_so_far and _fuzzy_match(arg_so_far, sub)):
                        yield Completion(sub, start_position=-len(arg_so_far))
            if cmd in _FILE_COMMANDS and not arg_so_far.startswith("-"):
                cwd: str = self._cwd()
                try:
                    base: Path = Path(cwd)
                    sep: str = "/"
                    if sep in arg_so_far:
                        prefix: str = arg_so_far[: arg_so_far.rfind(sep) + 1]
                        fragment: str = arg_so_far[arg_so_far.rfind(sep) + 1:]
                        search_dir: Path = (base / prefix).resolve()
                    else:
                        prefix = ""
                        fragment = arg_so_far
                        search_dir = base
                    for entry in sorted(search_dir.iterdir()):
                        if entry.name.startswith("."):
                            continue
                        tail: str = entry.name + ("/" if entry.is_dir() else "")
                        if entry.name.lower().startswith(fragment.lower()) or (fragment and _fuzzy_match(fragment, entry.name)):
                            yield Completion(prefix + tail, start_position=-len(arg_so_far))
                except Exception:
                    pass


# -- Fuzzy hint completions ----------------------------------------------------

def _get_fuzzy_completions(text: str, cwd: str) -> list[str]:
    if not text.startswith("/"):
        return []
    parts: list[str]  = text.split(maxsplit=1)
    typed_cmd: str    = parts[0]
    is_exact: bool    = typed_cmd.lower() in {c.lower() for c in _SLASH_COMMANDS}
    if len(parts) == 1 or not is_exact:
        return [cmd for cmd in _SLASH_COMMANDS if cmd.startswith(typed_cmd) or _fuzzy_match(typed_cmd, cmd)]
    cmd: str        = typed_cmd.lower()
    arg_so_far: str = parts[1]
    results: list[str] = []
    if cmd in _CMD_SUBARGS:
        for sub in _CMD_SUBARGS[cmd]:
            if sub.lower().startswith(arg_so_far.lower()) or (arg_so_far and _fuzzy_match(arg_so_far, sub)):
                results.append(f"{typed_cmd} {sub}")
    if not results and cmd in _FILE_COMMANDS and not arg_so_far.startswith("-"):
        if cmd == "/cd":
            try:
                arg = arg_so_far
                last_sep = max(arg.rfind("/"), arg.rfind("\\"))
                if last_sep >= 0:
                    typed_prefix: str = arg[: last_sep + 1]
                    fragment: str     = arg[last_sep + 1:]
                else:
                    typed_prefix = ""
                    fragment     = arg
                search_dir: Path = (Path(cwd) / typed_prefix).resolve() if typed_prefix else Path(cwd).resolve()
                for entry in sorted(search_dir.iterdir()):
                    if not entry.is_dir() or entry.name.startswith("."):
                        continue
                    if not fragment or entry.name.lower().startswith(fragment.lower()) or _fuzzy_match(fragment, entry.name):
                        results.append(f"{typed_cmd} {typed_prefix}{entry.name}")
                        if len(results) >= 7:
                            break
            except Exception:
                pass
        else:
            results.append(f"{typed_cmd} <file>")
    return results


# -- VT enable -----------------------------------------------------------------

def _enable_windows_vt() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import ctypes.wintypes
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        k32.GetStdHandle.restype = ctypes.wintypes.HANDLE
        ENABLE_VT: int = 0x0004
        for std_fd in (-10, -11, -12):
            handle = k32.GetStdHandle(ctypes.c_ulong(std_fd))
            mode   = ctypes.wintypes.DWORD(0)
            if k32.GetConsoleMode(handle, ctypes.byref(mode)):
                k32.SetConsoleMode(handle, mode.value | ENABLE_VT)
    except Exception:
        pass


# -- Inline prompt with fuzzy hint above ---------------------------------------

def _inline_prompt(prompt_str: str, cwd: str, history: list[str]) -> str:
    _enable_windows_vt()

    BOLD: str  = "\033[1m"
    DIM: str   = "\033[2m"
    CYAN: str  = (
        f"\033[38;2;"
        f"{int(SAKURA_DEEP[1:3],16)};"
        f"{int(SAKURA_DEEP[3:5],16)};"
        f"{int(SAKURA_DEEP[5:7],16)}m"
    )
    RESET: str = "\033[0m"

    UP_CLEAR   = "\033[1A\r\033[2K"
    LINE_CLEAR = "\r\033[2K"

    buf: list[str]       = []
    hist_idx: int        = len(history)
    saved_buf: list[str] = []

    def _text() -> str:
        return "".join(buf)

    def _build_hint(text: str) -> str:
        matches: list[str] = _get_fuzzy_completions(text, cwd)
        if not matches:
            return ""
        term_w: int = max(40, (console.width or 80) - 2)
        parts_h: list[str] = []
        plain_len: int = 0
        for i, m in enumerate(matches[:7]):
            sep = "  " if parts_h else ""
            if plain_len + len(sep) + len(m) > term_w:
                break
            parts_h.append(
                f"{BOLD}{CYAN}{m}{RESET}" if i == 0 else f"{DIM}{m}{RESET}"
            )
            plain_len += len(sep) + len(m)
        return "  ".join(parts_h)

    def _tab_complete(text: str) -> str:
        matches: list[str] = _get_fuzzy_completions(text, cwd)
        if not matches:
            return text
        best: str = matches[0]
        return best + " " if best in _SLASH_COMMANDS else best

    sys.stdout.write("\n" + prompt_str)
    sys.stdout.flush()

    def _render(text: str) -> None:
        hint: str = _build_hint(text)
        sys.stdout.write(
            UP_CLEAR + hint + "\n" + LINE_CLEAR + prompt_str + text
        )
        sys.stdout.flush()

    def _clear_hint() -> None:
        sys.stdout.write(UP_CLEAR + "\n" + LINE_CLEAR)
        sys.stdout.flush()

    try:
        if sys.platform == "win32":
            import msvcrt

            while True:
                ch: str = msvcrt.getwch()  # type: ignore[attr-defined]

                if ch in ("\x00", "\xe0"):
                    ch2: str = msvcrt.getwch()  # type: ignore[attr-defined]
                    if ch2 == "H":
                        if hist_idx > 0:
                            if hist_idx == len(history):
                                saved_buf = buf[:]
                            hist_idx -= 1
                            buf[:] = list(history[hist_idx])
                    elif ch2 == "P":
                        if hist_idx < len(history):
                            hist_idx += 1
                            buf[:] = list(history[hist_idx] if hist_idx < len(history) else saved_buf)
                    _render(_text())
                    continue

                if ch in ("\r", "\n"):
                    _clear_hint()
                    sys.stdout.write(prompt_str + _text() + "\n")
                    sys.stdout.flush()
                    break
                elif ch == "\x03":
                    _clear_hint()
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    raise KeyboardInterrupt
                elif ch == "\x04":
                    _clear_hint()
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    raise EOFError
                elif ch in ("\x08", "\x7f"):
                    if buf:
                        buf.pop()
                elif ch == "\t":
                    buf[:] = list(_tab_complete(_text()))
                else:
                    buf.append(ch)

                _render(_text())

        else:
            import tty
            import termios
            import select as _sel

            fd: int = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                while True:
                    _sel.select([sys.stdin], [], [])
                    raw: bytes = os.read(fd, 1)

                    if raw in (b"\r", b"\n"):
                        _clear_hint()
                        sys.stdout.write(prompt_str + _text() + "\n")
                        sys.stdout.flush()
                        break
                    elif raw == b"\x03":
                        _clear_hint()
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        raise KeyboardInterrupt
                    elif raw == b"\x04":
                        _clear_hint()
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        raise EOFError
                    elif raw in (b"\x08", b"\x7f"):
                        if buf:
                            buf.pop()
                    elif raw == b"\t":
                        buf[:] = list(_tab_complete(_text()))
                    elif raw == b"\x1b":
                        rest: bytes = os.read(fd, 2)
                        if rest == b"[A":
                            if hist_idx > 0:
                                if hist_idx == len(history):
                                    saved_buf = buf[:]
                                hist_idx -= 1
                                buf[:] = list(history[hist_idx])
                        elif rest == b"[B":
                            if hist_idx < len(history):
                                hist_idx += 1
                                buf[:] = list(history[hist_idx] if hist_idx < len(history) else saved_buf)
                    else:
                        try:
                            buf.append(raw.decode("utf-8"))
                        except Exception:
                            pass

                    _render(_text())
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    except (KeyboardInterrupt, EOFError):
        raise
    except Exception:
        return input(prompt_str)

    return _text()


# -- /check helpers ------------------------------------------------------------

_CODE_EXTENSIONS: set[str] = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".java", ".kt", ".swift",
    ".rb", ".php", ".cs", ".sh", ".bash", ".zsh",
    ".lua", ".r", ".scala", ".zig",
}


def _extract_function(source: str, func_name: str) -> str | None:
    py_pat = re.compile(rf"^([ \t]*)(async\s+)?def\s+{re.escape(func_name)}\s*\(", re.MULTILINE)
    m = py_pat.search(source)
    if m:
        indent: str  = m.group(1)
        lines: list[str] = source[m.start():].splitlines(keepends=True)
        body: list[str]  = [lines[0]]
        for line in lines[1:]:
            if line.strip() and not line.startswith("\t") and indent == "":
                if re.match(r"(async\s+)?def |class ", line.lstrip()):
                    break
            elif line.strip() and indent and not line.startswith(indent + " ") and not line.startswith(indent + "\t"):
                if re.match(r"[ \t]*(async\s+)?def |[ \t]*class ", line):
                    break
            body.append(line)
        while body and not body[-1].strip():
            body.pop()
        return "".join(body)

    js_pat = re.compile(
        rf"(?:(?:async\s+)?function\s+{re.escape(func_name)}|(?:const|let|var)\s+{re.escape(func_name)}\s*=|[\s,{{]\s*{re.escape(func_name)}\s*(?:\([^)]*\)\s*=>|\([^)]*\)\s*\{{))",
        re.MULTILINE,
    )
    m = js_pat.search(source)
    if m:
        brace_start: int = source.find("{", m.start())
        if brace_start != -1:
            depth: int = 0
            end: int   = brace_start
            for i, ch in enumerate(source[brace_start:], brace_start):
                if ch == "{":   depth += 1
                elif ch == "}": depth -= 1
                if depth == 0:  end = i + 1; break
            return source[m.start():end]

    generic_pat = re.compile(rf"[\w\s*&]*\b{re.escape(func_name)}\s*\([^)]*\)\s*[{{]", re.MULTILINE)
    m = generic_pat.search(source)
    if m:
        brace_start = source.find("{", m.start())
        if brace_start != -1:
            depth = 0
            end   = brace_start
            for i, ch in enumerate(source[brace_start:], brace_start):
                if ch == "{":   depth += 1
                elif ch == "}": depth -= 1
                if depth == 0:  end = i + 1; break
            return source[m.start():end]
    return None


def _build_check_prompt(label: str, code: str, scope: str) -> str:
    return (
        f"Please review the following {scope} for bugs, logic errors, "
        f"potential runtime exceptions, bad practices, and security issues.\n"
        f"For each issue found, state: file/function, severity (critical/warning/info), "
        f"a one-line description, and a suggested fix.\n"
        f"If no issues are found, say so.\n\n--- {label} ---\n```\n{code}\n```"
    )


def handle_check(arg: str, messages: list[dict], state: dict) -> None:
    cwd: str       = state["cwd"]
    arg_clean: str = arg.strip()

    if arg_clean.upper() == "ALL":
        try:
            source_files: list[Path] = [
                f for f in Path(cwd).rglob("*")
                if f.is_file() and f.suffix.lower() in _CODE_EXTENSIONS
                and not any(part.startswith(".") for part in f.parts)
            ]
        except Exception as exc:
            console.print(f"[error]Could not scan directory: {exc}[/error]")
            return
        if not source_files:
            console.print(f"[info]No source files found in {cwd}.[/info]")
            return
        parts_list: list[str] = []
        total_chars: int = 0
        skipped: list[str] = []
        for sf in sorted(source_files):
            rel: str = os.path.relpath(str(sf), cwd)
            try:
                content: str = sf.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if total_chars + len(content) > 400_000:
                skipped.append(rel)
                continue
            parts_list.append(f"# -- {rel} --\n{content}")
            total_chars += len(content)
        if skipped:
            console.print(f"[info]Skipped (too large): {', '.join(skipped[:5])}{' ...' if len(skipped) > 5 else ''}[/info]")
        combined: str   = "\n\n".join(parts_list)
        file_count: int = len(parts_list)
        console.print(f"[info]Checking {file_count} file(s) in {_short_cwd(cwd)}...[/info]")
        prompt: str = _build_check_prompt(f"{file_count} file(s) in {cwd}", combined, "workspace")

    elif ":" in arg_clean and not arg_clean.startswith(":"):
        last_colon: int = arg_clean.rfind(":")
        resolved: str   = resolve_path(arg_clean[:last_colon], cwd)
        func_name: str  = arg_clean[last_colon + 1:].strip().rstrip("()")
        if not Path(resolved).exists():
            console.print(f"[error]File not found: {resolved}[/error]")
            return
        source: str = read_file(resolved)
        func_src: str | None = _extract_function(source, func_name)
        if func_src is None:
            console.print(f"[error]Function '{func_name}' not found. Falling back to full file.[/error]")
            prompt = _build_check_prompt(resolved, source, f"file ({Path(resolved).name})")
        else:
            console.print(f"[info]Checking function '{func_name}' in {resolved}[/info]")
            prompt = _build_check_prompt(f"{Path(resolved).name}:{func_name}", func_src, f"function '{func_name}'")

    else:
        resolved = resolve_path(arg_clean, cwd)
        if not Path(resolved).exists():
            console.print(f"[error]File not found: {resolved}[/error]")
            return
        source = read_file(resolved)
        console.print(f"[info]Checking {resolved}[/info]")
        prompt = _build_check_prompt(resolved, source, f"file ({Path(resolved).name})")

    messages.append({"role": "user", "content": prompt})
    reply: str = stream_response(messages)
    if reply:
        messages.append({"role": "assistant", "content": reply})
        save_session(cwd, messages)


# -- /stackview ----------------------------------------------------------------

_SV_TYPES: dict[str, str] = {
    "fh":       "File history  (current project)",
    "fhf":      "File history full (all projects)",
    "sessions": "Saved sessions",
    "env":      "Runtime environment info",
}


def _sv_fh(cwd: str) -> None:
    tracked: list[str] = _all_tracked_files()
    local: list[str]   = [fp for fp in tracked if fp.startswith(cwd)]
    if not local:
        console.print(f"[info]No tracked files under {cwd}.[/info]")
        return
    rows: list[str] = []
    for fp in local:
        idx: dict = _load_vc(fp)
        commits: dict = idx.get("commits", {})
        head_id: str | None = idx.get("head")
        head_msg: str = commits[head_id]["message"][:40] if head_id and head_id in commits else "(no commits)"
        rows.append(
            f"  {os.path.relpath(fp, cwd):<36}"
            f"  {'exists' if Path(fp).exists() else 'missing':<7}"
            f"  {len(commits)} commits"
            f"  HEAD: [cyan]{head_id or '-'}[/cyan] {head_msg}"
        )
    console.print(Panel("\n".join(rows), title=f"File history [{_short_cwd(cwd)}]", border_style=SAKURA))


def _sv_fhf() -> None:
    tracked: list[str] = _all_tracked_files()
    if not tracked:
        console.print("[info]No tracked files.[/info]")
        return
    by_dir: dict[str, list[str]] = defaultdict(list)
    for fp in tracked:
        by_dir[str(Path(fp).parent)].append(fp)
    rows: list[str] = []
    for directory in sorted(by_dir):
        rows.append(f"  [{directory}]")
        for fp in sorted(by_dir[directory]):
            idx: dict   = _load_vc(fp)
            n: int      = len(idx.get("commits", {}))
            exists: str = "exists" if Path(fp).exists() else "missing"
            rows.append(f"    {Path(fp).name:<36}  {exists:<7}  {n} commits")
    console.print(Panel("\n".join(rows), title="File history (all projects)", border_style=SAKURA))


def _sv_sessions() -> None:
    if not SESSION_DIR.exists() or not list(SESSION_DIR.glob("*.json")):
        console.print("[info]No sessions saved yet.[/info]")
        return
    rows: list[str] = []
    for sf in sorted(SESSION_DIR.glob("*.json")):
        try:
            data: dict = json.loads(sf.read_text(encoding="utf-8"))
            rows.append(
                f"  {data.get('cwd','?'):<45}"
                f"  {data.get('saved_at','?')[:19].replace('T','  ')}"
                f"  {len(data.get('messages',[])):>3} msg"
                f"  {sf.stat().st_size:>6} B"
            )
        except Exception:
            rows.append(f"  {sf.name}  (unreadable)")
    console.print(Panel("\n".join(rows), title=f"Saved sessions ({SESSION_DIR})", border_style=SAKURA))


def _sv_env(cwd: str, messages: list[dict]) -> None:
    sf: Path = _session_path(cwd)
    rows: list[str] = [
        f"  Model        : {MODEL}",
        f"  CWD          : {cwd}",
        f"  VC dir       : {VC_DIR}",
        f"  Session dir  : {SESSION_DIR}",
        f"  Session file : {sf}  ({'  ' + str(sf.stat().st_size) + ' B' if sf.exists() else '(no session file)'})",
        f"  Messages     : {len([m for m in messages if m['role'] != 'system'])} in current session",
        f"  Python       : {sys.version.split()[0]}  ({sys.executable})",
    ]
    console.print(Panel("\n".join(rows), title="Environment", border_style=SAKURA_DEEP))


def handle_stackview(sv_type: str, cwd: str, messages: list[dict]) -> None:
    t: str = sv_type.strip().lower()
    if t == "fh":                    _sv_fh(cwd)
    elif t == "fhf":                 _sv_fhf()
    elif t in ("sessions", "sess"): _sv_sessions()
    elif t in ("env", "environment"): _sv_env(cwd, messages)
    elif t in ("", "help"):
        rows: list[str] = [f"  {k:<12}  {v}" for k, v in _SV_TYPES.items()]
        rows += ["  sess        Alias for 'sessions'", "  environment Alias for 'env'"]
        console.print(Panel("\n".join(rows), title="/stackview types", border_style=SAKURA_DEEP))
    else:
        console.print(f"[error]Unknown stackview type: '{t}'. Run /stackview help.[/error]")


# -- Slash commands ------------------------------------------------------------

def handle_slash_command(cmd: str, messages: list[dict], state: dict) -> bool:
    parts: list[str] = cmd.strip().split(maxsplit=1)
    name: str        = parts[0].lower()
    arg: str         = parts[1] if len(parts) > 1 else ""
    cwd: str         = state["cwd"]

    if name in ("/quit", "/exit", "/q"):
        console.print("[info]Goodbye.[/info]")
        return False

    elif name == "/cd":
        if not arg:
            console.print(f"[info]Current directory: {cwd}[/info]")
        else:
            target: Path = Path(arg) if Path(arg).is_absolute() else Path(cwd) / arg
            try:
                target = target.resolve(strict=True)
                if not target.is_dir():
                    console.print(f"[error]{target} is not a directory.[/error]")
                else:
                    save_session(cwd, messages)
                    state["cwd"] = str(target)
                    os.chdir(target)
                    new_messages: list[dict] = load_session(str(target))
                    messages.clear()
                    messages.extend(new_messages)
                    state["first_message"] = not any(m["role"] != "system" for m in messages)
                    try:
                        entries: list[str] = [e.name for e in target.iterdir() if not e.name.startswith(".")][:30]
                    except Exception:
                        entries = []
                    console.print(Panel(
                        f"[info]Changed to: [bold]{target}[/bold]\nContents: {', '.join(entries) if entries else '(empty)'}[/info]",
                        title="cd", border_style=SAKURA,
                    ))
            except FileNotFoundError:
                console.print(f"[error]Directory not found: {arg}[/error]")

    elif name == "/clear":
        messages.clear()
        console.clear()
        console.print("[info]Conversation cleared.[/info]")

    elif name == "/read":
        if not arg:
            console.print("[error]Usage: /read <filepath> | /read -a[/error]")
        elif arg.strip() == "-a":
            try:
                all_files: list[Path] = [
                    f for f in Path(cwd).rglob("*")
                    if f.is_file() and not any(part.startswith(".") for part in f.parts)
                ]
            except Exception as exc:
                console.print(f"[error]Could not scan directory: {exc}[/error]")
            else:
                snippets: list[str] = []
                total_chars: int    = 0
                skipped: list[str]  = []
                baselined: int      = 0
                for sf in sorted(all_files):
                    rel: str = os.path.relpath(str(sf), cwd)
                    try:
                        fc: str = sf.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        skipped.append(rel); continue
                    if total_chars + len(fc) > 400_000:
                        skipped.append(rel); continue
                    resolved_f: str = str(sf.resolve())
                    if not _load_vc(resolved_f).get("head"):
                        _vc_commit(resolved_f, fc, "Baseline (pre-edit)")
                        baselined += 1
                    snippets.append(f"### {rel}\n```\n{fc}\n```")
                    total_chars += len(fc)
                if skipped:
                    console.print("[info]Skipped: " + ", ".join(skipped[:5]) + (" ..." if len(skipped) > 5 else "") + "[/info]")
                if snippets:
                    state.setdefault("pending_context", []).append(
                        f"Here are all {len(snippets)} file(s) from `{cwd}`:\n\n" + "\n\n".join(snippets)
                    )
                    console.print(
                        f"[info]Loaded {len(snippets)} file(s) into context"
                        + (f", saved {baselined} baseline snapshot(s)." if baselined else ".")
                        + "[/info]"
                    )
                else:
                    console.print("[info]No readable files found.[/info]")
        else:
            resolved: str = resolve_path(arg, cwd)
            content: str  = read_file(resolved)
            if Path(resolved).exists():
                resolved_abs: str = str(Path(resolved).resolve())
                if not _load_vc(resolved_abs).get("head"):
                    _vc_commit(resolved_abs, content, "Baseline (pre-edit)")
                    console.print(f"[info]Baseline snapshot saved for [bold]{Path(resolved).name}[/bold][/info]")
            state.setdefault("pending_context", []).append(
                f"Here is the content of `{resolved}`:\n\n```\n{content}\n```"
            )
            console.print(f"[info]Loaded {resolved} into context.[/info]")

    elif name == "/run":
        if not arg:
            console.print("[error]Usage: /run <shell command>[/error]")
        else:
            output: str = run_command(arg)
            messages.append({"role": "user", "content": f"Output of `{arg}`:\n\n```\n{output}\n```"})
            console.print(Panel(output, title=f"$ {arg}", border_style=SAKURA_MUTED))

    elif name == "/undo":
        if not arg:
            tracked: list[str] = _all_tracked_files()
            candidates: list[str] = [
                fp for fp in tracked
                if _load_vc(fp).get("head") and
                _load_vc(fp)["commits"].get(_load_vc(fp)["head"], {}).get("parent_id")
            ]
            if not candidates:         console.print("[info]Nothing to undo.[/info]")
            elif len(candidates) == 1: do_undo(candidates[0])
            else:
                console.print("[info]Multiple files. Specify one:[/info]")
                for fp in candidates: console.print(f"  [info]/undo {fp}[/info]")
        else:
            do_undo(resolve_path(arg.split()[0], cwd))

    elif name == "/redo":
        tokens: list[str] = arg.split(maxsplit=1) if arg else []
        if not tokens:
            tracked = _all_tracked_files()
            candidates = [
                fp for fp in tracked
                if _load_vc(fp).get("head") and
                _load_vc(fp)["commits"].get(_load_vc(fp)["head"], {}).get("children")
            ]
            if not candidates:         console.print("[info]Nothing to redo.[/info]")
            elif len(candidates) == 1: do_redo(candidates[0])
            else:
                console.print("[info]Multiple files. Specify one:[/info]")
                for fp in candidates: console.print(f"  [info]/redo {fp}[/info]")
        elif len(tokens) == 1:
            do_redo(resolve_path(tokens[0], cwd))
        else:
            do_redo(resolve_path(tokens[0], cwd), target_id=tokens[1])

    elif name == "/checkout":
        if not arg:
            console.print("[error]Usage: /checkout <commit_id> [filepath][/error]")
        else:
            tokens = arg.split(maxsplit=1)
            commit_id_arg: str = tokens[0]
            fp_arg: str | None = tokens[1] if len(tokens) > 1 else None
            if fp_arg:
                do_checkout(resolve_path(fp_arg, cwd), commit_id_arg)
            else:
                tracked = _all_tracked_files()
                found: list[str] = []
                for fp in tracked:
                    idx: dict = _load_vc(fp)
                    if any(c.startswith(commit_id_arg) for c in idx.get("commits", {})):
                        found.append(fp)
                if len(found) == 1:
                    do_checkout(found[0], commit_id_arg)
                elif len(found) > 1:
                    console.print(f"[info]Commit ID matches multiple files. Specify filepath:[/info]")
                    for fp in found: console.print(f"  /checkout {commit_id_arg} {fp}")
                else:
                    console.print(f"[error]Commit '{commit_id_arg}' not found in any tracked file.[/error]")

    elif name == "/files":
        tracked = _all_tracked_files()
        if not tracked:
            console.print("[info]No tracked files.[/info]")
        else:
            rows: list[str] = []
            for fp in tracked:
                idx = _load_vc(fp)
                n   = len(idx.get("commits", {}))
                hid = idx.get("head", "-")
                rows.append(f"  {fp}  {'exists' if Path(fp).exists() else 'missing'}  {n} commits  HEAD={hid}")
            console.print(Panel("\n".join(rows), title="Tracked files", border_style=SAKURA))

    elif name == "/check":
        if not arg: console.print("[error]Usage: /check ALL | /check <file> | /check <file>:<function>[/error]")
        else:       handle_check(arg, messages, state)

    elif name == "/stackview":
        handle_stackview(arg, cwd, messages)

    elif name == "/commit":
        if not arg:
            console.print("[error]Usage: /commit <filepath> [message][/error]")
        else:
            tokens = arg.split(maxsplit=1)
            fp_c: str  = resolve_path(tokens[0], cwd)
            msg_c: str = tokens[1] if len(tokens) > 1 else ""
            do_manual_commit(fp_c, msg_c)

    elif name == "/log":
        if not arg:
            tracked = _all_tracked_files()
            if not tracked: console.print("[info]No tracked files.[/info]")
            elif len(tracked) == 1: show_log(tracked[0])
            else:
                console.print("[info]Multiple files. Specify one:[/info]")
                for fp in tracked: console.print(f"  /log {fp}")
        else:
            show_log(resolve_path(arg.split()[0], cwd))

    elif name == "/history":
        for i, m in enumerate(messages):
            console.print(f"[info][{i}] {m['role']}: {m['content'][:120].replace(chr(10), ' ')}[/info]")

    elif name == "/help":
        console.print(Panel(_help_table(), title="Help", border_style=SAKURA_DEEP))

    else:
        console.print(f"[error]Unknown command: {name}. Type /help for a list.[/error]")

    return True


# -- Response rendering --------------------------------------------------------

def render_response(text: str) -> None:
    _CODE_BLOCK_RE = re.compile(r"(```(?:\w+)?\n.*?```)", re.DOTALL)
    for part in _CODE_BLOCK_RE.split(text):
        if not part:
            continue
        if part.startswith("```") and part.endswith("```"):
            match = re.match(r"```(\w+)?\n(.*?)```", part, re.DOTALL)
            if not match:
                console.print(Markdown(part))
                continue
            lang: str = match.group(1) or "text"
            code: str = match.group(2)
            block_start: int = text.find(part)
            prefix_text: str = text[max(0, block_start - 200): block_start]
            wm = re.search(r"<!--\s*WRITE:\s*([^\s>]+)\s*-->", prefix_text)
            if wm:
                title: str  = f"Written to {wm.group(1)}"
                border: str = SAKURA_DEEP
            else:
                title  = f"Code ({lang})"
                border = SAKURA
            console.print(Panel(Syntax(code, lang, theme="dracula", line_numbers=True), title=title, border_style=border))
        else:
            cleaned: str = re.sub(r"<!--\s*WRITE:[^>]+-->", "", part).strip()
            if cleaned:
                console.print(Markdown(cleaned))


# -- Streaming -----------------------------------------------------------------

def _watch_for_cancel(cancel_event: threading.Event) -> None:
    try:
        import select as _sel, termios as _t, tty as _tty
        fd: int = sys.stdin.fileno()
        old     = _t.tcgetattr(fd)
        try:
            _tty.setraw(fd)
            while not cancel_event.is_set():
                r, _, _ = _sel.select([sys.stdin], [], [], 0.05)
                if r and os.read(fd, 1) == b"\x04":
                    cancel_event.set(); break
        finally:
            _t.tcsetattr(fd, _t.TCSADRAIN, old)
    except Exception:
        try:
            import msvcrt
            while not cancel_event.is_set():
                if msvcrt.kbhit() and msvcrt.getwch() == "\x04":
                    cancel_event.set(); break
                time.sleep(0.05)
        except Exception:
            pass


def _status_line(left: str, right: str) -> Text:
    width: int      = console.width
    plain_left: str = re.sub(r"\[/?[^\]]*\]", "", left)
    pad: int        = max(1, width - len(plain_left) - len(right))
    line: Text = Text()
    line.append("assistant", style=f"bold {SAKURA}")
    line.append("  ", style="")
    line.append(left.replace("assistant  ", ""), style="dim")
    line.append(" " * pad, style="")
    line.append(right, style=f"dim {SAKURA_MUTED}")
    return line


def _raw_stream(
    messages: list[dict],
    cancel_event: threading.Event | None = None,
) -> tuple[str, int]:
    """
    Stream tokens from Ollama with a rolling STREAM_MAX_LINES-line window.

    The terminal shows at most STREAM_MAX_LINES complete lines at once;
    when a new line would exceed the limit the oldest is dropped and the
    window redraws in-place.  The current (incomplete) partial line is
    always shown below the window.

    Returns (full_text, rendered_rows) where rendered_rows is the number
    of terminal rows currently occupied by the stream output.  The caller
    is responsible for erasing those rows (plus any rows above like the
    status line) before rendering the final markdown.
    """
    full: str          = ""
    window: list[str]  = []   # complete lines in the visible window
    partial: str       = ""   # current incomplete line being built
    rendered_rows: int = 0    # total rows on screen (window lines + partial line)

    def _redraw() -> None:
        """Redraw the entire window + partial in place."""
        nonlocal rendered_rows
        # Move cursor back to the top of our window area.
        # rendered_rows - 1 = number of lines ABOVE the partial (i.e. window lines).
        lines_above = rendered_rows - 1
        if lines_above > 0:
            sys.stdout.write(f"\033[{lines_above}A")
        sys.stdout.write("\r")
        # Write each window line, clearing to end-of-line.
        for line in window:
            sys.stdout.write("\033[2K" + line + "\n")
        # Write the partial line (no trailing newline).
        sys.stdout.write("\033[2K" + partial)
        sys.stdout.flush()
        rendered_rows = len(window) + 1   # window lines + partial line

    try:
        for chunk in ollama.chat(model=MODEL, messages=messages, stream=True):
            if cancel_event and cancel_event.is_set():
                break
            token: str = chunk["message"]["content"]
            full += token

            if "\n" in token:
                # Split on newlines; each split boundary means a line was completed.
                parts: list[str] = token.split("\n")

                # First segment completes the current partial.
                partial += parts[0]
                window.append(partial)
                if len(window) > STREAM_MAX_LINES:
                    window = window[-STREAM_MAX_LINES:]

                # Middle segments are complete lines with no further content yet.
                for mid in parts[1:-1]:
                    window.append(mid)
                    if len(window) > STREAM_MAX_LINES:
                        window = window[-STREAM_MAX_LINES:]

                # Last segment starts the new partial line.
                partial = parts[-1]
                _redraw()

            else:
                # No newline: extend partial and overwrite only the bottom line.
                partial += token
                if rendered_rows == 0:
                    # First token ever — just write it; cursor stays on same line.
                    sys.stdout.write(partial)
                    rendered_rows = 1
                else:
                    # Cursor is already on the partial line; overwrite it.
                    sys.stdout.write("\r\033[2K" + partial)
                sys.stdout.flush()

    except Exception as exc:
        if not (cancel_event and cancel_event.is_set()):
            console.print(f"[error]Ollama error: {exc}[/error]")
            console.print(f"[info]  ollama pull {MODEL}[/info]")

    return full, rendered_rows


def stream_response(messages: list[dict], cwd: str = "") -> str:
    cancel_event: threading.Event = threading.Event()

    # Print a blank line then the status bar.
    # These 2 lines are counted when clearing after streaming.
    console.print()
    console.print(_status_line("thinking...", "ctrl+d to cancel"))

    watcher: threading.Thread = threading.Thread(
        target=_watch_for_cancel, args=(cancel_event,), daemon=True
    )
    watcher.start()
    full_reply, window_rows = _raw_stream(messages, cancel_event)
    cancel_event.set()
    watcher.join(timeout=0.5)

    # ------------------------------------------------------------------ clear
    # Erase the stream window AND the 2 header lines (blank + status bar).
    #
    # Cursor is currently at the end of the partial line, which is:
    #   window_rows - 1  lines below the start of the window area
    #   + 2              lines below the blank/status lines
    # So total cursor-up needed to reach the blank line:
    #   (window_rows - 1) + 2  =  window_rows + 1  (for window_rows >= 1)
    #   or simply 2            (for window_rows == 0, nothing was printed)
    clear_rows: int = max(2, window_rows + 1)
    sys.stdout.write(f"\033[{clear_rows}A\r\033[J")
    sys.stdout.flush()
    # -------------------------------------------------------------------/ clear

    if not full_reply:
        if cancel_event.is_set():
            console.print("[info]Cancelled.[/info]")
        return full_reply

    if _reply_has_partial_write(full_reply):
        console.print(Panel(
            "[info]Partial file detected. Reprompting...[/info]",
            title="Partial write", border_style=SAKURA_DARK,
        ))
        messages.append({"role": "assistant", "content": full_reply})
        messages.append({"role": "user",      "content": _PARTIAL_REPROMPT})
        rc: threading.Event = threading.Event()
        console.print()
        console.print(_status_line("retrying...", "ctrl+d to cancel"))
        rw: threading.Thread = threading.Thread(
            target=_watch_for_cancel, args=(rc,), daemon=True
        )
        rw.start()
        rr, rw_rows = _raw_stream(messages, rc)
        rc.set(); rw.join(timeout=0.5)
        clear_rows_r = max(2, rw_rows + 1)
        sys.stdout.write(f"\033[{clear_rows_r}A\r\033[J")
        sys.stdout.flush()
        messages.pop(); messages.pop()
        if rr:
            full_reply = rr

    render_response(full_reply)
    apply_file_writes(full_reply)
    apply_command_runs(full_reply, cwd, messages)
    return full_reply


# -- Main loop -----------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(prog="qwen3-code")
    parser.add_argument("dir", nargs="?", default=None)
    parser.add_argument("--dir", "-d", dest="dir_flag", default=None, metavar="DIR")
    args = parser.parse_args()

    raw_dir: str | None = args.dir or args.dir_flag
    if raw_dir is not None:
        target: Path = Path(raw_dir).expanduser().resolve()
        if not target.is_dir(): print(f"[error] Not a directory: {raw_dir}"); sys.exit(1)
        os.chdir(target)

    # Enable VT processing early so ANSI codes work in the stream window on Windows.
    _enable_windows_vt()

    VC_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    initial_cwd: str = os.getcwd()
    state: dict      = {"cwd": initial_cwd, "first_message": True, "pending_context": []}

    console.print(Panel(
        f"[bold {SAKURA_DEEP}]qwen3-code[/bold {SAKURA_DEEP}]  -  simple coding assistant TUI\n"
        f"Model : [{SAKURA}]{MODEL}[/{SAKURA}]\n"
        f"CWD   : [{SAKURA}]{initial_cwd}[/{SAKURA}]\n\n"
        f"Type [{SAKURA_DEEP}]/help[/{SAKURA_DEEP}] for commands, [{SAKURA_DEEP}]/quit[/{SAKURA_DEEP}] to exit.",
        border_style=SAKURA, title="qwen3-code",
    ))

    messages: list[dict] = load_session(initial_cwd)
    if any(m["role"] != "system" for m in messages):
        state["first_message"] = False

    _input_history: list[str] = []

    while True:
        cwd: str   = state["cwd"]
        short: str = _short_cwd(cwd)
        try:
            user_input: str = _inline_prompt(f"you ({short}): ", cwd, _input_history)
        except (KeyboardInterrupt, EOFError):
            console.print("\n[info]Goodbye.[/info]")
            save_session(cwd, messages)
            break

        user_input = user_input.strip()
        if not user_input:
            continue
        if not _input_history or _input_history[-1] != user_input:
            _input_history.append(user_input)

        if user_input.startswith("/"):
            if not handle_slash_command(user_input, messages, state):
                save_session(state["cwd"], messages)
                break
            continue

        content: str = (build_context_snippet(cwd) + "\n\n" + user_input) if state["first_message"] else user_input
        state["first_message"] = False

        pending: list[str] = state.get("pending_context", [])
        if pending:
            content = "\n\n".join(pending) + "\n\n" + content
            state["pending_context"] = []

        messages.append({"role": "user", "content": content})
        reply: str = stream_response(messages, cwd)
        if reply:
            messages.append({"role": "assistant", "content": reply})
            save_session(cwd, messages)


if __name__ == "__main__":
    main()
