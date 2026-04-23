"""User-facing settings: load, save, and accessor helpers."""

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths & defaults
# ---------------------------------------------------------------------------

SETTINGS_PATH: Path = Path(__file__).parent.parent / "settings.json"

DEFAULT_SETTINGS: dict = {
    "app_name":               "qwen3-code",
    "assistant_name":         "assistant",
    "model":                  "huihui_ai/qwen3-coder-abliterated:30b",
    "open_from_last_session": True,
    "context_window":         128000,
    "insert_verify":          True,
    "learn_mode":             False,
}

SETTINGS_HELP: dict[str, str] = {
    "app_name":               "Display name shown in the header and panels",
    "assistant_name":         "Label used when the AI is thinking / responding",
    "model":                  "Ollama model tag to use for all inference",
    "open_from_last_session": "true/false  \u2014 resume previous conversation on startup",
    "context_window":         "Token limit for your model (e.g. 128000, 1000000) \u2014 used by /context bar",
    "insert_verify":          "true/false  \u2014 show syntax check + diff preview before applying INSERT markers",
    "learn_mode":             "true/false  \u2014 beginner tutorial mode: AI explains concepts step-by-step (toggle with /learn)",
}


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            data: dict = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            return {**DEFAULT_SETTINGS, **data}
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()


def save_settings(settings: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Live config dict (module-level singleton)
# ---------------------------------------------------------------------------

CFG: dict = load_settings()


def _model()          -> str:  return CFG["model"]
def _app_name()       -> str:  return CFG["app_name"]
def _assistant_name() -> str:  return CFG["assistant_name"]
def _context_window() -> int:  return int(CFG.get("context_window", 128_000))
def _learn_mode()     -> bool: return bool(CFG.get("learn_mode", False))


# ---------------------------------------------------------------------------
# /settings command handler
# ---------------------------------------------------------------------------

def handle_settings(arg: str) -> None:
    from qwen3_code.theme import console, SAKURA_DEEP
    from rich.panel import Panel

    parts: list[str] = arg.strip().split(maxsplit=1)

    if not parts:
        rows: list[str] = []
        for k, v in CFG.items():
            tag = " [dim](default)[/dim]" if v == DEFAULT_SETTINGS.get(k) else ""
            rows.append(
                f"  [bold cyan]{k:<30}[/bold cyan]  [bold]{v}[/bold]{tag}\n"
                f"  [dim]{SETTINGS_HELP.get(k, '')}[/dim]"
            )
        console.print(Panel("\n".join(rows), title=f"Settings  ({SETTINGS_PATH})", border_style=SAKURA_DEEP))
        return

    key: str = parts[0].lower()
    if key not in DEFAULT_SETTINGS:
        console.print(f"[error]Unknown setting '{key}'. Valid: {', '.join(DEFAULT_SETTINGS)}[/error]")
        return

    if len(parts) == 1:
        console.print(f"[bold cyan]{key}[/bold cyan] = [bold]{CFG[key]}[/bold]  [dim]{SETTINGS_HELP.get(key, '')}[/dim]")
        return

    raw: str = parts[1].strip()
    expected = DEFAULT_SETTINGS[key]
    if isinstance(expected, bool):
        if raw.lower() in ("true", "1", "yes", "on"):    value = True
        elif raw.lower() in ("false", "0", "no", "off"): value = False
        else:
            console.print(f"[error]'{key}' expects true/false[/error]")
            return
    elif isinstance(expected, int):
        try:    value = int(raw)
        except ValueError:
            console.print(f"[error]'{key}' expects an integer[/error]")
            return
    else:
        value = raw

    old = CFG[key]
    CFG[key] = value
    save_settings(CFG)
    console.print(f"[info][bold cyan]{key}[/bold cyan]: [dim]{old}[/dim] \u2192 [bold]{value}[/bold]  saved[/info]")
    if key == "model":
        console.print(f"[info]Run: ollama pull {value}[/info]")
