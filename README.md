# SwiftMedics — Speechmatics Mixed Persian/English Medical ASR v4

A production-oriented medical dictation app for Speechmatics Realtime with Persian, English, Persianized English medical words, nursing terminology, abbreviations, numbers, routes, and clinical expressions.

## What's new in v4

1. **Aho-Corasick post-processing engine** (replaces the OpenFst/Pynini transducer). All rule forms are learned **once** into a single automaton and then **all searched simultaneously** in one linear pass over the text — the fastest known approach for large rule sets, and it ships with Windows-friendly wheels (`pyahocorasick`) plus a built-in pure-Python automaton with identical output.
2. **Automatic injection — hotkeys and the manual countdown are gone.** Like [deepgram-v6](https://github.com/AmiraliGhamkhar/deepgram-v6), every *finalized* segment is pasted at the cursor immediately during the session (`Ctrl+V` clipboard paste, always with BiDi marks). Just click the target field once and dictate.
3. **Better injector** — mixed Persian/English payloads are wrapped in `RLM + RLE … PDF` and cleaned (whitespace collapse + ZWNJ repair) before pasting, so LTR-default EMR forms render RTL text correctly.
4. **Better overlay** — `python-bidi` visual-order shaping, smart direction detection based on the Persian/English character ratio (not just the first strong character), larger font, and a safer fallback chain.

## Pipeline

```text
Microphone (16 kHz mono PCM16, in-memory chunks only)
   |
   v
Speechmatics Realtime (speechmatics-rt SDK)
   |
   +----> ADD_PARTIAL_TRANSCRIPT -> overlay.set_partial()   (UI only)
   |
   v
ADD_TRANSCRIPT (final segment, raw, preserved)
   |
   v
normalize_text() (generic)
   |
   v
Medical Aho-Corasick canonicalization
   |
   +----> overlay.set_final()
   +----> injector.paste_text(segment + " ", RLE/PDF/RLM wrap)   [AUTOMATIC]
   |
   v
RAW / NORMALIZED / CANONICAL report (+ benchmark evaluation on --test-id)
```

Hard rules enforced by the design:

- **No LLM, no embeddings, no vector DB, no generative post-processing.**
- **No audio persistence.** Microphone chunks exist only in memory while streaming. No WAV files, no temp audio. Only text/metadata JSON reports are written, and only when requested (`--save-report` or `--test-id`).
- **Partials are for the UI/overlay only.** They are never accumulated into final text.
- **Post-processing runs only on finalized ASR segments.**
- **Ctrl+C stops the recording, it does not discard the dictation.** SIGINT ends the audio stream gracefully; the session is closed normally and the report stages still run.

## Why the canonicalization layer is conservative

It is **deterministic lexical canonicalization only**. It does not infer diagnosis, severity, negation, dosage correctness, or clinical meaning.

- Rules are exact, token-aware matches (word boundaries), never unsafe substring replacement.
- Longest match wins; ties are broken by tier (curated rules > abbreviations > observed aliases > phrases > validated terms > units), then by stable rule order.
- Every hit reports `form`, `canonical`, `tier`, `source`, and `position` for auditability.
- If the automaton engine ever fails, the layer degrades to a verified reference scanner — the finished transcript is never lost to an engine bug.

## The Aho-Corasick engine

`speechmatics_test/fst.py` (class name kept as `MedicalFST` for a stable API):

```text
rule forms  --(build once)-->  Aho-Corasick automaton (goto/fail/output)
text        --(one pass)-->    raw matches -> token-boundary filter
                              -> longest/tier/order resolution -> canonical text + hits
```

- **Native backend**: `pyahocorasick` (C extension, wheels for Windows/macOS/Linux).
- **Fallback backend**: a self-contained pure-Python automaton (`AhoAutomaton`) that produces byte-identical output — verified by parity tests and used automatically when the native package is missing.
- Speed is independent of the rule count: ~5x faster than the naive scanner at 2,000 rules and the gap widens with more rules.

## Injection (automatic — no hotkeys)

```powershell
.\\.venv\\Scripts\\python.exe app.py --language fa
```

1. Run the app. 2. Click the text field where the transcript must go **once**.
3. Dictate. Every finalized segment is canonicalized and pasted automatically.

- Paste is clipboard-based (`Ctrl+V`) — atomic per segment and free of keyboard-layout issues.
- RTL payloads are always wrapped as `RLM + RLE + text + PDF` (همیشه paste + BiDi marks) and cleaned (فاصله‌ها و ZWNJ قبل از inject تمیز می‌شوند) so mixed Persian/English renders correctly in LTR-default fields.
- A trailing space is appended after each segment (kept *inside* the BiDi embedding) so consecutive segments stay separated.
- Injector internals are hardened for Windows: explicit 64-bit ctypes prototypes, clipboard ownership handling, `OpenClipboard` retries, modifier-key hygiene, paste-consumption settle, and a serializing lock for realtime callbacks.
- Disable with `--no-inject`.

## Overlay

- Larger, right/left-aligned automatically based on the detected direction.
- **Smart direction detection** uses the proportion of Persian/Arabic vs Latin letters (تشخیص هوشمند جهت بر اساس درصد فارسی/انگلیسی) with a first-strong-character tiebreak — Persian-dominant sentences that merely start with a Latin word (e.g. `CT scan بیمار …`) stay right-aligned.
- **python-bidi** shapes the display string into visual order when Tk's own BiDi shaping would mis-order mixed text; the app degrades gracefully when the package is missing.
- Persian font fallback chain: Vazirmatn → Vazir → IRANSans → B Yekan → B Nazanin → Segoe UI → Tahoma → Arial.

## Speechmatics session configuration

- `max_delay`: configurable seconds in the valid Speechmatics range **0.7–4.0** (default **2.0**, the docs-recommended trade-off). Values outside the range are rejected at startup.
- `model`: configurable (`standard` | `enhanced`, default `enhanced`), passed via the modern `model` parameter.
- `max_delay_mode`: default `flexible` so spoken entities (numbers, doses) are formatted completely before the final is emitted.

## Install — Windows PowerShell

```powershell
cd C:\path\to\speechmatics_v3
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1
notepad .env
```

Set:

```text
SPEECHMATICS_API_KEY=YOUR_SPEECHMATICS_API_KEY
```

> **PyAudio on Linux:** install the PortAudio headers first
> (`sudo apt install portaudio19-dev python3-dev`); Windows uses prebuilt wheels.

## Run

Persian (auto-injection on by default):

```powershell
.\\.venv\\Scripts\\python.exe app.py --language fa
```

English:

```powershell
.\\.venv\\Scripts\\python.exe app.py --language en
```

Benchmark case (auto-saves a report):

```powershell
.\\.venv\\Scripts\\python.exe app.py --language fa --test-id mix_ct_lesion
```

Useful flags:

| Flag | Meaning |
| --- | --- |
| `--no-inject` | Disable automatic per-segment injection |
| `--no-overlay` | Disable the floating transcript overlay |
| `--no-vocab` | Disable Speechmatics custom vocabulary |
| `--no-medical-layer` | Disable the canonicalization layer |
| `--device-index N` | PyAudio input device index (default: system default) |
| `--model standard\|enhanced` | Speechmatics model (default `enhanced`) |
| `--max-delay 0.7-4.0` | Final-transcript delay in seconds (default `2.0`) |
| `--max-delay-mode fixed\|flexible` | Entity-aware final delay (default `flexible`) |
| `--save-report` | Save the text/metadata session JSON report |
| `--test-id <id>` | Benchmark case; automatically saves the report |

## Test protocol

1. **ASR baseline:** `--no-vocab --no-medical-layer`, speak naturally.
2. **Vocabulary test:** default run, repeat the exact same speech.
3. **Canonicalization test:** compare `final_transcript_raw` / `_normalized` / `_canonical` in the report, plus `medical_hits` (which rule fired where).
4. **Benchmark:** the expected transcript must reflect what was actually spoken; do not compare a 60-second exploration against a one-sentence expectation.

## Nursing terminology structure

- `medical_knowledge/nursing_terms.json` — rows converted from the nursing spreadsheet.
- `medical_knowledge/abbreviations.json` — BP, HTN, DM, IV, IM, SC, ICU, CCU, ECG, ABG, NPO, CPR, FBS, CBC, U/A, CXR, PRN, q2h, tds, …
- `medical_knowledge/nursing_phrases.json` — phrase-level nursing expressions (vital signs, blood pressure, oxygen saturation, nothing by mouth, …).
- `medical_knowledge/observed_asr_aliases.json` — observed forms from uploaded sessions (evidence for the dataset, not clinical ground truth).
- `medical_knowledge/fst_terms.json` — the curated rule set (phrases, abbreviations, units, Persianized-English pronunciations, English ASR variants) with explicit deterministic priority tiers. Other knowledge files merge into their own tiers.
- `medical_knowledge/speechmatics_additional_vocab.json` — compact vocabulary for the Speechmatics `additional_vocab` feature.

## Evaluation

Each saved report JSON contains WER, number accuracy, and similarity for the raw / normalized / canonical stages. Tokenization is medical-aware: `20 mg`, `20mg`, `120/80`, `5.5`, `3,14`, `O2`, `q2h`, `C3-C4`, `U/A`, `HbA1c` all tokenize correctly. Persian/Arabic-Indic digits fold to ASCII during normalization so `۲۰ mg` scores as correct, while genuinely misrecognized values still count as errors.

## Tests

```powershell
.\\.venv\\Scripts\\python.exe -m pytest -q
```

## Shell files

- `scripts/install.ps1` — create venv, install deps, seed `.env`
- `scripts/run.ps1` / `scripts/run.sh` — install + run
- `scripts/run_fa.ps1` / `scripts/run_en.ps1` — quick language runs
- `scripts/run_benchmark.ps1` — benchmark case
- `scripts/run_no_vocab.ps1` — vocabulary-disabled run
- `scripts/test_injector.ps1` — standalone injector smoke test

## Important Speechmatics design note

Speechmatics publishes separate realtime and multilingual capabilities. Do not interpret a successful Persian-stream test as proof of fully automatic Persian↔English realtime code-switching. This application measures the current API configuration empirically.

Official SDK/examples: https://github.com/speechmatics/speechmatics-academy
