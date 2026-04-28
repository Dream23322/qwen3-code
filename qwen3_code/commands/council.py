"""/council - multi-model deliberation (delegates to council.handle_council)."""

from qwen3_code.council import handle_council
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    handle_council(arg, state)


register(Command(
    name="/council",
    handler=_handler,
    usage="/council [start|end]",
    description="multi-model deliberation  [dim]members answer, leader picks[/dim]",
    category="General",
))
