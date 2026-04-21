#!/usr/bin/env python3
"""
qwen3-code: A simple Claude Code-style TUI powered by Ollama + huihui_ai/qwen3-coder-abliterated:30b
"""

import json
import os
import re
import subprocess
import sys
import textwrap
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

# ── Constants ──────────────────────────────────────────────────────────────────

MODEL: str   = "huihui_ai/qwen3-coder-abliterated:30b"
VC_DIR: Path = Path.home() / ".local" / "share" / "qwen3-code" / "vc"

SYSTEM_PROMPT: str = textwrap.dedent("""\
    You are an expert software engineer assistant embedded in a terminal.
    You help the user understand, write, debug, and refactor code.
    When showing code, always wrap it in fenced code blocks with the correct language tag.
    Be concise and direct. Prefer targeted, minimal changes.
    If asked to run a shell command, explain what it does first.

    FILE EDITING - when the user asks you to edit or rewrite a file, respond with
    the complete new file content using this EXACT format (the marker line is
    required so the tool can auto-save it):

    <!-- WRITE: path/to/file -->

    Always provide the full file, never partial diffs. The tool will back up the
    original before writing so the user can /undo at any time.
""").strip()

# ── Sakura pink palette ────────────────────────────────────────────────────────────

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

# ── Version control ────────────────────────────────────────────────────────────────
#
# Each tracked file gets a subdirectory under VC_DIR:
#   ~/.local/share/qwen3-code/vc/<safe_name>/
#       index.json          -- {"undo": ["ts1.bak", ...], "redo": [...]}
#       <timestamp>.bak     -- snapshot of file content at that point
#
# The in-memory stacks store Path objects pointing to .bak files so that
# history survives across sessions.

undo_stack: dict[str, list[Path]] = defaultdict(list)
redo_stack: dict[str, list[Path]] = defaultdict(list)


def _vc_dir_for(filepath: str) -> Path:
    """Return (and create) the VC subdirectory for the given filepath."""
    safe: str      = re.sub(r"[^\w.\-]", "_", Path(filepath).name)
    full_safe: str = re.sub(r"[^\w.\-]", "_", str(Path(filepath).resolve()))
    slot: Path     = VC_DIR / (safe + "_" + full_safe[-32:])
    slot.mkdir(parents=True, exist_ok=True)
    return slot


def _index_path(vc_slot: Path) -> Path:
    return vc_slot / "index.json"


def _save_index(filepath: str) -> None:
    """Persist the current undo/redo stacks for filepath to index.json."""
    slot: Path = _vc_dir_for(filepath)
    data: dict = {
        "filepath": filepath,
        "undo":     [str(p) for p in undo_stack[filepath]],
        "redo":     [str(p) for p in redo_stack[filepath]],
    }
    _index_path(slot).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_index(filepath: str) -> None:
    """Load persisted undo/redo stacks for filepath from index.json."""
    slot: Path       = _vc_dir_for(filepath)
    index: Path      = _index_path(slot)

    if not index.exists():
        return

    data: dict = json.loads(index.read_text(encoding="utf-8"))
    undo_stack[filepath] = [Path(p) for p in data.get("undo", []) if Path(p).exists()]
    redo_stack[filepath] = [Path(p) for p in data.get("redo", []) if Path(p).exists()]


def _all_tracked_files() -> list[str]:
    """Return list of filepaths that have a persisted index in VC_DIR."""
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
    """Write content to a timestamped .bak file and return its Path."""
    slot: Path      = _vc_dir_for(filepath)
    ts: str         = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    bak: Path       = slot / f"{ts}.bak"
    bak.write_text(content, encoding="utf-8")
    return bak


def write_file_with_vc(filepath: str, new_content: str) -> None:
    """
    Write new_content to filepath.
    Pushes the old version onto the undo stack, clears redo, and persists the index.
    """
    path: Path       = Path(filepath)
    old_content: str = path.read_text(encoding="utf-8") if path.exists() else ""

    bak: Path = _snapshot(filepath, old_content)
    undo_stack[filepath].append(bak)
    redo_stack[filepath].clear()
    _save_index(filepath)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_content, encoding="utf-8")

    lines: int = len(new_content.splitlines())
    console.print(Panel(
        f"[info]Wrote {lines} lines to [bold]{filepath}[/bold].\n"
        f"Previous version saved. Use /undo to revert.[/info]",
        title="File written",
        border_style=SAKURA_DEEP,
    ))


def do_undo(filepath: str) -> None:
    """Restore the previous version of filepath from the undo stack."""
    _load_index(filepath)

    if not undo_stack[filepath]:
        console.print(f"[error]Nothing to undo for {filepath}[/error]")
        return

    path: Path       = Path(filepath)
    current: str     = path.read_text(encoding="utf-8") if path.exists() else ""
    redo_bak: Path   = _snapshot(filepath, current)
    redo_stack[filepath].append(redo_bak)

    undo_bak: Path   = undo_stack[filepath].pop()
    previous: str    = undo_bak.read_text(encoding="utf-8")
    path.write_text(previous, encoding="utf-8")
    _save_index(filepath)

    console.print(Panel(
        f"[info]Reverted [bold]{filepath}[/bold] to previous version.\n"
        f"Use /redo to reapply, or /undo again to go further back.[/info]",
        title="Undo",
        border_style=SAKURA,
    ))


def do_redo(filepath: str) -> None:
    """Re-apply the most recently undone version of filepath."""
    _load_index(filepath)

    if not redo_stack[filepath]:
        console.print(f"[error]Nothing to redo for {filepath}[/error]")
        return

    path: Path       = Path(filepath)
    current: str     = path.read_text(encoding="utf-8") if path.exists() else ""
    undo_bak: Path   = _snapshot(filepath, current)
    undo_stack[filepath].append(undo_bak)

    redo_bak: Path   = redo_stack[filepath].pop()
    next_ver: str    = redo_bak.read_text(encoding="utf-8")
    path.write_text(next_ver, encoding="utf-8")
    _save_index(filepath)

    console.print(Panel(
        f"[info]Redid change on [bold]{filepath}[/bold].[/info]",
        title="Redo",
        border_style=SAKURA,
    ))


def show_file_history(filepath: str) -> None:
    """Print the version history for a tracked file."""
    _load_index(filepath)

    undo_baks: list[Path] = undo_stack[filepath]
    redo_baks: list[Path] = redo_stack[filepath]

    if not undo_baks and not redo_baks:
        console.print(f"[info]No history for {filepath}.[/info]")
        return

    rows: list[str] = []
    for i, bak in enumerate(undo_baks):
        ts: str   = bak.stem.replace("_", " ", 2)  # YYYYMMDD HH:MM:SS us
        size: int = bak.stat().st_size
        rows.append(f"  undo[{i}]  {ts}  ({size} bytes)  {bak.name}")

    for i, bak in enumerate(redo_baks):
        ts = bak.stem.replace("_", " ", 2)
        size = bak.stat().st_size
        rows.append(f"  redo[{i}]  {ts}  ({size} bytes)  {bak.name}")

    body: str = "\n".join(rows)
    console.print(Panel(
        f"[info]{body}[/info]",
        title=f"History: {filepath}",
        border_style=SAKURA,
    ))


# ── General helpers ─────────────────────────────────────────────────────────────────

def read_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception as exc:
        return f"[could not read file: {exc}]"


def run_command(cmd: str) -> str:
    try:
        result: subprocess.CompletedProcess = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output: str = result.stdout + result.stderr
        return output.strip() or "(no output)"
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

    lines: list[str] = [
        f"Working directory: {cwd}",
        f"Visible files: {', '.join(files) if files else 'none'}",
    ]
    return "\n".join(lines)


def resolve_path(arg: str, cwd: str) -> str:
    """Resolve arg relative to cwd if it is not already absolute."""
    p: Path = Path(arg)
    if p.is_absolute():
        return str(p)
    return str(Path(cwd) / p)


# ── WRITE marker detection ────────────────────────────────────────────────────────────

_WRITE_PATTERN: re.Pattern = re.compile(
    r"<!--\s*WRITE:\s*(?P<path>[^\s>]+)\s*-->\s*```(?:\w+)?\n(?P<code>.*?)```",
    re.DOTALL,
)


def apply_file_writes(reply: str) -> None:
    for match in _WRITE_PATTERN.finditer(reply):
        filepath: str = match.group("path").strip()
        code: str     = match.group("code")
        write_file_with_vc(filepath, code)


# ── Slash commands ───────────────────────────────────────────────────────────────────

def handle_slash_command(cmd: str, messages: list[dict], state: dict) -> bool:
    """
    Handle /commands typed by the user.
    state is a mutable dict with at least {"cwd": str}.
    Returns True if the main loop should continue, False to quit.
    """
    parts: list[str] = cmd.strip().split(maxsplit=1)
    name: str        = parts[0].lower()
    arg: str         = parts[1] if len(parts) > 1 else ""

    cwd: str = state["cwd"]

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
                    state["cwd"] = str(target)
                    os.chdir(target)
                    # List files in new dir
                    try:
                        entries: list[str] = [
                            e.name for e in target.iterdir()
                            if not e.name.startswith(".")
                        ][:30]
                    except Exception:
                        entries = []
                    console.print(Panel(
                        f"[info]Changed to: [bold]{target}[/bold]\n"
                        f"Contents: {', '.join(entries) if entries else '(empty)'}[/info]",
                        title="cd",
                        border_style=SAKURA,
                    ))
            except FileNotFoundError:
                console.print(f"[error]Directory not found: {arg}[/error]")

    elif name == "/clear":
        messages.clear()
        console.clear()
        console.print("[info]Conversation cleared.[/info]")

    elif name == "/read":
        if not arg:
            console.print("[error]Usage: /read <filepath>[/error]")
        else:
            resolved: str = resolve_path(arg, cwd)
            content: str  = read_file(resolved)
            snippet: str  = f"Here is the content of `{resolved}`:\n\n```\n{content}\n```"
            messages.append({"role": "user", "content": snippet})
            console.print(f"[info]Loaded {resolved} into context.[/info]")

    elif name == "/run":
        if not arg:
            console.print("[error]Usage: /run <shell command>[/error]")
        else:
            output: str  = run_command(arg)
            snippet: str = f"Output of `{arg}`:\n\n```\n{output}\n```"
            messages.append({"role": "user", "content": snippet})
            console.print(Panel(output, title=f"$ {arg}", border_style=SAKURA_MUTED))

    elif name == "/undo":
        if not arg:
            tracked: list[str] = _all_tracked_files()
            candidates: list[str] = []
            for fp in tracked:
                _load_index(fp)
                if undo_stack[fp]:
                    candidates.append(fp)

            if not candidates:
                console.print("[info]No undo history.[/info]")
            elif len(candidates) == 1:
                do_undo(candidates[0])
            else:
                console.print("[info]Multiple files have undo history. Specify one:[/info]")
                for fp in candidates:
                    console.print(f"[info]  /undo {fp}[/info]")
        else:
            do_undo(arg)

    elif name == "/redo":
        if not arg:
            tracked = _all_tracked_files()
            candidates = []
            for fp in tracked:
                _load_index(fp)
                if redo_stack[fp]:
                    candidates.append(fp)

            if not candidates:
                console.print("[info]No redo history.[/info]")
            elif len(candidates) == 1:
                do_redo(candidates[0])
            else:
                console.print("[info]Multiple files have redo history. Specify one:[/info]")
                for fp in candidates:
                    console.print(f"[info]  /redo {fp}[/info]")
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
                    u: int = len(undo_stack[fp])
                    r: int = len(redo_stack[fp])
                    exists: str = "exists" if Path(fp).exists() else "missing"
                    rows.append(f"  {fp}  ({exists})  undo={u}  redo={r}")

                console.print(Panel(
                    "[info]" + "\n".join(rows) + "[/info]",
                    title="Tracked files",
                    border_style=SAKURA,
                ))

    elif name == "/history":
        for i, m in enumerate(messages):
            role: str    = m["role"]
            preview: str = m["content"][:120].replace("\n", " ")
            console.print(f"[info][{i}] {role}: {preview}[/info]")

    elif name == "/help":
        help_text: str = textwrap.dedent("""\
            Available commands:
              /cd [dir]         - change working directory (no arg = show current)
              /read <file>      - load a file into the conversation context
              /run <cmd>        - run a shell command and add output to context
              /undo [file]      - revert the last AI-written file edit
              /redo [file]      - re-apply a reverted edit
              /files [file]     - list tracked files, or show history for one file
              /clear            - clear conversation history
              /history          - show message history
              /help             - show this help
              /quit             - exit

            /read and WRITE_FILE paths are resolved relative to the current
            working directory set by /cd.
            Version history is persisted to ~/.local/share/qwen3-code/vc/
            and survives across sessions.
        """)
        console.print(Panel(help_text, title="Help", border_style=SAKURA_DEEP))

    else:
        console.print(f"[error]Unknown command: {name}. Type /help for a list.[/error]")

    return True


# ── Rendering ─────────────────────────────────────────────────────────────────────

def render_markdown_with_code_blocks(text: str) -> None:
    pattern: str     = r"(```(?:\w+)?\n.*?```)"
    parts: list[str] = re.split(pattern, text, flags=re.DOTALL)

    for part in parts:
        if not part:
            continue

        if part.startswith("```") and part.endswith("```"):
            match = re.match(r"```(\w+)?\n(.*?)```", part, re.DOTALL)
            if match:
                lang: str = match.group(1) or "text"
                code: str = match.group(2)

                block_start: int = text.find(part)
                prefix: str      = text[max(0, block_start - 120): block_start]
                write_match      = re.search(r"<!--\s*WRITE:\s*([^\s>]+)\s*-->", prefix)

                if write_match:
                    fp: str     = write_match.group(1)
                    title: str  = f"Written to {fp}"
                    border: str = SAKURA_DEEP
                else:
                    title  = f"Code ({lang})"
                    border = SAKURA

                console.print(Panel(
                    Syntax(code, lang, theme="dracula", line_numbers=True),
                    title=title,
                    border_style=border,
                ))
        else:
            cleaned: str = re.sub(r"<!--\s*WRITE:[^>]+-->", "", part).strip()
            if cleaned:
                console.print(Markdown(cleaned))


# ── Streaming ────────────────────────────────────────────────────────────────────

def stream_response(messages: list[dict]) -> str:
    full_reply: str = ""

    console.print()
    console.print(Text("assistant", style="assistant"), end="  ")
    console.print(Text("thinking...", style="dim"))

    try:
        stream = ollama.chat(
            model=MODEL,
            messages=messages,
            stream=True,
        )

        for chunk in stream:
            delta: str   = chunk["message"]["content"]
            full_reply  += delta

    except Exception as exc:
        console.print(f"[error]Ollama error: {exc}[/error]")
        console.print("[info]Make sure Ollama is running and the model is pulled:[/info]")
        console.print(f"[info]  ollama pull {MODEL}[/info]")
        return full_reply

    apply_file_writes(full_reply)

    if full_reply:
        render_markdown_with_code_blocks(full_reply)

    return full_reply


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    VC_DIR.mkdir(parents=True, exist_ok=True)

    # Mutable session state — passed into handle_slash_command so /cd can update it.
    state: dict = {"cwd": os.getcwd()}

    console.print(Panel(
        f"[bold {SAKURA_DEEP}]qwen3-code[/bold {SAKURA_DEEP}]  -  simple coding assistant TUI\n"
        f"Model : [{SAKURA}]{MODEL}[/{SAKURA}]\n"
        f"CWD   : [{SAKURA}]{state['cwd']}[/{SAKURA}]\n\n"
        f"Type [{SAKURA_DEEP}]/help[/{SAKURA_DEEP}] for commands, "
        f"[{SAKURA_DEEP}]/quit[/{SAKURA_DEEP}] to exit.",
        border_style=SAKURA,
        title="qwen3-code",
    ))

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

    first_message: bool = True

    while True:
        cwd: str = state["cwd"]
        prompt_label: str = f"[bold {SAKURA_DEEP}]you ({cwd})[/bold {SAKURA_DEEP}]"

        try:
            user_input: str = Prompt.ask(prompt_label)
        except (KeyboardInterrupt, EOFError):
            console.print("\n[info]Goodbye.[/info]")
            break

        user_input = user_input.strip()

        if not user_input:
            continue

        if user_input.startswith("/"):
            if not handle_slash_command(user_input, messages, state):
                break
            continue

        if first_message:
            context: str  = build_context_snippet(cwd)
            content: str  = f"{context}\n\n{user_input}"
            first_message = False
        else:
            content = user_input

        messages.append({"role": "user", "content": content})

        reply: str = stream_response(messages)

        if reply:
            messages.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    main()