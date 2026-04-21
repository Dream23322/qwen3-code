#!/usr/bin/env python3
"""
qwen3-code: A simple Claude Code-style TUI powered by Ollama + huihui_ai/qwen3-coder-abliterated:30b
"""

import argparse
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

# ── Constants ──────────────────────────────────────────────────────────────────

MODEL: str        = "huihui_ai/qwen3-coder-abliterated:30b"
VC_DIR: Path      = Path.home() / ".local" / "share" / "qwen3-code" / "vc"
SESSION_DIR: Path = Path.home() / ".local" / "share" / "qwen3-code" / "sessions"

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

# ── Sakura pink palette ───────────────────────────────────────────────────────────────

SAKURA: str       = "#FFB7C5"
SAKURA_DEEP: str  = "#FF69B4"
SAKURA_MUTED: str = "#FFCDD6"
SAKURA_DARK: str  = "#C2185B"

# ── UI setup ──────────────────────────────────────────────────────────────────

custom_theme: Theme = Theme({
    "user":      f"bold {SAKURA_DEEP}",
    "assistant": f"bold {SAKURA}",
    "system":    f"dim {SAKURA_MUTED}",
    "error":     f"bold {SAKURA_DARK}",
    "info":      f"dim {SAKURA_MUTED}",
})

console: Console = Console(theme=custom_theme)

# ── Path helpers ────────────────────────────────────────────────────────────────

def _short_cwd(cwd: str) -> str:
    parts: list[str] = Path(cwd).parts
    if len(parts) <= 2:
        return cwd
    return os.path.join(parts[-2], parts[-1])


# ── Session persistence ─────────────────────────────────────────────────────────────

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


# ── Version control ────────────────────────────────────────────────────────────────

undo_stack: dict[str, list[Path]] = defaultdict(list)
redo_stack: dict[str, list[Path]] = defaultdict(list)


def _vc_dir_for(filepath: str) -> Path:
    safe: str      = re.sub(r"[^\w.\-]", "_", Path(filepath).name)
    full_safe: str = re.sub(r"[^\w.\-]", "_", str(Path(filepath).resolve()))
    slot: Path     = VC_DIR / (safe + "_" + full_safe[-32:])
    slot.mkdir(parents=True, exist_ok=True)
    return slot


def _index_path(vc_slot: Path) -> Path:
    return vc_slot / "index.json"


commit_log: dict[str, list[dict]] = defaultdict(list)


def _save_index(filepath: str) -> None:
    slot: Path = _vc_dir_for(filepath)
    data: dict = {
        "filepath": filepath,
        "undo":     [str(p) for p in undo_stack[filepath]],
        "redo":     [str(p) for p in redo_stack[filepath]],
        "commits":  commit_log[filepath],
    }
    _index_path(slot).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_index(filepath: str) -> None:
    slot: Path  = _vc_dir_for(filepath)
    index: Path = _index_path(slot)
    if not index.exists():
        return
    data: dict = json.loads(index.read_text(encoding="utf-8"))
    undo_stack[filepath]  = [Path(p) for p in data.get("undo", []) if Path(p).exists()]
    redo_stack[filepath]  = [Path(p) for p in data.get("redo", []) if Path(p).exists()]
    commit_log[filepath]  = [c for c in data.get("commits", []) if Path(c["snapshot"]).exists()]


# ── Named commit helpers ──────────────────────────────────────────────────────────

def do_commit(filepath: str, message: str) -> None:
    path: Path = Path(filepath)
    if not path.exists():
        console.print(f"[error]File not found: {filepath}[/error]")
        return
    content: str = path.read_text(encoding="utf-8")
    bak: Path    = _snapshot(filepath, content)
    ts: str      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _load_index(filepath)
    commit_log[filepath].append({"snapshot": str(bak), "message": message or "(no message)", "timestamp": ts})
    _save_index(filepath)
    idx: int = len(commit_log[filepath]) - 1
    console.print(Panel(
        f"[info]Committed [bold]{filepath}[/bold] as #{idx}\nMessage: {message or '(no message)'}\nSnapshot: {bak.name}[/info]",
        title="Commit", border_style=SAKURA_DEEP,
    ))


def show_log(filepath: str) -> None:
    _load_index(filepath)
    entries: list[dict] = commit_log[filepath]
    if not entries:
        console.print(f"[info]No commits for {filepath}.[/info]")
        return
    rows: list[str] = []
    for i, entry in enumerate(entries):
        marker: str = "HEAD" if i == len(entries) - 1 else "    "
        rows.append(f"  #{i:<3}  {marker}  {entry['timestamp']}  {entry['message']}")
    console.print(Panel("[info]" + "\n".join(rows) + "[/info]", title=f"Commit log: {filepath}", border_style=SAKURA))


def do_restore(filepath: str, idx_str: str) -> None:
    _load_index(filepath)
    entries: list[dict] = commit_log[filepath]
    if not entries:
        console.print(f"[error]No commits for {filepath}.[/error]")
        return
    try:
        idx: int = int(idx_str)
    except ValueError:
        console.print("[error]Index must be a number.[/error]")
        return
    if idx < 0 or idx >= len(entries):
        console.print(f"[error]Index {idx} out of range.[/error]")
        return
    snap_path: Path = Path(entries[idx]["snapshot"])
    if not snap_path.exists():
        console.print(f"[error]Snapshot file missing for commit #{idx}.[/error]")
        return
    path: Path = Path(filepath)
    if path.exists():
        undo_stack[filepath].append(_snapshot(filepath, path.read_text(encoding="utf-8")))
        redo_stack[filepath].clear()
    path.write_text(snap_path.read_text(encoding="utf-8"), encoding="utf-8")
    _save_index(filepath)
    console.print(Panel(
        f"[info]Restored [bold]{filepath}[/bold] to commit #{idx}\n"
        f"Message: {entries[idx]['message']}\nTimestamp: {entries[idx]['timestamp']}\n\nPrevious state saved — use /undo to go back.[/info]",
        title="Restore", border_style=SAKURA,
    ))


def _all_tracked_files() -> list[str]:
    result: list[str] = []
    if not VC_DIR.exists():
        return result
    for slot in sorted(VC_DIR.iterdir()):
        idx: Path = slot / "index.json"
        if idx.exists():
            try:
                data: dict = json.loads(idx.read_text(encoding="utf-8"))
                fp: str    = data.get("filepath", "")
                if fp:
                    result.append(fp)
            except Exception:
                pass
    return result


def _snapshot(filepath: str, content: str) -> Path:
    slot: Path = _vc_dir_for(filepath)
    ts: str    = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    bak: Path  = slot / f"{ts}.bak"
    bak.write_text(content, encoding="utf-8")
    return bak


def write_file_with_vc(filepath: str, new_content: str) -> None:
    path: Path       = Path(filepath)
    old_content: str = path.read_text(encoding="utf-8") if path.exists() else ""
    bak: Path        = _snapshot(filepath, old_content)
    undo_stack[filepath].append(bak)
    redo_stack[filepath].clear()
    _save_index(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_content, encoding="utf-8")
    lines: int = len(new_content.splitlines())
    console.print(Panel(
        f"[info]Wrote {lines} lines to [bold]{filepath}[/bold].\nPrevious version saved. Use /undo to revert.[/info]",
        title="File written", border_style=SAKURA_DEEP,
    ))


def do_undo(filepath: str) -> None:
    _load_index(filepath)
    if not undo_stack[filepath]:
        console.print(f"[error]Nothing to undo for {filepath}[/error]")
        return
    path: Path     = Path(filepath)
    current: str   = path.read_text(encoding="utf-8") if path.exists() else ""
    redo_stack[filepath].append(_snapshot(filepath, current))
    previous: str  = undo_stack[filepath].pop().read_text(encoding="utf-8")
    path.write_text(previous, encoding="utf-8")
    _save_index(filepath)
    console.print(Panel(
        f"[info]Reverted [bold]{filepath}[/bold] to previous version.\nUse /redo to reapply.[/info]",
        title="Undo", border_style=SAKURA,
    ))


def do_redo(filepath: str) -> None:
    _load_index(filepath)
    if not redo_stack[filepath]:
        console.print(f"[error]Nothing to redo for {filepath}[/error]")
        return
    path: Path     = Path(filepath)
    current: str   = path.read_text(encoding="utf-8") if path.exists() else ""
    undo_stack[filepath].append(_snapshot(filepath, current))
    next_ver: str  = redo_stack[filepath].pop().read_text(encoding="utf-8")
    path.write_text(next_ver, encoding="utf-8")
    _save_index(filepath)
    console.print(Panel(f"[info]Redid change on [bold]{filepath}[/bold].[/info]", title="Redo", border_style=SAKURA))


def show_file_history(filepath: str) -> None:
    _load_index(filepath)
    all_baks: list = list(enumerate(undo_stack[filepath])) + list(enumerate(redo_stack[filepath]))
    if not all_baks:
        console.print(f"[info]No history for {filepath}.[/info]")
        return
    rows: list[str] = []
    for i, bak in enumerate(undo_stack[filepath]):
        rows.append(f"  undo[{i}]  {bak.stem}  ({bak.stat().st_size} bytes)")
    for i, bak in enumerate(redo_stack[filepath]):
        rows.append(f"  redo[{i}]  {bak.stem}  ({bak.stat().st_size} bytes)")
    console.print(Panel("[info]" + "\n".join(rows) + "[/info]", title=f"History: {filepath}", border_style=SAKURA))


# ── General helpers ─────────────────────────────────────────────────────────────────

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


# ── Partial-write detection ──────────────────────────────────────────────────────────────

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


def _reply_has_partial_write(reply: str) -> bool:
    for match in _WRITE_PATTERN.finditer(reply):
        code: str = match.group("code")
        for pat in _PARTIAL_PATTERNS:
            if pat.search(code):
                return True
    return False


_WRITE_PATTERN: re.Pattern = re.compile(
    r"<!--\s*WRITE:\s*(?P<path>[^\s>]+)\s*-->\s*```(?:\w+)?\n(?P<code>.*?)```",
    re.DOTALL,
)


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


# ── Tab-completion ──────────────────────────────────────────────────────────────────

_SLASH_COMMANDS: list[str] = [
    "/cd", "/read", "/run", "/undo", "/redo", "/files",
    "/clear", "/check", "/stackview", "/history", "/help",
    "/commit", "/log", "/restore",
    "/quit", "/exit", "/q",
]

_CMD_SUBARGS: dict[str, list[str]] = {
    "/stackview": ["fh", "fhf", "sessions", "sess", "stack", "env", "environment", "help"],
    "/check": ["ALL"],
    "/read":  ["-a"],
}

_FILE_COMMANDS: set[str] = {"/read", "/check", "/undo", "/redo", "/files", "/cd", "/commit", "/log", "/restore"}


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


# ── Fuzzy hint completions ─────────────────────────────────────────────────────────

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


def _enable_windows_vt() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import ctypes.wintypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.GetStdHandle.restype = ctypes.wintypes.HANDLE
        ENABLE_VIRTUAL_TERMINAL_PROCESSING: int = 0x0004
        handle = kernel32.GetStdHandle(-11)
        mode   = ctypes.wintypes.DWORD()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        pass


def _inline_prompt(prompt_str: str, cwd: str, history: list[str]) -> str:
    """
    Character-by-character prompt with a fuzzy-hint line rendered ABOVE the
    input line.  On Enter the hint is cleared and only the prompt + input
    remain visible.

    Layout while typing:
        [hint line]   /cd ../Soda-Clicker  /cd ../Soda-Autoclicker  ...
        [prompt line] you (dir): /cd ../S_

    On Windows we use Win32 SetConsoleCursorPosition with correctly-typed
    HANDLE (64-bit safe) so the cursor can reliably jump between the two
    rows.  On Unix we use ANSI escape sequences.
    """
    _enable_windows_vt()

    BOLD: str  = "\033[1m"
    DIM: str   = "\033[2m"
    CYAN: str  = f"\033[38;2;{int(SAKURA_DEEP[1:3],16)};{int(SAKURA_DEEP[3:5],16)};{int(SAKURA_DEEP[5:7],16)}m"
    RESET: str = "\033[0m"

    buf: list[str]       = []
    hist_idx: int        = len(history)
    saved_buf: list[str] = []

    def _text() -> str:
        return "".join(buf)

    def _hint_line(text: str) -> str:
        matches: list[str] = _get_fuzzy_completions(text, cwd)
        if not matches:
            return ""
        parts_h: list[str] = []
        for i, m in enumerate(matches[:7]):
            parts_h.append(f"{BOLD}{CYAN}{m}{RESET}" if i == 0 else f"{DIM}{m}{RESET}")
        return "  ".join(parts_h)

    def _tab_complete(text: str) -> str:
        matches: list[str] = _get_fuzzy_completions(text, cwd)
        if not matches:
            return text
        best: str = matches[0]
        return best + " " if best in _SLASH_COMMANDS else best

    # ── Platform-specific render ─────────────────────────────────────────────

    if sys.platform == "win32":
        # ──────────────────────────────────────────────────────────────────────
        # Win32 cursor positioning.
        #
        # IMPORTANT: on 64-bit Windows, GetStdHandle returns a 64-bit HANDLE.
        # Without setting restype = HANDLE, ctypes truncates it to 32 bits,
        # which makes SetConsoleCursorPosition fail silently and the hint+prompt
        # end up on the same line.  We fix this by declaring proper argtypes and
        # restype for every Win32 call we make.
        # ──────────────────────────────────────────────────────────────────────
        import ctypes        # type: ignore[import]
        import ctypes.wintypes  # type: ignore[import]

        # ---- type definitions ----
        class _COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class _SMALL_RECT(ctypes.Structure):
            _fields_ = [
                ("Left",   ctypes.c_short), ("Top",    ctypes.c_short),
                ("Right",  ctypes.c_short), ("Bottom", ctypes.c_short),
            ]

        class _CSBI(ctypes.Structure):
            _fields_ = [
                ("dwSize",              _COORD),
                ("dwCursorPosition",    _COORD),
                ("wAttributes",         ctypes.c_ushort),
                ("srWindow",            _SMALL_RECT),
                ("dwMaximumWindowSize", _COORD),
            ]

        _k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        # ---- declare types so 64-bit HANDLE is not truncated ----
        _k32.GetStdHandle.restype  = ctypes.wintypes.HANDLE
        _k32.GetStdHandle.argtypes = [ctypes.wintypes.DWORD]

        _k32.GetConsoleScreenBufferInfo.restype  = ctypes.wintypes.BOOL
        _k32.GetConsoleScreenBufferInfo.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.POINTER(_CSBI),
        ]

        _k32.SetConsoleCursorPosition.restype  = ctypes.wintypes.BOOL
        _k32.SetConsoleCursorPosition.argtypes = [
            ctypes.wintypes.HANDLE,
            _COORD,   # passed by value
        ]

        _hout = _k32.GetStdHandle(ctypes.wintypes.DWORD(-11))  # STD_OUTPUT_HANDLE

        def _cur_y() -> int:
            csbi = _CSBI()
            _k32.GetConsoleScreenBufferInfo(_hout, ctypes.byref(csbi))
            return int(csbi.dwCursorPosition.Y)

        def _goto(x: int, y: int) -> None:
            coord = _COORD(x, y)
            _k32.SetConsoleCursorPosition(_hout, coord)

        # Print blank hint line then prompt; record the row numbers.
        sys.stdout.write("\n" + prompt_str)
        sys.stdout.flush()
        _prompt_y: int     = _cur_y()
        _hint_y: list[int] = [_prompt_y - 1]  # row directly above prompt

        def _render(text: str) -> None:
            hint: str = _hint_line(text)
            # Rewrite hint row
            _goto(0, _hint_y[0])
            sys.stdout.write(hint + "\033[K")   # \033[K = erase to EOL
            # Rewrite prompt row
            _goto(0, _hint_y[0] + 1)
            sys.stdout.write(prompt_str + text + "\033[K")
            sys.stdout.flush()

        def _clear_hint() -> None:
            """Erase the hint row and move cursor to start of prompt row."""
            _goto(0, _hint_y[0])
            sys.stdout.write("\033[K")
            _goto(0, _hint_y[0] + 1)
            sys.stdout.flush()

    else:
        # ── Unix: ANSI escape sequences ─────────────────────────────────────
        prev_hint_lines: list[int] = [1]
        sys.stdout.write(f"\n{prompt_str}")
        sys.stdout.flush()

        def _render(text: str) -> None:  # type: ignore[misc]
            hint: str           = _hint_line(text)
            term_width: int     = console.width or 80
            hint_plain: str     = re.sub(r"\033\[[^m]*m", "", hint)
            new_hint_lines: int = max(1, -(-len(hint_plain) // term_width)) if hint_plain else 1
            clear_seq: str      = "\033[1A\033[2K" * prev_hint_lines[0]
            prev_hint_lines[0]  = new_hint_lines
            sys.stdout.write(f"{clear_seq}{hint}\n\033[2K{prompt_str}{text}")
            sys.stdout.flush()

        def _clear_hint() -> None:  # type: ignore[misc]
            # Go up to hint line and erase it, leave cursor at start of prompt.
            sys.stdout.write("\033[1A\033[2K")
            sys.stdout.flush()

    # ── Input loop ────────────────────────────────────────────────────────────

    try:
        if sys.platform == "win32":
            import msvcrt

            while True:
                ch: str = msvcrt.getwch()  # type: ignore[attr-defined]

                if ch in ("\x00", "\xe0"):
                    ch2: str = msvcrt.getwch()  # type: ignore[attr-defined]
                    if ch2 == "H":   # up arrow
                        if hist_idx > 0:
                            if hist_idx == len(history):
                                saved_buf = buf[:]
                            hist_idx -= 1
                            buf[:] = list(history[hist_idx])
                    elif ch2 == "P": # down arrow
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


# ── /check helpers ─────────────────────────────────────────────────────────────────

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
        parts: list[str] = []
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
            parts.append(f"# ── {rel} ──\n{content}")
            total_chars += len(content)
        if skipped:
            console.print(f"[info]Skipped (too large): {', '.join(skipped[:5])}{' ...' if len(skipped) > 5 else ''}[/info]")
        combined: str   = "\n\n".join(parts)
        file_count: int = len(parts)
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


# ── /stackview ───────────────────────────────────────────────────────────────────────

_SV_TYPES: dict[str, str] = {
    "fh":       "File history  (current project)",
    "fhf":      "File history full (all projects)",
    "sessions": "Saved sessions",
    "stack":    "Undo/redo stack sizes",
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
        _load_index(fp)
        rows.append(f"  {os.path.relpath(fp, cwd):<40}  ({'exists' if Path(fp).exists() else 'missing':<7})  undo={len(undo_stack[fp])}  redo={len(redo_stack[fp])}")
    console.print(Panel("[info]" + "\n".join(rows) + "[/info]", title=f"File history  [{_short_cwd(cwd)}]", border_style=SAKURA))


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
            _load_index(fp)
            rows.append(f"    {Path(fp).name:<36}  ({'exists' if Path(fp).exists() else 'missing':<7})  undo={len(undo_stack[fp])}  redo={len(redo_stack[fp])}")
    console.print(Panel("[info]" + "\n".join(rows) + "[/info]", title="File history (all projects)", border_style=SAKURA))


def _sv_sessions() -> None:
    if not SESSION_DIR.exists() or not list(SESSION_DIR.glob("*.json")):
        console.print("[info]No sessions saved yet.[/info]")
        return
    rows: list[str] = []
    for sf in sorted(SESSION_DIR.glob("*.json")):
        try:
            data: dict = json.loads(sf.read_text(encoding="utf-8"))
            rows.append(f"  {data.get('cwd','?'):<45}  {data.get('saved_at','?')[:19].replace('T','  ')}  {len(data.get('messages',[])):>3} msg  {sf.stat().st_size:>6} B")
        except Exception:
            rows.append(f"  {sf.name}  (unreadable)")
    console.print(Panel("[info]" + "\n".join(rows) + "[/info]", title=f"Saved sessions  ({SESSION_DIR})", border_style=SAKURA))


def _sv_stack() -> None:
    tracked: list[str] = _all_tracked_files()
    if not tracked:
        console.print("[info]No tracked files.[/info]")
        return
    rows: list[str] = []
    total_baks: int  = 0
    total_bytes: int = 0
    for fp in tracked:
        _load_index(fp)
        u: int = len(undo_stack[fp])
        r: int = len(redo_stack[fp])
        bb: int = sum(b.stat().st_size for b in undo_stack[fp] + redo_stack[fp] if b.exists())
        total_baks  += u + r
        total_bytes += bb
        rows.append(f"  {fp:<50}  undo={u:<3}  redo={r:<3}  {bb:>8} B stored")
    rows.append(f"\n  Total: {total_baks} snapshots,  {total_bytes:,} bytes  in {VC_DIR}")
    console.print(Panel("[info]" + "\n".join(rows) + "[/info]", title="Undo/redo stack", border_style=SAKURA))


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
    console.print(Panel("[info]" + "\n".join(rows) + "[/info]", title="Environment", border_style=SAKURA_DEEP))


def handle_stackview(sv_type: str, cwd: str, messages: list[dict]) -> None:
    t: str = sv_type.strip().lower()
    if t == "fh":                    _sv_fh(cwd)
    elif t == "fhf":                 _sv_fhf()
    elif t in ("sessions", "sess"): _sv_sessions()
    elif t == "stack":               _sv_stack()
    elif t in ("env", "environment"): _sv_env(cwd, messages)
    elif t in ("", "help"):
        rows: list[str] = [f"  {k:<12}  {v}" for k, v in _SV_TYPES.items()]
        rows += ["  sess        Alias for 'sessions'", "  environment Alias for 'env'"]
        console.print(Panel("[info]" + "\n".join(rows) + "[/info]", title="/stackview types", border_style=SAKURA_DEEP))
    else:
        console.print(f"[error]Unknown stackview type: '{t}'. Run /stackview help.[/error]")


# ── Slash commands ───────────────────────────────────────────────────────────────────

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
                for sf in sorted(all_files):
                    rel: str = os.path.relpath(str(sf), cwd)
                    try:
                        fc: str = sf.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        skipped.append(rel); continue
                    if total_chars + len(fc) > 400_000:
                        skipped.append(rel); continue
                    snippets.append(f"### {rel}\n```\n{fc}\n```")
                    total_chars += len(fc)
                if skipped:
                    console.print("[info]Skipped: " + ", ".join(skipped[:5]) + (" ..." if len(skipped) > 5 else "") + "[/info]")
                if snippets:
                    state.setdefault("pending_context", []).append(
                        f"Here are all {len(snippets)} file(s) from `{cwd}`:\n\n" + "\n\n".join(snippets)
                    )
                    console.print(f"[info]Loaded {len(snippets)} file(s) into context.[/info]")
                else:
                    console.print("[info]No readable files found.[/info]")
        else:
            resolved: str = resolve_path(arg, cwd)
            content: str  = read_file(resolved)
            state.setdefault("pending_context", []).append(f"Here is the content of `{resolved}`:\n\n```\n{content}\n```")
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
            candidates: list[str] = [fp for fp in tracked if (_load_index(fp) or True) and undo_stack[fp]]
            if not candidates:       console.print("[info]No undo history.[/info]")
            elif len(candidates) == 1: do_undo(candidates[0])
            else:
                console.print("[info]Multiple files have undo history. Specify one:[/info]")
                for fp in candidates: console.print(f"[info]  /undo {fp}[/info]")
        else:
            do_undo(arg)

    elif name == "/redo":
        if not arg:
            tracked = _all_tracked_files()
            candidates = [fp for fp in tracked if (_load_index(fp) or True) and redo_stack[fp]]
            if not candidates:       console.print("[info]No redo history.[/info]")
            elif len(candidates) == 1: do_redo(candidates[0])
            else:
                console.print("[info]Multiple files have redo history. Specify one:[/info]")
                for fp in candidates: console.print(f"[info]  /redo {fp}[/info]")
        else:
            do_redo(arg)

    elif name == "/files":
        if arg:
            show_file_history(arg)
        else:
            tracked = _all_tracked_files()
            if not tracked:
                console.print("[info]No tracked files.[/info]")
            else:
                rows: list[str] = []
                for fp in tracked:
                    _load_index(fp)
                    rows.append(f"  {fp}  ({'exists' if Path(fp).exists() else 'missing'})  undo={len(undo_stack[fp])}  redo={len(redo_stack[fp])}")
                console.print(Panel("[info]" + "\n".join(rows) + "[/info]", title="Tracked files", border_style=SAKURA))

    elif name == "/check":
        if not arg: console.print("[error]Usage: /check ALL | /check <file> | /check <file>:<function>[/error]")
        else:       handle_check(arg, messages, state)

    elif name == "/stackview":
        handle_stackview(arg, cwd, messages)

    elif name == "/commit":
        if not arg: console.print("[error]Usage: /commit <file> [message][/error]")
        else:
            tokens: list[str] = arg.split(maxsplit=1)
            do_commit(resolve_path(tokens[0], cwd), tokens[1] if len(tokens) > 1 else "")

    elif name == "/log":
        if not arg: console.print("[error]Usage: /log <file>[/error]")
        else:       show_log(resolve_path(arg.split()[0], cwd))

    elif name == "/restore":
        if not arg: console.print("[error]Usage: /restore <file> <commit-index>[/error]")
        else:
            rt: list[str] = arg.split(maxsplit=1)
            if len(rt) < 2: console.print("[error]Usage: /restore <file> <commit-index>[/error]")
            else:           do_restore(resolve_path(rt[0], cwd), rt[1].strip())

    elif name == "/history":
        for i, m in enumerate(messages):
            console.print(f"[info][{i}] {m['role']}: {m['content'][:120].replace(chr(10), ' ')}[/info]")

    elif name == "/help":
        console.print(Panel(textwrap.dedent("""\
            Available commands:
              /cd [dir]         - change working directory (no arg = show current)
              /read <file>      - load a file into the conversation context
              /read -a          - load ALL files in the tree recursively (up to 400 KB)
              /run <cmd>        - run a shell command and add output to context
              /undo [file]      - revert the last AI-written file edit
              /redo [file]      - re-apply a reverted edit
              /files [file]     - list tracked files, or show history for one file
              /clear            - clear conversation history
              /check <target>   - AI code review
                  ALL           review all source files in cwd
                  file.py       review a single file
                  file.py:func  review a single function
              /stackview <type> - view stacks/sessions/env info
              /commit <file> [msg] - tag the current file state with a message
              /log <file>       - show named commit log for a file
              /restore <file> <#> - restore file to a specific commit index
              /history          - show message history
              /help             - show this help
              /quit             - exit
        """), title="Help", border_style=SAKURA_DEEP))

    else:
        console.print(f"[error]Unknown command: {name}. Type /help for a list.[/error]")

    return True


# ── Response rendering ────────────────────────────────────────────────────────────────

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


# ── Streaming ────────────────────────────────────────────────────────────────────

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


def _raw_stream(messages: list[dict], cancel_event: threading.Event | None = None) -> tuple[str, int]:
    full: str       = ""
    term_width: int = console.width or 80
    phys_lines: int = 1
    col: int        = 0
    try:
        for chunk in ollama.chat(model=MODEL, messages=messages, stream=True):
            if cancel_event and cancel_event.is_set():
                break
            token: str = chunk["message"]["content"]
            full += token
            for ch in token:
                if ch == "\n":  phys_lines += 1; col = 0
                else:
                    col += 1
                    if col >= term_width: phys_lines += 1; col = 0
            sys.stdout.write(token)
            sys.stdout.flush()
        sys.stdout.write("\n")
        sys.stdout.flush()
    except Exception as exc:
        if not (cancel_event and cancel_event.is_set()):
            console.print(f"[error]Ollama error: {exc}[/error]")
            console.print(f"[info]  ollama pull {MODEL}[/info]")
    return full, phys_lines


def stream_response(messages: list[dict], cwd: str = "") -> str:
    cancel_event: threading.Event = threading.Event()
    console.print()
    console.print(_status_line("thinking...", "ctrl+d to cancel"))
    watcher: threading.Thread = threading.Thread(target=_watch_for_cancel, args=(cancel_event,), daemon=True)
    watcher.start()
    full_reply, phys_lines = _raw_stream(messages, cancel_event)
    cancel_event.set()
    watcher.join(timeout=0.5)

    if not full_reply:
        if cancel_event.is_set(): console.print("[info]Cancelled.[/info]")
        return full_reply

    if _reply_has_partial_write(full_reply):
        console.print(Panel("[info]Partial file detected. Reprompting...[/info]", title="Partial write", border_style=SAKURA_DARK))
        messages.append({"role": "assistant", "content": full_reply})
        messages.append({"role": "user",      "content": _PARTIAL_REPROMPT})
        rc: threading.Event = threading.Event()
        console.print()
        console.print(_status_line("retrying...", "ctrl+d to cancel"))
        rw: threading.Thread = threading.Thread(target=_watch_for_cancel, args=(rc,), daemon=True)
        rw.start()
        rr, rl = _raw_stream(messages, rc)
        rc.set(); rw.join(timeout=0.5)
        messages.pop(); messages.pop()
        if rr: full_reply = rr; phys_lines = rl

    sys.stdout.write(f"\033[{phys_lines}A\033[J")
    sys.stdout.flush()
    render_response(full_reply)
    apply_file_writes(full_reply)
    apply_command_runs(full_reply, cwd, messages)
    return full_reply


# ── Main loop ─────────────────────────────────────────────────────────────────

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
