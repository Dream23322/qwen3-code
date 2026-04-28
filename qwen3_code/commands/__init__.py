"""Slash-command package: Command class, registry, and dispatcher.

Each command lives in its own module under ``qwen3_code/commands/`` (e.g.
``commands/help.py``) and registers itself by calling :func:`register` with
a :class:`Command` instance.  Adding a new command means dropping a single
new file in this package and adding it to the import list below -- no need
to touch a giant ``elif`` chain.

The public surface (``handle_slash_command``) is unchanged so ``main.py``
continues to work as before.
"""

from dataclasses import dataclass
from typing import Callable

from qwen3_code.theme import console


# Handler signature: (arg, messages, state) -> bool | None
# Returning False signals "exit the REPL"; anything else continues the loop.
CommandHandler = Callable[[str, list, dict], object]


@dataclass(frozen=True)
class Command:
    """Metadata + handler for a single slash command."""

    name:        str
    handler:     CommandHandler
    usage:       str             = ""
    description: str             = ""
    category:    str             = "General"
    aliases:     tuple           = ()


_REGISTRY: dict[str, Command] = {}
_ORDER:    list[str]          = []   # insertion order of canonical names


def register(cmd: Command) -> Command:
    """Register a command (called at import time by each command module)."""
    canonical: str = cmd.name.lower()
    if canonical not in _REGISTRY:
        _ORDER.append(canonical)
    _REGISTRY[canonical] = cmd
    for alias in cmd.aliases:
        _REGISTRY[alias.lower()] = cmd

    return cmd


def get_command(name: str) -> Command | None:
    return _REGISTRY.get(name.lower())


def all_commands() -> list[Command]:
    """Return registered commands in registration order, deduplicated."""
    seen: set[str]      = set()
    out:  list[Command] = []
    for canonical in _ORDER:
        cmd = _REGISTRY.get(canonical)
        if cmd is None or cmd.name in seen:
            continue
        seen.add(cmd.name)
        out.append(cmd)

    return out


def commands_by_category() -> dict[str, list[Command]]:
    grouped: dict[str, list[Command]] = {}
    for cmd in all_commands():
        grouped.setdefault(cmd.category, []).append(cmd)

    return grouped


# ---------------------------------------------------------------------------
# Side-effect imports: each submodule registers its command on import.
# Order here controls /help row order within a category.
# ---------------------------------------------------------------------------

from qwen3_code.commands import (  # noqa: E402, F401
    cd,
    read,
    tree,
    v,
    loadtree,
    context,
    refresh,
    run,
    plan,
    council,
    learn,
    clear,
    check,
    stackview,
    settings,
    history,
    help,
    quit,
    # Version-control commands
    undo,
    redo,
    checkout,
    commit,
    log,
    files,
)


def handle_slash_command(cmd: str, messages: list[dict], state: dict) -> bool:
    """Dispatch a slash command. Returns False to signal exit."""
    parts: list[str] = cmd.strip().split(maxsplit=1)
    if not parts:
        return True

    name: str = parts[0].lower()
    arg:  str = parts[1] if len(parts) > 1 else ""

    found: Command | None = get_command(name)
    if found is None:
        console.print(f"[error]Unknown command: {name}[/error]")
        return True

    result = found.handler(arg, messages, state)

    return False if result is False else True


__all__ = [
    "Command",
    "CommandHandler",
    "register",
    "get_command",
    "all_commands",
    "commands_by_category",
    "handle_slash_command",
]
