"""Isolated adapter for the Speechmatics Python realtime SDK (speechmatics-rt).

The adapter keeps partials separate from finalized segments, sends the
enhanced/flexible realtime configuration with a language-aware domain
(``domain="medical"`` only where Speechmatics documents the Enhanced Medical
model — see ``resolve_domain``), and records only useful word-level/final
-segment data from final SDK messages. Final transcript text is recovered
even when a message's structured word metadata is malformed.

Lifecycle guarantees: the EndOfTranscript wait after EndOfStream is bounded
(``STOP_SESSION_TIMEOUT``), and a server ``Error`` message is recorded in
``result.error`` and immediately stops the audio stream.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

from .text import normalize_text

#: Speechmatics realtime ``max_delay`` is valid between 0.7 and 4.0 seconds.
MIN_MAX_DELAY = 0.7
MAX_MAX_DELAY = 4.0
DEFAULT_MAX_DELAY = 2.0  # docs-recommended trade-off for most realtime uses

#: Hard bound for the EndOfTranscript wait after EndOfStream. The SDK's
#: ``stop_session`` waits unboundedly on its session-done event, so a server
#: that stalls on a HEALTHY connection would hang the app forever (network
#: death cannot: the SDK sets the same event when its receive loop fails).
#: Normal end-of-session latency is on the order of ``max_delay`` plus
#: processing, so 30 s is a generous bound.
STOP_SESSION_TIMEOUT = 30.0

VALID_MODELS = ("standard", "enhanced")
DEFAULT_MODEL = "enhanced"
VALID_MAX_DELAY_MODES = ("fixed", "flexible")
DEFAULT_MAX_DELAY_MODE = "flexible"

#: ``domain`` settings accepted from the CLI/adapter:
#:   auto    - send ``medical`` only for languages where Speechmatics
#:             documents the Enhanced Medical model, omit it otherwise;
#:   medical - force the medical domain regardless of language (explicit
#:             opt-in for enterprise/private deployments);
#:   none    - never send a domain.
VALID_DOMAINS = ("auto", "medical", "none")
DEFAULT_DOMAIN = "auto"

#: Languages the current Speechmatics documentation lists for the Enhanced
#: Medical model (Arabic, Danish, Dutch, English, Finnish, French, German,
#: Norwegian, Spanish, Swedish). Persian is NOT in that list, so ``auto``
#: must not send ``domain="medical"`` for ``fa``.
MEDICAL_DOMAIN_LANGUAGES = frozenset(
    {"ar", "da", "nl", "en", "fi", "fr", "de", "no", "es", "sv"}
)


def resolve_domain(
    language: Any, domain: Any, model: Any = DEFAULT_MODEL
) -> Optional[str]:
    """Return the domain to send (``None`` = omit it).

    Automatic medical-domain selection is an Enhanced-model capability;
    explicit settings continue to win for private/enterprise deployments.
    """
    setting = str(domain or DEFAULT_DOMAIN).strip().lower()
    if setting not in VALID_DOMAINS:
        raise ValueError(
            f"domain must be one of {VALID_DOMAINS} (got {domain!r})"
        )
    if setting == "none":
        return None
    if setting == "medical":
        return "medical"
    if str(model or DEFAULT_MODEL).strip().lower() != "enhanced":
        return None
    base = str(language or "").strip().lower().split("-")[0]
    return "medical" if base in MEDICAL_DOMAIN_LANGUAGES else None

# This threshold only marks lexical rule matches for review. It never creates
# a correction without a matching, validated rule.
LOW_CONFIDENCE_THRESHOLD = 0.75
# Values and compact clinical entities where a low confidence is useful to
# surface in the report, never to rewrite the recognized value.
_ENTITY_TOKEN = re.compile(
    r"(?:\d+(?:[.,]\d+)*(?:/\d+(?:[.,]\d+)*)*|hba1c|o2|c\d+-c\d+|q\d+h|mg|ml|mmhg)",
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


def _useful_number(value: Any) -> float | None:
    """Retain SDK numeric metadata while dropping bools and non-numbers."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _result_type_name(value: Any) -> str:
    """Normalize SDK/test enum or string result types for word filtering."""
    if isinstance(value, str):
        return value.lower()
    raw = getattr(value, "value", None)
    if isinstance(raw, str):
        return raw.lower()
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name.lower()
    return str(value).rsplit(".", 1)[-1].lower()


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
    #: Non-fatal parse/metadata problems (the transcript itself was kept).
    warnings: list[str] = field(default_factory=list)
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
        self.domain = str(domain)
        #: The domain actually sent for this language (``None`` = omitted).
        #: ``domain="medical"`` is only sent where Speechmatics documents the
        #: Enhanced Medical model for the language (or when forced).
        self.effective_domain = resolve_domain(language, self.domain, self.model)
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
        if isinstance(item, dict):
            return item.get(name, default)
        return getattr(item, name, default)

    @classmethod
    def _extract_word_results(cls, transcript_result: Any) -> list[dict[str, Any]]:
        """Keep first-alternative, final word evidence and discard SDK noise."""
        words: list[dict[str, Any]] = []
        for result in cls._field(transcript_result, "results", []) or []:
            if _result_type_name(cls._field(result, "type")) != "word":
                continue
            alternatives = cls._field(result, "alternatives", []) or []
            if not alternatives:
                continue
            alternative = alternatives[0]
            content = cls._field(alternative, "content")
            if not isinstance(content, str) or not content.strip():
                continue
            content = content.strip()

            language = cls._field(alternative, "language")
            words.append({
                "content": content,
                "confidence": _useful_number(cls._field(alternative, "confidence")),
                "language": (
                    language.strip()
                    if isinstance(language, str) and language.strip() else None
                ),
                "start_time": _useful_number(cls._field(result, "start_time")),
                "end_time": _useful_number(cls._field(result, "end_time")),
            })
        return words

    @classmethod
    def _segment_timing(cls, metadata: Any) -> dict[str, float]:
        """Return Speechmatics audio-relative segment timing when present."""
        timing: dict[str, float] = {}
        start_time = _useful_number(cls._field(metadata, "start_time"))
        end_time = _useful_number(cls._field(metadata, "end_time"))
        if start_time is not None:
            timing["start_time"] = start_time
        if end_time is not None:
            timing["end_time"] = end_time
        return timing

    # -------------------------------------------------------------------- run

    async def run(
        self,
        audio_iter: AsyncIterator[bytes],
        on_partial: Callable[[str], None],
        on_final: Callable[[str], None],
    ) -> SessionResult:
        """Stream ``audio_iter`` to Speechmatics and collect the session.

        Raises on SDK/network/session errors after recording the error in
        ``result.error``. Every path after ``started_at`` — including SDK
        import and configuration failures — runs the same ``finally``: the
        audio iterator is always released and ``ended_at`` is always set.
        """
        self.result.started_at = time.perf_counter()
        try:
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
            # Only send a domain where the Enhanced Medical model is
            # documented for the language; resolve_domain returns None for
            # Persian under the default ``auto`` setting.
            if self.effective_domain:
                config_kwargs["domain"] = self.effective_domain
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

            async with AsyncClient(api_key=self.api_key) as client:
                # Set when the service sends an Error message (expired key,
                # quota, rejected session). The SDK itself only logs the
                # reason and marks its session done, so the adapter records
                # the reason and stops feeding audio into the dead session.
                session_error = asyncio.Event()

                @client.on(ServerMessageType.ERROR)
                def handle_server_error(message):
                    reason = (
                        message.get("reason")
                        if isinstance(message, dict) else None
                    ) or "unknown server error"
                    if self.result.error is None:
                        self.result.error = f"server error: {reason}"
                    print(f"\n[server error] {reason}")
                    session_error.set()

                @client.on(ServerMessageType.ADD_PARTIAL_TRANSCRIPT)
                def handle_partial(message):
                    try:
                        result = TranscriptResult.from_message(message)
                        text = (
                            self._field(result.metadata, "transcript") or ""
                        ).strip()
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
                    text, words, metadata, warnings = (
                        self._parse_final_message(message, TranscriptResult)
                    )
                    self.result.warnings.extend(warnings)
                    if not text:
                        return
                    elapsed_ms = (
                        time.perf_counter() - self.result.started_at
                    ) * 1000.0
                    word_start = len(self.result.word_results)
                    self.result.word_results.extend(words)
                    segment = {
                        "t_ms": round(elapsed_ms, 1),
                        "text": text,
                        "word_start_index": word_start,
                        "word_end_index": len(self.result.word_results),
                    }
                    segment.update(self._segment_timing(metadata))
                    self.result.final_segments.append(segment)
                    on_final(text)

                try:
                    await client.start_session(
                        transcription_config=transcription_config,
                        audio_format=audio_format,
                    )
                    async for chunk in audio_iter:
                        # Checked before AND after the send: an error that is
                        # already known must not send any audio at all, and
                        # one that lands during a send stops the loop in the
                        # same iteration (at most one 200 ms chunk is wasted).
                        if session_error.is_set():
                            break
                        if chunk:
                            await client.send_audio(chunk)
                            if session_error.is_set():
                                break
                    try:
                        await asyncio.wait_for(
                            client.stop_session(),
                            timeout=STOP_SESSION_TIMEOUT,
                        )
                    except asyncio.TimeoutError as exc:
                        raise RuntimeError(
                            "Speechmatics did not send EndOfTranscript within "
                            f"{STOP_SESSION_TIMEOUT:.0f}s of EndOfStream; "
                            "closing the client (the transcript captured so "
                            "far is preserved)"
                        ) from exc
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

    # ------------------------------------------------------- final parsing

    @staticmethod
    def _transcript_from_raw_message(message: Any) -> str:
        """Last-resort transcript recovery from the raw event payload.

        Used only when the strict SDK parser raised: the dict-shaped wire
        message may still carry the dictated text while its structured
        results/metadata are unusable.
        """
        if not isinstance(message, dict):
            return ""
        metadata = message.get("metadata")
        candidates = [message.get("transcript")]
        if isinstance(metadata, dict):
            candidates.append(metadata.get("transcript"))
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""

    def _parse_final_message(
        self, message: Any, transcript_result_cls: Any
    ) -> tuple[str, list[dict[str, Any]], Any, list[str]]:
        """Extract ``(text, words, metadata, warnings)`` from a final event.

        The transcript text is what the clinician dictated; word-level
        evidence is optional metadata. A malformed ``results`` list must
        never cost the transcript (it used to drop the whole final segment),
        so text recovery and word extraction are intentionally separate.
        ``transcript_result_cls`` is the SDK ``TranscriptResult`` imported
        inside ``run()``.
        """
        warnings: list[str] = []
        try:
            result = transcript_result_cls.from_message(message)
            text = (self._field(result.metadata, "transcript") or "").strip()
        except Exception as exc:
            text = self._transcript_from_raw_message(message)
            if not text:
                warnings.append(
                    f"final event was dropped: {type(exc).__name__}: {exc}"
                )
                print(f"\n[final parse warning] {exc}")
                return "", [], None, warnings
            warnings.append(
                f"final metadata was malformed ({type(exc).__name__}: {exc}); "
                "transcript kept without word evidence"
            )
            print(f"\n[final kept without word metadata] {exc}")
            return text, [], None, warnings

        try:
            words = self._extract_word_results(result)
        except Exception as exc:
            words = []
            warnings.append(
                f"word metadata ignored ({type(exc).__name__}: {exc})"
            )
            print(f"\n[final word-metadata warning] {exc}")
        return text, words, self._field(result, "metadata"), warnings
