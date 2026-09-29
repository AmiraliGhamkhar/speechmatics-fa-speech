"""Post-processing benchmark contracts.

The benchmark is the reproducible proof for two claims this project makes:

1. the deterministic layer never damages a transcript the ASR got right
   (canonical == expected for every case, single-pass and streamed), and
2. the streamed (realtime final-boundary) path is byte-identical to the
   single-pass path, so cross-segment buffering cannot lose or duplicate text.

It also keeps the stage metrics separated, so a raw ASR score is never
confused with a post-processing score.
"""

from benchmark.benchmark_postprocess import STAGE_LABELS, _load_cases, run
from speechmatics_test.evaluation import number_f1, number_precision, number_recall


def test_every_case_canonicalizes_exactly_and_streams_identically():
    cases = _load_cases(_cases_path())
    report = run(cases)

    assert report["case_count"] == len(cases)
    assert report["single_pass"]["exact_match"] == 1.0
    assert report["streamed"]["exact_match"] == 1.0
    assert report["single_pass"]["wer"] == 0.0
    assert report["streamed"]["wer"] == 0.0
    assert report["streaming_agrees_with_single_pass"] == len(cases)
    assert all(case["streamed_ok"] for case in report["cases"])


def test_stage_metrics_are_separated_and_number_metrics_are_unambiguous():
    cases = [
        {
            "id": "fabricated_extra_number",
            "segments": ["دوز 20 mg و 50 mg"],
            "expected": "دوز 20 mg",
        }
    ]
    report = run(cases)

    # The reference contains exactly the numbers the hypothesis contains, so
    # adding an extra value must show up as a number-precision/recall loss.
    stages = report["stages"]
    assert set(stages) == set(STAGE_LABELS) == {
        "raw", "normalized",
        "medical_canonicalization_only", "numeric_fold_only",
        "canonical_single_pass", "canonical_streamed",
    }
    for stage in stages.values():
        for key in ("number_precision", "number_recall", "number_f1"):
            assert key in stage
    assert stages["canonical_streamed"]["number_recall"] is not None
    # every stage reports the unambiguous metrics, never a bare "accuracy"
    for name, stage in stages.items():
        assert "number_accuracy" not in stage, name


def test_the_two_deterministic_halves_are_measured_apart():
    """Stages C and D each move the transcript; neither alone is complete.

    Measuring them together (the old single ``canonical`` stage) cannot say
    which half broke: a destroyed medical term and an invented dose look
    identical in the end-to-end score. Run apart, each is visibly partial -
    C reproduces the terms but leaves the spoken numbers spoken, D folds the
    numbers it is allowed to but leaves the Persian terms untranslated.
    """
    cases = _load_cases(_cases_path())
    stages = run(cases)["stages"]
    medical = stages["medical_canonicalization_only"]
    numeric = stages["numeric_fold_only"]
    end_to_end = stages["canonical_single_pass"]

    assert end_to_end["exact_match"] == 1.0
    # C alone: terms canonical, numbers still spoken
    assert medical["exact_match"] < 1.0
    assert medical["number_recall"] < end_to_end["number_recall"]
    # D alone: numbers folded where permitted, terms still Persian
    assert numeric["exact_match"] < 1.0
    assert numeric["number_recall"] < end_to_end["number_recall"]


def test_the_numeric_fold_is_gated_by_the_lexical_pass():
    """Stage D scores below stage E BY DESIGN - that is the prose-safety rule.

    The fold refuses to digitize a spoken number unless a recognized
    measurement anchor touches it, and the anchors come from the dictionary
    pass ("فشار خون" -> "BP", "قند خون" -> "glucose", "میلی گرم" -> "mg").
    So a spoken number that has not yet been anchored must stay spoken: this
    is exactly what stops ordinary Persian prose from being converted into
    clinical values. Only C followed by D reaches the reference.
    """
    from speechmatics_test.matcher import NUMERIC_CONTEXT
    from speechmatics_test.text import fold_numeric_expressions, normalize_text

    unanchored = "قند خون صد و هشتاد"
    assert fold_numeric_expressions(
        normalize_text(unanchored), NUMERIC_CONTEXT
    ) == unanchored            # nothing invented without an anchor

    anchored = "glucose صد و هشتاد"
    assert fold_numeric_expressions(
        normalize_text(anchored), NUMERIC_CONTEXT
    ) == "glucose 180"          # the same number, once anchored


def test_metrics_are_the_evaluation_module_metrics():
    numbers = (number_precision, number_recall, number_f1)
    reference, hypothesis = "BP 120/80", "BP 120/80 99/44"
    assert numbers[0](reference, hypothesis) < 1.0
    assert numbers[1](reference, hypothesis) == 1.0   # nothing lost
    assert numbers[2](reference, hypothesis) < 1.0    # fabricated value caught


def _cases_path():
    from pathlib import Path

    return Path(__file__).resolve().parents[1] / "benchmark" / "postprocess_cases.json"
