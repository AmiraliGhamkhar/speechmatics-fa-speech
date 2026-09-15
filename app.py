from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import threading
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
        description="SwiftMedics Speechmatics mixed Persian/English medical ASR "
                    "with Aho-Corasick post-processing and automatic text injection"
    )
    p.add_argument("--language", choices=["fa", "en"], default="fa")
    p.add_argument("--test-id")
    p.add_argument("--no-vocab", action="store_true")
    p.add_argument("--no-medical-layer", action="store_true")
    p.add_argument("--inject", dest="inject", action="store_true", default=True,
                   help="Automatically inject each finalized segment into the "
                        "focused field (default). No hotkeys, no countdown.")
    p.add_argument("--no-inject", dest="inject", action="store_false",
                   help="Disable automatic injection.")
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


async def audio_source(recorder, max_seconds: float, stop_event=None):
    """Yield microphone chunks in memory only - never stored on disk.

    ``stop_event`` (a ``threading.Event``) lets Ctrl+C end the stream
    *gracefully*: the generator simply stops yielding, the realtime session
    closes normally, and the transcript/report is still produced. Raising
    KeyboardInterrupt through the event loop instead used to abort the whole
    pipeline and throw the dictated text away.
    """
    started = time.perf_counter()
    while time.perf_counter() - started < max_seconds:
        if stop_event is not None and stop_event.is_set():
            return
        chunk = await asyncio.to_thread(recorder.read)
        if stop_event is not None and stop_event.is_set():
            return
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
            add_bidi_marks=True,
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
    injected_segments: list[dict] = []

    print("=" * 72)
    print(" SwiftMedics / Speechmatics Mixed Medical ASR v4")
    print(" Aho-Corasick post-processing + automatic injection (no hotkeys)")
    print("=" * 72)
    print(f"Language stream : {args.language}")
    print(f"Model           : {args.model}")
    print(f"Max delay       : {args.max_delay:.1f}s ({args.max_delay_mode})")
    print(f"Medical vocab   : {'ON' if vocab else 'OFF'}")
    print(f"Matcher engine  : {medical.engine if not args.no_medical_layer else 'OFF'}")
    print(f"Device index    : {args.device_index if args.device_index is not None else 'default'}")
    print("Audio storage   : NONE (in-memory streaming only)")
    print(f"Report          : {json_path.name if json_path else 'not saved (use --save-report)'}")
    print(f"Max duration    : {args.max_seconds:.1f} sec")
    print(f"Auto-injection  : {'ON — every finalized segment is pasted at the cursor' if injector else 'OFF'}")
    print("=" * 72)
    if injector:
        print("Click the field where the transcript must go ONCE, then dictate.")
        print("Every finalized segment is pasted automatically. No keys to press.")
    print("Speak naturally. Press Ctrl+C to stop recording.")
    print()

    def on_partial(text: str):
        # UI/overlay only. Partials are never treated as final text.
        clean = normalize_text(text)
        print("\r[partial] " + clean[:200].ljust(200), end="", flush=True)
        if overlay:
            overlay.set_partial(clean)

    def on_final(text: str):
        # Finalized segment: normalize -> Aho-Corasick canonical -> display as
        # FINAL -> inject automatically right away (deepgram-v6 style).
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

        if injector:
            # Trailing space keeps consecutive segments separated in the
            # target field; prepare_mixed_text keeps it inside the BiDi wrap.
            injector.reset_partial()
            ok = injector.paste_text(segment + " ", add_rtl_mark=True)
            injected_segments.append({"text": segment, "success": bool(ok)})
            if overlay and ok:
                overlay.set_done(segment)

    # Ctrl+C must STOP THE RECORDING, not kill the program: everything after
    # this point (canonicalization, cleanliness, report) is exactly what the
    # user is dictating for. Previously SIGINT raised KeyboardInterrupt inside
    # the event loop, which escaped main() and threw the finished transcript
    # away.
    stop_event = threading.Event()
    loop = asyncio.get_running_loop()
    previous_sigint = None
    installed_signal_handler = False

    def request_stop() -> None:
        if not stop_event.is_set():
            stop_event.set()
            print("\n[session] Ctrl+C received - finishing the session "
                  "(transcript is preserved)...")

    try:
        loop.add_signal_handler(signal.SIGINT, request_stop)
        installed_signal_handler = True
    except (NotImplementedError, RuntimeError, ValueError):
        # Windows/ProactorEventLoop has no add_signal_handler: fall back to a
        # plain signal handler that is thread-safe enough for setting a flag.
        try:
            previous_sigint = signal.signal(
                signal.SIGINT, lambda *_: request_stop()
            )
            installed_signal_handler = True
        except (ValueError, OSError):
            previous_sigint = None

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
            audio = audio_source(recorder, args.max_seconds, stop_event)
            try:
                result = await stt.run(audio, on_partial, on_final)
            except KeyboardInterrupt:
                # Defensive: keep whatever the session already captured.
                print("\n[session] Ctrl+C received - stopping.")
                result = stt.result
            except Exception as exc:
                # A network/SDK failure must not discard an already dictated
                # transcript; report the error and continue to the report.
                print(f"\n[session error] {type(exc).__name__}: {exc}")
                result = stt.result
            finally:
                await audio.aclose()
    finally:
        if installed_signal_handler:
            try:
                loop.remove_signal_handler(signal.SIGINT)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
            if previous_sigint is not None:
                try:
                    signal.signal(signal.SIGINT, previous_sigint)
                except (ValueError, OSError):
                    pass
        if overlay:
            overlay.close()

    # ------------------------------------------------------------------ text
    # Stage 1: true Speechmatics RAW final transcript (no normalization).
    raw = (result.final_text or "").strip() if result is not None else ""
    # Stage 2: generic text normalization.
    normalized = normalize_text(raw)
    # Stage 3: deterministic medical Aho-Corasick canonicalization.
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

    if json_path is not None:
        report = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "language": args.language,
            "test_id": args.test_id,
            "medical_vocab_enabled": bool(vocab),
            "medical_layer_enabled": not args.no_medical_layer,
            "matcher_engine": medical.engine if not args.no_medical_layer else None,
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
            "medical_warnings": medical.warnings,
            "cleanliness": {
                "raw": raw_clean,
                "normalized": normalized_clean,
                "canonical": canonical_clean,
            },
            "injection": {
                "auto": True,
                "enabled": bool(injector),
                "segments": injected_segments,
            },
            "benchmark_expected": benchmark["expected"] if benchmark else None,
            "evaluation": evaluation_result,
        }
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if injector and injected_segments:
        n_ok = sum(1 for s in injected_segments if s["success"])
        print()
        print(f"[injector] auto-injected {n_ok}/{len(injected_segments)} finalized segments.")

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
