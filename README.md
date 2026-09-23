# SwiftMedics — Speechmatics Mixed Persian/English Medical ASR v4

Production-oriented medical dictation for **Speechmatics Realtime**, optimized for Persian/English clinical speech, medical terminology, abbreviations, numbers, dosages, routes, and nursing expressions.

## Highlights

* **Aho-Corasick medical matcher** — deterministic lexical canonicalization with a native `pyahocorasick` backend and a pure-Python fallback.
* **Single medical dictionary** — `medical_knowledge/medical_dictionary.json` is the source of truth for canonicalization and Speechmatics vocabulary.
* **Automatic injection** — finalized segments are pasted directly into the focused application; no hotkeys or manual countdown.
* **RTL-aware injection** — mixed Persian/English text uses Unicode BiDi controls and ZWNJ/whitespace normalization.
* **Realtime-safe injection** — clipboard operations run on a dedicated FIFO worker and never block the ASR callback.
* **Focus guard** — prevents text from being injected into an unintended window.
* **Overlay** — logical-order rendering with automatic Persian/English direction detection.
* **No LLM / embeddings / vector DB** — all post-processing is deterministic and auditable.
* **No audio persistence** — microphone audio remains in memory only.
* **Audit-ready reports** — raw, normalized, canonical, confidence, language, timing, and medical-match metadata can be saved as JSON.

## Pipeline

```text
Microphone
   ↓
Speechmatics Realtime
   ↓
Partial transcript ──→ Overlay
   ↓
Final transcript
   ↓
Generic normalization
   ↓
Medical Aho-Corasick matcher
   ↓
Canonical transcript
   ├──→ Overlay
   ├──→ Injection worker → Target application
   └──→ JSON report
```

Partials are UI-only. Only finalized Speechmatics segments enter the medical post-processing pipeline.

## Medical Canonicalization

The live dictation path is intentionally conservative.

**Ordinary Persian clinical narrative is preserved.** Deterministic normalization is limited to explicitly dictated abbreviations, units, numeric expressions, and bounded chart notation. For example, `نرس کال`, `فلبیت`, `مقیاس مورس`, `مقیاس برادن`, and `دکتر احمدی` remain exactly those Persian phrases; `ای سی جی` may become `ECG`, and a measured `فشار خون صد و چهل روی هشتاد` may become `BP 140/80`.

The system does **not** generate or fill nursing templates. It does not reconstruct sentences, translate ordinary Persian narrative, infer diagnosis/severity/negation, repair uncertain ASR numbers, or invent missing clinical information. If Speechmatics recognizes the wrong word or number, the post-processor leaves it alone unless an explicit bounded lexical/numeric rule applies.

The compatibility matcher API still supports its established dictionary-wide canonicalization by default. The application enables the conservative narrative-preserving mode for final transcript injection.

Rules are:

* Token-aware and boundary-safe.
* Longest-match first.
* Tier-aware for conflict resolution.
* ZWNJ-aware.
* Fully auditable through `form`, `canonical`, `tier`, `source`, and position.
* Protected by a reference-scanner fallback if the optimized matcher fails.
* Spoken numbers are folded into charted notation, but only where a unit or a
  context word says what kind of number it is.

### Numeric and clock notation

Age, blood pressure, saturation and clock times are **not** per-value
dictionary rules: a row per spoken hour (`ساعت هشت` -> `8`) fires inside
unrelated text (`هر دو ساعت` = "every two hours"), so that notation is
produced by a bounded, deterministic fold in `speechmatics_test/text.py`
(`fold_numeric_expressions`), applied to the text *after* the lexical pass and
configured by `matcher.NUMERIC_CONTEXT`. AM/PM is written only when the
abbreviation itself is spoken (`ای ام`, `پی ام`, `A.M.`, `P.M.`); ordinary
day-part words (`صبح`, `ظهر`, `عصر`, `شب`) stay Persian.

| Spoken | Canonical |
| --- | --- |
| `سن بیست سال`, `بیمار بیست و پنج ساله` | `سن 20 سال`, `بیمار 25 ساله` |
| `فشار خون صد و بیست روی هشتاد` | `BP 120/80` |
| `اشباع اکسیژن نود و هشت درصد` | `SpO2 98 %` |
| `ساعت هشت`, `ساعت هشت و نیم`, `ساعت هشت وربع` | `ساعت 8`, `ساعت 8:30`, `ساعت 8:15` |
| `ساعت هشت صبح`, `ساعت دو بعد از ظهر` | `ساعت 8 صبح`, `ساعت 2 بعد از ظهر` |
| `ای ام`, `پی ام`, `A.M.`, `P.M.` | `AM`, `PM` |

Deliberate limits:

* A numeral is folded only next to an anchor (`ساعت`, `سن`, `دوز`, `میلی گرم`,
  `BP`, `SpO2`, `درصد`, ...). `vivid`, `ivory`, `درد روی سینه` and
  `یک ضایعه در ریه` are left untouched.
* A number group is all-or-nothing: `پنج و شش ساله` or `صد و بیست و هشتاد` stay
  spoken rather than being half-converted, and `هزار` is not in the lexicon.
  The connector `روی` only anchors a right-hand number when a numeric left
  side exists, so a corrupt ASR fragment such as `MRI روی هشتاد و پنج` is not
  silently reinterpreted as a measurement.
* Durations are not clock readings: `دو و نیم ساعت` and `ساعت هشت و نیم ساعت`
  stay as spoken; `دقیقه` alone never anchors a number.
* Existing charting notation is never reformatted: `08:30`, `120/80`, `20 mg`,
  `98 %`, `12 PM`, `ساعت 08`, `۲۵ مارس ۲۰۲۵` (whose digits still fold) pass
  through unchanged, and the fold is idempotent.
* No calendar logic, no 12/24-hour arithmetic, no date math, no dose or
  severity inference. Trailing punctuation is preserved, never consumed.

Clinical abbreviations that collide with common English words require stronger evidence, such as uppercase charting forms.

Cross-segment matching is supported for phrases and numeric expressions that span finalized ASR segments. Only a bounded, potentially extendable suffix is retained (for example `پنجاه` + `و هشت` + `ساله`); ordinary text is emitted immediately, and the tail is always flushed at session end.

## Aho-Corasick Matcher

```text
medical_dictionary.json
        ↓
validate + normalize + deduplicate
        ↓
build automaton once
        ↓
scan transcript
        ↓
boundary filtering
        ↓
longest-match resolution
        ↓
canonical text + match metadata
```

The matcher is implemented in:

```text
speechmatics_test/matcher.py
```

`MedicalFST` remains available as a compatibility alias.

The native backend uses `pyahocorasick`. A pure-Python implementation provides equivalent output when the native package is unavailable.

## Speechmatics Configuration

Default configuration:

```text
Language:       fa
Model:          enhanced
Max delay:      2.0s
Delay mode:     flexible
Domain:         auto
Medical layer:  enabled
Vocabulary:     enabled
Injection:      enabled
Overlay:        enabled
```

### Medical Domain

Speechmatics documents the Enhanced Medical domain for a specific set of languages. **Persian is not currently included in that documented list.**

Therefore:

```text
--domain auto
```

does not send the medical domain for Persian.

Use:

```text
--domain medical
```

only when your Speechmatics deployment explicitly supports it.

## Additional Vocabulary

Speechmatics `additional_vocab` is intentionally bounded.

Only dictionary entries marked:

```json
"speechmatics": true
```

are exported.

The vocabulary focuses on high-value medical terms such as:

* Drugs
* Diseases
* Procedures
* Imaging
* Laboratory terms
* Anatomy
* Abbreviations
* Dosage units
* Compact entities such as `HbA1c`, `SpO2`, `C3-C4`, and `q2h`
* Persianized pronunciations

A bare number is never exported (it would fight the engine's own digit output),
and Speechmatics drops any element longer than six words, which
`SpeechmaticsRealtime._clean_vocab` enforces locally before the config is sent.
The list stays a bounded, curated biasing vocabulary rather than a dump of the
dictionary, and `tests/test_dictionary.py` guards that budget.

Generate or validate the derived vocabulary with:

```powershell
.\.venv\Scripts\python.exe scripts\export_additional_vocab.py
.\.venv\Scripts\python.exe scripts\export_additional_vocab.py --check
```

## Windows / PowerShell Setup

```powershell
cd C:\path\to\speechmatics-fa-speech

Set-ExecutionPolicy -Scope Process Bypass

.\scripts\install.ps1

notepad .env
```

Set your API key:

```text
SPEECHMATICS_API_KEY=YOUR_SPEECHMATICS_API_KEY
```

## Run

### Persian

```powershell
.\.venv\Scripts\python.exe app.py --language fa
```

### English

```powershell
.\.venv\Scripts\python.exe app.py --language en
```

### Benchmark

```powershell
.\.venv\Scripts\python.exe app.py --language fa --test-id mix_ct_lesion
```

## Useful Options

| Option                             | Description                                       |
| ---------------------------------- | ------------------------------------------------- |
| `--no-inject`                      | Disable automatic text injection                  |
| `--no-overlay`                     | Disable the transcript overlay                    |
| `--no-vocab`                       | Disable Speechmatics custom vocabulary            |
| `--no-medical-layer`               | Disable medical canonicalization                  |
| `--no-focus-guard`                 | Allow injection into the currently focused window |
| `--device-index N`                 | Select the PyAudio input device                   |
| `--model standard\|enhanced`       | Select Speechmatics model                         |
| `--max-delay 0.7-4.0`              | Configure finalization delay                      |
| `--max-delay-mode fixed\|flexible` | Configure delay behavior                          |
| `--domain auto\|medical\|none`     | Configure Speechmatics domain                     |
| `--save-report`                    | Save a session JSON report                        |
| `--test-id ID`                     | Run a benchmark case and save its report          |

## Automatic Injection

Start the application, click the target text field once, and dictate.

Each finalized segment is:

```text
ASR final
   ↓
canonicalization
   ↓
FIFO injection worker
   ↓
Clipboard → Ctrl+V
```

The injector provides:

* Atomic per-segment paste.
* FIFO ordering.
* Unicode BiDi wrapping.
* ZWNJ/whitespace cleanup.
* Clipboard restoration.
* Modifier-key protection.
* Focus protection.
* Honest per-segment `success: false` reporting when focus protection or paste fails.
* Windows clipboard retry/ownership handling.

Disable it with:

```powershell
.\.venv\Scripts\python.exe app.py --language fa --no-inject
```

## Overlay

The overlay renders **logical Unicode text** rather than pre-shaped visual text.

Direction is determined from the Persian/Arabic-to-Latin character ratio, with the first strong character used as a tiebreaker.

This keeps mixed text such as:

```text
CT scan بیمار دارای ضایعه است
```

readable without applying the BiDi algorithm twice.

Recommended font fallback:

```text
Vazirmatn
→ Vazir
→ IRANSans
→ B Yekan
→ B Nazanin
→ Segoe UI
→ Tahoma
→ Arial
```

## Evaluation

Reports can contain:

```text
WER
Number accuracy
Number precision
Number recall
Number F1
Similarity
Confidence
Language
Word timings
Medical matches
```

Medical-aware tokenization handles examples such as:

```text
20 mg
20mg
120/80
5.5
3,14
O2
q2h
C3-C4
U/A
HbA1c
```

Persian and Arabic-Indic digits are normalized to ASCII for evaluation.

`number_accuracy` is retained for API compatibility as recall. `number_precision`, `number_recall`, and `number_f1` provide stricter number evaluation.

## Testing

Run the complete test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Run matcher benchmarks:

```powershell
.\.venv\Scripts\python.exe benchmark\benchmark_matcher.py
```

or:

```powershell
.\scripts\run_matcher_benchmark.ps1
```

## Test Protocol

Use three separate runs:

### 1. ASR Baseline

```powershell
.\.venv\Scripts\python.exe app.py --language fa --no-vocab --no-medical-layer
```

### 2. Vocabulary

```powershell
.\.venv\Scripts\python.exe app.py --language fa
```

### 3. Canonicalization

Compare:

```text
final_transcript_raw
final_transcript_normalized
final_transcript_canonical
medical_hits
```

The expected transcript must match the speech actually spoken. Do not compare a long free-form recording against a short reference sentence.

## Project Structure

```text
app.py
speechmatics_test/
    matcher.py

medical_knowledge/
    medical_dictionary.json
    speechmatics_additional_vocab.json

scripts/
    install.ps1
    run.ps1
    run_fa.ps1
    run_en.ps1
    run_benchmark.ps1
    run_matcher_benchmark.ps1
    export_additional_vocab.py
    test_injector.ps1

tests/
    fixtures/
        pre_migration_canonicalization.json

benchmark/
    benchmark_matcher.py
    results_before.json
    results_after.json
```

## Design Constraints

This project intentionally avoids:

* LLM post-processing
* Embeddings
* Vector databases
* Generative correction
* Nursing-template filling or automatic report generation
* Audio recording/persistence
* Accumulating partial transcripts

The medical layer is a **lexical canonicalization system, not a clinical reasoning system**.

## Speechmatics Note

A successful Persian realtime session should not be interpreted as proof of fully automatic Persian-English code-switching support.

Mixed-language behavior should be evaluated empirically under the exact Speechmatics model, language, vocabulary, and domain configuration being used.

Official Speechmatics resources:

* Speechmatics Academy: https://github.com/speechmatics/speechmatics-academy
* Speechmatics Python SDK: https://github.com/speechmatics/speechmatics-python-sdk
