"""Shared constants, filesystem helpers, animated spinner, and ConsoleSession."""

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

    ============================================================
    RUNNING COMMANDS
    ============================================================
    When you need to execute a shell command, emit it using this EXACT marker:

        <!-- RUN: <shell command here> -->

    RULES:
    - You MAY emit multiple RUN markers in one response.
    - Place each RUN marker on its own line, in the order they should run.
    - NEVER simulate or invent command output in a code block.
    - The tool will ask the user to confirm each command before running it.

    ============================================================
    FILE EDITING  —  full rewrite
    ============================================================
    When you need to rewrite an entire file, use:

        <!-- WRITE: path/to/file -->
        ```python
        <complete file contents>
        ```

    Always provide the COMPLETE file. The tool backs up the original (/undo).

    ============================================================
    FILE EDITING  —  targeted insertion
    ============================================================
    When you only need to INSERT new lines at a specific location WITHOUT
    rewriting the whole file, use:

        <!-- INSERT: path/to/file:LINE_NUMBER -->
        ```python
        <lines to insert>
        ```

    LINE_NUMBER is 1-based. The new lines are inserted BEFORE that line,
    pushing existing content down. Use this for adding imports, functions,
    or blocks when the rest of the file is unchanged.

    Example — insert a new function before line 42 of utils.py:

        <!-- INSERT: utils.py:42 -->
        ```python
        def helper():
            return 42
        ```

    When insert_verify is enabled (default), the tool will:
      1. Run a syntax check on the resulting file.
      2. Show a diff preview around the insertion point.
      3. Ask the user to confirm before writing.

    Prefer INSERT over WRITE when your change is purely additive and
    the surrounding code is unchanged.

    ============================================================
    REQUESTING FILES
    ============================================================
    If you need to read a file that hasn't been provided yet, emit:

        <!-- READ: path/to/file -->

    You may emit multiple READ markers. The tool reads each file and
    reprompts you automatically. Do NOT guess file contents.
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


# ---------------------------------------------------------------------------
# ConsoleSession
# ---------------------------------------------------------------------------

class ConsoleSession:
    _MAX_VISIBLE = 40

    def __init__(self) -> None:
        self.history: list[tuple[str, str, int]] = []

    def _build_panel(self, cmd: str, lines: list[str], finished: bool, returncode: int):
        from rich.panel import Panel
        from rich.markup import escape as esc
        from qwen3_code.theme import SAKURA, SAKURA_DARK, SAKURA_MUTED

        body   = "\n".join(esc(l) for l in lines[-self._MAX_VISIBLE:]) if lines else "[dim]Running\u2026[/dim]"
        title  = ("Running" if not finished else ("\u2713 done" if returncode == 0 else f"exit {returncode}"))
        border = SAKURA_MUTED if not finished else (SAKURA if returncode == 0 else SAKURA_DARK)
        return Panel(f"[bold cyan]$ {esc(cmd)}[/bold cyan]\n\n{body}", title=title, border_style=border)

    def _summary_panel(self):
        from rich.panel import Panel
        from rich.markup import escape as esc
        from qwen3_code.theme import SAKURA_MUTED

        lines: list[str] = []
        for cmd, out, rc in self.history:
            icon = "[green]\u2713[/green]" if rc == 0 else f"[red]\u2717 exit {rc}[/red]"
            lines.append(f"{icon}  [bold cyan]$ {esc(cmd)}[/bold cyan]")
            for l in out.splitlines()[-3:]:
                lines.append(f"  [dim]{esc(l[:100])}[/dim]")
            lines.append("")
        body = "\n".join(lines).strip() or "[dim](no commands)[/dim]"
        return Panel(body, title="[bold]Console Session Summary[/bold]", border_style=SAKURA_MUTED)

    def run(self, cmd: str, cwd: str = "") -> str:
        from rich.live import Live
        from qwen3_code.theme import console

        output_lines: list[str] = []
        returncode   = 0

        try:
            proc = subprocess.Popen(
                cmd, shell=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                cwd=cwd or None,
            )
            assert proc.stdout is not None

            with Live(
                self._build_panel(cmd, [], False, 0),
                console=console,
                refresh_per_second=15,
                vertical_overflow="visible",
            ) as live:
                for raw_line in proc.stdout:
                    output_lines.append(raw_line.rstrip())
                    live.update(self._build_panel(cmd, output_lines, False, 0))
                proc.wait()
                returncode = proc.returncode
                live.update(self._build_panel(cmd, output_lines, True, returncode))

        except Exception as exc:
            output_lines.append(f"[command error: {exc}]")
            returncode = 1
            console.print(self._build_panel(cmd, output_lines, True, returncode))

        output = "\n".join(output_lines)
        self.history.append((cmd, output, returncode))
        return output or "(no output)"

    def print_summary(self) -> None:
        from qwen3_code.theme import console
        if len(self.history) > 1:
            console.print(self._summary_panel())


def run_command_live(cmd: str, cwd: str = "") -> str:
    return ConsoleSession().run(cmd, cwd)


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
