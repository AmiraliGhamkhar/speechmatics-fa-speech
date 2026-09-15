"""Presentation-only BiDi helpers (logical text in, display text out).

The application keeps ONE authoritative string per segment: the *canonical*
logical-order Unicode medical transcript.  Nothing in this module ever
changes that text - it only adds/removes Unicode *direction control*
characters that tell a renderer (Tk label, editor, EMR web form) which
paragraph direction to use:

    canonical_text : clean logical Unicode (no RLM/RLE/PDF, ever)
    display_text   : canonical_text + direction controls (overlay)
    injected_text  : canonical_text + direction controls (clipboard paste)

Characters are never reordered here.  Manual reversal of Persian or
English runs - the classic "visual order" hack - is exactly what produces
double-BiDi corruption once the renderer applies the real Unicode BiDi
algorithm on top.
"""

from __future__ import annotations

__all__ = [
    "RLM", "RLE", "PDF", "LRE", "LRM", "BIDI_CONTROLS",
    "contains_rtl", "detect_direction", "strip_bidi_controls",
    "wrap_for_direction", "has_bidi_controls",
]

#: Unicode explicit direction controls used by the application.
RLM = "\u200f"  # Right-to-Left Mark (sets the base direction)
RLE = "\u202b"  # Right-to-Left Embedding
PDF = "\u202c"  # Pop Directional Formatting
LRM = "\u200e"  # Left-to-Right Mark (never emitted; recognised for cleanup)
LRE = "\u202a"  # Left-to-Right Embedding (never emitted; recognised)

#: Every control we may need to recognise/strip when re-wrapping.
BIDI_CONTROLS = frozenset(
    "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)

_RTL_RANGES = (
    ("\u0600", "\u06ff"),
    ("\u0750", "\u077f"),
    ("\ufb50", "\ufdff"),
    ("\ufe70", "\ufeff"),
)


def _is_rtl_char(ch: str) -> bool:
    for lo, hi in _RTL_RANGES:
        if lo <= ch <= hi:
            return True
    return False


def contains_rtl(text: str) -> bool:
    """True when the string contains at least one RTL (Persian/Arabic) char."""
    return any(_is_rtl_char(ch) for ch in text or "")


def has_bidi_controls(text: str) -> bool:
    """True when the string already carries explicit direction controls."""
    return any(ch in BIDI_CONTROLS for ch in text or "")


def strip_bidi_controls(text: str) -> str:
    """Remove every explicit direction control, returning clean logical text."""
    if not text:
        return text or ""
    if not has_bidi_controls(text):
        return text
    return "".join(ch for ch in text if ch not in BIDI_CONTROLS)


def detect_direction(text: str, rtl_threshold: float = 0.35) -> str:
    """Return the *paragraph base direction* ("rtl"/"ltr") of logical text.

    This only chooses a base direction - it never reorders characters.
    """
    s = strip_bidi_controls(text or "").strip()
    if not s:
        return "rtl"

    rtl = sum(1 for ch in s if _is_rtl_char(ch))
    latin = sum(1 for ch in s if ch.isascii() and ch.isalpha())
    total = rtl + latin
    if total == 0:
        return "rtl"

    share = rtl / total
    if share >= 0.90:
        return "rtl"
    if share <= 0.10:
        return "ltr"
    if share >= rtl_threshold:
        return "rtl"
    # Ambiguous mixed text: respect the first strong directional character.
    for ch in s:
        if _is_rtl_char(ch):
            return "rtl"
        if ch.isascii() and ch.isalpha():
            return "ltr"
    return "rtl"


def wrap_for_direction(text: str, direction: str | None = None) -> str:
    """Return the presentation form of a *logical* string.

    RTL-dominant text  ->  ``RLM + RLE + logical_text + PDF``
    LTR-dominant text  ->  ``logical_text`` (unchanged)

    The function is **idempotent**: any direction controls already present
    are stripped first, so wrapping twice yields exactly one wrap and never
    ``RLM + RLM + RLE + RLE ... PDF + PDF``.  The logical text itself
    (including a trailing separator space, which stays *inside* the
    embedding) is never modified.
    """
    if not text:
        return text or ""
    logical = strip_bidi_controls(text)
    if not logical:
        return ""
    direction = direction or detect_direction(logical)
    if direction == "rtl" and contains_rtl(logical):
        return RLM + RLE + logical + PDF
    return logical
