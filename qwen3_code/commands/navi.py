"""/navi - toggle the navi (task router + slim prompt) mode."""

from rich.panel import Panel

from qwen3_code.theme import console, SAKURA, SAKURA_MUTED
from qwen3_code.settings import CFG, save_settings
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    a:       str  = arg.strip().lower()
    current: bool = bool(CFG.get("navi", False))

    if a in ("on", "true", "1", "yes"):
        new_val: bool = True
    elif a in ("off", "false", "0", "no"):
        new_val = False
    elif a == "":
        new_val = not current
    else:
        console.print("[error]Usage: /navi  |  /navi on  |  /navi off[/error]")
        return

    CFG["navi"] = new_val
    save_settings(CFG)

    if new_val:
        console.print(Panel(
            "[bold green]\u2713 Navi mode ON[/bold green]\n\n"
            "Before each response the model will now:\n"
            "  1. Restate your task in one sentence\n"
            "  2. Pick which action tags it actually needs\n"
            "  3. Receive a slim system prompt with only those tags\n\n"
            "[dim]Smaller, focused prompts make local models pick the right tag.[/dim]\n"
            "[dim]Toggle off with [bold]/navi[/bold] or [bold]/navi off[/bold][/dim]",
            title="/navi",
            border_style=SAKURA,
        ))
    else:
        console.print(Panel(
            "[bold]Navi mode OFF[/bold]\n"
            "[dim]Single-shot mode with the full prompt. Toggle on with [bold]/navi[/bold][/dim]",
            title="/navi",
            border_style=SAKURA_MUTED,
        ))


register(Command(
    name="/navi",
    handler=_handler,
    usage="/navi [on|off]",
    description="task summariser + tool router",
    category="General",
))
