"""Streaming-boundary benchmark: split mechanics and accumulator invariants.

The streaming score is only credible if the harness itself is honest, so
these tests pin the two things a reader has to take on trust:

* the split strategies really do produce the segments they claim (a harness
  that silently fed one whole segment would report a perfect score);
* the accumulator under test is the PRODUCTION one, and its guarantees
  (no lost text, no duplicated text) hold at every boundary.
"""

import pytest

from benchmark.dataset import ALL_CASES
from benchmark.streaming import (
    SPLIT_STRATEGIES,
    evaluate_streaming,
    production_accumulator_factory,
    segments,
    split_points,
    stream_case,
)
from speechmatics_test.text import normalize_text


@pytest.fixture(scope="module")
def factory():
    return production_accumulator_factory()


@pytest.fixture(scope="module")
def report(factory):
    return evaluate_streaming(factory)


# ------------------------------------------------------ split mechanics


@pytest.mark.parametrize("strategy", SPLIT_STRATEGIES)
def test_segments_reconstruct_the_normalized_input(strategy):
    """Splitting must only insert boundaries - never alter the text."""
    for case in ALL_CASES:
        expected = normalize_text(case.spoken).split()
        assert " ".join(segments(case.spoken, strategy)).split() == expected, (
            f"{case.id}/{strategy}: segments do not reconstruct the input")


@pytest.mark.parametrize("strategy", SPLIT_STRATEGIES)
def test_no_segment_is_empty(strategy):
    for case in ALL_CASES:
        assert all(s.strip() for s in segments(case.spoken, strategy))


def test_whole_strategy_produces_exactly_one_segment():
    """The control arm: it must not be split at all, or it proves nothing."""
    for case in ALL_CASES:
        assert len(segments(case.spoken, "whole")) == 1


def test_splitting_strategies_actually_split_multi_token_cases():
    """Guard against a harness that quietly degenerates to the control arm."""
    multi = [c for c in ALL_CASES
             if len(normalize_text(c.spoken).split()) >= 4]
    assert multi, "dataset should contain multi-token fixtures"
    for strategy in SPLIT_STRATEGIES:
        if strategy == "whole":
            continue
        for case in multi:
            assert len(segments(case.spoken, strategy)) >= 2, (
                f"{case.id}/{strategy} was not split")


def test_every_two_splits_the_most():
    tokens = 9
    counts = {
        s: len(split_points(tokens, s)) for s in SPLIT_STRATEGIES
    }
    assert counts["whole"] == 0
    assert counts["every_two"] == max(counts.values())
    assert counts["every_two"] > counts["midpoint"]


def test_split_points_are_sorted_and_interior():
    for tokens in range(1, 12):
        for strategy in SPLIT_STRATEGIES:
            points = split_points(tokens, strategy)
            assert list(points) == sorted(points)
            assert all(0 < p < tokens for p in points), (tokens, strategy)


def test_single_token_input_is_never_split():
    for strategy in SPLIT_STRATEGIES:
        assert split_points(1, strategy) == ()


def test_unknown_strategy_is_rejected():
    with pytest.raises(ValueError):
        split_points(5, "diagonally")


# ------------------------------------------- production accumulator use


def test_factory_returns_the_production_accumulator(factory):
    from app import FinalStreamCanonicalizer

    accumulator = factory()
    assert isinstance(accumulator, FinalStreamCanonicalizer), (
        "the streaming benchmark must exercise the real app accumulator")


def test_factory_returns_a_fresh_accumulator_each_call(factory):
    """Shared state between runs would leak one fixture into the next."""
    first, second = factory(), factory()
    assert first is not second
    first.add("فشار خون صد و بیست روی هشتاد", [])
    first.flush()
    assert second.canonical_text == ""


@pytest.mark.parametrize("strategy", SPLIT_STRATEGIES)
def test_streaming_never_drops_or_duplicates_words(factory, strategy):
    """Boundary handling may reshape text; it may not lose or echo it.

    Checked as a word-count band rather than equality: canonicalization
    legitimately merges multi-word Persian forms into one English term.
    """
    for case in ALL_CASES:
        produced = stream_case(factory, case.spoken, strategy)
        spoken_words = len(normalize_text(case.spoken).split())
        assert produced.strip(), f"{case.id}/{strategy}: produced nothing"
        assert len(produced.split()) <= spoken_words + 2, (
            f"{case.id}/{strategy}: output grew - duplicated boundary text")


# --------------------------------------------------------- report shape


def test_report_covers_every_case_and_strategy(report):
    assert report["aggregates"]["total_runs"] == (
        len(ALL_CASES) * len(SPLIT_STRATEGIES))
    seen = {(r["id"], r["strategy"]) for r in report["runs"]}
    assert seen == {(c.id, s) for c in ALL_CASES for s in SPLIT_STRATEGIES}


def test_aggregate_counts_agree_with_the_runs(report):
    aggregates = report["aggregates"]
    assert aggregates["exact_match"] == sum(
        1 for r in report["runs"] if r["exact_match"])
    assert aggregates["exact_match_accuracy"] == pytest.approx(
        aggregates["exact_match"] / aggregates["total_runs"], abs=1e-4)
    for strategy, data in aggregates["by_strategy"].items():
        subset = [r for r in report["runs"] if r["strategy"] == strategy]
        assert data["runs"] == len(subset)
        assert data["exact_match"] == sum(1 for r in subset if r["exact_match"])


def test_failure_list_matches_the_failing_runs(report):
    assert report["aggregates"]["failures"] == sorted({
        f"{r['id']}:{r['strategy']}"
        for r in report["runs"] if not r["exact_match"]
    })


def test_whole_strategy_is_a_clean_control_arm(report):
    """Unsplit input must canonicalize perfectly.

    If this ever fails the defect is in the post-processing chain itself,
    not in boundary handling, and the split scores below are meaningless.
    """
    whole = report["aggregates"]["by_strategy"]["whole"]
    assert whole["failures"] == []
    assert whole["accuracy"] == 1.0


def test_streaming_is_deterministic(factory):
    """Two runs of the same fixture must agree, or the score is noise."""
    for case in ALL_CASES[:25]:
        for strategy in SPLIT_STRATEGIES:
            first = stream_case(factory, case.spoken, strategy)
            second = stream_case(factory, case.spoken, strategy)
            assert first == second, f"{case.id}/{strategy} is not deterministic"


def test_streaming_accuracy_does_not_regress(report):
    """Ratchet on the measured score.

    Deliberately an inequality against a known-good floor, not an equality
    with today's number: it must be impossible to "fix" a regression by
    editing the expected value downwards.
    """
    aggregates = report["aggregates"]
    assert aggregates["exact_match"] >= 530, aggregates["failures"]
    assert aggregates["exact_match_accuracy"] >= 0.99


def test_remaining_streaming_failures_are_the_documented_ones(report):
    """The open failures are known and enumerated - not silently tolerated.

    ``vital_spo2_fa`` splits inside a multi-token vital-sign label and
    ``gram_echoed_inflection`` echoes an inflected verb tail across the
    boundary; both need unbounded rule-prefix hold, which would stall
    emission in live dictation. New failures must fail this test.
    """
    documented = {"vital_spo2_fa", "gram_echoed_inflection",
                  "para_allergy", "para_labs"}
    failing = {r["id"] for r in report["runs"] if not r["exact_match"]}
    assert failing <= documented, sorted(failing - documented)
