# SwiftMedics — Speechmatics Mixed Persian/English Medical ASR Test v3

A clean, reproducible Python benchmark application for testing Speechmatics Realtime with Persian, English, Persianized English medical words, nursing terminology, abbreviations, numbers, routes, and clinical expressions.

## What changed in v3

Version 3 is a production-oriented MVP built around separate, auditable stages:

1. **Speechmatics ASR** — preserves the true raw realtime final transcript.
2. **Generic text normalization** — Persian script unification, Persian/Arabic-Indic digit folding (`۲۰` → `20`), ZWNJ/whitespace, punctuation spacing. No medical knowledge.
3. **Medical FST canonicalization** — a deterministic OpenFst/Pynini transducer (plus an equivalent pure-Python fallback scanner) that performs token-aware lexical canonicalization only.
4. **Benchmark/evaluation** — compares RAW ASR vs NORMALIZED vs FST CANONICAL against a human-provided expected transcript (WER, number accuracy, similarity) with medical-aware tokenization.

Hard rules enforced by the design:

- **No LLM, no embeddings, no vector DB, no generative post-processing.**
- **No audio persistence.** Microphone chunks exist only in memory while streaming. No WAV files, no temp audio, no `recordings/` directory. Only text/metadata JSON reports are written, and only when requested (`--save-report` or `--test-id`).
- **Partials are for the UI/overlay only.** They are never treated as, or accumulated into, final text.
- **Post-processing runs only on finalized ASR segments.**
- **Ctrl+C stops the recording, it does not discard the dictation.** SIGINT ends the audio stream gracefully; the session is then closed normally and the canonicalization, cleanliness, injection and report stages all still run.

The project also keeps your uploaded Windows `injector.py` and `overlay.py`. The injector uses native Windows UTF-16 `SendInput`, clipboard verification/retries, modifier hygiene, and smart partial-revision handling. The overlay handles mixed RTL/LTR display, Persian font selection, and now distinguishes **partial** (revisable hypothesis) from **final** (finalized segment) display via separate APIs.

## Source data incorporated

- `اصطلاحات رایج پرستاری.xlsx`: 110+ populated nursing terminology rows converted into structured JSON.
- Uploaded Speechmatics session JSON: observed forms such as `سی سی یو`, `سی تی اسکن`, `لیژن`, `رایت لانگ`, `آی وی`, and `میلی گرم` are used as **observed aliases** (lowest-trust tier), not as general medical truth.
- `medical_knowledge/fst_terms.json`: the curated FST rule set (phrases, abbreviations, units, Persianized-English pronunciations, common ASR variants) with explicit deterministic priority tiers.

## Architecture

```text
Microphone (16 kHz mono PCM16, in-memory chunks only)
   |
   v
Speechmatics Realtime (speechmatics-rt SDK)
   |
   +----> ADD_PARTIAL_TRANSCRIPT -> overlay.set_partial()   (UI only)
   |
   v
ADD_TRANSCRIPT (final segments, raw, preserved)
   |
   v
RAW final transcript                  <- benchmark stage "raw"
   |
   v
normalize_text() (generic)            <- benchmark stage "normalized"
   |
   v
Medical FST (Pynini, token-aware)     <- benchmark stage "fst_canonical"
   |
   +----> overlay.set_final() per finalized segment
   +----> evaluation (raw / normalized / canonical vs expected)
   |
   +----> optional JSON report (text/metadata only, no audio)
```

## Why the FST layer is conservative

The FST is **deterministic lexical canonicalization only**. It does not infer
diagnosis, severity, negation, dosage correctness, or clinical meaning.

- Rules are exact, token-aware matches (word boundaries), never unsafe substring replacement.
- Longest match wins; ties are broken by tier (curated rules > abbreviations > observed aliases > phrases > validated terms > units), then by stable rule order.
- Every hit reports `form`, `canonical`, `tier`, `source`, and `position` for auditability.

## Speechmatics session configuration

- `max_delay`: configurable seconds in the valid Speechmatics range **0.7–4.0** (default **2.0**, the docs-recommended trade-off). Values outside the range are rejected at startup.
- `model`: configurable (`standard` | `enhanced`, default `enhanced`), passed via the modern `model` parameter instead of the deprecated `operating_point`.
- `max_delay_mode`: default `flexible` so spoken entities (numbers, doses) are formatted completely before the final is emitted.

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

### Optional: the Pynini/OpenFst backend

`pynini` is **optional** and is only installed automatically on Linux x86_64,
because it publishes Linux-only wheels. On Windows and macOS `pip` would fall
back to the source distribution, which requires a preinstalled OpenFst
toolchain and a C++ compiler — that made `pip install -r requirements.txt`
fail and blocked the entire install.

The medical FST layer ships a deterministic pure-Python scanner that
implements the identical priority scheme (longest match, then tier, then
rule order) and is verified against the Pynini backend by the test suite, so
**the application is fully functional without Pynini**.

To use the OpenFst backend on Windows/macOS, install it via conda:

```powershell
conda install -c conda-forge pynini=2.1.6.post1
```

`MedicalFST.uses_pynini` and the startup output report which backend is
active.

> **PyAudio on Linux:** install the PortAudio headers first
> (`sudo apt install portaudio19-dev python3-dev`), otherwise the PyAudio
> build fails. Windows uses prebuilt wheels and needs no extra steps.

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

Select microphone and tune the session:

```powershell
.\.venv\Scripts\python.exe app.py --language fa --device-index 3 --model standard --max-delay 2.5 --save-report
```

Useful flags:

| Flag | Meaning |
| --- | --- |
| `--device-index N` | PyAudio input device index (default: system default) |
| `--model standard|enhanced` | Speechmatics model (default `enhanced`) |
| `--max-delay 0.7-4.0` | Final-transcript delay in seconds (default `2.0`) |
| `--max-delay-mode fixed|flexible` | Entity-aware final delay (default `flexible`) |
| `--save-report` | Save the text/metadata session JSON report |
| `--test-id <id>` | Benchmark case; automatically saves the report |

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

### 3. Test deterministic medical FST canonicalization

Run:

```powershell
.\.venv\Scripts\python.exe app.py --language fa
```

Compare the three pipeline stages (also available in the saved report):

- `final_transcript_raw` - true Speechmatics raw final (before normalization)
- `final_transcript_normalized` - generic text normalization only
- `final_transcript_canonical` - after the medical FST
- `medical_hits` - which FST rule fired where (form/canonical/tier/source/position)

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

### `medical_knowledge/fst_terms.json`

The curated FST rule set: phrases (`سی سی یو` -> `CCU`, `سی تی اسکن` -> `CT scan`), abbreviations (`آی وی` -> `IV`), units (`میلی گرم` -> `mg`, `درصد` -> `%`), Persianized-English pronunciations (`رایت لانگ` -> `right lung`, `هایپرتنشن` -> `hypertension`), and English ASR variants (`iv` -> `IV`, `ct scan` -> `CT scan`). Tiers define deterministic priority; the other knowledge files are merged into their own tiers by `speechmatics_test/fst.py`.

### `medical_knowledge/speechmatics_additional_vocab.json`

A compact vocabulary intended for the Speechmatics `additional_vocab` feature.

## Evaluation

Each saved report JSON contains:

```json
{
  "first_partial_latency_ms": 0,
  "final_transcript_raw": "...",
  "final_transcript_normalized": "...",
  "final_transcript_canonical": "...",
  "medical_hits": [],
  "evaluation": {
    "raw":         { "wer": 0.0, "number_accuracy": 1.0, "similarity": 1.0 },
    "normalized":  { "wer": 0.0, "number_accuracy": 1.0, "similarity": 1.0 },
    "fst_canonical": { "wer": 0.0, "number_accuracy": 1.0, "similarity": 1.0 }
  }
}
```

Tokenization is medical-aware: `20 mg`, `20mg`, `120/80`, `5.5`, `3,14`,
`O2`, `q2h`, `C3-C4`, `U/A`, `HbA1c` all tokenize as expected, and
number accuracy only counts numeric tokens (multiset-aware).

Persian/Arabic-Indic digits are folded to ASCII during normalization, so a
dose dictated as `20 mg` and returned by Speechmatics as `۲۰ mg` is scored as
**correct**. Before this, every Persian-digit number counted as an error and
`number_accuracy` collapsed to `0.0` — the most misleading possible metric for
a dosage-critical medical benchmark. Genuinely misrecognized values (`۱۱۰/۸۰`
vs `120/80`) are still counted as errors.

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
