"""/plan - AI plans then auto-executes a task."""

from rich.panel import Panel

from qwen3_code.theme import console, SAKURA_MUTED
from qwen3_code.session import save_session
from qwen3_code.renderer import stream_response
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    cwd:  str = state["cwd"]
    task: str = arg
    if not task.strip():
        console.print("[error]Usage: /plan <task description>[/error]")
        return

    plan_prompt: str = (
        f"Create a concise numbered step-by-step plan for the following task. "
        f"Be specific about which files to create or edit and which commands to run. "
        f"Output the plan only \u2014 do NOT start implementing yet.\n\nTask: {task}"
    )
    messages.append({"role": "user", "content": plan_prompt})
    console.print(Panel(
        f"[bold]Planning:[/bold] {task}", title="/plan", border_style=SAKURA_MUTED,
    ))
    plan_reply = stream_response(messages)
    if not plan_reply:
        messages.pop()
        return
    messages.append({"role": "assistant", "content": plan_reply})

    exec_prompt: str = (
        "Good plan. Now execute it step by step. "
        "Use <!-- WRITE: path --> markers for any file edits and "
        "<!-- RUN: cmd --> markers for any shell commands that need to run."
    )
    messages.append({"role": "user", "content": exec_prompt})
    console.print(Panel(
        "[bold]Executing plan\u2026[/bold]", title="/plan", border_style=SAKURA_MUTED,
    ))
    exec_reply = stream_response(messages, cwd)
    if exec_reply:
        messages.append({"role": "assistant", "content": exec_reply})
    else:
        messages.pop()
    save_session(cwd, messages)


register(Command(
    name="/plan",
    handler=_handler,
    usage="/plan <task>",
    description="AI plans then auto-executes a task",
    category="General",
))
