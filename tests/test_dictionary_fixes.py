"""Focused dictionary checks for the clinical-entity safety pass."""

import json
import re
from collections import defaultdict
from pathlib import Path

from speechmatics_test.matcher import MedicalMatcher, casefold_preserving
from speechmatics_test.text import SPOKEN_NUMERALS, normalize_text


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
        "normal-saline-fa": "نرمال سالین",
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
        "normal-saline-en": "Normal Saline",
        "pressure-sore": "Pressure Sore",
        "skin-turgor": "Skin Turgor",
    }
    for term_id, canonical in expected.items():
        assert terms[term_id]["canonical"] == canonical
        assert terms[term_id]["speechmatics"] is True


def test_no_canonical_adds_a_number_its_form_did_not_have():
    """A rule may rename a term; it may never attach a value to it.

    ``normal saline`` used to canonicalize to ``Normal Saline 0.9%``, so every
    mention gained a concentration that was not dictated - and a differently
    dosed saline came out carrying two contradictory ones
    ("IV normal saline 0.45%" -> "IV Normal Saline 0.9% 0.45%").
    """
    # A STANDALONE numeric token is a measured value ("0.9%", "145"). Digits
    # that are part of a term's name ("SpO2", "C3-C4", "HbA1c") are not.
    value_token = re.compile(r"(?<![^\s])\d+(?:[.,]\d+)?%?(?![^\s])")

    def stated_by(form: str, value: str) -> bool:
        """Whether ``form`` already states ``value``, in any spelling."""
        digits = value.rstrip("%")
        # Written in the form, possibly fused ("q2h", "هر2ساعت", "هر ۲ ساعت").
        if digits in normalize_text(form):
            return True
        # Spoken as a word ("مرحله یک", "every three hours").
        english = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
            "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12,
        }
        try:
            wanted = int(digits)
        except ValueError:
            return False
        for word in normalize_text(form).casefold().split():
            if SPOKEN_NUMERALS.get(word) == wanted or english.get(word) == wanted:
                return True
        return False

    data = json.loads(DICTIONARY.read_text(encoding="utf-8"))
    offenders = []
    for term in data["terms"]:
        for value in set(value_token.findall(term["canonical"])):
            for form in term["forms"]:
                if not stated_by(form, value):
                    offenders.append((term["id"], form, term["canonical"]))
    assert offenders == []


def test_normal_saline_keeps_the_dictated_concentration():
    assert _canonicalize("normal saline") == "Normal Saline"
    assert _canonicalize("normal saline 0.45%") == "Normal Saline 0.45%"
    assert _canonicalize("نرمال سالین") == "نرمال سالین"


def test_no_duplicate_forms_in_dictionary():
    data = json.loads(DICTIONARY.read_text(encoding="utf-8"))
    owners: defaultdict[str, list[str]] = defaultdict(list)
    for term in data["terms"]:
        for form in term["forms"]:
            normalized = casefold_preserving(normalize_text(form))
            owners[normalized].append(term["id"])
    duplicates = {form: term_ids for form, term_ids in owners.items() if len(term_ids) > 1}
    assert duplicates == {}
