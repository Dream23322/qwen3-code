"""/tree - print the project file tree."""

from rich.panel import Panel

from qwen3_code.theme import console, SAKURA_DEEP
from qwen3_code.utils import _short_cwd
from qwen3_code.commands import Command, register
from qwen3_code.commands._helpers import build_rich_tree


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    cwd:             str  = state["cwd"]
    include_ignored: bool = "-i" in arg

    tree       = build_rich_tree(cwd, include_ignored=include_ignored)
    title: str = f"Tree [{_short_cwd(cwd)}]"
    if include_ignored:
        title += "  (all dirs)"
    console.print(Panel(tree, title=title, border_style=SAKURA_DEEP))


register(Command(
    name="/tree",
    handler=_handler,
    usage="/tree [-i]",
    description="show project file tree  [dim](-i includes ignored dirs)[/dim]",
    category="General",
))
