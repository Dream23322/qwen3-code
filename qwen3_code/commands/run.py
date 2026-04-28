"""/run - run a shell command (output streams live)."""

from qwen3_code.theme import console
from qwen3_code.utils import run_command_live
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    cwd: str = state["cwd"]
    if not arg:
        console.print("[error]Usage: /run <shell command>[/error]")
        return

    output: str = run_command_live(arg, cwd)
    messages.append(
        {"role": "user", "content": f"Output of `{arg}`:\n\n```\n{output}\n```"}
    )


register(Command(
    name="/run",
    handler=_handler,
    usage="/run <cmd>",
    description="run a shell command  [dim](output streams live)[/dim]",
    category="General",
))
