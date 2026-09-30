# SwiftMedics hospital demo — packaging & protection playbook

How to produce a **demo .exe for a hospital** where neither your **source code**
nor your **Speechmatics credentials** are reachable from the distributed bundle.

```text
Demo machine (hospital)                            Your side
┌──────────────────────────────┐                 ┌──────────────────────────────────┐
│  SwiftMedics.exe             │                 │  Token broker (api/token.py)     │
│  - compiled-in expiry date   │    HTTPS GET    │  - holds SPEECHMATICS_API_KEY    │
│  - holds NO API key          │ ──────────────▶ │  - mints 60-second realtime JWT  │
│  - compiled machine code     │ ◀────────────── │  - optional DEMO_TOKEN revocation│
│    (Nuitka build path)       │  {"token": ...} │                                  │
└──────────────┬───────────────┘                 └────────────────┬─────────────────┘
               │ wss://…rt.speechmatics.com  (JWT in header)      │ server-to-server
               ▼                                                  ▼
        Speechmatics Realtime ◀──────────────────────────── Speechmatics (key never moves)
```

## Why this design (research summary)

| Question | Finding |
| --- | --- |
| Does PyInstaller protect code? | **No.** It bundles `.pyc` bytecode; `pyinstxtractor` + `pycdc` decompiles it back to readable Python in minutes. |
| What actually protects code? | **Compilation to machine code.** Nuitka transpiles Python to C → no bytecode to decompile. Cython is the manual alternative; PyArmor (bytecode encryption) is weaker than compilation. |
| Can I hide the API key inside the exe? | **No.** Any value shipped inside a distributed binary is extractable. The fix is architectural: the exe fetches a **short-lived JWT** from your own broker. |
| Does Speechmatics support this? | Yes — official **temporary keys** (`POST /v1/api_keys?type=rt`, `ttl` seconds) are recommended for client-side realtime. A temporary key is used *in place of* the API key. |
| What stops the demo being used forever? | A compiled-in expiry stamp (`scripts/set_demo_expiry.py`) checked on every Start. |
| Will hospital IT run an unknown exe? | Only if signed. Code-sign the exe (OV certificate); note that even EV-signed builds must accrue SmartScreen reputation since 2024. |

## The three layers this repo now ships

1. **Credential protection** — `api/token.py` broker + JWT path in the desktop app.
2. **Demo control** — compiled-in expiry via `scripts/set_demo_expiry.py` (+ `build_demo.ps1`).
3. **Code protection** — Nuitka machine-code build via `scripts/build_windows_nuitka.ps1`.

---

## Layer 1 — the token broker (credentials never ship)

`api/token.py` is a dependency-free serverless function:

* `GET /api/token` → `{"token": "<jwt>", "ttl": 60}`.
* The Speechmatics API key lives **only** in the broker's environment.
* Optional `DEMO_TOKEN` env var: the client must send `Authorization: Bearer <DEMO_TOKEN>`;
  rotating/deleting it revokes every shipped demo instantly.
* `TOKEN_TTL` env var: token lifetime in seconds (clamped 10–300, default 60).
* Local mode for an on-site demo (key stays on the presenter machine):
  `python api/token.py --serve 8787`.

### Deploy to Vercel (free Hobby tier)

```bash
npm i -g vercel
vercel                 # from the repository root; api/token.py auto-routes
vercel env add SPEECHMATICS_API_KEY    # paste your private key
vercel env add DEMO_TOKEN              # optional: shared secret for revocation
vercel --prod
# your broker is now at https://<your-app>.vercel.app/api/token
```

Any HTTPS host works (the handler is provider-agnostic); the repo includes
`api/vercel.json` for one-command deployment.

### Desktop side

`%APPDATA%\SwiftMedics\config.json` on a demo machine:

```json
{
  "speechmatics_api_key": "",
  "token_broker_url": "https://<your-app>.vercel.app/api/token",
  "demo_token": "<DEMO_TOKEN if you set one on the broker>",
  "language": "fa"
}
```

With `token_broker_url` set and the key empty, the app fetches a short-lived
JWT on every Start (status: `در حال دریافت توکن امن...`) and uses it for that
session only. Operator-provided full API keys keep working unchanged.

---

## Layer 2 — the demo build (expiry + one command)

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\build_demo.ps1 -ExpiryDate 2026-11-30 `
    -BrokerUrl https://<your-app>.vercel.app/api/token `
    [-DemoToken <DEMO_TOKEN>] [-BuildId hospital-x-demo] [-KeepStamp]
```

What it does:

1. Generates `speechmatics_test/demo_build_stamp.py` (expiry + broker URL;
   git-ignored, never committed).
2. Runs the normal PyInstaller bundle.
3. Writes `dist\SwiftMedics\config-demo-template.json` pre-filled with the
   broker URL.
4. Removes the stamp afterwards (use `-KeepStamp` to rebuild repeatedly).

At Start the app refuses to run after expiry (`23:59:59` local time on the
given date). This is tamper-resistant for honest users; it is not a substitute
for compiled protection — combine with Layer 3.

To remove demo state entirely: delete `speechmatics_test/demo_build_stamp.py`.

---

## Layer 3 — code protection (Nuitka machine-code build)

```powershell
.\scripts\build_windows_nuitka.ps1
# → dist\SwiftMedics-nuitka\SwiftMedics.exe
```

* First build is slow (full C compile); later builds are faster.
* Output is a standalone folder; `SwiftMedics.exe` plus runtime `.pyd`s.
  There is no `.pyc` of your code anywhere in it — reverse engineering means
  reading x86 assembly.
* Combine layers: run `build_demo.ps1` logic, then build with Nuitka, or add
  `--expiry`/broker handling as needed. The demo stamp works identically in a
  Nuitka build because `demo_license.py` is compiled along with everything else.
* The pure-Python pyahocorasick fallback keeps the medical matcher fully
  functional even if a C extension is missed (parity-tested).
* **Sign the exe** (`signtool sign /fd SHA256 /tr ...`) before distribution.

---

## Hospital demo checklist

- [ ] Broker deployed; `SPEECHMATICS_API_KEY` set on the broker only.
- [ ] `DEMO_TOKEN` set (enables instant revocation).
- [ ] Demo build created with `build_demo.ps1 -ExpiryDate ... -BrokerUrl ...`.
- [ ] Exe **code-signed**; give hospital IT the vendor name + SHA-256 hash.
- [ ] Demo machine has `%APPDATA%\SwiftMedics\config.json` (or ship the
      included `config-demo-template.json`).
- [ ] Dry-run on a clean Windows VM **without** your real key present —
      confirm the app works via broker and that no key appears anywhere
      (`strings` the bundle, check the config, check the log).
- [ ] PHI policy: the app already keeps audio in memory only and no
      transcription is uploaded anywhere except Speechmatics; for the demo,
      dictate synthetic/fictional patient data only.
- [ ] After the demo: rotate `DEMO_TOKEN` (kills every shipped build's access),
      and let the expiry date lapse.

## Threat model — what this protects against

| Threat | Covered by |
| --- | --- |
| API key extracted from the exe | ✅ key is never in the exe — only 60-second JWTs travel |
| Key stolen from the demo machine | ✅ only a seconds-lived token ever exists there |
| Leaked demo exe used months later | ✅ compiled-in expiry + revocable `DEMO_TOKEN` |
| Bytecode lifted and decompiled | ✅ with the Nuitka build: machine code only |
| Determined nation-state attacker | ⚠️ no client-side scheme fully stops this; for production licensing use a server-side feature gate |
| Hospital IT blocking unsigned exe | ✅ sign it; supply hash/vendor for allowlisting |
