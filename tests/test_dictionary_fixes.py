"""Focused dictionary checks for the clinical-entity safety pass."""

import json
from collections import defaultdict
from pathlib import Path

from speechmatics_test.matcher import MedicalMatcher, casefold_preserving
from speechmatics_test.text import normalize_text


ROOT = Path(__file__).resolve().parents[1]
DICTIONARY = ROOT / "medical_knowledge" / "medical_dictionary.json"


def _terms_by_id() -> dict:
    data = json.loads(DICTIONARY.read_text(encoding="utf-8"))
    return {term["id"]: term for term in data["terms"]}


def _canonicalize(text: str) -> str:
    matcher = MedicalMatcher(ROOT)
    return matcher.canonicalize(normalize_text(text))[0]


def test_bed_side_up_is_the_fall_prevention_specific_match():
    term = _terms_by_id()["bedside-up"]
    assert term["context"] == "fall_prevention"
    assert term["priority"] == 1
    assert _canonicalize("bed side up") == "Bed Side Up"


def test_bedside_rails_wins_before_generic_bedside():
    terms = _terms_by_id()
    assert terms["bedside_rails"]["context"] == "fall_prevention"
    assert terms["bedside_rails"]["priority"] == 2
    assert _canonicalize("bed side rails") == "Bedside Rails"
    assert _canonicalize("bed side") == "Bedside"


def test_bedside_does_not_match_when_up_or_rails_follows():
    assert _canonicalize("bed side up") != "Bedside up"
    assert _canonicalize("bed side rails") != "Bedside rails"


def test_requested_persian_terms_are_in_dictionary_and_asr_vocab():
    terms = _terms_by_id()
    expected = {
        "appendectomy-fa": "آپاندکتومی",
        "myocardial-ischemia-fa": "ایسکمی میوکارد",
        "normal-saline-fa": "نرمال سالین 0.9%",
        "turgor-fa": "تورگور",
        "penicillin-fa": "پنی‌سیلین",
    }
    for term_id, canonical in expected.items():
        assert terms[term_id]["canonical"] == canonical
        assert terms[term_id]["speechmatics"] is True


def test_requested_english_terms_are_in_dictionary_and_asr_vocab():
    terms = _terms_by_id()
    expected = {
        "appendectomy-en": "Appendectomy",
        "myocardial-ischemia-en": "Myocardial Ischemia",
        "normal-saline-en": "Normal Saline 0.9%",
        "pressure-sore": "Pressure Sore",
        "skin-turgor": "Skin Turgor",
    }
    for term_id, canonical in expected.items():
        assert terms[term_id]["canonical"] == canonical
        assert terms[term_id]["speechmatics"] is True


def test_no_duplicate_forms_in_dictionary():
    data = json.loads(DICTIONARY.read_text(encoding="utf-8"))
    owners: defaultdict[str, list[str]] = defaultdict(list)
    for term in data["terms"]:
        for form in term["forms"]:
            normalized = casefold_preserving(normalize_text(form))
            owners[normalized].append(term["id"])
    duplicates = {form: term_ids for form, term_ids in owners.items() if len(term_ids) > 1}
    assert duplicates == {}
