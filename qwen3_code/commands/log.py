"""/log - show commit tree for a tracked file."""

from qwen3_code.theme import console
from qwen3_code.utils import resolve_path
from qwen3_code.vc import all_tracked_files, show_log
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    cwd:     str       = state["cwd"]
    tracked: list[str] = all_tracked_files()

    if not arg:
        if not tracked:
            console.print("[info]No tracked files.[/info]")
        elif len(tracked) == 1:
            show_log(tracked[0])
        else:
            console.print("[info]Multiple files:[/info]")
            for fp in tracked:
                console.print(f"  /log {fp}")
        return

    show_log(resolve_path(arg.split()[0], cwd))


register(Command(
    name="/log",
    handler=_handler,
    usage="/log [file]",
    description="show commit tree",
    category="Version control",
))
