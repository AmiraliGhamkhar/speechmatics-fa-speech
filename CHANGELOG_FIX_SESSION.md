# Fix Session Changelog

Each entry: **what was wrong → what changed → why it is safe → test added**.

Verification commands in this file are PowerShell. Run them from the repo root.

---

# Session 3 — Streaming, focus safety, benchmark validity

- Added a bounded nursing pending tail to `FinalStreamCanonicalizer`, including
  spoken numbers, clock expressions, ratios, and cross-final exact stutter.
  The existing nursing parser and repetition-safe matcher forms remain the
  source of truth; regression tests cover all reported boundary examples.
- Armed the injection focus target immediately before realtime starts and
  revalidated it immediately before Ctrl+V, closing both focus races.
- Made automatic medical-domain selection conditional on the Enhanced model;
  explicit domain selection remains unchanged.
- Realtime failures now preserve partial reports but return a non-zero status.
- Added production-accumulator streaming benchmark variants and multiset
  terminology TP/FP/FN accounting. Provenance now marks dirty worktrees.
- Merged three semantically equivalent case-only dictionary canonical groups;
  retained the clinically distinct `Mg`/`mg` pair intentionally.
- Pinned the validated `speechmatics-rt==1.1.1` SDK and made the PowerShell
  bootstrap select a compatible interpreter instead of requiring only 3.11.
- Dictionary `source_file` values are historical references; the active source
  of truth is `medical_knowledge/medical_dictionary.json`, and the vocabulary
  artifact is generated with `export-vocab`.

---

# Session 2 — Benchmark, tooling, dictionary, nursing text

## Starting state

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

**25 failed, 201 passed, 89 errors.** The dictionary declared the canonical
`"PO"` twice, so `load_dictionary` raised and every matcher fixture errored.
The application could not load its own dictionary.

## Result

```powershell
.\.venv\Scripts\python.exe -m pytest -q                                      # 476 passed
.\.venv\Scripts\python.exe -m compileall -q .                                # OK
.\.venv\Scripts\python.exe scripts\swiftmedics_tools.py --help               # OK
.\.venv\Scripts\python.exe scripts\swiftmedics_tools.py check-vocab          # OK
.\.venv\Scripts\python.exe scripts\swiftmedics_tools.py benchmark-matcher    # OK
.\.venv\Scripts\python.exe app.py --help                                     # OK
```

`fa`, `en`, `--no-vocab`, `--no-medical-layer`, `--no-inject`, `--no-overlay`,
`--no-text-polish` all exit gracefully without an API key.

Benchmark, same 107 fixtures, same machine:

| Metric | Baseline | Current |
| --- | --- | --- |
| exact match | 45/107 (42.1%) | 107/107 (100%) |
| terminology F1 | 0.7791 | 1.0 |
| number F1 | 0.7966 | 1.0 |
| false-number rate | 0.029 | 0.0 |
| build time (min) | 58.5 ms | 55.6 ms |
| matcher p50 | 5.78 µs | 5.72 µs |

Baseline = commit `4017279` with **only** the duplicate-canonical merge applied,
because without it the code cannot run the benchmark at all.

---

## 1. Dictionary declared the same canonical twice

- **FILE**: `medical_knowledge/medical_dictionary.json`
- **BUG**: `po` and `route_0049` both declared canonical `"PO"`.
  `load_dictionary` rejects duplicates, so it raised on every startup — the
  whole test suite's 89 errors trace to this one line.
- **FIX**: Merged `route_0049`'s forms into `po`.
- **WHY SAFE**: No form was lost; both entries described the same route.
- **TEST**: `tests/test_benchmark.py::test_dictionary_has_no_duplicate_ids_or_canonicals`,
  `::test_dictionary_loads_without_raising`.

## 2. Ordinary words were stored as medical aliases

- **FILE**: `medical_knowledge/medical_dictionary.json` (via `scripts/_dictionary_fixes.py`)
- **BUG**: The dictionary mapped everyday words to clinical terms, so the
  matcher *translated prose* instead of canonicalizing shorthand:
  `Please do it now` → `Please do intrathecal now`; `بخش قلب` → `بخش heart`;
  `دکتر احمدی` → `doctor احمدی`; `کشش پوستی` → `Traction dermatologic`;
  `سابقه جراحی قبلی ندارد` → `past surgical history قبلی ندارد`.
- **FIX**: Removed ~28 unsafe aliases. Also removed `hour_1`..`hour_12`
  (bare numerals `1`–`12` existed only for time handling; numbers do not
  belong in a lexical term list). Merged three duplicate concept pairs and
  resolved the SpO2 / oxygen-saturation alias conflict. 979 → **971 terms**.
- **WHY SAFE**: Applied by an idempotent script with a per-change audit trail
  naming the benchmark case that caught it. Canonical values were not changed
  for style; no bulk alias rewrite.
- **TEST**: `tests/test_benchmark.py::test_ordinary_words_are_not_medical_aliases`,
  `::test_dictionary_contains_no_numeric_only_terms`,
  `::test_required_nursing_terminology_is_present`.

## 3. Ambiguous English abbreviations rewrote plain prose

- **FILE**: `speechmatics_test/matcher.py`
- **BUG**: The uppercase-evidence guard covered only 8 forms. Words like
  `it`, `us`, `cold`, `skin`, `post`, `pt` were still matched case-insensitively.
- **FIX**: `_AMBIGUOUS_SHORT_FORMS` extended 8 → 28. Each addition is an
  ordinary English word that is *also* charted shorthand.
- **WHY SAFE**: Narrows *when* existing rules fire. The clinical sense stays
  reachable through the uppercase form (`IT`, `US`, `COLD`); Persian and
  fully-spelled aliases are different match forms and unaffected.
- **TEST**: existing ambiguous-short-form tests, plus benchmark boundary cases.

## 4. ASR stutter combined with a neighbour into a phrase nobody said

- **FILE**: `speechmatics_test/nursing_text.py`, `speechmatics_test/medical_layer.py`
- **BUG**: `نمره نمره درد ثبت شد` → `نمره pain score ثبت شد`. The leftover
  stutter token plus the next word spelled the dictionary phrase `نمره درد`,
  so the matcher emitted a term the speaker never uttered.
- **FIX**: New `prepolish_asr_artifacts()` collapses identical adjacent words
  **before** the matcher runs.
- **WHY SAFE**: Only exact adjacent repeats are touched. Word-level ASR
  evidence is positional, so it is dropped rather than misaligned when
  characters actually moved.
- **TEST**: `tests/test_nursing_text.py` repetition tests.

## 5. Repetition cleanup destroyed real abbreviations

- **FILE**: `speechmatics_test/matcher.py`, `speechmatics_test/nursing_text.py`
- **BUG**: Regression introduced by fix 4 — blind de-duplication turned
  `سی سی یو` (CCU) into `سی یو` and broke `آر آر` (RR). The abbreviation
  genuinely contains a doubled syllable.
- **FIX**: New `MedicalMatcher.repetition_safe_forms` (22 folded forms) passed
  as `protected=` into the cleanup, which skips them.
- **WHY SAFE**: Derived from the dictionary itself, so it stays correct as
  terms change.
- **TEST**: `tests/test_nursing_text.py::test_repetition_safe_abbreviations_survive`
  and benchmark case `gram_repeated_abbrev_safe`.

## 6. A fabricated vital sign (clinical safety)

- **FILE**: `speechmatics_test/nursing_text.py`
- **BUG**: `سی و شش و هفت` — how a nurse dictates **36.7 °C** — was parsed by
  summing its parts into **43**. A body temperature nobody said, invented by
  the postprocessor, with no warning. Found by running real spoken sentences
  through the pipeline, not by a fixture.
- **FIX**: `_parse_number_words` now requires a well-formed cardinal: each
  magnitude class named at most once, in descending order. A malformed run is
  left **entirely** verbatim (not half-converted) and raises a warning.
- **WHY SAFE**: Strictly reduces what gets converted. All legitimate cardinals
  (`سی و پنج`, `صد و چهل`, `سه هزار و دویست و سی و پنج`) still convert.
- **TEST**: `tests/test_nursing_text.py::test_malformed_cardinal_is_never_summed_into_a_fabricated_value`,
  `::test_malformed_cardinal_is_not_half_converted`,
  `::test_well_formed_cardinals_still_convert`, benchmark case
  `num_malformed_cardinal`.

## 7. Dictated blood pressure never reached charted form

- **FILE**: `speechmatics_test/nursing_text.py`
- **BUG**: `فشار خون صد و چهل روی هشتاد و پنج` produced `140 روی 85`
  instead of the charted `140/85`.
- **FIX**: New `normalize_spoken_ratios()`, firing only with 1–3 digit
  numerals on **both** sides of `روی` / `بر` / `over`.
- **WHY SAFE**: `روی` is an ordinary preposition; requiring numerals on both
  sides leaves `پانسمان روی زخم` ("dressing on the wound") untouched. Both
  values are preserved exactly.
- **TEST**: `tests/test_nursing_text.py::test_spoken_blood_pressure_becomes_a_charted_ratio`,
  `::test_ratio_word_between_non_numbers_is_left_alone`.

## 8. Vocabulary export silently weakened ASR recognition

- **FILE**: `speechmatics_test/matcher.py`
- **BUG**: `_build_additional_vocab` copied only the declared `sounds_like`
  field, dropping every spoken form — `BP` lost `فشار خون`, `بی پی`. The
  hand-maintained artifact had contained them. Nothing failed, because nothing
  asserted on it. The committed artifact was also **stale**: it advertised
  `hour` / `o'clock` after those terms were removed, and was missing
  `Magnesium` / `g`.
- **FIX**: Hints are now derived from the term's own forms plus declared
  `sounds_like`, filtered to pronounceable strings (no digits or punctuation —
  `B/P` is written-only), never repeating the canonical.
- **WHY SAFE**: Entry count is unchanged (136); only the hint lists grew.
- **TEST**: `tests/test_benchmark.py::test_vocabulary_keeps_spoken_forms_as_pronunciation_hints`,
  `::test_vocabulary_hints_are_pronounceable`,
  `::test_vocabulary_has_no_stale_entries`.

## 9. Two test assertions were impossible to satisfy

- **FILE**: `tests/test_app.py`, `tests/test_dictionary.py`
- **BUG**: Both asserted `len(vocab) < 100` while the shipped dictionary
  exported 136–146 entries. The literal was **unsatisfiable** — the tests only
  ever "passed" because the dictionary raised first (bug 1).
- **FIX**: Replaced with `< 300` plus a ratio invariant
  (`< 0.25 × term count`) and a comment explaining the history.
- **WHY SAFE**: Still enforces "bounded curated subset, not a dump", which is
  the invariant that actually matters.

## 10. Six of my own benchmark fixtures were wrong

Corrected in `benchmark/dataset.py` with `note=` justifications rather than
bending the code to match bad expectations. Examples: `اشباع` alone is not an
SpO2 alias (fixture now uses `اشباع اکسیژن`); `علائم حیاتی` → `vital signs`
is correct; the `pain score:` colon is correct charted form.

## 11. Tooling consolidated

- **FILE**: `scripts/swiftmedics_tools.py` (new) and all `.ps1` / `.sh` files
- **BUG**: One script per task, each with its own venv/pip/.env bootstrap that
  had drifted apart.
- **FIX**: All logic in one Python tool with 10 subcommands. Legacy scripts
  became thin forwarders — kept, not deleted.
- **TEST**: `tests/test_scripts.py` asserts every legacy entry point exists,
  forwards to the right subcommand, and contains no `pip install` or
  `-m venv` of its own.

## 12. New benchmark framework

- **FILES**: `benchmark/dataset.py`, `benchmark/run_benchmark.py`
- 107 frozen fixtures across 5 stages, reported **separately** so a formatting
  fix is never presented as a terminology gain.
- Anti-contamination guards: a reference may not be a truncation of the spoken
  text; every declared term/number must appear in its own reference; digits
  may not vanish. Legitimate compressions (`ده و نیم` → `10:30`) are listed
  explicitly with justification, and a meta-test verifies each listed case
  really does compress.
- Determinism checks: identical output across repeated runs and across freshly
  built matcher instances.
- `benchmark/README.md` is generated from the measured run — nothing hardcoded.

---

# Session 1 — Earlier fixes

Format: **FILE / BUG / FIX / WHY SAFE / TEST ADDED**.

## 1. Cross-segment buffering held complete, non-extendable phrases forever

- **FILE**: `speechmatics_test/matcher.py`, `speechmatics_test/medical_layer.py`, `app.py`
- **BUG**: `FinalStreamCanonicalizer._safe_cut` used `is_rule_token_prefix`,
  which returns `True` for a token sequence that is *itself* a complete rule
  with no longer sibling (e.g. `"iv"`). The accumulator held such phrases
  forever, waiting for a continuation no rule defines, delaying injection.
- **FIX**: Added `is_strict_rule_token_prefix` (proper prefixes only). When the
  entire remaining buffer can only match as a complete, non-extendable phrase
  it is emitted immediately. Mid-buffer positions keep the original check, so
  compounds like `رایت لانگ` vs `لانگ ساوندز` are still held together.
- **WHY SAFE**: Purely additive API; `is_rule_token_prefix` is untouched.
- **TEST**: `tests/test_app.py::test_accumulator_emits_standalone_complete_rule_without_delay`,
  `::test_accumulator_still_holds_genuinely_ambiguous_compound_tail`,
  `tests/test_matcher.py::test_is_strict_rule_token_prefix_excludes_standalone_complete_rules`.

## 2. Unsafe canonicalization of ambiguous short forms

- **FILE**: `speechmatics_test/matcher.py`
- **BUG**: `or`, `p`, `now`, `diff`, `ac`, `pc`, `hs`, `od` matched
  case-insensitively, so "The patient is stable now" became "...immediately".
- **FIX**: Added `_AMBIGUOUS_SHORT_FORMS` and an uppercase-evidence guard,
  wired into both the automaton and reference scanners so they agree.
- **WHY SAFE**: Only those forms are restricted; Persian and fully-spelled
  aliases are different match forms.
- **TEST**: `tests/test_matcher.py::test_ambiguous_short_forms_require_uppercase_evidence`
  and two siblings.

## 3. `number_accuracy` could not flag fabricated numbers

- **FILE**: `speechmatics_test/evaluation.py`
- **BUG**: Recall-only, so reference `"20 mg"` vs hypothesis `"20 mg 50 mg"`
  (a fabricated dose) scored a perfect `1.0`.
- **FIX**: Added `number_precision`, `number_recall`, `number_f1`.
  `number_accuracy` keeps its exact semantics for API stability.
- **WHY SAFE**: Purely additive; no existing field changed value.
- **TEST**: `tests/test_core.py::test_number_precision_recall_f1_flag_extra_hypothesis_numbers`
  and one sibling.

## 4. Clipboard left corrupted on a partial set failure

- **FILE**: `injector.py`
- **BUG**: `EmptyClipboard()` could succeed (destroying the user's content) and
  a later step in the same call fail, returning `False`. The restoration block
  never ran, permanently losing the original clipboard.
- **FIX**: Added `_clipboard_touched`, set the instant `EmptyClipboard()`
  succeeds. Restoration now triggers on every path where content was destroyed.
- **WHY SAFE**: Only widens the existing `finally` trigger; the Win32 call
  sequence, retry logic and locking are unchanged.
- **TEST**: `tests/test_injector.py::test_paste_windows_restores_clipboard_after_partial_set_failure`.
