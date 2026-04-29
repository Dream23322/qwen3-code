"""/v - generate (and cache) AI file descriptions, then show the tree."""

from rich.panel import Panel

from qwen3_code.theme import console, SAKURA_DEEP
from qwen3_code.utils import _short_cwd
from qwen3_code.commands import Command, register
from qwen3_code.commands._helpers import (
    collect_files_for_tree,
    generate_file_descriptions_streamed,
    build_rich_tree,
    desc_context_block,
)


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    cwd:             str  = state["cwd"]
    include_ignored: bool = "-i" in arg

    file_list = collect_files_for_tree(cwd, include_ignored=include_ignored)
    if not file_list:
        console.print("[info]No files found.[/info]")
        return

    console.print(
        f"[dim]Generating descriptions for up to {len(file_list)} file(s)\u2026[/dim]"
    )
    descriptions = generate_file_descriptions_streamed(file_list, cwd=cwd)
    tree         = build_rich_tree(cwd, include_ignored=include_ignored, descriptions=descriptions)
    title: str   = f"Tree + descriptions [{_short_cwd(cwd)}]"
    if include_ignored:
        title += "  (all dirs)"
    console.print(Panel(tree, title=title, border_style=SAKURA_DEEP))

    if descriptions:
        state.setdefault("pending_context", []).append(
            desc_context_block(cwd, descriptions)
        )
        console.print("[dim]Descriptions added to AI context.[/dim]")


register(Command(
    name="/v",
    handler=_handler,
    usage="/v [-i]",
    description="generate + cache AI descriptions, show tree  [dim](streamed)[/dim]",
    category="General",
))
