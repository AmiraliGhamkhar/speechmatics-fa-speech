from __future__ import annotations

from dataclasses import dataclass, asdict


ARROWS = {
    "←", "→", "↔", "⇐", "⇒", "⇄", "➜", "➝", "➞", "➤",
    "⟵", "⟶", "⬅", "➡", "↩", "↪", "⟷", "⇆", "⇔",
}

BIDI_MARKS = {
    "\u200e": "LRM",
    "\u200f": "RLM",
    "\u202a": "LRE",
    "\u202b": "RLE",
    "\u202c": "PDF",
    "\u202d": "LRO",
    "\u202e": "RLO",
    "\u2066": "LRI",
    "\u2067": "RLI",
    "\u2068": "FSI",
    "\u2069": "PDI",
}


@dataclass
class TextCleanliness:
    is_clean: bool
    arrows: list[str]
    bidi_marks: list[str]
    control_characters: list[str]
    replacement_characters: int
    zwnj_count: int
    newline_count: int
    tab_count: int
    non_ascii_symbols: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def inspect_text(text: str) -> TextCleanliness:
    arrows = sorted({ch for ch in text if ch in ARROWS})
    bidi = sorted({name for ch, name in BIDI_MARKS.items() if ch in text})
    controls: list[str] = []
    symbols: list[str] = []

    for ch in text:
        code = ord(ch)
        if code < 32 and ch not in ("\n", "\t", "\r"):
            controls.append(f"U+{code:04X}")
            continue

        if (
            not ch.isascii()
            and not ("\u0600" <= ch <= "\u06FF")
            and ch != "\u200c"
            and ch not in ARROWS
            and ch not in BIDI_MARKS
        ):
            symbols.append(ch)

    replacement = text.count("\ufffd")

    return TextCleanliness(
        is_clean=(
            not arrows
            and not bidi
            and not controls
            and replacement == 0
        ),
        arrows=arrows,
        bidi_marks=bidi,
        control_characters=sorted(set(controls)),
        replacement_characters=replacement,
        zwnj_count=text.count("\u200c"),
        newline_count=text.count("\n"),
        tab_count=text.count("\t"),
        non_ascii_symbols=sorted(set(symbols)),
    )
