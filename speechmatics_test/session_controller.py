"""Reusable realtime dictation controller for the desktop floating UI.

The original ``app.py`` entry point is intentionally CLI-shaped: parse args,
print to the console, run until Ctrl+C.  The Windows executable needs the same
ASR/canonicalization/injection pipeline behind Start/Stop buttons.  This module
wraps that pipeline in a small thread-safe controller without changing the CLI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .desktop_config import DesktopConfig, app_data_dir
from .entity_guard import ClinicalEntityGuard
from .medical_layer import MedicalLayer
from .realtime import SpeechmaticsRealtime, confidence_summary, resolve_domain
from .text import normalize_text

# Reuse the exact streaming canonicalizer and FIFO injection worker that the
# tested CLI path uses.  They live in app.py today because the historical CLI
# grew first; importing them keeps desktop injection/report semantics identical
# without moving public test fixtures in this change.
from app import FinalStreamCanonicalizer, InjectionWorker  # noqa: E402

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DictationSettings:
    """Runtime settings for one desktop dictation session."""

    api_key: str
    language: str = "fa"
    model: str = "enhanced"
    max_delay: float = 2.0
    max_delay_mode: str = "flexible"
    domain: str = "auto"
    inject: bool = True
    medical_layer: bool = True
    medical_vocab: bool = True
    focus_guard: bool = True
    entity_guard: bool = True
    device_index: int | None = None
    save_report: bool = False
    max_seconds: float = 24 * 60 * 60.0

    @classmethod
    def from_desktop_config(cls, config: DesktopConfig) -> "DictationSettings":
        return cls(
            api_key=config.speechmatics_api_key,
            language=config.language,
            model=config.model,
            max_delay=config.max_delay,
            max_delay_mode=config.max_delay_mode,
            domain=config.domain,
            inject=config.inject,
            medical_layer=config.medical_layer,
            medical_vocab=config.medical_vocab,
            focus_guard=config.focus_guard,
            entity_guard=config.entity_guard,
            device_index=config.device_index,
            save_report=config.save_report,
        )


@dataclass
class SessionSummary:
    """Text/metadata summary produced when a desktop session stops."""

    raw: str = ""
    normalized: str = ""
    canonical: str = ""
    injected_segments: list[dict] = field(default_factory=list)
    review_flags: list[dict] = field(default_factory=list)
    entity_flags: list[dict] = field(default_factory=list)
    report_path: Path | None = None
    error: str | None = None


@dataclass
class SessionCallbacks:
    """Callbacks invoked from the worker thread.

    UI code should marshal these back onto its UI thread.  Every callback is
    optional; exceptions are logged and swallowed so one UI update cannot kill
    the recording session.
    """

    on_status: Callable[[str], None] | None = None
    on_partial: Callable[[str], None] | None = None
    on_final: Callable[[str], None] | None = None
    on_injection: Callable[[dict], None] | None = None
    on_error: Callable[[str], None] | None = None
    on_stopped: Callable[[SessionSummary], None] | None = None


class DictationSession:
    """Start/Stop wrapper around Speechmatics realtime dictation."""

    def __init__(
        self,
        settings: DictationSettings,
        callbacks: SessionCallbacks | None = None,
        *,
        resource_root: Path | None = None,
    ) -> None:
        self.settings = settings
        self.callbacks = callbacks or SessionCallbacks()
        self.resource_root = resource_root or default_resource_root()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._summary = SessionSummary()

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    @property
    def summary(self) -> SessionSummary:
        return self._summary

    def start(self) -> None:
        """Start the session in a background thread."""

        with self._lock:
            if self.is_running:
                raise RuntimeError("Dictation session is already running")
            self._stop_event = threading.Event()
            self._summary = SessionSummary()
            self._thread = threading.Thread(
                target=self._thread_main,
                name="desktop-dictation-session",
                daemon=True,
            )
            self._thread.start()

    def request_stop(self) -> None:
        """Ask the recording loop to stop gracefully and drain injection."""

        self._stop_event.set()
        self._emit_status("stopping")

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the worker thread.  Returns true when it is stopped."""

        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    # ------------------------------------------------------------------ run

    def _thread_main(self) -> None:
        try:
            summary = asyncio.run(self._run_async())
            self._summary = summary
            self._emit_stopped(summary)
        except Exception as exc:  # defensive: surface every startup/runtime error
            message = f"{type(exc).__name__}: {exc}"
            log.exception("Desktop dictation session failed")
            self._summary = SessionSummary(error=message)
            self._emit_error(message)
            self._emit_stopped(self._summary)

    async def _run_async(self) -> SessionSummary:
        settings = self.settings
        if not settings.api_key.strip():
            raise RuntimeError("Speechmatics API key is missing")

        self._emit_status("starting")

        from .microphone import MicrophoneRecorder

        medical = None if not settings.medical_layer else MedicalLayer(self.resource_root)
        entity_guard = ClinicalEntityGuard() if settings.entity_guard else None
        vocab = [] if (not settings.medical_vocab or medical is None) else medical.additional_vocab
        injector = self._create_injector(settings.inject)

        effective_domain = resolve_domain(settings.language, settings.domain)
        log.info(
            "Starting desktop session: language=%s model=%s domain=%s effective_domain=%s "
            "vocab=%s inject=%s focus_guard=%s",
            settings.language,
            settings.model,
            settings.domain,
            effective_domain,
            bool(vocab),
            bool(injector),
            settings.focus_guard,
        )

        accumulator = FinalStreamCanonicalizer(medical)
        injected_segments: list[dict] = []
        result = None
        recorder = None
        stt: SpeechmaticsRealtime | None = None

        def on_injection_result(record: dict) -> None:
            self._safe_call(self.callbacks.on_injection, record)

        worker = InjectionWorker(injector, on_result=on_injection_result) if injector else None
        worker_shutdown = False

        # The desktop button is made non-activating and restores the last target
        # window before calling start(), so arming here usually captures the
        # text field the user had focused before pressing Start.
        worker_armed = False
        if injector and settings.focus_guard:
            try:
                worker_armed = bool(injector.arm_target())
                self._emit_status("listening" if worker_armed else "listening_unarmed")
            except Exception as exc:
                log.warning("Could not arm injection target: %s", exc)
                self._emit_status("listening_unarmed")
        else:
            self._emit_status("listening")

        def on_partial(text: str) -> None:
            clean = normalize_text(text)
            if clean:
                self._safe_call(self.callbacks.on_partial, clean)

        def on_final(text: str) -> None:
            nonlocal worker_armed
            clean = normalize_text(text)
            if not clean:
                return
            if stt is None:
                return
            segment = stt.result.final_segments[-1]
            final_words = stt.result.word_results[
                segment["word_start_index"]:segment["word_end_index"]
            ]
            emitted = accumulator.add(clean, final_words)
            if emitted:
                self._safe_call(self.callbacks.on_final, accumulator.canonical_text)
                if worker:
                    if not worker_armed and settings.focus_guard:
                        try:
                            worker_armed = bool(injector.arm_target())
                        except Exception as exc:
                            log.warning("Could not arm injection target on first final: %s", exc)
                    worker.submit(emitted)

        def finish_injection() -> list[dict]:
            """Flush canonical tail, then drain queued paste operations."""

            nonlocal worker_shutdown
            tail = accumulator.flush()
            if tail:
                self._safe_call(self.callbacks.on_final, accumulator.canonical_text)
                if worker:
                    worker.submit(tail)
            if worker is None:
                return []
            worker.shutdown()
            worker_shutdown = True
            return list(worker.records)

        try:
            try:
                recorder = MicrophoneRecorder(device_index=settings.device_index)
            except Exception as exc:
                raise RuntimeError(
                    "Microphone could not be opened. Check that a microphone is connected "
                    "and PyAudio is installed. Original error: " + str(exc)
                ) from exc

            with recorder:
                stt = SpeechmaticsRealtime(
                    api_key=settings.api_key,
                    language=settings.language,
                    additional_vocab=vocab,
                    max_delay=settings.max_delay,
                    model=settings.model,
                    max_delay_mode=settings.max_delay_mode,
                    domain=settings.domain,
                )
                audio = self._audio_source(recorder, settings.max_seconds)
                try:
                    result = await stt.run(audio, on_partial, on_final)
                except Exception as exc:
                    # Preserve any final transcript already delivered, then let
                    # the UI know.  This mirrors the CLI's "report what we have"
                    # behavior instead of dropping dictated text on network/API errors.
                    log.exception("Speechmatics session error")
                    self._emit_error(f"{type(exc).__name__}: {exc}")
                    result = stt.result
                finally:
                    try:
                        await audio.aclose()
                    except Exception:
                        pass
                    injected_segments = finish_injection()
        finally:
            if worker is not None and not worker_shutdown:
                # If setup failed after creating a worker but before the normal
                # finish path above, make a best-effort shutdown so its daemon
                # thread does not keep clipboard resources busy.
                try:
                    worker.shutdown()
                except Exception:
                    pass

        raw = (result.final_text or "").strip() if result is not None else ""
        normalized = normalize_text(raw)
        canonical = accumulator.canonical_text
        entity_flags = entity_guard.scan(canonical) if entity_guard is not None else []
        summary = SessionSummary(
            raw=raw,
            normalized=normalized,
            canonical=canonical,
            injected_segments=injected_segments,
            review_flags=list(accumulator.flags),
            entity_flags=[flag.to_dict() for flag in entity_flags],
            error=getattr(result, "error", None) if result is not None else None,
        )
        if settings.save_report:
            summary.report_path = self._write_report(
                summary=summary,
                result=result,
                medical=medical,
                medical_hits=accumulator.hits,
                settings=settings,
                effective_domain=effective_domain,
                injection_enabled=bool(injector),
            )
        self._emit_status("stopped")
        return summary

    async def _audio_source(self, recorder, max_seconds: float):
        """Yield microphone chunks until Stop is requested."""

        started = time.perf_counter()
        while time.perf_counter() - started < max_seconds:
            if self._stop_event.is_set():
                return
            chunk = await asyncio.to_thread(recorder.read)
            if self._stop_event.is_set():
                return
            if chunk:
                yield chunk

    @staticmethod
    def _create_injector(enabled: bool):
        if not enabled:
            return None
        try:
            from injector import TextInjector

            return TextInjector(
                enable_smart_rewrite=True,
                restore_clipboard=True,
                paste_settle_seconds=0.25,
                add_bidi_marks=False,
            )
        except Exception as exc:
            log.exception("Injector disabled: %s", exc)
            return None

    def _write_report(
        self,
        *,
        summary: SessionSummary,
        result,
        medical: MedicalLayer | None,
        medical_hits: list[dict],
        settings: DictationSettings,
        effective_domain: str | None,
        injection_enabled: bool,
    ) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        report_dir = app_data_dir() / "results"
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / f"session_{stamp}_{settings.language}.json"
        word_results = getattr(result, "word_results", []) if result is not None else []
        report = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source": "desktop_app",
            "language": settings.language,
            "model": settings.model,
            "max_delay": settings.max_delay,
            "max_delay_mode": settings.max_delay_mode,
            "speechmatics": {
                "domain": effective_domain,
                "domain_requested": settings.domain,
                "model": settings.model,
                "max_delay": settings.max_delay,
                "max_delay_mode": settings.max_delay_mode,
            },
            "device_index": settings.device_index,
            "medical_vocab_enabled": settings.medical_vocab and medical is not None,
            "medical_layer_enabled": medical is not None,
            "matcher_engine": medical.engine if medical is not None else None,
            "first_partial_latency_ms": getattr(result, "first_partial_ms", None),
            "session_error": getattr(result, "error", None) if result is not None else None,
            "parse_warnings": getattr(result, "warnings", []) if result is not None else [],
            "partials": getattr(result, "partials", []) if result is not None else [],
            "final_segments": getattr(result, "final_segments", []) if result is not None else [],
            "word_results": word_results,
            "confidence_summary": confidence_summary(word_results),
            "final_transcript_raw": summary.raw,
            "final_transcript_normalized": summary.normalized,
            "final_transcript_canonical": summary.canonical,
            "review_flags": summary.review_flags,
            "entity_flags": summary.entity_flags,
            "medical_hits": medical_hits,
            "injection": {
                "auto": True,
                "enabled": injection_enabled,
                "segments": summary.injected_segments,
            },
        }
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    # ---------------------------------------------------------------- events

    def _emit_status(self, status: str) -> None:
        self._safe_call(self.callbacks.on_status, status)

    def _emit_error(self, message: str) -> None:
        self._safe_call(self.callbacks.on_error, message)

    def _emit_stopped(self, summary: SessionSummary) -> None:
        self._safe_call(self.callbacks.on_stopped, summary)

    @staticmethod
    def _safe_call(callback: Optional[Callable], *args) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            log.exception("Session callback failed")



def default_resource_root() -> Path:
    """Return the directory containing bundled project data."""

    if getattr(sys, "frozen", False):  # PyInstaller
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[1]
