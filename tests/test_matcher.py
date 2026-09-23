"""Medical Aho-Corasick matcher tests.

Covers: requirement examples, longest match / overlapping phrases,
Persian-English mixed terminology, abbreviations, numbers and units,
token-aware matching, hit reporting, determinism, idempotence, ZWNJ
variants, tier-based conflict resolution, the automaton/reference-scanner
agreement, native/pure-python engine parity, and the ``MedicalFST`` alias.
"""

import json
import re
from pathlib import Path

import pytest

from speechmatics_test.matcher import (
    AhoAutomaton,
    FstError,
    MedicalFST,
    MedicalMatcher,
    NUMERIC_CONTEXT,
    TIER_ORDER,
)
from speechmatics_test.text import (
    SPOKEN_NUMERAL_JOINER,
    SPOKEN_NUMERALS,
    fold_clock_times,
    fold_numeric_expressions,
    fold_spoken_numbers,
    fold_spoken_ratio,
    normalize_text,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def matcher() -> MedicalMatcher:
    return MedicalMatcher(ROOT)


# Back-compat: the public name must stay constructible and identical.
@pytest.fixture(scope="module")
def fst(matcher) -> MedicalMatcher:
    return matcher


def canon(matcher, text):
    out, _ = matcher.canonicalize(normalize_text(text))
    return out


# ------------------------------------------------------------- dictionary io

def write_dictionary(root: Path, terms: list[dict]) -> Path:
    """Write a synthetic medical_dictionary.json (new consolidated schema)."""
    knowledge = root / "medical_knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    path = knowledge / "medical_dictionary.json"
    path.write_text(
        json.dumps({"version": 1, "terms": terms}, ensure_ascii=False),
        encoding="utf-8",
    )
    return root


def term(term_id, canonical, tier, forms, type="term", **extra):
    return {"id": term_id, "canonical": canonical, "type": type,
            "tier": tier, "forms": forms, **extra}


# ------------------------------------------------------------ requirement map

def test_ccu(fst):
    assert canon(fst, "بیمار در سی سی یو است") == "بیمار در CCU است"


def test_icu(fst):
    assert canon(fst, "بیمار به آی سی یو منتقل شد") == "بیمار به ICU منتقل شد"


def test_ct_scan(fst):
    assert canon(fst, "سی تی اسکن انجام شد") == "CT scan انجام شد"


def test_iv(fst):
    assert canon(fst, "آی وی داده شود") == "IV داده شود"


def test_mg(fst):
    assert canon(fst, "میلی گرم") == "mg"


def test_right_lung(fst):
    assert canon(fst, "رایت لانگ") == "right lung"


def test_lesion(fst):
    assert canon(fst, "لیژن") == "lesion"


def test_mixed_sentence(fst):
    text = "در سی تی اسکن یک لیژن در رایت لانگ مشاهده شد"
    assert canon(fst, text) == "در CT scan یک lesion در right lung مشاهده شد"


# ------------------------------------------------- longest match / overlapping

def test_longest_match_hypertension_phrase(fst):
    # "فشار خون بالا" (longer) must win over "فشار خون"
    assert canon(fst, "فشار خون بالا دارد") == "HTN دارد"
    assert canon(fst, "فشار خون را اندازه بگیرید") == "BP را اندازه بگیرید"


def test_longest_match_ct(fst):
    assert canon(fst, "سی تی") == "CT"
    assert canon(fst, "سی تی اسکن") == "CT scan"


def test_longest_match_unit(fst):
    # "میلی لیتر" must win over the embedded shorter "لیتر"
    assert canon(fst, "میلی لیتر") == "mL"
    assert canon(fst, "لیتر") == "L"


def test_overlapping_substring_forms(fst):
    # "داخل وریدی" is itself a full-phrase rule (longest match -> IV)
    assert canon(fst, "داخل وریدی") == "IV"
    assert canon(fst, "وریدی") == "IV"


def test_overlapping_forms_longest_wins(tmp_path):
    write_dictionary(tmp_path, [
        term("iv", "IV", "abbreviation", ["آی وی"], type="abbreviation"),
        term("ivf", "IVF", "phrase", ["آی وی اف"], type="phrase"),
    ])
    layer = MedicalMatcher(tmp_path)
    assert canon(layer, "آی وی اف") == "IVF"     # longer form wins
    assert canon(layer, "آی وی") == "IV"          # shorter form alone
    assert canon(layer, "آی وی اف و آی وی") == "IVF و IV"


def test_prefix_pair_does_not_corrupt(fst):
    assert canon(fst, "فشار خون") == "BP"
    assert canon(fst, "فشار خون بالا") == "HTN"


# ---------------------------------------------------------- token awareness

def test_no_substring_match_inside_word(fst):
    assert canon(fst, "vivid color") == "vivid color"  # no "iv" replacement
    assert canon(fst, "ivory tower") == "ivory tower"
    assert canon(fst, "سی") == "سی"
    assert canon(fst, "سیسییو") == "سیسییو"  # needs spaces: not the phrase
    assert canon(fst, "میلی") == "میلی"


def test_token_boundaries_respected(fst):
    assert canon(fst, "iv drip") == "IV drip"
    assert canon(fst, "دوز 20 میلی گرم آی وی") == "دوز 20 mg IV"
    assert canon(fst, "BP,120/80") == "BP,120/80"


def test_no_match_in_concatenated_words(fst):
    # Without token boundaries (no spaces), forms must NOT match,
    # even though their codepoints appear mid-token.
    assert canon(fst, "دیابتدرآیسییو") == "دیابتدرآیسییو"
    assert canon(fst, "mriبیمار") == "mriبیمار"
    assert canon(fst, "ivبی") == "ivبی"
    assert canon(fst, "CCUومیلی") == "CCUومیلی"


# ------------------------------------------------------------------ language

def test_persian_english_mixed(fst):
    text = "بیمار HTN دارد و iv line برقرار است"
    out = canon(fst, text)
    assert "HTN" in out and "IV line" in out


def test_persianized_english_pronunciations(fst):
    assert canon(fst, "هایپرتنشن") == "hypertension"
    assert canon(fst, "ام آر آی") == "MRI"
    assert canon(fst, "الترسوند") == "ultrasound"
    assert canon(fst, "اولتراسوند") == "ultrasound"


# ---------------------------------------------------------------- abbreviations

def test_abbreviations(fst):
    assert canon(fst, "بی پی") == "BP"
    assert canon(fst, "ای سی جی") == "ECG"
    assert canon(fst, "ان پی او") == "NPO"
    assert canon(fst, "سی پی آر") == "CPR"
    assert canon(fst, "اچ تی ان") == "HTN"
    assert canon(fst, "دی ام") == "DM"


def test_english_lowercase_variants(fst):
    assert canon(fst, "mri head") == "MRI head"
    assert canon(fst, "ct abdomen") == "CT abdomen"
    assert canon(fst, "ct scan") == "CT scan"
    assert canon(fst, "ct") == "CT"


# ------------------------------------------------ ambiguous short-form safety

def test_ambiguous_short_forms_require_uppercase_evidence(fst):
    """OR/P/NOW/DIFF/AC/PC/HS/OD collide with common English words - only a
    fully uppercase occurrence (the conventional charting style) is treated
    as clinical shorthand; lowercase/mixed case is left as ordinary English.
    """
    # ordinary sentences must NOT be rewritten
    assert canon(fst, "The patient is stable now") == "The patient is stable now"
    assert canon(fst, "he works in or elsewhere") == "he works in or elsewhere"
    assert canon(fst, "this or that") == "this or that"
    assert canon(fst, "the patient has a diff opinion") == \
        "the patient has a diff opinion"
    assert canon(fst, "od the medicine") == "od the medicine"
    assert canon(fst, "pc medication") == "pc medication"
    assert canon(fst, "hs code review") == "hs code review"
    assert canon(fst, "ac before food") != "before meals before meals"

    # fully uppercase clinical shorthand still fires
    assert canon(fst, "The patient is stable NOW") == \
        "The patient is stable immediately"
    assert canon(fst, "DIFF blood test") == \
        "white blood cell differential blood test"
    assert canon(fst, "OD the medicine") == "once a day the medicine"
    assert canon(fst, "PC medication") == "after meals medication"
    assert canon(fst, "HS code review") == "at bedtime code review"


def test_ambiguous_short_forms_persian_and_spelled_aliases_unaffected(fst):
    """Unambiguous aliases for the SAME concepts (Persian script, or fully
    spelled English) are a different match form entirely and are not
    subject to the uppercase-only restriction.
    """
    assert canon(fst, "بیمار او آر رفت") == "بیمار OR رفت"
    assert canon(fst, "before food snack") == "before meals snack"
    assert canon(fst, "give medication before meals") == \
        "give medication before meals"


def test_safe_case_insensitive_abbreviations_remain_case_insensitive(fst):
    """Unambiguous abbreviations (no common-word collision) keep ordinary
    case-insensitive matching - only the specific ambiguous short forms are
    restricted.
    """
    assert canon(fst, "mri head") == "MRI head"
    assert canon(fst, "MRI head") == "MRI head"
    assert canon(fst, "ct scan") == "CT scan"
    assert canon(fst, "CT scan") == "CT scan"
    assert canon(fst, "ecg") == "ECG"
    assert canon(fst, "ECG") == "ECG"


# ------------------------------------------------------------ numbers / units

def test_numbers_and_units(fst):
    assert canon(fst, "دوز 20 میلی گرم") == "دوز 20 mg"
    assert canon(fst, "5.5 میلی لیتر") == "5.5 mL"
    assert canon(fst, "2 کیلوگرم") == "2 kg"
    assert canon(fst, "80 درصد") == "80 %"
    assert canon(fst, "هر دو ساعت") == "q2h"
    assert canon(fst, "سه بار در روز") == "tds"


def test_numbers_left_alone(fst):
    assert canon(fst, "BP 120/80") == "BP 120/80"
    assert canon(fst, "دوز 20 mg") == "دوز 20 mg"  # already canonical


def test_structured_numeric_entities_are_preserved(fst):
    text = normalize_text("BP 120/80 5 mg 2.5 mL 20 mg HbA1c O2 C3-C4 q2h")
    words = [
        {"content": content, "confidence": 0.99, "language": "en",
         "start_time": index, "end_time": index + 0.1}
        for index, content in enumerate(text.split())
    ]
    out, hits = fst.canonicalize(text, words)
    assert out == text
    assert hits == []


# --------------------------------------------------------------- ZWNJ variants

def test_zwnj_input_variants_match_the_same_form(fst):
    """ASR emits نیم‌فاصله and space interchangeably; both must match."""
    assert canon(fst, "بی‌پی") == "BP"
    assert canon(fst, "میلی‌گرم") == "mg"
    assert canon(fst, "فشار خون بالا با بی‌پی") == "HTN با BP"


# ------------------------------------------------ confidence / language evidence

def test_low_confidence_validated_alias_is_auditable_not_automatic(fst):
    text = normalize_text("سی تی اسکن")
    words = [
        {"content": "سی", "confidence": 0.91, "language": "fa", "start_time": 0.0, "end_time": 0.1},
        {"content": "تی", "confidence": 0.52, "language": "fa", "start_time": 0.1, "end_time": 0.2},
        {"content": "اسکن", "confidence": 0.93, "language": "fa", "start_time": 0.2, "end_time": 0.4},
    ]
    out, hits = fst.canonicalize(text, words)
    assert out == "CT scan"  # same explicit lexical rule as the legacy path
    assert hits[0]["asr_confidence"] == 0.52
    assert hits[0]["asr_low_confidence"] is True
    assert hits[0]["asr_language"] == "Persian"
    assert hits[0]["asr_language_matches_form"] is True


def test_confidence_never_invents_a_medical_correction(fst):
    text = normalize_text("نامشخص 120/80")
    words = [
        {"content": "نامشخص", "confidence": 0.10, "language": "fa"},
        {"content": "120/80", "confidence": 0.10, "language": "en"},
    ]
    assert fst.canonicalize(text, words) == (text, [])


def test_high_confidence_canonical_terms_remain_unchanged(fst):
    text = normalize_text("MRI HbA1c O2 C3-C4 q2h 5 mg")
    words = [
        {"content": content, "confidence": 0.99, "language": "en"}
        for content in text.split()
    ]
    assert fst.canonicalize(text, words) == (text, [])


def test_missing_word_metadata_follows_legacy_path(fst):
    text = normalize_text("سی تی اسکن و لیژن")
    assert fst.canonicalize(text) == fst.canonicalize(text, [])


def test_language_mismatch_is_a_conservative_audit_signal(fst):
    text = normalize_text("لیژن")
    words = [{"content": "لیژن", "confidence": 0.40, "language": "en"}]
    out, hits = fst.canonicalize(text, words)
    assert out == "lesion"  # explicit lexical matches still remain deterministic
    assert hits[0]["asr_language_matches_form"] is False


# ------------------------------------------------------------- hits / audit

def test_hits_reported(fst):
    out, hits = fst.canonicalize(normalize_text("سی سی یو و میلی گرم"))
    assert out == "CCU و mg"
    assert {h["canonical"] for h in hits} == {"CCU", "mg"}
    for h in hits:
        assert set(h) == {"form", "canonical", "tier", "source", "position"}
        assert isinstance(h["position"], int)
        assert h["tier"] in TIER_ORDER  # auditable tier NAME, not a magic int


def test_hit_reports_provenance(fst):
    out, hits = fst.canonicalize(normalize_text("میلی گرم"))
    assert hits[0]["tier"] == "unit"
    assert hits[0]["source"] == "fst_terms.json"
    assert hits[0]["position"] == 0


def test_no_hits_when_no_match(fst):
    out, hits = fst.canonicalize("بیمار در اتاق است")
    assert out == "بیمار در اتاق است"
    assert hits == []


# ------------------------------------------------------- determinism / other

def test_deterministic_across_runs(fst):
    text = normalize_text("سی تی اسکن و فشار خون بالا و آی وی 20 میلی گرم")
    first, hits1 = fst.canonicalize(text)
    second, hits2 = fst.canonicalize(text)
    assert first == second
    assert hits1 == hits2


def test_idempotent(fst):
    text = normalize_text("در سی تی اسکن لیژن و فشار خون بالا")
    once, _ = fst.canonicalize(text)
    twice, _ = fst.canonicalize(once)
    assert once == twice


def test_empty_input(fst):
    assert fst.canonicalize("") == ("", [])


def test_matching_does_not_mutate_matcher_state(fst):
    before_rules = list(fst.rules)
    before_vocab = list(fst.additional_vocab)
    before_automaton = fst._ac_native
    fst.canonicalize(normalize_text("سی سی یو و آی وی و فشار خون بالا"))
    assert fst.rules == before_rules
    assert fst.additional_vocab == before_vocab
    assert fst._ac_native is before_automaton


# --------------------------------------------- automaton / reference parity

def test_automaton_matches_reference_scanner(fst):
    """The Aho-Corasick engine must agree exactly with the naive scanner."""
    text = normalize_text(
        "سی سی یو، آی وی 20 میلی گرم، ct scan و iv line و هایپرتنشن"
    )
    ac_out, ac_hits = fst._scan(text)
    ref_out, ref_hits = fst._scan_reference(text)
    assert ac_out == ref_out
    assert ac_hits == ref_hits


def test_automaton_matches_reference_on_clinical_paragraph(fst):
    text = normalize_text(
        "بیمار در سی تی اسکن لیژن رایت لانگ و فشار خون بالا دارد "
        "دوز 5.5 میلی لیتر آی وی هر دو ساعت داده شد"
    )
    words = [
        {"content": content, "confidence": 0.60, "language": "fa"}
        for content in text.split()
    ]
    ac_out, ac_hits = fst._scan(text, words)
    ref_out, ref_hits = fst._scan_reference(text, words)
    assert ac_out == ref_out
    assert ac_hits == ref_hits


def test_native_and_pure_python_engines_have_identical_output(fst):
    text = normalize_text("سی تی اسکن لیژن 5 میلی گرم")
    words = [
        {"content": content, "confidence": 0.60, "language": "fa"}
        for content in text.split()
    ]
    fallback = MedicalMatcher(ROOT)
    fallback._ac_native = None
    fallback._ac_python = AhoAutomaton([rule.match_form for rule in fallback.rules])
    assert fst.canonicalize(text, words) == fallback.canonicalize(text, words)


def test_pure_python_engine_matches_reference_scanner():
    """Full parity sweep: pure-python automaton == naive reference scanner."""
    matcher = MedicalMatcher(ROOT)
    matcher._ac_native = None
    matcher._ac_python = AhoAutomaton([rule.match_form for rule in matcher.rules])
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "pre_migration_canonicalization.json")
        .read_text(encoding="utf-8")
    )
    for case in fixture["cases"]:
        normalized = normalize_text(case["raw"])
        ac_out, ac_hits = matcher._scan(normalized)
        ref_out, ref_hits = matcher._scan_reference(normalized)
        assert ac_out == ref_out
        assert ac_hits == ref_hits, case["raw"]


def test_automaton_persian_digits(fst):
    """Digit folding happens upstream; the automaton handles the result."""
    text = normalize_text("دوز ۲۰ میلی گرم آی وی")
    out, _ = fst.canonicalize(text)
    assert "20" in out and "mg" in out and "IV" in out


def test_engine_failure_degrades_to_reference_scanner(fst, monkeypatch):
    """An engine failure must not cost the user their finished transcript."""

    def boom(_text, _words=None):
        raise RuntimeError("simulated engine failure")

    monkeypatch.setattr(fst, "_scan", boom)
    text = normalize_text("در سی تی اسکن یک لیژن دیده شد")
    out, hits = fst.canonicalize(text)

    assert out == "در CT scan یک lesion دیده شد"
    assert hits
    assert any("aho-corasick engine failed" in w for w in fst.warnings)


def test_engine_is_built_once_and_reported(fst):
    """No per-input automaton rebuilds: compiled a single time at load."""
    assert fst.uses_ahocorasick is True
    assert fst._ac_native is not None
    assert fst.engine.startswith("aho-corasick")
    same_engine = fst._ac_native
    for _ in range(50):
        fst.canonicalize(normalize_text("سی سی یو و آی وی"))
    assert fst._ac_native is same_engine


# ------------------------------------------------------------- raw automaton

def test_pure_python_automaton_finds_all_matches():
    forms = ["آی وی", "آی وی اف", "فشار خون"]
    ac = AhoAutomaton(forms)
    text = "آی وی اف و آی وی و فشار خون"
    found = sorted((end, idx) for end, idx in ac.iter(text))
    # آی وی (chars 0..4, ends 4), آی وی اف (chars 0..7, ends 7),
    # آی وی standalone (ends 15), فشار خون (ends 26)
    assert (4, 0) in found and (7, 1) in found
    assert (15, 0) in found and (26, 2) in found


def test_pure_python_automaton_is_deterministic():
    forms = ["ab", "b", "abc"]
    ac = AhoAutomaton(forms)
    first = list(ac.iter("abcabc"))
    second = list(ac.iter("abcabc"))
    assert first == second


# ------------------------------------------------------------- rule loading

def test_conflicting_forms_resolve_deterministically(tmp_path):
    write_dictionary(tmp_path, [
        term("iv", "IV", "curated", ["آی وی"], type="abbreviation"),
        term("ct-scan", "CT scan", "curated", ["سی تی اسکن"], type="imaging"),
        term("ct", "CT", "curated", ["سی تی"], type="abbreviation"),
    ])
    layer = MedicalMatcher(tmp_path)
    assert layer.warnings == []
    assert canon(layer, "آی وی") == "IV"
    assert canon(layer, "سی تی اسکن") == "CT scan"


def test_conflict_keeps_first_source_and_warns(tmp_path):
    write_dictionary(tmp_path, [
        term("iv", "IV", "curated", ["آی وی"], type="abbreviation"),
        term("intravenous", "intravenous", "phrase", ["آی وی"], type="route"),
    ])
    layer = MedicalMatcher(tmp_path)
    # different canonicals for the same form: reported conflict, curated wins
    assert any("conflicting canonicals" in w for w in layer.warnings)
    out, _ = layer.canonicalize("آی وی")
    assert out == "IV"


def test_duplicate_forms_within_one_term_dedupe_silently(tmp_path):
    write_dictionary(tmp_path, [
        term("iv", "IV", "curated", ["آی وی", "آی وی", "آی‌وی"],
             type="abbreviation"),
    ])
    layer = MedicalMatcher(tmp_path)
    # same form (incl. a ZWNJ variant) listed repeatedly: clean dedupe
    assert layer.warnings == []
    assert len(layer.rules) == 1
    out, _ = layer.canonicalize("آی وی")
    assert out == "IV"


def test_higher_tier_beats_file_order(tmp_path):
    """A lower-ranked term listed FIRST must still lose a tier conflict."""
    write_dictionary(tmp_path, [
        term("route", "intravenous", "phrase", ["آی وی"], type="route"),
        term("iv", "IV", "curated", ["آی وی"], type="abbreviation"),
    ])
    layer = MedicalMatcher(tmp_path)
    assert any("conflicting canonicals" in w for w in layer.warnings)
    out, _ = layer.canonicalize("آی وی")
    assert out == "IV"  # curated beats phrase regardless of file order


def test_same_tier_conflict_resolves_by_stable_order(tmp_path):
    write_dictionary(tmp_path, [
        term("first", "A", "validated_term", ["فرم مشترک"]),
        term("second", "B", "validated_term", ["فرم مشترک"]),
    ])
    layer = MedicalMatcher(tmp_path)
    out, hits = layer.canonicalize("فرم مشترک")
    assert out == "A"  # first term in stable file order wins
    assert hits[0]["canonical"] == "A"


def test_conflicting_canonicals_warn(tmp_path):
    write_dictionary(tmp_path, [
        term("iv", "IV", "curated", ["آی وی"], type="abbreviation"),
        term("intravenous", "intravenous", "observed_alias", ["آی وی"]),
    ])
    layer = MedicalMatcher(tmp_path)
    assert any("conflicting canonicals" in w for w in layer.warnings)
    out, _ = layer.canonicalize("آی وی")
    assert out == "IV"  # curated tier wins


def test_punctuation_forms_are_skipped(tmp_path):
    write_dictionary(tmp_path, [
        term("ccu", "CCU", "curated", ["سی سی یو", "سی سی یو،"],
             type="abbreviation"),
    ])
    layer = MedicalMatcher(tmp_path)
    assert any("punctuation" in w for w in layer.warnings)
    out, _ = layer.canonicalize("سی سی یو،")
    assert out == "CCU،"  # clean form matches, comma preserved


def test_unknown_tier_raises(tmp_path):
    write_dictionary(tmp_path, [
        term("x", "y", "nope", ["فرم"]),
    ])
    with pytest.raises(FstError):
        MedicalMatcher(tmp_path)


def test_uses_ahocorasick_flag(fst):
    try:
        import ahocorasick  # noqa: F401
        assert fst.uses_ahocorasick is True
    except ImportError:
        assert fst.uses_ahocorasick is False


# ------------------------------------------------------------- performance

def test_automaton_scales_with_many_rules(fst):
    """Sanity: a long paragraph with no matches stays fast and unchanged."""
    text = normalize_text(" ".join(f"کلمه{i} برای آزمون سرعت" for i in range(500)))
    out, hits = fst.canonicalize(text)
    assert hits == []
    assert out == text


# --------------------------------------------- token-prefix emission guard

def test_max_rule_tokens_matches_longest_form(fst):
    longest = max(len(rule.form.split()) for rule in fst.rules)
    assert fst.max_rule_tokens == longest
    assert fst.max_rule_tokens >= 3  # e.g. "فشار خون بالا"


def test_is_rule_token_prefix_detects_full_and_partial_forms(fst):
    # an exact rule form is (trivially) a token-prefix of itself
    assert fst.is_rule_token_prefix(["فشار", "خون", "بالا"]) is True
    # a leading fragment of a multi-token form is a prefix
    assert fst.is_rule_token_prefix(["فشار", "خون"]) is True
    # an unrelated token sequence is not
    assert fst.is_rule_token_prefix(["بیمار", "دارد"]) is False
    assert fst.is_rule_token_prefix([]) is False
    # matching ignores case (English forms)
    assert fst.is_rule_token_prefix(["ct"]) is True


def test_is_strict_rule_token_prefix_excludes_standalone_complete_rules(fst):
    # "iv" is a complete one-token rule with no longer sibling rule
    # starting "iv ...": is_rule_token_prefix is trivially True (it matches
    # its own full form) but is_strict_rule_token_prefix must be False,
    # since nothing could ever extend it into a longer match.
    assert fst.is_rule_token_prefix(["iv"]) is True
    assert fst.is_strict_rule_token_prefix(["iv"]) is False

    # a genuine leading fragment of a longer rule remains strict-prefix
    # True in both senses (e.g. "فشار خون" can still extend to
    # "فشار خون بالا").
    assert fst.is_rule_token_prefix(["فشار", "خون"]) is True
    assert fst.is_strict_rule_token_prefix(["فشار", "خون"]) is True

    # the full, longest form itself has nothing longer to extend into.
    assert fst.is_rule_token_prefix(["فشار", "خون", "بالا"]) is True
    assert fst.is_strict_rule_token_prefix(["فشار", "خون", "بالا"]) is False

    assert fst.is_strict_rule_token_prefix([]) is False


# ----------------------------------------------- privacy of engine warning

def test_engine_failure_warning_does_not_leak_transcript(fst, monkeypatch):
    secret = "بیمار محرمانه فشار خون بالا دارد today"
    calls = {"n": 0}

    def boom(text, word_results=None):
        calls["n"] += 1
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(fst, "_scan", boom)
    canonical, hits = fst.canonicalize(normalize_text(secret), [])
    assert calls["n"] == 1
    # the reference scanner still produced the canonical transcript
    assert "HTN" in canonical
    failure_warnings = [w for w in fst.warnings if "engine failed" in w]
    assert failure_warnings, "expected an engine-failure warning"
    for warning in failure_warnings:
        assert "محرمانه" not in warning
        assert "بیمار" not in warning
        assert "chars (sha256:" in warning


# ------------------------------------------------- MedicalFST alias (§0)

def test_medicalfst_alias_is_the_matcher():
    assert MedicalFST is MedicalMatcher


def test_medicalfst_constructs_and_matches_identically(fst):
    legacy_named = MedicalFST(ROOT)
    assert legacy_named.engine == fst.engine
    text = normalize_text("در سی تی اسکن یک لیژن دیده شد")
    assert legacy_named.canonicalize(text) == fst.canonicalize(text)


# ------------------------------------------ pre-migration behavior fixture

def test_consolidated_dictionary_reproduces_legacy_behavior(fst):
    """Every pre-consolidation output must be reproduced exactly."""
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "pre_migration_canonicalization.json")
        .read_text(encoding="utf-8")
    )
    assert len(fixture["cases"]) >= 400
    mismatches = []
    for case in fixture["cases"]:
        normalized = normalize_text(case["raw"])
        out, hits = fst.canonicalize(normalized)
        expected_hits = [
            {"form": h["form"], "canonical": h["canonical"],
             "position": h["position"]}
            for h in hits
        ]
        if out != case["canonical"] or expected_hits != case["hits"]:
            mismatches.append(case["raw"])
    assert mismatches == []


# ==========================================================================
# numeric and clock folding: age, blood pressure, SpO2, time of day, AM/PM
#
# ``canon`` drives the real pipeline (normalize_text -> lexical pass -> fold),
# so every case below is the behaviour of the service and not of a test-local
# copy.  The fold is deliberately anchor-driven: a number is only rewritten
# when the words around it say what kind of number it is.
# ==========================================================================

NUMERIC_CASES = [
    # --- age: "سن <n> سال" / "<n> ساله" ---------------------------------
    ("سن بیست سال", "سن 20 سال"),
    ("بیست ساله", "20 ساله"),
    ("بیمار بیست و پنج ساله", "بیمار 25 ساله"),
    ("بیمار شصت و هفت ساله", "بیمار 67 ساله"),
    ("سن بیمار چهل و دو سال است", "سن بیمار 42 سال است"),
    ("بیمار 25 ساله", "بیمار 25 ساله"),
    ("سن 65 سال", "سن 65 سال"),
    ("بیمار 72 ساله", "بیمار 72 ساله"),
    ("۲۰ ساله", "20 ساله"),
    # --- blood pressure: concept + systolic/diastolic --------------------
    ("فشار خون", "BP"),
    ("فشار خون صد و بیست روی هشتاد", "BP 120/80"),
    ("فشار خون ۱۲۰ روی ۸۰", "BP 120/80"),
    ("فشار 120 روی 80", "فشار 120/80"),
    ("صد و بیست روی هشتاد", "120/80"),
    # a run that does not form one number is left entirely alone, never half
    ("فشار خون صد و بیست و هشتاد", "BP صد و بیست و هشتاد"),
    ("فشار خون 120/80", "BP 120/80"),
    ("فشار خون بالا", "HTN"),
    # --- SpO2: concept and percentage, never a bare "98 %" -------------
    ("اشباع اکسیژن", "SpO2"),
    ("اس پی او دو نود و هشت", "SpO2 98"),
    ("اشباع اکسیژن نود و هشت درصد", "SpO2 98 %"),
    ("اشباع اکسیژن 98 درصد", "SpO2 98 %"),
    ("ساتوریشن نود و هشت", "oxygen saturation 98"),
    ("ساتوریشن 97 درصد", "oxygen saturation 97 %"),
    ("اکسیژن نود و پنج درصد", "O2 95 %"),
    ("SpO2 98%", "SpO2 98%"),
    ("SpO2 98 %", "SpO2 98 %"),
    ("۹۸ درصد", "98 %"),
    # --- time of day ------------------------------------------------------
    ("ساعت هشت", "ساعت 8"),
    ("ساعت ۱۲", "ساعت 12"),
    ("ساعت 08", "ساعت 08"),
    ("ساعت 14", "ساعت 14"),
    ("08:30", "08:30"),
    ("14:30", "14:30"),
    ("23:15", "23:15"),
    ("ساعت هشت و نیم", "ساعت 8:30"),
    ("ساعت هشت وربع", "ساعت 8:15"),
    ("ساعت هشت و چهل و پنج دقیقه", "ساعت 8:45 دقیقه"),
    ("ساعت دو و 15 دقیقه", "ساعت 2:15 دقیقه"),
    ("هشت و نیم صبح", "8:30 صبح"),
    ("ساعت هشت صبح", "ساعت 8 صبح"),
    ("ساعت دو بعد از ظهر", "ساعت 2 بعد از ظهر"),
    ("ساعت سه عصر", "ساعت 3 عصر"),
    ("ساعت هشت شب", "ساعت 8 شب"),
    ("ساعت دوازده ظهر", "ساعت 12 ظهر"),
    # --- AM / PM ---------------------------------------------------------
    ("8 AM", "8 AM"),
    ("8 PM", "8 PM"),
    ("12 AM", "12 AM"),
    ("12 PM", "12 PM"),
    ("8 am", "8 AM"),
    ("۸ صبح", "8 صبح"),
    ("at 8 A.M. give IV", "at 8 AM. give IV"),
    ("ساعت ۸:۳۰ صبح", "ساعت 8:30 صبح"),
    # Explicit spoken abbreviations only; ordinary day-parts stay Persian.
    ("ای ام", "AM"),
    ("پی ام", "PM"),
    ("صبح", "صبح"),
    ("عصر", "عصر"),
    ("شب", "شب"),
    ("ظهر", "ظهر"),
    ("نیمه شب", "نیمه شب"),
]


@pytest.mark.parametrize("raw,expected", NUMERIC_CASES)
def test_requested_numeric_rewrites(fst, raw, expected):
    assert canon(fst, raw) == expected


@pytest.mark.parametrize("raw,_expected", NUMERIC_CASES)
def test_numeric_rewrites_are_idempotent(fst, raw, _expected):
    """Re-canonicalizing the output must not rewrite it a second time."""
    once = canon(fst, raw)
    assert canon(fst, once) == once


SAFE_CASES = [
    # Ordinary English must not be read as clinical notation.
    ("vivid", "vivid"),
    ("ivory", "ivory"),
    ("The patient is stable now", "The patient is stable now"),
    ("8", "8"),
    ("120", "120"),
    ("ordinary 120 numbers", "ordinary 120 numbers"),
    # Persian words that merely look like anchors.
    ("درد روی سینه", "درد روی سینه"),
    ("یک ضایعه در ریه", "یک ضایعه در ریه"),
    ("پنج و شش ساله", "پنج و شش ساله"),
    ("دو هفته قبل", "دو هفته قبل"),
    ("ده میلیارد", "ده میلیارد"),
    # "هزار" is deliberately not in the numeral lexicon: no half-thousands.
    ("هزار میلی لیتر", "هزار mL"),
    # A duration is not a clock reading, and half an hour of it stays spoken.
    ("دو و نیم ساعت", "دو و نیم ساعت"),
    ("ساعت هشت و نیم ساعت", "ساعت 8 و نیم ساعت"),
    ("هر دو ساعت داده شد", "q2h داده شد"),
    # Units keep their own canonical spelling.
    ("دوز 20 mg", "دوز 20 mg"),
    ("دوز ۵ میلی گرم", "دوز 5 mg"),
    ("۵.۵ میلی لیتر", "5.5 mL"),
    ("۶۰ کیلوگرم", "60 kg"),
    # Dates and calendar years are not clock values.
    ("۲۵ مارس ۲۰۲۵", "25 مارس 2025"),
    ("۱۳۹۰", "1390"),
    ("۳ بار در روز", "3 بار در روز"),
]


@pytest.mark.parametrize("raw,expected", SAFE_CASES)
def test_folds_leave_ordinary_text_alone(fst, raw, expected):
    assert canon(fst, raw) == expected


def test_fold_is_anchored_and_never_half_converts_a_number(fst):
    # "ده دقیقه" has no lead and no after-anchor: left as spoken rather than
    # folded into a meaningless bare "10".
    assert canon(fst, "هشت و سی دقیقه") == "هشت و سی دقیقه"
    assert canon(fst, "ساعت هشت و سی دقیقه") == "ساعت 8:30 دقیقه"
    # an invalid hour is not a clock reading
    assert canon(fst, "ساعت 24") == "ساعت 24"
    assert canon(fst, "ساعت 23 و 60 دقیقه") == "ساعت 23 و 60 دقیقه"


def test_folds_apply_after_the_lexical_pass_and_add_no_hits(fst):
    """Folds rewrite canonical text; they must never invent medical hits."""
    text = normalize_text("فشار خون صد و بیست روی هشتاد")
    canonical, hits = fst.canonicalize(text)
    assert canonical == "BP 120/80"
    assert [hit["canonical"] for hit in hits] == ["BP"]


def test_standalone_fold_stage_matches_the_pipeline():
    """The fold is the last stage, so it can be exercised on its own."""
    lexical = "فشار خون صد و بیست روی هشتاد"
    folded = fold_numeric_expressions(lexical, NUMERIC_CONTEXT)
    assert folded == "فشار خون 120/80"
    assert fold_spoken_numbers(folded, NUMERIC_CONTEXT) == folded
    assert fold_clock_times("ساعت هشت و نیم", NUMERIC_CONTEXT) == "ساعت 8:30"
    assert fold_spoken_ratio("120 روی 80", NUMERIC_CONTEXT) == "120/80"
    assert fold_spoken_ratio("درد روی سینه", NUMERIC_CONTEXT) == "درد روی سینه"


def test_minute_word_is_a_tail_marker_not_an_anchor():
    """"دقیقه" must stay out of ``after`` or bare durations get corrupted."""
    assert "دقیقه" not in NUMERIC_CONTEXT.after
    assert "دقیقه" == NUMERIC_CONTEXT.minute_unit
    assert NUMERIC_CONTEXT.clock_lead == "ساعت"


def test_numeric_fold_is_engine_independent():
    """The fold sits outside both scanners, so both produce the same text."""
    matcher = MedicalMatcher(ROOT)
    matcher._ac_native = None
    matcher._ac_python = AhoAutomaton([rule.match_form for rule in matcher.rules])
    for raw, _expected in NUMERIC_CASES + SAFE_CASES:
        text = normalize_text(raw)
        assert matcher.canonicalize(text) == MedicalMatcher(ROOT).canonicalize(text)



_FOLD_PUNCTUATION = ".,:;!?،؛؟«»()[]{}""'"


def _fold_tokens(text: str) -> list[str]:
    """Tokens of ``text`` without the punctuation the fold deliberately leaves."""
    return [token.strip(_FOLD_PUNCTUATION) for token in text.split(" ")]


@pytest.mark.parametrize("raw,_expected", NUMERIC_CASES + SAFE_CASES)
def test_the_fold_only_rewrites_number_spans(fst, raw, _expected):
    """Anti-hallucination invariant for the fold stage.

    Every token the fold removes from the lexical output must be number text -
    a numeral word, the "و" joiner (possibly glued to what follows it), a
    half/quarter word, the ratio connector it consumed, or digits it joined -
    and the fold may introduce no word the lexical pass did not already
    contain, only digits and the colon notation. A fold that silently ate a
    clinical word fails here even if the expected strings above were updated to
    match it.
    """
    removable = (set(SPOKEN_NUMERALS)
                 | {SPOKEN_NUMERAL_JOINER, NUMERIC_CONTEXT.ratio_connector}
                 | NUMERIC_CONTEXT.half_words | NUMERIC_CONTEXT.quarter_words)
    removable |= {SPOKEN_NUMERAL_JOINER + word
                  for word in NUMERIC_CONTEXT.half_words | NUMERIC_CONTEXT.quarter_words}
    lexical, _hits = fst._scan(normalize_text(raw))
    source = _fold_tokens(lexical)
    folded = _fold_tokens(fold_numeric_expressions(lexical, NUMERIC_CONTEXT))
    left = list(source)
    for token in folded:
        if token in left:
            left.remove(token)
    assert [t for t in left if t not in removable and not t.isdigit()] == [], raw
    for token in folded:
        if any(character.isdigit() or character == ":" for character in token):
            continue
        assert token in source, (raw, token)


def test_clinical_paragraph_end_to_end(fst):
    """One realistic mixed-language paragraph: pinned, and stable on a re-run."""
    paragraph = (
        "بیمار مرد شصت و هفت ساله ساعت دو بعد از ظهر مراجعه کرد. فشار "
        "خون صد و پنجاه روی نود، اشباع اکسیژن نود و پنج درصد، سن بیمار "
        "۴۲ سال. دوز پانصد میلی گرم داده شد. ساعت هشت و نیم صبح CXR "
        "گرفته شد."
    )
    expected = (
        "بیمار مرد 67 ساله ساعت 2 بعد از ظهر مراجعه کرد. BP 150/90، "
        "SpO2 95 %، سن بیمار 42 سال. دوز 500 mg داده شد. ساعت 8:30 صبح "
        "CXR گرفته شد."
    )
    assert canon(fst, paragraph) == expected
    assert canon(fst, expected) == expected


ORDINARY_PERSIAN = [
    ("دکتر آمد", "دکتر آمد"),
    ("پزشک آمد", "پزشک آمد"),
    ("پرستار", "پرستار"),
    ("بیمار", "بیمار"),
    ("فشار بیمار پایین است", "فشار بیمار پایین است"),
    ("نبض بیمار منظم است", "نبض بیمار منظم است"),
    ("سرم وصل است", "سرم وصل است"),
    ("قلب بیمار", "قلب بیمار"),
    ("تب دارد", "تب دارد"),
    ("جراح آمد", "جراح آمد"),
    ("اورژانس", "اورژانس"),
    ("ای ام", "AM"),
    ("پی ام", "PM"),
    ("پی آر", "PR"),
    ("ایسیجی", "ECG"),
    ("پی تی تی", "PTT"),
    ("آی ام", "IM"),
]


@pytest.mark.parametrize("raw,expected", ORDINARY_PERSIAN)
def test_ordinary_persian_stays_persian_and_spoken_abbreviations_chart(fst, raw, expected):
    """Role words and day parts stay Persian; spoken abbreviations stay chart form."""
    assert canon(fst, raw) == expected
    assert canon(fst, expected) == expected


def test_number_and_meridiem_rows_are_single_sourced():
    """Dictionary invariants that keep the fold authoritative for numbers.

    A row per spoken hour used to emit digits *and* fight the fold over the
    same span, and a bare "morning" was rewritten to the frequency phrase
    "every morning" by the dictionary; both are prevented here.
    """
    data = json.loads(
        (ROOT / "medical_knowledge" / "medical_dictionary.json").read_text(
            encoding="utf-8"
        )
    )
    by_id = {term["id"]: term for term in data["terms"]}
    assert not [term for term in data["terms"] if re.fullmatch(r"hour_\d+", term["id"])]
    assert by_id["am"]["canonical"] == "AM"
    assert by_id["pm"]["canonical"] == "PM"
    assert "midnight" not in by_id
    mapped: dict[str, set[str]] = {}
    for term in data["terms"]:
        for form in term["forms"]:
            mapped.setdefault(form.strip().casefold(), set()).add(term["canonical"])
    assert "every morning" not in mapped.get("morning", set())
    assert "every evening" not in mapped.get("evening", set())
    assert "noon / midday" not in mapped.get("noon", set())
    # AM/PM is the spoken abbreviation only. Day-part words stay Persian,
    # so a bare "شب" cannot split "نیمه شب" into a meridiem either.
    assert "ای ام" in by_id["am"]["forms"] and "پی ام" in by_id["pm"]["forms"]
    day_parts = {"صبح", "ظهر", "عصر", "شب", "بعد از ظهر", "قبل از ظهر", "نیمه شب"}
    for term in data["terms"]:
        if term["canonical"] in {"AM", "PM"}:
            assert day_parts.isdisjoint(term["forms"])
            assert day_parts.isdisjoint(term.get("sounds_like") or [])
    assert "12 AM" not in mapped.get("نیمه شب", set())
    assert by_id["spo2"]["canonical"] == "SpO2"
