"""/commit - manually commit current file state."""

from qwen3_code.theme import console
from qwen3_code.utils import resolve_path
from qwen3_code.vc import do_manual_commit
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    cwd: str = state["cwd"]
    if not arg:
        console.print("[error]Usage: /commit <filepath> [message][/error]")
        return

    tokens: list[str] = arg.split(maxsplit=1)
    do_manual_commit(
        resolve_path(tokens[0], cwd),
        tokens[1] if len(tokens) > 1 else "",
    )


register(Command(
    name="/commit",
    handler=_handler,
    usage="/commit <file> [msg]",
    description="manually commit current file state",
    category="Version control",
))
