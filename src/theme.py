"""Monochrome dark theme tokens.

Tkinter can't do gradients, glows or glassmorphism, so this leans on the
parts of a modern dark UI it *can* render well: near-black surfaces, a
strict grey ramp, generous spacing, and hard typographic hierarchy.
Colour is reserved almost entirely for state -- if something is green or
red, it means something.
"""
from __future__ import annotations

# --- Surfaces: near-black, stepped so panels read as layered ----------

BG = "#0a0a0b"           # window
SURFACE = "#111113"      # cards / bands
SURFACE_HI = "#17171a"   # inputs, raised elements
SURFACE_ACTIVE = "#202024"
BORDER = "#26262b"
BORDER_HI = "#34343a"

# --- Text: a grey ramp, not opacity -----------------------------------

TEXT = "#fafafa"
TEXT_MUTED = "#8b8b93"
TEXT_FAINT = "#5c5c64"

# --- State. The only saturated colour in the app ----------------------

OK = "#4ade80"
WARN = "#fbbf24"
ERROR = "#f87171"
IDLE = "#52525b"

# --- Controls: white-on-black primary, monochrome ---------------------

PRIMARY = "#fafafa"
PRIMARY_HOVER = "#d4d4d8"
PRIMARY_TEXT = "#0a0a0b"
GHOST = "transparent"
GHOST_HOVER = "#202024"
DANGER = "#7f1d1d"
DANGER_HOVER = "#991b1b"

# --- Spacing scale (4px base) -----------------------------------------

XS, SM, MD, LG, XL, XXL = 4, 8, 12, 16, 24, 32

# --- Radius ------------------------------------------------------------

R_SM, R_MD, R_LG = 6, 10, 14

# --- Type. Segoe UI is the best thing reliably on Windows -------------

FAMILY = "Segoe UI"
MONO = "Cascadia Mono"

DISPLAY = (FAMILY, 34, "bold")     # the countdown
STAT = (FAMILY, 26, "bold")        # tile numbers
TITLE = (FAMILY, 15, "bold")
BODY = (FAMILY, 13)
LABEL = (FAMILY, 12)
CAPTION = (FAMILY, 11)
# Section headers read as eyebrows: small, spaced, muted, uppercased.
EYEBROW = (FAMILY, 10, "bold")
LOG = (MONO, 11)

LEVEL_COLOUR = {
    "success": OK,
    "warn": WARN,
    "error": ERROR,
    "info": TEXT_MUTED,
}


def eyebrow(text: str) -> str:
    """Letter-spaced uppercase, faked with spaces since Tk has no tracking."""
    return " ".join(text.upper())
