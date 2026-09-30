# SwiftMedics — Speechmatics Mixed Persian/English Medical ASR v4

Production-oriented medical dictation for **Speechmatics Realtime**, optimized for Persian/English clinical speech, medical terminology, abbreviations, numbers, dosages, routes, and nursing expressions.

## Highlights

* **Aho-Corasick medical matcher** — deterministic lexical canonicalization with a native `pyahocorasick` backend and a pure-Python fallback.
* **Single medical dictionary** — `medical_knowledge/medical_dictionary.json` is the source of truth for canonicalization and Speechmatics vocabulary.
* **Automatic injection** — finalized segments are pasted directly into the focused application; no hotkeys or manual countdown.
* **Clean logical-text injection** — pasted Persian/English text is normalized without storing hidden Unicode BiDi controls; direction wrapping is limited to the overlay (the injector API retains an explicit compatibility option).
* **Realtime-safe injection** — clipboard operations run on a dedicated FIFO worker and never block the ASR callback.
* **Focus guard** — prevents text from being injected into an unintended window.
* **Overlay** — logical-order rendering with automatic Persian/English direction detection.
* **No LLM / embeddings / vector DB** — all post-processing is deterministic and auditable.
* **No audio persistence** — microphone audio remains in memory only.
* **Audit-ready reports** — raw, normalized, canonical, confidence, language, timing, medical-match metadata, and non-destructive low-confidence review flags can be saved as JSON.

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
| `مورس چهل و پنج`, `برادن بیست` | `مورس 45`, `برادن 20` |
| `ای ام`, `پی ام`, `A.M.`, `P.M.` | `AM`, `PM` |
| `دوز دو هزار میلی گرم`, `پلاکت صد و پنجاه هزار` | `دوز 2000 mg`, `پلاکت 150000` |
| `هزار و دویست میلی گرم`, `دوز دو هزار و پانصد` | `1200 mg`, `دوز 2500` |
| `دمای بدن سی و هفت و هشت دهم`, `کراتینین یک و دو دهم` | `Temp 37.8`, `کراتینین 1.2` |
| `کراتینین سه و پنج صدم`, `دمای بدن سی و هشت و نیم` | `کراتینین 3.05`, `Temp 38.5` |

Deliberate limits:

* A numeral is folded only next to an anchor (`ساعت`, `سن`, `دوز`, `میلی گرم`,
  `BP`, `SpO2`, `درصد`, ...). `vivid`, `ivory`, `درد روی سینه` and
  `یک ضایعه در ریه` are left untouched.
* A number group is all-or-nothing: `پنج و شش ساله` or `صد و بیست و هشتاد` stay
  spoken rather than being half-converted, and a malformed thousand group
  (`دو هزار هزار`) stays spoken in full - no part of it is digitized alone.
  A bare `هزار` is folded only in front of a measured unit (`هزار میلی لیتر` ->
  `1000 mL`), never in front of a duration or a count of repetitions
  (`هزار سال پیش`, `هزار بار گفتم`, `چند هزار تومان`), and `دو هزار سال پیش`
  -> `2000 سال پیش` behaves exactly like the existing `پنج سال` rule.
  Larger magnitudes (`میلیون`, `میلیارد`) are deliberately NOT folded, but they
  still extend the group, so `پلاکت چهار میلیون` stays spoken instead of being
  half-converted to `پلاکت 4 میلیون` - a value nobody dictated.
  The connector `روی` only anchors a right-hand number when a numeric left
  side exists, so a corrupt ASR fragment such as `MRI روی هشتاد و پنج` is not
  silently reinterpreted as a measurement.
* A dictated decimal arrives in three shapes and all three fold to the exact
  value: implicit tenths (`یک و هشت` -> `1.8`, a lone unit digit followed by
  another), a spoken half (`سی و هشت و نیم` -> `38.5`), and an explicit
  fraction denominator (`سی و هفت و هشت دهم` -> `37.8`, `سه و پنج صدم` ->
  `3.05`). An explicit denominator outranks the implicit reading, because the
  speaker named the fraction: `سی و هشت دهم` is thirty and eight tenths
  (`30.8`), not `38` with the word "tenths" stranded beside it. The
  denominator word is consumed with the value it spells, so the output never
  contains a digit next to `دهم`/`صدم`/`هزارم` - the self-contradictory
  `Temp 38 دهم` and `glucose 125 دهم` were fabricated values, and the test
  suite fails if one reappears.
* A numeral run must descend by magnitude class (hundreds -> tens -> units),
  which is how Persian numbers are built. Two numerals of the same class
  spoken back to back are two separate numbers and are never added together:
  `دوز نهصد سیصد` stays spoken instead of becoming a `1200` nobody dictated,
  and neither half is digitized on its own.
* A ratio is exactly two numbers. A chain (`120 روی 80 روی 60`) is not a blood
  pressure, its middle value belongs to both halves, and it is left exactly as
  dictated rather than folded into a fabricated `120/8080/60`.
* Durations are not clock readings: `دو و نیم ساعت` and `ساعت هشت و نیم ساعت`
  stay as spoken; `دقیقه` alone never anchors a number.
* Existing charting notation is never reformatted: `08:30`, `120/80`, `20 mg`,
  `98 %`, `12 PM`, `ساعت 08`, `۲۵ مارس ۲۰۲۵` (whose digits still fold) pass
  through unchanged, and the fold is idempotent.
* No calendar logic, no 12/24-hour arithmetic, no date math, no dose or
  severity inference. Trailing punctuation is preserved, never consumed.

Clinical abbreviations and aliases that collide with common English words
require stronger evidence. `US`, `IT`, `ID`, `BE`, `HIM`, `MR`, `TOP`, `CAT`,
`COLD`, `AM`/`PM` and the charted dosage forms (`CAP`, `TABS`, `SOL`, `SUSP`,
`SYR`, `UNG`, `RECT`, `UNITS`, `DROPS`, `PILL`, `LAB`, `POST`, `LYING`,
`SKIN`, `SOFT`, `DRAIN`, `ORAL`, `DAILY`, `STAT`, `PREP`, `REG`, `ANTE`,
`CUM`, `ANTIBIOTICS`) count as charting shorthand only when the original text
is fully uppercase or a number touches it (`8 am`). Lowercase occurrences are
ordinary English prose and are left byte-identical: `the skin is pale`,
`it was soft`, `drain the wound`, `the patient is lying in bed`,
`the lab results`, `I am tired today` and `the nurse gave him a glass of water`
survive the narrative path unchanged. Two collisions are documented rather
than solvable without a semantic layer: an all-caps ordinary English word
(`the US report`, `BE careful`, `the IT department`) is indistinguishable from
chart notation. `scripts/validate_dictionary.py` and the test suite fail if a
new alias appears without its guard.

Cross-segment matching is supported for phrases and numeric expressions that span finalized ASR segments. Only a bounded, potentially extendable suffix is retained (for example `پنجاه` + `و هشت` + `ساله`); ordinary text is emitted immediately, and the tail is always flushed at session end.

An emission is never cut *inside* a complete dictionary match, so a term that straddles a Speechmatics final boundary still canonicalizes as one unit (`سی بی سی و ای بی جی` -> `CBC و ABG`, not `CBC و ای بی جی`).

One dictated **value** is held together the same way. A measurement is
all-or-nothing, so the buffered suffix extends across the whole number group -
numerals, the joiner `و`, the magnitude `هزار`, a spoken half and an explicit
denominator - and across the unit it belongs to, whether that unit is already
complete (`میلی گرم`) or still only a prefix of one (`میلی`, with `گرم` in the
next final). `MedicalLayer.strict_prefix_canonical_heads` supplies the second
half: it reports what a held fragment could still become, which is how the cut
tells a pending *unit* (keep the number in front of it) from a pending *new
measurement phrase* (emit the completed value before it). Without this,
`دوز دو هزار و پانصد میلی گرم` was cut after `و` and injected as
`دوز 2000 و 500 mg` - one 2500 mg dose reported as two numbers, neither of
them 2500. The hold stays bounded by the same 12-token window, so a stream of
numeral-only finals still drains instead of accumulating the session.

Chart notation split by Speechmatics across two finals is reassembled at the boundary before the fold passes run: a final ending in `digits + ':'` or `digits + '.'` is joined (without spaces) to the next final's leading digits (`۱۰:` + `۳۰` -> `10:30`, `۳۶.` + `۷` -> `36.7`), and a final ending in digits is joined to a next final starting with `/digits` (`۱۴۵` + `/۹۰` -> `145/90`). The join is boundary-only and vocabulary-free: prose before or after the value is untouched, a fragment whose continuation never arrives is flushed verbatim, and two complete numbers in a row are left as two numbers.

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

The native backend uses `pyahocorasick`. A pure-Python implementation provides equivalent output when the native package is unavailable, and a naive reference scanner implements the identical priority scheme as the verified degradation path, so an engine failure never costs the clinician a finished transcript. All three agree on every rule form, every canonical and a randomized token corpus, in both scan modes.

### Canonical spans are fixed points

A canonical is the dictionary's own declaration of correct output, so feeding
it back through the matcher must return it unchanged. When a canonical
contained another rule's form, the scan used to discard the longest match as a
no-op instead of *claiming* its span, which let shorter rules fire inside an
already-correct phrase:

| Input (already canonical) | Before | Now |
| --- | --- | --- |
| `vitamin B12` | `vitamin vitamin B12` | `vitamin B12` |
| `PEG tube` | `PEG tube tube` | `PEG tube` |
| `nasogastric tube in place` | `nasogastric in place` (a word deleted) | `nasogastric tube in place` |
| `chronic obstructive pulmonary disease` | `chronic obstructive respiratory disease` | unchanged |
| `pulmonary embolism` | `respiratory embolism` | `pulmonary embolism` |
| `blood pH 7.4` | `blood past medical history 7.4` | `blood pH 7.4` |
| `past surgical history` | `past past surgical history` | `past surgical history` |
| `MR angiography` | `medical records angiography` | `MR angiography` |

`matcher.at_risk_canonicals` finds those canonicals and the loader registers
each as a self-mapping **claim rule** at the lowest tier: it can never outrank
a real rule, it contributes no prefix (so ordinary prose such as the leading
word `at` is never held back), and the scanner copies its span verbatim and
resumes after it. `scripts/validate_dictionary.py` reports the count and fails
if any multi-token canonical stops being a fixed point.

Two dictionary conflicts of the same class were fixed at the source, because
the loader had already been arbitrating them silently by tier:
`nasogastric tube`, `NG tube` and `NGT` were listed as forms of the *route*
`nasogastric`, which deleted the device noun from correct text, and
`once a day` was listed as a form of `every day`, which left the frequency
chain `OD` -> `once a day` -> `every day` non-idempotent. The device spellings
now belong to the `nasogastric tube` term and `once a day` is its own
canonical. Loader warnings fell from 170 to 167.

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

Speechmatics documents the Enhanced Medical domain for a specific set of languages (Arabic English, Danish, Dutch, English, Finnish, French, German, Norwegian, Spanish, Swedish). **Persian is not currently included in that documented list**, and further languages are available only by arrangement with Speechmatics.

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

For Persian, the accuracy levers that remain are the **custom dictionary**
(`additional_vocab`, see below) and `max_delay`.

### max_delay: latency vs accuracy

Speechmatics documents these trade-offs for realtime transcription (relative to
its Batch service, which is the accuracy ceiling):

| `max_delay` | Effect |
| --- | --- |
| 0.7-1.5s | Ultra-fast; <5% relative accuracy degradation |
| 2.0s (default) | Recommended balance; ~1% relative degradation |
| 4.0s | Accuracy equivalent to Batch |

Because partials are enabled and rendered live, raising `max_delay` costs
perceived latency much less than it costs real accuracy - the clinician sees
the partial immediately, while the *final* (and therefore injected) segment
becomes more accurate:

```text
--max-delay 3.0            # higher accuracy, finals arrive later
--max-delay-mode fixed     # only if entity formatting must never delay a final
```

`max_delay_mode flexible` (the default here) is what lets the engine finish a
spoken number before finalizing it, which is also why the cross-segment
canonicalizer has to handle values split across finals such as `10:` + `30`.

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

`sounds_like` is applied by Speechmatics **only in the session language's main
script**, so the same `_clean_vocab` filters pronunciations by the language
actually being streamed. On a Persian session the 4 Latin-script hints in the
export (`MRI`: `M R I`, `HbA1c`: `H B A one C`, `C3-C4`: `C three C four`,
`metformin`: `met for min`) cannot take effect and are removed, while the terms
themselves are still sent. An `--language en` session drops the 341 Persian
hints instead - the export is a Persian-stream artifact, so on the English
stream it biases content only. Every removal is recorded in the session
report's `vocabulary_notes`, so a report never implies a bias reached the ASR
when it did not. A pronunciation that is digits-only or a genuine mix of both
scripts is kept and left to the service's own validation: this filter decides
what to send, and an uncertain verdict should not silently discard data.

Server warnings are no longer dropped either. The SDK's own `Warning` handler
only writes to its logger, so `validation_warning` (an `additional_vocab` entry
the service rejected, sent in-band before `RecognitionStarted`), `idle_timeout`
and `duration_limit_exceeded` never reached the session report - a
pronunciation bias could silently fail to happen and a transcript could stop
early with no recorded reason. The adapter now subscribes to
`ServerMessageType.WARNING` and records each distinct warning in
`result.service_warnings`, kept separate from `result.warnings` (which is
strictly about our own message parsing). The subscription is resolved by name
rather than imported directly, so an SDK build without the member surfaces no
warnings instead of failing the session.
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

## Windows floating-button desktop app

The repository also includes a Windows desktop entry point intended for a
folder-bundled `.exe`. When launched, it shows only a small floating **Start**
button. While recording it expands to show **Stop** plus a live transcript;
clicking Stop ends recording, drains already-finalized injection jobs, and
returns to the Start state.

Desktop configuration is read from:

```text
%APPDATA%\SwiftMedics\config.json
```

Press Start once with no config to create a template, or create it manually:

```json
{
  "speechmatics_api_key": "YOUR_SPEECHMATICS_API_KEY",
  "language": "fa",
  "model": "enhanced",
  "max_delay": 2.0,
  "max_delay_mode": "flexible",
  "domain": "auto",
  "inject": true,
  "medical_layer": true,
  "medical_vocab": true,
  "focus_guard": true,
  "entity_guard": true,
  "device_index": null,
  "save_report": false
}
```

## Development

Build the developer folder bundle from Windows PowerShell (fallback packaging;
quick local tests, no demo expiry, no broker):

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\build_windows.ps1
```

Output:

```text
dist\SwiftMedics\SwiftMedics.exe
```

This path uses PyInstaller (`scripts/swiftmedics-pyinstaller.spec`), which
distributes ordinary Python bytecode — fine for development, not for handing
the app to a hospital. Use the Nuitka demo build below for that. Logs are
written to `%APPDATA%\SwiftMedics\logs\swiftmedics.log`.

### Building the .exe from CI

PyInstaller cannot cross-compile, so the bundle must be built **on Windows**.
`.github/workflows/build-windows.yml` does that on `windows-latest`: it installs
the pinned dependencies on Python 3.12, runs the test suite and the vocabulary
artifact check as gates, builds the bundle, and uploads `dist\SwiftMedics` as
the `SwiftMedics-windows` artifact. Push to `main`, open a pull request, or run
the **build-windows** workflow manually.

Two deployment notes for clinical environments:

* UPX packing is disabled in `scripts/swiftmedics-pyinstaller.spec` on purpose.
  Packed executables are a common source of antivirus false positives and
  cannot be reliably code-signed.
* The bundle is unsigned. Hospitals and EMR desktops often require a signed
  binary; sign the exe with your code-signing certificate before distribution.

## Hospital Demo Release

The hospital demo is a **Nuitka-compiled** build with the demo expiry and the
token broker URL compiled in. The long-lived Speechmatics API key never
reaches the hospital machine:

```text
Hospital PC
    │
    ├── SwiftMedics.exe
    │     ├── Nuitka-compiled application
    │     ├── NO long-lived Speechmatics API key
    │     ├── demo expiry compiled into the build
    │     └── broker URL compiled into the demo build
    │
    └── %APPDATA%\SwiftMedics\config.json
          └── optional DEMO_TOKEN

                 │ HTTPS
                 ▼

        Vercel Token Broker  (api/token.py)
                 │  SPEECHMATICS_API_KEY, server-side only
                 ▼
          Speechmatics Realtime
```

At every Start the app checks the compiled-in expiry (refusing expired builds
and builds whose system clock was rolled back), then exchanges the broker URL
for a **short-lived realtime JWT** (default 60 s) and holds it in memory for
that session only. The websocket endpoint is never taken from the broker — the
app always connects to Speechmatics directly.

Produce the demo bundle (one command, Windows PowerShell):

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\build_hospital_demo.ps1 `
    -ExpiryDate 2026-11-30 `
    -BrokerUrl https://YOUR-BROKER.vercel.app/api/token `
    -BuildId hospital-x-demo
```

Optional switches: `-DemoToken <value>` (pre-fills the config template),
`-ProductVersion 1.0.0.0` (Windows file metadata), `-SkipInstall`,
`-KeepStamp` (keep the generated stamp for repeated rebuilds).

The script creates `.venv`, installs dependencies + Nuitka, validates the
broker URL with the app's own runtime rules, generates the temporary stamp
(`speechmatics_test/demo_build_stamp.py` — git-ignored, compiled into the
build, deleted afterwards), runs the Nuitka standalone build (no console,
Windows version metadata embedded), collects
`dist\SwiftMedics-hospital-demo\`, writes `config-demo-template.json`, and
**fails the build** if the long-lived API key is found anywhere in the bundle.

The hospital machine needs **no Python, no Nuitka, no repository, and no API
key** — only the distributed folder and internet access. Before shipping:
code-sign the exe, and dry-run it on a clean Windows VM. See `DEMO.md` for the
full checklist and threat model.

Note on protection: Nuitka compiles the Python program to C and machine code,
so the bundle contains no ordinary Python bytecode of the application and
cannot be decompiled the way a PyInstaller bundle can. It does **not** make
reverse engineering impossible — a determined attacker can still analyze
native code — but it removes the easy path and substantially raises the
difficulty.

## Vercel Token Broker Deployment

The broker (`api/token.py`, standard library only) holds
`SPEECHMATICS_API_KEY` server-side and mints short-lived realtime tokens.
Deploy it before building any demo:

```bash
npm i -g vercel
vercel login
vercel link                      # from the repository root
vercel env add SPEECHMATICS_API_KEY production   # paste the private key
vercel env add DEMO_TOKEN production             # optional: shared secret; rotating it revokes shipped demos
vercel env add TOKEN_TTL production              # optional: seconds (10-300, default 60)
vercel --prod
# your broker is now at https://<your-app>.vercel.app/api/token
```

CORS is **disabled by default** (the client is a native Windows app); set
`ALLOWED_ORIGIN` only if you also serve a browser client from the same
broker. For an on-site demo without any cloud dependency, the same module can
run locally on the presenter machine (the key then stays on that machine):
`python api/token.py --serve 8787`.

Security flow, in one line: **Speechmatics API key → broker server only;
hospital EXE → short-lived token only.**

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
* Clean logical-Unicode paste by default (no stored RLM/RLE/PDF controls).
* Optional BiDi wrapping for direct API callers that need target-editor compatibility.
* ZWNJ/whitespace cleanup.
* Clipboard restoration.
* Modifier-key protection.
* Focus protection. The target window is armed from the first injected segment
  and every later paste verifies it is still focused. The app's own console is
  never armed: it is not a dictation target, and arming it would skip every
  subsequent paste.
* Honest per-segment `success: false` reporting when focus protection or paste fails.
* Windows clipboard retry/ownership handling.

Disable it with:

```powershell
.\.venv\Scripts\python.exe app.py --language fa --no-inject
```

## Overlay

The overlay renders **logical Unicode text** rather than pre-shaped visual text.

On Windows the overlay window is created with `WS_EX_NOACTIVATE` and hands the foreground focus back to the window that was focused before it appeared. It follows the cursor for readability but never becomes the keyboard-focus window, so the injector's focus guard arms the real dictation target and `[injector] focus changed - paste skipped` is not triggered by the overlay itself.

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

`number_accuracy` is retained for API compatibility as recall: it is
RECALL-only and cannot see a fabricated value, so it is never reported as
"accuracy" on its own. `number_precision`, `number_recall`, and `number_f1`
are the unambiguous numeric metrics.

`benchmark/benchmark_postprocess.py` reports the pipeline stages separately, so
a raw ASR score and a post-processing score are never conflated:

```text
raw                           - A: the stored ASR segments verbatim
normalized                    - B: generic normalization only
medical_canonicalization_only - C: the dictionary pass, numeric fold DISABLED
numeric_fold_only             - D: the number/clock/ratio fold, dictionary DISABLED
canonical_single_pass         - E: C then D over the whole transcript
canonical_streamed            - E': the same through the realtime final
                                   boundaries - the text actually injected
```

Every stage reports exact match, WER, entity P/R, and number
precision/recall/F1, and the benchmark proves the streamed path is
byte-identical to the single-pass path for every case.

C and D are measured apart because the merged stage cannot say which half
broke: a destroyed medical term and an invented dose look identical in E. They
are also **not independent** - D is deliberately gated by C, since the fold
refuses to digitize a spoken number without a recognized measurement anchor and
the anchors come from the dictionary pass (`فشار خون` -> `BP`,
`میلی گرم` -> `mg`). So `numeric_fold_only` scores below E by design, and that
gap *is* the guarantee that ordinary prose is never converted into numbers; a
test pins the relationship so it cannot invert unnoticed.

## Testing

Run the complete test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Validate the dictionary (bounded Speechmatics vocabulary, pronunciation limits
and script, BiDi-free canonicals, the guard on every English alias that could
rewrite ordinary prose, every multi-token canonical as a fixed point of the
matcher, and the loader's tier arbitrations broken down by kind):

```powershell
.\.venv\Scripts\python.exe scripts\validate_dictionary.py
```

Run matcher benchmarks:

```powershell
.\.venv\Scripts\python.exe benchmark\benchmark_matcher.py
```

Run the post-processing integrity benchmark (no audio, no credentials). Every
case is a transcript the ASR got right, so any error it reports was introduced
by the deterministic layer itself; it scores the single-pass and the streamed
path separately and checks that they agree:

```powershell
.\.venv\Scripts\python.exe benchmark\benchmark_postprocess.py --show-failures
```

```powershell
.\.venv\Scripts\python.exe benchmark\benchmark_matcher.py
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
desktop_app.py
injector.py
overlay.py

api/
    token.py                     # token broker (Vercel /api/token)

medical_knowledge/
    medical_dictionary.json
    speechmatics_additional_vocab.json

speechmatics_test/
    broker_client.py
    demo_license.py
    desktop_config.py
    matcher.py
    medical_layer.py
    realtime.py
    session_controller.py
    ...

scripts/
    install.ps1
    run.ps1
    build_windows.ps1            # developer PyInstaller fallback bundle
    build_hospital_demo.ps1      # hospital demo release (Nuitka + broker)
    set_demo_expiry.py
    swiftmedics-pyinstaller.spec
    export_additional_vocab.py
    validate_dictionary.py

tests/
    fixtures/
        pre_migration_canonicalization.json

benchmark/
    benchmark_matcher.py
    benchmark_asr.py
    benchmark_postprocess.py
    postprocess_cases.json
    results_before.json
    results_after.json
    postprocess_before.json
    postprocess_after.json
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
