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

    ============================================================
    RUNNING COMMANDS
    ============================================================
    When you need to execute a shell command, emit it using this EXACT marker:

        <!-- RUN: <shell command here> -->

    RULES:
    - You MAY emit multiple RUN markers in one response when a task requires
      a sequence of commands (e.g. create venv, activate, install, verify).
    - Place each RUN marker on its own line, in the order they should run.
    - NEVER simulate or invent command output in a code block. If the user asks
      you to "run", "execute", or "create a console session", use real RUN
      markers so the commands actually execute on their machine.
    - Do NOT emit a RUN marker for commands the user did not request.
    - The tool will ask the user to confirm each command before running it.

    Example — setting up a venv:

        <!-- RUN: python -m venv myenv -->
        <!-- RUN: myenv\Scripts\activate && pip install requests -->

    ============================================================
    FILE EDITING
    ============================================================
    When the user asks you to edit or rewrite a file, respond with the
    complete new file content using this EXACT format:

        <!-- WRITE: path/to/file -->
        ```python
        <complete file contents>
        ```

    Always provide the COMPLETE file from top to bottom. The tool will back up
    the original so the user can /undo at any time.

    ============================================================
    REQUESTING FILES
    ============================================================
    If you need to read a file that hasn't been provided yet, emit:

        <!-- READ: path/to/file -->

    You may emit multiple READ markers in one response. The tool will read
    each file and provide the contents automatically, then reprompt you to
    continue. Do NOT guess or make up file contents — if you need a file,
    request it with a READ marker.

    Example — asking for two files before answering:

        <!-- READ: src/main.py -->
        <!-- READ: src/utils.py -->
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


# ---------------------------------------------------------------------------
# ConsoleSession  —  live-updating boxed output
# ---------------------------------------------------------------------------

class ConsoleSession:
    """Runs commands with a live-updating Rich Panel around all output.

    Each command streams into a panel that updates in-place.  When the
    command finishes the border turns green (success) or red (failure).
    When multiple commands are run in one session a summary panel is
    printed at the end.
    """

    _MAX_VISIBLE = 40  # max output lines visible inside the box while streaming

    def __init__(self) -> None:
        self.history: list[tuple[str, str, int]] = []  # (cmd, output, returncode)

    # ------------------------------------------------------------------
    # Panel builders
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Run a single command
    # ------------------------------------------------------------------

    def run(self, cmd: str, cwd: str = "") -> str:
        """Run *cmd*, streaming its output into a live-updating box."""
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
        """Print a session summary panel when 2+ commands were run."""
        from qwen3_code.theme import console
        if len(self.history) > 1:
            console.print(self._summary_panel())


def run_command_live(cmd: str, cwd: str = "") -> str:
    """Convenience wrapper: run one command with the boxed live display."""
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
