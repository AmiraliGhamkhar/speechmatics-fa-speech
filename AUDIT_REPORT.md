# Engineering audit — backend reliability and Persian/English medical transcription

**Repository:** `AmiraliGhamkhar/speechmatics-fa-speech`
**Date:** 2026-09-29
**Baseline commit:** `f5a2b5842a392b4e1628acd6cab2d2f34fb1dfd5`
**Branch:** `arena/01a0ed04-speechmatics-fa-speech`

Architecture is unchanged. Speechmatics remains the ASR, post-processing remains
deterministic, and nothing was added that could infer, translate or clinically
reinterpret uncertain output. No LLM, no embeddings, no vector store, no
generative correction, no new runtime dependency. Every defect below was
**reproduced before it was fixed**, and every fix is pinned by a test that
`benchmark/verify_regressions.py` proves fails when the fix is removed.

---

## Summary

| Area | Verdict | Defects found | Fixed |
| --- | --- | --- | --- |
| 1. Realtime backend | Sound lifecycle; **server warnings were dropped** | 1 (D7) | 1 |
| 2. Persian numbers | Thousands/ratios/clocks correct; **decimals broken** | 3 (D3, D4, D5) | 3 |
| 3. Medical matcher | Engines agree; **canonical output was rewritable** | 2 (D1, D8) | 2 |
| 4. Speechmatics vocabulary | Bounded and clean; **4 pronunciations inert** | 1 (D6) | 1 |
| 5. Accuracy evaluation | Metrics honest; **two stages were merged** | 1 (harness gap) | 1 |
| 6. Testing | Strong baseline; gaps at the new fixes | — | +115 tests |
| 7. Safety | No fabrication, no BiDi, no audio persistence | 0 | — |
| 8. Engineering constraints | Met | — | — |
| Local setup | **Microphone open failure escaped as a traceback** | 1 (D2) | 1 |

Tests: **887 passed → 1002 passed**, 1 skipped, 0 failed.
Post-processing benchmark: exact **1.000** on both canonical paths, **53/53**
streaming == single-pass.
Loader warnings: **170 → 167** (three silent tier arbitrations resolved).

---

## 1. Realtime backend

### Verified sound (no change needed)

| Property | How it holds | Evidence |
| --- | --- | --- |
| Session lifecycle | `run()` refuses to start on a busy instance, calls `reset()` first, and sets `_running` before any await | `test_reusing_an_instance_starts_from_a_clean_result`, `test_reset_clears_state_and_refuses_while_running` |
| No stale state between sessions | `reset()` rebuilds `SessionResult`, so no partial, segment, word, warning or error survives | same as above |
| Bounded `send_audio` | `SEND_AUDIO_TIMEOUT = 10.0` wraps a websocket write the SDK leaves unbounded; a half-open connection cannot hang the app forever | `test_send_stall_is_bounded_and_preserves_the_transcript` |
| Bounded shutdown | `STOP_SESSION_TIMEOUT = 30.0` bounds the `EndOfTranscript` wait | `test_stalled_send_keeps_the_transcript_and_names_the_reason` |
| Original server error preserved | a teardown failure cannot overwrite `result.error`, and a send timeout cannot clobber a server error | `test_server_error_survives_a_failing_teardown`, `test_send_timeout_does_not_clobber_a_server_error` |
| Transcript never lost | a segment is recorded **before** its callback runs, so a failing UI/injection callback cannot cost the clinician text; network and SDK failures fall back to `stt.result` | `test_failing_callbacks_never_cost_the_transcript`, `test_callback_failure_does_not_stop_later_segments`, `test_audio_teardown_failure_keeps_the_transcript` |
| Clean termination | mid-stream failure and external cancellation were both probed directly: no orphaned task, no lost final | probe B / probe C, clean |
| SDK assumptions | the adapter's timeout and message-shape assumptions were checked against the installed `speechmatics-rt` source | read, not assumed |

### D7 — server warnings were dropped (fixed)

`run()` subscribed to `ERROR`, `ADD_PARTIAL_TRANSCRIPT` and `ADD_TRANSCRIPT` but
never to `ServerMessageType.WARNING`, and the SDK's own handler
(`_async_client.py:378`) only writes to its logger:

```python
def _on_warning(self, msg): self._logger.warning("Server warning: %s", msg.get("reason", "unknown"))
```

So the service's own statements about the session never reached the report. The
consequential ones:

- **`validation_warning`** — sent in-band *before* `RecognitionStarted` when the
  service rejects an `additional_vocab` entry. A pronunciation bias then
  silently never happened (this is how D6 stayed invisible).
- **`idle_timeout`**, **`duration_limit_exceeded`**, **`session_timeout`** —
  explain a transcript that stopped early with no recorded reason.
- **`add_audio_after_eos`** — audio sent into a dead session.

**Fix:** a `WARNING` handler records each *distinct* message in the new
`SessionResult.service_warnings`, surfaced in the report under the same key.

Deliberate choices:

- Kept **separate** from the existing `warnings` field, whose dataclass
  contract is "non-fatal *parse*/metadata problems". An operator triaging a
  short transcript must be able to tell "the provider complained" from "we
  mis-parsed a message".
- Duplicates are recorded once, so a repeating `idle_timeout` cannot bury the
  report.
- The event is resolved with `getattr(ServerMessageType, "WARNING", None)`
  rather than imported directly. **Observability must never cost a session:** on
  an SDK build without that member the adapter surfaces no warnings instead of
  raising inside `run()` and discarding the transcript. Pinned by
  `test_warning_subscription_degrades_when_the_sdk_has_no_warning`.
- Review-only: warnings never alter recognized text.

The SDK's `EventEmitter._handlers` is a `dict[event, set[Callable]]` and `emit()`
wraps every callback in `try/except`, so registering a second `WARNING` handler
adds to the SDK's own (both run) and a failing handler cannot break the session.

### D2 — microphone OPEN failure escaped as a raw traceback (fixed)

Only `MicrophoneRecorder.__init__` was inside the guarded step. But `__init__`
merely builds the PyAudio object — the input device is opened in `__enter__`,
which is where a busy, unplugged or permission-denied device actually fails.

Reproduced with a fake PortAudio raising `OSError(-9999, "Unanticipated host
error [ALSA capture open]")`:

```text
before:  RESULT: !! RAW TRACEBACK ESCAPED !!   (exit status from the interpreter)
after :  RESULT: clean return, code = 1
         [microphone error] OSError: [Errno -9999] Unanticipated host error ...
         A microphone is required for dictation. Check that a device is connected ...
         PyAudio instances created: 1 | terminated: 1
```

**Fix:** `recorder.__enter__()` moved inside the existing guard, and
`with recorder:` replaced by `with contextlib.closing(recorder):` so the stream
is not entered twice. `closing()` calls `close()` on unwind, which is exactly
what `MicrophoneRecorder.__exit__` delegates to — behaviourally identical, and
`close()` is already idempotent. `__enter__` releases PyAudio itself before
re-raising, so the failure path leaks nothing.

`microphone.py` itself needed no change: it already never writes audio to disk,
already releases the stream and PyAudio on every exit path, and already counts
`overflow_events` so silent PortAudio sample loss is visible in the report.

---

## 2. Persian numbers

### Verified sound (no change needed)

Thousands (`هزار`) were already folded as one multiplicative group, ratios are
still exactly two numbers, clock notation is untouched, and the conservative
rules hold: a numeral folds only next to an anchor, a group is all-or-nothing,
larger magnitudes (`میلیون`, `میلیارد`) extend the group without folding, a run
must descend by magnitude class, and `هزار بار گفتم` / `هزار سال پیش` stay
spoken.

### D4 — an explicit fraction denominator was never read (fixed)

A dictated decimal arrives in **three** shapes; only two were handled.

| Shape | Example | Value | Before | After |
| --- | --- | --- | --- | --- |
| implicit tenths | `یک و هشت` | 1.8 | `1.8` ✓ | `1.8` |
| spoken half | `سی و هشت و نیم` | 38.5 | `38.5` ✓ | `38.5` |
| **explicit denominator** | `سی و هفت و هشت دهم` | **37.8** | *refused, stayed spoken* | `37.8` |
| **explicit denominator** | `سی و هشت دهم` | **30.8** | **`Temp 38 دهم`** | `Temp 30.8` |
| **explicit denominator** | `صد و بیست و پنج دهم` | **120.5** | **`glucose 125 دهم`** | `glucose 120.5` |
| **explicit denominator** | `یازده و نه دهم` | **11.9** | **`11 و نه دهم`** | `11.9` |

`دمای بدن سی و هشت دهم` is *thirty and eight tenths*. The implicit-tenths rule
read `سی و هشت` as `38` and stranded the word that says "tenths" beside it, so
a dictated **hypothermic 30.8 °C** was written as a **febrile 38 °C** — the
single most clinically dangerous number defect found in this audit.

**Fix:** an explicit-denominator branch in `spoken_number_at`'s joiner path,
ordered ahead of the cardinal continuation and the implicit-tenths reading. When
the speaker named the fraction, the digits in front of it are cardinal.

**Why safe:** precedence applies only when a denominator word actually follows,
so `یک و هشت` is still 1.8 and `سی و هشت` is still 38. Hundredths keep their
leading zero (`سه و پنج صدم` → `3.05`, never `3.5`). An unanchored `هشت دهم`, a
lone `دهم`, and the ordinal prose `دهم ماه رمضان` ("the tenth of Ramadan") all
stay spoken — nothing is inferred that was not dictated.

### D3 — a group was half-converted when it ended in a denominator (fixed)

`_number_group_end`'s joiner branch called `spoken_number_at` without
`allow_single_unsafe=True` (unlike the numeral branch directly below it) and did
not extend over a trailing denominator, so a group could look complete while its
tail was outstanding. The fold then digitized only the head:

```text
دوز دو و نیم دهم   ->   دوز 2.5 دهم        (before)
دوز دو و نیم دهم   ->   دوز دو و نیم دهم   (after: refused whole)
```

A digit sitting next to the very word that says the digit is a fraction is both
an all-or-nothing violation and a stated value nobody dictated.

**Fix:** `allow_single_unsafe=True` in the joiner branch plus a
trailing-denominator extension in the group-end loop. The change only ever makes
the group *longer*, and a longer group is folded whole or refused whole.

**Anti-hallucination invariant added:** the output may never contain a digit
immediately followed by `دهم` / `صدم` / `هزارم`
(`test_no_digit_is_ever_left_beside_a_dangling_denominator`). That is the exact
signature of a half-converted group, and it is now a machine-checked property
rather than a comment.

### D5 — a multi-thousand value was cut at its joiner (fixed)

Streaming only. The unit walkback crossed adjacent `SPOKEN_NUMERALS` only, so it
stopped at the joiner `و` and at `هزار`. Reproduced at cut = 13 characters of
`دوز دو هزار و پانصد میلی گرم`:

```text
emitted: "دوز دو هزار و"  ->  "دوز 2000 و"
held   : "پانصد میلی گرم" ->  "500 mg"
injected: دوز 2000 و 500 mg      (one 2500 mg dose reported as TWO numbers, neither of them 2500)
```

`_numeric_tail_start` had the same shortened group, so a buffer ending in
`دو و نیم` or `... و هشت دهم` was not recognized as an open value at all.

**Fix (shared with D1):** one module-level `_NUMBER_GROUP_TOKENS` definition —
numerals, `هزار`, the joiner, explicit denominators, unfolded magnitudes, the
spoken half/quarter and the ratio connector, composed from `text`'s numeric
grammar and `NUMERIC_CONTEXT`'s domain vocabulary — now used by both the
walkback and the tail scan, instead of two independently shortened lists.

**Verified:** every WORD boundary of 22 clinical measurements, 2- and 3-way
splits — **400 fragmentations, 0 divergences** from the single-pass output.
Re-measured with the fix surgically removed: **88 of those 400 diverged**, e.g.
`('دوز','دو هزار و','پانصد میلی گرم')` → `دوز 2000 و 500 mg` instead of
`دوز 2500 mg`. Character-level splits inside a word are out of scope:
Speechmatics finalizes on word boundaries.

### Coverage now benchmarked

Blood pressure, SpO2, pulse, temperature (all three decimal shapes), age,
weight, percentages, doses (µg→g scale), thousands, lab analytes, named nursing
scales (Morse, Braden, GCS), clock times, ratios, ventilator settings,
intake/output, and ordinary non-clinical Persian prose.

---

## 3. Medical matcher

### Verified sound (no change needed)

| Property | Result |
| --- | --- |
| **Native / pure-Python / reference parity** | **0 mismatches** across 3 engines × **7,641** inputs × both scan modes = **45,846 pairwise comparisons**. Corpus: the 432-case migration fixture, every canonical, every rule form, 22 realistic mixed-language sentences, and 4,000 randomized token sequences (seed 20260929). |
| Degradation path | `_scan_reference` implements the identical priority scheme; an engine failure falls back to it and records a length + truncated SHA-256 digest instead of transcript content |
| Longest match / tier precedence | unchanged; claim rules are appended at the lowest tier with the highest `seq`, so they can never outrank a real rule |
| Boundary safety | punctuation, ZWNJ normalization, `I am` not matched, `10 am` → `10 AM` correct |
| ZWNJ | normalized on input; the one ZWNJ-bearing canonical (`پنی‌سیلین`) restores it deliberately |
| Narrative-preserving mode | **1,963** Persian-form rules audited: **1,774 blocked, 189 allowed**, and **0 of the 189 introduce a Persian word that was not already in the input**. Of the 189 allowed, 69 keep Persian context and 120 go fully Latin — every one substitutes a Latin abbreviation, a Latin spelling or a chart numeral *into* a Persian frame (`ام آر آی اسکن` → `MRI اسکن`, `آی وی لاین` → `IV لاین`, `سه بار در روز` → `3 بار در روز`). None rewrites a Persian word into a different Persian word. |
| Token preservation | Random segmentations through an identity canonicalizer: **0** character-preservation violations. The emission machinery is sound. |

### D8 — a canonical was rewritable by the matcher's own output (fixed)

The scan resolved ties by longest match, but a match that rewrote *nothing* was
**discarded** instead of **claiming** its span. Shorter rules were then free to
fire inside a phrase the dictionary had already declared correct.

Measured on the live narrative path by canonicalizing each distinct canonical
back through the matcher: of **935** canonicals, **46 were not fixed points and
41 of those were visibly corrupted** — a word duplicated, deleted or substituted.
(The 5 that were not visibly corrupted are the single-token expansions listed
below.) Persian-only inputs were unaffected, because the narrative guard blocks
Persian-form rules — which is why this went unnoticed in a Persian-first suite.

| Input (already canonical) | Before | After |
| --- | --- | --- |
| `blood pH 7.4` | **`blood past medical history 7.4`** | `blood pH 7.4` |
| `nasogastric tube in place` | **`nasogastric in place`** (word deleted) | unchanged |
| `chronic obstructive pulmonary disease` | **`chronic obstructive respiratory disease`** | unchanged |
| `pulmonary embolism` | **`respiratory embolism`** | unchanged |
| `MR angiography` | **`medical records angiography`** | unchanged |
| `CT pulmonary angiography` | **`CT respiratory angiography`** | unchanged |
| `STAT order` | **`immediately order`** | unchanged |
| `at bedtime` | `at at bedtime` | unchanged |
| `vitamin B12` | `vitamin vitamin B12` | unchanged |
| `PEG tube` | `PEG tube tube` | unchanged |
| `past surgical history` | `past past surgical history` | unchanged |
| `history of present illness` | `history of history of present illness` | unchanged |
| `estimated GFR 45` | `estimated estimated GFR 45` | unchanged |

A lab value renamed to a chart-section heading is the worst failure class this
layer can produce: the number is intact, so no numeric metric would ever flag it.

**Fix:** new public `at_risk_canonicals()` finds multi-token canonicals
containing another rule's form by token-aligned substring check; the loader
registers each as a self-mapping **claim rule**. Claims enter `_by_first` and
the complete-form maps but **no prefix set** — a canonical's leading token is
usually an ordinary English word (`at`, `past`, `blood`), so treating it as a
hold trigger would delay emission of ordinary prose for no gain. Both `_scan`
and `_scan_reference` copy a claimed span verbatim and resume after it.

Result: **46 → 5** non-fixed-points, re-measured the same way, and all 5 are
single-token abbreviation
expansions the dictionary performs on purpose (`CK-MB`, `HCO3`, `PLT`, `US`,
`nebulizer`). A single-token canonical cannot claim itself, so narrowing those
would be a policy change rather than a fix, and the ambiguous ones are already
guarded by `_AMBIGUOUS_SHORT_FORMS`.

An off-by-one found during the fix is itself pinned by a test: the substring
scan must reach `len(parts)` inclusive, because the embedded form is very often
the canonical's *last* token (`at bedtime` contains `bedtime`, `vitamin B12`
contains `B12`). A half-open range silently missed all of those.

### Two dictionary conflicts fixed at the source

Both were already being arbitrated silently by tier, and both destroyed
information rather than reformatting it:

| Form | Was mapped to | By | Now |
| --- | --- | --- | --- |
| `nasogastric tube`, `NG tube`, `NGT` | `nasogastric` (a *route*) | `route_0062`, tier `curated` | `nasogastric tube` (the *device*) |
| `once a day` | `every day` | `time_frequency_0001` | its own canonical |

The device spellings now belong to the `nasogastric tube` term. The frequency
chain `OD → once a day → every day` — recorded in the previous changelog as a
known limitation "deliberately left in place" because repairing it seemed to
mean re-canonicalizing dozens of entries — turned out to be a **one-element**
removal. Loader warnings fell **170 → 167**, and `OD the medicine →
once a day the medicine` (pinned by an existing test) still holds.

This is the sanctioned direction for a dictionary problem: fix the data, never
loosen the replacement policy.

### D1 — a unit split across finals stranded its number (fixed)

`_keep_number_with_its_unit` asked `rule_match_at()`, which sees **complete**
forms only. When Speechmatics split the unit itself there was no complete rule
at the cut, so the pending `mg` was invisible:

```text
"پانصد میلی" + "گرم"  ->  "500" then a stray "میلی گرم"   (before)
"پانصد میلی" + "گرم"  ->  "500 mg"                        (after)
```

**Fix:** new `MedicalMatcher.strict_prefix_canonical_heads()` (exposed on
`MedicalLayer`) answers what a held fragment could still grow into. It is the
companion to the existing `is_strict_rule_token_prefix`, which answers *whether*
a fragment can grow but not *into what* — and the second answer is what the cut
needs, because a pending **unit** must keep the number in front of it while a
pending **new measurement phrase** (`اشباع اکسیژن` → `SpO2`) must not.

---

## 4. Speechmatics vocabulary

### Audit of the 146 exported entries

| Metric | Value |
| --- | --- |
| Entries exported | **146** of 952 terms (**15.3%**) — a curated biasing list, not a dump |
| Against the service's hard cap | 146 / 20,000 items (**0.73%**) |
| **Against the project's own tested budget** | **146 / 150 — 4 slots free** |
| By type | abbreviation 69, term 17, condition 15, phrase 11, procedure 9, drug 8, lab 6, imaging 6, anatomy 2, dosage_unit 2, vital_sign 1 |
| Entries with `sounds_like` | 140 / 146 |
| Total pronunciations | 345 |
| Over the 6-word content limit | **0** |
| Over the 6-word `sounds_like` limit | **0** |
| BiDi controls in content / forms / pronunciations | **0 / 0 / 0** |
| Bare numbers exported | **0** |
| ZWNJ in `sounds_like` | 18 (valid Persian orthography, e.g. `پی‌تی‌تی`) |
| ZWNJ in content | 1 (`پنی‌سیلین`) |
| Artifact in sync with the dictionary | ✓ (`export_additional_vocab.py --check`) |

### D6 — four pronunciations were sent and silently ignored (fixed)

Speechmatics applies `sounds_like` **only in the main script of the session
language**. The export is a Persian-stream artifact, so 4 of its 345
pronunciations are Latin and cannot take effect on a `fa` session:

| Content | Inert `sounds_like` |
| --- | --- |
| `MRI` | `M R I` |
| `HbA1c` | `H B A one C` |
| `C3-C4` | `C three C four` |
| `metformin` | `met for min` |

The service ignores them and answers with a `validation_warning` that nothing
surfaced (D7). The operator believes those four terms are pronunciation-biased
when they are not.

**Fix:** `_clean_vocab(vocab, language=None, dropped_pronunciations=None)`
filters by script, keeping the **term** and dropping only the dead hint. Each
removal is recorded in `SessionResult.vocabulary_notes` and in the report.

- With `language` omitted the output is **byte-identical** to the previous
  behaviour (asserted), so the staticmethod stays usable for dictionary-side
  validation and the existing test's contract is preserved.
- A digits-only or genuinely mixed-script hint returns **no verdict** and is
  **kept** — this filter decides what to send, so an uncertain case must not be
  silently discarded; the service's own validation still gets to speak.
- Region subtags (`fa-IR`, `en-US`) classify as their base language.

**Consequence worth stating plainly:** on an `--language en` session the same
filter drops **341** Persian pronunciations. The export is a Persian-stream
artifact, so the English stream is biased on *content only*. That is not a bug —
it is what the data is — but it should be a deliberate choice, and the report
now makes it visible per session.

### High-value missing pronunciations

**295** unexported terms have a Latin canonical *and* a Persian form (Persian
dictation → Latin chart output is the hardest recognition problem), broken down
as: lab 132, procedure 75, drug 47, imaging 41.

**Recommendation — bounded, and explicitly not "export the dictionary":**

1. **Highest value: the 47 unexported drug names with Persian
   transliterations** — `acetaminophen`, `aspirin`, `clopidogrel`,
   `ciprofloxacin`, `amoxicillin`, `amlodipine`, `azithromycin`, `captopril`,
   `cefazolin`, `budesonide`, … A misheard drug name is the highest-consequence
   ASR error in this domain, and the existing 8 exported drugs prove the pattern
   works (each carries a Persianized `sounds_like`).
2. **The dictionary carries no `sounds_like` for any of the 295.** Each addition
   needs an authored Persianized pronunciation, so this is data work, not a
   flag flip.
3. **The budget is the binding constraint, not the service.** At 146/150 there
   are 4 slots. Adding drugs requires either trading out lower-value entries or
   raising the ceiling in `tests/test_dictionary.py` — and a raise should be
   justified by a **measured** recognition gain, not by headroom. Speechmatics
   recommends ~1,000 words and warns of up to a 15 s init delay with
   `additional_vocab`, so the ceiling is worth revisiting; it should not be
   lifted silently.
4. **Weakest current entries to trade out:** `g` (a single letter with no
   `sounds_like` — a very weak bias) and `magnesium` (Latin content, no
   `sounds_like`, so nothing pronounceable reaches a `fa` session). The other
   four entries without pronunciations (`آپاندکتومی`, `ایسکمی میوکارد`,
   `نرمال سالین`, `تورگور`) have Persian *content*, which biases on its own.
5. The **311** high-value terms that stay unexported are not lost: their Persian
   forms are matcher rules, so they canonicalize deterministically **once
   recognized**. Biasing only affects recognition, never post-processing — which
   is exactly why the bounded list is the right design.

---

## 5. Accuracy evaluation

### Metrics were already honest — the stages were not separated

`evaluation.py` already refuses to call recall-only `number_accuracy` an
accuracy: its docstring states the limitation explicitly (`reference "20 mg"`
vs `hypothesis "20 mg 50 mg"` scores a perfect 1.0 on recall), it is retained
only for API/report stability, and `number_precision` / `number_recall` /
`number_f1` are the unambiguous metrics. No change was needed there.

The gap was in the **benchmark's stage separation**. It documented "five
separated metrics" but published four, with medical canonicalization and the
numeric fold merged into one `canonical_single_pass` stage. A destroyed medical
term and an invented dose were indistinguishable in that number.

### Now: five stages in pipeline order

```text
raw                           A  the stored ASR segments verbatim
normalized                    B  generic normalization only
medical_canonicalization_only C  the dictionary pass, numeric fold DISABLED
numeric_fold_only             D  the number/clock/ratio fold, dictionary DISABLED
canonical_single_pass         E  C then D over the whole transcript
canonical_streamed            E' the same through realtime final boundaries —
                                 the text actually injected
```

Current measurement (53 cases):

| Stage | exact | WER | num P/R/F1 | entity P/R |
| --- | --- | --- | --- | --- |
| A `raw` | 0.260 | 0.497132 | 1.000 / 0.258 / 0.258 | 1.000 / 0.300 |
| B `normalized` | 0.280 | 0.497132 | 1.000 / 0.258 / 0.258 | 1.000 / 0.300 |
| C `medical_canonicalization_only` | 0.540 | 0.300191 | 1.000 / 0.258 / 0.258 | 1.000 / 0.300 |
| D `numeric_fold_only` | 0.460 | 0.261950 | 1.000 / 0.793 / 0.810 | 1.000 / 0.900 |
| E `canonical_single_pass` | **1.000** | **0.000000** | 1.000 / 1.000 / 1.000 | 1.000 / 1.000 |
| E' `canonical_streamed` | **1.000** | **0.000000** | 1.000 / 1.000 / 1.000 | 1.000 / 1.000 |

**An important property the split exposed:** C and D are **not independent**. D
is deliberately gated by C, because the fold refuses to digitize a spoken number
without a recognized measurement anchor, and the anchors come from the
dictionary pass (`فشار خون` → `BP`, `میلی گرم` → `mg`). So D scoring below E is
**by design** — that gap *is* the guarantee that ordinary prose is never
converted into numbers. Concretely, `قند خون صد و هشتاد` stays fully spoken
under D alone and becomes `glucose 180` only once C has anchored it. A test pins
the relationship (`numeric_fold_only < canonical_single_pass`, plus
`fold("قند خون صد و هشتاد")` unchanged vs `fold("glucose صد و هشتاد")` folded)
so it cannot invert unnoticed.

Stage C is computed through the same private entry point the test suite already
uses for this split (`tests/test_matcher.py` calls `_scan` directly for the same
reason), with the same narrative policy as the live path — so no public API was
widened for a benchmark.

### Evaluation cases extended 38 → 53

New coverage: canonical spans on the live path (4 cases), one dictated value
split across finals at its joiner and inside its unit (4), explicit fraction
denominators (1), ventilator settings (1), intake/output (1), nursing
diet/wound/mobility/isolation orders (1), and three kinds of ordinary
non-clinical Persian prose (3).

Every new case was reviewed against its output before being accepted, and each
agrees across the single-pass and streamed paths.

**Raw-ASR numbers were not re-measured.** `benchmark_asr.py --dry-run` is
unchanged (RAW WER 0.178571, num P/R/F1 0.375/0.375/0.375, entity P/R
0.375/0.375) because it needs live credentials and audio. **No accuracy
improvement is claimed for recognition** — every improvement here is in the
deterministic layer, measured by a reproducible benchmark.

---

## 6. Testing

| | Baseline | After |
| --- | --- | --- |
| `pytest -q` | **887 passed**, 1 skipped | **1002 passed**, 1 skipped, 0 failed |
| Collected | 888 | 1003 |
| Benchmark cases | 38 | 53 |
| `verify_regressions.py` breaks | 5 (one already broken) | **14, all correctly failing** |
| Loader warnings | 170 | 167 |

Per-file collection: `test_matcher` 607, `test_app` 83, `test_bidi_and_case` 58,
`test_realtime` 50, `test_injector` 43, `test_dictionary` 40, `test_core` 25,
`test_ui_threading` 21, `test_e2e` 19, `test_microphone` 15,
`test_entity_guard` 12, `test_dictionary_fixes` 8, `test_safety_regressions` 6,
`test_benchmark_postprocess` 5, `test_benchmark_asr` 5, `test_scripts` 5,
`test_entity_guard_integration` 1.

### +115 tests added

- **D3/D4** — 14 explicit-denominator cases pinned in *both* scan modes, 11
  `spoken_number_at` shapes, prose-safety corpus, idempotence, and the
  no-dangling-denominator invariant.
- **D8** — every multi-token canonical as a fixed point in both modes, 20
  realistic sentences on the live narrative path, a no-duplicated-word
  signature check, `at_risk_canonicals` correctness (including the trailing-token
  off-by-one), and a structural proof that claim rules contribute no prefix.
- **D1/D5** — 13 targeted boundary splits, an exhaustive sweep of every word
  boundary of 22 measurements (>300 fragmentations), and a drain test proving
  the longer hold cannot accumulate the session.
- **D6/D7** — script filtering both directions plus region subtags and the
  no-language legacy path, unclassifiable hints kept, server warnings recorded
  and deduplicated, and the degraded-SDK path.
- **D2** — a fake recorder whose `__enter__` fails: clean message, exit 1, no
  traceback, device released, SIGINT and overlay cleanup intact.
- **Evaluation** — the six stage labels, C/D measured apart, and the C→D gating
  relationship.

### Existing tests changed, and why

Three pre-existing tests were touched, each with its original intent preserved:

1. `test_vocab_cleaning` and `test_config_values_sent` used **cross-script**
   pronunciations as incidental fixture data. Their vocab expectations were
   switched to same-script data so they keep testing what they were written for
   (trim/dedupe/junk filtering, and config pass-through), and the new script
   behaviour got its own dedicated tests. Silently letting a new rule redefine
   an old test would have hidden the change.
2. The test fake SDK gained a `WARNING` member, a dual-form `on()` matching the
   real `EventEmitter` signature, and a `no_warning_event` behaviour flag.
3. `FakeMic` gained the `close()` its real counterpart has always had.

**The pre-migration behavioral snapshot needed a 4-line change.** 2 of its 432
cases now report a *longer hit span* for **byte-identical canonical text**,
because `iv line` matches the whole `IV line` canonical instead of only its `iv`
token. A guard asserted that all 432 canonical strings were unchanged **before**
the fixture was touched, so the snapshot's actual protection (the transcript) was
verified rather than assumed.

### Regression-by-breaking

`benchmark/verify_regressions.py` disables each fix by a surgical text
replacement and confirms the pinned tests fail, then restores the files
byte-identically. Break **B**'s anchor had been silently invalidated by the D4
restructure and was repaired. All **14** breaks now report `correctly FAILS`,
and the script ends with `files restored exactly: True`.

```text
A. drop the spoken half (نیم)                      -> 15 failed
B. drop the unit+و+unit decimal (1.8)              ->  7 failed
C. drop the lab/measurement anchors                ->  5 failed
D. drop the Persian measurement anchors            ->  2 failed
E. drop the decimal blockers                       ->  4 failed
F. drop the explicit-denominator decimal      (D4) -> 13 failed
G. drop the trailing-denominator group ext.   (D3) ->  2 failed
H. drop the canonical claim rules             (D8) -> 19 failed
I. drop the pending-unit head lookup          (D1) ->  5 failed
J. shrink the unit walkback to numerals       (D5) ->  9 failed
K. shrink the numeric tail to the old group   (D5) ->  2 failed
L. drop the vocabulary script filter          (D6) ->  2 failed
M. drop the WARNING subscription              (D7) ->  1 failed
N. drop the guarded microphone __enter__      (D2) ->  1 failed
```

### Full validation

```text
python -m pytest -q                                     -> 1002 passed, 1 skipped
python -m compileall -q app.py injector.py overlay.py
    speechmatics_test tests benchmark scripts           -> OK
scripts/export_additional_vocab.py --check              -> OK (2575 rules, 952 terms,
                                                              146 entries, in sync)
scripts/validate_dictionary.py                          -> OK (exit 0)
benchmark/benchmark_postprocess.py                      -> exact 1.000 both canonical
                                                           paths, 53/53 agree
benchmark/benchmark_asr.py --dry-run                    -> unchanged (no live ASR run)
benchmark/benchmark_matcher.py                          -> build 74.4-75.8 ms,
                                                           match 36.5-36.9 us, peak 5.14 MB
                                                           (benchmark/results.json restored
                                                            to HEAD - see Performance)
benchmark/verify_regressions.py                         -> 14/14, restored exactly
app.py --help                                           -> OK
git diff --check                                        -> OK
```

### Performance and memory

Measured two ways, because absolute timings are machine-specific and the two
questions are different.

**(a) Same-machine A/B on the real dictionary.** Construction was timed 12× with
the D8 claim rules enabled and again with them surgically disabled
(`claims = at_risk_canonicals(alias_rules)` → `claims = []`), which removes both
the claim rules and the prefix-head maps they share a code path with:

| | rules | build median | build min | resident | peak |
| --- | --- | --- | --- | --- | --- |
| with the fix | 2575 | 432.21 ms | 401.99 ms | 4.67 MB | 5.24 MB |
| fix disabled | 2501 | 433.75 ms | 392.50 ms | 4.58 MB | 5.18 MB |

**Build time is unchanged** (the 1.5 ms difference is inside run-to-run noise and
in the *unfavourable* direction for the baseline), at a cost of **+0.09 MB
resident / +0.06 MB peak** on the real dictionary.

**(b) Against the committed benchmark artifact.** `benchmark/results.json` was
produced on different hardware, so its *timings* are not comparable — but its
*memory* figures are, and so is the latency distribution shape:

| | committed baseline | now |
| --- | --- | --- |
| Peak traced memory, 2000 synthetic rules | 3.971 MB | 5.14 MB (**+1.17 MB**) |
| Per-sentence mean latency, 2000 rules | median 36.49 µs | median 36.48–36.89 µs (**unchanged**) |
| `match_repeats_per_sentence` | 100 | 100 |

The +1.17 MB at 2000 synthetic rules is the prefix-head maps, which duplicate
the key set of the existing strict-prefix sets. Merging them would recover most
of it but would couple `is_strict_rule_token_prefix` to a non-empty-canonical
assumption; two structures with distinct jobs was judged the more robust choice
for a desktop clinical app, and 5 MB is not a constraint there.

**`benchmark/results.json` was deliberately NOT re-committed.** Regenerating it
on this machine roughly doubled every recorded timing — including build time at
100 rules, which none of these changes touch — because the sandbox is slower
than the machine that produced the committed baseline. Overwriting it would have
published a fake 2× regression into a tracked artifact. The file is restored to
`HEAD`; no test reads it. Re-record it on the reference machine if a new baseline
is wanted.

---

## 7. Safety

| Requirement | Status |
| --- | --- |
| Never fabricate, infer, translate or clinically reinterpret uncertain ASR output | **Holds, and two violations were removed.** D4 was writing `Temp 38` for a dictated 30.8 and `glucose 125` for 120.5; D5 was writing `2000 و 500` for a dictated 2500. D3's dangling denominator (`دوز 2.5 دهم`) was a stated value nobody said. All three are now impossible, and each has a machine-checked invariant. |
| Low-confidence values flagged for review, not silently corrected | Unchanged. `ClinicalEntityGuard` remains review-only, `FinalStreamCanonicalizer.flags` / `last_flags` never alter canonical or injected text, and the new `service_warnings` and `vocabulary_notes` follow the same rule. |
| No hidden Unicode BiDi controls in stored/canonical text | **0** in canonicals, **0** in forms, **0** in pronunciations. `presentation.has_bidi_controls` is asserted by `validate_dictionary.py` and the test suite. The ZWNJ (U+200C) in `پنی‌سیلین` and 18 pronunciations is legitimate Persian orthography, not a BiDi control, and is unaffected. |
| Do not persist microphone audio | Unchanged. `microphone.py` never writes audio; `audio_source` yields in memory only; `.gitignore` excludes `results/*.json`; `Audio storage : NONE (in-memory streaming only)` is printed in the session banner. The D2 fix does not touch this. |
| Deterministic post-processing | Unchanged. No randomness anywhere in the pipeline; the three scan engines agree over 45,846 pairwise comparisons, so the output does not depend on which engine is installed. |

---

## 8. Engineering constraints

| Constraint | How it was met |
| --- | --- |
| Minimum necessary changes | 8 defects, 8 targeted fixes. No stable component was rewritten; `microphone.py`, `entity_guard.py`, `cleanliness.py`, `session_controller.py`, `medical_layer.py` (beyond one pass-through) and `evaluation.py` are unchanged or near-unchanged. |
| Do not refactor working code for style | The only structural additions are the prefix-head maps and the claim-rule registration, both required by a defect. |
| Preserve public APIs | Additive only: `at_risk_canonicals()`, `strict_prefix_canonical_heads()`, `CANONICAL_CLAIM_SOURCE`, `SessionResult.service_warnings`, `SessionResult.vocabulary_notes`, and two optional parameters on `_clean_vocab`. `MedicalFST`, `number_accuracy`'s exact definition and value, `is_strict_rule_token_prefix`, `rule_match_at` and the report's existing keys are all unchanged. Two report keys were **added**, none renamed or removed. |
| Update README only where behaviour changed | 7 sections updated: the numeric table, the decimal-shape limits, the cross-segment number-group hold, a new "Canonical spans are fixed points" subsection, the vocabulary script filter and server warnings, the five evaluation stages, and the `validate_dictionary` description. The Design Constraints section already stated the required prohibitions and needed no change. |
| No new dependencies | None. `contextlib` is stdlib. |
| No accuracy claim without a reproducible benchmark | Every claimed improvement is a deterministic-layer change measured by `benchmark_postprocess.py` (53 cases, exact 1.000) or by a parity sweep. **No recognition-accuracy improvement is claimed**, because no live ASR run was performed. |

---

## Known limitations deliberately left in place

1. **Five single-token canonical expansions** — `CK-MB → creatine kinase-MB`,
   `HCO3 → bicarbonate`, `PLT → platelet count`, `US → ultrasound`,
   `nebulizer → inhalation`. These are the dictionary expanding an abbreviation
   on purpose; a single-token canonical cannot claim itself. Reported as a note
   by `validate_dictionary.py`, not a problem.
2. **Two documented ambiguous collisions** — an all-caps ordinary English word
   (`the US report`, `BE careful`, `the IT department`) is indistinguishable
   from chart notation without a semantic layer. Guarded by
   `_AMBIGUOUS_SHORT_FORMS`; unresolvable by design.
3. **`PEEP پنج` stays spoken** — `PEEP` is not in the anchor vocabulary, so the
   fold declines to digitize it. Adding ventilator anchors is a vocabulary
   decision with its own prose-collision risk, and leaving a value spoken is the
   safe direction. Reported here rather than silently "fixed".
4. **`<number> سال` does take chart digits** (`5 سال پیش عمل شد`), because
   `سال` is the age/duration unit the dictionary already anchors on. A
   possessive or adjective spelling (`سالم` = "my years" / "healthy") is
   deliberately **not** that anchor — the word is genuinely ambiguous — so
   `امسال بیست و پنج سالم شد` and `بیمار سالم است` both survive unchanged. Both
   behaviours are now pinned as benchmark cases so neither can drift unnoticed.
5. **70 loader tier arbitrations remain** (`conflicting canonicals for form`),
   now broken down by kind so they are actionable. Each is a duplicate form
   owned by two terms; the loader resolves it deterministically by tier and logs
   the decision. None currently destroys a token — the two that did were fixed —
   but they are a standing data-quality backlog, not a code defect.
6. **97 forms are skipped for punctuation** (`q.d.`, `o.d.`, `C3-C4`-style
   hyphenation, `لوله بینی-معدی`). Deliberate: the scanner is token-aligned, so
   a punctuated form could never match reliably. Data hygiene, reported per kind.
7. **The English stream is content-biased only.** The vocabulary export is a
   Persian-stream artifact, so 341 of its 345 pronunciations are inert on
   `--language en`. Making the English stream properly biased would need a
   second, Latin-script export — a data-authoring project, not a code fix.
8. **Character-level splits inside a word are not handled.** Speechmatics
   finalizes on word boundaries, so `پانصد م` + `یلی گرم` is not a shape the
   stream can produce; the exhaustive sweep tests word boundaries only.

---

## Provenance of every number in this report

No figure here is recalled from an earlier measurement. Each was re-derived
against the current tree, and four that did **not** reproduce were corrected:

| Figure | How it was obtained |
| --- | --- |
| 1002 passed / 1 skipped | `pytest -q`, full suite |
| 14/14 breaks with exact failure counts | `benchmark/verify_regressions.py`, run in full |
| 167 loader warnings (97 punctuation + 70 conflicting), 74 claim rules, 2575 rules | `matcher.warnings` counted and bucketed by kind |
| 935 canonicals → 46 not fixed points → 5 | each distinct canonical re-canonicalized through the live narrative path |
| 46 → 5, and 41 visibly corrupted before | same census with `claims = []` applied and the file restored byte-identically |
| 45,846 parity comparisons, 0 mismatches | 3 engines × 7,641 inputs × 2 modes, `_scan` vs `_scan_reference` called directly |
| 1,963 / 1,774 / 189 / 0 new Persian words | per-rule narrative-mode scan with a Persian-token subset test |
| 400 fragmentations, 88 divergences before / 0 after | the sweep recomputed, then re-run with breaks J+K applied |
| 341 vs 4 dropped pronunciations | `_clean_vocab` called with `language="en"` and `"fa"` |
| 146 / 952 exported, 146 / 150 budget, 345 pronunciations, 0 BiDi | vocabulary artifact enumerated directly |
| Build 432 vs 434 ms, +0.09 MB | 12 timed constructions each way, same machine |
| Stage table, exact 1.000, 53/53 | `benchmark_postprocess.py` |
| RAW WER 0.178571, num 0.375/0.375/0.375 | `benchmark_asr.py --dry-run` |

**Corrected during verification** (initial drafts carried stale values from
mid-session probes): the D8 before-count (51/45 → **46/41**), the parity
comparison count (14,924 → **45,846**), the narrative-mode census
(1,834/129/0-Persian → **1,774/189/0-new-Persian-words**), and the pre-D5
divergence count (18 → **88**).

Two measurement bugs were caught and fixed rather than reported: constructing a
"reference" matcher by nulling both engines does **not** route to
`_scan_reference` (it short-circuits and returns the input unchanged, which
produced 7,543 phantom mismatches), and `canonicalize()` returns
`(text, hits)`, not `text`.

Every temporary patch used for a before/after measurement was restored
byte-identically and confirmed by `sha256sum -c` plus `git diff --check`.

---

## Reproducing this audit

```powershell
.\.venv\Scripts\python.exe -m pytest -q                          # 1002 passed, 1 skipped
.\.venv\Scripts\python.exe benchmark\benchmark_postprocess.py    # exact 1.000, 53/53
.\.venv\Scripts\python.exe benchmark\verify_regressions.py       # 14/14 breaks fail
.\.venv\Scripts\python.exe scripts\validate_dictionary.py        # OK, exit 0
.\.venv\Scripts\python.exe scripts\export_additional_vocab.py --check
.\.venv\Scripts\python.exe benchmark\benchmark_matcher.py
```

Change-by-change detail — file, bug, fix, why safe, test added — is in
`CHANGELOG_FIX_SESSION.md` under *"Backend reliability and accuracy audit pass
(2026-09-29)"*.
