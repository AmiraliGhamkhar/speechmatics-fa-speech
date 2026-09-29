"""Post-processing benchmark contracts.

The benchmark is the reproducible proof for two claims this project makes:

1. the deterministic layer never damages a transcript the ASR got right
   (canonical == expected for every case, single-pass and streamed), and
2. the streamed (realtime final-boundary) path is byte-identical to the
   single-pass path, so cross-segment buffering cannot lose or duplicate text.

It also keeps the stage metrics separated, so a raw ASR score is never
confused with a post-processing score.
"""

from benchmark.benchmark_postprocess import _load_cases, run
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
    assert set(stages) == {
        "raw", "normalized", "canonical_single_pass", "canonical_streamed",
    }
    for stage in stages.values():
        for key in ("number_precision", "number_recall", "number_f1"):
            assert key in stage
    assert stages["canonical_streamed"]["number_recall"] is not None


def test_metrics_are_the_evaluation_module_metrics():
    numbers = (number_precision, number_recall, number_f1)
    reference, hypothesis = "BP 120/80", "BP 120/80 99/44"
    assert numbers[0](reference, hypothesis) < 1.0
    assert numbers[1](reference, hypothesis) == 1.0   # nothing lost
    assert numbers[2](reference, hypothesis) < 1.0    # fabricated value caught


def _cases_path():
    from pathlib import Path

    return Path(__file__).resolve().parents[1] / "benchmark" / "postprocess_cases.json"
