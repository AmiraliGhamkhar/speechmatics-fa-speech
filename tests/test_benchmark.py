"""Benchmark dataset integrity, reproducibility and dictionary consistency.

These guard the properties the benchmark's credibility rests on:

* the dataset is internally coherent and non-contaminated (no reference is
  a truncation of the spoken form, no clinical content is silently dropped);
* the pipeline is deterministic - the same input yields the same output on
  every run, so two result files are comparable;
* the dictionary's metadata matches reality, and the exported Speechmatics
  vocabulary matches the dictionary.
"""

import json
import re
from pathlib import Path

import pytest

from benchmark.dataset import (
    ALL_CASES,
    DATASET_VERSION,
    STAGES,
    dataset_summary,
)
from benchmark.run_benchmark import _term_metrics, evaluate_cases, run_pipeline
from scripts.swiftmedics_tools import audit_dictionary
from speechmatics_test.matcher import MedicalMatcher
from speechmatics_test.presentation import BIDI_CONTROLS

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "medical_knowledge"


@pytest.fixture(scope="module")
def matcher():
    return MedicalMatcher(ROOT)


# ------------------------------------------------------- dataset hygiene

def test_dataset_has_a_version():
    assert DATASET_VERSION
    assert dataset_summary()["case_count"] == len(ALL_CASES)


def test_case_ids_are_unique():
    ids = [c.id for c in ALL_CASES]
    assert len(ids) == len(set(ids))


def test_every_case_declares_a_known_stage():
    for case in ALL_CASES:
        assert case.stage in STAGES, case.id


def test_every_required_group_is_covered():
    """The specification enumerates the terminology groups that must be
    represented; a missing group would make the report look better than
    the system actually is."""
    groups = {c.group for c in ALL_CASES}
    required = {
        "abbreviations", "vital_signs", "laboratory", "routes", "devices",
        "procedures", "assessments", "fall_risk", "pressure_injury",
        "iv_terminology", "cardiovascular", "orthopedic", "skin_assessment",
        "persianized_english", "time", "units", "numbers_integer",
        "numbers_decimal", "numbers_ratio", "numbers_percent",
        "boundary_safety", "bidi",
    }
    assert required <= groups, sorted(required - groups)


def test_every_required_paragraph_scenario_is_covered():
    groups = {c.group for c in ALL_CASES if c.stage == "paragraph"}
    required = {
        "admission_assessment", "vital_signs", "allergy_history", "iv_line",
        "fall_prevention", "braden_assessment", "morse_assessment",
        "education", "physician_notification", "consultation",
        "routine_labs", "skin_assessment", "cardiovascular", "orthopedic",
        "mixed_language",
    }
    assert required <= groups, sorted(required - groups)


# ------------------------------------------------- contamination guards

#: Cases whose reference is legitimately much shorter in TOKENS because a
#: multi-word spoken phrase collapses into one canonical token. Listed
#: explicitly (with the collapse that justifies it) so the anti-truncation
#: guard stays meaningful instead of being globally loosened.
_LEGITIMATE_COMPRESSIONS = {
    "time_half": "ده و نیم -> 10:30",
    "time_minutes_word": "ده و سی دقیقه -> 10:30",
    "time_digits_word": "10 و 30 دقیقه -> 10:30",
    "time_persian_digits": "۱۰ و ۳۰ دقیقه -> 10:30",
    "time_quarter": "ده و ربع -> 10:15",
    "time_with_lead": "ساعت ده و سی دقیقه -> ساعت 10:30",
    "time_bare_pair": "ساعت ده سی -> ساعت 10:30",
    "time_morning": "ده و نیم صبح -> 10:30 صبح",
    "time_night": "ده و نیم شب -> 10:30 شب",
    "unit_percent_fa": "اشباع اکسیژن 97 درصد -> SpO2: 97%",
    "unit_mmhg_fa": "میلی متر جیوه -> mmHg",
    "unit_celsius_fa": "درجه سانتی گراد -> °C",
    "unit_mg_fa": "میلی گرم -> mg",
    "unit_ml_fa": "میلی لیتر -> mL",
    "unit_cm_fa": "سانتی متر -> cm",
    "gram_vital_spo2": "oxygen saturation و 97% -> SpO2: 97%",
    "gram_vital_period": "Temp . 36.7. °C -> Temp: 36.7 °C",
    "gram_repeated_word": "نمره نمره درد -> pain score (stutter + phrase)",
    "lab_ua": "آزمایش ادرار -> U/A",
    "iv_infiltration": "نشت دارویی -> infiltration",
    "num_dose_ml": "میلی لیتر -> mL",
    "time_unbound_number": "10 و 30 دقیقه 90 -> 10:30 90 (the 90 is kept)",
    "num_ratio_spoken_fa": "فشار خون صد و چهل روی هشتاد و پنج -> BP: 140/85",
    "num_ratio_spoken_en": "blood pressure 140 over 85 mmHg -> BP: 140/85 mmHg",
}


def test_reference_is_not_a_truncation_of_the_spoken_form():
    """Anti-contamination: an expected transcript that is dramatically
    shorter than the spoken text usually means clinical content was
    quietly dropped to make the score look good.

    Token count alone is not the right measure - a legitimate
    canonicalization can collapse several spoken words into one token
    (``ده و نیم`` -> ``10:30``). Those cases are enumerated above with the
    collapse that explains them; everything else must keep at least half
    its tokens.
    """
    for case in ALL_CASES:
        if case.id in _LEGITIMATE_COMPRESSIONS:
            continue
        spoken_words = len(case.spoken.split())
        expected_words = len(case.expected.split())
        assert expected_words >= spoken_words * 0.5, (
            f"{case.id}: expected form lost too much content "
            f"({spoken_words} -> {expected_words} tokens)"
        )


def test_declared_compressions_are_real():
    """Every entry in the allow-list must actually be a compressing case,
    so the list cannot silently accumulate excuses for broken fixtures."""
    by_id = {c.id: c for c in ALL_CASES}
    for case_id in _LEGITIMATE_COMPRESSIONS:
        case = by_id.get(case_id)
        assert case is not None, f"{case_id} is no longer in the dataset"
        assert len(case.expected.split()) < len(case.spoken.split()), case_id


def test_no_number_is_dropped_between_spoken_and_expected():
    """Every numeral spoken must appear in the reference (possibly
    reformatted, e.g. 10 + 30 -> 10:30)."""
    for case in ALL_CASES:
        spoken_digits = re.findall(r"\d", case.spoken)
        expected_digits = re.findall(r"\d", case.expected)
        if spoken_digits:
            assert expected_digits, f"{case.id}: all numerals vanished"


def test_declared_expectations_appear_in_the_reference():
    """A case cannot expect a term/number that its own reference lacks."""
    for case in ALL_CASES:
        for term in case.expected_terms:
            assert term in case.expected, f"{case.id}: {term!r} not in reference"
        for number in case.expected_numbers:
            assert number in case.expected, \
                f"{case.id}: {number!r} not in reference"


def test_references_carry_no_bidi_controls():
    for case in ALL_CASES:
        assert not any(ch in BIDI_CONTROLS for ch in case.expected), case.id
        assert not any(ch in BIDI_CONTROLS for ch in case.spoken), case.id


def test_terminology_metrics_count_expected_extra_missing_and_duplicates():
    from benchmark.dataset import Case
    case = Case("metric", "", "", "medical_canonicalization", "metrics",
                ("HTN", "HTN"))
    metrics = _term_metrics(
        case, "HTN COPD", ["HTN", "COPD"], ["HTN", "HTN"]
    )
    assert (metrics["tp"], metrics["fp"], metrics["fn"]) == (1, 1, 1)
    exact = _term_metrics(case, "HTN HTN", ["HTN", "HTN"], ["HTN", "HTN"])
    assert (exact["tp"], exact["fp"], exact["fn"]) == (2, 0, 0)


# --------------------------------------------------------- reproducibility

def test_pipeline_is_deterministic(matcher):
    """Same input, same output - twice, in the same process."""
    for case in ALL_CASES:
        first, hits_a, _ = run_pipeline(matcher, case.spoken)
        second, hits_b, _ = run_pipeline(matcher, case.spoken)
        assert first == second, case.id
        assert [h["canonical"] for h in hits_a] == \
            [h["canonical"] for h in hits_b], case.id


def test_pipeline_is_deterministic_across_matcher_instances():
    """A freshly built matcher produces identical output (no build-order
    nondeterminism in rule compilation)."""
    a, b = MedicalMatcher(ROOT), MedicalMatcher(ROOT)
    for case in ALL_CASES:
        assert run_pipeline(a, case.spoken)[0] == \
            run_pipeline(b, case.spoken)[0], case.id


def test_benchmark_evaluation_is_reproducible(matcher):
    first = evaluate_cases(matcher)["aggregates"]
    second = evaluate_cases(matcher)["aggregates"]
    assert first == second


def test_pipeline_output_never_contains_bidi_controls(matcher):
    for case in ALL_CASES:
        produced, _, _ = run_pipeline(matcher, case.spoken)
        assert not any(ch in BIDI_CONTROLS for ch in produced), case.id


def test_pipeline_never_fabricates_a_number(matcher):
    """Digit multiset out must be explainable by the digits going in.

    Clock normalization may reformat (10 + 30 -> 10:30), so this checks
    that no digit appears in the output that was absent from the input.
    """
    for case in ALL_CASES:
        produced, _, _ = run_pipeline(matcher, case.spoken)
        produced_digits = set(re.findall(r"\d", produced))
        expected_digits = set(re.findall(r"\d", case.expected))
        assert produced_digits <= expected_digits, case.id


def test_warning_cases_actually_warn(matcher):
    """A fixture that declares an ambiguity must make the system SAY so."""
    for case in ALL_CASES:
        _, _, report = run_pipeline(matcher, case.spoken)
        assert bool(report.warnings) == case.expect_warning, (
            f"{case.id}: warnings={report.warnings}"
        )


# ------------------------------------------------- dictionary consistency

def test_dictionary_metadata_matches_actual_term_count():
    """Regression: metadata claimed 990 entries while 979 shipped."""
    report = audit_dictionary()
    assert report["metadata_matches_actual"], (
        f"metadata says {report['metadata_entry_count']}, "
        f"actual {report['actual_terms']}"
    )


def test_dictionary_has_no_duplicate_ids_or_canonicals():
    """Regression: 'PO' was declared twice (ids 'po' and 'route_0049'),
    which made the dictionary fail to load AT ALL."""
    report = audit_dictionary()
    assert report["duplicate_ids"] == []
    assert report["duplicate_canonicals"] == []


def test_dictionary_has_no_structurally_invalid_entries():
    assert audit_dictionary()["invalid_entries"] == []


def test_dictionary_loads_without_raising():
    """The whole application depends on this; it was broken at baseline."""
    assert MedicalMatcher(ROOT).terms


def test_vocabulary_artifact_matches_the_dictionary():
    report = audit_dictionary()
    assert report["vocabulary_matches_speechmatics_flag"], (
        f"{report['vocabulary_entries']} artifact entries vs "
        f"{report['speechmatics_entries']} speechmatics:true terms"
    )


def test_dictionary_contains_no_numeric_only_terms():
    """Numbers must not be modelled as medical dictionary terms.

    The hour_1..hour_12 entries mapped the bare digits '1'..'12' to
    themselves purely for time handling; time is now a deterministic
    normalization stage.
    """
    data = json.loads(
        (KNOWLEDGE / "medical_dictionary.json").read_text(encoding="utf-8"))
    for term in data["terms"]:
        assert not re.fullmatch(r"[\d\s.:]+", term["canonical"]), term["id"]
        for form in term["forms"]:
            assert not re.fullmatch(r"\s*\d+\s*", form), (term["id"], form)


def test_required_nursing_terminology_is_present():
    """The terminology groups the specification names must exist."""
    data = json.loads(
        (KNOWLEDGE / "medical_dictionary.json").read_text(encoding="utf-8"))
    canonicals = {t["canonical"] for t in data["terms"]}
    required = {
        "nursing assessment", "initial nursing assessment", "skin turgor",
        "hydration status", "neurovascular assessment",
        "Nurse call", "bedside rails", "fall precautions",
        "Morse Fall Scale", "Braden Scale", "pressure injury",
        "IV line", "phlebitis", "redness", "infiltration", "infusion",
        "BP", "PR", "HR", "RR", "SpO2", "Temp",
        "CBC", "PT", "PTT", "aPTT", "BG", "FBS", "INR",
        "chest pain", "shortness of breath", "pain score", "vital signs",
        "heart rate", "oxygen saturation", "blood pressure",
        "mmHg", "%", "°C", "mg", "g", "mcg", "mL", "L", "bpm", "cm", "mm",
        "Fr", "Ecchymosis", "ECG", "IV", "HbA1c",
    }
    assert required <= canonicals, sorted(required - canonicals)


def test_ordinary_words_are_not_medical_aliases():
    """Regression: ordinary Persian/English words were mapped to English
    clinical terms, which rewrote plain prose ('بخش قلب' -> 'بخش heart',
    'Please do it now' -> 'Please do intrathecal now')."""
    data = json.loads(
        (KNOWLEDGE / "medical_dictionary.json").read_text(encoding="utf-8"))
    forbidden = {
        "قلب", "پزشک", "دکتر", "علائم", "صبح", "شب", "نیم", "پوستی",
        "کشش", "هوشیار", "سرم", "بخش", "پذیرش", "کنار تخت", "فیکس",
        "شکستگی", "خطر سقوط",
    }
    offenders = [
        (t["id"], form) for t in data["terms"]
        for form in t["forms"] if form in forbidden
    ]
    assert offenders == [], offenders


# ------------------------------------------- vocabulary quality (ASR bias)

def test_vocabulary_keeps_spoken_forms_as_pronunciation_hints():
    """Regression: automating the export silently weakened recognition.

    The artifact that shipped by hand listed a term's spoken forms in
    ``sounds_like`` (``BP`` -> ``فشار خون``, ``بی پی``, ...). An export that
    copied only the declared ``sounds_like`` field dropped them, throwing
    away the strongest ASR-biasing signal the dictionary holds - without
    failing a single test, because nothing asserted on it.
    """
    vocab = json.loads(
        (KNOWLEDGE / "speechmatics_additional_vocab.json")
        .read_text(encoding="utf-8"))
    by_content = {
        e["content"]: e["sounds_like"] for e in vocab if isinstance(e, dict)
    }
    assert "فشار خون" in by_content["BP"]
    assert "بی پی" in by_content["BP"]
    assert "ضربان قلب" in by_content["HR"]
    assert "داخل وریدی" in by_content["IV"]
    assert "میلی لیتر" in by_content["mL"]


def test_vocabulary_hints_are_pronounceable():
    """``sounds_like`` describes speech, so written-only variants carrying
    punctuation or digits (``B/P``, ``B.P.``) must not be sent."""
    vocab = json.loads(
        (KNOWLEDGE / "speechmatics_additional_vocab.json")
        .read_text(encoding="utf-8"))
    for entry in vocab:
        if not isinstance(entry, dict):
            continue
        for hint in entry["sounds_like"]:
            assert hint.strip(), entry["content"]
            assert not re.search(r"[\d/.,:;()\[\]_+*%°-]", hint), (
                f"{entry['content']}: {hint!r} is not pronounceable"
            )
            assert hint != entry["content"], (
                f"{entry['content']}: canonical repeated as its own hint"
            )


def test_vocabulary_has_no_duplicate_contents():
    vocab = json.loads(
        (KNOWLEDGE / "speechmatics_additional_vocab.json")
        .read_text(encoding="utf-8"))
    contents = [e["content"] if isinstance(e, dict) else e for e in vocab]
    assert len(contents) == len(set(contents))


def test_vocabulary_has_no_stale_entries():
    """Regression: the committed artifact still advertised ``hour`` and
    ``o'clock`` after those numeric/time terms left the dictionary, and was
    missing ``Magnesium`` and ``g`` which had been added to it."""
    data = json.loads(
        (KNOWLEDGE / "medical_dictionary.json").read_text(encoding="utf-8"))
    flagged = {t["canonical"] for t in data["terms"] if t.get("speechmatics")}
    vocab = json.loads(
        (KNOWLEDGE / "speechmatics_additional_vocab.json")
        .read_text(encoding="utf-8"))
    contents = {e["content"] if isinstance(e, dict) else e for e in vocab}
    assert contents == flagged, {
        "stale": sorted(contents - flagged),
        "missing": sorted(flagged - contents),
    }


# ------------------------------- dictionary ambiguity audit (this pass)

def test_dictionary_has_no_case_only_duplicate_canonicals():
    """Two spellings of one concept let tier order decide the output.

    Regression: 'chest X-ray'/'chest x-ray', 'Intensive Care Unit'/
    'intensive care unit' and 'Magnesium'/'magnesium' all shipped as
    separate terms, so the same Persian form resolved to a different
    canonical depending on which term won arbitration.
    """
    report = audit_dictionary()
    assert report["case_only_duplicate_canonicals"] == [], (
        "case-only duplicates must be merged in scripts/_dictionary_fixes.py "
        "or declared in DISTINCT_CASE_CANONICALS with a reason")


def test_intentional_case_distinctions_are_real_and_documented():
    """The audit exemption must not become a dumping ground.

    'Mg' (magnesium) vs 'mg' (milligram) is a genuine distinction; every
    exempted pair must still exist, or the exemption is stale.
    """
    from scripts._dictionary_fixes import DISTINCT_CASE_CANONICALS

    data = json.loads(
        (KNOWLEDGE / "medical_dictionary.json").read_text(encoding="utf-8"))
    canonicals = {t["canonical"] for t in data["terms"]}
    assert audit_dictionary()["intentional_case_distinctions"] == sorted(
        DISTINCT_CASE_CANONICALS)
    for folded, (spellings, reason) in DISTINCT_CASE_CANONICALS.items():
        assert reason.strip(), f"{folded}: exemption needs a reason"
        for spelling in spellings:
            assert spelling in canonicals, (
                f"stale exemption: {spelling!r} is no longer in the dictionary")


def test_clinically_unsafe_abbreviation_collisions_are_resolved():
    """A short form must not resolve to an unrelated clinical concept.

    Each of these was claimed by two terms at once, so the matcher silently
    picked one: STEMI/NSTEMI lost their ST-elevation qualifier to the
    generic 'myocardial infarction', 'gtt' (drops) resolved to a glucose
    tolerance test, 'CC' (cubic centimetre) to 'chief complaint' and 'RR'
    (respiratory rate) to 'recovery room'.
    """
    matcher = MedicalMatcher(ROOT)
    by_form = {r.folded: r.canonical for r in matcher.rules}
    assert by_form.get("stemi") == "ST-elevation myocardial infarction"
    assert by_form.get("nstemi") == "non-ST-elevation myocardial infarction"
    assert by_form.get("gtt") == "drop"
    assert by_form.get("cc") == "cubic centimeter"
    assert by_form.get("rr") in (None, "respiratory rate")
    assert by_form.get("sr") in (None, "erythrocyte sedimentation rate")


def test_cross_concept_alias_conflicts_do_not_grow():
    """Ratchet: benign abbreviation/expansion pairs are fine, new
    cross-concept collisions are not."""
    report = audit_dictionary()
    assert (report["same_concept_alias_forms"]
            + report["cross_concept_alias_forms"]
            == report["conflicting_alias_forms"])
    assert report["cross_concept_alias_forms"] <= 60, (
        report["cross_concept_alias_examples"])


# ------------------------------------------------------ dependency pins

def test_speechmatics_sdk_is_pinned():
    """An unpinned realtime SDK breaks only against the live service.

    The client is built against a specific API surface, so the version has
    to be reproducible from requirements.txt alone.
    """
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^speechmatics-rt==\d+\.\d+\.\d+$",
                     requirements, re.MULTILINE), (
        "speechmatics-rt must be pinned to an exact version")


def test_pinned_sdk_matches_the_installed_version():
    """A pin that does not match what the tests ran against is fiction."""
    pytest.importorskip("speechmatics.rt")
    from importlib.metadata import PackageNotFoundError, version

    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    pinned = re.search(r"^speechmatics-rt==(\S+)$",
                       requirements, re.MULTILINE).group(1)
    try:
        installed = version("speechmatics-rt")
    except PackageNotFoundError:  # pragma: no cover - not installed
        pytest.skip("speechmatics-rt is not installed")
    assert installed == pinned, (
        f"requirements pin {pinned} but {installed} is installed")
