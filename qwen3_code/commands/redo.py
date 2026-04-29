"""/redo - move HEAD to a child commit."""

from qwen3_code.theme import console
from qwen3_code.utils import resolve_path
from qwen3_code.vc import all_tracked_files, _load_vc, do_redo
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    cwd:    str       = state["cwd"]
    tokens: list[str] = arg.split(maxsplit=1) if arg else []

    if not tokens:
        tracked: list[str] = all_tracked_files()
        candidates: list[str] = [
            fp for fp in tracked
            if _load_vc(fp).get("head")
            and _load_vc(fp)["commits"].get(_load_vc(fp)["head"], {}).get("children")
        ]
        if not candidates:
            console.print("[info]Nothing to redo.[/info]")
        elif len(candidates) == 1:
            do_redo(candidates[0])
        else:
            console.print("[info]Multiple files:[/info]")
            for fp in candidates:
                console.print(f"  /redo {fp}")
        return

    if len(tokens) == 1:
        do_redo(resolve_path(tokens[0], cwd))
    else:
        do_redo(resolve_path(tokens[0], cwd), target_id=tokens[1])


register(Command(
    name="/redo",
    handler=_handler,
    usage="/redo [file] [id]",
    description="move HEAD to child  [dim](menu if branched)[/dim]",
    category="Version control",
))
