"""MicrophoneRecorder tests using a fake pyaudio module (no real hardware)."""

import glob
import os
from pathlib import Path

import pytest

import speechmatics_test.microphone as microphone_module
from speechmatics_test.microphone import MicrophoneError, MicrophoneRecorder


def make_fake_pyaudio(fail_open=False):
    """Build a fake ``pyaudio`` module with a ``PyAudio()`` factory.

    Returns ``(module, state)``; ``state`` records events/kwargs for asserts.
    """
    state = {"open_kwargs": None, "events": [], "stream": None}

    class FakeStream:
        def read(self, n, exception_on_overflow=False):
            state["events"].append(("read", n))
            return b"\x00" * (n * 2)  # 16-bit PCM

        def stop_stream(self):
            state["events"].append("stop_stream")

        def close(self):
            state["events"].append("stream_closed")

    class FakePyAudioInstance:
        def open(self, **kwargs):
            if fail_open:
                state["events"].append("open_failed")
                raise OSError("no microphone")
            state["open_kwargs"] = kwargs
            state["events"].append("open")
            stream = FakeStream()
            state["stream"] = stream
            return stream

        def terminate(self):
            state["events"].append("terminate")

    module = type("FakePyAudioModule", (), {})
    module.paInt16 = 8
    module.PyAudio = FakePyAudioInstance
    return module, state


# ----------------------------------------------------------------- init/device

def test_device_index_is_passed_to_open():
    fake, state = make_fake_pyaudio()
    with MicrophoneRecorder(device_index=7, pyaudio_module=fake):
        pass
    assert state["open_kwargs"]["input_device_index"] == 7


def test_default_device_index_is_none():
    fake, state = make_fake_pyaudio()
    with MicrophoneRecorder(pyaudio_module=fake) as rec:
        pass
    assert rec.device_index is None
    assert state["open_kwargs"]["input_device_index"] is None


def test_open_format_matches_streaming_contract():
    fake, state = make_fake_pyaudio()
    with MicrophoneRecorder(pyaudio_module=fake):
        pass
    kw = state["open_kwargs"]
    assert kw["rate"] == 16000
    assert kw["channels"] == 1
    assert kw["input"] is True
    assert kw["frames_per_buffer"] == MicrophoneRecorder.CHUNK
    assert kw["format"] == 8  # paInt16


# ------------------------------------------------------ context manager / exit

def test_context_manager_releases_resources_on_normal_exit():
    fake, state = make_fake_pyaudio()
    with MicrophoneRecorder(pyaudio_module=fake) as rec:
        assert rec.stream is not None
    order = [e for e in state["events"] if e in ("stop_stream", "stream_closed", "terminate")]
    assert order == ["stop_stream", "stream_closed", "terminate"]
    assert state["stream"] is not None  # stream object existed and was closed


def test_context_manager_releases_resources_on_error():
    fake, state = make_fake_pyaudio()
    with pytest.raises(RuntimeError):
        with MicrophoneRecorder(pyaudio_module=fake):
            raise RuntimeError("boom")
    assert "terminate" in state["events"]
    assert "stream_closed" in state["events"]


def test_double_close_is_idempotent():
    fake, state = make_fake_pyaudio()
    rec = MicrophoneRecorder(pyaudio_module=fake)
    with rec:
        pass
    rec.close()  # second close must be a no-op
    assert state["events"].count("terminate") == 1
    assert state["events"].count("stream_closed") == 1


def test_failed_open_still_releases_pyaudio():
    fake, state = make_fake_pyaudio(fail_open=True)
    with pytest.raises(OSError):
        with MicrophoneRecorder(pyaudio_module=fake):
            pass
    assert "open_failed" in state["events"]
    assert "terminate" in state["events"]


def test_double_enter_raises():
    fake, state = make_fake_pyaudio()
    rec = MicrophoneRecorder(pyaudio_module=fake)
    with rec:
        with pytest.raises(MicrophoneError):
            rec.__enter__()


def test_read_requires_started():
    fake, state = make_fake_pyaudio()
    rec = MicrophoneRecorder(pyaudio_module=fake)
    with pytest.raises(MicrophoneError):
        rec.read()


# ----------------------------------------------------------- no audio storage

def test_no_save_method_exists():
    assert not hasattr(MicrophoneRecorder, "save")
    assert not hasattr(MicrophoneRecorder, "frames")


def test_module_has_no_wave_import():
    src = Path(microphone_module.__file__).read_text(encoding="utf-8")
    assert "import wave" not in src
    assert ".wav" not in src


def test_streaming_creates_no_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake, state = make_fake_pyaudio()
    rec = MicrophoneRecorder(pyaudio_module=fake)
    with rec:
        for _ in range(5):
            chunk = rec.read()
            assert chunk  # chunk exists in memory
            del chunk
    assert list(tmp_path.iterdir()) == []
    assert glob.glob(os.path.join(str(tmp_path), "**", "*.wav"), recursive=True) == []


def test_missing_pyaudio_raises_clear_error(monkeypatch):
    monkeypatch.setattr(microphone_module, "pyaudio", None)
    with pytest.raises(MicrophoneError, match="PyAudio"):
        MicrophoneRecorder()
