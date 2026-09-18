"""Medical canonicalization facade.

Keeps the pipeline stages separate and explicit:

    Speechmatics raw final  ->  normalize_text()  ->  MedicalFST  ->  canonical

- The RAW Speechmatics output is preserved untouched (see
  ``SpeechmaticsRealtime.SessionResult.final_text``).
- ``normalize_text`` is generic text normalization only (script unification,
  ZWNJ/whitespace, punctuation spacing) - no medical knowledge.
- ``MedicalFST`` performs deterministic lexical canonicalization using an
  **Aho-Corasick** automaton built from the rule set in
  ``medical_knowledge/fst_terms.json`` plus the other knowledge files, each
  kept in its own tier. The automaton learns every form once and searches
  all of them simultaneously in a single pass, so the layer stays fast no
  matter how large the rule set grows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .fst import MedicalFST
from .text import normalize_text


class MedicalLayer:
    """Facade over the deterministic medical Aho-Corasick layer."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.fst = MedicalFST(self.root)

    @property
    def warnings(self) -> list[str]:
        return self.fst.warnings

    @property
    def engine(self) -> str:
        return self.fst.engine

    @property
    def max_rule_tokens(self) -> int:
        """Longest rule form in tokens (see MedicalFST.max_rule_tokens)."""
        return self.fst.max_rule_tokens

    def is_rule_token_prefix(self, tokens: list[str]) -> bool:
        """Whether ``tokens`` start some rule form (see MedicalFST)."""
        return self.fst.is_rule_token_prefix(tokens)

    def canonicalize(
        self,
        normalized_text: str,
        word_results: list[dict[str, Any]] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Apply lexical rules to an already-normalized finalized segment.

        ``word_results`` is optional final-only ASR evidence. Missing metadata
        follows the exact legacy canonicalization path.
        """
        return self.fst.canonicalize(normalized_text, word_results)

    def normalize(
        self, text: str, word_results: list[dict[str, Any]] | None = None
    ) -> tuple[str, list[dict[str, Any]]]:
        """Convenience: generic normalization + medical layer in one call."""
        return self.fst.canonicalize(normalize_text(text), word_results)
