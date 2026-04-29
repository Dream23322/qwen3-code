"""/quit - exit the REPL."""

from qwen3_code.theme import console
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> bool:
    console.print("[info]Goodbye.[/info]")
    return False


register(Command(
    name="/quit",
    handler=_handler,
    usage="/quit",
    description="exit",
    category="General",
    aliases=("/exit", "/q"),
))
