#!/usr/bin/env python3
"""
qwen3-code: A simple Claude Code-style TUI powered by Ollama + huihui_ai/qwen3-coder-abliterated:30b
"""

import os
import sys
import json
import subprocess
import textwrap
from pathlib import Path

try:
    import ollama
except ImportError:
    print("[error] ollama package not found. Run: pip install ollama")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.theme import Theme
    from rich.text import Text
except ImportError:
    print("[error] rich package not found. Run: pip install rich")
    sys.exit(1)

# ── Constants ──────────────────────────────────────────────────────────────────

MODEL: str = "huihui_ai/qwen3-coder-abliterated:30b"
SYSTEM_PROMPT: str = textwrap.dedent("""\
    You are an expert software engineer assistant embedded in a terminal.
    You help the user understand, write, debug, and refactor code.
    When showing code, always wrap it in fenced code blocks with the correct language tag.
    Be concise and direct. Prefer targeted, minimal changes.
    If asked to run a shell command, explain what it does first.
""").strip()

# ── UI setup ──────────────────────────────────────────────────────────────────

custom_theme: Theme = Theme({
    "user":      "bold cyan",
    "assistant": "bold green",
    "system":    "dim yellow",
    "error":     "bold red",
    "info":      "dim white",
})

console: Console = Console(theme=custom_theme)

# ── Helpers ───────────────────────────────────────────────────────────────────

def read_file(path: str) -> str:
    """Read a local file and return its contents as a string."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception as exc:
        return f"[could not read file: {exc}]"


def run_command(cmd: str) -> str:
    """Run a shell command and return combined stdout + stderr."""
    try:
        result: subprocess.CompletedProcess = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output: str = result.stdout + result.stderr
        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return "[command timed out after 30 s]"
    except Exception as exc:
        return f"[command error: {exc}]"


def build_context_snippet(cwd: str) -> str:
    """Return a small context header injected into the first user message."""
    try:
        files: list[str] = [
            f.name for f in Path(cwd).iterdir()
            if f.is_file() and not f.name.startswith(".")
        ][:20]
    except Exception:
        files = []

    lines: list[str] = [
        f"Working directory: {cwd}",
        f"Visible files: {', '.join(files) if files else 'none'}",
    ]

    return "\n".join(lines)


def handle_slash_command(cmd: str, messages: list[dict]) -> bool:
    """
    Handle /commands typed by the user.
    Returns True if the main loop should continue, False to quit.
    """
    parts: list[str] = cmd.strip().split(maxsplit=1)
    name: str = parts[0].lower()
    arg: str = parts[1] if len(parts) > 1 else ""

    if name in ("/quit", "/exit", "/q"):
        console.print("[info]Goodbye![/info]")
        return False

    elif name == "/clear":
        messages.clear()
        console.clear()
        console.print("[info]Conversation cleared.[/info]")

    elif name == "/read":
        if not arg:
            console.print("[error]Usage: /read <filepath>[/error]")
        else:
            content: str = read_file(arg)
            snippet: str = f"Here is the content of `{arg}`:\n\n```\n{content}\n```"
            messages.append({"role": "user", "content": snippet})
            console.print(f"[info]Loaded {arg} into context.[/info]")

    elif name == "/run":
        if not arg:
            console.print("[error]Usage: /run <shell command>[/error]")
        else:
            output: str = run_command(arg)
            snippet: str = f"Output of `{arg}`:\n\n```\n{output}\n```"
            messages.append({"role": "user", "content": snippet})
            console.print(Panel(output, title=f"$ {arg}", border_style="yellow"))

    elif name == "/history":
        for i, m in enumerate(messages):
            role: str = m["role"]
            preview: str = m["content"][:120].replace("\n", " ")
            console.print(f"[info][{i}] {role}: {preview}[/info]")

    elif name == "/help":
        help_text: str = textwrap.dedent("""\
            Available commands:
              /read <file>   — load a file into the conversation context
              /run <cmd>     — run a shell command and add output to context
              /clear         — clear conversation history
              /history       — show message history
              /help          — show this help
              /quit          — exit

            Just type normally to chat with the model.
        """)
        console.print(Panel(help_text, title="Help", border_style="cyan"))

    else:
        console.print(f"[error]Unknown command: {name}. Type /help for a list.[/error]")

    return True


def stream_response(messages: list[dict]) -> str:
    """
    Stream a response from Ollama and return the full assistant reply.
    """
    full_reply: str = ""

    console.print()
    console.print(Text("assistant", style="assistant"), end="  ")

    try:
        stream = ollama.chat(
            model=MODEL,
            messages=messages,
            stream=True,
        )

        for chunk in stream:
            delta: str = chunk["message"]["content"]
            full_reply += delta
            console.print(delta, end="", markup=False)

        console.print()
        console.print()

    except Exception as exc:
        console.print(f"\n[error]Ollama error: {exc}[/error]")
        console.print("[info]Make sure Ollama is running and the model is pulled:[/info]")
        console.print(f"[info]  ollama pull {MODEL}[/info]")

    return full_reply


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    cwd: str = os.getcwd()

    console.print(Panel(
        f"[bold]qwen3-code[/bold]  —  simple coding assistant TUI\n"
        f"Model : [cyan]{MODEL}[/cyan]\n"
        f"CWD   : [cyan]{cwd}[/cyan]\n\n"
        f"Type [cyan]/help[/cyan] for commands, [cyan]/quit[/cyan] to exit.",
        border_style="green",
    ))

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

    first_message: bool = True

    while True:
        try:
            user_input: str = Prompt.ask("[user]you[/user]")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[info]Exiting.[/info]")
            break

        user_input = user_input.strip()

        if not user_input:
            continue

        # Slash commands
        if user_input.startswith("/"):
            if not handle_slash_command(user_input, messages):
                break
            continue

        # Prepend a small context snippet to the very first user message
        if first_message:
            context: str = build_context_snippet(cwd)
            content: str = f"{context}\n\n{user_input}"
            first_message = False
        else:
            content = user_input

        messages.append({"role": "user", "content": content})

        reply: str = stream_response(messages)

        if reply:
            messages.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    main()
