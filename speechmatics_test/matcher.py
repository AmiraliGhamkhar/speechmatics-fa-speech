"""Deterministic medical lexical canonicalization (Aho-Corasick matcher).

Pipeline position (applied to FINAL ASR segments only, never partials):

    Speechmatics final  ->  normalize_text()  ->  MedicalMatcher.canonicalize()
                                              ->  canonical medical transcript

Contract
--------
- Matching is exact and token-aware: both sides of a rule form must border a
  boundary character (or the text edge); never an unsafe substring replace.
- Longest match wins. Ties are impossible at match time (one rule per folded
  form), so the tier order (curated > abbreviation > observed_alias > phrase
  > validated_term > unit) and stable rule order resolve conflicts at LOAD
  time, deterministically, with a reported warning.
- No semantic inference: deterministic lexical replacement only.
- Every hit reports ``form``, ``canonical``, ``tier``, ``source`` and
  ``position`` for auditability.
- Word-level ASR evidence (confidence/language) can only annotate a lexical
  hit or break an otherwise equal tie; it never creates a correction.

Engines
-------
1. ``pyahocorasick`` (native C) - the production backend.
2. ``AhoAutomaton`` - a built-in pure-Python automaton with identical
   output, used automatically when the native package is missing.
3. ``_scan_reference`` - a naive per-position scanner kept as the verified
   reference implementation (parity-tested against the automaton) and used
   as the degradation path if the engine ever fails, so a finished
   transcript is never lost to an engine bug.

The class is exposed as ``MedicalFST`` (alias) for API stability: the
historical name predates the Aho-Corasick engine and is kept on purpose.
"""

from __future__ import annotations

import hashlib
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

__all__ = [
    "MedicalMatcher", "MedicalFST", "FstError", "MedicalRule", "FstRule",
    "AhoAutomaton", "ENGINE_NAME", "TIER_ORDER", "TIER_RANK", "TERM_TYPES",
    "DictionaryTerm", "casefold_preserving", "load_dictionary", "build_matcher",
]

#: Public engine identifier (reported by the app banner and the reports).
ENGINE_NAME = "aho-corasick"

#: Deterministic priority order for conflicting canonicals (index = rank).
TIER_ORDER = (
    "curated",          # hand-curated rule set (legacy fst_terms.json)
    "abbreviation",     # clinical abbreviations (legacy abbreviations.json)
    "observed_alias",   # observed ASR forms (legacy observed_asr_aliases.json)
    "phrase",           # nursing phrases (legacy nursing_phrases.json)
    "validated_term",   # validated terms (legacy nursing_terms.json)
    "unit",             # dosage units (legacy unit-level fst_terms rules)
)
TIER_RANK = {name: rank for rank, name in enumerate(TIER_ORDER)}

#: Enumerated term categories (free-form clinical categorization, documented
#: for the dictionary author; not used for matching priority).
TERM_TYPES = frozenset({
    "condition",      # diseases / findings (hypertension, lesion, ...)
    "drug",           # medications (metformin, heparin, ...)
    "procedure",      # interventions (intubation, biopsy, ...)
    "imaging",        # imaging studies (MRI, CT scan, ultrasound, ...)
    "lab",            # lab values / analytes (HbA1c, troponin, Mg, ...)
    "anatomy",        # anatomical structures (right lung, C3-C4, ...)
    "abbreviation",   # clinical abbreviations (BP, HTN, NPO, ...)
    "dosage_unit",    # dose units (mg, mL, mmHg, ...)
    "route",          # administration routes (IV, IM, SC, ...)
    "vital_sign",     # vital signs (heart rate, O2 saturation, ...)
    "phrase",         # nursing expressions ("vital signs", ...)
    "term",           # validated generic clinical term
})

_DICTIONARY_VERSION = 1


class FstError(RuntimeError):
    """Raised when the medical dictionary is invalid or matching fails."""


#: Characters that separate tokens (whitespace + common Latin/Persian
#: punctuation). Shared by the automaton boundary filter, the reference
#: scanner and the word-alignment logic - one source of truth.
_BOUNDARY_CHARACTERS = (
    " \t\n\r"
    ".,;:!?'\"()[]{}<>+-/#&%$=@`"
    "،؛؟«»…"
)
_BOUNDARY_CHARS = frozenset(_BOUNDARY_CHARACTERS)
_LOW_CONFIDENCE_THRESHOLD = 0.75

#: Case-folded rule forms that collide with common, unrelated English words
#: (a plain sentence saying "now"/"or" must not be rewritten). These short
#: clinical shorthand aliases only fire on stronger evidence than ordinary
#: case-insensitive matching: the ORIGINAL (unfolded) matched text must be
#: fully uppercase (the conventional way these are actually charted, e.g.
#: "AC"/"PC"/"HS"/"OD"/"DIFF"/"NOW"/"OR"/"P"), never a lowercase or
#: mixed-case ordinary-English occurrence. Safe, unambiguous abbreviations
#: (MRI, CT, ECG, CXR, HbA1c, SpO2, ...) are NOT in this set and keep the
#: normal case-insensitive behavior - they do not collide with common words.
#: Persian-script forms and fully-spelled English aliases for the SAME
#: underlying terms (e.g. "قبل از غذا", "ante cibum", "before food") are a
#: different ``match_form`` entirely and are therefore unaffected by this
#: restriction; they were already unambiguous "stronger evidence" per se.
#: Extended after the nursing benchmark caught real regressions in ordinary
#: English prose: "Please do it now" became "Please do intrathecal now"
#: (``it`` -> intrathecal) and "keep the patient warm, not cold" would have
#: become "... not chronic obstructive pulmonary disease" (``cold`` -> COPD).
#: Each addition is an ordinary English word that is ALSO charted shorthand;
#: requiring the uppercase charting form keeps the clinical meaning available
#: ("IT", "COLD", "US") while leaving prose untouched.
_AMBIGUOUS_SHORT_FORMS = frozenset({
    "or", "p", "now", "diff", "ac", "pc", "hs", "od",
    "it",     # intrathecal  vs the pronoun
    "be",     # barium enema vs the verb
    "cold",   # COPD         vs the adjective
    "him",    # medical records (HIM) vs the pronoun
    "lab",    # laboratory   vs the ordinary noun (already English)
    "post",   # after        vs "post-operative"/"post" the noun
    "skin",   # dermatologic vs the ordinary noun
    "soft",   # soft diet    vs the adjective
    "top",    # topical      vs the ordinary noun
    "us",     # ultrasound   vs the pronoun
    "am",     # AM           vs the verb "am"
    "cap",    # capsule      vs the ordinary noun
    "reg",    # regular diet
    "sr",     # review of systems
    "ss",     # one half
    "ca",     # calcium / carcinoma - far too ambiguous unmarked
    "ob",     # obstetrics vs occult blood
    "pe",     # pulmonary embolism vs physical examination
    "pt",     # physical therapy vs prothrombin time vs patient
})


def _language_group(language: Any) -> str:
    """Reduce optional ASR language tags to the three useful match signals."""
    value = str(language or "").lower()
    if value.startswith(("fa", "fas", "per", "persian")):
        return "Persian"
    if value.startswith(("en", "eng", "english")):
        return "English"
    return "unknown"


def _form_language(form: str) -> str:
    """Infer only the script of a lexical rule form, never its meaning."""
    if any("\u0600" <= ch <= "\u08ff" for ch in form):
        return "Persian"
    if any(ch.isascii() and ch.isalpha() for ch in form):
        return "English"
    return "unknown"


def casefold_preserving(text: str) -> str:
    """Case-insensitive folding that is guaranteed length-preserving.

    ``str.casefold()`` is the correct Unicode case-insensitive mapping, but a
    handful of code points expand (``\\u00df`` -> ``ss``, ``\\ufb00`` -> ``ff``).
    An expansion would shift every following index, so match positions
    returned by the automaton could no longer be used against the ORIGINAL
    text. Folding character by character and keeping any expanding character
    unchanged keeps ``len(fold(t)) == len(t)``: index ``i`` of the folded
    string always describes index ``i`` of the original.

    Persian/Arabic letters are caseless, so Persian text is never modified.
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


# ------------------------------------------------------------------ schema


@dataclass(frozen=True)
class DictionaryTerm:
    """One validated ``medical_dictionary.json`` entry.

    ``forms`` may be empty only for vocab-only terms (``speechmatics`` true):
    they bias the ASR vocabulary but contribute no matcher rule.
    """

    id: str
    canonical: str
    type: str
    tier: str
    forms: tuple[str, ...]
    speechmatics: bool = False
    sounds_like: tuple[str, ...] = ()
    source: str = "medical_dictionary.json"


def _validate_term(entry: Any, index: int, seen_ids: set[str],
                   seen_canonicals: set[str]) -> DictionaryTerm:
    """Validate one dictionary entry; raise FstError on structural problems.

    Form-level hygiene (punctuation, empty-after-normalize) is NOT checked
    here - those produce load warnings, not errors, because observed ASR
    data legitimately contains such forms.
    """
    where = f"term #{index}"
    if not isinstance(entry, dict):
        raise FstError(f"{where}: entry must be an object, got {type(entry).__name__}")

    term_id = entry.get("id")
    if not isinstance(term_id, str) or not term_id.strip():
        raise FstError(f"{where}: missing or empty 'id'")
    where = f"term {term_id!r}"
    if term_id in seen_ids:
        raise FstError(f"{where}: duplicate id")
    seen_ids.add(term_id)

    canonical = entry.get("canonical")
    if not isinstance(canonical, str) or not canonical.strip():
        raise FstError(f"{where}: missing or empty 'canonical'")
    canonical = canonical.strip()
    if canonical in seen_canonicals:
        raise FstError(
            f"{where}: duplicate canonical {canonical!r} - merge its forms "
            f"into the existing term instead"
        )
    seen_canonicals.add(canonical)

    type_ = entry.get("type")
    if type_ not in TERM_TYPES:
        raise FstError(
            f"{where}: unknown type {type_!r} (known: {sorted(TERM_TYPES)})"
        )

    tier = entry.get("tier")
    if tier not in TIER_RANK:
        raise FstError(
            f"{where}: missing or unknown tier {tier!r} (known: {list(TIER_ORDER)})"
        )

    raw_forms = entry.get("forms", [])
    if not isinstance(raw_forms, list) or any(
        not isinstance(form, str) or not form.strip() for form in raw_forms
    ):
        raise FstError(f"{where}: 'forms' must be a list of non-empty strings")
    speechmatics = entry.get("speechmatics", False)
    if not isinstance(speechmatics, bool):
        raise FstError(f"{where}: 'speechmatics' must be a boolean")
    if not raw_forms and not speechmatics:
        raise FstError(
            f"{where}: empty 'forms' is only valid for vocab-only terms "
            f"(speechmatics: true)"
        )

    sounds_like = entry.get("sounds_like", [])
    if not isinstance(sounds_like, list) or any(
        not isinstance(sound, str) or not sound.strip() for sound in sounds_like
    ):
        raise FstError(f"{where}: 'sounds_like' must be a list of non-empty strings")

    source_file = entry.get("source_file")
    if source_file is not None and (
        not isinstance(source_file, str) or not source_file.strip()
    ):
        raise FstError(f"{where}: 'source_file' must be a non-empty string")

    return DictionaryTerm(
        id=term_id,
        canonical=canonical,
        type=type_,
        tier=tier,
        forms=tuple(raw_forms),
        speechmatics=speechmatics,
        sounds_like=tuple(sounds_like),
        source=source_file if source_file else "medical_dictionary.json",
    )


def load_dictionary(path: Path) -> tuple[list[DictionaryTerm], list[str]]:
    """Read and validate ``medical_dictionary.json``.

    Returns ``(terms, warnings)``. Raises FstError for missing files,
    malformed JSON, an unsupported schema version, or any structurally
    invalid entry (missing tier, unknown type, empty forms on a non-vocab
    term, duplicate id/canonical, ...). Startup must fail clearly on
    invalid required dictionary data.
    """
    path = Path(path)
    if not path.exists():
        raise FstError(f"medical dictionary not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FstError(f"medical dictionary {path} is invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise FstError(f"medical dictionary {path} must be a JSON object")
    version = data.get("version")
    if version != _DICTIONARY_VERSION:
        raise FstError(
            f"medical dictionary {path}: unsupported version {version!r} "
            f"(expected {_DICTIONARY_VERSION})"
        )
    entries = data.get("terms")
    if not isinstance(entries, list):
        raise FstError(f"medical dictionary {path}: 'terms' must be a list")
    if not entries:
        raise FstError(f"medical dictionary {path}: 'terms' is empty")

    seen_ids: set[str] = set()
    seen_canonicals: set[str] = set()
    terms = [
        _validate_term(entry, index, seen_ids, seen_canonicals)
        for index, entry in enumerate(entries)
    ]
    return terms, []


# ------------------------------------------------------------------- rules


@dataclass(frozen=True)
class MedicalRule:
    """One deterministic input-form -> canonical mapping."""

    form: str       # normalized input form (token-aware, exact match)
    canonical: str  # replacement text
    tier: int       # rank in TIER_ORDER (0 = highest priority)
    source: str     # provenance (legacy source file or the dictionary)
    seq: int = 0    # stable ordering index (assigned at load time)
    #: Case-folded form used for MATCHING ONLY (never for output). Always
    #: the same length as ``form`` (see ``casefold_preserving``).
    folded: str = ""

    @property
    def match_form(self) -> str:
        """The string the automaton actually searches for (case-folded)."""
        return self.folded or casefold_preserving(self.form)

    @property
    def key(self) -> tuple:
        """Priority key: longest first, then tier, then stable order."""
        return (-len(self.form), self.tier, self.seq)


#: Back-compat alias: the rule type's historical name.
FstRule = MedicalRule


# ------------------------------------------------------- pure-python engine


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


# ----------------------------------------------------------------- matcher


#: Zero-width non-joiner: a Persian orthographic separator, not punctuation.
_ZWNJ = "\u200c"


def _is_pronounceable_hint(form: str) -> bool:
    """True when ``form`` is safe to send as a Speechmatics ``sounds_like``.

    ``sounds_like`` entries describe how a word is SPOKEN, so only letters,
    spaces and the ZWNJ are acceptable. Written-only variants such as
    ``B/P``, ``B.P.`` or ``C3-C4`` carry punctuation or digits the ASR
    cannot pronounce, and would be noise (or rejected) in the API payload.
    """
    if not form or not form.strip():
        return False
    return all(
        ch.isalpha() or ch.isspace() or ch == _ZWNJ for ch in form
    )


def _build_additional_vocab(terms: list[DictionaryTerm]) -> list:
    """Bounded Speechmatics ``additional_vocab`` from eligible terms only.

    Only ``speechmatics: true`` terms are considered - the vocabulary is a
    deliberately curated ASR-biasing list, never a dump of the dictionary.

    Each entry's ``sounds_like`` is the declared ``sounds_like`` PLUS the
    term's own spoken forms. Those forms are exactly how a nurse pronounces
    the term out loud (``BP`` is said ``فشار خون`` / ``بی پی``), so omitting
    them throws away the strongest biasing signal the dictionary has. This
    mirrors the hand-maintained artifact that shipped before the export was
    automated: it contained those forms, while a naive regeneration dropped
    them and silently weakened recognition.

    Forms that are written-only (punctuation or digits, e.g. ``B/P``) are
    excluded by :func:`_is_pronounceable_hint`, and the canonical itself is
    never repeated as its own pronunciation hint.
    """
    vocab: list[Any] = []
    for term in terms:
        if not term.speechmatics:
            continue
        hints: list[str] = []
        for candidate in (*term.sounds_like, *term.forms):
            if candidate == term.canonical or candidate in hints:
                continue
            if not _is_pronounceable_hint(candidate):
                continue
            hints.append(candidate)
        if hints:
            vocab.append({"content": term.canonical, "sounds_like": hints})
        else:
            vocab.append(term.canonical)
    return vocab


@dataclass
class MedicalMatcher:
    """Loads the medical dictionary and canonicalizes finalized transcript text.

    Despite the historical alias ``MedicalFST`` (the public API is stable),
    the engine is an Aho-Corasick automaton. The dictionary is loaded,
    validated, deduplicated and compiled exactly once at construction; no
    file I/O or rebuild happens afterwards.
    """

    root: Path
    dictionary_rel: str = "medical_knowledge/medical_dictionary.json"

    # populated by __post_init__
    terms: list[DictionaryTerm] = field(default_factory=list, init=False, repr=False)
    rules: list[MedicalRule] = field(default_factory=list, init=False, repr=False)
    warnings: list[str] = field(default_factory=list, init=False, repr=False)
    uses_ahocorasick: bool = field(default=False, init=False, repr=False)
    #: Bounded ASR vocabulary derived from ``speechmatics: true`` terms.
    additional_vocab: list = field(default_factory=list, init=False, repr=False)
    _ac_native: Any = field(default=None, init=False, repr=False)
    _ac_python: Optional[AhoAutomaton] = field(default=None, init=False, repr=False)
    _by_first: dict = field(default_factory=dict, init=False, repr=False)
    _form_prefixes: set = field(default_factory=set, init=False, repr=False)
    _strict_form_prefixes: set = field(default_factory=set, init=False, repr=False)

    # ---------------------------------------------------------------- load

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.terms, _ = load_dictionary(self.root / self.dictionary_rel)
        self.rules = self._compile_rules(self.terms)
        if not self.rules:
            self.warnings.append(
                "medical canonicalization layer has no rules; it is a no-op"
            )
        self._build_automaton()
        self.additional_vocab = _build_additional_vocab(self.terms)

    def _compile_rules(self, terms: list[DictionaryTerm]) -> list[MedicalRule]:
        """Normalize forms once, dedupe, resolve conflicts by tier + order.

        Forms are normalized (ZWNJ variants fold to the same form), rules
        are keyed by the case-folded form, and a form claimed by several
        terms with different canonicals resolves deterministically to the
        best ``(tier, stable term order)`` - every resolution is reported.

        A form that equals its OWN term's canonical is still registered as
        a (self -> self) rule here, even though both scan engines treat a
        match that already equals its rule's canonical as a no-op and emit
        no hit for it. Registering it anyway lets it WIN the tier conflict
        against a different, weaker-tier term that happens to list this
        exact string as one of ITS aliases - otherwise a term's own
        canonical spelling could be silently reassigned to another term's
        canonical (e.g. the abbreviation "CT" being rewritten to
        "computed tomography" because a lower-tier validated_term entry
        also lists "CT" as an alias).
        """
        # (tier, term_index) identifies the best claim per folded form.
        best: dict[str, tuple[tuple[int, int], MedicalRule]] = {}
        lost_canonicals: dict[str, set[str]] = {}

        def _consider(form: str, tier: int, source: str, priority: tuple[int, int]) -> None:
            folded = casefold_preserving(form)
            rule = MedicalRule(
                form=form, canonical=term.canonical,
                tier=tier, source=source, folded=folded,
            )
            current = best.get(folded)
            if current is None:
                best[folded] = (priority, rule)
                return
            if current[1].canonical == rule.canonical:
                # Same target: clean dedupe, no warning. But if the slot is
                # currently held by the term's own self-mapping placeholder
                # (form == canonical, registered only to arbitrate tier
                # conflicts against OTHER terms), a real alias form for the
                # same canonical must replace it - otherwise the alias is
                # silently dropped and the placeholder (which never matches
                # anything, by design) is compiled in its place.
                if current[1].form == current[1].canonical and form != term.canonical:
                    best[folded] = (priority, rule)
                return  # same target: clean dedupe, no warning
            lost = lost_canonicals.setdefault(folded, set())
            if current[0] <= priority:
                if rule.canonical not in lost:
                    lost.add(rule.canonical)
                    self.warnings.append(
                        f"conflicting canonicals for form {form!r}: "
                        f"keeping {current[1].canonical!r} "
                        f"({TIER_ORDER[current[1].tier]}, "
                        f"{current[1].source}) over {rule.canonical!r} "
                        f"({TIER_ORDER[tier]}, {source})"
                    )
            else:
                if current[1].canonical not in lost:
                    lost.add(current[1].canonical)
                    self.warnings.append(
                        f"conflicting canonicals for form {form!r}: "
                        f"keeping {rule.canonical!r} ({TIER_ORDER[tier]}, "
                        f"{source}) over "
                        f"{current[1].canonical!r} "
                        f"({TIER_ORDER[current[1].tier]}, "
                        f"{current[1].source})"
                    )
                best[folded] = (priority, rule)

        for term_index, term in enumerate(terms):
            priority = (TIER_RANK[term.tier], term_index)
            seen_forms: set[str] = set()

            # The term's own canonical spelling always participates in
            # arbitration first (bypassing the punctuation-form skip below,
            # which exists for genuine ALIAS hygiene, not for a term's own
            # canonical): this is what lets a term win the tier conflict
            # against a different, weaker-tier term that happens to list
            # this exact string as one of ITS aliases (see the docstring
            # above). A canonical containing punctuation (e.g. "U/A") is
            # never itself emitted as a matchable rule form change, but it
            # still must claim the slot so a lower-tier alias cannot.
            canonical_form = normalize_text(term.canonical)
            if canonical_form:
                seen_forms.add(canonical_form)
                _consider(canonical_form, priority[0], term.source, priority)

            for raw_form in term.forms:
                form = normalize_text(raw_form)
                if not form or form in seen_forms:
                    continue
                seen_forms.add(form)
                if any(ch in _BOUNDARY_CHARS and ch != " " for ch in form):
                    self.warnings.append(
                        f"skipping rule form with punctuation: {form!r} "
                        f"(-> {term.canonical!r}, {term.source})"
                    )
                    continue
                _consider(form, priority[0], term.source, priority)

        # Stable rule order: tier, then term order - matching the legacy
        # loader's deterministic seq assignment. Self-mapping rules (a
        # term's own canonical, registered above only to arbitrate cross-
        # term conflicts) are dropped here if they still win their own
        # slot: they would never produce a hit anyway (the scan engines
        # treat a match equal to its rule's canonical as a no-op), so
        # keeping them out of the compiled rule set avoids doubling
        # ``len(rules)`` for every ordinary term with no such conflict.
        ordered = [
            best[folded][1] for folded in sorted(best, key=lambda f: best[f][0])
            if best[folded][1].form != best[folded][1].canonical
        ]
        rules = [
            MedicalRule(form=r.form, canonical=r.canonical, tier=r.tier,
                        source=r.source, seq=seq, folded=r.folded)
            for seq, r in enumerate(ordered)
        ]

        by_first: dict[str, list[MedicalRule]] = {}
        for rule in rules:
            by_first.setdefault(rule.match_form[0], []).append(rule)
        for bucket in by_first.values():
            bucket.sort(key=lambda r: r.key)
        self._by_first = by_first

        # Token-prefix set of every rule form: lets the incremental
        # final-segment canonicalizer check "could this suffix still grow
        # into a longer rule" in O(tokens) instead of scanning all rules.
        # ``_form_prefixes`` includes each rule's own full token sequence
        # (kept for ``is_rule_token_prefix``'s documented "prefix OR full
        # form" contract). ``_strict_form_prefixes`` only contains PROPER
        # prefixes (strictly fewer tokens than some rule) - a tuple that is
        # merely a complete, non-extendable rule by itself (e.g. the
        # standalone one-token rule "iv", which is not a leading fragment
        # of any longer rule) is deliberately excluded, so the cross-segment
        # buffer in ``_safe_cut`` does not delay emitting it while waiting
        # for a continuation that no rule defines.
        prefixes: set[tuple[str, ...]] = set()
        strict_prefixes: set[tuple[str, ...]] = set()
        for rule in rules:
            form_tokens = rule.match_form.split()
            for take in range(1, len(form_tokens) + 1):
                prefixes.add(tuple(form_tokens[:take]))
            for take in range(1, len(form_tokens)):
                strict_prefixes.add(tuple(form_tokens[:take]))
        self._form_prefixes = prefixes
        self._strict_form_prefixes = strict_prefixes
        return rules

    # -------------------------------------------------------------- automaton

    def _build_automaton(self) -> None:
        """Learn all rule forms once, up front (the core of Aho-Corasick)."""
        if not self.rules:
            return
        # Case-insensitive MATCHING: the automaton learns the case-folded
        # forms and searches the case-folded text. Output always uses the
        # untouched canonical value and the untouched original text.
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

    # ----------------------------------------------------------- boundaries

    @staticmethod
    def _passes_ambiguous_short_form_guard(rule: "MedicalRule", matched: str) -> bool:
        """Ambiguous short forms (see ``_AMBIGUOUS_SHORT_FORMS``) require the
        ORIGINAL matched text to be fully uppercase before they fire; a
        lowercase or mixed-case occurrence is left untouched because it is
        far more likely to be the ordinary English word ("now", "or", ...)
        than clinical shorthand. Every other rule is unaffected (returns
        True unconditionally) - this is a targeted safety narrowing, not a
        general case-sensitivity change.
        """
        if rule.match_form not in _AMBIGUOUS_SHORT_FORMS:
            return True
        return matched.isupper()

    @staticmethod
    def _is_start_boundary(text: str, i: int) -> bool:
        """Position i is a token start (beginning of text or boundary before)."""
        return i == 0 or text[i - 1] in _BOUNDARY_CHARS

    @staticmethod
    def _is_end_boundary(text: str, i: int) -> bool:
        """Position i is a token end (end of text or boundary at i)."""
        return i == len(text) or text[i] in _BOUNDARY_CHARS

    # ------------------------------------------------------------- evidence

    def _word_spans(
        self, text: str, word_results: Optional[list[dict[str, Any]]]
    ) -> list[tuple[int, int, dict[str, Any]]]:
        """Align available final ASR words to this normalized segment.

        Alignment is deliberately exact and in order. If structured metadata
        does not line up with the transcript, it is ignored and the lexical
        behavior remains intact.
        """
        if not word_results:
            return []
        haystack = casefold_preserving(text)
        cursor = 0
        spans: list[tuple[int, int, dict[str, Any]]] = []
        for word in word_results:
            content = word.get("content") if isinstance(word, dict) else None
            if not isinstance(content, str):
                continue
            content = normalize_text(content)
            if not content:
                continue
            folded = casefold_preserving(content)
            start = haystack.find(folded, cursor)
            while start >= 0 and (
                not self._is_start_boundary(text, start)
                or not self._is_end_boundary(text, start + len(folded))
            ):
                start = haystack.find(folded, start + 1)
            if start < 0:
                continue
            end = start + len(folded)
            spans.append((start, end, word))
            cursor = end
        return spans

    @staticmethod
    def _span_evidence(
        rule: MedicalRule,
        start: int,
        end: int,
        word_spans: list[tuple[int, int, dict[str, Any]]],
    ) -> Optional[dict[str, Any]]:
        """Return auditable evidence only for a complete lexical word match."""
        matched = [
            word for word_start, word_end, word in word_spans
            if word_start >= start and word_end <= end
        ]
        if len(matched) != len(rule.form.split()):
            return None

        confidences = [
            float(word["confidence"])
            for word in matched
            if isinstance(word.get("confidence"), (int, float))
            and not isinstance(word.get("confidence"), bool)
        ]
        languages = [_language_group(word.get("language")) for word in matched]
        known_languages = [language for language in languages if language != "unknown"]
        expected_language = _form_language(rule.form)
        language_matches = (
            all(language == expected_language for language in known_languages)
            if known_languages and expected_language != "unknown" else None
        )
        confidence = min(confidences) if confidences else None
        return {
            "asr_confidence": round(confidence, 4) if confidence is not None else None,
            "asr_low_confidence": bool(
                confidence is not None and confidence < _LOW_CONFIDENCE_THRESHOLD
            ),
            "asr_language": (
                known_languages[0]
                if len(set(known_languages)) == 1 else "unknown"
            ),
            "asr_language_matches_form": language_matches,
        }

    @staticmethod
    def _candidate_key(
        rule: MedicalRule, evidence: Optional[dict[str, Any]]
    ) -> tuple:
        """Keep lexical precedence; use compatible low-confidence evidence last."""
        # Low-confidence evidence ranks the candidate WORSE (higher key),
        # exactly as the docstring promises. (Today this never decides
        # anything: two different rules of equal length cannot match the
        # same span, so this only guards the documented intent if equal
        # ties ever become possible.)
        evidence_rank = 1 if evidence and evidence["asr_low_confidence"] and \
            evidence["asr_language_matches_form"] is not False else 0
        return (-len(rule.form), rule.tier, evidence_rank, rule.seq)

    @staticmethod
    def _hit(rule: MedicalRule, position: int,
             evidence: Optional[dict[str, Any]]) -> dict[str, Any]:
        hit = {
            "form": rule.form,
            "canonical": rule.canonical,
            "tier": TIER_ORDER[rule.tier],
            "source": rule.source,
            "position": position,
        }
        if evidence is not None:
            hit.update(evidence)
        return hit

    # --------------------------------------------------------------- engines

    def _scan(
        self, text: str, word_results: Optional[list[dict[str, Any]]] = None
    ) -> tuple[str, list[dict[str, Any]]]:
        """Aho-Corasick scan: one linear pass finds every candidate.

        Candidates are filtered to token boundaries, then the greedy
        left-to-right rule applies: at each position only the LONGEST
        boundary-valid candidate is kept (same-length ties are impossible
        because the loader guarantees one rule per folded form, so the
        tier/order tie-break already happened at load time), it is emitted
        and scanning resumes after it; everything else is copied verbatim.
        """
        length = len(text)
        word_spans = self._word_spans(text, word_results)
        # Search the case-folded projection of the text; ``casefold_preserving``
        # guarantees index i of ``haystack`` is index i of ``text``, so every
        # position below applies to the ORIGINAL logical text.
        haystack = casefold_preserving(text)
        rules = self.rules

        best_at: dict[int, tuple[int, int]] = {}
        for end, idx in self._iter_matches(haystack):
            rule = rules[idx]
            start = end + 1 - len(rule.match_form)
            end_excl = end + 1
            # Token-aware: a match inside a longer token must be discarded
            # (never a substring replacement).
            if not (start == 0 or text[start - 1] in _BOUNDARY_CHARS):
                continue
            if not (end_excl == length or text[end_excl] in _BOUNDARY_CHARS):
                continue
            # Case-insensitive matching lets lowercase ASR variants map to
            # canonical casing, but an already canonical token is not a hit.
            if text[start:end_excl] == rule.canonical:
                continue
            # Ambiguous short forms (OR/P/NOW/DIFF/AC/PC/HS/OD) collide with
            # common English words and require stronger evidence: the
            # ORIGINAL matched text must be fully uppercase.
            if not self._passes_ambiguous_short_form_guard(rule, text[start:end_excl]):
                continue
            current = best_at.get(start)
            if current is None or end_excl > current[0]:
                best_at[start] = (end_excl, idx)

        out: list[str] = []
        hits: list[dict[str, Any]] = []
        i = 0
        copied = 0
        while i < length:
            found = best_at.get(i)
            if found is None:
                i += 1
                continue
            end_excl, idx = found
            rule = rules[idx]
            # Evidence only for the emitted winner: it never changes which
            # candidate wins (length dominates), so this is output-identical
            # to scoring every candidate.
            evidence = self._span_evidence(rule, i, end_excl, word_spans)
            if copied < i:
                out.append(text[copied:i])
            out.append(rule.canonical)
            hits.append(self._hit(rule, i, evidence))
            i = copied = end_excl
        if copied < length:
            out.append(text[copied:])
        return "".join(out), hits

    def _scan_reference(
        self, text: str, word_results: Optional[list[dict[str, Any]]] = None
    ) -> tuple[str, list[dict[str, Any]]]:
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
        word_spans = self._word_spans(text, word_results)
        while i < length:
            if self._is_start_boundary(text, i):
                best: Optional[tuple[MedicalRule, Optional[dict[str, Any]]]] = None
                for rule in self._by_first.get(haystack[i], ()):
                    end = i + len(rule.match_form)
                    if haystack.startswith(rule.match_form, i) and self._is_end_boundary(
                        text, end
                    ) and text[i:end] != rule.canonical \
                            and self._passes_ambiguous_short_form_guard(rule, text[i:end]):
                        evidence = self._span_evidence(rule, i, end, word_spans)
                        if best is None or self._candidate_key(rule, evidence) < \
                                self._candidate_key(*best):
                            best = (rule, evidence)
                if best is not None:
                    rule, evidence = best
                    out.append(rule.canonical)
                    hits.append(self._hit(rule, i, evidence))
                    i += len(rule.form)
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

    @property
    def max_rule_tokens(self) -> int:
        """Longest rule form measured in whitespace tokens (0 without rules)."""
        return max((len(rule.form.split()) for rule in self.rules), default=0)

    @property
    def repetition_safe_forms(self) -> frozenset:
        """Case-folded rule forms that contain a genuine repeated token.

        Persian spells a number of clinical abbreviations with a real
        doubled syllable - ``سی سی یو`` (CCU), ``آر آر`` (RR), ``تی تی``
        (TT), ``پی تی تی`` (PTT). A generic "collapse duplicated words"
        cleanup destroys exactly these terms (``سی سی یو`` -> ``سی یو``),
        so the nursing-text stutter rule consults this set and leaves them
        alone. Returned folded because the cleanup compares case-folded.

        Both the full form and each repeated ``"x x"`` pair inside it are
        included, so the rule can recognise the repetition either way.
        """
        safe: set[str] = set()
        for rule in self.rules:
            tokens = rule.match_form.split()
            for index in range(len(tokens) - 1):
                if tokens[index] == tokens[index + 1]:
                    safe.add(rule.match_form)
                    safe.add(f"{tokens[index]} {tokens[index + 1]}")
        return frozenset(safe)

    def is_rule_token_prefix(self, tokens: list[str]) -> bool:
        """True when ``tokens`` start some rule form (prefix or full form).

        Comparison mirrors the matcher exactly: normalized tokens,
        case-folded per ``casefold_preserving``. Kept for API stability and
        direct rule-form membership checks; the cross-segment buffer itself
        uses ``is_strict_rule_token_prefix`` (see there for why the
        distinction matters).
        """
        if not tokens:
            return False
        return tuple(casefold_preserving(token) for token in tokens) \
            in self._form_prefixes

    def is_strict_rule_token_prefix(self, tokens: list[str]) -> bool:
        """True when ``tokens`` are a PROPER prefix of some longer rule form.

        Unlike ``is_rule_token_prefix``, a tuple that only matches a rule's
        own complete, full-length form (and is not also a leading fragment
        of some OTHER, longer rule) returns False here. This is the correct
        check for cross-segment buffering: holding text back is only
        justified when a future ASR token could still extend it into a
        longer (longest-match-wins) rule. A standalone complete phrase such
        as the one-token rule "iv" is not a strict prefix of anything (no
        rule starts with "iv " followed by more tokens), so it must be
        emitted immediately instead of waiting for a continuation that no
        rule defines - the bug this method fixes delayed exactly such
        complete, non-extendable phrases by one segment for no reason.
        """
        if not tokens:
            return False
        return tuple(casefold_preserving(token) for token in tokens) \
            in self._strict_form_prefixes

    def canonicalize(
        self, text: str, word_results: Optional[list[dict[str, Any]]] = None
    ) -> tuple[str, list[dict[str, Any]]]:
        """Canonicalize a normalized FINAL transcript with optional ASR evidence.

        Confidence and language data can only annotate or break an otherwise
        equal lexical-rule tie. They never create a match, change a recognized
        canonical term, or override the established lexical precedence.
        """
        if not text or not self.rules:
            return text, []
        try:
            return self._scan(text, word_results)
        except Exception as exc:
            # An engine failure must never cost the clinician their finished
            # transcript: the naive scanner implements the identical priority
            # scheme and is verified against the automaton by the test suite.
            # The warning is copied into shared JSON reports, so it carries a
            # length + truncated SHA-256 digest instead of transcript content
            # (the digest is only a correlation aid, never reversible output).
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
            self.warnings.append(
                f"aho-corasick engine failed on {len(text)} chars "
                f"(sha256:{digest}) ({exc}); "
                f"using the equivalent reference scanner output"
            )
            return self._scan_reference(text, word_results)


#: Public API stability (§0): the class's historical name, kept on purpose
#: even though the engine is Aho-Corasick, not OpenFst.
MedicalFST = MedicalMatcher


def build_matcher(root: Path) -> MedicalMatcher:
    """Construct a matcher for the repository rooted at ``root``."""
    return MedicalMatcher(root)
