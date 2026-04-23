"""Partial-write detection, file-write application, RUN-marker handling, and READ-request handling."""

import re
from pathlib import Path

from rich.panel import Panel

from qwen3_code.theme import console, SAKURA_DEEP, SAKURA_DARK, SAKURA_MUTED
from qwen3_code.utils import ConsoleSession
from qwen3_code.vc import write_file_with_vc

# ---------------------------------------------------------------------------
# Partial-write detection
# ---------------------------------------------------------------------------

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

_RUN_PATTERN: re.Pattern = re.compile(r"<!--\s*RUN:\s*(?P<cmd>[^>]+?)\s*-->", re.DOTALL)

# ---------------------------------------------------------------------------
# READ request markers  (AI → tool)
# ---------------------------------------------------------------------------

_READ_REQUEST_RE: re.Pattern = re.compile(
    r"<!--\s*READ:\s*(?P<path>[^\s>]+)\s*-->"
)


def has_read_requests(reply: str) -> bool:
    """Return True if the reply contains at least one <!-- READ: path --> marker."""
    return bool(_READ_REQUEST_RE.search(reply))


def collect_read_requests(reply: str) -> list[str]:
    """Return deduplicated list of paths from <!-- READ: path --> markers."""
    seen: set[str] = set()
    paths: list[str] = []
    for m in _READ_REQUEST_RE.finditer(reply):
        p = m.group("path").strip()
        if p not in seen:
            seen.add(p)
            paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# Partial-write helpers
# ---------------------------------------------------------------------------

def reply_has_partial_write(reply: str) -> bool:
    for m in _WRITE_PATTERN.finditer(reply):
        for pat in _PARTIAL_PATTERNS:
            if pat.search(m.group("code")):
                return True
    return False


def apply_file_writes(reply: str) -> None:
    for m in _WRITE_PATTERN.finditer(reply):
        write_file_with_vc(m.group("path").strip(), m.group("code"))


def apply_command_runs(reply: str, cwd: str, messages: list[dict]) -> None:
    """Process <!-- RUN: cmd --> markers with a shared split-screen console session.

    All approved commands in a single AI response share one ConsoleSession so
    the right-side history panel accumulates across the whole sequence.
    """
    matches = list(_RUN_PATTERN.finditer(reply))
    if not matches:
        return

    session = ConsoleSession()

    for m in matches:
        cmd = m.group("cmd").strip()
        if not cmd:
            continue
        console.print(Panel(
            f"[bold]The assistant wants to run:[/bold]\n  [bold]{cmd}[/bold]\n\n[info]CWD: {cwd}[/info]",
            title="Permission required", border_style=SAKURA_DARK,
        ))
        try:
            answer = input("Allow? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print("[info]Skipped.[/info]")
            continue
        if answer not in ("y", "yes"):
            console.print("[info]Command skipped.[/info]")
            messages.append({"role": "user", "content": f"[Command `{cmd}` was denied.]"})
            continue
        output = session.run(cmd, cwd)
        messages.append({"role": "user", "content": f"[Command `{cmd}` output:]\n```\n{output}\n```"})

    # Print a final summary panel when multiple commands were run
    session.print_summary()
