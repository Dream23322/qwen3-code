"""/undo - move HEAD to parent commit."""

from qwen3_code.theme import console
from qwen3_code.utils import resolve_path
from qwen3_code.vc import all_tracked_files, _load_vc, do_undo
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    cwd:     str       = state["cwd"]
    tracked: list[str] = all_tracked_files()

    if not arg:
        candidates: list[str] = [
            fp for fp in tracked
            if _load_vc(fp).get("head")
            and _load_vc(fp)["commits"].get(_load_vc(fp)["head"], {}).get("parent_id")
        ]
        if not candidates:
            console.print("[info]Nothing to undo.[/info]")
        elif len(candidates) == 1:
            do_undo(candidates[0])
        else:
            console.print("[info]Multiple files:[/info]")
            for fp in candidates:
                console.print(f"  /undo {fp}")
        return

    do_undo(resolve_path(arg.split()[0], cwd))


register(Command(
    name="/undo",
    handler=_handler,
    usage="/undo [file]",
    description="move HEAD to parent commit",
    category="Version control",
))
