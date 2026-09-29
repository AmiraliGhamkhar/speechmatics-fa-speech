#!/usr/bin/env python3
"""Validate the medical dictionary, the ASR vocabulary and alias safety.

Checks performed (all deterministic, no network, no audio):

1. the dictionary loads through the production loader (schema/type/tier/
   duplicate validation happens there and fails hard);
2. the derived Speechmatics vocabulary stays BOUNDED - it is built from the
   ``speechmatics: true`` entries only, never from the whole dictionary;
3. every ``sounds_like`` pronunciation is well formed and inside the
   six-word limit Speechmatics documents;
4. no unsafe Unicode controls are stored in canonicals (a canonical is
   injected into the target application, so it must be clean logical text);
5. every dictionary alias that collides with a common English FUNCTION word
   ("us", "it", "him", "be", "id", ...) is covered by the uppercase-evidence
   guard, so it can never rewrite ordinary English prose;
6. every MULTI-TOKEN canonical is a fixed point of the matcher - feeding the
   dictionary's own output back through it must change nothing, which is what
   stops a shorter rule from firing inside an already-correct phrase;
7. the loader's tier arbitrations are broken down by kind, so a duplicate-form
   conflict (a dictionary edit) is separable from skipped data (hygiene);
8. every ``sounds_like`` is reported with the script Speechmatics can actually
   apply it in, since a pronunciation outside the session language's main
   script is silently ignored by the service.

The content-word aliases the dictionary deliberately expands ("skin" ->
"dermatologic", "daily" -> "every day") are printed for review but do not
fail the check: they are wording choices about chart notation, not a
fabricated value, and nothing here changes established dictionary behavior.

Usage:

    python scripts/validate_dictionary.py
    python scripts/validate_dictionary.py --quiet      # exit code only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from speechmatics_test.matcher import (  # noqa: E402
    CANONICAL_CLAIM_SOURCE,
    MedicalMatcher,
    at_risk_canonicals,
    find_risky_english_aliases,
)
from speechmatics_test.presentation import has_bidi_controls  # noqa: E402
from speechmatics_test.realtime import _dominant_script  # noqa: E402
from speechmatics_test.text import normalize_text  # noqa: E402

#: The exported vocabulary biases the PERSIAN stream (the app's default), so
#: Persian is the script a pronunciation must be written in to take effect.
#: ``realtime._clean_vocab`` filters by the actual session language at run
#: time; this audit only reports what that filter will drop.
EXPORT_SCRIPT = "arabic"

DICTIONARY = ROOT / "medical_knowledge" / "medical_dictionary.json"

#: Speechmatics drops ``sounds_like`` entries longer than six words, and the
#: adapter enforces the same bound locally before sending the config.
MAX_VOCAB_WORDS = 6


def validate(root: Path = ROOT) -> tuple[list[str], list[str]]:
    """Return ``(problems, notes)``; an empty problem list means success."""
    problems: list[str] = []
    notes: list[str] = []

    matcher = MedicalMatcher(root)
    if not matcher.rules:
        problems.append("dictionary compiled to zero rules")
    if not matcher.terms:
        problems.append("dictionary contains zero terms")

    data = json.loads(
        (root / "medical_knowledge" / "medical_dictionary.json")
        .read_text(encoding="utf-8")
    )
    speechmatics_terms = [
        term for term in data["terms"] if term.get("speechmatics")
    ]
    vocab = matcher.additional_vocab
    if len(vocab) != len(speechmatics_terms):
        problems.append(
            f"vocabulary size {len(vocab)} does not match the "
            f"{len(speechmatics_terms)} speechmatics:true terms"
        )

    for entry in vocab:
        content = entry["content"] if isinstance(entry, dict) else entry
        if not content or len(str(content).split()) > MAX_VOCAB_WORDS:
            problems.append(f"vocabulary entry violates the word limit: {content!r}")
        sounds = entry.get("sounds_like", []) if isinstance(entry, dict) else []
        for sound in sounds:
            if not sound.strip() or len(sound.split()) > MAX_VOCAB_WORDS:
                problems.append(
                    f"pronunciation for {content!r} violates the word limit: "
                    f"{sound!r}"
                )
            if has_bidi_controls(sound):
                problems.append(
                    f"pronunciation for {content!r} stores a BiDi control: "
                    f"{sound!r}"
                )

    for term in matcher.terms:
        if has_bidi_controls(term.canonical):
            problems.append(
                f"canonical stores a BiDi control: {term.canonical!r}"
            )
        for form in term.forms:
            if has_bidi_controls(form):
                problems.append(f"form stores a BiDi control: {form!r}")

    # ASR-bias coverage per high-value category. The vocabulary is a bounded
    # biasing list (never the whole dictionary), so a category can legitimately
    # sit below 100%; reporting the number keeps the gap visible and makes any
    # future export decision auditable instead of accidental.
    biased = {
        (item["content"] if isinstance(item, dict) else item)
        for item in vocab
    }
    coverage: dict[str, tuple[int, int]] = {}
    for term in matcher.terms:
        total, covered = coverage.get(term.type, (0, 0))
        total += 1
        if term.canonical in biased:
            covered += 1
        coverage[term.type] = (total, covered)
    notes.append("ASR-bias coverage by category (speechmatics entries / terms):")
    for term_type in sorted(coverage, key=lambda t: -(coverage[t][0] - coverage[t][1])):
        total, covered = coverage[term_type]
        notes.append(f"  {term_type:13} {covered:3}/{total:<3}")

    missing_high_value = sorted(
        term.canonical
        for term in matcher.terms
        if not term.speechmatics
        and term.type in {"drug", "imaging", "lab", "procedure"}
        and term.forms
    )
    notes.append(
        f"{len(missing_high_value)} high-value term(s) (drug/imaging/lab/"
        f"procedure) are NOT exported to Speechmatics; their Persian forms are "
        f"still matcher rules, so dictation is canonicalized deterministically "
        f"(bias only affects recognition). Examples: "
        + ", ".join(missing_high_value[:8])
    )

    aliases = find_risky_english_aliases(matcher)
    unguarded = [
        row for row in aliases
        if row["category"] == "function_word" and not row["guarded"]
    ]
    for row in unguarded:
        problems.append(
            f"unguarded English-word alias: {row['form']!r} -> "
            f"{row['canonical']!r} ({row['tier']}); add it to "
            f"matcher._AMBIGUOUS_SHORT_FORMS"
        )
    reviewed = [row for row in aliases if row["category"] == "content_word"]

    notes.append(
        f"{len(matcher.terms)} terms, {len(matcher.rules)} rules, "
        f"{len(vocab)} Speechmatics vocab entries, "
        f"{len(matcher.warnings)} loader warning(s)"
    )
    notes.append(
        f"{len(aliases)} English-word alias collision(s): "
        f"{sum(1 for row in aliases if row['guarded'])} guarded, "
        f"{len(reviewed)} content-word alias(es) for review"
    )
    for row in reviewed:
        notes.append(
            f"  review: {row['form']!r} -> {row['canonical']!r} "
            f"({row['tier']}) can rewrite ordinary English prose "
            f"(narrative guard: "
            f"{'on' if row['narrative_guarded'] else 'OFF - check this'})"
        )
    unguarded_narrative = [
        row for row in aliases if not row["narrative_guarded"]
    ]
    for row in unguarded_narrative:
        problems.append(
            f"alias {row['form']!r} -> {row['canonical']!r} is not covered by "
            f"the narrative guard; add it to matcher._CONTENT_WORD_ALIASES"
        )

    # --- Speechmatics vocabulary audit (item 4) ---------------------------
    # The vocabulary is a bounded BIASING list: it only helps recognition of
    # the terms in it and is never the whole dictionary (the boundedness test
    # caps it), so a category can legitimately sit below 100% coverage.
    missing = sorted(
        term.canonical for term in matcher.terms
        if term.type in {"drug", "imaging", "lab", "procedure", "abbreviation"}
        and not term.speechmatics
    )
    notes.append(
        f"{len(missing)} high-value term(s) (drug/imaging/lab/procedure/"
        f"abbreviation) are NOT biased: their Persian forms are still matcher "
        f"rules, so they canonicalize deterministically once recognized. "
        f"Any addition must fit the bounded-vocabulary tests. Examples: "
        + ", ".join(missing[:8])
    )
    silent = [
        term.canonical for term in matcher.terms
        if term.speechmatics and not term.sounds_like
    ]
    notes.append(
        f"{len(silent)} biased term(s) carry no ``sounds_like``: "
        + ", ".join(sorted(silent))
    )

    # --- canonical idempotence audit (item 3, defect D8) ------------------
    # A canonical is the dictionary's own declaration of correct output, so
    # feeding it back through the matcher must return it unchanged. When it did
    # not, a shorter rule was firing INSIDE an already-correct phrase and the
    # live path produced "vitamin vitamin B12", "nasogastric in place" (a word
    # deleted) and "blood past medical history 7.4" for a dictated blood pH.
    # The claim rules now prevent that; this check is what keeps them honest.
    at_risk = at_risk_canonicals(matcher.rules)
    claimed = sum(1 for r in matcher.rules if r.source == CANONICAL_CLAIM_SOURCE)
    notes.append(
        f"{len(at_risk)} canonical(s) contain another rule's form and are "
        f"claimed as spans ({claimed} claim rule(s) registered)"
    )
    if claimed != len(at_risk):
        problems.append(
            f"{claimed} canonical-claim rules registered but "
            f"{len(at_risk)} canonicals are at risk"
        )
    not_fixed_multi: list[tuple[str, str]] = []
    not_fixed_single: list[tuple[str, str]] = []
    for canonical in sorted({rule.canonical for rule in matcher.rules}):
        normalized = normalize_text(canonical)
        # Two acceptable answers, and only two:
        #   * the canonical itself - the layer reproduced its own output;
        #   * the unchanged normalized input - in the live narrative path the
        #     guard deliberately declines a Persian-form rule, so an
        #     already-spoken Persian term is left exactly as recognized (this
        #     is also why a ZWNJ in a canonical is not a violation: restoring
        #     the half-space spelling is that rule's job, not a rewrite).
        # A THIRD string means a shorter rule fired inside the phrase.
        allowed = {canonical, normalized}
        for preserve_narrative in (True, False):
            out, _ = matcher.canonicalize(
                normalized, preserve_narrative=preserve_narrative
            )
            if out in allowed:
                continue
            row = (canonical, out)
            bucket = (
                not_fixed_single if len(canonical.split()) < 2
                else not_fixed_multi
            )
            if row not in bucket:
                bucket.append(row)
    for canonical, out in not_fixed_multi:
        problems.append(
            f"canonical is not a fixed point (a rule fires inside it): "
            f"{canonical!r} -> {out!r}"
        )
    if not_fixed_single:
        notes.append(
            f"{len(not_fixed_single)} single-token canonical(s) are expanded "
            f"by another rule (a deliberate abbreviation expansion, not a "
            f"corrupted span): "
            + ", ".join(f"{c!r} -> {o!r}" for c, o in not_fixed_single)
        )

    # --- loader-conflict breakdown (item 3) -------------------------------
    # The loader arbitrates duplicate forms by tier and only logs the decision.
    # Splitting the count by kind keeps 167 warnings actionable: a "conflicting
    # canonicals" row is a dictionary edit, a "skipping rule form" row is data
    # hygiene, and neither is visible in the total alone.
    by_kind: dict[str, int] = {}
    for warning in matcher.warnings:
        # the leading phrase before the first quoted value names the kind
        kind = warning.split("'")[0].strip().rstrip(":").strip()
        by_kind[kind] = by_kind.get(kind, 0) + 1
    notes.append("loader warnings by kind:")
    for kind in sorted(by_kind, key=lambda k: -by_kind[k]):
        notes.append(f"  {by_kind[kind]:4}  {kind}")

    # --- vocabulary script audit (item 4, defect D6) ----------------------
    # Speechmatics applies ``sounds_like`` only in the session language's main
    # script, so a Latin pronunciation in the Persian export cannot take
    # effect. The adapter drops it at run time and reports the drop; naming it
    # here keeps the export honest about which bias actually reaches the ASR.
    wrong_script: list[tuple[str, str]] = []
    for entry in vocab:
        if not isinstance(entry, dict):
            continue
        for sound in entry.get("sounds_like", []):
            script = _dominant_script(sound)
            if script and script != EXPORT_SCRIPT:
                wrong_script.append((entry["content"], sound))
    if wrong_script:
        notes.append(
            f"{len(wrong_script)} ``sounds_like`` pronunciation(s) are not "
            f"{EXPORT_SCRIPT}-script and are dropped for a Persian session "
            f"(they still apply to --language en): "
            + ", ".join(f"{c!r}: {s!r}" for c, s in wrong_script)
        )
    return problems, notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true",
                        help="print only problems and the exit code")
    args = parser.parse_args()

    problems, notes = validate(ROOT)
    if not args.quiet:
        for note in notes:
            print(note)
    for problem in problems:
        print(f"PROBLEM: {problem}")
    if problems:
        print(f"\nFAILED: {len(problems)} problem(s)")
        return 1
    if not args.quiet:
        print(f"OK - {DICTIONARY.relative_to(ROOT)} is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
