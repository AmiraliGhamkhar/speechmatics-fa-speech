"""Generic text normalization and medical-aware tokenization.

This module is language/tooling plumbing only: it contains no medical
knowledge. It unifies Persian script, normalizes whitespace/ZWNJ, and offers
a tokenizer that keeps medical values intact (``20 mg``, ``120/80``,
``5.5``, ``O2``, ``q2h``, ``C3-C4``, ``U/A``, routes, abbreviations).
"""

from __future__ import annotations

import re
import unicodedata

# Arabic -> Persian script unification + diacritic/hamza removal.
ARABIC_TO_PERSIAN = str.maketrans({
    "\u064a": "\u06cc", "\u0649": "\u06cc", "\u0643": "\u06a9",
    "\u0640": "", "\u064b": "", "\u064c": "", "\u064d": "",
    "\u064e": "", "\u064f": "", "\u0650": "", "\u0651": "", "\u0652": "",
})

RTL_RE = re.compile(r"[\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff]")


def normalize_text(text: str) -> str:
    """Generic normalization: NFC, Persian script, ZWNJ->space, spacing."""
    text = unicodedata.normalize("NFC", text or "")
    text = text.translate(ARABIC_TO_PERSIAN)
    text = text.replace("ي", "ی").replace("ك", "ک")
    text = re.sub(r"[ \t\u200c\u200d]+", " ", text).strip()
    text = re.sub(r"\s+([،؛؟,.!?])", r"\1", text)
    return text


def is_rtl(text: str) -> bool:
    for ch in (text or "").strip():
        if RTL_RE.match(ch):
            return True
        if ch.isascii() and ch.isalpha():
            return False
    return True


# ------------------------------------------------------------------ tokens

_WORD = r"[A-Za-z\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff]+"
_NUM = r"\d+(?:[.,]\d+)*"
# Atom: word with an optional numeric suffix and optional trailing letters:
#   mg | O2 | A1c | q2h
_ATOM = rf"{_WORD}(?:{_NUM})?(?:{_WORD})?"

# Token shapes (order matters):
#   1) numbers incl. decimals, comma decimals and BP-style ratios:
#        20 | 5.5 | 3,14 | 120/80
#   2) atoms optionally hyphen/slash joined:
#        mg | O2 | A1c | q2h | C3-C4 | U/A | right-lung
#   3) a single non-word, non-space character (punctuation, %, etc.)
# Whitespace is not a token (findall simply skips it).
_TOKEN = re.compile(
    rf"(?:{_NUM}(?:/{_NUM})*)"
    rf"|(?:{_ATOM}(?:[-/]{_ATOM})*)"
    r"|(?:[^\w\s])"
)


def tokens(text: str) -> list[str]:
    """Medical-aware tokenization for WER/number metrics on mixed text."""
    t = normalize_text(text)
    return _TOKEN.findall(t)
