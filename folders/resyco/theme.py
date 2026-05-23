"""Blue-first terminal palette for FirstSky (colorama)."""
from __future__ import annotations

try:
    from colorama import Fore, Style, init as _colorama_init

    _colorama_init(autoreset=True)
except ImportError:

    class _NoColor:
        def __getattr__(self, _: str) -> str:
            return ""

    Fore = Style = _NoColor()  # type: ignore[misc, assignment]

S = Style
F = Fore

C_RESET = S.RESET_ALL

# Frames & dividers (muted slate-blue)
C_FRAME = S.DIM + F.BLUE
C_BORDER = S.DIM + F.BLUE
C_DIV = S.DIM + F.BLUE

# Field labels & secondary emphasis (reduced saturation)
C_LABEL = F.BLUE
C_ACCENT = F.CYAN
C_KEY = S.BRIGHT + F.BLUE
C_KEY_SOFT = S.DIM + F.CYAN
C_VALUE = F.LIGHTWHITE_EX
C_TITLE = S.BRIGHT + F.CYAN
C_HEAD = S.BRIGHT + F.BLUE
C_BRAND = S.BRIGHT + F.CYAN
C_WEAK = S.DIM + F.BLUE

# Status & feedback (muted blue family)
C_OK = S.BRIGHT + F.BLUE
C_STEP = F.CYAN
C_INFO = F.CYAN
C_WARN = F.BLUE
C_BAD = S.BRIGHT + F.LIGHTWHITE_EX
C_EXIT = S.BRIGHT + F.BLUE
C_SPIN = F.CYAN
C_PROGRESS = F.BLUE
C_PROMPT = F.BLUE

# Tool picker: distinct, softer blue tiers
C_TOOL_A = S.BRIGHT + F.CYAN
C_TOOL_B = S.BRIGHT + F.BLUE
C_TOOL_C = S.DIM + F.CYAN

