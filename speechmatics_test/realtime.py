"""Isolated adapter for the Speechmatics Python realtime SDK (speechmatics-rt).

The adapter keeps partials separate from finalized segments, sends the
medical/enhanced/flexible realtime configuration, and records only useful
word-level data from final ``TranscriptResult.results`` entries.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

from .text import normalize_text

#: Speechmatics realtime ``max_delay`` is valid between 0.7 and 4.0 seconds.
MIN_MAX_DELAY = 0.7
MAX_MAX_DELAY = 4.0
DEFAULT_MAX_DELAY = 2.0  # docs-recommended trade-off for most realtime uses

VALID_MODELS = ("standard", "enhanced")
DEFAULT_MODEL = "enhanced"
VALID_MAX_DELAY_MODES = ("fixed", "flexible")
DEFAULT_MAX_DELAY_MODE = "flexible"
DEFAULT_DOMAIN = "medical"

# This threshold only marks lexical rule matches for review. It never creates
# a correction without a matching, validated rule.
LOW_CONFIDENCE_THRESHOLD = 0.75
# Values and compact clinical entities where a low confidence is useful to
# surface in the report, never to rewrite the recognized value.
_ENTITY_TOKEN = re.compile(
    r"(?:\d+(?:[.,]\d+)*(?:/\d+(?:[.,]\d+)*)*|hba1c|o2|c\d+-c\d+|q\d+h|mg|ml)",
    re.IGNORECASE,
)


def _language_group(language: Any) -> str:
    """Classify Speechmatics language metadata without changing its value."""
    value = str(language or "").lower()
    if value.startswith(("fa", "fas", "per", "persian")):
        return "Persian"
    if value.startswith(("en", "eng", "english")):
        return "English"
    return "unknown"


def confidence_summary(word_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Return concise, report-safe confidence and language aggregates."""
    confidences = [
        float(word["confidence"])
        for word in word_results
        if isinstance(word.get("confidence"), (int, float))
        and not isinstance(word.get("confidence"), bool)
    ]
    language_counts = {"Persian": 0, "English": 0, "unknown": 0}
    for word in word_results:
        language_counts[_language_group(word.get("language"))] += 1

    uncertain_entities = []
    entity_word_count = 0
    low_confidence_entity_count = 0
    for word in word_results:
        content = word.get("content")
        confidence = word.get("confidence")
        if not isinstance(content, str) or not _ENTITY_TOKEN.fullmatch(normalize_text(content)):
            continue
        entity_word_count += 1
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) \
                and confidence < LOW_CONFIDENCE_THRESHOLD:
            low_confidence_entity_count += 1
            # These are the same five fields as a word result, limited to
            # uncertain numeric/entity tokens for a concise safety review.
            if len(uncertain_entities) < 20:
                uncertain_entities.append({
                    key: word.get(key)
                    for key in ("content", "confidence", "language", "start_time", "end_time")
                })

    summary: dict[str, Any] = {
        "word_count": len(word_results),
        "scored_word_count": len(confidences),
        "low_confidence_threshold": LOW_CONFIDENCE_THRESHOLD,
        "low_confidence_count": sum(
            value < LOW_CONFIDENCE_THRESHOLD for value in confidences
        ),
        "language_counts": language_counts,
        "entity_word_count": entity_word_count,
        "low_confidence_entity_count": low_confidence_entity_count,
        "low_confidence_entities": uncertain_entities,
    }
    if confidences:
        summary.update({
            "mean": round(sum(confidences) / len(confidences), 4),
            "min": round(min(confidences), 4),
            "max": round(max(confidences), 4),
        })
    return summary


@dataclass
class SessionResult:
    """Bookkeeping for one realtime session (text/metadata only)."""

    language: str
    partials: list[dict] = field(default_factory=list)
    final_segments: list[dict] = field(default_factory=list)
    # Flattened final-only words; segment offsets point into this list so
    # reports do not duplicate each word under every final segment.
    word_results: list[dict[str, Any]] = field(default_factory=list)
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
        domain: str = DEFAULT_DOMAIN,
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
        self.domain = domain
        self.result = SessionResult(language=language)

    # ------------------------------------------------------------------ util

    @staticmethod
    def _clean_vocab(vocab: list) -> list:
        """Return a compact valid custom-dictionary payload.

        Speechmatics accepts phrases in ``sounds_like`` (up to six words),
        which is important for letter-spelled abbreviations such as ``M R I``
        and Persianized pronunciations. Duplicate content is merged instead
        of consuming extra vocabulary slots.
        """
        cleaned: list[str | dict[str, Any]] = []
        positions: dict[str, int] = {}
        for item in vocab:
            if isinstance(item, str):
                content, sounds_like = item.strip(), []
            elif isinstance(item, dict):
                raw_content = item.get("content")
                content = raw_content.strip() if isinstance(raw_content, str) else ""
                sounds_like = item.get("sounds_like", [])
                if isinstance(sounds_like, str):
                    sounds_like = [sounds_like]
            else:
                continue

            if not content or len(content.split()) > 6:
                continue
            if not isinstance(sounds_like, list):
                sounds_like = []
            valid_sounds = []
            for sound in sounds_like:
                if not isinstance(sound, str):
                    continue
                sound = sound.strip()
                if sound and len(sound.split()) <= 6 and sound not in valid_sounds:
                    valid_sounds.append(sound)

            index = positions.get(content)
            if index is None:
                positions[content] = len(cleaned)
                cleaned.append(
                    {"content": content, "sounds_like": valid_sounds}
                    if valid_sounds else content
                )
                continue

            # Preserve the first entry's ordering while upgrading/merging its
            # pronunciations when another source names the same term.
            existing = cleaned[index]
            existing_sounds = (
                list(existing.get("sounds_like", []))
                if isinstance(existing, dict) else []
            )
            merged = existing_sounds + [
                sound for sound in valid_sounds if sound not in existing_sounds
            ]
            if merged:
                cleaned[index] = {"content": content, "sounds_like": merged}
        return cleaned

    @staticmethod
    def _field(item: Any, name: str, default: Any = None) -> Any:
        """Read SDK objects and lightweight test fixtures uniformly."""
        return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)

    @classmethod
    def _extract_word_results(cls, transcript_result: Any) -> list[dict[str, Any]]:
        """Keep first-alternative, final word evidence and discard SDK noise."""
        words: list[dict[str, Any]] = []
        for result in cls._field(transcript_result, "results", []) or []:
            if cls._field(result, "type") != "word":
                continue
            alternatives = cls._field(result, "alternatives", []) or []
            if not alternatives:
                continue
            alternative = alternatives[0]
            content = cls._field(alternative, "content")
            if not isinstance(content, str) or not content.strip():
                continue
            content = content.strip()

            def useful_number(value: Any) -> float | None:
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return float(value)
                return None

            language = cls._field(alternative, "language")
            words.append({
                "content": content,
                "confidence": useful_number(cls._field(alternative, "confidence")),
                "language": language.strip() if isinstance(language, str) and language.strip() else None,
                "start_time": useful_number(cls._field(result, "start_time")),
                "end_time": useful_number(cls._field(result, "end_time")),
            })
        return words

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
            "domain": self.domain,
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
                        words = self._extract_word_results(result)
                    except Exception as exc:
                        print(f"\n[final parse warning] {exc}")
                        return
                    if not text:
                        return
                    elapsed_ms = (
                        time.perf_counter() - self.result.started_at
                    ) * 1000.0
                    word_start = len(self.result.word_results)
                    self.result.word_results.extend(words)
                    self.result.final_segments.append({
                        "t_ms": round(elapsed_ms, 1),
                        "text": text,
                        "word_start_index": word_start,
                        "word_end_index": len(self.result.word_results),
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
