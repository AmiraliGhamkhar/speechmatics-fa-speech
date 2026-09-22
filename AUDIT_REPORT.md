# SwiftMedics — Engineering Audit & Fix Session Report

Repository: `AmiraliGhamkhar/speechmatics-fa-speech`
Branch: `arena/01a0ca6b-speechmatics-fa-speech` (commit `08a9164`, from `8a4d47c`)
Audited on Linux / Python 3.11.2. **No Windows desktop was available — see section F.**

---

## A. Bugs found

Severity: **C**ritical / **H**igh / **M**edium / **L**ow / **D**oc.
"Fixed" = code changed this session. Ordered by severity.

| # | Sev | File | Defect | Consequence | Status |
|---|-----|------|--------|-------------|--------|
| 1 | **C** | `app.py` → `injector.py` | `arm_target()` was called inside `with recorder:` just before `stt.run(...)`, i.e. while the **SwiftMedics console was still the foreground window**. The console HWND became the armed target. | The reported production failure: the user clicks Word/EMR, focus is no longer the console, and `_focus_guard_ok()` rejects **every** paste → `auto-injected 0/34 finalized segments`. Automatic injection was completely non-functional. | **Fixed** |
| 2 | **H** | `injector.py` | The guard had only two states. `_armed_hwnd is None` was interpreted as "guard inactive → allow paste". There was no "guard requested but no target acquired" state. | Fail-**open**. If arming failed (non-Windows, no foreground API, or the new wait timing out), medical text would be pasted into whatever window happened to hold focus. The unsafe default for a clinical tool. | **Fixed** |
| 3 | **H** | `app.py` `InjectionWorker._run` | No `try/except` anywhere in the worker loop. Reproduced twice: a raise from `paste_text` **or** from the `on_result` overlay callback terminates the daemon thread. | Silent catastrophic loss. After the first exception the thread is dead; every later finalized segment is never pasted **and never recorded**, so `injection.segments` under-reports and the console shows no failure at all. | **Fixed** |
| 4 | **M** | `app.py` `InjectionWorker.shutdown` | Jobs still in the queue when the worker died or the join timed out were discarded with no record. | The report under-counts lost segments — a clinician could believe text was delivered when it was not. | **Fixed** |
| 5 | **M** | `injector.py` `_open_clipboard` | `OpenClipboard(foreground_hwnd)` passed **another process's** window handle, making the target application the clipboard *owner*. | `EmptyClipboard()` then destroys the clipboard on behalf of the target app, which receives `WM_DESTROYCLIPBOARD`/render requests for data it never produced. Ill-defined behaviour, app-dependent. | **Fixed** |
| 6 | **M** | `injector.py` `_paste_windows` | Only `CF_UNICODETEXT` is captured for restore. A non-text clipboard (image, files, Excel range) was overwritten with no warning and no restore. | The clinician's copied X-ray/file selection vanishes silently. | **Fixed** (warn once; restore still not attempted — see E) |
| 7 | **M** | `speechmatics_test/nursing_text.py` | `_normalize_spoken_times` read a compound hour's first word only, leaving the remainder to match as a *second* time. A dead `combined = …; pass` block showed the case was known but unhandled. | **Fabricated clinical timestamp**: `ساعت بیست و یک و سی دقیقه` (21:30) → `ساعت 20 و 01:30`. Violates the "never invent numbers" rule. Escalated from the LOW "dead code" finding once behaviour was probed. | **Fixed** |
| 8 | **L** | `app.py` | The microphone-init failure path `return 1`s after the injection worker thread was already started. | Leaked non-daemon-drained worker thread on a common error path. | **Fixed** |
| 9 | **L** | `scripts/swiftmedics_tools.py:28` | Unused `import os`. | None (hygiene). | **Fixed** |
| 10 | **D** | `benchmark/results_current.json`, `benchmark/README.md` | Provenance cited commit `266508fd44c3d16295e13e9358109d8acb7c9dde`; `git cat-file` confirms **no such object exists** in this repository. | Unreproducible benchmark claims. | **Fixed** (regenerated) |
| 11 | **D** | `README.md` | Documented the buggy flow ("arms the currently focused window at startup") as correct behaviour. | Documentation actively described the bug as a feature. | **Fixed** |

### Audited and found correct (no change made)

FIFO ordering and no-duplicate guarantees of `InjectionWorker`; raw/normalized/canonical
stage distinctness and `canonical == injected` parity; `FinalStreamCanonicalizer`
streaming accumulation (13 split/unsplit sequences — joined output always equals
`acc.canonical_text`); number/time safety (`سی و شش و هفت` stays unparsed, `ساعت 10 و 30
دقیقه 90` does not absorb the `90`, warning emitted); idempotency `f(f(x)) == f(x)` across
all 107 fixtures × 5 pipeline functions (0 violations); Speechmatics config vs
`speechmatics-rt==1.1.1` (every field used exists; chunk 6400 B = 200 ms @ 16 kHz);
final-message parsing (metadata failure does not discard the transcript); matcher
abbreviation collisions (IV/CT/PT/IT/US/cold — only uppercase `US`/`IT` fire, which is the
documented uppercase-evidence rule); dictionary schema (968 terms, 0 duplicate
ids/canonicals); vocab in sync (136 entries); BiDi marks confined to the injection
boundary; audio in-memory only; modifier-key handling; double focus re-validation around
the Ctrl+V keystroke.

---

## B. Files changed

| File | Change |
|------|--------|
| `injector.py` | **+`await_target()`** (transition-based acquisition), **+`focus_guard_active`**, `_focus_guard_ok()` fails closed, `arm_target()` engages the guard even on failure, `_open_clipboard()` uses `NULL`, **+`_warn_nontext_clipboard_once()`**, `CountClipboardFormats` prototype. |
| `app.py` | **+`acquire_injection_target()`** + `TARGET_SELECTION_TIMEOUT`; call site replaces `injector.arm_target()`; `InjectionWorker._run` exception-guarded; **+`_record_undrained()`**; mic-failure path shuts the worker down; report gains `injection.focus_guard` / `injection.target_acquired`; startup banner shows guard state. |
| `speechmatics_test/nursing_text.py` | `_normalize_spoken_times`: compound-hour candidates tried longest-first via a new local `match_minute()`; removes the dead `combined`/`pass` block. |
| `scripts/swiftmedics_tools.py` | Removed unused `os` import. |
| `tests/test_injector.py` | +10 tests (7 target acquisition, 3 clipboard). |
| `tests/test_e2e.py` | +4 app-level tests; `TargetSelectingInjector` / `FocusGuardedInjector` fakes now model the real focus-guard contract. |
| `tests/test_app.py` | +3 worker-resilience tests. |
| `tests/test_nursing_text.py` | +4 compound-hour tests. |
| `README.md` | Startup flow, focus-guard section (incl. fail-closed + top-level-window limitation), `--no-focus-guard` help, test count. |
| `CHANGELOG_FIX_SESSION.md` | Session 5 entry. |
| `benchmark/README.md`, `benchmark/results_current.json` | Regenerated with real provenance. |

Architecture preserved: no new dependency, no framework, no hotkey, no global keyboard
hook, no UI Automation, no LLM. `FinalStreamCanonicalizer → InjectionWorker → injector →
target window` is unchanged. CLI flags and output formats are unchanged (the report gains
two additive keys).

---

## C. Tests and benchmark

### Tests

| | Command | Result |
|---|---|---|
| Before | `.venv/bin/python -m pytest -q` | **538 passed** in 9.00 s |
| After | `.venv/bin/python -m pytest -q` | **561 passed** in 8.77 s |
| Targeted | `pytest tests/test_injector.py tests/test_realtime.py tests/test_matcher.py tests/test_nursing_text.py tests/test_app.py tests/test_e2e.py -q` | **347 passed** |

23 tests added, 0 removed, 0 pre-existing test weakened. `compileall` clean;
`app.py --help` works; `check-vocab` OK; `audit-dictionary` rc=0.

**The new tests were verified to actually catch the bug**: with `app.py` reverted to
`injector.arm_target()` and everything else in place, the 4 new e2e tests fail
(`test_startup_arms_the_user_selected_field_not_the_console`,
`test_first_finalized_segment_is_injected_automatically`,
`test_focus_change_during_dictation_is_still_refused`,
`test_no_target_selected_does_not_paste_anywhere`). They pass with the fix.

### Benchmark

`.venv/bin/python scripts/swiftmedics_tools.py benchmark` (equivalent of
`.\scripts\run_benchmark.ps1`).

| Metric | Before | After |
|---|---|---|
| Whole-text exact match | 107/107 (100.0%) | **107/107 (100.0%)** |
| Terminology F1 | 1.0 | 1.0 |
| Number F1 / false-number rate | 1.0 / 0.0 | 1.0 / 0.0 |
| Dropped-number rate | 0.0 | 0.0 |
| Warning behaviour OK | 107 | 107 |
| BiDi clean | true | true |
| Per stage | 1/1, 40/40, 37/37, 12/12, 17/17 | identical |
| Streaming variants | 397/405 (98.02%) | **397/405 (98.02%)** |
| Streaming divergences | 8 ids | **identical 8 ids** |

No regression; the aggregates and streaming blocks compare byte-identical. Timing numbers
differ run-to-run (sandbox noise) and carry no claim.

> **These figures measure deterministic post-processing only — they are not Speechmatics
> ASR accuracy.** The benchmark feeds written spoken-form text through the matcher and
> nursing normalizer; no audio and no speech recognition are involved.

---

## D. Injection verification

What the fix guarantees, and how each claim is evidenced:

1. **The app never arms its own console.** `await_target()` records the foreground
   window at call time and only accepts a *different* window. Evidenced by
   `test_await_target_never_arms_the_application_console` and
   `test_startup_arms_the_user_selected_field_not_the_console`, which assert
   `armed_target != CONSOLE`.
2. **The target is acquired before the first finalized segment can be injected.**
   `acquire_injection_target()` is awaited before `stt.run(...)`, so no final can be
   produced first. Evidenced by `test_first_finalized_segment_is_injected_automatically`
   (the first segment is delivered, not refused).
3. **Every finalized segment is auto-injected, in order, with no user action.** The e2e
   run asserts `pasted == ["بیمار در CCU است ", "HTN دارد ", "وضعیت پایدار است "]` and
   `auto-injected 3/3 finalized segments.` No hotkey, countdown, Enter or Ctrl+V exists in
   the path.
4. **The focus guard still protects.** Switching to an unrelated application after arming
   is refused with a clear message and reported as a failure
   (`test_focus_change_during_dictation_is_still_refused`: 1 delivered, 2 refused,
   `AUTO-INJECTION FAILED` printed, `auto-injected 1/3`). The guard was **not** disabled
   and `--no-focus-guard` is still opt-in (`action="store_true"`).
5. **No target selected ⇒ nothing is pasted anywhere.** `test_no_target_selected_never_pastes`
   and `test_no_target_selected_does_not_paste_anywhere` assert zero clipboard writes and
   zero keystrokes, with `injection.target_acquired: false` in the report.
6. **A failure is never silently recorded as success.** A refused or raising paste yields
   `success: False` (+ `error`), prints immediately, and does not stop the stream.
7. **The clipboard is restored.** An end-to-end mocked sequence (console → editor → 3
   pastes → alt-tab → refusal) ends with the clipboard back to the user's original text
   and exactly 3 keystrokes sent.

**What was mocked:** `GetForegroundWindow`/window info, `OpenClipboard`,
`CountClipboardFormats`, clipboard get/set, and the Ctrl+V `SendInput` keystroke. The real
`user32`/`kernel32` calls were **not** executed — they cannot be on Linux.

---

## E. Remaining limitations

1. **Top-level window granularity only.** The armed handle is the foreground *top-level*
   window. Moving between controls inside the same application (two fields of one EMR
   form) is allowed and pastes at the current caret. SwiftMedics does **not** track focus
   at control level and deliberately does not use UI Automation, accessibility APIs, or
   mouse hooks. Now documented in the README rather than implied away.
2. **Non-text clipboard content is still not restored.** Only `CF_UNICODETEXT` is
   captured. A copied image/file/Excel range is replaced by the transcript; the user is
   now warned once per session. Restoring arbitrary formats would require a multi-format
   clipboard subsystem, which the brief rules out.
3. **Target selection has a 60 s timeout.** If the clinician does not click a field within
   that window, injection is disabled for the session (fail-closed) and the transcript
   must be copied from the console or the report.
4. **Re-arming after an intentional app switch is not automatic.** Once the target is
   armed, moving to a different top-level application produces refusals until the process
   is restarted. This is the safety behaviour the brief mandates, not an oversight.
5. **8 streaming benchmark divergences remain** (`num_percent`, `unit_separated_en`,
   `unit_mmhg_en`, `gram_repeated_word_plain`, `gram_echoed_verb`,
   `gram_echoed_inflection`, `gram_vital_spo2`, `para_labs`). All are formatting joins
   across an already-emitted boundary — text already injected cannot be retroactively
   rewritten. No clinical content is fabricated or dropped. Unchanged by this session.
6. **179 conflicting alias forms** in the dictionary audit (pre-existing, rc=0, reported
   as informational). Not touched — the brief forbids mass dictionary rewriting without
   evidence.
7. **Low-confidence clinical entities**: no policy change was made. The existing
   deterministic behaviour (surface the text, warn on unbound/ambiguous numerals, never
   reinterpret) was audited as correct; adding a confidence model would require ML, which
   is excluded.

---

## F. Evidence statement — no unverified success claims

- The **561 passing tests** and the **107/107 / 397-405 benchmark** figures are real
  outputs of commands run in this session, reproduced above verbatim. Nothing is
  extrapolated.
- **The Windows injection fix has NOT been verified on real Windows hardware.** This
  session ran on Linux (Python 3.11.2). `user32`/`kernel32` were never called; every Win32
  interaction in the tests is a mock. `ctypes.wintypes` imports cleanly here, which makes
  the structures unit-testable, but that is not proof of runtime behaviour.
- **Interactive verification on a real Windows desktop remains required** before relying
  on this in clinical use. The specific sequence to run:
  1. Start `python app.py --language fa --save-report` and leave the console focused.
  2. Confirm the prompt "Click the text field where the transcript must go" appears.
  3. Click into Word (or the EMR field). Confirm `Target armed (hwnd=…)` prints and that
     the handle is **not** the console's.
  4. Dictate several segments; confirm each finalized segment appears at the caret with no
     keypress, in spoken order.
  5. Alt-tab to a browser and dictate; confirm the paste is **refused** with
     `focus changed - paste skipped…` and counted as failed.
  6. Stop with Ctrl+C; confirm `auto-injected N/N` matches what landed in the document and
     that `injection.target_acquired` is `true` in the report.
  7. Repeat once without clicking any field; confirm nothing is pasted anywhere.
- The claims in section D are statements about **tested behaviour under mocked Win32
  APIs**. They establish that the logic is correct and that the old bug is caught by
  regression tests; they do not establish that the ctypes layer behaves identically on a
  live Windows desktop.
