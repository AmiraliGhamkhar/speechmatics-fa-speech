from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class SessionResult:
    language: str
    partials: list[dict] = field(default_factory=list)
    final_segments: list[dict] = field(default_factory=list)
    started_at: Optional[float] = None
    first_partial_ms: Optional[float] = None
    ended_at: Optional[float] = None
    error: Optional[str] = None

    @property
    def final_text(self) -> str:
        return " ".join(
            item.get("text", "").strip()
            for item in self.final_segments
            if item.get("text")
        ).strip()


class SpeechmaticsRealtime:
    """Isolated adapter for the current Speechmatics Python realtime SDK."""

    def __init__(
        self,
        api_key: str,
        language: str,
        additional_vocab: Optional[list] = None,
        max_delay: float = 1.0,
    ) -> None:
        self.api_key = api_key
        self.language = language
        self.additional_vocab = additional_vocab or []
        self.max_delay = max_delay
        self.result = SessionResult(language=language)

    @staticmethod
    def _clean_vocab(vocab: list) -> list:
        """Produce a payload accepted by Speechmatics additional_vocab schema."""
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

            # Speechmatics expects sounds_like to be an array of pronunciation
            # tokens. Do not send whitespace-containing tokens here.
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

        # Preserve order while removing duplicates.
        result = []
        seen = set()
        for item in cleaned:
            key = repr(item)
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result

    async def run(
        self,
        audio_iter,
        on_partial: Callable[[str], None],
        on_final: Callable[[str], None],
    ) -> SessionResult:
        try:
            from speechmatics.rt import (
                AsyncClient,
                ServerMessageType,
                TranscriptionConfig,
                TranscriptResult,
                OperatingPoint,
                AudioFormat,
                AudioEncoding,
            )
        except ImportError as exc:
            raise RuntimeError(
                "Speechmatics realtime SDK import failed. "
                "Run: python -m pip install --upgrade speechmatics-rt\n"
                f"Original import error: {exc}"
            ) from exc

        self.result.started_at = time.perf_counter()

        audio_format = AudioFormat(
            encoding=AudioEncoding.PCM_S16LE,
            chunk_size=4096,
            sample_rate=16000,
        )

        vocab = self._clean_vocab(self.additional_vocab)

        config_kwargs = {
            "language": self.language,
            "enable_partials": True,
            "max_delay": self.max_delay,
            "operating_point": OperatingPoint.ENHANCED,
        }
        if vocab:
            config_kwargs["additional_vocab"] = vocab

        try:
            transcription_config = TranscriptionConfig(**config_kwargs)
        except Exception as exc:
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

                await client.start_session(
                    transcription_config=transcription_config,
                    audio_format=audio_format,
                )

                async for chunk in audio_iter:
                    if chunk:
                        await client.send_audio(chunk)

                await client.end_session()

        except Exception as exc:
            self.result.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.result.ended_at = time.perf_counter()

        return self.result
