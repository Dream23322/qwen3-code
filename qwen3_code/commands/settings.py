"""/settings - view/edit settings."""

from qwen3_code.settings import handle_settings
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    handle_settings(arg)


register(Command(
    name="/settings",
    handler=_handler,
    usage="/settings [key val]",
    description="view/edit settings  [dim](saved to settings.json)[/dim]",
    category="General",
))
