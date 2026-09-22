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
from .nursing_text import (
    PolishReport,
    polish_nursing_text,
    prepolish_asr_artifacts,
)
from .text import normalize_text


class MedicalLayer:
    """Facade over the deterministic medical Aho-Corasick layer.

    ``polish`` (default on) enables the deterministic nursing text stage
    (:mod:`speechmatics_test.nursing_text`): spoken numbers/times/units,
    charted vital-sign punctuation, ASR-stutter cleanup and Persian
    typography. It runs AFTER lexical canonicalization and never changes
    medical content - see that module's safety contract. Pass
    ``polish=False`` to get the exact pre-polish canonicalization output.
    """

    def __init__(self, root: Path, polish: bool = True) -> None:
        self.root = Path(root)
        self.fst = MedicalMatcher(self.root)
        self.polish = polish
        #: Warnings raised by the polish stage (unbound numerals, ...).
        self.polish_warnings: list[str] = []
        #: How many canonicalize() calls the LEXICAL medical stage actually
        #: rewrote. Reports need this to tell "a medical term was
        #: canonicalized" apart from "the nursing stage reformatted a
        #: number", which a naive ``output != input`` check conflates.
        self.medical_replacement_count = 0

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
    ) -> tuple[str, list[dict[str, Any]]]:
        """Apply lexical rules to an already-normalized finalized segment.

        ``word_results`` is optional final-only ASR evidence. Missing metadata
        follows the exact legacy canonicalization path.

        With ``polish`` enabled the deterministic nursing text stage runs
        around the matcher:

        1. ``prepolish_asr_artifacts`` collapses duplicated words BEFORE
           matching (a stutter can otherwise combine with its neighbour into
           a dictionary phrase the speaker never said), while protecting the
           forms that legitimately contain a repeated syllable (``سی سی یو``).
        2. the matcher canonicalizes, exactly as before;
        3. ``polish_nursing_text`` applies numbers/times/units, charted
           vital-sign punctuation and Persian typography.

        The returned hits always describe the LEXICAL layer only, so the
        audit trail keeps meaning what it always meant.
        """
        if not self.polish:
            canonical, hits = self.fst.canonicalize(
                normalized_text, word_results
            )
            if canonical != normalized_text:
                self.medical_replacement_count += 1
            return canonical, hits

        report = PolishReport()
        prepared = prepolish_asr_artifacts(
            normalized_text, report,
            protected=self.fst.repetition_safe_forms,
        )
        # Word-level ASR evidence is positional. It can only be trusted when
        # the pre-polish step did not move any characters, so it is dropped
        # (not misaligned) when a stutter was actually removed.
        evidence = word_results if prepared == normalized_text else None
        canonical, hits = self.fst.canonicalize(prepared, evidence)
        if canonical != prepared:
            self.medical_replacement_count += 1
        polished = polish_nursing_text(canonical, report)
        self.polish_warnings.extend(report.warnings)
        return polished, hits

    def normalize(
        self, text: str, word_results: list[dict[str, Any]] | None = None
    ) -> tuple[str, list[dict[str, Any]]]:
        """Convenience: generic normalization + medical layer in one call.

        Routes through ``canonicalize`` so it gets the identical treatment
        (including the polish stage) rather than a second, divergent path.
        """
        return self.canonicalize(normalize_text(text), word_results)
