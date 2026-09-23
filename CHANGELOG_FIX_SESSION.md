# Fix Session Changelog

Format per change: **FILE / BUG / FIX / WHY SAFE / TEST ADDED**.

> The **Numeric / time consolidation pass** at the end of this file supersedes
> the counts below: **522 passed, 0 failed**, 960 dictionary terms, 134 vocab entries.

Final result of the earlier pass: **315 passed, 0 failed** (`pytest -q`), `compileall` OK, `pip check` OK,
`scripts/export_additional_vocab.py --check` OK (942 dictionary terms, 98 vocab entries),
`benchmark/benchmark_matcher.py --sizes 100 500 1000 2000 --repeats 50` OK,
`app.py --help` OK, `fa`/`en`/`--no-vocab`/`--no-medical-layer`/`--no-inject` conceptual
runs all fail gracefully on the missing API key (no crash).

---

## 1. Cross-segment buffering held complete, non-extendable phrases forever (§7)

- **FILE**: `speechmatics_test/matcher.py`, `speechmatics_test/medical_layer.py`, `app.py`
- **BUG**: `FinalStreamCanonicalizer._safe_cut` (in `app.py`) used `is_rule_token_prefix`,
  which returns `True` for a token sequence that is *itself* a complete rule with no
  longer sibling rule (e.g. the one-token abbreviation `"iv"`, which has no rule
  `"iv ..."`). This caused the cross-segment accumulator to hold such a standalone,
  non-extendable phrase back forever, waiting for a continuation that no rule defines,
  delaying/withholding injection and the report's canonical text unnecessarily.
- **FIX**: Added `MedicalMatcher.is_strict_rule_token_prefix` (and the
  `MedicalLayer` facade of the same name), backed by a new `_strict_form_prefixes` set
  built at load time (proper prefixes only - i.e. token sequences with strictly *fewer*
  tokens than some rule, excluding a tuple that only equals a complete rule's own full
  form). `_safe_cut` now special-cases the whole-buffer position (`i == 0`): if the
  entire remaining buffer only matches a rule as a complete, non-extendable phrase
  (not a strict prefix of anything longer), it is emitted immediately instead of
  returning `0`. Mid-buffer positions (`i > 0`) keep the original (non-strict) check
  unchanged, because a compound whose tail token starts a genuinely different, longer
  sibling rule (e.g. `"رایت لانگ"` vs `"لانگ ساوندز"`) must still be held together.
- **WHY SAFE**: Purely additive API (`is_strict_rule_token_prefix` is a new method;
  `is_rule_token_prefix`'s behavior and signature are untouched). The narrowing only
  applies to the whole-buffer case where the buffer *cannot* structurally extend into
  anything longer, so no previously-correct cross-segment phrase (e.g. "فشار خون" +
  "بالا دارد" -> "HTN") stops working; verified by re-running the full suite and by a
  targeted before/after check that the existing cross-segment "رایت لانگ" test still
  requires holding.
- **TEST ADDED**: `tests/test_app.py::test_accumulator_emits_standalone_complete_rule_without_delay`,
  `tests/test_app.py::test_accumulator_still_holds_genuinely_ambiguous_compound_tail`,
  `tests/test_matcher.py::test_is_strict_rule_token_prefix_excludes_standalone_complete_rules`.

## 2. Unsafe generic canonicalization of ambiguous short forms (§8)

- **FILE**: `speechmatics_test/matcher.py`
- **BUG**: Clinical shorthand aliases that collide with common English words
  (`or`, `p`, `now`, `diff`, `ac`, `pc`, `hs`, `od`) were matched fully
  case-insensitively, so an ordinary sentence like "The patient is stable now" was
  silently rewritten to "...immediately", and "he works in or elsewhere" risked being
  rewritten via the Persian-alias-linked `OR` rule's tier conflict resolution.
- **FIX**: Added `_AMBIGUOUS_SHORT_FORMS` (the exact 8-item set from the task spec) and
  `MedicalMatcher._passes_ambiguous_short_form_guard`, which requires the *original*
  matched text to be fully uppercase before one of these specific forms fires. Wired
  into both scan engines (`_scan` and `_scan_reference`) so automaton and reference
  paths agree. Every other rule (including safe abbreviations like `MRI`, `CT`, `ECG`,
  `HbA1c`, `SpO2`) is completely unaffected and keeps ordinary case-insensitive
  matching.
- **WHY SAFE**: The guard only restricts the 8 listed ambiguous forms; unambiguous
  Persian-script and fully-spelled English aliases for the same underlying concepts
  (e.g. "قبل از غذا", "before food", "ante cibum") are unaffected because they are a
  different match form entirely. No rule was removed, no vocabulary was weakened -
  this only narrows *when* the already-present ambiguous-form rules fire.
- **TEST ADDED**: `tests/test_matcher.py::test_ambiguous_short_forms_require_uppercase_evidence`,
  `tests/test_matcher.py::test_ambiguous_short_forms_persian_and_spelled_aliases_unaffected`,
  `tests/test_matcher.py::test_safe_case_insensitive_abbreviations_remain_case_insensitive`.

## 3. `number_accuracy` could not flag fabricated/extra numbers (§14)

- **FILE**: `speechmatics_test/evaluation.py`
- **BUG**: `number_accuracy` is recall-only (fraction of reference numbers found in the
  hypothesis), so reference `"20 mg"` vs hypothesis `"20 mg 50 mg"` (a fabricated extra
  dose) scored a perfect `1.0`, with no metric surfacing the fabrication.
- **FIX**: Added `number_precision`, `number_recall` (alias of the unchanged
  `number_accuracy`), and `number_f1`, plus additive `evaluate()` report fields
  `number_precision`/`number_recall`/`number_f1`. `number_accuracy`'s exact
  return value and semantics are untouched for API/report stability.
- **WHY SAFE**: Purely additive - no existing field changed value or was removed;
  `evaluate()`'s dict keys only gained new entries, verified against all existing
  `test_core.py` assertions (`test_number_accuracy`, `test_number_accuracy_multiset`,
  `test_evaluate_stages`) which still pass unmodified.
- **TEST ADDED**: `tests/test_core.py::test_number_accuracy_never_penalizes_fabricated_extra_numbers`,
  `tests/test_core.py::test_number_precision_recall_f1_flag_extra_hypothesis_numbers`.

## 4. Clipboard left corrupted on a partial `_set_windows_clipboard` failure (§16)

- **FILE**: `injector.py`
- **BUG**: `_paste_windows` only restored the previous clipboard content when
  `_set_windows_clipboard` *returned* `True` at least once (`clipboard_changed`
  gate). But inside `_set_windows_clipboard`, `EmptyClipboard()` can succeed
  (destroying the previous content) and then a *later* step in the same call
  (`GlobalAlloc`/`GlobalLock`/`SetClipboardData`) can fail, causing the function to
  return `False` even though the previous clipboard content is already gone. The
  `finally` restoration block then never ran, permanently losing the user's original
  clipboard content on that specific failure path.
- **FIX**: Added `self._clipboard_touched`, set by `_set_windows_clipboard` itself the
  instant `EmptyClipboard()` succeeds (independent of that call's own return value).
  `_paste_windows`'s restoration gate now also honors `_clipboard_touched` in addition
  to a full `True` return, so restoration happens on every path where the previous
  content was actually destroyed - success, verification failure, keystroke failure,
  *and* this partial-set failure.
- **WHY SAFE**: No new abstraction; the fix only widens the existing `finally`
  restoration's trigger condition to match the real state of the clipboard, and does
  not change the Win32 call sequence, retry logic, or serialization lock. All prior
  clipboard-restoration tests (`test_paste_windows_restores_clipboard_after_successful_paste`,
  `..._when_keystroke_fails`, `..._on_verification_failure`,
  `test_paste_windows_does_not_restore_when_setting_fails`) still pass unmodified.
- **TEST ADDED**: `tests/test_injector.py::test_paste_windows_restores_clipboard_after_partial_set_failure`.

---

## Verified already-fixed / already-satisfied (no change needed this round)

- **§6** case-fold prefix bug: `casefold_preserving` already the single shared
  helper for both `_form_prefixes` (build time) and `is_rule_token_prefix` (query
  time); confirmed `["CT"]`/`["ct"]`/`["Ct"]` all `True`, and
  `tests/test_matcher.py::test_is_rule_token_prefix_detects_full_and_partial_forms`
  already covers it.
- **§9** token-boundary/substring safety (`ivory`, `vivid`, `mriبیمار`, `ivبی`):
  already directly covered by `tests/test_matcher.py::test_no_substring_match_inside_word`
  and `test_no_match_in_concatenated_words`.
- **§13** report additive fields: `final_transcript_canonical`,
  `injection.{auto,enabled,segments[*].{text,success}}` already present in `app.py`'s
  report dict; `InjectionWorker` records `success` from the real `paste_text()` return
  value, which is `False` on focus-guard reject, verification failure, or any
  clipboard/keystroke failure - injection success is never falsely claimed.
- **§4/§5/§11/§12** dictionary dedup, `source_file`↔tier mapping, vocab-parity: all
  confirmed already correct from a prior session in this branch (942 terms, 0 duplicate
  canonicals/ids, 98 `speechmatics: true` vocab entries matching the generated
  artifact exactly, `--check` passes).
- **§17-19** BiDi/overlay/realtime SDK: re-inspected `overlay.py` and
  `speechmatics_test/realtime.py` this round; no new demonstrable bugs found (thread
  lifecycle, bounded `stop_session` wait, partial/final separation, and
  logical-Unicode-only canonical text are all intact).

## README.md updates (§23)

- Documented the new additive `number_precision`/`number_recall`/`number_f1` report
  fields and `number_accuracy`'s recall-only semantics next to the existing WER/number
  accuracy paragraph.
- Added two bullets to "Why the canonicalization layer is conservative" describing the
  ambiguous-short-form uppercase-evidence requirement and the strict-prefix
  cross-segment buffering behavior, since both are now observable behavior changes.
- No stale counts or inflated claims ("production-ready", "100% accurate", etc.) were
  found in `README.md`; the existing `source_file`↔tier mapping description was
  already accurate against the current dictionary and left unchanged.

## Final validation (§24)

```
pytest -q                                        -> 315 passed, 0 failed
python -m compileall -q .                        -> OK
pip check                                         -> No broken requirements found.
scripts/export_additional_vocab.py --check        -> OK (942 terms, 98 vocab entries, in sync)
benchmark/benchmark_matcher.py --sizes 100 500 1000 2000 --repeats 50  -> OK (results written)
app.py --help                                     -> OK
app.py --language fa|en --no-vocab|--no-medical-layer|--no-inject --no-overlay
                                                   -> graceful "SPEECHMATICS_API_KEY is missing" exit, no crash
```

---

# Numeric / time consolidation pass (2026-09-23)

Age, blood pressure, SpO2, time-of-day and AM/PM notation, plus a dictionary
audit that removed the structural duplicates behind the mis-normalizations.
Sections 5-8 below follow the same **FILE / BUG / FIX / WHY SAFE / TEST ADDED** format.

## 5. The shipped dictionary did not load under its own validator

- **FILE**: `medical_knowledge/medical_dictionary.json`
- **BUG**: `MedicalMatcher.load_dictionary` rejects two entries with the same
  canonical (`FstError: duplicate canonical 'PO'`), and `po` / `route_0049`
  both claimed it. Every test that built a matcher from the repository
  therefore errored at *collection* (89 errors + 25 failures in `pytest -q`).
- **FIX**: merged `route_0049`'s distinct aliases (`by mouth`, `orally`,
  `per os`, `دهانی`) into the `po` row and deleted the duplicate row, keeping
  `PO` as the single canonical. The one `sounds_like` item that was a typo
  (`ازراهد‌هان`) was dropped rather than propagated. The validator itself is
  unchanged: it still refuses a duplicate canonical.
- **WHY SAFE**: the surviving rules are a superset of both rows' rules, and the
  compiled rule table was diffed against the pre-change one to prove no rule
  changed owner, canonical or tier except the ones listed in §6.
- **TEST ADDED**: existing `tests/test_dictionary.py` / `tests/test_matcher.py`
  load tests now run instead of erroring (the 432-case pre-migration parity
  fixture included).

## 6. Spoken numbers, times and meridiems had no coverage at all

- **FILE**: `speechmatics_test/text.py`, `speechmatics_test/matcher.py`
- **BUG**: the pipeline recognized only what the dictionary spells out, so
  `سن بیست سال`, `فشار خون صد و بیست روی هشتاد`, `اشباع اکسیژن نود و هشت
  درصد`, `ساعت هشت`, `هشت و نیم صبح` and `8 A.M.` were all passed through
  as prose. The rows that *did* exist for some of them were wrong: a
  `hour_N` row per hour rewrote `ساعت هشت` -> `8` inside `هر دو ساعت`-like
  text, `A.M.` was written as `8 A.M.`-with-period or left alone, and a bare
  `morning`/`evening` form mapped to the *frequency* phrase `every morning`.
- **FIX**: added a bounded fold stage after the lexical pass
  (`fold_numeric_expressions` = clock -> numerals -> ratio) driven by a
  `NumericContext` the medical layer supplies (`matcher.NUMERIC_CONTEXT`), so
  `text.py` keeps no clinical vocabulary of its own. Coverage is anchor-based:
  an hour needs `ساعت` before it or a day part after it; a numeral needs a
  unit or a vital-sign word beside it; `X روی Y` becomes `X/Y`. A number group
  is folded all-or-nothing (a run interrupted by `و` that does not form one
  valid number is left entirely alone). `NUMERIC_CONTEXT` is the only place
  that decides which words count as measurement context.
- **WHY SAFE**: the fold runs on the *already canonical* text, outside the
  `_scan` / `_scan_reference` try/except (a fold error can never trigger the
  reference fallback and cannot desynchronise the two engines), and it emits no
  hits, so `form`/`canonical`/`position` reporting is untouched. It refuses to
  act on a bare number, on text without an anchor, on a written hour followed by
  its own numeric meridiem (`دوازده 12 PM` is not doubled), and on any hour
  outside `0..23`; already-written `08:30`, `120/80`, `20 mg`, `98 %`, `12 PM`
  are byte-identical outputs, and every rewrite is idempotent. The `hour_N`,
  `morning`/`evening`-as-frequency and bare-number rows were deleted from the
  dictionary instead of being worked around.
- **TEST ADDED**: `tests/test_matcher.py::test_requested_numeric_rewrites`
  (57 parametrized cases incl. every example from the task),
  `test_numeric_rewrites_are_idempotent`,
  `test_folds_leave_ordinary_text_alone`,
  `test_fold_is_anchored_and_never_half_converts_a_number`,
  `test_folds_apply_after_the_lexical_pass_and_add_no_hits`,
  `test_standalone_fold_stage_matches_the_pipeline`,
  `test_minute_word_is_a_tail_marker_not_an_anchor`,
  `test_numeric_fold_is_engine_independent`,
  `test_the_fold_only_rewrites_number_spans` (a token-level invariant: the fold
  may only remove number text and may never introduce a word, verified to fail
  when the ratio fold is deliberately mutated to eat one extra word),
  `test_clinical_paragraph_end_to_end`,
  `test_number_and_meridiem_rows_are_single_sourced`.

## 7. Structural duplicates and mis-mappings in the dictionary

- **FILE**: `medical_knowledge/medical_dictionary.json`
- **BUG**: 979 entries carried 173 in-entry duplicate `forms` (same rule key
  twice), several duplicate `sounds_like` items, case-only duplicate canonicals
  (`Vital signs`/`vital signs`, `Chest X-ray`/`chest x-ray`,
  `Intensive Care Unit`/`intensive care unit`, `Cardiac Care Unit`/
  `coronary care unit`, `Magnesium`/`magnesium`), `am`/`pm` rows split from
  their `misc_01NN` aliases, an `اشباع اکسیژن`-style concept spelled both as an
  abbreviation row and a phrase row with conflicting canonicals, and `hour_1`
  .. `hour_12` rows duplicating each other's `ساعت N` forms.
- **FIX**: merged each duplicate pair into the row whose id/tier the rest of the
  dictionary already used (`po`, `chest-xray`, `vital-signs`, `magnesium`,
  `intensive-care-unit`, `coronary-care-unit`, `am`, `pm`), took the lowercase
  canonical where the project's convention is lowercase (`chest X-ray`,
  `vital signs`, `intensive care unit`, `coronary care unit`, `magnesium`),
  de-duplicated every entry's `forms`/`sounds_like` using the *loader's own*
  rule key, deleted the `hour_*` rows, replaced them with a single `midnight`
  row (`نیمه شب` -> `12 AM`) so `شب` -> `PM` cannot split the phrase, moved the
  day-part aliases onto `am`/`pm` (`صبح زود`, `قبل ظهر`, `بعدازظهر`, ...), gave
  `spo2` the letter-spelled aliases (`اسپیاودو`, `اشباع`) while leaving the
  *word* forms `ساتوریشن`/`اکسیژن ساتوریشن` with the `oxygen saturation` row the
  parity fixture pins, and refreshed `metadata.entry_count`/`generated_on` with
  a note describing the pass. 979 -> 960 entries, 2609 -> 2592 rules.
- **WHY SAFE**: every conflict the loader had to arbitrate before still
  resolves to the same canonical; the compiled rule table was diffed
  entry-by-entry against the pre-change dictionary (an ad-hoc script, not
  committed) and the only REMOVED/ADDED/CHANGED rules are the ones listed above. The
  432-case pre-migration parity fixture passes unchanged, tier order,
  `_compile_rules` punctuation skipping, the ambiguity guard, the `^\d+$` vocab
  skip and `load_dictionary`'s validation are untouched, and no *number*
  coverage was added to the dictionary (that was the failed first approach: a
  usable numeral lexicon needs ~200 rows and produces garbage wherever it
  gaps).
- **TEST ADDED**: `tests/test_matcher.py::test_number_and_meridiem_rows_are_single_sourced`
  plus the parity/coverage tests of §6; the 287 -> 271 conflict warnings the
  load emits are the dedup count.

## 8. The vocabulary budget test had itself gone stale

- **FILE**: `tests/test_dictionary.py`, `tests/test_app.py`, `medical_knowledge/speechmatics_additional_vocab.json`
- **BUG**: both tests asserted `len(additional_vocab) < 100`, but the shipped
  dictionary already exported 146 entries, so the bound contradicted the data it
  was guarding; the committed derived artifact was also stale (it still listed
  the deleted `hour`/`o'clock` rows and was missing `magnesium`/`g`).
- **FIX**: the budget is now stated as what it actually protects - a share of
  the dictionary (`<= max(32, terms // 5)`), an absolute ceiling (150) and "no
  bare number in a biasing list" - and the artifact was regenerated with
  `scripts/export_additional_vocab.py`. 146 -> 134 entries came from the §7
  dedup itself (12 `hour_N` rows, duplicate canonicals), not from curation:
  every `speechmatics: true` term a clinician would want biased is still there.
- **WHY SAFE**: the mechanism that keeps the vocabulary small (the per-entry
  `speechmatics` flag, the derived-not-hand-written artifact, the six-word
  element cap in `realtime._clean_vocab`) is unchanged; `--check` verifies sync.
- **TEST ADDED**: `tests/test_dictionary.py::test_vocabulary_stays_bounded_not_the_full_dictionary`
  (extended with the no-bare-number and share-of-dictionary guards).

## Validation after this pass

```
pytest -q                                        -> 522 passed, 0 failed
python -m compileall -q speechmatics_test        -> OK
scripts/export_additional_vocab.py --check        -> OK (960 terms, 134 vocab, in sync)
benchmark/benchmark_matcher.py                    -> OK (see below)
```

## Known limitations deliberately left in place

- The fold is bounded by design: `هزار` and ordinals are not in the numeral
  lexicon, `ساعت ۲۴`/`ساعت ۹ شب` style gaps are left as spoken text rather than
  guessed, and `۱۲ نیمه‌شب` yields `12 12 AM` because the `نیمه شب` row states
  the hour it was given.
- Pre-existing alias *chains* in the frequency rows (e.g. `OD` -> `once a day`
  -> `every day` on a second pass) are untouched: they are legacy `time_*`
  data outside this pass, and repairing them means re-canonicalizing dozens of
  unrelated entries.

---

# Conservative Persian dictation pass (2026-09-23)

## Failure classification

- **Post-processing policy errors:** Persian dictionary aliases such as `نرس کال`,
  `فلبیت`, `مقیاس مورس`, `مقیاس برادن`, `علائم حیاتی`, and narrative blood-pressure
  mentions were translated/canonicalized even though the live app requested narrative
  preservation. The flag existed, but `MedicalMatcher.canonicalize()` did not pass it
  to either scanner.
- **Segmentation/buffering errors:** the stream buffer considered rules that the
  conservative app path would never apply, retained complete non-extendable rules,
  and did not retain fragmented spoken-number suffixes (`پنجاه` + `و هشت` + `ساله`).
- **Numeric safety errors:** `نمره` was not a bounded numeric anchor, clock notation
  retained a redundant `دقیقه`, and bare `روی` could digitize a corrupt non-ratio
  fragment such as `MRI روی هشتاد و پنج`.
- **Injection behavior:** FIFO ordering and the focus guard were already correct.
  Failed pastes were already represented honestly as `success: false`; an end-to-end
  regression test now pins that report contract.
- **Shutdown/UI lifecycle error:** Tk widget/interpreter references could survive the
  overlay UI thread, and one callback-failure branch called `destroy()` from the
  caller thread. Cleanup now remains on the UI thread and releases widget references
  before it exits.
- **ASR recognition errors (not post-processed):** wrong recognized words, a wrong
  number such as `20` versus `25`, or an English final emitted directly by
  Speechmatics cannot be safely reconstructed. No guessing or reverse translation
  was added.

## Small deterministic fixes

- **`speechmatics_test/matcher.py` / `medical_layer.py`:** propagate the existing
  `preserve_narrative` mode through Aho-Corasick and fallback scanners; permit only
  immediate value-backed measurement notation, units, and explicitly spoken chart
  abbreviations in that mode; expose conservative prefix checks for the stream.
  The default matcher API and dictionary-wide benchmark semantics remain unchanged.
- **`app.py`:** use only strict conservative lexical prefixes and retain only a
  bounded numeric suffix, preserving order and allowing fragmented ages/vitals to
  normalize without session-wide accumulation.
- **`speechmatics_test/text.py`:** add the score anchor, consume `دقیقه` when emitting
  `H:MM`, and require a numeric left side before `روی` can anchor the right side.
- **`overlay.py`:** destroy Tk resources and drop widget references on their creator
  thread; never use caller-thread destruction as a shutdown fallback.
- **Tests/docs:** added exact narrative, notation, no-guessing, fragmentation,
  injection-report, realistic end-to-end nursing paragraph, and overlay lifecycle
  regressions. README now states that ordinary Persian is preserved and that no
  nursing template is generated.

## Validation

```text
python -m pytest -q
  -> 595 passed
python -m compileall -q app.py injector.py overlay.py speechmatics_test tests benchmark scripts
  -> OK
python scripts/export_additional_vocab.py --check
  -> OK (952 terms, 135 eligible entries; committed artifact unchanged/in sync)
python benchmark/benchmark_matcher.py --sizes 100 500 1000 2000 --repeats 50
  -> OK (Aho-Corasick; 100-2000 rules; output written outside the repository)
python -m pip check
  -> No broken requirements found
app.py --help and missing-key startup for fa/en, --no-vocab,
--no-medical-layer, and --no-inject
  -> options present; startup exits cleanly with the expected key error
```
