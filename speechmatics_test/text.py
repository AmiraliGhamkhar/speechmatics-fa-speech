
from __future__ import annotations
import re
import unicodedata

ARABIC_TO_PERSIAN = str.maketrans({
    "\u064a": "\u06cc", "\u0649": "\u06cc", "\u0643": "\u06a9",
    "\u0640": "", "\u064b": "", "\u064c": "", "\u064d": "",
    "\u064e": "", "\u064f": "", "\u0650": "", "\u0651": "", "\u0652": "",
})
RTL_RE = re.compile(r"[\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff]")

def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    text = text.translate(ARABIC_TO_PERSIAN)
    text = text.replace("ي","ی").replace("ك","ک")
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

def tokens(text: str):
    return re.findall(r"[\w\u0600-\u06ff]+(?:[-/.][\w\u0600-\u06ff]+)*|[^\w\s]", normalize_text(text), flags=re.UNICODE)
