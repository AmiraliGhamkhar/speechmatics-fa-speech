# SwiftMedics — Speechmatics Mixed Persian/English Medical ASR Test v2

A clean, reproducible Python benchmark application for testing Speechmatics Realtime with Persian, English, Persianized English medical words, nursing terminology, abbreviations, numbers, routes, and clinical expressions.

## What changed in v2

Version 2 is built around three separate layers:

1. **Speechmatics ASR** — preserves the raw realtime output.
2. **Medical terminology layer** — conservative, deterministic aliases from your provided nursing terminology and your observed ASR session.
3. **Benchmark/evaluation layer** — compares canonicalized output against a human-provided expected transcript.

The project also keeps your uploaded Windows `injector.py` and `overlay.py`. The injector already uses native Windows UTF-16 `SendInput`, clipboard verification/retries, modifier hygiene, and smart partial-revision handling. The overlay already handles mixed RTL/LTR display and Persian font selection.

## Source data incorporated

- `اصطلاحات رایج پرستاری.xlsx`: 110 populated nursing terminology rows were converted into structured JSON.
- Uploaded Speechmatics session JSON: observed forms such as `سی سی یو`, `سی تی اسکن`, `لیژن`, `رایت لانگ`, `آی وی`, and `میلی گرم` were used as **observed aliases**, not as general medical truth.
- Uploaded WAV: kept as the first reproducible session artifact outside the source tree; put future recordings under `recordings/`.

## Architecture

```text
Microphone
   |
   v
Speechmatics Realtime
   |
   +----> partials -> overlay
   |
   v
raw final transcript
   |
   v
text normalization
   |
   v
nursing/medical alias layer
   |
   v
canonicalized transcript
   |
   +----> evaluation
   |
   +----> JSON audit log
```

## Why the medical layer is conservative

The dictionary is **not an LLM** and does not infer diagnosis, dosage, laterality, negation, or clinical meaning. It only applies explicit mappings.

That is intentional. ASR correction and clinical interpretation should remain separate so the benchmark can show exactly what Speechmatics produced.

## Install — Windows PowerShell

```powershell
cd C:\path\to\speechmatics_mixed_medical_test_v2
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1
notepad .env
```

Set:

```text
SPEECHMATICS_API_KEY=YOUR_SPEECHMATICS_API_KEY
```

## Run

Persian:

```powershell
.\.venv\Scripts\python.exe app.py --language fa
```

English:

```powershell
.\.venv\Scripts\python.exe app.py --language en
```

Without Speechmatics custom vocabulary:

```powershell
.\.venv\Scripts\python.exe app.py --language fa --no-vocab
```

Without the medical post-processing layer:

```powershell
.\.venv\Scripts\python.exe app.py --language fa --no-medical-layer
```

Run a benchmark case:

```powershell
.\.venv\Scripts\python.exe app.py --language fa --test-id mix_ct_lesion
```

Optional Windows text injection:

```powershell
.\.venv\Scripts\python.exe app.py --language fa --inject
```

Disable overlay:

```powershell
.\.venv\Scripts\python.exe app.py --language fa --no-overlay
```

## Test protocol

### 1. Establish ASR baseline

Run:

```powershell
.\.venv\Scripts\python.exe app.py --language fa --no-vocab --no-medical-layer
```

Speak naturally.

### 2. Test Speechmatics vocabulary

Run:

```powershell
.\.venv\Scripts\python.exe app.py --language fa
```

Repeat the exact same speech.

### 3. Test deterministic medical normalization

Run:

```powershell
.\.venv\Scripts\python.exe app.py --language fa
```

Compare:

- `final_transcript_raw`
- `final_transcript_normalized`
- `final_transcript_canonicalized`
- `medical_hits`

### 4. Benchmark

The benchmark requires the expected transcript to reflect what was actually spoken. Do not compare a 60-second exploratory recording against a one-sentence expectation.

## Nursing terminology structure

### `medical_knowledge/nursing_terms.json`

Contains the rows from the nursing spreadsheet.

### `medical_knowledge/abbreviations.json`

High-value abbreviation mappings such as:

- BP
- HTN
- DM
- IV
- IM
- SC
- ICU
- CCU
- ECG
- ABG
- NPO
- CPR
- FBS
- CBC
- U/A
- CXR
- PRN
- q2h
- tds

### `medical_knowledge/nursing_phrases.json`

Phrase-level nursing expressions such as:

- vital signs
- blood pressure
- oxygen saturation
- nothing by mouth
- intravenous
- intramuscular
- subcutaneous
- as needed
- every two hours
- three times daily

### `medical_knowledge/observed_asr_aliases.json`

Only observed forms from the uploaded session. These are evidence for the test dataset, not clinical ground truth.

### `medical_knowledge/speechmatics_additional_vocab.json`

A compact vocabulary intended for the Speechmatics `additional_vocab` feature.

## Evaluation

Each result JSON contains:

```json
{
  "first_partial_latency_ms": 0,
  "final_transcript_raw": "...",
  "final_transcript_normalized": "...",
  "final_transcript_canonicalized": "...",
  "medical_hits": [],
  "evaluation": {
    "wer": 0.0,
    "number_accuracy": 1.0,
    "similarity": 1.0
  }
}
```

For a medical ASR benchmark, also inspect:

- English medical term accuracy
- Persianized-English recognition
- abbreviation accuracy
- number accuracy
- dosage/unit accuracy
- route accuracy
- laterality accuracy
- negation accuracy
- latency
- partial stability

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Shell files

- `scripts/install.ps1`
- `scripts/run.ps1`
- `scripts/run_fa.ps1`
- `scripts/run_en.ps1`
- `scripts/run_benchmark.ps1`
- `scripts/run_no_vocab.ps1`
- `scripts/run.sh`

## Important Speechmatics design note

Speechmatics publishes separate realtime and multilingual capabilities. Do not interpret a successful Persian-stream test as proof of fully automatic Persian↔English realtime code-switching. This application is intentionally designed to measure the current API configuration empirically.

Official SDK/examples: https://github.com/speechmatics/speechmatics-academy


## Injection test (Windows)

Use the standalone injector test before combining it with ASR:

```powershell
.\scripts\test_injector.ps1
```

The test lets you click the exact target field and then pastes a mixed Persian/English string.

## ASR injection workflow

Run:

```powershell
python app.py --language fa --inject
```

After recording, SwiftMedics captures the current foreground window, gives you a countdown, and then pastes into the foreground window that is active when the countdown ends. **Do not click back into the terminal.**

## Output cleanliness

Before injection, v2 prints and saves a Unicode cleanliness report. It checks:

- arrow characters such as `→` and `←`
- bidi control marks such as LRM/RLM/RLE/PDF
- unexpected control characters
- replacement character `�`
- ZWNJ (`U+200C`)
- newlines/tabs
- unusual non-ASCII symbols

The exact injection payload is also printed with Python `repr()`, so invisible characters are easier to identify. The injector itself does **not** add arrows. If arrows exist in the ASR/canonicalized transcript, they are data and will be pasted exactly unless `--reject-arrows` is used.

To block injection when arrows are present:

```powershell
python app.py --language fa --inject --reject-arrows
```

## Injection design note

The earlier failure was primarily a foreground-focus problem: pasting immediately after the ASR session meant the terminal could still be the active window. v2 separates ASR completion from target selection and injection.
