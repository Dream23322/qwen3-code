"""Session persistence: save and restore conversation history per directory."""

import json
import re
from datetime import datetime
from pathlib import Path

from qwen3_code.theme import console, SAKURA
from qwen3_code.utils import SESSION_DIR, SYSTEM_PROMPT
from qwen3_code.settings import CFG


def _session_path(cwd: str) -> Path:
    safe = re.sub(r"[^\w.\-]", "_", cwd)
    return SESSION_DIR / f"{safe}.json"


def save_session(cwd: str, messages: list[dict]) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    non_system = [m for m in messages if m.get("role") != "system"]
    data = {"cwd": cwd, "saved_at": datetime.now().isoformat(), "messages": non_system}
    _session_path(cwd).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_session(cwd: str) -> list[dict]:
    from rich.panel import Panel
    base: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if not CFG.get("open_from_last_session", True):
        return base
    path = _session_path(cwd)
    if not path.exists():
        return base
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        saved = data.get("messages", [])
        console.print(Panel(
            f"[info]Resumed session for [bold]{cwd}[/bold]\n"
            f"{len(saved)} message(s) from {data.get('saved_at', '?')}[/info]",
            title="Session loaded", border_style=SAKURA,
        ))
        return base + saved
    except Exception as exc:
        console.print(f"[error]Could not load session: {exc}[/error]")
        return base
