#!/usr/bin/env python3
"""Entry point for qwen3-code.

All logic lives in the qwen3_code/ package.  This file is intentionally tiny
so that smaller models only need to read main.py to understand the high-level
flow, and can then navigate to the relevant module for details.

Package layout
--------------
  qwen3_code/
    settings.py   - user config (CFG, load/save, /settings handler)
    theme.py      - colour palette and shared Rich console
    utils.py      - constants, filesystem helpers, animated spinner
    session.py    - conversation persistence (load/save per directory)
    vc.py         - git-like version control (commits, undo, redo, checkout)
    refresh.py    - /refresh command (reload files, prune stale context)
    partial.py    - partial-write detection, apply_file_writes, apply_command_runs
    completer.py  - fuzzy hint completions and raw-mode inline prompt
    renderer.py   - streaming response renderer (ollama chat loop)
    commands.py   - all /slash command handlers and dispatcher
"""

import os
import sys
from pathlib import Path

from qwen3_code.theme import console, SAKURA, SAKURA_DEEP
from qwen3_code.settings import _model, _app_name, CFG
from qwen3_code.utils import VC_DIR, SESSION_DIR, build_context_snippet, _short_cwd
from qwen3_code.session import load_session, save_session
from qwen3_code.completer import inline_prompt, enable_windows_vt
from qwen3_code.commands import handle_slash_command
from qwen3_code.renderer import stream_response


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="qwen3-code")
    parser.add_argument("dir", nargs="?", default=None)
    parser.add_argument("--dir", "-d", dest="dir_flag", default=None, metavar="DIR")
    args = parser.parse_args()

    raw_dir = args.dir or args.dir_flag
    if raw_dir is not None:
        target = Path(raw_dir).expanduser().resolve()
        if not target.is_dir():
            print(f"[error] Not a directory: {raw_dir}")
            sys.exit(1)
        os.chdir(target)

    enable_windows_vt()
    VC_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    initial_cwd = os.getcwd()
    state       = {"cwd": initial_cwd, "first_message": True, "pending_context": []}

    from rich.panel import Panel
    console.print(Panel(
        f"[bold {SAKURA_DEEP}]{_app_name()}[/bold {SAKURA_DEEP}]  -  simple coding assistant TUI\n"
        f"Model : [{SAKURA}]{_model()}[/{SAKURA}]\n"
        f"CWD   : [{SAKURA}]{initial_cwd}[/{SAKURA}]\n\n"
        f"Type [{SAKURA_DEEP}]/help[/{SAKURA_DEEP}] for commands, "
        f"[{SAKURA_DEEP}]/settings[/{SAKURA_DEEP}] to configure, "
        f"[{SAKURA_DEEP}]/quit[/{SAKURA_DEEP}] to exit.",
        border_style=SAKURA, title=_app_name(),
    ))

    messages: list[dict] = load_session(initial_cwd)
    if any(m["role"] != "system" for m in messages):
        state["first_message"] = False

    _input_history: list[str] = []

    while True:
        cwd   = state["cwd"]
        short = _short_cwd(cwd)
        try:
            user_input = inline_prompt(f"you ({short}): ", cwd, _input_history)
        except (KeyboardInterrupt, EOFError):
            console.print("\n[info]Goodbye.[/info]")
            save_session(cwd, messages)
            break

        user_input = user_input.strip()
        if not user_input:
            continue
        if not _input_history or _input_history[-1] != user_input:
            _input_history.append(user_input)

        if user_input.startswith("/"):
            if not handle_slash_command(user_input, messages, state):
                save_session(state["cwd"], messages)
                break
            continue

        content = (build_context_snippet(cwd) + "\n\n" + user_input) if state["first_message"] else user_input
        state["first_message"] = False

        pending = state.get("pending_context", [])
        if pending:
            content = "\n\n".join(pending) + "\n\n" + content
            state["pending_context"] = []

        messages.append({"role": "user", "content": content})
        reply = stream_response(messages, cwd)
        if reply:
            messages.append({"role": "assistant", "content": reply})
            save_session(cwd, messages)


if __name__ == "__main__":
    main()
