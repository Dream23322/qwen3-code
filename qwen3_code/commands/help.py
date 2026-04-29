"""/help - auto-generated help table from the command registry."""

from rich.panel import Panel
from rich.table import Table

from qwen3_code.theme import console, SAKURA_DEEP
from qwen3_code.settings import CFG
from qwen3_code.commands import Command, register, commands_by_category


# Optional dim subtitle shown next to a category header in the help table.
_CATEGORY_NOTES: dict[str, str] = {
    "Version control": "[dim]git-like, tree-based[/dim]",
}


def _help_table() -> Table:
    learn_status: str = " [green](ON)[/green]" if CFG.get("learn_mode") else ""

    t = Table(show_header=False, box=None, padding=(0, 1), expand=True)
    t.add_column("cmd",  no_wrap=True, min_width=26)
    t.add_column("desc", justify="right")

    grouped: dict[str, list[Command]] = commands_by_category()
    first:   bool                     = True
    for category, cmds in grouped.items():
        if not first:
            t.add_row("", "")
        first = False
        t.add_row(
            f"[bold]{category}[/bold]",
            _CATEGORY_NOTES.get(category, ""),
        )
        for cmd in cmds:
            usage: str = cmd.usage or cmd.name
            desc:  str = cmd.description
            if cmd.name == "/learn":
                desc = f"{desc}{learn_status}"
            t.add_row(f"  {usage}", desc)

    return t


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    console.print(Panel(_help_table(), title="Help", border_style=SAKURA_DEEP))


register(Command(
    name="/help",
    handler=_handler,
    usage="/help",
    description="show this help",
    category="General",
))
