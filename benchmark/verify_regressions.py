"""Regression-by-breaking: disable each fix in turn and confirm the new tests fail.

Restores the files exactly afterwards; the working tree is verified clean at
the end.
"""
import subprocess
import sys
from pathlib import Path

TEXT = Path("speechmatics_test/text.py")
MATCHER = Path("speechmatics_test/matcher.py")
TESTS = Path("tests/test_matcher.py")

BACKUP = {p: p.read_text(encoding="utf-8") for p in (TEXT, MATCHER, TESTS)}

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

BREAKS = [
    ("A. drop the spoken half (نیم)", TEXT,
     "        if tail is not None:", "        if False:",
     DECIMAL_TESTS),
    ("B. drop the unit+و+unit decimal (1.8)", TEXT,
     "                return (cursor + 2 - index, parts[0] + follower / 10)",
     "                break", DECIMAL_TESTS),
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
]

def run(tests):
    command = [sys.executable, "-m", "pytest", "tests/test_matcher.py", "-q",
               "--no-header", "-p", "no:cacheprovider"]
    if tests:
        command += ["-k", " or ".join(tests)]
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=600)
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return result.returncode, lines[-1] if lines else "<no output>"

def restore():
    for path, text in BACKUP.items():
        path.write_text(text, encoding="utf-8")

ok = True
try:
    code, line = run([])
    print(f"baseline (all tests): {'PASS' if code == 0 else 'FAIL'} - {line}")
    code, line = run(DECIMAL_TESTS + ANCHOR_TESTS)
    print(f"baseline (new tests only): {'PASS' if code == 0 else 'FAIL'} - {line}\n")
    if code != 0:
        ok = False
    for name, path, old, new, tests in BREAKS:
        original = BACKUP[path]
        if original.count(old) != 1:
            print(f"  {name}: ANCHOR NOT UNIQUE ({original.count(old)}) - skipped")
            ok = False
            continue
        path.write_text(original.replace(old, new), encoding="utf-8")
        try:
            code, line = run(tests)
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
