"""/history - print message history."""

from qwen3_code.theme import console
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    for i, m in enumerate(messages):
        line: str = m["content"][:120].replace(chr(10), " ")
        console.print(f"[info][{i}] {m['role']}: {line}[/info]")


register(Command(
    name="/history",
    handler=_handler,
    usage="/history",
    description="show message history",
    category="General",
))
