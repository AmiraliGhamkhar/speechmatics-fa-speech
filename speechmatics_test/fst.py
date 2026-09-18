"""Deprecated module kept for API stability - import ``matcher`` instead.

The medical matcher (class ``MedicalMatcher``, alias ``MedicalFST``) lives
in ``speechmatics_test/matcher.py``. This module re-exports the full public
surface so existing ``from speechmatics_test.fst import ...`` calls keep
working; it is scheduled for removal once downstream users have migrated.
"""

from __future__ import annotations

from .matcher import (  # noqa: F401
    AhoAutomaton,
    ENGINE_NAME,
    DictionaryTerm,
    FstError,
    FstRule,
    MedicalFST,
    MedicalMatcher,
    MedicalRule,
    TERM_TYPES,
    TIER_ORDER,
    TIER_RANK,
    build_matcher,
    casefold_preserving,
    load_dictionary,
)

__all__ = [
    "MedicalFST", "MedicalMatcher", "FstError", "FstRule", "MedicalRule",
    "AhoAutomaton", "ENGINE_NAME", "TIER_ORDER", "TIER_RANK", "TERM_TYPES",
    "DictionaryTerm", "casefold_preserving", "load_dictionary", "build_matcher",
]
