"""Medical canonicalization facade.

Keeps the pipeline stages separate and explicit:

    Speechmatics raw final  ->  normalize_text()  ->  MedicalFST  ->  canonical

- The RAW Speechmatics output is preserved untouched (see
  ``SpeechmaticsRealtime.SessionResult.final_text``).
- ``normalize_text`` is generic text normalization only (script unification,
  ZWNJ/whitespace, punctuation spacing) - no medical knowledge.
- ``MedicalFST`` performs deterministic lexical canonicalization using the
  FST rule set in ``medical_knowledge/fst_terms.json`` plus the other
  knowledge files, each kept in its own tier.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .fst import MedicalFST
from .text import normalize_text


class MedicalLayer:
    """Facade over the deterministic medical FST layer."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.fst = MedicalFST(self.root)

    @property
    def warnings(self) -> list[str]:
        return self.fst.warnings

    def canonicalize(self, normalized_text: str) -> tuple[str, list[dict[str, Any]]]:
        """Apply the medical FST to already-normalized finalized text.

        Returns ``(canonical_text, hits)``. This is the only stage that may
        rewrite medical terminology.
        """
        return self.fst.canonicalize(normalized_text)

    def normalize(self, text: str) -> tuple[str, list[dict[str, Any]]]:
        """Convenience: generic normalization + medical FST in one call."""
        return self.fst.canonicalize(normalize_text(text))
