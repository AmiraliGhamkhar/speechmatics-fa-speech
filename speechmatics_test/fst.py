"""Deterministic medical lexical canonicalization (FST layer).

Pipeline position (applied to FINAL ASR segments only, never to partials):

    Speechmatics FINAL  ->  normalize_text()  ->  MedicalFST.canonicalize()
                                              ->  canonical medical transcript

Design
------
- Rules come from ``medical_knowledge/fst_terms.json`` (the curated primary
  rules) plus the other knowledge files, each kept in its own tier so that
  observed ASR aliases, abbreviations, phrases and validated vocabulary never
  mix implicitly. Every rule keeps its source and tier for auditability.
- The transducer is an OpenFst/Pynini FST over Unicode codepoint labels with
  two boundary states:

      state 0 = "at a token boundary"  (start of text / after boundary char)
      state 1 = "inside a token"

  A rule chain may only START in state 0 and must be FOLLOWED by a boundary
  character (or end of input), which makes matching token-aware rather than
  unsafe substring replacement. (Codepoint labels are used instead of UTF-8
  bytes because Persian punctuation and letters share UTF-8 lead bytes, which
  would make byte-level boundary detection ambiguous.)
- At any position the FST can either copy one character (weight
  ``COPY_WEIGHT``) or follow one rule chain. Rule weight is
  ``(lmax - len(form)) * LENGTH_STEP + tier * TIER_STEP + seq * SEQ_STEP``
  so the shortest path is exactly: longest match first, then tier priority,
  then stable rule order. With ``COPY_WEIGHT = 1.0`` a longer match always
  beats a shorter one even across tiers, which makes the single global
  shortest path equal to a greedy left-to-right longest match.
- No semantic inference: the FST only performs deterministic lexical
  replacement. It never infers diagnosis, severity, negation or dosage
  correctness.

If Pynini cannot be imported (minimal environments), a deterministic pure
Python scanner implementing the same priority scheme produces the output;
``MedicalFST.uses_pynini`` exposes which backend is active.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .text import normalize_text

try:  # pragma: no cover - depends on environment
    import pynini
except ImportError:  # pragma: no cover
    pynini = None

__all__ = ["MedicalFST", "FstError", "FstRule"]


class FstError(RuntimeError):
    """Raised when the FST rule set is invalid or an FST operation fails."""


#: Characters that separate tokens (whitespace + common Latin/Persian
#: punctuation). Both the byte set (for the FST) and the char set (for the
#: scanner) are derived from this single source of truth.
_BOUNDARY_CHARACTERS = (
    " \t\n\r"
    ".,;:!?'\"()[]{}<>+-/#&%$=@`"
    "،؛؟«»…"
)
_BOUNDARY_CHARS = frozenset(_BOUNDARY_CHARACTERS)
_BOUNDARY_ORDS = frozenset(map(ord, _BOUNDARY_CHARACTERS))

#: Per-rule weight constants (tropical semiring, lower = preferred).
COPY_WEIGHT = 1.0      # cost of copying one character
LENGTH_STEP = 0.01     # longer forms get cheaper: (lmax - len) * LENGTH_STEP
TIER_STEP = 0.001      # lower tier wins for equal-length forms
SEQ_STEP = 1e-6        # total order: stable rule order breaks remaining ties


@dataclass(frozen=True)
class FstRule:
    """One deterministic input-form -> canonical mapping."""

    form: str       # normalized input form (token-aware, exact match)
    canonical: str  # replacement text
    tier: int       # 0 = highest priority
    source: str     # provenance, e.g. "fst_terms.json" or "observed_asr_aliases.json"
    seq: int = 0    # stable ordering index (assigned at load time)

    @property
    def key(self) -> tuple:
        """Priority key: longest first, then tier, then order."""
        return (-len(self.form), self.tier, self.seq)


@dataclass
class MedicalFST:
    """Loads the medical rules and canonicalizes finalized transcript text."""

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
    uses_pynini: bool = field(default=False, init=False, repr=False)
    _lmax: int = field(default=0, init=False, repr=False)
    _fst_cache: dict = field(default_factory=dict, init=False, repr=False)
    _by_first: dict = field(default_factory=dict, init=False, repr=False)

    # ------------------------------------------------------------------ load

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.uses_pynini = pynini is not None
        raw = self._load_primary()
        raw += self._load_extra()
        self._build_rules(raw)
        self._lmax = max((len(r.form) for r in self.rules), default=0)
        if not self.rules:
            self.warnings.append("medical FST has no rules; canonicalization is a no-op")

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

        best: dict[str, FstRule] = {}
        for rule in sorted(unique.values(), key=priority):
            cur = best.get(rule.form)
            if cur is None:
                best[rule.form] = rule
            elif cur.canonical != rule.canonical:
                self.warnings.append(
                    f"conflicting canonicals for form '{rule.form}': "
                    f"keeping '{cur.canonical}' ({cur.source}, tier {cur.tier}) "
                    f"over '{rule.canonical}' ({rule.source}, tier {rule.tier})"
                )

        ordered = [
            FstRule(form=r.form, canonical=r.canonical, tier=r.tier,
                    source=r.source, seq=seq)
            for seq, r in enumerate(best.values())
        ]
        self.rules = ordered

        by_first: dict[str, list[FstRule]] = {}
        for rule in self.rules:
            by_first.setdefault(rule.form[0], []).append(rule)
        for lst in by_first.values():
            lst.sort(key=lambda r: r.key)
        self._by_first = by_first

    # --------------------------------------------------------------- pynini

    @staticmethod
    def _acceptor(text: str) -> Any:
        """String acceptor over Unicode codepoint labels (one arc/char).

        pynini.accep() would encode the text as UTF-8 bytes, which is
        ambiguous for boundary detection (e.g. 0xD8 is both the lead byte
        of Persian punctuation and of Persian letters), so we build the
        acceptor over codepoints instead.
        """
        f = pynini.Fst()
        f.add_state()
        f.set_start(0)  # NOTE: add_state() does not set start in pynini 2.1.x
        cur = 0
        for ch in text:
            f.add_state()
            nxt = f.num_states() - 1
            f.add_arc(cur, pynini.Arc(ord(ch), ord(ch), 0.0, nxt))
            cur = nxt
        f.set_final(cur)
        return f

    def _get_fst(self, alphabet: frozenset) -> Any:
        """Build the transducer for a given alphabet of codepoint labels."""
        cached = self._fst_cache.get(alphabet)
        if cached is not None:
            return cached

        f = pynini.Fst()
        f.add_state()      # 0 = at token boundary (start)
        f.add_state()      # 1 = inside token
        f.set_start(0)
        f.set_final(0)
        f.set_final(1)

        # Copy arcs, keeping the boundary state consistent.
        for c in sorted(alphabet):
            target = 0 if c in _BOUNDARY_ORDS else 1
            f.add_arc(0, pynini.Arc(c, c, COPY_WEIGHT, target))
            f.add_arc(1, pynini.Arc(c, c, COPY_WEIGHT, target))

        # Rule chains: only start at a boundary, must end at a boundary.
        for rule in self.rules:
            form_ords = [ord(c) for c in rule.form]
            canon_ords = [ord(c) for c in rule.canonical]
            weight = (
                (self._lmax - len(form_ords)) * LENGTH_STEP
                + rule.tier * TIER_STEP
                + rule.seq * SEQ_STEP
            )
            n = max(len(form_ords), len(canon_ords))
            f.add_state()
            end = f.num_states() - 1  # final (end-of-input) + boundary arcs
            f.set_final(end)
            cur = 0
            for k in range(n):
                il = form_ords[k] if k < len(form_ords) else 0
                ol = canon_ords[k] if k < len(canon_ords) else 0
                if k == n - 1:
                    f.add_arc(cur, pynini.Arc(il, ol, weight, end))
                else:
                    f.add_state()
                    nxt = f.num_states() - 1
                    f.add_arc(cur, pynini.Arc(il, ol, 0.0, nxt))
                    cur = nxt
            # After the form: a boundary char keeps us at the boundary state.
            for c in sorted(alphabet):
                if c in _BOUNDARY_ORDS:
                    f.add_arc(end, pynini.Arc(c, c, COPY_WEIGHT, 0))

        pynini.arcsort(f, sort_type="ilabel")
        self._fst_cache[alphabet] = f
        return f

    def _run_pynini(self, text: str) -> str:
        alphabet = frozenset(map(ord, text)) | frozenset(
            ord(c) for rule in self.rules for c in rule.form
        )
        try:
            fst = self._get_fst(alphabet)
            composed = pynini.compose(self._acceptor(text), fst)
            path = pynini.shortestpath(composed)
            state = path.start()
            out = []
            steps = 0
            while float(path.final(state)) != 0.0:
                arcs = list(path.arcs(state))
                if len(arcs) != 1:  # pragma: no cover - cannot happen on a path
                    raise FstError("FST shortest path is not deterministic")
                if arcs[0].olabel:
                    out.append(chr(arcs[0].olabel))
                state = arcs[0].nextstate
                steps += 1
                if steps > 1_000_000:  # pragma: no cover
                    raise FstError("FST path is unexpectedly long")
            return "".join(out)
        except FstError:
            raise
        except Exception as exc:  # pragma: no cover - backend failure
            raise FstError(f"Pynini canonicalization failed: {exc}") from exc

    # --------------------------------------------------------------- scanner

    @staticmethod
    def _is_start_boundary(text: str, i: int) -> bool:
        """Position i is a token start (beginning of text or boundary before)."""
        return i == 0 or text[i - 1] in _BOUNDARY_CHARS

    @staticmethod
    def _is_end_boundary(text: str, i: int) -> bool:
        """Position i is a token end (end of text or boundary at i)."""
        return i == len(text) or text[i] in _BOUNDARY_CHARS

    def _scan(self, text: str) -> tuple[str, list[dict[str, Any]]]:
        """Deterministic greedy scan: longest match, then tier, then order.

        Produces the canonical output and the hit list in a single pass.
        """
        out: list[str] = []
        hits: list[dict[str, Any]] = []
        i = 0
        length = len(text)
        while i < length:
            if self._is_start_boundary(text, i):
                best: Optional[FstRule] = None
                for rule in self._by_first.get(text[i], ()):
                    end = i + len(rule.form)
                    if text.startswith(rule.form, i) and self._is_end_boundary(
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

    def canonicalize(self, text: str) -> tuple[str, list[dict[str, Any]]]:
        """Canonicalize already-normalized finalized text.

        Returns ``(canonical_text, hits)`` where each hit records which rule
        fired, where, and its provenance. Deterministic: same input always
        yields the same output and hit list.
        """
        if not text:
            return text, []
        scan_out, hits = self._scan(text)
        if self.uses_pynini:
            canonical = self._run_pynini(text)
            # The scanner is the reference implementation used for hit
            # metadata; the two must agree (identical priority scheme).
            if canonical != scan_out:
                self.warnings.append(
                    f"FST/scanner mismatch for {text[:40]!r}: FST={canonical!r} "
                    f"scanner={scan_out!r} - using FST output"
                )
            return canonical, hits
        return scan_out, hits
