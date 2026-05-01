"""/rules - apply built-in rule presets or custom coding-rule files."""

from qwen3_code.rules import handle_rules
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    handle_rules(arg, state)


register(Command(
    name="/rules",
    handler=_handler,
    usage="/rules [preset|custom|list|show|off]",
    description="apply built-in rule presets (pep8, ...) or custom rules",
    category="General",
))
