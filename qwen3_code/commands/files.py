"""/files - list all tracked files."""

from pathlib import Path

from rich.panel import Panel

from qwen3_code.theme import console, SAKURA
from qwen3_code.vc import all_tracked_files, _load_vc
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    tracked: list[str] = all_tracked_files()
    if not tracked:
        console.print("[info]No tracked files.[/info]")
        return

    rows: list[str] = [
        f"  {fp}  {'exists' if Path(fp).exists() else 'missing'}  "
        f"{len(_load_vc(fp).get('commits', {}))} commits  "
        f"HEAD={_load_vc(fp).get('head', '-')}"
        for fp in tracked
    ]
    console.print(Panel("\n".join(rows), title="Tracked files", border_style=SAKURA))


register(Command(
    name="/files",
    handler=_handler,
    usage="/files",
    description="list all tracked files",
    category="Version control",
))
