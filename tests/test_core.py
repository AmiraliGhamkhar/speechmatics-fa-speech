from pathlib import Path

import pytest

from speechmatics_test.evaluation import evaluate, evaluate_stages, numbers
from speechmatics_test.medical_layer import MedicalLayer
from speechmatics_test.realtime import SessionResult
from speechmatics_test.text import normalize_text, tokens

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ normalize

def test_text_normalization():
    assert normalize_text("كیست   هموراژیک") == "کیست هموراژیک"


def test_normalization_zwnj_and_punctuation():
    assert normalize_text("سی‌سی‌یو") == "سی سی یو"
    assert normalize_text("دوز 20 ،") == "دوز 20،"
    assert normalize_text("a  b") == "a b"


def test_normalize_is_idempotent():
    t = "بیمار در سی تی اسکن و فشار خون بالا"
    assert normalize_text(normalize_text(t)) == normalize_text(t)


# ------------------------------------------------------------------ tokenizer

def test_tokenizer_plain_words():
    assert tokens("hello world") == ["hello", "world"]


def test_tokenizer_numbers_and_units():
    assert tokens("دوز 20 mg") == ["دوز", "20", "mg"]
    assert tokens("20mg") == ["20", "mg"]
    assert tokens("5.5 mg") == ["5.5", "mg"]
    assert tokens("3,14 mL") == ["3,14", "mL"]


def test_tokenizer_bp_ratio():
    assert tokens("BP 120/80") == ["BP", "120/80"]


def test_tokenizer_alnum_codes():
    assert tokens("O2 saturation") == ["O2", "saturation"]
    assert tokens("C3-C4 vertebroe") == ["C3-C4", "vertebroe"]
    assert tokens("q2h dosing") == ["q2h", "dosing"]
    assert tokens("HbA1c level") == ["HbA1c", "level"]
    assert tokens("U/A done") == ["U/A", "done"]


def test_numbers_helper():
    assert numbers("BP 120/80 و دوز 20 mg, C3-C4 q2h O2") == ["120/80", "20"]


# ------------------------------------------------------------------- metrics

def test_wer_identical():
    x = evaluate("دوز 20 میلی گرم", "دوز 20 میلی گرم")
    assert x["wer"] == 0.0
    assert x["number_accuracy"] == 1.0


def test_number_accuracy():
    x = evaluate("دوز 20 میلی گرم", "دوز 20 میلی گرم")
    assert x["number_accuracy"] == 1.0

    assert evaluate("BP 120/80", "BP 110/80")["number_accuracy"] == 0.0
    assert evaluate("no numbers here", "whatever")["number_accuracy"] is None


def test_number_accuracy_multiset():
    # Two "20" in reference, one in hypothesis -> 0.5, not 1.0
    x = evaluate("20 mg and 20 mL", "20 mg")
    assert x["number_accuracy"] == 0.5


def test_evaluate_stages():
    result = evaluate_stages("expected text", {
        "raw": "raw text",
        "normalized": "normalized text",
        "fst_canonical": "expected text",
    })
    assert set(result) == {"raw", "normalized", "fst_canonical"}
    assert result["fst_canonical"]["wer"] == 0.0
    assert "wer" in result["raw"] and "number_accuracy" in result["raw"]


# ------------------------------------------------- partial vs final separation

def test_session_result_final_text_uses_finals_only():
    r = SessionResult(language="fa")
    r.partials.append({"t_ms": 10, "text": "this is only a partial"})
    r.final_segments.append({"t_ms": 20, "text": "first final"})
    r.final_segments.append({"t_ms": 30, "text": "second final"})
    assert r.final_text == "first final second final"
    assert "partial" not in r.final_text


def test_session_result_final_text_is_raw():
    r = SessionResult(language="fa")
    r.final_segments.append({"t_ms": 1, "text": "سی تی اسکن"})  # raw, unnormalized
    assert r.final_text == "سی تی اسکن"


# ---------------------------------------------------------------- medical FST

def test_alias_canonicalization():
    layer = MedicalLayer(ROOT)
    out, hits = layer.normalize("کیست هموراژیک در تخمدان")
    assert "cyst" in out
    assert "hemorrhagic" in out
    assert hits


def test_layer_canonicalize_expects_normalized_input():
    layer = MedicalLayer(ROOT)
    out_via_normalize, _ = layer.normalize("سی تی اسکن")
    out_direct, _ = layer.canonicalize(normalize_text("سی تی اسکن"))
    assert out_via_normalize == out_direct == "CT scan"
