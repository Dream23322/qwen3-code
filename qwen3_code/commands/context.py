"""/context - context tools (delegates to context_tools.handle_context)."""

from qwen3_code.context_tools import handle_context
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    handle_context(arg, messages, state)


register(Command(
    name="/context",
    handler=_handler,
    usage="/context [sub]",
    description="context tools  [dim]display / clear / clean[/dim]",
    category="General",
))
