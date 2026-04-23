"""Partial-write detection, file-write application, and RUN-marker handling."""

import re
import sys

from rich.panel import Panel

from qwen3_code.theme import console, SAKURA_DEEP, SAKURA_DARK, SAKURA_MUTED
from qwen3_code.utils import run_command_live
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
    """Apply <!-- RUN: cmd --> markers, streaming output live."""
    for m in _RUN_PATTERN.finditer(reply):
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
        output = run_command_live(cmd, cwd)
        messages.append({"role": "user", "content": f"[Command `{cmd}` output:]\n```\n{output}\n```"})
