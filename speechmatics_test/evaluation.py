
from __future__ import annotations
import re
from difflib import SequenceMatcher
from .text import normalize_text, tokens

def edit_distance(a, b):
    prev = list(range(len(b)+1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(cur[-1]+1, prev[j]+1, prev[j-1]+(x != y)))
        prev=cur
    return prev[-1]

def wer(reference: str, hypothesis: str) -> float:
    r = tokens(reference)
    h = tokens(hypothesis)
    return edit_distance(r,h) / max(1,len(r))

def number_accuracy(reference: str, hypothesis: str) -> float | None:
    nums_r = re.findall(r"\d+(?:[.,/]\d+)*", reference)
    if not nums_r:
        return None
    nums_h = re.findall(r"\d+(?:[.,/]\d+)*", hypothesis)
    matched = sum(1 for x in nums_r if x in nums_h)
    return matched / len(nums_r)

def evaluate(reference: str, hypothesis: str) -> dict:
    return {
        "wer": round(wer(reference, hypothesis), 4),
        "number_accuracy": number_accuracy(reference, hypothesis),
        "similarity": round(SequenceMatcher(None, normalize_text(reference), normalize_text(hypothesis)).ratio(), 4),
    }
