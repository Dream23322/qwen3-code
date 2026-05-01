"""Response rendering and streaming (ollama chat loop)."""

import os
import re
import sys
import threading
import time
from pathlib import Path

import ollama
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from qwen3_code.theme import console, SAKURA, SAKURA_DEEP, SAKURA_DARK, SAKURA_MUTED
from qwen3_code.settings import _model, _assistant_name, _learn_mode, _navi_mode
from qwen3_code.utils import (
    _phys_rows, STREAM_MAX_LINES, PARTIAL_REPROMPT,
    resolve_path, read_file, build_system_prompt, spinning_dots,
)
from qwen3_code.partial import (
    reply_has_partial_write, apply_file_writes, apply_file_inserts, apply_command_runs,
    has_read_requests, collect_read_requests, has_inserts, parse_attrs,
)
from qwen3_code.navi import select_tools_for_task

# ---------------------------------------------------------------------------
# Learn-mode system message
# ---------------------------------------------------------------------------

_LEARN_SYSTEM_MSG: dict = {
    "role": "system",
    "content": (
        "LEARN MODE is active. The user is a beginner learning to code. "
        "Follow these rules for every response:\n"
        "\n"
        "1. Explain the WHY behind every step \u2014 don't just show the what.\n"
        "2. Break solutions into small, numbered steps the user can follow.\n"
        "3. Define any technical terms or jargon the first time you use them.\n"
        "4. Use simple analogies to make abstract concepts concrete.\n"
        "5. When writing code, briefly explain what each key part does immediately after.\n"
        "6. Don't silently do everything for the user \u2014 if a step is simple enough, "
        "describe it and let them try first, then provide the answer.\n"
        "7. Encourage curiosity: point out what they could explore or experiment with next.\n"
        "8. Keep a friendly, patient, non-condescending tone.\n"
        "9. If the user's question is unclear, ask one focused clarifying question "
        "instead of assuming.\n"
        "10. After answering, offer a quick comprehension check (e.g. \"Does that make sense? "
        "Try running it and let me know what you see.\")."
    ),
}


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

# Match any rendered "block": new <q*> tags first, then legacy fences last.
_BLOCK_RE: re.Pattern = re.compile(
    r"<qwrite\s+(?P<wattrs>[^>]*?)>\s*\n?(?P<wcode>.*?)</qwrite>"
    r"|<qinsert\s+(?P<iattrs>[^>]*?)>\s*\n?(?P<icode>.*?)</qinsert>"
    r"|<qcode(?:\s+(?P<cattrs>[^>]*?))?\s*>\s*\n?(?P<ccode>.*?)</qcode>"
    r"|(?P<legacy>```(?:\w+)?\n.*?```)",
    re.DOTALL | re.IGNORECASE,
)

_LEGACY_FENCE_INNER: re.Pattern = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)


def _render_prose_segment(part: str) -> None:
    """Strip action markers from *part* and render whatever prose remains."""
    cleaned = re.sub(r"<!--\s*WRITE:[^>]+-->",  "", part)
    cleaned = re.sub(r"<!--\s*INSERT:[^>]+-->", "", cleaned)
    cleaned = re.sub(r"<!--\s*READ:[^>]+-->",   "", cleaned)
    cleaned = re.sub(r"<!--\s*RUN:[^>]+-->",    "", cleaned)
    cleaned = re.sub(r"<qread\s+[^/>]*?/\s*>",  "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"<qrun\s*>.*?</qrun>", "", cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = cleaned.strip()
    if cleaned:
        console.print(Markdown(cleaned))


def _code_panel(code: str, lang: str, title: str, border: str) -> Panel:
    return Panel(
        Syntax(code, lang or "text", theme="dracula", line_numbers=True),
        title=title,
        border_style=border,
    )


def render_response(text: str) -> None:
    """Render a completed AI response: prose as Markdown, code as Syntax panels."""
    last_end: int = 0
    for m in _BLOCK_RE.finditer(text):
        if m.start() > last_end:
            _render_prose_segment(text[last_end:m.start()])

        if m.group("wcode") is not None:
            attrs = parse_attrs(m.group("wattrs"))
            path  = attrs.get("path", "").strip() or "(unknown)"
            lang  = attrs.get("lang", "").strip() or "text"
            console.print(_code_panel(
                m.group("wcode"), lang,
                f"Written to {path}", SAKURA_DEEP,
            ))
        elif m.group("icode") is not None:
            attrs = parse_attrs(m.group("iattrs"))
            path  = attrs.get("path", "").strip() or "(unknown)"
            line  = attrs.get("line", "").strip() or "?"
            lang  = attrs.get("lang", "").strip() or "text"
            console.print(_code_panel(
                m.group("icode"), lang,
                f"Insert into {path} (before line {line})", SAKURA,
            ))
        elif m.group("ccode") is not None:
            attrs = parse_attrs(m.group("cattrs") or "")
            lang  = attrs.get("lang", "").strip() or "text"
            console.print(_code_panel(
                m.group("ccode"), lang,
                f"Code ({lang})", SAKURA,
            ))
        else:
            block = m.group("legacy") or ""
            inner = _LEGACY_FENCE_INNER.match(block)
            if not inner:
                console.print(Markdown(block))
            else:
                lang = inner.group(1) or "text"
                code = inner.group(2)
                prefix = text[max(0, m.start() - 200): m.start()]
                wm = re.search(r"<!--\s*WRITE:\s*([^\s>]+)\s*-->", prefix)
                im = re.search(r"<!--\s*INSERT:\s*([^\s>:]+):(\d+)\s*-->", prefix)
                if wm:
                    title, border = f"Written to {wm.group(1)}", SAKURA_DEEP
                elif im:
                    title  = f"Insert into {im.group(1)} (before line {im.group(2)})"
                    border = SAKURA
                else:
                    title, border = f"Code ({lang})", SAKURA
                console.print(_code_panel(code, lang, title, border))

        last_end = m.end()

    if last_end < len(text):
        _render_prose_segment(text[last_end:])


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def _raw_stream(
    messages: list[dict],
    cancel_event: threading.Event | None = None,
) -> tuple[str, int]:
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
                before_split = partial
                combined     = before_split + token
                all_new      = combined.split("\n")
                window.clear()
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


def _do_stream(
    messages: list[dict],
    status_label: str = "thinking...",
) -> tuple[str, bool]:
    cancel_event = threading.Event()
    console.print()
    console.print(_status_line(status_label, "ctrl+d to cancel"))
    watcher = threading.Thread(target=_watch_for_cancel, args=(cancel_event,), daemon=True)
    watcher.start()
    reply, rows = _raw_stream(messages, cancel_event)
    cancelled = cancel_event.is_set()
    cancel_event.set()
    watcher.join(timeout=0.5)
    sys.stdout.write(f"\033[{max(2, rows + 1)}A\r\033[J")
    sys.stdout.flush()
    return reply, cancelled


def _effective_messages(messages: list[dict]) -> list[dict]:
    """Return messages with learn-mode system message prepended when active."""
    if not _learn_mode():
        return messages
    # Don't double-insert if already present
    if messages and messages[0].get("content") == _LEARN_SYSTEM_MSG["content"]:
        return messages
    return [_LEARN_SYSTEM_MSG] + messages


# ---------------------------------------------------------------------------
# Navi: per-turn slim system prompt
# ---------------------------------------------------------------------------

def _with_navi_system(messages: list[dict], slim: str | None) -> list[dict]:
    """If *slim* is set, drop existing system messages and prepend the slim one.

    This builds a NEW list -- the caller's `messages` is not mutated, so the
    persisted session keeps the full system prompt even after navi runs.
    """
    if slim is None:
        return messages
    non_system: list[dict] = [m for m in messages if m.get("role") != "system"]
    return [{"role": "system", "content": slim}] + non_system


def _compute_navi_system(messages: list[dict]) -> str | None:
    """Run the router once for the latest user message and return a slim prompt.

    Returns None if there's no user message to route on.
    """
    user_msg: str = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_msg = m.get("content", "")
            break
    if not user_msg:
        return None

    with spinning_dots("navi: routing"):
        summary, tools = select_tools_for_task(user_msg)

    tools_label: str = ", ".join(sorted(tools)) if tools else "(none)"
    console.print(Panel(
        f"[bold]Task:[/bold] {summary}\n"
        f"[bold]Tools:[/bold] {tools_label}",
        title="navi",
        border_style=SAKURA_DEEP,
    ))
    return build_system_prompt(tools)


def stream_response(messages: list[dict], cwd: str = "") -> str:
    """Stream a response from the model.

    Handles:
    - Ctrl+D cancel
    - Partial-write detection and reprompt
    - <qread path="..." /> markers (and legacy <!-- READ: --> markers)
    - <qwrite path="..."> markers (and legacy <!-- WRITE: --> markers)
    - <qinsert path="..." line="N"> markers (and legacy <!-- INSERT: --> markers)
    - <qrun>cmd</qrun> markers (and legacy <!-- RUN: --> markers)
    - Learn mode (prepends beginner-friendly system message)
    - Navi mode (replaces full system prompt with a slim per-turn one)
    """
    # ---- Navi: pick the slim system prompt for THIS turn -----------------
    navi_system: str | None = _compute_navi_system(messages) if _navi_mode() else None

    def _effective(msgs: list[dict]) -> list[dict]:
        return _effective_messages(_with_navi_system(msgs, navi_system))

    full_reply, user_cancelled = _do_stream(_effective(messages))

    if not full_reply:
        if user_cancelled:
            console.print("[info]Cancelled.[/info]")
        return full_reply

    if user_cancelled:
        console.print("[info]Cancelled \u2014 partial response shown, no files written.[/info]")
        render_response(full_reply)
        return full_reply

    # ---- Partial-write reprompt ------------------------------------------
    if reply_has_partial_write(full_reply):
        console.print(Panel("[info]Partial file detected. Reprompting...[/info]",
                            title="Partial write", border_style=SAKURA_DARK))
        messages.append({"role": "assistant", "content": full_reply})
        messages.append({"role": "user",      "content": PARTIAL_REPROMPT})
        rr, rc_cancelled = _do_stream(_effective(messages), "retrying...")
        messages.pop()
        messages.pop()
        if rc_cancelled:
            console.print("[info]Cancelled \u2014 partial response shown, no files written.[/info]")
            render_response(rr or full_reply)
            return rr or full_reply
        if rr:
            full_reply = rr

    # ---- READ request loop -----------------------------------------------
    _MAX_READ_ROUNDS = 3
    for _rnd in range(_MAX_READ_ROUNDS):
        if not has_read_requests(full_reply):
            break
        req_paths = collect_read_requests(full_reply)
        file_blocks: list[str] = []
        for rp in req_paths:
            resolved = resolve_path(rp, cwd) if cwd else rp
            content  = read_file(resolved)
            file_blocks.append(
                f"Here is the content of `{resolved}`:\n\n"
                f"<qcode lang=\"text\">\n{content}\n</qcode>"
            )
        if not file_blocks:
            break
        names = ", ".join(Path(p).name for p in req_paths)
        console.print(Panel(
            f"[info]AI requested {len(file_blocks)} file(s): [bold]{names}[/bold]\nLoading and reprompting\u2026[/info]",
            title="File read request", border_style=SAKURA_DEEP,
        ))
        messages.append({"role": "assistant", "content": full_reply})
        messages.append({
            "role": "user",
            "content": "Files you requested:\n\n" + "\n\n".join(file_blocks),
        })
        new_reply, was_cancelled = _do_stream(_effective(messages), "reading files...")
        if was_cancelled or not new_reply:
            if was_cancelled:
                console.print("[info]Cancelled.[/info]")
            messages.pop()
            messages.pop()
            break
        full_reply = new_reply

    # ---- Final render + apply --------------------------------------------
    render_response(full_reply)
    apply_file_writes(full_reply)
    apply_file_inserts(full_reply, cwd)
    apply_command_runs(full_reply, cwd, messages)
    return full_reply
