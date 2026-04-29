"""/cd - change working directory."""

import os
from pathlib import Path

from rich.panel import Panel

from qwen3_code.theme import console, SAKURA
from qwen3_code.session import save_session, load_session
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    cwd: str = state["cwd"]
    if not arg:
        console.print(f"[info]Current directory: {cwd}[/info]")
        return

    target: Path = Path(arg) if Path(arg).is_absolute() else Path(cwd) / arg
    try:
        target = target.resolve(strict=True)
    except FileNotFoundError:
        console.print(f"[error]Directory not found: {arg}[/error]")
        return

    if not target.is_dir():
        console.print(f"[error]{target} is not a directory.[/error]")
        return

    save_session(cwd, messages)
    state["cwd"] = str(target)
    os.chdir(target)

    new_msgs: list[dict] = load_session(str(target))
    messages.clear()
    messages.extend(new_msgs)

    # Reset per-directory state
    state["pending_context"] = []
    state["first_message"]   = not any(m["role"] != "system" for m in messages)

    try:
        entries: list[str] = [
            e.name for e in target.iterdir() if not e.name.startswith(".")
        ][:30]

    except Exception:
        entries = []

    msg_count:    int = len([m for m in messages if m["role"] != "system"])
    session_note: str = (
        f"Resumed session  ({msg_count} message(s))"
        if msg_count else "New session"
    )
    console.print(Panel(
        f"[info]Changed to: [bold]{target}[/bold]\n"
        f"Contents: {', '.join(entries) or '(empty)'}\n"
        f"[dim]{session_note}[/dim][/info]",
        title="cd", border_style=SAKURA,
    ))


register(Command(
    name="/cd",
    handler=_handler,
    usage="/cd [dir]",
    description="change working directory",
    category="General",
))
