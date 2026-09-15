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
      - fail_send_after: fail send_audio after N successful sends
      - fail_stop: exception to raise from stop_session
    """
    behavior = behavior or {}
    registry = {"configs": [], "clients": []}

    class ServerMessageType(Enum):
        ADD_PARTIAL_TRANSCRIPT = "AddPartialTranscript"
        ADD_TRANSCRIPT = "AddTranscript"

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
        def __init__(self, transcript):
            self.transcript = transcript

    class TranscriptResult:
        def __init__(self, metadata):
            self.metadata = metadata

        @classmethod
        def from_message(cls, message):
            return cls(_Metadata(message.get("transcript")))

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

        def on(self, event):
            def decorator(callback):
                self.handlers[event] = callback
                return callback
            return decorator

        async def start_session(self, transcription_config=None,
                                audio_format=None, **kw):
            if behavior.get("fail_start"):
                raise behavior["fail_start"]

        async def send_audio(self, chunk):
            if behavior.get("fail_send_after") is not None and \
                    len(self.sent) >= behavior["fail_send_after"]:
                raise RuntimeError("websocket send failed")
            self.sent.append(chunk)
            if self._script:
                kind, text = self._script.pop(0)
                event = (
                    ServerMessageType.ADD_PARTIAL_TRANSCRIPT
                    if kind == "partial" else ServerMessageType.ADD_TRANSCRIPT
                )
                if event in self.handlers:
                    self.handlers[event]({"transcript": text})

        async def stop_session(self):
            if behavior.get("fail_stop"):
                raise behavior["fail_stop"]
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
        additional_vocab=["CT scan", {"content": "lesion", "sounds_like": ["لیژن"]}],
    )
    audio = FakeAudio([b"a"])
    asyncio.run(stt.run(audio, lambda t: None, lambda t: None))
    cfg = registry["configs"][0]
    assert cfg["language"] == "en"
    assert cfg["model"].name == "ENHANCED"
    assert cfg["enable_partials"] is True
    assert cfg["max_delay"] == 2.0
    assert cfg["max_delay_mode"] == "flexible"
    assert cfg["additional_vocab"] == [
        "CT scan", {"content": "lesion", "sounds_like": ["لیژن"]}
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
            {"content": "lesion", "sounds_like": ["لیژن", "bad token with space"]},
            {"content": "  "},                      # no usable content
            42,                                     # junk
        ],
    )
    audio = FakeAudio([b"a"])
    asyncio.run(stt.run(audio, lambda t: None, lambda t: None))
    assert registry["configs"][0]["additional_vocab"] == [
        "CT scan", {"content": "lesion", "sounds_like": ["لیژن"]}
    ]


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
