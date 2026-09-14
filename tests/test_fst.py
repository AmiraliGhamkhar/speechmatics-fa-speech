"""Medical FST layer tests.

Covers: requirement examples, longest match / overlapping phrases,
Persian-English mixed terminology, abbreviations, numbers and units,
token-aware matching, hit reporting, determinism, idempotence, and the
pynini/scanner agreement.
"""

import json
from pathlib import Path

import pytest

from speechmatics_test.fst import FstError, MedicalFST
from speechmatics_test.text import normalize_text

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def fst():
    return MedicalFST(ROOT)


def canon(fst, text):
    out, _ = fst.canonicalize(normalize_text(text))
    return out


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
    (tmp_path / "medical_knowledge").mkdir()
    tiers = {
        "abbreviation": 0, "observed_alias": 1, "phrase": 2,
        "validated_term": 3, "unit": 4,
    }
    (tmp_path / "medical_knowledge" / "fst_terms.json").write_text(json.dumps({
        "tiers": tiers,
        "rules": [
            {"form": "آی وی", "canonical": "IV", "tier": "abbreviation"},
            {"form": "آی وی اف", "canonical": "IVF", "tier": "phrase"},
        ],
    }), encoding="utf-8")
    layer = MedicalFST(tmp_path)
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
    # Regression: without token boundaries (no spaces), forms must NOT match,
    # even though their bytes/codepoints appear mid-token.
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


# ------------------------------------------------------------- hits / audit

def test_hits_reported(fst):
    out, hits = fst.canonicalize(normalize_text("سی سی یو و میلی گرم"))
    assert out == "CCU و mg"
    assert {h["canonical"] for h in hits} == {"CCU", "mg"}
    for h in hits:
        assert set(h) == {"form", "canonical", "tier", "source", "position"}
        assert isinstance(h["position"], int)


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


def test_pynini_backend_matches_scanner(fst):
    text = normalize_text(
        "سی سی یو، آی وی 20 میلی گرم، ct scan و iv line و هایپرتنشن"
    )
    fst_output, _ = fst.canonicalize(text)
    scan_output, _ = fst._scan(text)
    assert fst_output == scan_output


def test_fallback_scanner_matches_pynini(fst):
    if not fst.uses_pynini:
        pytest.skip("pynini not available")
    text = normalize_text(
        "بیمار در سی تی اسکن لیژن رایت لانگ و فشار خون بالا دارد"
    )
    pynini_output, _ = fst.canonicalize(text)
    fst.uses_pynini = False
    try:
        scan_output, _ = fst.canonicalize(text)
    finally:
        fst.uses_pynini = True
    assert scan_output == pynini_output


# ------------------------------------------------------------- rule loading

def test_conflicting_forms_resolve_deterministically(tmp_path):
    (tmp_path / "medical_knowledge").mkdir()
    tiers = {
        "abbreviation": 0, "observed_alias": 1, "phrase": 2,
        "validated_term": 3, "unit": 4,
    }
    (tmp_path / "medical_knowledge" / "fst_terms.json").write_text(json.dumps({
        "tiers": tiers,
        "rules": [
            {"form": "آی وی", "canonical": "IV", "tier": "abbreviation"},
            {"form": "سی تی اسکن", "canonical": "CT scan", "tier": "phrase"},
            {"form": "سی تی", "canonical": "CT", "tier": "abbreviation"},
        ],
    }), encoding="utf-8")
    layer = MedicalFST(tmp_path)
    assert layer.warnings == []
    assert canon(layer, "آی وی") == "IV"
    assert canon(layer, "سی تی اسکن") == "CT scan"


def test_conflict_keeps_curated_source_and_warns(tmp_path):
    (tmp_path / "medical_knowledge").mkdir()
    tiers = {
        "abbreviation": 0, "observed_alias": 1, "phrase": 2,
        "validated_term": 3, "unit": 4,
    }
    (tmp_path / "medical_knowledge" / "fst_terms.json").write_text(json.dumps({
        "tiers": tiers,
        "rules": [
            {"form": "آی وی", "canonical": "IV", "tier": "abbreviation"},
        ],
    }), encoding="utf-8")
    (tmp_path / "medical_knowledge" / "observed_asr_aliases.json").write_text(
        json.dumps({"آی وی": {"spoken_forms": ["آی وی"]}}), encoding="utf-8")
    layer = MedicalFST(tmp_path)
    # same canonical -> clean dedupe, no warning
    assert layer.warnings == []
    out, _ = layer.canonicalize("آی وی")
    assert out == "IV"


def test_conflicting_canonicals_warn(tmp_path):
    (tmp_path / "medical_knowledge").mkdir()
    tiers = {
        "abbreviation": 0, "observed_alias": 1, "phrase": 2,
        "validated_term": 3, "unit": 4,
    }
    (tmp_path / "medical_knowledge" / "fst_terms.json").write_text(json.dumps({
        "tiers": tiers,
        "rules": [
            {"form": "آی وی", "canonical": "IV", "tier": "abbreviation"},
        ],
    }), encoding="utf-8")
    (tmp_path / "medical_knowledge" / "observed_asr_aliases.json").write_text(
        json.dumps({"intravenous": {"spoken_forms": ["آی وی"]}}),
        encoding="utf-8",
    )
    layer = MedicalFST(tmp_path)
    assert any("conflicting canonicals" in w for w in layer.warnings)
    out, _ = layer.canonicalize("آی وی")
    assert out == "IV"  # curated fst_terms.json wins


def test_punctuation_forms_are_skipped(tmp_path):
    (tmp_path / "medical_knowledge").mkdir()
    tiers = {
        "abbreviation": 0, "observed_alias": 1, "phrase": 2,
        "validated_term": 3, "unit": 4,
    }
    (tmp_path / "medical_knowledge" / "fst_terms.json").write_text(json.dumps({
        "tiers": tiers,
        "rules": [
            {"form": "سی سی یو", "canonical": "CCU", "tier": "phrase"},
            {"form": "سی سی یو،", "canonical": "CCU", "tier": "observed_alias"},
        ],
    }), encoding="utf-8")
    layer = MedicalFST(tmp_path)
    assert any("punctuation" in w for w in layer.warnings)
    out, _ = layer.canonicalize("سی سی یو،")
    assert out == "CCU،"  # clean form matches, comma preserved


def test_unknown_tier_raises(tmp_path):
    (tmp_path / "medical_knowledge").mkdir()
    (tmp_path / "medical_knowledge" / "fst_terms.json").write_text(json.dumps({
        "tiers": {"phrase": 0},
        "rules": [{"form": "x", "canonical": "y", "tier": "nope"}],
    }), encoding="utf-8")
    with pytest.raises(FstError):
        MedicalFST(tmp_path)


def test_uses_pynini_flag(fst):
    try:
        import pynini  # noqa: F401
        assert fst.uses_pynini is True
    except ImportError:
        assert fst.uses_pynini is False
