"""/loadtree - inject the project tree (optionally with AI descriptions) into AI context."""

from qwen3_code.theme import console
from qwen3_code.commands import Command, register
from qwen3_code.commands._helpers import (
    load_desc_cache,
    collect_files_for_tree,
    generate_file_descriptions_streamed,
    build_text_tree,
)


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    cwd: str = state["cwd"]

    flags:             list[str] = arg.lower().split()
    include_ignored:   bool      = "-i" in flags
    with_descriptions: bool      = "-d" in flags

    descriptions: dict[str, str] | None = None
    if with_descriptions:
        descriptions = load_desc_cache(cwd)
        if descriptions:
            console.print(
                f"[dim]Using cached descriptions ({len(descriptions)} files).[/dim]"
            )
        else:
            file_list = collect_files_for_tree(cwd, include_ignored=include_ignored)
            if file_list:
                console.print(
                    f"[dim]Generating descriptions for up to {len(file_list)} file(s)\u2026[/dim]"
                )
                descriptions = generate_file_descriptions_streamed(file_list, cwd=cwd)
                console.print(
                    f"[dim]Got descriptions for {len(descriptions)} file(s).[/dim]"
                )

    tree_text:  str       = build_text_tree(cwd, include_ignored=include_ignored, descriptions=descriptions)
    note_parts: list[str] = []
    if include_ignored:
        note_parts.append("all directories included")
    if with_descriptions:
        note_parts.append("AI descriptions included")
    note: str = (
        "(" + ", ".join(note_parts) + ")"
        if note_parts else "(ignored dirs noted but not expanded)"
    )
    context_block: str = (
        f"Project directory tree for `{cwd}` {note}:\n\n"
        f"```\n{tree_text}\n```"
    )
    state.setdefault("pending_context", []).append(context_block)

    line_count: int = tree_text.count("\n") + 1
    console.print(
        f"[info]Project tree loaded into context ({line_count} lines). {note}[/info]"
    )


register(Command(
    name="/loadtree",
    handler=_handler,
    usage="/loadtree [-i] [-d]",
    description="inject project tree into AI context  [dim](-i incl. ignored, -d adds AI descriptions)[/dim]",
    category="General",
))
