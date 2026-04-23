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


# ---------------------------------------------------------------------------
# Split-screen console session
# ---------------------------------------------------------------------------

class ConsoleSession:
    """Maintains a persistent right-side panel showing the full command history.

    While a command runs, the terminal is split:
      - Left 2/3  : live streaming output of the current command
      - Right 1/3 : scrolling log of all commands run in this session

    After the session ends the history panel is printed as a final summary.
    """

    def __init__(self) -> None:
        self.history: list[tuple[str, str, int]] = []  # (cmd, output, returncode)

    # ------------------------------------------------------------------
    # Panel builders
    # ------------------------------------------------------------------

    def _history_panel(self, current_cmd: str = ""):
        from rich.panel import Panel
        from rich.markup import escape as esc
        lines: list[str] = []
        for cmd, out, rc in self.history:
            icon = "[green]\u2713[/green]" if rc == 0 else f"[red]\u2717({rc})[/red]"
            lines.append(f"{icon} [bold cyan]$ {esc(cmd)}[/bold cyan]")
            tail = out.splitlines()[-4:]
            for l in tail:
                lines.append(f"  [dim]{esc(l[:80])}[/dim]")
            lines.append("")
        if current_cmd:
            lines.append(f"[bold yellow]\u25b6[/bold yellow] [bold]$ {esc(current_cmd)}[/bold]")
        body = "\n".join(lines).strip() or "[dim]No commands run yet.[/dim]"
        # local import to avoid circular dep at module level
        from qwen3_code.theme import SAKURA_MUTED
        return Panel(body, title="[bold]Console Session[/bold]", border_style=SAKURA_MUTED)

    @staticmethod
    def _output_panel(cmd: str, lines: list[str]):
        from rich.panel import Panel
        from rich.markup import escape as esc
        from qwen3_code.theme import SAKURA_MUTED
        body = "\n".join(esc(l) for l in lines[-40:]) if lines else "[dim]Running\u2026[/dim]"
        return Panel(
            f"[bold cyan]$ {esc(cmd)}[/bold cyan]\n\n{body}",
            title="Running",
            border_style=SAKURA_MUTED,
        )

    # ------------------------------------------------------------------
    # Run a single command with split-screen live display
    # ------------------------------------------------------------------

    def run(self, cmd: str, cwd: str = "") -> str:
        """Run *cmd* with a split-screen live display; return full output string."""
        from rich.layout import Layout
        from rich.live   import Live
        from qwen3_code.theme import console, SAKURA, SAKURA_DARK

        output_lines: list[str] = []
        returncode = 0

        layout = Layout()
        layout.split_row(
            Layout(name="output",  ratio=2),
            Layout(name="session", ratio=1),
        )
        layout["output"].update(self._output_panel(cmd, []))
        layout["session"].update(self._history_panel(current_cmd=cmd))

        try:
            with Live(
                layout,
                console=console,
                refresh_per_second=15,
                vertical_overflow="visible",
                transient=False,
            ):
                try:
                    proc = subprocess.Popen(
                        cmd, shell=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, bufsize=1,
                        cwd=cwd or None,
                    )
                    assert proc.stdout is not None
                    for raw_line in proc.stdout:
                        output_lines.append(raw_line.rstrip())
                        layout["output"].update(self._output_panel(cmd, output_lines))
                    proc.wait()
                    returncode = proc.returncode
                except Exception as exc:
                    output_lines.append(f"[error: {exc}]")
                    returncode = 1
                # Update session panel with completed entry
                output_so_far = "\n".join(output_lines)
                self.history.append((cmd, output_so_far, returncode))
                layout["output"].update(self._output_panel(cmd, output_lines))
                layout["session"].update(self._history_panel())
        except Exception:
            # Fallback: plain streaming if Live/Layout unavailable
            console.print(f"[bold]$ {cmd}[/bold]")
            for line in output_lines:
                sys.stdout.write(line + "\n")
            sys.stdout.flush()

        # Status line below the live display
        status_style = SAKURA if returncode == 0 else SAKURA_DARK
        console.print(
            f"[{status_style}]{'\u2713 done' if returncode == 0 else f'exit {returncode}'}[/{status_style}]"
        )

        return "\n".join(output_lines) or "(no output)"

    def print_summary(self) -> None:
        """Print a final history panel after all commands are done."""
        from qwen3_code.theme import console
        if len(self.history) > 1:
            console.print(self._history_panel())


def run_command_live(cmd: str, cwd: str = "") -> str:
    """Convenience wrapper: run a single command with the split-screen session UI."""
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
