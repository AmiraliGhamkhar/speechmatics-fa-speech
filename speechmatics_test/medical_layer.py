"""Medical canonicalization facade.

Keeps the pipeline stages separate and explicit:

    Speechmatics raw final  ->  normalize_text()  ->  MedicalMatcher  ->  canonical

- The RAW Speechmatics output is preserved untouched (see
  ``SpeechmaticsRealtime.SessionResult.final_text``).
- ``normalize_text`` is generic text normalization only (script unification,
  ZWNJ/whitespace, punctuation spacing) - no medical knowledge.
- ``MedicalMatcher`` (aliased ``MedicalFST``) performs deterministic lexical
  canonicalization using an Aho-Corasick automaton compiled once from
  ``medical_knowledge/medical_dictionary.json``: every form is learned once
  and searched simultaneously in a single pass, so the layer stays fast no
  matter how large the dictionary grows.
- ``additional_vocab`` is the bounded Speechmatics custom vocabulary derived
  from the dictionary's ``speechmatics: true`` entries only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .matcher import MedicalMatcher
from .text import normalize_text


class MedicalLayer:
    """Facade over the deterministic medical Aho-Corasick layer."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.fst = MedicalMatcher(self.root)

    @property
    def warnings(self) -> list[str]:
        return self.fst.warnings

    @property
    def engine(self) -> str:
        return self.fst.engine

    @property
    def max_rule_tokens(self) -> int:
        """Longest rule form in tokens (see MedicalMatcher.max_rule_tokens)."""
        return self.fst.max_rule_tokens

    @property
    def additional_vocab(self) -> list:
        """Bounded Speechmatics vocabulary from speechmatics-eligible terms."""
        return self.fst.additional_vocab

    def is_rule_token_prefix(self, tokens: list[str]) -> bool:
        """Whether ``tokens`` start some rule form (see MedicalMatcher)."""
        return self.fst.is_rule_token_prefix(tokens)

    def is_strict_rule_token_prefix(self, tokens: list[str]) -> bool:
        """Whether ``tokens`` are a PROPER prefix of a longer rule form.

        See ``MedicalMatcher.is_strict_rule_token_prefix``: unlike
        ``is_rule_token_prefix``, a tuple that only equals a complete,
        non-extendable rule's own full form returns False here.
        """
        return self.fst.is_strict_rule_token_prefix(tokens)

    def canonicalize(
        self,
        normalized_text: str,
        word_results: list[dict[str, Any]] | None = None,
        *,
        preserve_narrative: bool = False,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Apply lexical rules to an already-normalized finalized segment.

        ``word_results`` is optional final-only ASR evidence. Missing metadata
        follows the exact legacy canonicalization path.
        """
        return self.fst.canonicalize(
            normalized_text, word_results, preserve_narrative=preserve_narrative
        )

    def normalize(
        self, text: str, word_results: list[dict[str, Any]] | None = None,
        *, preserve_narrative: bool = False,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Convenience: generic normalization + medical layer in one call."""
        return self.fst.canonicalize(
            normalize_text(text), word_results, preserve_narrative=preserve_narrative
        )
