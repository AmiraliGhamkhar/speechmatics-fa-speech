"""Focused regressions for clean injection and non-destructive ASR review flags."""

from pathlib import Path

import app as app_module
from injector import TextInjector
from speechmatics_test.medical_layer import MedicalLayer
from speechmatics_test.presentation import has_bidi_controls
from speechmatics_test.text import normalize_text


ROOT = Path(__file__).resolve().parents[1]


def test_default_injector_pastes_clean_logical_unicode():
    injector = TextInjector(dry_run=True, add_bidi_marks=False)
    logical = "بیمار SpO2 97 % دارد"

    payload = injector.prepare_mixed_text(logical)

    assert payload == logical
    assert not has_bidi_controls(payload)


def test_bidi_injection_remains_explicit_compatibility_opt_in():
    injector = TextInjector(dry_run=True, add_bidi_marks=True)
    payload = injector.prepare_mixed_text("بیمار SpO2 97 % دارد")

    assert has_bidi_controls(payload)
    assert injector.logical_payload(payload) == "بیمار SpO2 97 % دارد"


def test_low_confidence_entity_is_flagged_without_changing_text():
    accumulator = app_module.FinalStreamCanonicalizer(MedicalLayer(ROOT))
    text = normalize_text("Temp 45 ثبت شد")
    words = [
        {"content": "Temp", "confidence": 0.98, "language": "en",
         "start_time": 0.0, "end_time": 0.2},
        {"content": "45", "confidence": 0.42, "language": "fa",
         "start_time": 0.2, "end_time": 0.4},
        {"content": "ثبت", "confidence": 0.99, "language": "fa",
         "start_time": 0.4, "end_time": 0.6},
        {"content": "شد", "confidence": 0.99, "language": "fa",
         "start_time": 0.6, "end_time": 0.8},
    ]

    emitted = accumulator.add(text, words)

    assert emitted == "Temp 45 ثبت شد"
    assert accumulator.canonical_text == "Temp 45 ثبت شد"
    assert accumulator.last_flags == [{
        "kind": "low_confidence_entity",
        "content": "45",
        "confidence": 0.42,
        "language": "fa",
        "start_time": 0.2,
        "end_time": 0.4,
    }]
    assert accumulator.flags == accumulator.last_flags


def test_high_confidence_entity_has_no_review_flag():
    accumulator = app_module.FinalStreamCanonicalizer(MedicalLayer(ROOT))
    emitted = accumulator.add(normalize_text("SpO2 97 %"), [
        {"content": "SpO2", "confidence": 0.99, "language": "en"},
        {"content": "97", "confidence": 0.98, "language": "fa"},
        {"content": "%", "confidence": 0.98, "language": "fa"},
    ])

    assert emitted == "SpO2 97 %"
    assert accumulator.last_flags == []
    assert accumulator.flags == []
