"""Session persistence: save and restore conversation history per directory."""

import hashlib
import json
from datetime import datetime
from pathlib import Path

from qwen3_code.theme import console, SAKURA
from qwen3_code.utils import SESSION_DIR, SYSTEM_PROMPT
from qwen3_code.settings import CFG

# File that records the most-recently-used working directory.
_LAST_CWD_FILE: Path = SESSION_DIR / "_last_cwd.txt"


def _session_path(cwd: str) -> Path:
    """Return a collision-free session file path for *cwd*.

    Uses a SHA-1 hash of the absolute path so that directories whose names
    share substrings (e.g. '/proj/my_src' vs '/proj/my/src') never clash.
    """
    h = hashlib.sha1(cwd.encode("utf-8")).hexdigest()[:16]
    return SESSION_DIR / f"{h}.json"


def save_last_cwd(cwd: str) -> None:
    """Persist *cwd* as the most-recently-used directory."""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    _LAST_CWD_FILE.write_text(cwd, encoding="utf-8")


def load_last_cwd() -> str | None:
    """Return the last saved CWD, or *None* if none exists / directory is gone."""
    if not _LAST_CWD_FILE.exists():
        return None
    try:
        cwd = _LAST_CWD_FILE.read_text(encoding="utf-8").strip()
        return cwd if cwd and Path(cwd).is_dir() else None
    except Exception:
        return None


def save_session(cwd: str, messages: list[dict]) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    non_system = [m for m in messages if m.get("role") != "system"]
    data = {
        "cwd":      cwd,
        "saved_at": datetime.now().isoformat(),
        "messages": non_system,
    }
    _session_path(cwd).write_text(json.dumps(data, indent=2), encoding="utf-8")
    save_last_cwd(cwd)


def load_session(cwd: str) -> list[dict]:
    from rich.panel import Panel
    base: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if not CFG.get("open_from_last_session", True):
        return base
    path = _session_path(cwd)
    if not path.exists():
        return base
    try:
        data  = json.loads(path.read_text(encoding="utf-8"))
        saved = data.get("messages", [])
        # Sanity-check: session must belong to this exact cwd
        if data.get("cwd") != cwd:
            return base
        console.print(Panel(
            f"[info]Resumed session for [bold]{cwd}[/bold]\n"
            f"{len(saved)} message(s) from {data.get('saved_at', '?')}[/info]",
            title="Session loaded", border_style=SAKURA,
        ))
        return base + saved
    except Exception as exc:
        console.print(f"[error]Could not load session: {exc}[/error]")
        return base
