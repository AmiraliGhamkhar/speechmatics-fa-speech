"""Offline ASR benchmark metric regressions (no audio or network required)."""

from benchmark.benchmark_asr import DEFAULT_TEST_CASES, run_benchmark, word_error_rate


def test_word_error_rate_is_zero_for_equal_transcripts():
    assert word_error_rate("BP 120/80", "BP 120/80")["wer"] == 0.0


def test_dry_run_scores_stored_transcript_and_emits_flag_metrics():
    report = run_benchmark(DEFAULT_TEST_CASES, mode="offline", dry_run=True)
    case = report["cases"][0]

    assert report["mode"] == "offline"
    assert report["dry_run"] is True
    assert case["raw_wer"]["wer"] > 0
    assert case["entity_metrics"]["recall"] < 1
    assert case["flag_effectiveness"]["wrong_entities_flagged"] > 0
    assert any(flag["type"] == "temperature" for flag in case["entity_flags"])


def test_live_mode_requires_explicit_api_key():
    try:
        run_benchmark(DEFAULT_TEST_CASES, mode="live", api_key="")
    except RuntimeError as exc:
        assert "SPEECHMATICS_API_KEY" in str(exc)
    else:  # pragma: no cover - avoids silently accepting an unsafe live default
        raise AssertionError("live benchmark unexpectedly ran without a key")
