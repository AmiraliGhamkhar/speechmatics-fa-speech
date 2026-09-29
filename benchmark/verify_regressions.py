"""Regression-by-breaking: disable each fix in turn and confirm the new tests fail.

Restores the files exactly afterwards; the working tree is verified clean at
the end.
"""
import subprocess
import sys
from pathlib import Path

TEXT = Path("speechmatics_test/text.py")
MATCHER = Path("speechmatics_test/matcher.py")
REALTIME = Path("speechmatics_test/realtime.py")
APP = Path("app.py")
TESTS = Path("tests/test_matcher.py")
TEST_APP = Path("tests/test_app.py")
TEST_REALTIME = Path("tests/test_realtime.py")
TEST_E2E = Path("tests/test_e2e.py")

BACKUP = {
    p: p.read_text(encoding="utf-8")
    for p in (TEXT, MATCHER, REALTIME, APP, TESTS, TEST_APP, TEST_REALTIME, TEST_E2E)
}

DECIMAL_TESTS = [
    "test_spoken_decimal_halves_fold_to_the_exact_value",
    "test_spoken_number_run_reads_decimal_shapes",
    "test_spoken_decimals_never_fold_without_a_measurement",
    "test_a_plain_integer_is_never_rendered_as_a_float",
    "test_spoken_decimal_fold_is_idempotent",
    "test_decimal_shapes_are_not_recognised_without_half_words",
]
ANCHOR_TESTS = [
    "test_measurement_words_anchor_the_value_that_follows",
    "test_qualitative_lab_context_never_invents_a_value",
]
DENOMINATOR_TESTS = [
    "test_explicit_denominator_decimals_fold_to_the_exact_value",
    "test_explicit_denominator_decimals_fold_the_same_in_the_live_path",
    "test_explicit_denominator_fold_is_idempotent",
    "test_spoken_number_run_reads_explicit_denominators",
    "test_denominator_words_never_digitize_prose",
    "test_no_digit_is_ever_left_beside_a_dangling_denominator",
]
CLAIM_TESTS = [
    "test_every_multi_token_canonical_is_a_fixed_point",
    "test_canonical_phrases_survive_the_live_narrative_path",
    "test_canonical_claim_never_duplicates_a_word",
    "test_at_risk_canonicals_finds_the_self_embedding_shapes",
    "test_claim_rules_do_not_buffer_ordinary_prose",
]
STREAM_TESTS = [
    "test_a_number_group_survives_any_segment_boundary",
    "test_every_word_boundary_split_of_a_measurement_matches_single_pass",
]
VOCAB_TESTS = [
    "test_wrong_script_pronunciations_are_dropped_and_reported",
    "test_unclassifiable_pronunciations_are_kept",
]
WARNING_TESTS = ["test_server_warnings_are_recorded_not_only_logged"]
MIC_TESTS = ["test_microphone_open_failure_is_reported_not_a_traceback"]

#: Which test files each break's tests live in.
APP_FILES = ["tests/test_app.py"]
REALTIME_FILES = ["tests/test_realtime.py"]
E2E_FILES = ["tests/test_e2e.py"]

BREAKS = [
    ("A. drop the spoken half (نیم)", TEXT,
     "        if tail is not None:", "        if False:",
     DECIMAL_TESTS),
    # (anchor updated: the D4 fix moved this computation out of the return
    # statement and made it consult an explicit denominator first)
    ("B. drop the unit+و+unit decimal (1.8)", TEXT,
     "                value = parts[0] + follower / 10",
     "                value = parts[0]", DECIMAL_TESTS),
    ("C. drop the lab/measurement anchors", MATCHER,
     "    }) | _VITAL_SIGN_TOKENS | _LAB_ANALYTE_TOKENS | _PERSIAN_MEASUREMENT_TOKENS),",
     "    }) | _VITAL_SIGN_TOKENS),", ANCHOR_TESTS),
    ("D. drop the Persian measurement anchors", MATCHER,
     "    }) | _VITAL_SIGN_TOKENS | _LAB_ANALYTE_TOKENS | _PERSIAN_MEASUREMENT_TOKENS),",
     "    }) | _VITAL_SIGN_TOKENS | _LAB_ANALYTE_TOKENS),", ANCHOR_TESTS),
    ("E. drop the decimal blockers (age range / duration)", MATCHER,
     '    decimal_blockers=frozenset({\n'
     '        "سال", "ساله", "ماه", "ماهه", "هفته", "روز", "ساعت", "دقیقه",\n'
     '    }),', "    decimal_blockers=frozenset(),", DECIMAL_TESTS),

    # ---- explicit fraction denominators (D4) and the all-or-nothing group (D3)
    ("F. drop the explicit-denominator decimal (D4)", TEXT,
     "            if _is_unit(follower):\n"
     "                fraction = _fraction_denominator(tokens, cursor + 2)",
     "            if False:\n"
     "                fraction = _fraction_denominator(tokens, cursor + 2)",
     DENOMINATOR_TESTS),
    ("G. drop the trailing-denominator group extension (D3)", TEXT,
     "        if _bare(tokens[group_end]) in SPOKEN_FRACTION_DENOMINATORS:",
     "        if False and _bare(tokens[group_end]) in SPOKEN_FRACTION_DENOMINATORS:",
     DENOMINATOR_TESTS),

    # ---- canonical spans must be fixed points (D8)
    ("H. drop the canonical claim rules (D8)", MATCHER,
     "        claims = at_risk_canonicals(alias_rules)",
     "        claims = []",
     CLAIM_TESTS),

    # ---- one number group across final-segment boundaries (D1 + D5)
    ("I. drop the pending-unit head lookup (D1)", APP,
     "        heads |= self._medical.strict_prefix_canonical_heads(\n"
     "            tokens[cut:], preserve_narrative=True\n"
     "        )",
     "        heads |= frozenset()",
     STREAM_TESTS, APP_FILES),
    ("J. shrink the unit walkback to adjacent numerals (D5)", APP,
     "               and (tokens[cut - 1] in _NUMBER_GROUP_TOKENS\n"
     "                    or tokens[cut - 1].isdigit())",
     "               and tokens[cut - 1] in SPOKEN_NUMERALS",
     STREAM_TESTS, APP_FILES),
    ("K. shrink the numeric tail to the old group (D5)", APP,
     "        number_tokens = _NUMBER_GROUP_TOKENS",
     "        number_tokens = frozenset(SPOKEN_NUMERALS) | frozenset(\n"
     "            SPOKEN_THOUSANDS) | {SPOKEN_NUMERAL_JOINER}",
     STREAM_TESTS, APP_FILES),

    # ---- Speechmatics session surface (D6, D7)
    ("L. drop the vocabulary script filter (D6)", REALTIME,
     '                if expected_script and (\n'
     '                    _dominant_script(sound) not in ("", expected_script)\n'
     '                ):',
     "                if False:",
     VOCAB_TESTS, REALTIME_FILES),
    ("M. drop the WARNING subscription (D7)", REALTIME,
     "                if warning_event is not None:",
     "                if False:",
     WARNING_TESTS, REALTIME_FILES),

    # ---- microphone open failure must not escape as a traceback (D2)
    ("N. drop the guarded microphone __enter__ (D2)", APP,
     "            recorder.__enter__()",
     "            pass",
     MIC_TESTS, E2E_FILES),
]

#: Test files a break may need; the matcher file is always included because
#: most fixes are pinned there.
DEFAULT_FILES = ["tests/test_matcher.py"]


def run(tests, files=None):
    command = [sys.executable, "-m", "pytest",
               *(files or DEFAULT_FILES), "-q",
               "--no-header", "-p", "no:cacheprovider"]
    if tests:
        command += ["-k", " or ".join(tests)]
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=1800)
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return result.returncode, lines[-1] if lines else "<no output>"

def restore():
    for path, text in BACKUP.items():
        path.write_text(text, encoding="utf-8")

ok = True
try:
    all_files = ["tests/test_matcher.py", "tests/test_app.py",
                 "tests/test_realtime.py", "tests/test_e2e.py"]
    code, line = run([], all_files)
    print(f"baseline (all tests): {'PASS' if code == 0 else 'FAIL'} - {line}")
    every_test = sorted({t for entry in BREAKS for t in entry[4]})
    code, line = run(every_test, all_files)
    print(f"baseline (pinned tests only): {'PASS' if code == 0 else 'FAIL'} - {line}\n")
    if code != 0:
        ok = False
    for entry in BREAKS:
        name, path, old, new, tests = entry[:5]
        files = entry[5] if len(entry) > 5 else None
        original = BACKUP[path]
        if original.count(old) != 1:
            print(f"  {name}: ANCHOR NOT UNIQUE ({original.count(old)}) - skipped")
            ok = False
            continue
        path.write_text(original.replace(old, new), encoding="utf-8")
        try:
            code, line = run(tests, files)
        finally:
            restore()
        verdict = "correctly FAILS" if code != 0 else "!!! STILL PASSES - test is dead"
        if code == 0:
            ok = False
        print(f"  {name}: {verdict} - {line}")
finally:
    restore()

print("\nfiles restored exactly:",
      all(p.read_text(encoding="utf-8") == t for p, t in BACKUP.items()))
sys.exit(0 if ok else 1)
