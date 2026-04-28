"""/refresh - reload tracked files, prune stale context."""

from qwen3_code.refresh import handle_refresh
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    handle_refresh(messages, state)


register(Command(
    name="/refresh",
    handler=_handler,
    usage="/refresh",
    description="reload tracked files, prune stale context  [dim](gone files removed)[/dim]",
    category="General",
))
