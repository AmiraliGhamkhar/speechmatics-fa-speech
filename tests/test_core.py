from pathlib import Path

from speechmatics_test.evaluation import evaluate, evaluate_stages, numbers
from speechmatics_test.medical_layer import MedicalLayer
from speechmatics_test.realtime import SessionResult, confidence_summary
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


def test_number_accuracy_never_penalizes_fabricated_extra_numbers():
    """number_accuracy (recall-only) keeps its exact legacy definition and
    value for API stability: an extra, fabricated hypothesis number scores
    a perfect 1.0 here because every reference number is still present.
    """
    x = evaluate("20 mg", "20 mg 50 mg")
    assert x["number_accuracy"] == 1.0


def test_number_precision_recall_f1_flag_extra_hypothesis_numbers():
    """The additive number_precision/number_recall/number_f1 fields DO
    catch the imperfection that number_accuracy alone cannot: reference
    "20 mg" vs hypothesis "20 mg 50 mg" must not be reported as perfect.
    """
    from speechmatics_test.evaluation import (
        number_f1,
        number_precision,
        number_recall,
    )

    x = evaluate("20 mg", "20 mg 50 mg")
    assert x["number_precision"] == 0.5
    assert x["number_recall"] == 1.0
    assert round(x["number_f1"], 4) == round(2 * 0.5 * 1.0 / (0.5 + 1.0), 4)
    assert x["number_f1"] < 1.0

    # direct function access matches the report fields
    assert number_precision("20 mg", "20 mg 50 mg") == 0.5
    assert number_recall("20 mg", "20 mg 50 mg") == 1.0
    assert round(number_f1("20 mg", "20 mg 50 mg"), 4) == x["number_f1"]

    # perfect match still scores perfectly on every metric
    perfect = evaluate("20 mg", "20 mg")
    assert perfect["number_precision"] == 1.0
    assert perfect["number_recall"] == 1.0
    assert perfect["number_f1"] == 1.0

    # no numbers anywhere: every numeric metric is None (nothing to score)
    none_case = evaluate("no numbers here", "still none")
    assert none_case["number_accuracy"] is None
    assert none_case["number_precision"] is None
    assert none_case["number_recall"] is None
    assert none_case["number_f1"] is None

    # hypothesis invents numbers where the reference has none: precision
    # correctly reports a total miss instead of "nothing to compare".
    invented = evaluate("no numbers here", "20 mg appeared")
    assert invented["number_accuracy"] is None  # reference has no numbers
    assert invented["number_precision"] == 0.0
    assert invented["number_f1"] == 0.0


def test_evaluate_stages():
    result = evaluate_stages("expected text", {
        "raw": "raw text",
        "normalized": "normalized text",
        "canonical": "expected text",
    })
    assert set(result) == {"raw", "normalized", "canonical"}
    assert result["canonical"]["wer"] == 0.0
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


def test_confidence_summary_preserves_unknown_language_and_missing_scores():
    summary = confidence_summary([
        {"content": "MRI", "confidence": 0.95, "language": "en"},
        {"content": "سی", "confidence": 0.40, "language": "fa-IR"},
        {"content": "?", "confidence": None, "language": None},
    ])
    assert summary == {
        "word_count": 3,
        "scored_word_count": 2,
        "low_confidence_threshold": 0.75,
        "low_confidence_count": 1,
        "language_counts": {"Persian": 1, "English": 1, "unknown": 1},
        "entity_word_count": 0,
        "low_confidence_entity_count": 0,
        "low_confidence_entities": [],
        "mean": 0.675,
        "min": 0.4,
        "max": 0.95,
    }


def test_confidence_summary_surfaces_uncertain_numeric_and_entity_tokens():
    words = [
        {"content": "120/80", "confidence": 0.40, "language": "en", "start_time": 1.0, "end_time": 1.2},
        {"content": "5", "confidence": 0.95, "language": "en", "start_time": 1.2, "end_time": 1.3},
        {"content": "mg", "confidence": 0.45, "language": "en", "start_time": 1.3, "end_time": 1.4},
        {"content": "2.5", "confidence": 0.96, "language": "en", "start_time": 1.4, "end_time": 1.5},
        {"content": "mL", "confidence": 0.96, "language": "en", "start_time": 1.5, "end_time": 1.6},
        {"content": "HbA1c", "confidence": 0.50, "language": "en", "start_time": 1.6, "end_time": 1.8},
        {"content": "O2", "confidence": 0.96, "language": "en", "start_time": 1.8, "end_time": 1.9},
        {"content": "C3-C4", "confidence": 0.96, "language": "en", "start_time": 1.9, "end_time": 2.0},
        {"content": "q2h", "confidence": 0.96, "language": "en", "start_time": 2.0, "end_time": 2.1},
        {"content": "mmHg", "confidence": 0.50, "language": "en", "start_time": 2.1, "end_time": 2.2},
    ]
    summary = confidence_summary(words)
    assert summary["entity_word_count"] == len(words)
    assert summary["low_confidence_entity_count"] == 4
    assert [word["content"] for word in summary["low_confidence_entities"]] == [
        "120/80", "mg", "HbA1c", "mmHg",
    ]


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


# ------------------------------------------------------- digit normalization

def test_persian_digits_are_folded_to_ascii():
    """Speechmatics returns Persian digits on Persian streams.

    Regression: a dose dictated as "20 mg" came back as "۲۰ mg" and the
    benchmark scored a perfectly correct number as wrong.
    """
    assert normalize_text("۲۰ میلی گرم") == "20 میلی گرم"
    assert normalize_text("٢٠ mg") == "20 mg"          # Arabic-Indic
    assert normalize_text("۱۲۰/۸۰") == "120/80"


def test_persian_decimal_separator_is_normalized():
    assert normalize_text("۵٫۵") == "5.5"
    assert tokens("۵٫۵ mL") == ["5.5", "mL"]


def test_number_metrics_survive_persian_digits():
    x = evaluate("دوز 20 mg", "دوز ۲۰ mg")
    assert x["number_accuracy"] == 1.0
    assert x["wer"] == 0.0

    bp = evaluate("BP 120/80", "BP ۱۲۰/۸۰")
    assert bp["number_accuracy"] == 1.0
    assert bp["wer"] == 0.0


def test_genuinely_wrong_numbers_are_still_wrong():
    """Digit folding must not mask real recognition errors."""
    assert evaluate("BP 120/80", "BP ۱۱۰/۸۰")["number_accuracy"] == 0.0


def test_digit_normalization_is_idempotent():
    t = "دوز ۲۰ میلی گرم و BP ۱۲۰/۸۰"
    assert normalize_text(normalize_text(t)) == normalize_text(t)
