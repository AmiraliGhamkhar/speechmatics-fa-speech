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

The medical layer is intentionally conservative.

It performs **deterministic lexical normalization only**. It does not infer diagnosis, severity, negation, dosage correctness, or clinical intent.

Rules are:

* Token-aware and boundary-safe.
* Longest-match first.
* Tier-aware for conflict resolution.
* ZWNJ-aware.
* Fully auditable through `form`, `canonical`, `tier`, `source`, and position.
* Protected by a reference-scanner fallback if the optimized matcher fails.

Clinical abbreviations that collide with common English words require stronger evidence, such as uppercase charting forms.

Cross-segment matching is supported for phrases that span multiple finalized ASR segments.

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
| `--no-text-polish`                 | Disable nursing text normalization (numbers, times, units, formatting) |

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

Run the nursing accuracy benchmark (103 fixtures, offline, no API key):

```powershell
.\.venv\Scripts\python.exe scripts\swiftmedics_tools.py benchmark
```

It writes `benchmark/results_current.json` and regenerates
`benchmark/README.md` from the measured values. Accuracy is reported
separately per pipeline stage (generic normalization, medical
canonicalization, number/time/unit, grammar/format, full paragraphs), so a
formatting fix is never presented as a terminology improvement.

Run matcher performance benchmarks:

```powershell
.\.venv\Scripts\python.exe scripts\swiftmedics_tools.py benchmark-matcher
```

or, equivalently:

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
    swiftmedics_tools.py        <- all tooling logic lives here
    _dictionary_fixes.py        <- idempotent, audited dictionary repairs
    install.ps1                 <- thin wrappers, kept for compatibility
    run.ps1
    run.sh
    run_fa.ps1
    run_en.ps1
    run_no_vocab.ps1
    run_benchmark.ps1
    run_matcher_benchmark.ps1
    export_additional_vocab.py
    test_injector.ps1

tests/
    fixtures/
        pre_migration_canonicalization.json

benchmark/
    dataset.py                  <- 103 frozen nursing fixtures
    run_benchmark.py            <- accuracy + performance runner
    benchmark_matcher.py        <- matcher scaling benchmark
    README.md                   <- generated from measured results
    results_baseline.json
    results_current.json
    results_comparison.json
```

## Design Constraints

This project intentionally avoids:

* LLM post-processing
* Embeddings
* Vector databases
* Generative correction
* Audio recording/persistence
* Accumulating partial transcripts

The medical layer is a **lexical canonicalization system, not a clinical reasoning system**.

## Speechmatics Note

A successful Persian realtime session should not be interpreted as proof of fully automatic Persian-English code-switching support.

Mixed-language behavior should be evaluated empirically under the exact Speechmatics model, language, vocabulary, and domain configuration being used.

Official Speechmatics resources:

* Speechmatics Academy: https://github.com/speechmatics/speechmatics-academy
* Speechmatics Python SDK: https://github.com/speechmatics/speechmatics-python-sdk

## Tooling

All tooling logic lives in a single script, `scripts/swiftmedics_tools.py`:

```powershell
.\.venv\Scripts\python.exe scripts\swiftmedics_tools.py --help
```

| Subcommand          | Purpose                                                  |
| ------------------- | -------------------------------------------------------- |
| `install`           | Create `.venv` and install `requirements.txt`            |
| `run`               | Run `app.py` with pass-through arguments                 |
| `run-fa`            | Run Persian dictation                                     |
| `run-en`            | Run English dictation                                     |
| `benchmark`         | Nursing accuracy benchmark (offline)                      |
| `benchmark-matcher` | Matcher build/latency/memory scaling benchmark            |
| `export-vocab`      | Regenerate `speechmatics_additional_vocab.json`           |
| `check-vocab`       | Verify the vocabulary artifact matches the dictionary     |
| `audit-dictionary`  | Report duplicates, invalid entries, metadata drift        |
| `test-injector`     | Standalone injector smoke test                            |

The `.ps1` / `.sh` files are thin forwarding wrappers so existing commands and
documentation keep working; they contain no logic of their own.

## Nursing Text Normalization

After medical canonicalization, a deterministic normalization stage
(`speechmatics_test/nursing_text.py`) turns spoken nursing documentation into
clean clinical prose. It is rule-based and auditable - no model, no inference.

| Spoken                     | Output                |
| -------------------------- | --------------------- |
| `سی و پنج ساله`            | `35 ساله`             |
| `ده و نیم`                 | `10:30`               |
| `ساعت ده سی`               | `ساعت 10:30`          |
| `۱۰ و ۳۰ دقیقه`            | `10:30`               |
| `میلی متر جیوه`            | `mmHg`                |
| `درجه سانتی گراد`          | `°C`                  |
| `نمره نمره`                | `نمره`                |
| `می باشد. باشد.`           | `می‌باشد.`            |
| `Temp . 36.7. °C`          | `Temp: 36.7 °C`       |
| `blood pressure. 140/85 mmHg` | `BP: 140/85 mmHg`  |

Safety rules it will not break:

* **No number is ever invented or deleted.** An unbound numeral next to a time
  (`10 و 30 دقیقه 90`) is kept verbatim and reported as a warning rather than
  absorbed into the timestamp.
* **Ambiguous cardinals need numeric context.** `یک`, `نه`, `سی`, `ده`, `شش`,
  `صد`, `دو` are only converted when a counter or explicit `و`-run makes the
  reading unambiguous, so `سی تی اسکن` and `یک بیمار` are left alone.
* **Repetition cleanup is repetition-safe.** Abbreviations with a genuine
  doubled syllable (`سی سی یو` = CCU, `آر آر` = RR) are protected from
  de-duplication.
* **Canonical text stays in logical Unicode order.** No BiDi control character
  is ever written into canonical output; presentation controls belong only to
  the overlay and injector.

Disable the stage with `--no-text-polish`. Warnings it raises are printed in
the session banner and stored under `text_polish_warnings` in the report JSON.
