"""/learn - toggle beginner tutorial mode."""

from rich.panel import Panel

from qwen3_code.theme import console, SAKURA, SAKURA_MUTED
from qwen3_code.settings import CFG, save_settings
from qwen3_code.commands import Command, register


def _handler(arg: str, messages: list[dict], state: dict) -> None:
    a:       str  = arg.strip().lower()
    current: bool = bool(CFG.get("learn_mode", False))

    if a in ("on", "true", "1", "yes"):
        new_val: bool = True
    elif a in ("off", "false", "0", "no"):
        new_val = False
    elif a == "":
        new_val = not current
    else:
        console.print("[error]Usage: /learn  |  /learn on  |  /learn off[/error]")
        return

    CFG["learn_mode"] = new_val
    save_settings(CFG)

    if new_val:
        console.print(Panel(
            "[bold green]\u2713 Learn mode ON[/bold green]\n\n"
            "The AI will now:\n"
            "  \u2022 Explain the [bold]why[/bold] behind every step\n"
            "  \u2022 Break solutions into small numbered steps\n"
            "  \u2022 Define jargon and use analogies\n"
            "  \u2022 Guide you instead of doing everything silently\n"
            "  \u2022 Encourage you to try parts yourself\n\n"
            "[dim]Toggle off with [bold]/learn[/bold] or [bold]/learn off[/bold][/dim]",
            title="/learn",
            border_style=SAKURA,
        ))
    else:
        console.print(Panel(
            "[bold]Learn mode OFF[/bold]\n"
            "[dim]Back to standard concise mode. Toggle on with [bold]/learn[/bold][/dim]",
            title="/learn",
            border_style=SAKURA_MUTED,
        ))


register(Command(
    name="/learn",
    handler=_handler,
    usage="/learn [on|off]",
    description="beginner tutorial mode",
    category="General",
))
