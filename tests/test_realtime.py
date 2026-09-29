"""SpeechmaticsRealtime adapter tests against a fake ``speechmatics.rt``.

Verifies: valid config values (max_delay/model/max_delay_mode), partial vs
final separation, raw final preservation, session completion, and clean
resource handling (client closed + audio iterator aclose) on success and on
Speechmatics/network errors.
"""

import asyncio
import sys
import types
from enum import Enum

import pytest

from speechmatics_test.realtime import SpeechmaticsRealtime


# --------------------------------------------------------------------- fakes

class FakeAudio:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.chunks:
            raise StopAsyncIteration
        return self.chunks.pop(0)

    async def aclose(self):
        self.closed = True


def install_fake_sdk(monkeypatch, behavior=None):
    """Install a fake speechmatics.rt module; return an inspection registry.

    ``behavior``:
      - script: list of ("partial"|"final", text) delivered one per chunk
      - fail_start: exception to raise from start_session
      - server_error: server Error reason delivered at session start
      - error_after_send: dispatch a server Error after N successful sends
      - fail_send_after: fail send_audio after N successful sends
      - fail_stop: exception to raise from stop_session
      - hang_stop: stop_session never completes (silent-server scenario)
      - hang_send_after: send_audio never completes from send N on
        (half-open websocket)
      - callbacks: not used here; callbacks are supplied per test
    """
    behavior = behavior or {}
    registry = {"configs": [], "clients": []}

    # Built functionally so a test can model an SDK release with no WARNING
    # member at all (see test_warning_subscription_degrades...).
    _message_types = {
        "ADD_PARTIAL_TRANSCRIPT": "AddPartialTranscript",
        "ADD_TRANSCRIPT": "AddTranscript",
        "ERROR": "Error",
    }
    if not behavior.get("no_warning_event"):
        _message_types["WARNING"] = "Warning"
    ServerMessageType = Enum("ServerMessageType", _message_types)

    class Model(Enum):
        STANDARD = "standard"
        ENHANCED = "enhanced"

    class OperatingPoint(Enum):  # deprecated legacy enum (must NOT be used)
        STANDARD = "standard"
        ENHANCED = "enhanced"

    class AudioEncoding(Enum):
        PCM_S16LE = "pcm_s16le"

    class AudioFormat:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class TranscriptionConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            registry["configs"].append(kwargs)

    class _Metadata:
        def __init__(self, transcript, start_time=None, end_time=None):
            self.transcript = transcript
            self.start_time = start_time
            self.end_time = end_time

    class TranscriptResult:
        def __init__(self, metadata, results=None):
            self.metadata = metadata
            self.results = results or []

        @classmethod
        def from_message(cls, message):
            if behavior.get("fail_final_parse"):
                # Simulate the strict real SDK parser hitting malformed
                # structured fields on a final event.
                raise KeyError("results")
            metadata = message.get("metadata") or {}
            return cls(
                _Metadata(
                    message.get("transcript"),
                    metadata.get("start_time"),
                    metadata.get("end_time"),
                ),
                message.get("results"),
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.handlers = {}
            self.sent = []
            self.stopped = False
            self.closed = False
            self._script = list(behavior.get("script", []))
            registry["clients"].append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            self.closed = True
            return False

        def on(self, event, callback=None):
            # Mirrors the real EventEmitter, which accepts both the decorator
            # form and a direct (event, callback) registration.
            if callback is not None:
                self.handlers[event] = callback
                return callback

            def decorator(callback):
                self.handlers[event] = callback
                return callback
            return decorator

        async def start_session(self, transcription_config=None,
                                audio_format=None, **kw):
            if behavior.get("fail_start"):
                raise behavior["fail_start"]
            if behavior.get("server_error"):
                handler = self.handlers.get(ServerMessageType.ERROR)
                if handler:
                    handler({"message": "Error", "reason": behavior["server_error"]})
            for warning in behavior.get("server_warnings") or ():
                handler = self.handlers.get(ServerMessageType.WARNING)
                if handler:
                    handler(warning)

        async def send_audio(self, chunk):
            if behavior.get("hang_send_after") is not None and \
                    len(self.sent) >= behavior["hang_send_after"]:
                # A write on a half-open TCP connection: the SDK's send has no
                # timeout of its own, so it would await forever.
                await asyncio.sleep(3600)
            if behavior.get("fail_send_after") is not None and \
                    len(self.sent) >= behavior["fail_send_after"]:
                raise RuntimeError("websocket send failed")
            self.sent.append(chunk)
            if behavior.get("error_after_send") and \
                    len(self.sent) == behavior["error_after_send"]:
                handler = self.handlers.get(ServerMessageType.ERROR)
                if handler:
                    handler({"message": "Error", "reason": "session rejected"})
            if self._script:
                item = self._script.pop(0)
                kind, text = item[:2]
                results = item[2] if len(item) > 2 else []
                metadata = item[3] if len(item) > 3 else {}
                event = (
                    ServerMessageType.ADD_PARTIAL_TRANSCRIPT
                    if kind == "partial" else ServerMessageType.ADD_TRANSCRIPT
                )
                if event in self.handlers:
                    self.handlers[event]({
                        "transcript": text,
                        "results": results,
                        "metadata": metadata,
                    })

        async def stop_session(self):
            if behavior.get("fail_stop"):
                raise behavior["fail_stop"]
            if behavior.get("hang_stop"):
                # Mimics AsyncClient.stop_session waiting on a session-done
                # event that only EndOfTranscript can set.
                await asyncio.sleep(3600)
            self.stopped = True

    rt = types.ModuleType("speechmatics.rt")
    rt.AsyncClient = FakeClient
    rt.ServerMessageType = ServerMessageType
    rt.Model = Model
    rt.OperatingPoint = OperatingPoint
    rt.AudioEncoding = AudioEncoding
    rt.AudioFormat = AudioFormat
    rt.TranscriptionConfig = TranscriptionConfig
    rt.TranscriptResult = TranscriptResult
    rt.SessionError = type("SessionError", (Exception,), {})
    rt.TransportError = type("TransportError", (Exception,), {})
    pkg = types.ModuleType("speechmatics")
    pkg.rt = rt
    monkeypatch.setitem(sys.modules, "speechmatics", pkg)
    monkeypatch.setitem(sys.modules, "speechmatics.rt", rt)
    return registry


def run_ok(monkeypatch, script=None):
    registry = install_fake_sdk(monkeypatch, {"script": script or []})
    stt = SpeechmaticsRealtime(api_key="test-key", language="fa")
    audio = FakeAudio([b"a", b"b", b"c", b"d"])
    partials, finals = [], []
    result = asyncio.run(stt.run(audio, partials.append, finals.append))
    return result, audio, partials, finals, registry


# ------------------------------------------------------- partial vs final

def test_partial_vs_final_separation(monkeypatch):
    result, audio, partials, finals, registry = run_ok(
        monkeypatch,
        script=[
            ("partial", "بیمار در"),
            ("final", "بیمار در سی تی اسکن"),
            ("partial", "و فشار"),
            ("final", "و فشار خون بالا"),
        ],
    )
    assert partials == ["بیمار در", "و فشار"]
    assert finals == ["بیمار در سی تی اسکن", "و فشار خون بالا"]
    assert result.partials == [
        {"t_ms": result.partials[0]["t_ms"], "text": "بیمار در"},
        {"t_ms": result.partials[1]["t_ms"], "text": "و فشار"},
    ]
    assert [f["text"] for f in result.final_segments] == [
        "بیمار در سی تی اسکن", "و فشار خون بالا"
    ]
    # raw final transcript is built from finals only, unnormalized
    assert result.final_text == "بیمار در سی تی اسکن و فشار خون بالا"


def test_partials_never_leak_into_final(monkeypatch):
    result, *_ = run_ok(monkeypatch, script=[
        ("partial", "partial only text"),
        ("final", "final text"),
    ])
    assert "partial only" not in result.final_text
    assert result.final_text == "final text"
    assert result.first_partial_ms is not None


# ------------------------------------------------------------------- config

def test_config_values_sent(monkeypatch):
    registry = install_fake_sdk(monkeypatch, {"script": []})
    stt = SpeechmaticsRealtime(
        api_key="k", language="en",
        # same-script pronunciation: this test is about the config that gets
        # sent, not about the script filter (see
        # test_wrong_script_pronunciations_are_dropped_and_reported).
        additional_vocab=[
            "CT scan", {"content": "lesion", "sounds_like": ["lee zhun"]},
        ],
    )
    audio = FakeAudio([b"a"])
    asyncio.run(stt.run(audio, lambda t: None, lambda t: None))
    cfg = registry["configs"][0]
    assert cfg["language"] == "en"
    assert cfg["domain"] == "medical"
    assert cfg["model"].name == "ENHANCED"
    assert cfg["enable_partials"] is True
    assert cfg["max_delay"] == 2.0
    assert cfg["max_delay_mode"] == "flexible"
    assert cfg["additional_vocab"] == [
        "CT scan", {"content": "lesion", "sounds_like": ["lee zhun"]}
    ]
    # deprecated operating_point must not be used
    assert "operating_point" not in cfg


def test_config_custom_model_and_delay(monkeypatch):
    registry = install_fake_sdk(monkeypatch, {"script": []})
    stt = SpeechmaticsRealtime(
        api_key="k", language="fa",
        model="standard", max_delay=0.7, max_delay_mode="fixed",
    )
    audio = FakeAudio([b"a"])
    asyncio.run(stt.run(audio, lambda t: None, lambda t: None))
    cfg = registry["configs"][0]
    assert cfg["model"].name == "STANDARD"
    assert cfg["max_delay"] == 0.7
    assert cfg["max_delay_mode"] == "fixed"


def test_max_delay_validation():
    with pytest.raises(ValueError):
        SpeechmaticsRealtime(api_key="k", language="fa", max_delay=0.5)
    with pytest.raises(ValueError):
        SpeechmaticsRealtime(api_key="k", language="fa", max_delay=4.5)
    # boundaries are valid
    SpeechmaticsRealtime(api_key="k", language="fa", max_delay=0.7)
    SpeechmaticsRealtime(api_key="k", language="fa", max_delay=4.0)


@pytest.mark.parametrize("max_delay", [2.0, 2.5, 3.0, 3.5, 4.0])
def test_benchmark_max_delay_values_are_supported(max_delay):
    stt = SpeechmaticsRealtime(api_key="k", language="fa", max_delay=max_delay)
    assert stt.max_delay == max_delay


def test_model_validation():
    with pytest.raises(ValueError):
        SpeechmaticsRealtime(api_key="k", language="fa", model="bogus")
    with pytest.raises(ValueError):
        SpeechmaticsRealtime(api_key="k", language="fa", max_delay_mode="bogus")


def test_vocab_cleaning(monkeypatch):
    registry = install_fake_sdk(monkeypatch, {"script": []})
    stt = SpeechmaticsRealtime(
        api_key="k", language="fa",
        additional_vocab=[
            "  CT scan  ", "", "CT scan",          # dupes / empty
            # two same-script pronunciations: both must survive the merge
            {"content": "lesion",
             "sounds_like": ["لیژن", "لزیون"]},
            {"content": "  "},                      # no usable content
            42,                                     # junk
        ],
    )
    audio = FakeAudio([b"a"])
    asyncio.run(stt.run(audio, lambda t: None, lambda t: None))
    assert registry["configs"][0]["additional_vocab"] == [
        "CT scan",
        {"content": "lesion", "sounds_like": ["لیژن", "لزیون"]},
    ]


def test_wrong_script_pronunciations_are_dropped_and_reported(monkeypatch):
    """REGRESSION (D6): ``sounds_like`` only works in the language's own script.

    Speechmatics documents that a custom-dictionary pronunciation is applied
    only in the MAIN SCRIPT of the session language, so a Latin ``sounds_like``
    on a Persian session (and a Persian one on an English session) cannot take
    effect: the service ignores it and answers with an in-band
    ``validation_warning``. The exported vocabulary is shared by both language
    streams and carries 345 pronunciations, 4 of which are Latin - so the
    filter has to live where the language is known.

    The TERM itself must still be sent (only the dead hint goes), and the drop
    must be REPORTED: silently discarding it would let the operator believe a
    term is pronunciation-biased when it is not.
    """
    registry = install_fake_sdk(monkeypatch, {"script": []})
    vocab = [
        {"content": "MRI", "sounds_like": ["M R I", "ام آر آی"]},
        {"content": "پنی سیلین", "sounds_like": ["پنیسیلین"]},
    ]

    stt = SpeechmaticsRealtime(api_key="k", language="fa", additional_vocab=vocab)
    asyncio.run(stt.run(FakeAudio([b"a"]), lambda t: None, lambda t: None))
    # Persian session: the Latin hint goes, the Persian one and BOTH terms stay
    assert registry["configs"][0]["additional_vocab"] == [
        {"content": "MRI", "sounds_like": ["ام آر آی"]},
        {"content": "پنی سیلین", "sounds_like": ["پنیسیلین"]},
    ]
    assert len(stt.result.vocabulary_notes) == 1
    assert "'MRI'" in stt.result.vocabulary_notes[0]
    assert "'M R I'" in stt.result.vocabulary_notes[0]
    # a configuration note is not a message-parsing problem
    assert stt.result.warnings == []

    registry = install_fake_sdk(monkeypatch, {"script": []})
    stt = SpeechmaticsRealtime(api_key="k", language="en", additional_vocab=vocab)
    asyncio.run(stt.run(FakeAudio([b"a"]), lambda t: None, lambda t: None))
    # English session: the mirror image - Persian hints go, both terms stay
    assert registry["configs"][0]["additional_vocab"] == [
        {"content": "MRI", "sounds_like": ["M R I"]},
        "پنی سیلین",
    ]
    # both Persian hints are unusable on an English session
    assert len(stt.result.vocabulary_notes) == 2

    # A region subtag must classify the same way as its base language.
    registry = install_fake_sdk(monkeypatch, {"script": []})
    stt = SpeechmaticsRealtime(api_key="k", language="fa-IR", additional_vocab=vocab)
    asyncio.run(stt.run(FakeAudio([b"a"]), lambda t: None, lambda t: None))
    assert len(stt.result.vocabulary_notes) == 1

    # No language -> exactly the legacy behaviour (nothing is filtered), so the
    # staticmethod stays usable for dictionary-side validation.
    assert SpeechmaticsRealtime._clean_vocab(vocab) == [
        {"content": "MRI", "sounds_like": ["M R I", "ام آر آی"]},
        {"content": "پنی سیلین", "sounds_like": ["پنیسیلین"]},
    ]


def test_unclassifiable_pronunciations_are_kept(monkeypatch):
    """A digits/punctuation-only or genuinely mixed hint is NOT dropped.

    The filter decides what to SEND, so an uncertain verdict is left to the
    service's own validation instead of being silently discarded here.
    """
    registry = install_fake_sdk(monkeypatch, {"script": []})
    vocab = [{"content": "B12", "sounds_like": ["12", "B12 بی تولف", "C3-C4"]}]
    stt = SpeechmaticsRealtime(api_key="k", language="fa", additional_vocab=vocab)
    asyncio.run(stt.run(FakeAudio([b"a"]), lambda t: None, lambda t: None))
    sent = registry["configs"][0]["additional_vocab"][0]["sounds_like"]
    assert "12" in sent                      # no letters at all: no opinion
    assert "B12 بی تولف" in sent           # a real mix: kept
    assert "C3-C4" not in sent               # unambiguously Latin: dropped
    assert len(stt.result.vocabulary_notes) == 1


def test_server_warnings_are_recorded_not_only_logged(monkeypatch):
    """REGRESSION (D7): the SDK's own WARNING handler only writes to its logger.

    A service complaint therefore never reached the session report. The one
    that matters most is ``validation_warning``, sent in-band BEFORE
    RecognitionStarted when an ``additional_vocab`` entry is rejected - a
    pronunciation bias that silently never happened. ``idle_timeout`` /
    ``duration_limit_exceeded`` explain a transcript that stopped early.
    """
    registry = install_fake_sdk(monkeypatch, {
        "script": [("final", "سی تی اسکن")],
        "server_warnings": [
            {"message": "additional_vocab entry ignored",
             "reason": "validation_warning", "type": "Warning"},
            {"message": "idle timeout reached",
             "reason": "idle_timeout", "type": "Warning"},
            # a repeated reason must not bury the report
            {"message": "idle timeout reached",
             "reason": "idle_timeout", "type": "Warning"},
            {},                                    # no reason/message: ignored
            "not a dict",                          # malformed: ignored
        ],
    })
    stt = SpeechmaticsRealtime(api_key="k", language="fa")
    result = asyncio.run(
        stt.run(FakeAudio([b"a"]), lambda t: None, lambda t: None)
    )
    assert result.service_warnings == [
        "validation_warning additional_vocab entry ignored",
        "idle_timeout idle timeout reached",
    ]
    # warnings are review-only: the transcript and its parse state are untouched
    assert result.final_text == "سی تی اسکن"
    assert result.warnings == []
    assert result.error is None
    assert registry["clients"][0].stopped is True


def test_warning_subscription_degrades_when_the_sdk_has_no_warning(monkeypatch):
    """Observability must never cost a session.

    ``ServerMessageType.WARNING`` is resolved by name, not imported directly:
    on an SDK build without that member the adapter surfaces no warnings
    instead of raising inside ``run()`` and discarding the transcript.
    """
    registry = install_fake_sdk(monkeypatch, {
        "script": [("final", "CT scan")],
        "no_warning_event": True,
    })
    import speechmatics.rt as rt

    assert "WARNING" not in rt.ServerMessageType.__members__
    stt = SpeechmaticsRealtime(api_key="k", language="fa")
    result = asyncio.run(
        stt.run(FakeAudio([b"a"]), lambda t: None, lambda t: None)
    )
    assert result.final_text == "CT scan"      # session still completed
    assert result.service_warnings == []       # and simply reported none
    assert result.error is None
    assert registry["clients"][0].stopped is True


def test_final_word_results_preserve_confidence_language_and_timing(monkeypatch):
    registry = install_fake_sdk(monkeypatch, {"script": [
        ("final", "HbA1c 5 mg", [
            {
                "type": "word", "start_time": 1.0, "end_time": 1.2,
                "alternatives": [
                    {"content": "HbA1c", "confidence": 0.96, "language": "en"},
                    {"content": "Hb A1c", "confidence": 0.20, "language": "en"},
                ],
            },
            {
                "type": "word", "start_time": 1.21, "end_time": 1.3,
                "alternatives": [{"content": "5", "confidence": 0.62, "language": "en"}],
            },
            {
                "type": "word", "start_time": 1.31, "end_time": 1.5,
                "alternatives": [{"content": "mg", "confidence": 0.91, "language": "en"}],
            },
            {
                "type": "punctuation", "start_time": 1.5, "end_time": 1.5,
                "alternatives": [{"content": ".", "confidence": 1.0, "language": "en"}],
            },
        ]),
    ]})
    stt = SpeechmaticsRealtime(api_key="k", language="en")
    result = asyncio.run(stt.run(FakeAudio([b"a"]), lambda t: None, lambda t: None))

    assert result.word_results == [
        {"content": "HbA1c", "confidence": 0.96, "language": "en", "start_time": 1.0, "end_time": 1.2},
        {"content": "5", "confidence": 0.62, "language": "en", "start_time": 1.21, "end_time": 1.3},
        {"content": "mg", "confidence": 0.91, "language": "en", "start_time": 1.31, "end_time": 1.5},
    ]
    assert result.final_segments[0]["word_start_index"] == 0
    assert result.final_segments[0]["word_end_index"] == 3
    assert registry["configs"][0]["domain"] == "medical"


def test_final_segments_preserve_speechmatics_timing(monkeypatch):
    result, *_ = run_ok(monkeypatch, script=[
        (
            "final", "clinical segment", [],
            {"start_time": 2.0, "end_time": 2.8},
        ),
    ])
    assert result.final_segments[0]["start_time"] == 2.0
    assert result.final_segments[0]["end_time"] == 2.8


def test_word_result_type_accepts_sdk_style_enums():
    class ResultType(Enum):
        WORD = "word"
        PUNCTUATION = "punctuation"

    transcript_result = {
        "results": [
            {
                "type": ResultType.WORD,
                "start_time": 0.0,
                "end_time": 0.2,
                "alternatives": [
                    {"content": "MRI", "confidence": 0.88, "language": "en"},
                ],
            },
            {
                "type": ResultType.PUNCTUATION,
                "start_time": 0.2,
                "end_time": 0.2,
                "alternatives": [
                    {"content": ".", "confidence": 1.0, "language": "en"},
                ],
            },
        ],
    }
    assert SpeechmaticsRealtime._extract_word_results(transcript_result) == [
        {
            "content": "MRI", "confidence": 0.88, "language": "en",
            "start_time": 0.0, "end_time": 0.2,
        },
    ]


def test_missing_structured_results_keeps_final_text_and_empty_words(monkeypatch):
    result, *_ = run_ok(monkeypatch, script=[("final", "CT scan 120/80")])
    assert result.final_text == "CT scan 120/80"
    assert result.word_results == []
    assert result.final_segments[0]["word_start_index"] == 0
    assert result.final_segments[0]["word_end_index"] == 0


def test_no_vocab_omits_only_custom_vocabulary(monkeypatch):
    registry = install_fake_sdk(monkeypatch, {"script": []})
    stt = SpeechmaticsRealtime(api_key="k", language="fa", additional_vocab=[])
    asyncio.run(stt.run(FakeAudio([b"a"]), lambda t: None, lambda t: None))
    cfg = registry["configs"][0]
    assert "additional_vocab" not in cfg
    # fa is not documented for the Enhanced Medical model: auto omits it
    assert "domain" not in cfg
    assert stt.effective_domain is None


# -------------------------------------------------------------- session end

def test_stop_session_called_and_client_closed(monkeypatch):
    result, audio, partials, finals, registry = run_ok(monkeypatch)
    client = registry["clients"][0]
    assert client.stopped is True
    assert client.closed is True
    assert audio.closed is True
    assert result.error is None
    assert result.ended_at is not None and result.started_at is not None


def test_empty_chunks_not_sent(monkeypatch):
    registry = install_fake_sdk(monkeypatch, {"script": []})
    stt = SpeechmaticsRealtime(api_key="k", language="fa")
    audio = FakeAudio([b"", b"abc", b""])
    asyncio.run(stt.run(audio, lambda t: None, lambda t: None))
    assert registry["clients"][0].sent == [b"abc"]


# --------------------------------------------------------------- error paths

def test_start_error_records_and_cleans_up(monkeypatch):
    registry = install_fake_sdk(monkeypatch, {"fail_start": RuntimeError("boom")})
    stt = SpeechmaticsRealtime(api_key="k", language="fa")
    audio = FakeAudio([b"a", b"b"])
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(stt.run(audio, lambda t: None, lambda t: None))
    assert "boom" in stt.result.error
    assert stt.result.ended_at is not None
    assert registry["clients"][0].closed is True
    assert audio.closed is True  # audio iterator always released


def test_send_error_records_and_cleans_up(monkeypatch):
    registry = install_fake_sdk(monkeypatch, {"fail_send_after": 1})
    stt = SpeechmaticsRealtime(api_key="k", language="fa")
    audio = FakeAudio([b"a", b"b", b"c"])
    with pytest.raises(RuntimeError, match="send failed"):
        asyncio.run(stt.run(audio, lambda t: None, lambda t: None))
    assert "send failed" in stt.result.error
    assert registry["clients"][0].closed is True
    assert audio.closed is True


def test_stop_error_records_and_cleans_up(monkeypatch):
    registry = install_fake_sdk(monkeypatch, {"fail_stop": RuntimeError("eof")})
    stt = SpeechmaticsRealtime(api_key="k", language="fa")
    audio = FakeAudio([b"a"])
    with pytest.raises(RuntimeError, match="eof"):
        asyncio.run(stt.run(audio, lambda t: None, lambda t: None))
    assert "eof" in stt.result.error
    assert registry["clients"][0].closed is True
    assert audio.closed is True


def test_bad_message_is_skipped_not_fatal(monkeypatch):
    install_fake_sdk(monkeypatch, {"script": [
        ("partial", None),   # missing transcript -> parse path handles None
        ("final", "ok"),
    ]})
    stt = SpeechmaticsRealtime(api_key="k", language="fa")
    audio = FakeAudio([b"a", b"b"])
    result = asyncio.run(stt.run(audio, lambda t: None, lambda t: None))
    assert [f["text"] for f in result.final_segments] == ["ok"]
    assert result.error is None


# ------------------------------------------------- domain language support

def test_resolve_domain_matrix():
    from speechmatics_test.realtime import resolve_domain
    # documented Enhanced Medical languages keep the domain under auto
    for language in ["en", "de", "ar", "sv", "EN", "en-US"]:
        assert resolve_domain(language, "auto") == "medical"
    # Persian is not documented for Enhanced Medical
    assert resolve_domain("fa", "auto") is None
    assert resolve_domain("fa-IR", "auto") is None
    # explicit settings win
    assert resolve_domain("fa", "medical") == "medical"
    assert resolve_domain("en", "none") is None
    assert resolve_domain("fa", "none") is None


def test_config_invalid_domain_rejected():
    with pytest.raises(ValueError):
        SpeechmaticsRealtime(api_key="k", language="en", domain="bogus")


def test_fa_omits_undocumented_medical_domain(monkeypatch):
    registry = install_fake_sdk(monkeypatch, {"script": []})
    stt = SpeechmaticsRealtime(api_key="k", language="fa")
    asyncio.run(stt.run(FakeAudio([b"a"]), lambda t: None, lambda t: None))
    assert "domain" not in registry["configs"][0]
    assert stt.effective_domain is None


def test_medical_domain_can_be_forced(monkeypatch):
    registry = install_fake_sdk(monkeypatch, {"script": []})
    stt = SpeechmaticsRealtime(api_key="k", language="fa", domain="medical")
    asyncio.run(stt.run(FakeAudio([b"a"]), lambda t: None, lambda t: None))
    assert registry["configs"][0]["domain"] == "medical"
    assert stt.effective_domain == "medical"


def test_domain_none_omits_even_for_supported_languages(monkeypatch):
    registry = install_fake_sdk(monkeypatch, {"script": []})
    stt = SpeechmaticsRealtime(api_key="k", language="en", domain="none")
    asyncio.run(stt.run(FakeAudio([b"a"]), lambda t: None, lambda t: None))
    assert "domain" not in registry["configs"][0]
    assert stt.effective_domain is None


# ------------------------------------------- malformed final metadata (M3)

def test_malformed_final_metadata_keeps_transcript(monkeypatch):
    install_fake_sdk(monkeypatch, {
        "script": [("final", "بیمار در سی سی یو است")],
        "fail_final_parse": True,
    })
    stt = SpeechmaticsRealtime(api_key="k", language="fa")
    finals = []
    result = asyncio.run(stt.run(FakeAudio([b"a"]), lambda t: None, finals.append))
    # the dictated text survives without word evidence
    assert finals == ["بیمار در سی سی یو است"]
    assert result.final_text == "بیمار در سی سی یو است"
    assert result.word_results == []
    assert result.final_segments[0]["word_start_index"] == 0
    assert result.final_segments[0]["word_end_index"] == 0
    assert result.warnings and "malformed" in result.warnings[0]
    assert result.error is None


def test_word_metadata_failure_keeps_transcript_and_words(monkeypatch):
    install_fake_sdk(monkeypatch, {"script": [("final", "CT scan")]})
    stt = SpeechmaticsRealtime(api_key="k", language="en")

    def broken_extract(transcript_result):
        raise TypeError("bad results payload")

    monkeypatch.setattr(SpeechmaticsRealtime, "_extract_word_results", staticmethod(broken_extract))
    result = asyncio.run(stt.run(FakeAudio([b"a"]), lambda t: None, lambda t: None))
    assert result.final_text == "CT scan"
    assert result.word_results == []
    assert result.warnings and "word metadata ignored" in result.warnings[0]


def test_transcript_from_raw_message_recovers_both_shapes():
    parse = SpeechmaticsRealtime._transcript_from_raw_message
    assert parse({"transcript": "top level"}) == "top level"
    assert parse({"metadata": {"transcript": "in metadata"}}) == "in metadata"
    assert parse({"metadata": "junk", "transcript": "  padded  "}) == "padded"
    assert parse({"metadata": {}}) == ""
    assert parse("not a dict") == ""
    assert parse({}) == ""


# ------------------------------------- setup-failure cleanup (M4/H1 paths)

def test_import_failure_releases_audio_and_sets_ended_at(monkeypatch):
    install_fake_sdk(monkeypatch, {"script": []})
    rt_module = sys.modules["speechmatics.rt"]
    monkeypatch.delattr(rt_module, "Model")
    stt = SpeechmaticsRealtime(api_key="k", language="fa")
    audio = FakeAudio([b"a"])
    with pytest.raises(RuntimeError, match="import failed"):
        asyncio.run(stt.run(audio, lambda t: None, lambda t: None))
    assert stt.result.ended_at is not None
    assert audio.closed is True
    assert stt.result.error and "ImportError" in stt.result.error


def test_audio_format_failure_releases_audio_and_sets_ended_at(monkeypatch):
    install_fake_sdk(monkeypatch, {"script": []})
    rt_module = sys.modules["speechmatics.rt"]

    class Boom:
        def __init__(self, **kwargs):
            raise TypeError("bad audio format")

    monkeypatch.setattr(rt_module, "AudioFormat", Boom)
    stt = SpeechmaticsRealtime(api_key="k", language="en")
    audio = FakeAudio([b"a"])
    with pytest.raises(TypeError, match="bad audio format"):
        asyncio.run(stt.run(audio, lambda t: None, lambda t: None))
    assert stt.result.ended_at is not None
    assert audio.closed is True


# ------------------------------------------ hung EndOfTranscript (BUG 1)

def test_stop_session_hang_is_bounded_and_preserves_transcript(monkeypatch):
    """A server that stalls on a HEALTHY connection must not hang the app
    forever: the EndOfTranscript wait is bounded, the transcript captured
    so far survives, and every resource is still released."""
    import speechmatics_test.realtime as realtime_module

    registry = install_fake_sdk(monkeypatch, {
        "script": [("final", "بیمار در سی سی یو است")],
        "hang_stop": True,
    })
    monkeypatch.setattr(realtime_module, "STOP_SESSION_TIMEOUT", 0.05)
    stt = SpeechmaticsRealtime(api_key="k", language="fa")
    audio = FakeAudio([b"a", b"b"])
    finals = []
    with pytest.raises(RuntimeError, match="EndOfTranscript"):
        asyncio.run(stt.run(audio, lambda t: None, finals.append))
    # the dictated transcript survived the shutdown hang
    assert finals == ["بیمار در سی سی یو است"]
    assert stt.result.final_text == "بیمار در سی سی یو است"
    assert "EndOfTranscript" in stt.result.error
    assert stt.result.ended_at is not None
    assert audio.closed is True
    client = registry["clients"][0]
    assert client.closed is True   # context-manager teardown still ran
    assert client.stopped is False  # stop_session never completed


# --------------------------------------------- server Error message (BUG 2)

def test_server_error_is_recorded_and_stops_the_send_loop(monkeypatch):
    """The service's Error message must land in result.error and the app
    must stop streaming audio into the dead session immediately."""
    registry = install_fake_sdk(monkeypatch, {"server_error": "quota exceeded"})
    stt = SpeechmaticsRealtime(api_key="k", language="en")
    audio = FakeAudio([b"a", b"b", b"c", b"d"])
    result = asyncio.run(stt.run(audio, lambda t: None, lambda t: None))
    assert result.error == "server error: quota exceeded"
    # the error was known before the first send: no audio enters a dead session
    assert registry["clients"][0].sent == []
    assert audio.closed is True
    assert result.ended_at is not None


def test_server_error_mid_stream_stops_after_at_most_one_chunk(monkeypatch):
    """An Error landing during a send suppresses every further chunk (the
    in-flight 200 ms chunk is the unavoidable worst case)."""
    registry = install_fake_sdk(monkeypatch, {"error_after_send": 1})
    stt = SpeechmaticsRealtime(api_key="k", language="en")
    audio = FakeAudio([b"a", b"b", b"c", b"d"])
    result = asyncio.run(stt.run(audio, lambda t: None, lambda t: None))
    assert result.error == "server error: session rejected"
    assert registry["clients"][0].sent == [b"a"]
    assert result.final_text == ""


def test_server_error_survives_a_failing_teardown(monkeypatch):
    """The service's reason must not be replaced by the teardown's error.

    The SDK only logs an Error message and marks the session done, so a server
    error is normally followed by a failing ``stop_session`` - the transport is
    already going away. Recording that transport error over the reason replaced
    the one actionable message ("your API key has expired", quota, rejected
    session) in the report and the UI with a meaningless transport failure.
    """
    registry = install_fake_sdk(monkeypatch, {
        "server_error": "Your API key has expired",
        "fail_stop": RuntimeError("websocket closed"),
    })
    stt = SpeechmaticsRealtime(api_key="k", language="en")
    audio = FakeAudio([b"a", b"b"])

    with pytest.raises(RuntimeError):
        # The transport failure is still raised to the caller...
        asyncio.run(stt.run(audio, lambda t: None, lambda t: None))

    # ...but the report keeps the reason the service actually gave.
    assert stt.result.error == "server error: Your API key has expired"
    assert registry["clients"][0].stopped is False
    assert audio.closed is True
    assert stt.result.ended_at is not None


def test_transport_error_is_still_recorded_without_a_server_error(monkeypatch):
    """The guard must not swallow a real transport failure on its own."""
    install_fake_sdk(monkeypatch, {"fail_stop": RuntimeError("eof")})
    stt = SpeechmaticsRealtime(api_key="k", language="en")
    audio = FakeAudio([b"a"])

    with pytest.raises(RuntimeError):
        asyncio.run(stt.run(audio, lambda t: None, lambda t: None))

    assert stt.result.error == "RuntimeError: eof"


def test_server_error_before_any_audio_still_completes(monkeypatch):
    registry = install_fake_sdk(monkeypatch, {"server_error": "rejected"})
    stt = SpeechmaticsRealtime(api_key="k", language="en")
    audio = FakeAudio([b"a"])
    result = asyncio.run(stt.run(audio, lambda t: None, lambda t: None))
    assert result.error == "server error: rejected"
    assert registry["clients"][0].sent == []
    assert result.final_text == ""
    assert result.ended_at is not None


# ------------------------------------- stalled send (network half-open, BUG 3)

def test_send_stall_is_bounded_and_preserves_the_transcript(monkeypatch):
    """A websocket write that never completes must not hang the session.

    The SDK's ``send_audio`` has no timeout of its own, so on a half-open
    connection it awaited forever: no final, no error, no shutdown, and the
    microphone kept feeding an iterator nobody consumed. The send is bounded,
    the failure is reported with a clear reason, and the transcript captured
    before the stall survives.
    """
    import speechmatics_test.realtime as realtime_module

    registry = install_fake_sdk(monkeypatch, {
        "script": [("final", "بیمار در سی سی یو است")],
        "hang_send_after": 1,
    })
    monkeypatch.setattr(realtime_module, "SEND_AUDIO_TIMEOUT", 0.05)
    stt = SpeechmaticsRealtime(api_key="k", language="fa")
    audio = FakeAudio([b"a", b"b", b"c"])
    finals = []
    with pytest.raises(RuntimeError, match="network"):
        asyncio.run(stt.run(audio, lambda t: None, finals.append))
    assert finals == ["بیمار در سی سی یو است"]
    assert stt.result.final_text == "بیمار در سی سی یو است"
    assert "network" in (stt.result.error or "")
    assert stt.result.ended_at is not None
    assert audio.closed is True
    assert registry["clients"][0].closed is True


def test_send_timeout_does_not_clobber_a_server_error(monkeypatch):
    """The service's own reason still wins over the transport symptom."""
    import speechmatics_test.realtime as realtime_module

    install_fake_sdk(monkeypatch, {
        "server_error": "quota exceeded",
        "hang_send_after": 1,
    })
    monkeypatch.setattr(realtime_module, "SEND_AUDIO_TIMEOUT", 0.05)
    stt = SpeechmaticsRealtime(api_key="k", language="en")
    result = asyncio.run(stt.run(FakeAudio([b"a"]), lambda t: None, lambda t: None))
    assert result.error == "server error: quota exceeded"


# ------------------------------------ session-state isolation (BUG 4)

def test_reusing_an_instance_starts_from_a_clean_result(monkeypatch):
    """No partial, final, word, warning or error may leak between sessions."""
    registry = install_fake_sdk(monkeypatch, {"script": [
        ("partial", "first partial"),
        ("final", "first final"),
    ]})
    stt = SpeechmaticsRealtime(api_key="k", language="en")
    first = asyncio.run(
        stt.run(FakeAudio([b"a", b"b"]), lambda t: None, lambda t: None)
    )
    assert first.final_text == "first final"
    assert len(first.partials) == 1
    assert first.error is None

    # Second session on the SAME instance: a fresh client replays the same
    # script, so the bookkeeping must hold exactly ONE session's worth of
    # data. Leaking the previous SessionResult would double every count.
    registry["clients"].clear()
    second = asyncio.run(
        stt.run(FakeAudio([b"b", b"c"]), lambda t: None, lambda t: None)
    )
    assert second is stt.result
    assert second is not first
    assert len(second.partials) == 1          # not 2: no stale partials
    assert len(second.final_segments) == 1    # not 2: no stale finals
    assert second.final_text == "first final"
    assert second.word_results == []
    assert second.warnings == []
    assert second.error is None
    assert second.partials is not first.partials
    # the first session's result object is untouched
    assert len(first.partials) == 1
    assert first.final_text == "first final"


def test_reset_clears_state_and_refuses_while_running(monkeypatch):
    install_fake_sdk(monkeypatch, {"script": [("final", "text")]})
    stt = SpeechmaticsRealtime(api_key="k", language="en")
    asyncio.run(stt.run(FakeAudio([b"a"]), lambda t: None, lambda t: None))
    assert stt.result.final_text == "text"
    assert stt.is_running is False
    stt.reset()
    assert stt.result.final_text == ""
    assert stt.result.started_at is None
    stt._running = True
    with pytest.raises(RuntimeError, match="running"):
        stt.reset()
    stt._running = False


def test_concurrent_run_on_one_instance_is_refused(monkeypatch):
    install_fake_sdk(monkeypatch, {"script": []})
    stt = SpeechmaticsRealtime(api_key="k", language="en")
    stt._running = True
    audio = FakeAudio([b"a"])
    with pytest.raises(RuntimeError, match="already running"):
        asyncio.run(stt.run(audio, lambda t: None, lambda t: None))
    # the refused call did not touch the audio stream or the session state
    assert audio.closed is False


# ------------------------------- callback failures (BUG 5)

def test_failing_callbacks_never_cost_the_transcript(monkeypatch):
    """UI callbacks run on the SDK receive path: a raising one must not kill
    the session or drop the finalized segment."""
    install_fake_sdk(monkeypatch, {"script": [
        ("partial", "partial text"),
        ("final", "بیمار در سی سی یو است"),
    ]})

    def explode(_text):
        raise RuntimeError("overlay is gone")

    stt = SpeechmaticsRealtime(api_key="k", language="fa")
    result = asyncio.run(stt.run(FakeAudio([b"a", b"b"]), explode, explode))
    assert result.final_text == "بیمار در سی سی یو است"
    assert [segment["text"] for segment in result.final_segments] == [
        "بیمار در سی سی یو است"
    ]
    assert result.error is None
    assert any("partial callback failed" in warning for warning in result.warnings)
    assert any("final callback failed" in warning for warning in result.warnings)


def test_callback_failure_does_not_stop_later_segments(monkeypatch):
    install_fake_sdk(monkeypatch, {"script": [
        ("final", "first"),
        ("final", "second"),
    ]})
    seen = []

    def only_first(text):
        seen.append(text)
        if len(seen) == 1:
            raise RuntimeError("boom")

    stt = SpeechmaticsRealtime(api_key="k", language="en")
    result = asyncio.run(stt.run(FakeAudio([b"a", b"b"]), lambda t: None, only_first))
    assert seen == ["first", "second"]
    assert result.final_text == "first second"
