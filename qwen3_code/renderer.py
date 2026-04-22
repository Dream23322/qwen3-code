"""Response rendering and streaming (ollama chat loop)."""

import os
import re
import sys
import threading
import time

import ollama
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from qwen3_code.theme import console, SAKURA, SAKURA_DEEP, SAKURA_DARK, SAKURA_MUTED
from qwen3_code.settings import _model, _assistant_name
from qwen3_code.utils import _phys_rows, STREAM_MAX_LINES, PARTIAL_REPROMPT
from qwen3_code.partial import reply_has_partial_write, apply_file_writes, apply_command_runs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _status_line(left: str, right: str) -> Text:
    width       = console.width
    plain_left  = re.sub(r"\[/?[^\]]*\]", "", left)
    pad         = max(1, width - len(plain_left) - len(right))
    line        = Text()
    line.append(_assistant_name(), style=f"bold {SAKURA}")
    line.append("  ")
    line.append(left.replace(_assistant_name() + "  ", ""), style="dim")
    line.append(" " * pad)
    line.append(right, style=f"dim {SAKURA_MUTED}")
    return line


def _watch_for_cancel(cancel_event: threading.Event) -> None:
    """Background thread: set *cancel_event* when Ctrl+D is pressed."""
    try:
        import select as _sel, termios as _t, tty as _tty
        fd  = sys.stdin.fileno()
        old = _t.tcgetattr(fd)
        try:
            _tty.setraw(fd)
            while not cancel_event.is_set():
                r, _, _ = _sel.select([sys.stdin], [], [], 0.05)
                if r and os.read(fd, 1) == b"\x04":
                    cancel_event.set(); break
        finally:
            _t.tcsetattr(fd, _t.TCSADRAIN, old)
    except Exception:
        try:
            import msvcrt  # type: ignore[import]
            while not cancel_event.is_set():
                if msvcrt.kbhit() and msvcrt.getwch() == "\x04":
                    cancel_event.set(); break
                time.sleep(0.05)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render_response(text: str) -> None:
    """Render a completed AI response: prose as Markdown, code as Syntax panels."""
    _CODE_BLOCK_RE = re.compile(r"(```(?:\w+)?\n.*?```)", re.DOTALL)
    for part in _CODE_BLOCK_RE.split(text):
        if not part:
            continue
        if part.startswith("```") and part.endswith("```"):
            m = re.match(r"```(\w+)?\n(.*?)```", part, re.DOTALL)
            if not m:
                console.print(Markdown(part))
                continue
            lang, code    = m.group(1) or "text", m.group(2)
            block_start   = text.find(part)
            prefix        = text[max(0, block_start - 200): block_start]
            wm            = re.search(r"<!--\s*WRITE:\s*([^\s>]+)\s*-->", prefix)
            title, border = (f"Written to {wm.group(1)}", SAKURA_DEEP) if wm else (f"Code ({lang})", SAKURA)
            console.print(Panel(Syntax(code, lang, theme="dracula", line_numbers=True),
                                title=title, border_style=border))
        else:
            cleaned = re.sub(r"<!--\s*WRITE:[^>]+-->", "", part).strip()
            if cleaned:
                console.print(Markdown(cleaned))


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def _raw_stream(
    messages: list[dict],
    cancel_event: threading.Event | None = None,
) -> tuple[str, int]:
    """Stream tokens with a rolling window; returns (full_text, physical_rows)."""
    full: str         = ""
    window: list[str] = []
    partial: str      = ""
    rendered_rows     = 0
    partial_phys      = 0

    def _tw() -> int:
        return max(1, console.width or 80)

    def _phys(t: str) -> int:
        return _phys_rows(t, _tw())

    def _redraw() -> None:
        nonlocal rendered_rows, partial_phys
        if rendered_rows > 1:
            sys.stdout.write(f"\033[{rendered_rows - 1}A")
        sys.stdout.write("\r\033[J")
        for line in window:
            sys.stdout.write(line + "\n")
        sys.stdout.write(partial)
        sys.stdout.flush()
        partial_phys  = _phys(partial)
        rendered_rows = sum(_phys(l) for l in window) + partial_phys

    try:
        for chunk in ollama.chat(model=_model(), messages=messages, stream=True):
            if cancel_event and cancel_event.is_set():
                break
            token = chunk["message"]["content"]
            full += token
            if "\n" in token:
                parts = token.split("\n")
                partial += parts[0]
                for p in [partial] + parts[1:-1]:
                    window.append(p if p != partial else partial)
                    if p == partial:
                        pass
                    if len(window) > STREAM_MAX_LINES:
                        window = window[-STREAM_MAX_LINES:]
                # rebuild properly
                window.clear()
                all_lines = (partial + token[len(parts[0]):]).split("\n") if False else []
                # simpler rebuild:
                rebuilt = (partial.rstrip(parts[0] if partial.endswith(parts[0]) else ""))
                # Just do it correctly:
                before_split = partial  # what we had before this token
                combined     = before_split + token
                all_new      = combined.split("\n")
                for line in all_new[:-1]:
                    window.append(line)
                    if len(window) > STREAM_MAX_LINES:
                        window = window[-STREAM_MAX_LINES:]
                partial = all_new[-1]
                _redraw()
            else:
                new_partial = partial + token
                new_phys    = _phys(new_partial)
                if rendered_rows == 0:
                    sys.stdout.write(new_partial)
                    sys.stdout.flush()
                    partial_phys = new_phys; rendered_rows = new_phys
                else:
                    if partial_phys > 1:
                        sys.stdout.write(f"\033[{partial_phys - 1}A")
                    sys.stdout.write("\r\033[J" + new_partial)
                    sys.stdout.flush()
                    rendered_rows = rendered_rows - partial_phys + new_phys
                    partial_phys  = new_phys
                partial = new_partial
    except Exception as exc:
        if not (cancel_event and cancel_event.is_set()):
            console.print(f"[error]Ollama error: {exc}[/error]")
            console.print(f"[info]  ollama pull {_model()}[/info]")

    return full, rendered_rows


def stream_response(messages: list[dict], cwd: str = "") -> str:
    cancel_event = threading.Event()
    console.print()
    console.print(_status_line("thinking...", "ctrl+d to cancel"))

    watcher = threading.Thread(target=_watch_for_cancel, args=(cancel_event,), daemon=True)
    watcher.start()
    full_reply, window_rows = _raw_stream(messages, cancel_event)

    user_cancelled = cancel_event.is_set()
    cancel_event.set()
    watcher.join(timeout=0.5)

    sys.stdout.write(f"\033[{max(2, window_rows + 1)}A\r\033[J")
    sys.stdout.flush()

    if not full_reply:
        if user_cancelled:
            console.print("[info]Cancelled.[/info]")
        return full_reply

    if user_cancelled:
        console.print("[info]Cancelled \u2014 partial response shown, no files written.[/info]")
        render_response(full_reply)
        return full_reply

    if reply_has_partial_write(full_reply):
        console.print(Panel("[info]Partial file detected. Reprompting...[/info]",
                            title="Partial write", border_style=SAKURA_DARK))
        messages.append({"role": "assistant", "content": full_reply})
        messages.append({"role": "user",      "content": PARTIAL_REPROMPT})
        rc = threading.Event()
        console.print()
        console.print(_status_line("retrying...", "ctrl+d to cancel"))
        rw = threading.Thread(target=_watch_for_cancel, args=(rc,), daemon=True)
        rw.start()
        rr, rw_rows = _raw_stream(messages, rc)
        rc_cancelled = rc.is_set()
        rc.set(); rw.join(timeout=0.5)
        sys.stdout.write(f"\033[{max(2, rw_rows + 1)}A\r\033[J")
        sys.stdout.flush()
        messages.pop(); messages.pop()
        if rr and not rc_cancelled:
            full_reply = rr

    render_response(full_reply)
    apply_file_writes(full_reply)
    apply_command_runs(full_reply, cwd, messages)
    return full_reply
