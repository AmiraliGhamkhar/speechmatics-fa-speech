"""Deterministic medical lexical canonicalization (Aho-Corasick layer).

Pipeline position (applied to FINAL ASR segments only, never to partials):

    Speechmatics FINAL  ->  normalize_text()  ->  MedicalFST.canonicalize()
                                              ->  canonical medical transcript

Engine: Aho-Corasick
--------------------
The matcher is an **Aho-Corasick automaton**:

- It **learns every rule form once** at load time (a single automaton
  build) instead of re-compiling anything per utterance.
- It then **searches ALL forms simultaneously** in one left-to-right pass
  over the input text, in O(len(text) + number_of_matches) time,
  completely independent of how many rules exist. This is the fastest
  known approach for a large dictionary and is exactly what a growing
  clinical rule set needs. (The previous per-alphabet OpenFst rebuild -
  and its cache - is gone entirely.)
- Matches are filtered to **token boundaries** (both sides of the form
  must border a boundary character or the text edge), so replacement is
  token-aware rather than unsafe substring replacement.
- Overlapping candidates at a position are resolved deterministically:
  **longest match first**, then tier (curated > observed), then stable
  rule order. A shorter boundary-valid match still wins over a longer
  boundary-invalid one.
- No semantic inference: the layer only performs deterministic lexical
  replacement. It never infers diagnosis, severity, negation or dosage
  correctness.

Two interchangeable engines implement the automaton:

1. ``pyahocorasick`` (native C) when the package is installed
   (``MedicalFST.uses_ahocorasick is True``), and
2. a built-in pure-Python Aho-Corasick automaton (goto/failure/output
   table) that produces identical output, so the application is fully
   functional everywhere - including platforms without a C toolchain.

A deterministic naive scanner (``_scan_reference``) is kept as the
reference implementation; the test suite verifies the automaton against
it, and ``canonicalize`` degrades to it if the engine ever fails, so an
engine problem can never cost the clinician their finished transcript.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from .text import normalize_text

try:  # pragma: no cover - depends on environment
    import ahocorasick as _native_ac
except ImportError:  # pragma: no cover
    _native_ac = None

__all__ = ["MedicalFST", "FstError", "FstRule", "AhoAutomaton", "ENGINE_NAME",
           "casefold_preserving"]

#: Public engine identifier (reported by the app banner and the reports).
ENGINE_NAME = "aho-corasick"


class FstError(RuntimeError):
    """Raised when the rule set is invalid or the matcher operation fails."""


#: Characters that separate tokens (whitespace + common Latin/Persian
#: punctuation). The character set (used by both the automaton boundary
#: filter and the reference scanner) is derived from this single source
#: of truth.
_BOUNDARY_CHARACTERS = (
    " \t\n\r"
    ".,;:!?'\"()[]{}<>+-/#&%$=@`"
    "،؛؟«»…"
)
_BOUNDARY_CHARS = frozenset(_BOUNDARY_CHARACTERS)


def casefold_preserving(text: str) -> str:
    """Case-insensitive folding that is guaranteed length-preserving.

    ``str.casefold()`` is the correct Unicode case-insensitive mapping, but a
    handful of code points expand (``\u00df`` -> ``ss``, ``\ufb00`` -> ``ff``).
    An expansion would shift every following index, so the match positions
    returned by the automaton could no longer be used against the ORIGINAL
    text.  Folding character by character and keeping any expanding character
    unchanged keeps ``len(fold(t)) == len(t)`` and index ``i`` of the folded
    string always describing index ``i`` of the original.

    Persian/Arabic letters are caseless: ``casefold`` returns them untouched,
    so Persian text is never modified by this function.
    """
    if not text:
        return text or ""
    if text.isascii():
        return text.lower()
    out = []
    for ch in text:
        folded = ch.casefold()
        out.append(folded if len(folded) == 1 else ch.lower()[:1] or ch)
    return "".join(out)


@dataclass(frozen=True)
class FstRule:
    """One deterministic input-form -> canonical mapping."""

    form: str       # normalized input form (token-aware, exact match)
    canonical: str  # replacement text
    tier: int       # 0 = highest priority
    source: str     # provenance, e.g. "fst_terms.json" or "observed_asr_aliases.json"
    seq: int = 0    # stable ordering index (assigned at load time)
    #: Case-folded form used for MATCHING ONLY (never for output). Always the
    #: same length as ``form`` (see ``casefold_preserving``).
    folded: str = ""

    @property
    def match_form(self) -> str:
        """The string the automaton actually searches for (case-folded)."""
        return self.folded or casefold_preserving(self.form)

    @property
    def key(self) -> tuple:
        """Priority key: longest first, then tier, then order."""
        return (-len(self.form), self.tier, self.seq)


# ---------------------------------------------------------------------- AC


class AhoAutomaton:
    """Self-contained pure-Python Aho-Corasick automaton.

    Builds the goto/failure/output tables once; ``iter()`` then finds all
    pattern occurrences in a single pass over the text. Patterns are
    identified by their index in the ``forms`` list.
    """

    __slots__ = ("_goto", "_fail", "_out")

    def __init__(self, forms: list[str]) -> None:
        goto: list[dict[str, int]] = [{}]
        fail: list[int] = [0]
        out: list[list[int]] = [[]]

        for idx, form in enumerate(forms):
            state = 0
            for ch in form:
                target = goto[state].get(ch)
                if target is None:
                    target = len(goto)
                    goto[state][ch] = target
                    goto.append({})
                    fail.append(0)
                    out.append([])
                state = target
            out[state].append(idx)

        # Failure links via BFS; dictionary outputs are merged in so every
        # state carries the full set of patterns that end there.
        queue: deque[int] = deque()
        for child in goto[0].values():
            queue.append(child)  # depth-1 nodes fail to the root
        while queue:
            state = queue.popleft()
            for ch, target in goto[state].items():
                queue.append(target)
                f = fail[state]
                while f and ch not in goto[f]:
                    f = fail[f]
                fallback = goto[f].get(ch, 0)
                fail[target] = 0 if fallback == target else fallback
                if out[fail[target]]:
                    out[target] = out[target] + out[fail[target]]

        self._goto = goto
        self._fail = fail
        self._out = out

    def iter(self, text: str) -> Iterator[tuple[int, int]]:
        """Yield ``(end_index_inclusive, pattern_index)`` for every match."""
        goto, fail, out = self._goto, self._fail, self._out
        state = 0
        for i, ch in enumerate(text):
            while state and ch not in goto[state]:
                state = fail[state]
            state = goto[state].get(ch, 0)
            for pattern in out[state]:
                yield i, pattern


@dataclass
class MedicalFST:
    """Loads the medical rules and canonicalizes finalized transcript text.

    Despite the historical class name (the layer's public API is stable),
    the matching engine is an Aho-Corasick automaton - not an OpenFst
    transducer. Rule loading, tiers, dedup/conflict resolution, hits and
    the ``canonicalize`` contract are unchanged.
    """

    root: Path
    terms_rel: str = "medical_knowledge/fst_terms.json"
    extra_sources: tuple = (
        "medical_knowledge/observed_asr_aliases.json",
        "medical_knowledge/abbreviations.json",
        "medical_knowledge/nursing_phrases.json",
        "medical_knowledge/nursing_terms.json",
    )

    # populated by __post_init__
    rules: list[FstRule] = field(default_factory=list, init=False, repr=False)
    warnings: list[str] = field(default_factory=list, init=False, repr=False)
    uses_ahocorasick: bool = field(default=False, init=False, repr=False)
    _by_first: dict = field(default_factory=dict, init=False, repr=False)
    _ac_native: Any = field(default=None, init=False, repr=False)
    _ac_python: Optional[AhoAutomaton] = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------ load

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        raw = self._load_primary()
        raw += self._load_extra()
        self._build_rules(raw)
        if not self.rules:
            self.warnings.append(
                "medical canonicalization layer has no rules; it is a no-op"
            )
        self._build_automaton()

    def _read_json(self, rel: str, required: bool = False) -> Optional[Any]:
        path = self.root / rel
        if not path.exists():
            if required:
                raise FstError(f"required knowledge file not found: {rel}")
            return None  # optional source: silently skip
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self.warnings.append(f"knowledge file {rel} is invalid JSON: {exc}")
            return None

    def _load_primary(self) -> list[tuple[str, str, str, str]]:
        """fst_terms.json -> list of (form, canonical, tier_name, source)."""
        data = self._read_json(self.terms_rel, required=True)
        if data is None:  # pragma: no cover - required=True raises above
            return []
        tiers = data.get("tiers", {})
        if not isinstance(tiers, dict) or not tiers:
            raise FstError(f"{self.terms_rel} is missing a non-empty 'tiers' map")
        out: list[tuple[str, str, str, str]] = []
        for rule in data.get("rules", []):
            form = normalize_text(str(rule.get("form", "")))
            canonical = str(rule.get("canonical", "")).strip()
            tier_name = rule.get("tier", "validated_term")
            if not form or not canonical or form == canonical:
                continue
            if tier_name not in tiers:
                raise FstError(
                    f"{self.terms_rel}: unknown tier '{tier_name}' "
                    f"(known: {sorted(tiers)})"
                )
            out.append((form, canonical, tier_name, "fst_terms.json"))
        if not out:
            raise FstError(f"{self.terms_rel} contains no usable rules")
        return out

    def _load_extra(self) -> list[tuple[str, str, str, str]]:
        """Merge the other knowledge files, each in its own tier."""
        out: list[tuple[str, str, str, str]] = []

        data = self._read_json(self.extra_sources[0])  # observed_asr_aliases.json
        if isinstance(data, dict):
            for canonical, info in data.items():
                forms = list(info.get("spoken_forms", [])) + list(
                    info.get("common_asr_forms", [])
                )
                for form in forms:
                    out.append((str(form), str(canonical), "observed_alias",
                                "observed_asr_aliases.json"))

        data = self._read_json(self.extra_sources[1])  # abbreviations.json
        if isinstance(data, dict):
            for canonical, info in data.items():
                forms = list(info.get("spoken", [])) + list(info.get("persian", []))
                for form in forms:
                    out.append((str(form), str(info.get("canonical", canonical)),
                                "abbreviation", "abbreviations.json"))

        data = self._read_json(self.extra_sources[2])  # nursing_phrases.json
        if isinstance(data, list):
            for phrase in data:
                for form in phrase.get("spoken_forms", []):
                    out.append((str(form), str(phrase.get("canonical", "")),
                                "phrase", "nursing_phrases.json"))

        data = self._read_json(self.extra_sources[3])  # nursing_terms.json
        if isinstance(data, dict):
            for entry in data.get("entries", []):
                forms = list(entry.get("spoken_forms", [])) + list(
                    entry.get("common_asr_forms", [])
                )
                for form in forms:
                    out.append((str(form), str(entry.get("canonical", "")),
                                "validated_term", "nursing_terms.json"))

        return out

    def _build_rules(self, raw: list[tuple[str, str, str, str]]) -> None:
        # Tier numbering comes from the primary file's 'tiers' order.
        data = self._read_json(self.terms_rel)
        tiers = data.get("tiers", {}) if data else {}
        tier_of = {name: idx for idx, name in enumerate(tiers)}

        # Deduplicate: identical (form, canonical) pairs collapse regardless
        # of source; per-form conflicts are resolved deterministically by
        # (source group, tier): curated fst_terms.json rules first, then the
        # lower tier wins.
        unique: dict[tuple[str, str], FstRule] = {}
        for form, canonical, tier_name, source in raw:
            form = normalize_text(form)
            canonical = canonical.strip()
            if not form or not canonical or form == canonical:
                continue
            if any(
                ch in _BOUNDARY_CHARS and ch != " " for ch in form
            ):
                self.warnings.append(
                    f"skipping rule form with punctuation: {form!r} "
                    f"(-> {canonical!r}, {source})"
                )
                continue
            tier = tier_of.get(tier_name)
            if tier is None:
                raise FstError(
                    f"rule form '{form}' has unknown tier '{tier_name}' "
                    f"(known: {sorted(tier_of)})"
                )
            key = (form, canonical)
            if key not in unique:
                unique[key] = FstRule(
                    form=form, canonical=canonical, tier=tier, source=source
                )

        def priority(rule: FstRule) -> tuple:
            group = 0 if rule.source == "fst_terms.json" else 1
            return (group, rule.tier)

        # Keyed by the CASE-FOLDED form: matching is case-insensitive, so two
        # rules differing only in case are the same rule and must resolve
        # deterministically through the existing priority/warning path
        # instead of racing inside the automaton.
        best: dict[str, FstRule] = {}
        for rule in sorted(unique.values(), key=priority):
            folded = casefold_preserving(rule.form)
            cur = best.get(folded)
            if cur is None:
                best[folded] = rule
            elif cur.canonical != rule.canonical:
                self.warnings.append(
                    f"conflicting canonicals for form '{rule.form}': "
                    f"keeping '{cur.canonical}' ({cur.source}, tier {cur.tier}) "
                    f"over '{rule.canonical}' ({rule.source}, tier {rule.tier})"
                )

        ordered = [
            FstRule(form=r.form, canonical=r.canonical, tier=r.tier,
                    source=r.source, seq=seq,
                    folded=casefold_preserving(r.form))
            for seq, r in enumerate(best.values())
        ]
        self.rules = ordered

        by_first: dict[str, list[FstRule]] = {}
        for rule in self.rules:
            by_first.setdefault(rule.match_form[0], []).append(rule)
        for lst in by_first.values():
            lst.sort(key=lambda r: r.key)
        self._by_first = by_first

    # -------------------------------------------------------------- automaton

    def _build_automaton(self) -> None:
        """Learn all rule forms once, up front (the core of Aho-Corasick)."""
        if not self.rules:
            return
        # Case-insensitive MATCHING: the automaton learns the case-folded
        # forms and later searches the case-folded text. Output always uses
        # the untouched canonical value and the untouched original text.
        forms = [rule.match_form for rule in self.rules]
        self.uses_ahocorasick = _native_ac is not None
        if _native_ac is not None:
            automaton = _native_ac.Automaton()
            for idx, form in enumerate(forms):
                automaton.add_word(form, idx)
            automaton.make_automaton()
            self._ac_native = automaton
        else:
            self._ac_python = AhoAutomaton(forms)

    def _iter_matches(self, text: str) -> Iterator[tuple[int, int]]:
        """Yield ``(end_index_inclusive, rule_index)`` for every raw match."""
        if self._ac_native is not None:
            yield from self._ac_native.iter(text)
        elif self._ac_python is not None:
            yield from self._ac_python.iter(text)

    @staticmethod
    def _is_start_boundary(text: str, i: int) -> bool:
        """Position i is a token start (beginning of text or boundary before)."""
        return i == 0 or text[i - 1] in _BOUNDARY_CHARS

    @staticmethod
    def _is_end_boundary(text: str, i: int) -> bool:
        """Position i is a token end (end of text or boundary at i)."""
        return i == len(text) or text[i] in _BOUNDARY_CHARS

    # --------------------------------------------------------------- engines

    def _scan(self, text: str) -> tuple[str, list[dict[str, Any]]]:
        """Aho-Corasick scan: one linear pass finds every candidate.

        Candidates are filtered to token boundaries, then the greedy
        left-to-right rule applies: at each boundary position, the best
        candidate (longest, then tier, then stable order) is emitted and
        scanning resumes after it; everything else is copied verbatim.
        """
        candidates_at: dict[int, list[tuple[int, FstRule]]] = {}
        length = len(text)
        # Search the case-folded projection of the text; ``casefold_preserving``
        # guarantees index i of ``haystack`` is index i of ``text``, so every
        # position below is applied to the ORIGINAL logical text.
        haystack = casefold_preserving(text)
        for end, idx in self._iter_matches(haystack):
            rule = self.rules[idx]
            start = end - len(rule.match_form) + 1
            end_excl = end + 1
            # Token-aware: an Aho-Corasick match inside a longer token
            # must be discarded (never a substring replacement).
            if not self._is_start_boundary(text, start):
                continue
            if not self._is_end_boundary(text, end_excl):
                continue
            if end_excl > length:
                continue  # pragma: no cover - cannot happen
            bucket = candidates_at.get(start)
            entry = (end_excl, rule)
            if bucket is None:
                candidates_at[start] = [entry]
            else:
                bucket.append(entry)

        out: list[str] = []
        hits: list[dict[str, Any]] = []
        i = 0
        while i < length:
            candidates = candidates_at.get(i)
            if candidates:
                best_end, best_rule = candidates[0]
                for end, rule in candidates[1:]:
                    if rule.key < best_rule.key:
                        best_end, best_rule = end, rule
                out.append(best_rule.canonical)
                hits.append({
                    "form": best_rule.form,
                    "canonical": best_rule.canonical,
                    "tier": best_rule.tier,
                    "source": best_rule.source,
                    "position": i,
                })
                i = best_end
                continue
            out.append(text[i])
            i += 1
        return "".join(out), hits

    def _scan_reference(self, text: str) -> tuple[str, list[dict[str, Any]]]:
        """Deterministic naive scanner (reference implementation).

        Implements the identical priority scheme per position instead of an
        automaton. Kept for parity testing and as the degradation path if
        the automaton engine ever fails.
        """
        out: list[str] = []
        hits: list[dict[str, Any]] = []
        i = 0
        length = len(text)
        haystack = casefold_preserving(text)
        while i < length:
            if self._is_start_boundary(text, i):
                best: Optional[FstRule] = None
                for rule in self._by_first.get(haystack[i], ()):
                    end = i + len(rule.match_form)
                    if haystack.startswith(rule.match_form, i) and self._is_end_boundary(
                        text, end
                    ):
                        if best is None or rule.key < best.key:
                            best = rule
                if best is not None:
                    out.append(best.canonical)
                    hits.append({
                        "form": best.form,
                        "canonical": best.canonical,
                        "tier": best.tier,
                        "source": best.source,
                        "position": i,
                    })
                    i += len(best.form)
                    continue
            out.append(text[i])
            i += 1
        return "".join(out), hits

    # --------------------------------------------------------------- public

    @property
    def engine(self) -> str:
        """Human-readable engine identifier for banners/reports."""
        if self._ac_native is not None:
            return f"{ENGINE_NAME} (pyahocorasick)"
        if self._ac_python is not None:
            return f"{ENGINE_NAME} (pure-python)"
        return "none (no rules)"

    def canonicalize(self, text: str) -> tuple[str, list[dict[str, Any]]]:
        """Canonicalize already-normalized finalized text.

        Returns ``(canonical_text, hits)`` where each hit records which rule
        fired, where, and its provenance. Deterministic: same input always
        yields the same output and hit list.
        """
        if not text:
            return text, []
        if not self.rules:
            return text, []
        try:
            return self._scan(text)
        except Exception as exc:
            # An engine failure must never cost the clinician their finished
            # transcript: the naive scanner implements the identical priority
            # scheme and is verified against the automaton by the test suite.
            self.warnings.append(
                f"aho-corasick engine failed for {text[:40]!r} ({exc}); "
                f"using the equivalent reference scanner output"
            )
            return self._scan_reference(text)
