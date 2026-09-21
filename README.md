# SwiftMedics — Speechmatics Mixed Persian/English Medical ASR v4

A production-oriented medical dictation app for Speechmatics Realtime with Persian, English, Persianized English medical words, nursing terminology, abbreviations, numbers, routes, and clinical expressions.

## What's new in v4

1. **Aho-Corasick post-processing engine** (replaces the OpenFst/Pynini transducer). All rule forms are learned **once** into a single automaton and then **all searched simultaneously** in one linear pass over the text — the fastest known approach for large rule sets, and it ships with Windows-friendly wheels (`pyahocorasick`) plus a built-in pure-Python automaton with identical output.
2. **Automatic injection — hotkeys and the manual countdown are gone.** Like [deepgram-v6](https://github.com/AmiraliGhamkhar/deepgram-v6), every *finalized* segment is pasted at the cursor immediately during the session (`Ctrl+V` clipboard paste, always with BiDi marks). Just click the target field once and dictate.
3. **Better injector** — mixed Persian/English payloads are wrapped in `RLM + RLE … PDF` and cleaned (whitespace collapse + ZWNJ repair) before pasting, so LTR-default EMR forms render RTL text correctly.
4. **Better overlay** — logical-order text with explicit Unicode direction controls (`RLM + RLE … PDF`) instead of visual-order pre-shaping, smart *base direction* detection based on the Persian/English character ratio (not just the first strong character), larger font, and a safer fallback chain.
5. **v4.1 — one consolidated medical dictionary.** The five split knowledge files were merged into a single validated source of truth, `medical_knowledge/medical_dictionary.json` (tiered, conflict-resolved at load with reported warnings, ZWNJ-aware normalization, one-time build). The matcher lives in `speechmatics_test/matcher.py` (`MedicalMatcher`, still exported as `MedicalFST` for API stability); the bounded Speechmatics `additional_vocab` is derived from the dictionary's `speechmatics: true` entries only. A pre-migration behavioral snapshot (`tests/fixtures/pre_migration_canonicalization.json`) proves the consolidated dictionary reproduces the old outputs exactly.

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
   +----> injection worker -> injector.paste_text   [AUTOMATIC, FIFO order]
   |       (canonical text + " ", RLE/PDF/RLM wrap, off the SDK thread)
   v
RAW / NORMALIZED / CANONICAL report
   (the canonical stage is the exact text that was injected)
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
- A handful of clinical short forms collide with common English words (`OR`, `P`, `NOW`, `DIFF`, `AC`, `PC`, `HS`, `OD`); they only fire on stronger evidence — the matched text must be fully uppercase (the conventional charting style) — so an ordinary sentence like "the patient is stable now" is left unchanged, while "NOW" (or the unambiguous Persian/spelled-out aliases for the same concepts) still canonicalizes. Unambiguous abbreviations (`MRI`, `CT`, `ECG`, `HbA1c`, `SpO2`, ...) keep normal case-insensitive matching.
- The cross-segment final-transcript accumulator (used by injection and the report) only holds text back when it is a genuine **strict** prefix of a longer rule (i.e. more tokens could still complete a longer match); a standalone complete phrase with no such longer sibling rule (e.g. the one-token abbreviation `IV`) is injected immediately instead of waiting for a continuation no rule defines.

## The Aho-Corasick engine

`speechmatics_test/matcher.py` — class `MedicalMatcher`, exported as `MedicalFST` for a stable API (the historical name predates the Aho-Corasick engine and is kept on purpose):

```text
medical_dictionary.json --(load+validate once)-->  rules (deduped, tier-resolved)
rules  --(build once)-->  Aho-Corasick automaton (goto/fail/output)
text   --(one pass)-->    raw matches -> token-boundary filter
                         -> longest match per position -> canonical text + hits
```

- **Native backend**: `pyahocorasick` (C extension, wheels for Windows/macOS/Linux), pinned in `requirements.txt`.
- **Fallback backend**: a self-contained pure-Python automaton (`AhoAutomaton`) that produces byte-identical output — verified by parity tests and used automatically when the native package is missing.
- **Degradation path**: if the engine ever fails mid-session, `canonicalize` falls back to the verified naive reference scanner, so a finished transcript is never lost to an engine bug.
- Speed is independent of the rule count: ~5x faster than the naive scanner at 2,000 rules and the gap widens with more rules. Matcher micro-benchmarks (build time, per-sentence latency, memory at ~100/500/1000/2000 terms): `scripts/run_matcher_benchmark.ps1` / `python benchmark/benchmark_matcher.py` (results from the consolidation refactor are committed under `benchmark/results_before.json` / `benchmark/results_after.json`).

## Injection (automatic — no hotkeys)

```powershell
.\\.venv\\Scripts\\python.exe app.py --language fa
```

1. Run the app. 2. Click the text field where the transcript must go **once**.
3. Dictate. Every finalized segment is canonicalized and pasted automatically.

- Paste is clipboard-based (`Ctrl+V`) — atomic per segment and free of keyboard-layout issues.
- RTL payloads are always wrapped as `RLM + RLE + text + PDF` (همیشه paste + BiDi marks) and cleaned (فاصله‌ها و ZWNJ قبل از inject تمیز می‌شوند) so mixed Persian/English renders correctly in LTR-default fields.
- A trailing space is appended after each segment (kept *inside* the BiDi embedding) so consecutive segments stay separated.
- **Injection never blocks ASR:** pastes run on a dedicated FIFO worker thread, in final order, instead of inside the Speechmatics receive callback.
- **Focus guard:** the first paste arms the window the user dictated into; later pastes are skipped while any other window is focused, so alt-tabbing can never paste medical text into the wrong application (disable with `--no-focus-guard`).
- **Clipboard is always restored** — including when the paste itself fails — and a user-held Ctrl is never released by the synthetic Ctrl+V.
- Medical phrases that span two final segments are canonicalized over the accumulated text (e.g. `فشار خون` + `بالا دارد` injects `HTN دارد`, not `BP بالا`), and the report's canonical stage is exactly the injected text.
- Injector internals are hardened for Windows: explicit 64-bit ctypes prototypes, clipboard ownership handling, `OpenClipboard` retries, modifier-key hygiene, paste-consumption settle, and a serializing lock for realtime callbacks.
- Disable with `--no-inject`.

## Overlay

- Larger, right/left-aligned automatically based on the detected direction.
- **Smart direction detection** uses the proportion of Persian/Arabic vs Latin letters (تشخیص هوشمند جهت بر اساس درصد فارسی/انگلیسی) with a first-strong-character tiebreak — Persian-dominant sentences that merely start with a Latin word (e.g. `CT scan بیمار …`) stay right-aligned.
- **Logical order everywhere.** The overlay never reorders characters: it renders `canonical_text` and only adds explicit Unicode direction controls (`RLM + RLE … PDF` for RTL-dominant text). Pre-shaping with `python-bidi.get_display()` double-applied the BiDi algorithm, since Tk already lays out BiDi text itself.
- Persian font fallback chain: Vazirmatn → Vazir → IRANSans → B Yekan → B Nazanin → Segoe UI → Tahoma → Arial.

## Speechmatics session configuration

- `domain`: `--domain auto` (default) sends `domain=medical` only for the languages Speechmatics documents for the Enhanced Medical model (Arabic, Danish, Dutch, English, Finnish, French, German, Norwegian, Spanish, Swedish). **Persian is not in that list**, so Persian sessions run on the Enhanced model *without* a domain and the app prints a notice; `--domain medical` forces it explicitly (for enterprise/private deployments), `--domain none` never sends it. Reports record both `domain_requested` and the domain actually sent.
- `max_delay`: configurable seconds in the valid Speechmatics range **0.7–4.0** (default **2.0**, the docs-recommended trade-off). This supports controlled runs at `2.0`, `2.5`, `3.0`, `3.5`, and `4.0` with the existing `--max-delay` flag.
- `model`: configurable (`standard` | `enhanced`, default `enhanced`), passed via the modern `model` parameter.
- `max_delay_mode`: default `flexible` so spoken entities (numbers, doses) are formatted completely before the final is emitted.
- `additional_vocab` is intentionally curated and bounded: high-value drugs, diseases, procedures, imaging, labs, anatomy, abbreviations, dosage units, compact entities (`HbA1c`, `O2`, `C3-C4`, `q2h`), and observed Persianized pronunciations. It is derived at startup from the dictionary's `speechmatics: true` entries only — never a dump of the full dictionary — and the rest of the dictionary stays local Aho-Corasick rules. `sounds_like` accepts short pronunciation phrases such as `M R I` and `ام آر آی`.

Final messages also retain first-alternative word `content`, `confidence`, `language`, and timing. Saved reports add `speechmatics` (with `domain`/`domain_requested`), `word_results`, `confidence_summary`, `parse_warnings` (non-fatal transcript-metadata problems — the transcript itself is always kept), and `audio_overflow_events` (suspected dropped microphone samples); established report fields remain unchanged. Confidence/language evidence can annotate a validated lexical hit and only breaks an otherwise equal lexical tie—it never creates a medical correction on its own.

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
| `--no-focus-guard` | Allow injection into whatever window is focused (default: armed target only) |
| `--device-index N` | PyAudio input device index (default: system default) |
| `--model standard\|enhanced` | Speechmatics model (default `enhanced`) |
| `--max-delay 0.7-4.0` | Final-transcript delay in seconds (default `2.0`) |
| `--max-delay-mode fixed\|flexible` | Entity-aware final delay (default `flexible`) |
| `--domain auto\|medical\|none` | `auto` sends `medical` only where Speechmatics documents it (not for `fa`) |
| `--save-report` | Save the text/metadata session JSON report |
| `--test-id <id>` | Benchmark case; automatically saves the report |

## Test protocol

1. **ASR baseline:** `--no-vocab --no-medical-layer`, speak naturally.
2. **Vocabulary test:** default run, repeat the exact same speech.
3. **Canonicalization test:** compare `final_transcript_raw` / `_normalized` / `_canonical` in the report, plus `medical_hits` (which rule fired where).
4. **Benchmark:** the expected transcript must reflect what was actually spoken; do not compare a 60-second exploration against a one-sentence expectation.

## Medical dictionary

`medical_knowledge/medical_dictionary.json` is the **single source of truth** for the medical layer. One entry per canonical term; every entry carries an explicit priority `tier` and is validated at startup (invalid entries fail loudly, never silently):

```json
{
  "id": "htn",
  "canonical": "HTN",
  "type": "abbreviation",
  "tier": "curated",
  "forms": ["فشار خون بالا", "اچ تی ان", "ایچ تی ان"],
  "speechmatics": true,
  "sounds_like": ["H T N", "اچ تی ان"],
  "source_file": "fst_terms.json"
}
```

- **tier** (required) — the deterministic priority order for conflicting canonicals: `curated` > `abbreviation` > `observed_alias` > `phrase` > `validated_term` > `unit`. These map 1:1 to the legacy source files (see below).
- **type** (required) — enumerated clinical category: `condition`, `drug`, `procedure`, `imaging`, `lab`, `anatomy`, `abbreviation`, `dosage_unit`, `route`, `vital_sign`, `phrase`, `term`.
- **forms** — normalized input forms (ZWNJ variants fold to the same form; punctuation forms are skipped with a warning). May be empty only for vocab-only entries (`speechmatics: true`), which bias ASR but never match.
- **speechmatics** — eligibility for the bounded `additional_vocab` (never automatic inclusion; the vocab stays a curated subset, not a dump of the dictionary).
- **sounds_like** — pronunciation hints handed to Speechmatics for vocab entries only; they never become matcher rules.
- **source_file** — optional migration traceability back to the legacy knowledge file.

The dictionary was consolidated from five legacy knowledge files, each into its own tier: `fst_terms.json` → `curated` (its unit-level rules → `unit`), `abbreviations.json` → `abbreviation`, `observed_asr_aliases.json` → `observed_alias`, `nursing_phrases.json` → `phrase`, `nursing_terms.json` → `validated_term`. Those files were removed after the migration was verified (a 432-case behavioral snapshot in `tests/fixtures/pre_migration_canonicalization.json` proves the consolidated dictionary reproduces the old outputs exactly, including hit positions); each term's `source_file` records its historical origin. `medical_knowledge/speechmatics_additional_vocab.json` is a generated artifact mirroring the derived vocabulary for inspection — regenerate it with `scripts/export_additional_vocab.py` (`--check` verifies sync).

## Evaluation

Each saved report JSON contains WER, number accuracy, and similarity for the raw / normalized / canonical stages. Tokenization is medical-aware: `20 mg`, `20mg`, `120/80`, `5.5`, `3,14`, `O2`, `q2h`, `C3-C4`, `U/A`, `HbA1c` all tokenize correctly. Persian/Arabic-Indic digits fold to ASCII during normalization so `۲۰ mg` scores as correct, while genuinely misrecognized values still count as errors.

`number_accuracy` is recall-only against the reference (kept exactly as-is for report/API stability): it does not penalize a hypothesis that also contains an extra, fabricated number, so reference `"20 mg"` vs hypothesis `"20 mg 50 mg"` still scores a perfect `1.0` there. Each evaluation also reports additive `number_precision`, `number_recall` (an alias of `number_accuracy`), and `number_f1` fields that DO catch that case (`number_precision` drops to `0.5` for the example above).

## Tests

```powershell
.\\.venv\\Scripts\\python.exe -m pytest -q
```

## Shell files

- `scripts/install.ps1` — create venv, install deps, seed `.env`
- `scripts/run.ps1` / `scripts/run.sh` — install + run (extra flags are passed through, e.g. `.\scripts\run.ps1 --language en`)
- `scripts/run_fa.ps1` / `scripts/run_en.ps1` — quick language runs
- `scripts/run_benchmark.ps1` — benchmark case
- `scripts/run_matcher_benchmark.ps1` — matcher micro-benchmark (dictionary build/latency/memory at ~100–2000 terms)
- `scripts/export_additional_vocab.py` — regenerate the Speechmatics `additional_vocab` artifact from the dictionary's `speechmatics: true` entries (`--check` verifies sync)
- `scripts/run_no_vocab.ps1` — vocabulary-disabled run
- `scripts/test_injector.ps1` — standalone injector smoke test

## Important Speechmatics design note

Speechmatics publishes separate realtime and multilingual capabilities. Do not interpret a successful Persian-stream test as proof of fully automatic Persian↔English realtime code-switching. This application measures the current API configuration empirically.

Official SDK/examples: https://github.com/speechmatics/speechmatics-academy
