"""Isolated adapter for the Speechmatics Python realtime SDK (speechmatics-rt).

Responsibilities
----------------
- Build a valid ``TranscriptionConfig``:
  - ``max_delay`` in seconds, valid Speechmatics range 0.7-4.0
    (default 2.0, the value the docs recommend for most realtime use cases);
  - ``model`` (standard|enhanced) - configurable, not hard-coded; uses the
    modern ``model`` parameter instead of the deprecated ``operating_point``;
  - ``max_delay_mode`` = "flexible" by default so spoken entities (numbers,
    doses) are formatted completely before the final is emitted.
- Route ADD_PARTIAL_TRANSCRIPT -> ``on_partial`` (UI/overlay only) and
  ADD_TRANSCRIPT -> ``on_final`` (finalized segments only). Partials are
  recorded for benchmarking but never mixed into the final transcript.
- Clean resource handling: the async client is closed by its context manager
  and the audio iterator is always ``aclose``d (normal exit, errors, Ctrl+C).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Optional

#: Speechmatics realtime ``max_delay`` is valid between 0.7 and 4.0 seconds.
MIN_MAX_DELAY = 0.7
MAX_MAX_DELAY = 4.0
DEFAULT_MAX_DELAY = 2.0  # docs-recommended trade-off for most realtime uses

VALID_MODELS = ("standard", "enhanced")
DEFAULT_MODEL = "enhanced"
VALID_MAX_DELAY_MODES = ("fixed", "flexible")
DEFAULT_MAX_DELAY_MODE = "flexible"


@dataclass
class SessionResult:
    """Bookkeeping for one realtime session (text/metadata only)."""

    language: str
    partials: list[dict] = field(default_factory=list)
    final_segments: list[dict] = field(default_factory=list)
    started_at: Optional[float] = None
    first_partial_ms: Optional[float] = None
    ended_at: Optional[float] = None
    error: Optional[str] = None

    @property
    def final_text(self) -> str:
        """The true RAW Speechmatics final transcript (before any
        normalization), joined from finalized segments only."""
        return " ".join(
            item.get("text", "").strip()
            for item in self.final_segments
            if item.get("text")
        ).strip()


class SpeechmaticsRealtime:
    """Thin, testable wrapper around ``speechmatics.rt.AsyncClient``."""

    def __init__(
        self,
        api_key: str,
        language: str,
        additional_vocab: Optional[list] = None,
        max_delay: float = DEFAULT_MAX_DELAY,
        model: str = DEFAULT_MODEL,
        max_delay_mode: str = DEFAULT_MAX_DELAY_MODE,
    ) -> None:
        if not (MIN_MAX_DELAY <= float(max_delay) <= MAX_MAX_DELAY):
            raise ValueError(
                f"max_delay must be between {MIN_MAX_DELAY} and "
                f"{MAX_MAX_DELAY} seconds (got {max_delay})"
            )
        if model not in VALID_MODELS:
            raise ValueError(
                f"model must be one of {VALID_MODELS} (got {model!r})"
            )
        if max_delay_mode not in VALID_MAX_DELAY_MODES:
            raise ValueError(
                f"max_delay_mode must be one of {VALID_MAX_DELAY_MODES} "
                f"(got {max_delay_mode!r})"
            )
        self.api_key = api_key
        self.language = language
        self.additional_vocab = additional_vocab or []
        self.max_delay = float(max_delay)
        self.model = model
        self.max_delay_mode = max_delay_mode
        self.result = SessionResult(language=language)

    # ------------------------------------------------------------------ util

    @staticmethod
    def _clean_vocab(vocab: list) -> list:
        """Produce a payload accepted by the Speechmatics additional_vocab
        schema (plain strings or ``{"content", "sounds_like"}`` entries)."""
        cleaned = []
        for item in vocab:
            if isinstance(item, str):
                if item.strip():
                    cleaned.append(item.strip())
                continue

            if not isinstance(item, dict):
                continue

            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                continue

            sounds_like = item.get("sounds_like")
            if isinstance(sounds_like, str):
                sounds_like = [sounds_like]

            # Speechmatics expects sounds_like as an array of pronunciation
            # tokens; whitespace-containing tokens are not valid.
            if isinstance(sounds_like, list):
                valid = [
                    s.strip()
                    for s in sounds_like
                    if isinstance(s, str)
                    and s.strip()
                    and not any(ch.isspace() for ch in s)
                ]
                if valid:
                    cleaned.append({
                        "content": content.strip(),
                        "sounds_like": valid,
                    })
                    continue

            cleaned.append(content.strip())

        result = []
        seen = set()
        for item in cleaned:  # preserve order, drop duplicates
            key = repr(item)
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result

    # -------------------------------------------------------------------- run

    async def run(
        self,
        audio_iter: AsyncIterator[bytes],
        on_partial: Callable[[str], None],
        on_final: Callable[[str], None],
    ) -> SessionResult:
        """Stream ``audio_iter`` to Speechmatics and collect the session.

        Raises on SDK/network/session errors after recording the error in
        ``result.error``; the client and the audio iterator are always
        released (``finally`` + context manager).
        """
        self.result.started_at = time.perf_counter()
        try:
            from speechmatics.rt import (
                AsyncClient,
                AudioEncoding,
                AudioFormat,
                Model,
                ServerMessageType,
                TranscriptionConfig,
                TranscriptResult,
            )
        except ImportError as exc:
            self.result.error = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(
                "Speechmatics realtime SDK import failed. "
                "Run: python -m pip install --upgrade speechmatics-rt\n"
                f"Original import error: {exc}"
            ) from exc

        audio_format = AudioFormat(
            encoding=AudioEncoding.PCM_S16LE,
            sample_rate=16000,
            chunk_size=3200 * 2,  # bytes per 200 ms chunk (16-bit)
        )

        config_kwargs: dict = {
            "language": self.language,
            "model": Model(self.model),
            "enable_partials": True,
            "max_delay": self.max_delay,
            "max_delay_mode": self.max_delay_mode,
        }
        vocab = self._clean_vocab(self.additional_vocab)
        if vocab:
            config_kwargs["additional_vocab"] = vocab

        try:
            transcription_config = TranscriptionConfig(**config_kwargs)
        except Exception as exc:
            self.result.error = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(
                "Speechmatics rejected the transcription configuration.\n"
                f"Configuration: {config_kwargs}\n"
                f"Original error: {exc}"
            ) from exc

        try:
            async with AsyncClient(api_key=self.api_key) as client:
                @client.on(ServerMessageType.ADD_PARTIAL_TRANSCRIPT)
                def handle_partial(message):
                    try:
                        result = TranscriptResult.from_message(message)
                        text = (result.metadata.transcript or "").strip()
                    except Exception as exc:
                        print(f"\n[partial parse warning] {exc}")
                        return
                    if not text:
                        return
                    elapsed_ms = (
                        time.perf_counter() - self.result.started_at
                    ) * 1000.0
                    if self.result.first_partial_ms is None:
                        self.result.first_partial_ms = elapsed_ms
                    self.result.partials.append({
                        "t_ms": round(elapsed_ms, 1),
                        "text": text,
                    })
                    on_partial(text)

                @client.on(ServerMessageType.ADD_TRANSCRIPT)
                def handle_final(message):
                    try:
                        result = TranscriptResult.from_message(message)
                        text = (result.metadata.transcript or "").strip()
                    except Exception as exc:
                        print(f"\n[final parse warning] {exc}")
                        return
                    if not text:
                        return
                    elapsed_ms = (
                        time.perf_counter() - self.result.started_at
                    ) * 1000.0
                    self.result.final_segments.append({
                        "t_ms": round(elapsed_ms, 1),
                        "text": text,
                    })
                    on_final(text)

                try:
                    await client.start_session(
                        transcription_config=transcription_config,
                        audio_format=audio_format,
                    )
                    async for chunk in audio_iter:
                        if chunk:
                            await client.send_audio(chunk)
                    await client.stop_session()
                except Exception as exc:
                    self.result.error = f"{type(exc).__name__}: {exc}"
                    raise
        finally:
            self.result.ended_at = time.perf_counter()
            aclose = getattr(audio_iter, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    pass

        return self.result
