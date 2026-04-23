"""Shared constants, filesystem helpers, and the animated spinner."""

import contextlib
import math
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Generator

# ---------------------------------------------------------------------------
# Runtime constants
# ---------------------------------------------------------------------------

VC_DIR: Path      = Path.home() / ".local" / "share" / "qwen3-code" / "vc"
SESSION_DIR: Path = Path.home() / ".local" / "share" / "qwen3-code" / "sessions"
STREAM_MAX_LINES: int        = 10
SIZE_REDUCTION_THRESHOLD: float = 0.20

# Directories skipped during /read -a  (content not read, but their presence
# is noted in the context so the AI knows they exist).
IGNORED_DIRS: set[str] = {
    ".venv", "venv", ".env", "env",
    "node_modules", "__pycache__", ".git", ".hg", ".svn",
    "dist", "build", ".next", ".nuxt", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "coverage", ".eggs",
}

SYSTEM_PROMPT: str = textwrap.dedent("""\
    You are an expert software engineer assistant embedded in a terminal.
    You help the user understand, write, debug, and refactor code.
    When showing code, always wrap it in fenced code blocks with the correct language tag.
    Be concise and direct. Prefer targeted, minimal changes.
    If asked to run a shell command, explain what it does first.

    RUNNING COMMANDS - when you want to run a shell command as part of helping
    the user, emit it using this EXACT marker format:

    <!-- RUN: <shell command here> -->

    The tool will confirm with the user before running. Only emit one RUN marker
    per response. Never emit a RUN marker for commands the user did not request.

    FILE EDITING - when the user asks you to edit or rewrite a file, respond with
    the complete new file content using this EXACT format:

    <!-- WRITE: path/to/file -->

    Always provide the COMPLETE file from top to bottom. The tool will back up
    the original so the user can /undo at any time.
""").strip()

PARTIAL_REPROMPT: str = (
    "Your last response contained a partial file (truncation markers like "
    "\"...\", \"# rest of\", or similar). "
    "Provide the COMPLETE file using the <!-- WRITE: path --> format."
)

# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

def _phys_rows(text: str, term_w: int) -> int:
    """Physical terminal rows a plain-text string occupies."""
    if term_w <= 0 or not text:
        return 1
    return max(1, math.ceil(len(text) / term_w))


def _short_cwd(cwd: str) -> str:
    parts = Path(cwd).parts
    if len(parts) <= 2:
        return cwd
    return os.path.join(parts[-2], parts[-1])


def resolve_path(arg: str, cwd: str) -> str:
    p = Path(arg)
    return str(p) if p.is_absolute() else str(Path(cwd) / p)


# ---------------------------------------------------------------------------
# File / process helpers
# ---------------------------------------------------------------------------

def read_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception as exc:
        return f"[could not read file: {exc}]"


def run_command(cmd: str, cwd: str = "") -> str:
    """Run a shell command silently. Returns full output string."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30,
            cwd=cwd or None,
        )
        return (result.stdout + result.stderr).strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return "[command timed out after 30 s]"
    except Exception as exc:
        return f"[command error: {exc}]"


def run_command_live(cmd: str, cwd: str = "") -> str:
    """Run a shell command, streaming each line to stdout as it arrives.
    Returns the full output as a string for injecting back into the conversation."""
    from qwen3_code.theme import console, SAKURA, SAKURA_DARK, SAKURA_MUTED
    from rich.panel import Panel

    lines: list[str] = []
    console.print(Panel(
        f"[bold]$ {cmd}[/bold]",
        title="Running", border_style=SAKURA_MUTED,
    ))
    try:
        proc = subprocess.Popen(
            cmd, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            cwd=cwd or None,
        )
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            sys.stdout.write(raw_line)
            sys.stdout.flush()
            lines.append(raw_line)
        proc.wait()
        rc: int = proc.returncode
    except Exception as exc:
        err = f"[command error: {exc}]"
        console.print(f"[error]{err}[/error]")
        return err

    output: str  = "".join(lines).strip() or "(no output)"
    status_color = SAKURA if rc == 0 else SAKURA_DARK
    console.print(f"[{status_color}]{'\u2713 done' if rc == 0 else f'exit {rc}'}[/{status_color}]")
    return output


def build_context_snippet(cwd: str) -> str:
    try:
        files = [f.name for f in Path(cwd).iterdir() if f.is_file() and not f.name.startswith(".")][:20]
    except Exception:
        files = []
    return "\n".join([f"Working directory: {cwd}", f"Visible files: {', '.join(files) if files else 'none'}"])


# ---------------------------------------------------------------------------
# Animated spinner
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def spinning_dots(message: str) -> Generator[None, None, None]:
    """Show an animated '...' spinner on one stdout line while blocking work runs."""
    stop = threading.Event()
    frames = ["   ", ".  ", ".. ", "..."]

    def _spin() -> None:
        i = 0
        while not stop.is_set():
            sys.stdout.write(f"\r  {message}{frames[i % len(frames)]}")
            sys.stdout.flush()
            i += 1
            time.sleep(0.25)
        sys.stdout.write("\r" + " " * (len(message) + 8) + "\r")
        sys.stdout.flush()

    t = threading.Thread(target=_spin, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=1.0)
