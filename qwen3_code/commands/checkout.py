"""/checkout - check out any commit by ID."""

from qwen3_code.theme import console
from qwen3_code.utils import resolve_path
from qwen3_code.vc import all_tracked_files, _load_vc, do_checkout
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    cwd: str = state["cwd"]
    if not arg:
        console.print("[error]Usage: /checkout <commit_id> [filepath][/error]")
        return

    tokens:  list[str]   = arg.split(maxsplit=1)
    cid_arg: str         = tokens[0]
    fp_arg:  str | None  = tokens[1] if len(tokens) > 1 else None

    if fp_arg:
        do_checkout(resolve_path(fp_arg, cwd), cid_arg)
        return

    tracked: list[str] = all_tracked_files()
    found:   list[str] = [
        fp for fp in tracked
        if any(c.startswith(cid_arg) for c in _load_vc(fp).get("commits", {}))
    ]
    if len(found) == 1:
        do_checkout(found[0], cid_arg)
    elif len(found) > 1:
        console.print("[info]Matches multiple files. Specify filepath:[/info]")
        for fp in found:
            console.print(f"  /checkout {cid_arg} {fp}")
    else:
        console.print(f"[error]Commit '{cid_arg}' not found.[/error]")


register(Command(
    name="/checkout",
    handler=_handler,
    usage="/checkout <id> [file]",
    description="check out any commit by ID",
    category="Version control",
))
