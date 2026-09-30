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

## The two release paths this repo ships

1. **Hospital demo (the one true demo path)** —
   `scripts/build_hospital_demo.ps1`: Nuitka machine-code build with the
   compiled-in expiry + broker URL (Layer 1 + Layer 2 + Layer 3 combined).
2. **Developer fallback** — `scripts/build_windows.ps1` (PyInstaller):
   quick local bundle for development machines only. It contains ordinary
   Python bytecode, so it is **not** for hospital distribution.

Credential protection (the broker) is part of the demo path by construction:
the demo build cannot exist without `-BrokerUrl`.

---

## Layer 1 — the token broker (credentials never ship)

`api/token.py` is a dependency-free serverless function exposing the WSGI
`application` entry point that the current Vercel Python runtime loads for
file-based `/api` functions:

* `GET /api/token` → `{"token": "<jwt>", "ttl": 60}`.
* The Speechmatics API key lives **only** in the broker's environment.
* Optional `DEMO_TOKEN` env var: the client must send `Authorization: Bearer <DEMO_TOKEN>`;
  rotating/deleting it revokes every shipped demo instantly.
* `TOKEN_TTL` env var: token lifetime in seconds (clamped 10–300, default 60).
  **Use 300 in production**: the temporary key expires after this many
  seconds and Speechmatics closes the websocket mid-dictation when it does
  (surfaced by the app as a transport error). 60 s is only useful for quick
  hand-tests.
* Responses are never cached (`Cache-Control: no-store`); credentials are
  never logged.
* CORS is **disabled by default** (the client is a native Windows app, not a
  browser). Set `ALLOWED_ORIGIN` to a specific origin only if you also serve
  a web client from the same broker — an open CORS policy on a token mint
  only invites browser-based abuse.
* Only `/api/token` is served; every other route answers 404 (with Vercel's
  entrypoint mode the function receives all routes, and only the token mint
  may be reachable).
* Local mode for an on-site demo (key stays on the presenter machine):
  `python api/token.py --serve 8787`.

### Deploy to Vercel (free Hobby tier)

```bash
npm i -g vercel
vercel login
vercel link                     # from the repository root
vercel env add SPEECHMATICS_API_KEY production    # paste your private key
vercel env add DEMO_TOKEN production              # shared secret for revocation
vercel env add TOKEN_TTL production               # use 300 (seconds)
vercel --prod
# your broker is now at https://<your-app>.vercel.app/api/token
```

`pyproject.toml` pins the deployed entrypoint (`[tool.vercel] entrypoint =
"api.token:application"` — without it Vercel's Python detection picks the
root `app.py`, the desktop CLI, and the deploy fails); `vercel.json` at the
project root configures the function (duration, excluded files).

**Disable Deployment Protection** for this project (Project → Settings →
Deployment Protection → Disabled): the demo exe is a native client and
cannot pass Vercel Authentication, so with protection on it can never reach
`/api/token`.

Verify with `curl.exe` (not PowerShell's `curl` alias — it cannot send
headers): no token → `403`; with the demo token → `200` and a JWT.

### Desktop side

`%APPDATA%\SwiftMedics\config.json` on a demo machine (optional; demo builds
already carry the broker URL compiled in — the file can override it):

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

## Layer 2 + 3 — the hospital demo build (one command)

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\build_hospital_demo.ps1 -ExpiryDate 2026-11-30 `
    -BrokerUrl https://<your-app>.vercel.app/api/token `
    [-BuildId hospital-x-demo] [-DemoToken <DEMO_TOKEN>] `
    [-ProductVersion 1.0.0.0] [-SkipInstall] [-KeepStamp]
```

The broker URL is **compiled into the exe**: always pass the stable
production alias with the `/api/token` path — never a per-deployment host
(`...-abc123.vercel.app`), which changes on every deploy, and never a bare
origin, which 404s. The `-DemoToken` value is baked into the shipped config
template; rotating the broker's `DEMO_TOKEN` afterwards requires a rebuild
(or deleting `%APPDATA%\SwiftMedics\config.json` on the machine).

What it does:

1. Creates `.venv` and installs dependencies + Nuitka (`-SkipInstall` to skip).
2. Validates the broker URL with the app's own runtime rules.
3. Generates `speechmatics_test/demo_build_stamp.py` (expiry + build ID +
   broker URL; git-ignored, never committed) and compiles it **into** the
   build via Nuitka — no `.py` stamp ships beside the exe.
4. Builds a standalone, no-console `SwiftMedics.exe` with embedded Windows
   product metadata, `medical_knowledge` data files and all runtime packages.
5. Writes `dist\SwiftMedics-hospital-demo\config-demo-template.json`.
6. Runs the secret-safety gate: the build **fails** if the long-lived API key
   appears anywhere in the bundle (the value itself is never printed).
7. Removes the temporary stamp afterwards (use `-KeepStamp` to rebuild).

At Start the app refuses to run after expiry (`23:59:59` local time on the
given date) **and** after a detected system-clock rollback (a per-user
high-water mark of UTC time with a 24 h NTP-travel tolerance, stored next to
the config). This is tamper-resistant for honest users; it is not a
substitute for compiled protection — the Nuitka layer is.

### Diagnosing Start failures: smoke_demo_chain.py

`scripts/smoke_demo_chain.py` runs the same chain the exe runs, one stage at
a time, and names the failing stage with the provider's own reason:

```powershell
.\.venv\Scripts\python.exe scripts\smoke_demo_chain.py `
    --broker-url https://<your-app>.vercel.app/api/token `
    --demo-token <DEMO_TOKEN>
```

1. Broker URL rules (the client's own validation)
2. `GET /api/token` — 403 here = `DEMO_TOKEN` mismatch; anything else =
   reachability/server config
3. JWT claim decode (shows `exp` seconds remaining without printing the token)
4. Real websocket handshake to Speechmatics (reports the server's close
   code/reason verbatim)

A "transport closed"-style failure mid-dictation with all four stages passing
almost always means the temporary key expired: raise `TOKEN_TTL` to 300 and
rebuild/redeploy.

Client-side hardening on every Start: the broker URL must parse as https
(loopback http allowed for local mode), without embedded credentials, query,
fragment or a non-standard port; the fetched credential must be JWT-shaped;
and the websocket endpoint is never taken from the broker — the app always
connects to Speechmatics directly, so even a compromised broker cannot
redirect the audio stream.

To remove demo state from a developer checkout entirely: delete
`speechmatics_test/demo_build_stamp.py` (the build script does this for you).

### What Nuitka does and does not protect

The distributed bundle contains compiled machine code, not Python bytecode:
there is no `.pyc` of the application to decompile with `pyinstxtractor`/
`pycdc`, which removes the trivial extraction path. Reverse engineering is
**still possible** for a determined attacker — native code can be disassembled
and string constants (including the compiled-in broker URL and expiry date)
can be recovered. Nuitka substantially raises the difficulty; it does not make
it impossible. For production licensing, combine with a server-side feature
gate (the broker is exactly that gate for the dictation capability).

---

## Hospital demo checklist

- [ ] Broker deployed; `SPEECHMATICS_API_KEY` set on the broker only.
- [ ] `DEMO_TOKEN` set (enables instant revocation) — and treated as a secret:
  it must never appear in screenshots, terminal transcripts, or tickets.
- [ ] `TOKEN_TTL=300` set (the 60 s default expires mid-dictation).
- [ ] Deployment Protection disabled on the Vercel project.
- [ ] Demo build created with `build_hospital_demo.ps1 -ExpiryDate ... -BrokerUrl ...`
      using the **stable production alias + `/api/token`**.
- [ ] `smoke_demo_chain.py` passed on the build machine (all four stages).
- [ ] Exe **code-signed**; give hospital IT the vendor name + SHA-256 hash.
- [ ] Demo machine has no config file (compiled-in defaults) or ships
      `config-demo-template.json` copied to `%APPDATA%\SwiftMedics\config.json`.
- [ ] Dry-run on a clean Windows VM **without** your real key present —
      confirm the app works via broker and that no key appears anywhere
      (the build's secret gate already scans the bundle; check the config
      and the log too).
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
