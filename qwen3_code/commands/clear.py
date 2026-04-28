"""/clear - reset conversation history."""

from qwen3_code.theme import console
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    messages.clear()
    console.clear()
    console.print("[info]Conversation cleared.[/info]")


register(Command(
    name="/clear",
    handler=_handler,
    usage="/clear",
    description="clear conversation history",
    category="General",
))
