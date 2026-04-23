"""Tab-completion, fuzzy hint completions, and the raw-input prompt loop."""

import os
import sys
from pathlib import Path

from qwen3_code.theme import console, SAKURA_DEEP
from qwen3_code.settings import DEFAULT_SETTINGS

try:
    from prompt_toolkit.completion import Completer, Completion
    _PT_AVAILABLE = True
except ImportError:
    _PT_AVAILABLE = False

# ---------------------------------------------------------------------------
# Command registry
# ---------------------------------------------------------------------------

SLASH_COMMANDS: list[str] = [
    "/cd", "/read", "/refresh", "/run", "/undo", "/redo", "/files",
    "/clear", "/check", "/stackview", "/settings", "/history", "/help",
    "/commit", "/log", "/checkout",
    "/tree", "/loadtree", "/plan", "/v",
    "/quit", "/exit", "/q",
]

CMD_SUBARGS: dict[str, list[str]] = {
    "/stackview": ["fh", "fhf", "sessions", "sess", "env", "environment", "help"],
    "/check":     ["ALL"],
    "/read":      ["-a"],
    "/settings":  list(DEFAULT_SETTINGS.keys()),
    "/tree":      ["-i"],
    "/loadtree":  ["-i", "-d", "-i -d", "-d -i"],
    "/v":         ["-i"],
}

FILE_COMMANDS: set[str] = {"/read", "/check", "/undo", "/redo", "/files",
                            "/cd", "/commit", "/log", "/checkout"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fuzzy_match(query: str, candidate: str) -> bool:
    q, c, qi = query.lower(), candidate.lower(), 0
    for ch in c:
        if qi < len(q) and ch == q[qi]:
            qi += 1
    return qi == len(q)


def get_fuzzy_completions(text: str, cwd: str) -> list[str]:
    if not text.startswith("/"):
        return []
    parts     = text.split(maxsplit=1)
    typed_cmd = parts[0]
    is_exact  = typed_cmd.lower() in {c.lower() for c in SLASH_COMMANDS}
    if len(parts) == 1 or not is_exact:
        return [c for c in SLASH_COMMANDS if c.startswith(typed_cmd) or fuzzy_match(typed_cmd, c)]
    cmd, arg = typed_cmd.lower(), parts[1]
    results: list[str] = []
    if cmd in CMD_SUBARGS:
        for sub in CMD_SUBARGS[cmd]:
            if sub.lower().startswith(arg.lower()) or (arg and fuzzy_match(arg, sub)):
                results.append(f"{typed_cmd} {sub}")
    if not results and cmd in FILE_COMMANDS and not arg.startswith("-"):
        if cmd == "/cd":
            try:
                last_sep = max(arg.rfind("/"), arg.rfind("\\"))
                if last_sep >= 0:
                    tp, frag = arg[:last_sep+1], arg[last_sep+1:]
                else:
                    tp, frag = "", arg
                search = (Path(cwd) / tp).resolve() if tp else Path(cwd).resolve()
                for e in sorted(search.iterdir()):
                    if not e.is_dir() or e.name.startswith("."):
                        continue
                    if not frag or e.name.lower().startswith(frag.lower()) or fuzzy_match(frag, e.name):
                        results.append(f"{typed_cmd} {tp}{e.name}")
                        if len(results) >= 7:
                            break
            except Exception:
                pass
        else:
            results.append(f"{typed_cmd} <file>")
    return results


# ---------------------------------------------------------------------------
# prompt_toolkit completer
# ---------------------------------------------------------------------------

if _PT_AVAILABLE:
    class SlashCompleter(Completer):  # type: ignore[misc]
        def __init__(self, cwd_getter):
            self._cwd = cwd_getter

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if not text.startswith("/"):
                return
            parts    = text.split(maxsplit=1)
            typed    = parts[0]
            is_exact = typed.lower() in {c.lower() for c in SLASH_COMMANDS}
            if len(parts) == 1 or not is_exact:
                for cmd in SLASH_COMMANDS:
                    if cmd.startswith(typed) or fuzzy_match(typed, cmd):
                        yield Completion(cmd, start_position=-len(text.rstrip()))
                return
            cmd = typed.lower()
            arg = parts[1]
            if cmd in CMD_SUBARGS:
                for sub in CMD_SUBARGS[cmd]:
                    if sub.lower().startswith(arg.lower()) or (arg and fuzzy_match(arg, sub)):
                        yield Completion(sub, start_position=-len(arg))
            if cmd in FILE_COMMANDS and not arg.startswith("-"):
                cwd = self._cwd()
                try:
                    base = Path(cwd)
                    if "/" in arg:
                        prefix = arg[:arg.rfind("/")+1]
                        frag   = arg[arg.rfind("/")+1:]
                        sdir   = (base / prefix).resolve()
                    else:
                        prefix, frag, sdir = "", arg, base
                    for e in sorted(sdir.iterdir()):
                        if e.name.startswith("."):
                            continue
                        tail = e.name + ("/" if e.is_dir() else "")
                        if e.name.lower().startswith(frag.lower()) or (frag and fuzzy_match(frag, e.name)):
                            yield Completion(prefix + tail, start_position=-len(arg))
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Windows VT enable
# ---------------------------------------------------------------------------

def enable_windows_vt() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes, ctypes.wintypes
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        k32.GetStdHandle.restype = ctypes.wintypes.HANDLE
        for std_fd in (-10, -11, -12):
            handle = k32.GetStdHandle(ctypes.c_ulong(std_fd))
            mode   = ctypes.wintypes.DWORD(0)
            if k32.GetConsoleMode(handle, ctypes.byref(mode)):
                k32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Inline prompt
# ---------------------------------------------------------------------------

def inline_prompt(prompt_str: str, cwd: str, history: list[str]) -> str:
    """Raw-mode prompt with one-line fuzzy hint above and arrow-key history."""
    enable_windows_vt()

    BOLD  = "\033[1m"
    DIM   = "\033[2m"
    CYAN  = (f"\033[38;2;{int(SAKURA_DEEP[1:3],16)};"
             f"{int(SAKURA_DEEP[3:5],16)};{int(SAKURA_DEEP[5:7],16)}m")
    RESET = "\033[0m"
    UP_CLEAR   = "\033[1A\r\033[2K"
    LINE_CLEAR = "\r\033[2K"

    buf: list[str]       = []
    hist_idx: int        = len(history)
    saved_buf: list[str] = []

    def _text() -> str:
        return "".join(buf)

    def _build_hint(text: str) -> str:
        matches = get_fuzzy_completions(text, cwd)
        if not matches:
            return ""
        tw   = max(40, (console.width or 80) - 2)
        out: list[str] = []
        plen = 0
        for i, m in enumerate(matches[:7]):
            sep = "  " if out else ""
            if plen + len(sep) + len(m) > tw:
                break
            out.append(f"{BOLD}{CYAN}{m}{RESET}" if i == 0 else f"{DIM}{m}{RESET}")
            plen += len(sep) + len(m)
        return "  ".join(out)

    def _tab_complete(text: str) -> str:
        matches = get_fuzzy_completions(text, cwd)
        if not matches:
            return text
        best = matches[0]
        return best + " " if best in SLASH_COMMANDS else best

    def _render(text: str) -> None:
        hint = _build_hint(text)
        sys.stdout.write(UP_CLEAR + hint + "\n" + LINE_CLEAR + prompt_str + text)
        sys.stdout.flush()

    def _clear_hint() -> None:
        sys.stdout.write(UP_CLEAR + "\n" + LINE_CLEAR)
        sys.stdout.flush()

    sys.stdout.write("\n" + prompt_str)
    sys.stdout.flush()

    try:
        if sys.platform == "win32":
            import msvcrt  # type: ignore[import]
            while True:
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    ch2 = msvcrt.getwch()
                    if ch2 == "H" and hist_idx > 0:
                        if hist_idx == len(history):
                            saved_buf = buf[:]
                        hist_idx -= 1
                        buf[:] = list(history[hist_idx])
                    elif ch2 == "P" and hist_idx < len(history):
                        hist_idx += 1
                        buf[:] = list(history[hist_idx] if hist_idx < len(history) else saved_buf)
                    _render(_text()); continue
                if ch in ("\r", "\n"):
                    _clear_hint(); sys.stdout.write(prompt_str + _text() + "\n"); sys.stdout.flush(); break
                elif ch == "\x03": _clear_hint(); sys.stdout.write("\n"); sys.stdout.flush(); raise KeyboardInterrupt
                elif ch == "\x04": _clear_hint(); sys.stdout.write("\n"); sys.stdout.flush(); raise EOFError
                elif ch in ("\x08", "\x7f"):
                    if buf: buf.pop()
                elif ch == "\t":
                    buf[:] = list(_tab_complete(_text()))
                else:
                    buf.append(ch)
                _render(_text())
        else:
            import tty, termios, select as _sel  # noqa: E401
            fd  = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                while True:
                    _sel.select([sys.stdin], [], [])
                    raw = os.read(fd, 1)
                    if raw in (b"\r", b"\n"):
                        _clear_hint(); sys.stdout.write(prompt_str + _text() + "\n"); sys.stdout.flush(); break
                    elif raw == b"\x03": _clear_hint(); sys.stdout.write("\n"); sys.stdout.flush(); raise KeyboardInterrupt
                    elif raw == b"\x04": _clear_hint(); sys.stdout.write("\n"); sys.stdout.flush(); raise EOFError
                    elif raw in (b"\x08", b"\x7f"):
                        if buf: buf.pop()
                    elif raw == b"\t":
                        buf[:] = list(_tab_complete(_text()))
                    elif raw == b"\x1b":
                        rest = os.read(fd, 2)
                        if rest == b"[A" and hist_idx > 0:
                            if hist_idx == len(history): saved_buf = buf[:]
                            hist_idx -= 1; buf[:] = list(history[hist_idx])
                        elif rest == b"[B" and hist_idx < len(history):
                            hist_idx += 1
                            buf[:] = list(history[hist_idx] if hist_idx < len(history) else saved_buf)
                    else:
                        try: buf.append(raw.decode("utf-8"))
                        except Exception: pass
                    _render(_text())
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (KeyboardInterrupt, EOFError):
        raise
    except Exception:
        return input(prompt_str)
    return _text()
