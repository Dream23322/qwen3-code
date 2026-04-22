"""Colour palette, Rich theme, and shared console instance."""

from rich.console import Console
from rich.theme import Theme

# ---------------------------------------------------------------------------
# Sakura palette
# ---------------------------------------------------------------------------

SAKURA: str       = "#FFB7C5"
SAKURA_DEEP: str  = "#FF69B4"
SAKURA_MUTED: str = "#FFCDD6"
SAKURA_DARK: str  = "#C2185B"

# ---------------------------------------------------------------------------
# Rich theme + console
# ---------------------------------------------------------------------------

custom_theme: Theme = Theme({
    "user":      f"bold {SAKURA_DEEP}",
    "assistant": f"bold {SAKURA}",
    "system":    f"dim {SAKURA_MUTED}",
    "error":     f"bold {SAKURA_DARK}",
    "info":      f"dim {SAKURA_MUTED}",
})

console: Console = Console(theme=custom_theme)
