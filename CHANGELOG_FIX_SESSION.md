# Fix Session Changelog

Format per change: **FILE / BUG / FIX / WHY SAFE / TEST ADDED**.
Final result: **315 passed, 0 failed** (`pytest -q`), `compileall` OK, `pip check` OK,
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
