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

# ---------------------------------------------------------------------------
# System prompt -- composable per-tool sections
# ---------------------------------------------------------------------------
#
# The full prompt is built from three parts:
#   1. _BASE_SYSTEM_PROMPT  : identity, style, hard "USE TOOLS" pressure
#   2. _TOOL_SECTIONS[k]    : per-tool docs, included on demand
#   3. _REMINDER            : trailing reminder block
#
# `build_system_prompt(tools=None)` returns the full prompt by default. Pass
# a subset like {"read", "run"} to get a slim prompt covering only those
# action tags -- this is what navi mode does after picking the tools the
# current task actually needs.

_BASE_SYSTEM_PROMPT: str = textwrap.dedent("""\
    You are an expert software engineer assistant embedded in a terminal.
    You help the user understand, write, debug, and refactor code.
    Be concise and direct. Prefer targeted, minimal changes.

    ============================================================
    USE TOOLS -- DO NOT DESCRIBE WHAT TO DO, DO IT
    ============================================================
    You have ACTION TAGS available (documented below). Whenever the user's
    request involves a real file or a shell command on this machine, you
    MUST use the tags. Do not just tell the user what to do -- DO IT.

    Decision shortcuts (memorise these):
      "show / read / what's in / explain X"        -> emit  <qread path="X" />
      "fix / change / edit / refactor X"           -> if you do not have X, emit <qread/> first; THEN <qwrite> or <qinsert>
      "add / append / insert / import / register"  -> emit  <qinsert>
      "rewrite / replace the whole file"           -> emit  <qwrite>
      "run / try / test / install / build / lint"  -> emit  <qrun>cmd</qrun>
      "give me an example / pseudocode / sketch"   -> emit  <qcode>  (display only, no file change)

    Hard rules:
    - If you do not already have a file's contents, REQUEST it with
      <qread path="..." />. NEVER guess what a file contains.
    - Inside any <q*> tag, write content EXACTLY as it should appear --
      do NOT escape backticks, dollar signs, angle brackets, or anything
      else. The tag itself terminates the block.
    - Do NOT use triple-backtick markdown fences anywhere in your output.
      Markdown fences break inside markdown prose.
""").strip()

_TOOL_SECTIONS: dict[str, str] = {
    "code": textwrap.dedent("""\
        ============================================================
        <qcode>  --  display code (no file action)
        ============================================================

            <qcode lang="python">
            def hello():
                print("hi")
            </qcode>

        The lang attribute is optional and defaults to "text".
        Use this only when you want to SHOW code without writing a file.
    """).strip(),

    "write": textwrap.dedent("""\
        ============================================================
        <qwrite>  --  full file rewrite
        ============================================================

            <qwrite path="path/to/file" lang="python">
            <complete file contents>
            </qwrite>

        Always provide the COMPLETE file. The tool backs up the original
        (recover with /undo). Prefer <qinsert> when the change is purely
        additive -- it is faster and far less likely to introduce bugs.
    """).strip(),

    "insert": textwrap.dedent("""\
        ============================================================
        <qinsert>  --  targeted insertion
        ============================================================

            <qinsert path="path/to/file" line="42" lang="python">
            <lines to insert>
            </qinsert>

        The "line" attribute is 1-based. New lines are inserted BEFORE
        that line, pushing existing content down. Use this for adding
        imports, functions, or blocks when surrounding code is unchanged.
    """).strip(),

    "read": textwrap.dedent("""\
        ============================================================
        <qread/>  --  request a file's contents
        ============================================================

            <qread path="path/to/file" />

        You may emit multiple <qread/> tags. The tool reads each file
        and reprompts you automatically with the contents. Whenever you
        need a file you do not have, USE THIS -- never guess.
    """).strip(),

    "run": textwrap.dedent("""\
        ============================================================
        <qrun>  --  execute a shell command
        ============================================================

            <qrun>shell command here</qrun>

        Rules:
        - You MAY emit multiple <qrun> tags in one response.
        - Place each tag on its own line, in execution order.
        - NEVER simulate or invent command output.
        - The user is asked to confirm each command before it runs.
    """).strip(),
}

_REMINDER: str = textwrap.dedent("""\
    ============================================================
    QUICK REMINDER
    ============================================================
    - Use the action tags above. Do NOT use ``` markdown fences.
    - Inside <q*> tags, write content literally with NO escaping.
    - When in doubt, REQUEST a file with <qread/> rather than guessing.
""").strip()

_TOOL_ORDER: tuple[str, ...] = ("code", "write", "insert", "read", "run")


def build_system_prompt(tools: set[str] | None = None) -> str:
    """Compose the system prompt.

    Pass *tools* (a subset of {"code", "write", "insert", "read", "run"})
    to get a slim prompt that documents only those action tags. Pass None
    (default) for the full prompt covering every tool.
    """
    if tools is None:
        selected: list[str] = list(_TOOL_ORDER)
    else:
        selected = [t for t in _TOOL_ORDER if t in tools]

    if selected:
        section_block: str = "\n\n".join(_TOOL_SECTIONS[k] for k in selected)
    else:
        section_block = (
            "============================================================\n"
            "ACTION TAGS\n"
            "============================================================\n"
            "(no action tags requested for this turn -- respond with prose only)"
        )

    return "\n\n".join([_BASE_SYSTEM_PROMPT, section_block, _REMINDER])


SYSTEM_PROMPT: str = build_system_prompt()

PARTIAL_REPROMPT: str = (
    "Your last response contained a partial file (truncation markers like "
    "\"...\", \"# rest of\", or similar). "
    "Provide the COMPLETE file using the "
    "<qwrite path=\"...\" lang=\"...\"> ... </qwrite> format."
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
