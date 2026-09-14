from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from speechmatics_test.cleanliness import inspect_text
from speechmatics_test.evaluation import evaluate
from speechmatics_test.medical_layer import MedicalLayer
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
    p.add_argument("--inject", action="store_true", help="Inject final text into a foreground target selected during countdown.")
    p.add_argument("--inject-delay", type=int, default=5, help="Seconds to give you to select the target cursor before injection.")
    p.add_argument("--reject-arrows", action="store_true", help="Do not inject if arrow characters are detected.")
    p.add_argument("--no-overlay", action="store_true")
    p.add_argument("--device-index", type=int)
    p.add_argument("--max-seconds", type=float, default=300.0)
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
    if not (ROOT / "medical_knowledge" / "speechmatics_additional_vocab.json").exists():
        return []
    return json.loads((ROOT / "medical_knowledge" / "speechmatics_additional_vocab.json").read_text(encoding="utf-8"))


async def audio_source(recorder, max_seconds: float):
    started = time.perf_counter()
    while time.perf_counter() - started < max_seconds:
        chunk = await asyncio.to_thread(recorder.read)
        if chunk:
            recorder.frames.append(chunk)
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
    wav_path = ROOT / "recordings" / f"session_{stamp}_{args.language}.wav"
    json_path = ROOT / "results" / f"session_{stamp}_{args.language}.json"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)

    vocab = [] if args.no_vocab else load_vocab()
    medical = MedicalLayer(ROOT)
    benchmark = load_benchmark(args.test_id)
    overlay = create_overlay(args.no_overlay)
    injector = create_injector(args.inject)

    final_segments: list[str] = []
    result = None

    print("=" * 72)
    print(" SwiftMedics / Speechmatics Mixed Medical ASR Test v2")
    print("=" * 72)
    print(f"Language stream : {args.language}")
    print(f"Medical vocab   : {'ON' if vocab else 'OFF'}")
    print(f"Medical layer   : {'ON' if not args.no_medical_layer else 'OFF'}")
    print(f"Injection       : {'ON' if injector else 'OFF'}")
    print(f"Max duration    : {args.max_seconds:.1f} sec")
    print(f"Recording       : {wav_path.name}")
    print("=" * 72)
    print("Speak naturally. Press Ctrl+C to stop recording.")
    print()

    def on_partial(text: str):
        clean = normalize_text(text)
        print("\\r[partial] " + clean[:200].ljust(200), end="", flush=True)
        if overlay:
            overlay.set_partial(clean)

    def on_final(text: str):
        clean = normalize_text(text)
        if not clean:
            return
        final_segments.append(clean)
        print("\
[final]   " + clean)
        if overlay:
            overlay.set_partial(clean)

    recorder = MicrophoneRecorder(wav_path, device_index=args.device_index)
    try:
        with recorder:
            stt = SpeechmaticsRealtime(
                api_key=api_key,
                language=args.language,
                additional_vocab=vocab,
                max_delay=1.0,
            )
            try:
                result = await stt.run(
                    audio_source(recorder, args.max_seconds),
                    on_partial,
                    on_final,
                )
            except KeyboardInterrupt:
                print("\
[session] Ctrl+C received.")
                result = stt.result
    finally:
        if overlay:
            overlay.close()

    raw = " ".join(final_segments).strip()
    if not raw and result is not None:
        raw = (result.final_text or "").strip()

    normalized = normalize_text(raw)
    canonical = normalized
    medical_hits = []

    if not args.no_medical_layer:
        canonical, medical_hits = medical.normalize(normalized)

    raw_clean = print_cleanliness("RAW TRANSCRIPT", raw)
    canonical_clean = print_cleanliness("CANONICALIZED TRANSCRIPT", canonical)

    evaluation_result = (
        evaluate(benchmark["expected"], canonical)
        if benchmark
        else None
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

            injection_info = injection_countdown(
                injector,
                args.inject_delay,
            )

            ok = injector.paste_text(
                canonical,
                add_rtl_mark=False,
            )

            injection_info["success"] = bool(ok)
            print()
            print(
                "[injector] "
                + ("Paste command sent successfully." if ok else "Paste failed.")
            )

    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "language": args.language,
        "test_id": args.test_id,
        "medical_vocab_enabled": bool(vocab),
        "medical_layer_enabled": not args.no_medical_layer,
        "audio_path": str(wav_path.relative_to(ROOT)),
        "first_partial_latency_ms": getattr(result, "first_partial_ms", None),
        "partials": getattr(result, "partials", []),
        "final_segments": getattr(result, "final_segments", []),
        "final_transcript_raw": raw,
        "final_transcript_normalized": normalized,
        "final_transcript_canonicalized": canonical,
        "medical_hits": medical_hits,
        "cleanliness": {
            "raw": raw_clean,
            "canonicalized": canonical_clean,
        },
        "injection": injection_info,
        "benchmark_expected": benchmark["expected"] if benchmark else None,
        "evaluation": evaluation_result,
    }

    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print()
    print("=" * 72)
    print("FINAL RAW:")
    print(raw or "[empty]")
    print()
    print("FINAL CANONICALIZED:")
    print(canonical or "[empty]")

    if evaluation_result:
        print()
        print("EVALUATION:")
        print(json.dumps(evaluation_result, ensure_ascii=False, indent=2))

    print()
    print(f"WAV : {wav_path}")
    print(f"JSON: {json_path}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0)
