# SwiftMedics — Persian/English Medical Dictation

Realtime medical dictation on **Speechmatics**, tuned for mixed Persian/English
nursing speech. Finalized speech is canonicalized against a medical dictionary,
normalized into clean clinical prose, and pasted straight into the window you
select as the target.

> **Normal mode = automatic finalized-text injection.** Every finalized,
> post-processed segment is pasted into the armed target cursor/window
> **without requiring a hotkey** — no Enter, no Space, no Ctrl+V, no
> countdown. The app is *not* a preview-only transcriber: transcription and
> injection happen together, automatically.

Everything after the ASR is **deterministic and auditable** — no LLM, no
embeddings, no generative correction. Audio is never written to disk.

---

## Quick Start (Windows / PowerShell)

```powershell
cd C:\path\to\speechmatics-fa-speech
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1
notepad .env
```

Put your key in `.env`:

```text
SPEECHMATICS_API_KEY=YOUR_SPEECHMATICS_API_KEY
```

Then dictate:

```powershell
.\scripts\run_fa.ps1          # Persian
.\scripts\run_en.ps1          # English
```

The normal dictation flow is:

```text
1. Start SwiftMedics  ->  it asks you to click the destination field
2. Click inside the target application (Word / EMR / browser field / editor)
                      ->  the app sees the foreground window change and ARMS
                          that window; dictation starts automatically
3. Speak              ->  Speechmatics streams realtime results
4. Each final segment ->  normalize -> medical canonicalization
                          -> nursing normalization (numbers/times/units)
5. Automatic injection of the finalized text at the armed cursor
6. Repeat             ->  segments paste in spoken (FIFO) order
7. Ctrl+C in the console to stop
```

Step 5 needs **no keypress at all** — no hotkey, no countdown, no manual
re-arm. Arming happens once, *before* the realtime session begins producing
text, so the first finalized segment — even one the post-processor briefly
buffers to complete a medical phrase or a spoken number — is injected into
the intended window. The target is the window you switch focus **to**; the
SwiftMedics console itself is never armed. Press `Ctrl+C` in the console to
stop (it also aborts the wait if you change your mind).

> **Requires Python 3.11+.** The project is validated with Python 3.11 and
> `speechmatics-rt==1.1.1`. `install.ps1` prefers an existing project
> interpreter, then Python 3.11, then another compatible Python, creates
> `.venv`, and installs `requirements.txt`. If PowerShell blocks the script, the
> `Set-ExecutionPolicy` line above unblocks it for that window only.

---

## What It Does

```text
Microphone → Speechmatics → partial ─────────────→ Overlay
                             │
                             final
                             ↓
              generic normalization
                             ↓
              bounded streaming accumulator
              (final-boundary pending tails)
                             ↓
              medical canonicalization   (Aho-Corasick)
                             ↓
              nursing text normalization (numbers, times, units, format)
                             ↓
              canonical text ─→ Overlay / paste into app / JSON report
```

Partials are display-only. Only finalized segments are post-processed. The
accumulator retains only a small unresolved suffix, allowing medical phrases,
spoken numbers, clock times, ratios, and protected stutter handling to remain
correct when Speechmatics places a final-segment boundary inside them.

**Example.** Spoken:

```text
مددجو آقای سی و پنج ساله با درد قفسه سینه در ساعت ده و سی دقیقه وارد بخش قلب شد
```

Output:

```text
مددجو آقای 35 ساله با chest pain در ساعت 10:30 وارد بخش قلب شد
```

The result is continuous nursing documentation, not a filled-in template.

---

## Common Commands

All tooling is one script — `scripts\swiftmedics_tools.py`:

```powershell
.\.venv\Scripts\python.exe scripts\swiftmedics_tools.py --help
```

| Command | What it does |
| --- | --- |
| `install` | Create `.venv`, install `requirements.txt` |
| `run-fa` / `run-en` | Start Persian / English dictation |
| `run` | Run `app.py` with your own arguments |
| `benchmark` | Accuracy benchmark — offline, no API key |
| `benchmark-matcher` | Matcher speed/memory scaling |
| `export-vocab` | Rebuild the Speechmatics vocabulary file |
| `check-vocab` | Verify that file matches the dictionary |
| `audit-dictionary` | Report duplicates, invalid entries, metadata drift |
| `test-injector` | Paste-into-app smoke test |

The `.ps1` / `.sh` files are thin wrappers around these subcommands, kept so
existing commands keep working. They contain no logic of their own.

### Useful flags

```powershell
.\.venv\Scripts\python.exe app.py --language fa --no-inject
```

| Flag | Effect |
| --- | --- |
| `--no-inject` | Don't paste; transcribe only |
| `--no-overlay` | Hide the on-screen transcript |
| `--no-vocab` | Don't send the custom vocabulary to Speechmatics |
| `--no-medical-layer` | Skip medical canonicalization |
| `--no-text-polish` | Skip numbers/times/units/formatting |
| `--no-focus-guard` | Skip target selection; paste wherever focus is |
| `--save-report` | Write a session JSON to `results\` |
| `--device-index N` | Pick a specific microphone |
| `--model standard\|enhanced` | Speechmatics model |
| `--max-delay 0.7-4.0` | How long to wait before finalizing |
| `--domain auto\|medical\|none` | Speechmatics domain |

---

## Medical Canonicalization

Shorthand and Persianized pronunciations become charted terms:
`سی تی اسکن` → `CT scan`, `فشار خون بالا` → `HTN`, `لاین وریدی` → `IV line`.

The layer is deliberately conservative. It is **lexical only** — it never
infers diagnosis, severity, negation, or dosage correctness.

* Matching is token-aware and boundary-safe (`ivory` never matches `IV`).
* Longest match wins; ties resolve by tier, deterministically.
* Abbreviations that collide with ordinary English (`it`, `us`, `cold`, `pt`)
  fire only on uppercase charting forms, so plain prose is left alone.
* Phrases spanning two finalized segments are still matched.
* Every hit is auditable: form, canonical, tier, source, position.

Source: `speechmatics_test\matcher.py`. `MedicalFST` remains as an alias.
The native backend is `pyahocorasick`; a pure-Python fallback produces
identical output if it isn't installed.

---

## Nursing Text Normalization

Turns dictated speech into charted form (`speechmatics_test\nursing_text.py`).

| Spoken | Output |
| --- | --- |
| `سی و پنج ساله` | `35 ساله` |
| `ده و نیم` / `ساعت ده سی` | `10:30` / `ساعت 10:30` |
| `صد و چهل روی هشتاد و پنج` | `140/85` |
| `میلی متر جیوه` | `mmHg` |
| `درجه سانتی گراد` | `°C` |
| `نمره نمره` | `نمره` |
| `می باشد. باشد.` | `می‌باشد.` |
| `Temp . 36.7. °C` | `Temp: 36.7 °C` |
| `blood pressure. 140/85 mmHg` | `BP: 140/85 mmHg` |

### Safety rules

* **Never invents or drops a number.** Anything ambiguous is left exactly as
  spoken and flagged. `سی و شش و هفت` (dictated 36.7) is *not* summed into 43 —
  it stays verbatim with a warning.
* **Ambiguous cardinals need context.** `یک`, `ده`, `سی`, `نه`, `دو` convert
  only when a counter or explicit `و`-run makes the reading certain, so
  `سی تی اسکن` and `یک بیمار` are untouched.
* **Repetition cleanup is repetition-safe.** `سی سی یو` (CCU) and `آر آر` (RR)
  legitimately repeat a syllable and are protected.
* **Canonical text stays in logical Unicode order.** BiDi controls belong only
  to the overlay and the injector, never to stored text.

Warnings appear in the console banner and under `text_polish_warnings` in the
report JSON. Disable the whole stage with `--no-text-polish`.

---

## Vocabulary

`medical_knowledge\medical_dictionary.json` is the single source of truth
(**968 terms**). Entries marked `"speechmatics": true` are exported to
`speechmatics_additional_vocab.json` (**136 entries**) and sent to the ASR as
biasing hints, with spoken Persian forms attached as `sounds_like`.

After editing the dictionary:

```powershell
.\.venv\Scripts\python.exe scripts\swiftmedics_tools.py export-vocab
.\.venv\Scripts\python.exe scripts\swiftmedics_tools.py check-vocab
```

`check-vocab` fails if the file and the dictionary disagree.

---

## Testing & Benchmarks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

**561 tests, all passing.**

```powershell
.\.venv\Scripts\python.exe scripts\swiftmedics_tools.py benchmark
```

107 nursing fixtures, offline, no API key. These measure deterministic
**post-processing**, not Speechmatics recognition accuracy: there is no audio
or ASR request in this benchmark. The output separately reports whole-text
fixtures and streaming variants that replay every fixture through the
production `FinalStreamCanonicalizer` (the same accumulator that feeds
automatic injection) at synthetic final-segment boundaries — after token 1,
mid-way, and before the last token, plus a one-final control. It writes
`benchmark\results_current.json` and regenerates `benchmark\README.md` from
the measured run — no number in it is hardcoded. The run embeds the actual
Git commit, Python version, platform, and measured library versions, so the
artifact always identifies what produced it.

Accuracy is reported **per stage**, so a formatting fix is never presented as a
terminology improvement:

| Stage | Baseline | Current |
| --- | --- | --- |
| generic normalization | 100% | 100% |
| medical canonicalization | 70.0% | 100% |
| number / time / unit | 35.1% | 100% |
| grammar / format | 8.3% | 100% |
| full paragraphs | 11.8% | 100% |
| **exact match overall** | **45/107 (42.1%)** | **107/107 (100%)** |

Terminology F1 0.78 → 1.0, number F1 0.80 → 1.0, false-number rate
0.029 → 0.0, with no latency or memory regression. Full detail and the exact
baseline definition are in `benchmark\README.md`.

Matcher speed/memory scaling:

```powershell
.\scripts\run_matcher_benchmark.ps1
```

### Measuring real ASR accuracy

Compare three runs and read `final_transcript_raw`,
`final_transcript_normalized`, `final_transcript_canonical`, and
`medical_hits` from the report:

```powershell
.\.venv\Scripts\python.exe app.py --language fa --no-vocab --no-medical-layer   # ASR baseline
.\.venv\Scripts\python.exe app.py --language fa --no-medical-layer              # + vocabulary
.\.venv\Scripts\python.exe app.py --language fa                                 # full pipeline
```

The reference must be what you actually said. Comparing a long recording
against a short reference sentence produces a meaningless score.

---

## Injection & Overlay

**Automatic injection is the normal mode** (`--inject` is the default).
Finalized segments — never partials — go to a FIFO worker thread, so
clipboard work never blocks the Speechmatics receive callback and segments
always paste in spoken order. The injector does atomic per-segment paste,
BiDi wrapping, ZWNJ/whitespace cleanup, clipboard save/restore,
modifier-key protection, focus protection, and Windows clipboard retry
handling. Injection and display are separate outputs: the overlay can be
on (`default`) or off (`--no-overlay`) without changing injection, and
`--no-inject` (transcribe/preview only) never hides the overlay.

If a paste fails (e.g. the clipboard is wedged open by another app, or the
focus guard refused it), the segment is **not** counted as delivered: a
warning is printed immediately, the failure is recorded in the session
report's `injection.segments`, and the FIFO stream continues.

### Focus guard

At startup the app waits for you to **select** the destination: it records
which window is foreground, then arms the first *different* window that
takes focus (`await_target()` runs before the realtime session starts).
Arming the window that is already focused would arm the SwiftMedics console
itself, after which every paste is refused — that is the failure this flow
exists to prevent.

Each Windows paste re-validates focus twice: once before touching the
clipboard, and once more *immediately before the Ctrl+V keystroke* — a
clipboard preparation takes long enough that alt-tabbing mid-paste would
otherwise leak chart text into the wrong application. A refused paste is
reported as failed (never silently "delivered").

The guard is **fail-closed**: if no target is ever selected (or the
platform exposes no foreground-window API), nothing is pasted anywhere and
the session says so. Transcription and the report are unaffected.

Targeting is **top-level window** granularity. The armed handle is the
foreground window, so moving between fields *inside* the same application
(e.g. two boxes of one EMR form) is allowed and pastes at the current
caret; SwiftMedics does not track focus at the individual control level
and does not use UI Automation or accessibility APIs.

`--no-focus-guard` disables exactly this: nothing is armed and the paste
goes to whatever is focused at paste time. Use it only when you
deliberately want to dictate into a moving focus target.

### Overlay

The overlay renders logical-order text and picks direction from the
Persian-to-Latin character ratio, so `CT scan بیمار دارای ضایعه است` reads
correctly without applying BiDi twice. It shows partials live, marks
injected segments as done, and never rewrites medical content. Font
fallback: Vazirmatn → Vazir → IRANSans → B Yekan → B Nazanin → Segoe UI →
Tahoma → Arial.

---

## Speechmatics Notes

Defaults: language `fa`, model `enhanced`, max delay `2.0s` flexible, domain
`auto`, medical layer / vocabulary / injection / overlay all on.

Speechmatics' Enhanced **Medical domain does not currently list Persian**, so
`--domain auto` does not request it for `fa`. Use `--domain medical` only if
your deployment explicitly supports it.

A working Persian session is not by itself proof of automatic Persian-English
code-switching. Test mixed speech under your exact model, language, vocabulary,
and domain settings.

* Speechmatics Academy — https://github.com/speechmatics/speechmatics-academy
* Python SDK — https://github.com/speechmatics/speechmatics-python-sdk

---

## Project Layout

```text
app.py                            CLI entry point

speechmatics_test/
    matcher.py                    Aho-Corasick medical matcher
    nursing_text.py               numbers, times, units, formatting
    medical_layer.py              facade used by the app
    realtime.py  presentation.py  evaluation.py  text.py

medical_knowledge/
    medical_dictionary.json               968 terms (source of truth)
    speechmatics_additional_vocab.json    136 generated vocab entries

scripts/
    swiftmedics_tools.py          all tooling logic lives here
    _dictionary_fixes.py          idempotent, audited dictionary repairs
    *.ps1  run.sh                 thin wrappers, kept for compatibility

benchmark/
    dataset.py                    107 frozen nursing fixtures
    run_benchmark.py              accuracy + performance runner
    benchmark_matcher.py          matcher scaling
    README.md                     generated from the measured run
    results_baseline.json  results_current.json  results_comparison.json

tests/                            561 tests
```

---

## Design Constraints

Deliberately **not** used: LLM post-processing, embeddings, vector databases,
generative correction, audio recording, or accumulation of partial transcripts.

The medical layer is a lexical canonicalization system, not a clinical
reasoning system. When it cannot resolve something safely, it leaves the text
alone and says so.
