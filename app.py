from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from speechmatics_test.cleanliness import inspect_text
from speechmatics_test.evaluation import evaluate_stages
from speechmatics_test.medical_layer import MedicalLayer
from speechmatics_test.realtime import (
    DEFAULT_MAX_DELAY,
    DEFAULT_MAX_DELAY_MODE,
    DEFAULT_MODEL,
    MAX_MAX_DELAY,
    MIN_MAX_DELAY,
)
from speechmatics_test.text import normalize_text


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="SwiftMedics Speechmatics mixed Persian/English medical ASR benchmark"
    )
    p.add_argument("--language", choices=["fa", "en"], default="fa")
    p.add_argument("--test-id")
    p.add_argument("--no-vocab", action="store_true")
    p.add_argument("--no-medical-layer", action="store_true")
    p.add_argument("--inject", action="store_true",
                   help="Inject final text into a foreground target selected during countdown.")
    p.add_argument("--inject-delay", type=int, default=5,
                   help="Seconds to give you to select the target cursor before injection.")
    p.add_argument("--reject-arrows", action="store_true",
                   help="Do not inject if arrow characters are detected.")
    p.add_argument("--no-overlay", action="store_true")
    p.add_argument("--device-index", type=int,
                   help="PyAudio input device index (default: system default device)")
    p.add_argument("--max-seconds", type=float, default=300.0)
    p.add_argument("--model", choices=["standard", "enhanced"], default=DEFAULT_MODEL,
                   help="Speechmatics model (default: %(default)s)")
    p.add_argument("--max-delay", type=float, default=DEFAULT_MAX_DELAY,
                   help=f"Final-transcript delay in seconds, valid {MIN_MAX_DELAY}-{MAX_MAX_DELAY} "
                        f"(default: %(default)s, docs-recommended)")
    p.add_argument("--max-delay-mode", choices=["fixed", "flexible"],
                   default=DEFAULT_MAX_DELAY_MODE,
                   help="flexible lets the engine finish spoken entities (numbers) "
                        "(default: %(default)s)")
    p.add_argument("--save-report", action="store_true",
                   help="Save the text/metadata session report as JSON "
                        "(always saved when --test-id is given)")
    return p.parse_args()


def load_benchmark(test_id: str | None) -> dict | None:
    if not test_id:
        return None
    data = json.loads((ROOT / "benchmark" / "sentences.json").read_text(encoding="utf-8"))
    for x in data:
        if x.get("id") == test_id:
            return x
    raise RuntimeError(f"Unknown test ID: {test_id}")


def load_vocab() -> list:
    path = ROOT / "medical_knowledge" / "speechmatics_additional_vocab.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


async def audio_source(recorder, max_seconds: float):
    """Yield microphone chunks in memory only - never stored on disk."""
    started = time.perf_counter()
    while time.perf_counter() - started < max_seconds:
        chunk = await asyncio.to_thread(recorder.read)
        if chunk:
            yield chunk


def create_overlay(disabled: bool):
    if disabled:
        return None
    try:
        from overlay import TranscriptOverlay
        return TranscriptOverlay(enabled=True)
    except Exception as exc:
        print(f"[overlay disabled] {exc}")
        return None


def create_injector(enabled: bool):
    if not enabled:
        return None
    try:
        from injector import TextInjector
        return TextInjector(
            enable_smart_rewrite=True,
            restore_clipboard=True,
            paste_settle_seconds=0.25,
        )
    except Exception as exc:
        print(f"[injector disabled] {exc}")
        return None


def print_cleanliness(label: str, text: str) -> dict:
    report = inspect_text(text).to_dict()
    print()
    print(f"{label} CLEANLINESS")
    print("-" * 72)
    print(f"Clean           : {'YES' if report['is_clean'] else 'NO'}")
    print(f"Arrows          : {report['arrows'] or 'none'}")
    print(f"Bidi marks      : {report['bidi_marks'] or 'none'}")
    print(f"Control chars   : {report['control_characters'] or 'none'}")
    print(f"Replacement char: {report['replacement_characters']}")
    print(f"ZWNJ            : {report['zwnj_count']}")
    print(f"Newlines        : {report['newline_count']}")
    print(f"Tabs            : {report['tab_count']}")
    print(f"Other symbols   : {report['non_ascii_symbols'] or 'none'}")
    return report


def injection_countdown(injector, delay: int) -> dict:
    before = injector.get_foreground_window_info()
    print()
    print("TARGET SELECTION")
    print("-" * 72)
    print(f"Current foreground window: {before.get('title') or '[untitled]'}")
    print(f"You have {delay} seconds to click the exact text field/cursor where the transcript must go.")
    print("DO NOT click back into this terminal. SwiftMedics will inject automatically at zero.")

    for remaining in range(max(1, delay), 0, -1):
        print(f"  Injecting in {remaining}...", flush=True)
        time.sleep(1)

    after = injector.get_foreground_window_info()
    print(f"Target window: {after.get('title') or '[untitled]'}")

    if before.get("hwnd") == after.get("hwnd"):
        print("[warning] The foreground window did not change during the countdown.")
        print("          The paste may go into this terminal rather than your intended app.")
    else:
        print("[target] Foreground window changed; injecting into the selected target.")

    return {"before": before, "after": after, "changed": before.get("hwnd") != after.get("hwnd")}


async def main() -> int:
    args = parse_args()
    api_key = os.getenv("SPEECHMATICS_API_KEY", "").strip()

    if not api_key:
        print("ERROR: SPEECHMATICS_API_KEY is missing from .env")
        return 1

    from speechmatics_test.microphone import MicrophoneRecorder
    from speechmatics_test.realtime import SpeechmaticsRealtime

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = (
        ROOT / "results" / f"session_{stamp}_{args.language}.json"
        if (args.save_report or args.test_id) else None
    )

    vocab = [] if args.no_vocab else load_vocab()
    medical = MedicalLayer(ROOT)
    benchmark = load_benchmark(args.test_id)
    overlay = create_overlay(args.no_overlay)
    injector = create_injector(args.inject)

    result = None

    print("=" * 72)
    print(" SwiftMedics / Speechmatics Mixed Medical ASR Test v3")
    print("=" * 72)
    print(f"Language stream : {args.language}")
    print(f"Model           : {args.model}")
    print(f"Max delay       : {args.max_delay:.1f}s ({args.max_delay_mode})")
    print(f"Medical vocab   : {'ON' if vocab else 'OFF'}")
    print(f"FST layer       : {'ON' if not args.no_medical_layer else 'OFF'}")
    print(f"Device index    : {args.device_index if args.device_index is not None else 'default'}")
    print(f"Audio storage   : NONE (in-memory streaming only)")
    print(f"Report          : {json_path.name if json_path else 'not saved (use --save-report)'}")
    print(f"Max duration    : {args.max_seconds:.1f} sec")
    print("=" * 72)
    print("Speak naturally. Press Ctrl+C to stop recording.")
    print()

    def on_partial(text: str):
        # UI/overlay only. Partials are never treated as final text.
        clean = normalize_text(text)
        print("\r[partial] " + clean[:200].ljust(200), end="", flush=True)
        if overlay:
            overlay.set_partial(clean)

    def on_final(text: str):
        # Finalized segment only: normalize -> FST canonical -> display as FINAL.
        clean = normalize_text(text)
        if not clean:
            return
        segment, _hits = (
            medical.canonicalize(clean) if not args.no_medical_layer
            else (clean, [])
        )
        print("\n[final]   " + segment)
        if overlay:
            overlay.set_final(segment)

    recorder = MicrophoneRecorder(device_index=args.device_index)
    try:
        with recorder:
            stt = SpeechmaticsRealtime(
                api_key=api_key,
                language=args.language,
                additional_vocab=vocab,
                max_delay=args.max_delay,
                model=args.model,
                max_delay_mode=args.max_delay_mode,
            )
            audio = audio_source(recorder, args.max_seconds)
            try:
                result = await stt.run(audio, on_partial, on_final)
            except KeyboardInterrupt:
                print("\n[session] Ctrl+C received - stopping.")
                result = stt.result
            finally:
                await audio.aclose()
    finally:
        if overlay:
            overlay.close()

    # ------------------------------------------------------------------ text
    # Stage 1: true Speechmatics RAW final transcript (no normalization).
    raw = (result.final_text or "").strip() if result is not None else ""
    # Stage 2: generic text normalization.
    normalized = normalize_text(raw)
    # Stage 3: deterministic medical FST canonicalization.
    if not args.no_medical_layer:
        canonical, medical_hits = medical.canonicalize(normalized)
    else:
        canonical, medical_hits = normalized, []

    raw_clean = print_cleanliness("RAW TRANSCRIPT", raw)
    normalized_clean = print_cleanliness("NORMALIZED TRANSCRIPT", normalized)
    canonical_clean = print_cleanliness("CANONICALIZED TRANSCRIPT", canonical)

    evaluation_result = (
        evaluate_stages(benchmark["expected"], {
            "raw": raw,
            "normalized": normalized,
            "fst_canonical": canonical,
        })
        if benchmark else None
    )

    injection_info = None

    if injector and canonical:
        if args.reject_arrows and canonical_clean["arrows"]:
            print()
            print("[injector] BLOCKED: arrow characters detected and --reject-arrows is enabled.")
        else:
            print()
            print("EXACT INJECTION PAYLOAD")
            print("-" * 72)
            print(repr(canonical))
            print("The visible transcript above is what will be pasted; no arrows are added by the injector.")

            injection_info = injection_countdown(injector, args.inject_delay)

            ok = injector.paste_text(canonical, add_rtl_mark=False)

            injection_info["success"] = bool(ok)
            print()
            print("[injector] "
                  + ("Paste command sent successfully." if ok else "Paste failed."))

    if json_path is not None:
        report = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "language": args.language,
            "test_id": args.test_id,
            "medical_vocab_enabled": bool(vocab),
            "medical_layer_enabled": not args.no_medical_layer,
            "model": args.model,
            "max_delay": args.max_delay,
            "max_delay_mode": args.max_delay_mode,
            "device_index": args.device_index,
            "first_partial_latency_ms": getattr(result, "first_partial_ms", None),
            "session_error": getattr(result, "error", None),
            "partials": getattr(result, "partials", []),
            "final_segments": getattr(result, "final_segments", []),
            "final_transcript_raw": raw,
            "final_transcript_normalized": normalized,
            "final_transcript_canonical": canonical,
            "medical_hits": medical_hits,
            "fst_warnings": medical.warnings,
            "cleanliness": {
                "raw": raw_clean,
                "normalized": normalized_clean,
                "canonical": canonical_clean,
            },
            "injection": injection_info,
            "benchmark_expected": benchmark["expected"] if benchmark else None,
            "evaluation": evaluation_result,
        }
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print()
    print("=" * 72)
    print("FINAL RAW:")
    print(raw or "[empty]")
    print()
    print("FINAL NORMALIZED:")
    print(normalized or "[empty]")
    print()
    print("FINAL CANONICAL:")
    print(canonical or "[empty]")

    if evaluation_result:
        print()
        print("EVALUATION (raw / normalized / fst_canonical):")
        print(json.dumps(evaluation_result, ensure_ascii=False, indent=2))

    print()
    if json_path is not None:
        print(f"JSON: {json_path}")
    else:
        print("No report saved (use --save-report or --test-id to save).")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0)
