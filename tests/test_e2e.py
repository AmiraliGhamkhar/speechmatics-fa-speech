"""End-to-end simulation of app.py main() with a fake SDK and fake mic.

Proves the full realtime pipeline (mic chunks -> Speechmatics -> raw ->
normalized -> Aho-Corasick canonical -> report) runs cleanly, routes finals
through the final API, auto-injects every finalized segment without any
hotkeys/countdown, and never writes audio to disk.
"""

import asyncio
import json
import sys

import app as app_module
import speechmatics_test.microphone as microphone_module
from tests.test_realtime import install_fake_sdk


class FakeMic:
    """Stands in for MicrophoneRecorder (no hardware)."""

    instances = []

    def __init__(self, device_index=None, pyaudio_module=None):
        self.device_index = device_index
        self.closed = False
        FakeMic.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False

    def read(self):
        return b"\x00" * 6400


async def fake_audio_source(recorder, max_seconds, stop_event=None):
    for _ in range(4):
        if stop_event is not None and stop_event.is_set():
            return
        yield b"\x00" * 6400


def test_end_to_end_session(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    FakeMic.instances = []
    monkeypatch.setattr(microphone_module, "MicrophoneRecorder", FakeMic)
    monkeypatch.setattr(app_module, "audio_source", fake_audio_source)
    monkeypatch.setattr(sys, "argv", [
        "app.py", "--language", "fa", "--no-overlay", "--no-inject",
        "--device-index", "3",
    ])
    install_fake_sdk(monkeypatch, {
        "script": [
            ("partial", "بیمار در سی تی اسکن"),
            ("final", "بیمار در سی تی اسکن"),
            ("partial", "لیژن رایت لانگ"),
            ("final", "لیژن در رایت لانگ"),
        ],
    })

    code = asyncio.run(app_module.main())

    out = capsys.readouterr().out

    assert code == 0
    # final segments were displayed as FINAL, not as partials
    assert "[final]" in out
    # the three pipeline stages are reported
    assert "FINAL RAW:" in out
    assert "FINAL NORMALIZED:" in out
    assert "FINAL CANONICAL:" in out
    # Aho-Corasick canonicalization applied to the joined finals
    assert "CT scan" in out.split("FINAL CANONICAL:")[1]
    assert "right lung" in out.split("FINAL CANONICAL:")[1]
    # raw is preserved unnormalized before the canonical stage
    raw_block = out.split("FINAL RAW:")[1].split("FINAL NORMALIZED:")[0]
    assert "سی تی اسکن" in raw_block or "لیژن" in raw_block
    # microphone resource released
    assert FakeMic.instances and FakeMic.instances[0].closed is True
    # no audio (or any) file was written
    assert list(tmp_path.iterdir()) == []


def test_end_to_end_session_with_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    monkeypatch.setattr(microphone_module, "MicrophoneRecorder", FakeMic)
    monkeypatch.setattr(app_module, "audio_source", fake_audio_source)
    monkeypatch.setattr(sys, "argv", [
        "app.py", "--language", "fa", "--no-overlay", "--no-inject",
        "--save-report",
    ])
    install_fake_sdk(monkeypatch, {
        "script": [
            ("final", "بیمار در سی سی یو است"),
            ("final", "فشار خون بالا دارد"),
        ],
    })

    code = asyncio.run(app_module.main())
    assert code == 0

    # report written to <repo>/results (gitignored), not next to audio
    reports = sorted((app_module.ROOT / "results").glob("session_*.json"))
    assert reports, "expected a session report JSON"
    report = json.loads(reports[-1].read_text(encoding="utf-8"))
    try:
        assert report["final_transcript_raw"] == "بیمار در سی سی یو است فشار خون بالا دارد"
        assert report["final_transcript_normalized"] == report["final_transcript_raw"]
        assert "CCU" in report["final_transcript_canonical"]
        assert "HTN" in report["final_transcript_canonical"]
        assert "audio_path" not in report  # audio is never persisted
        assert report["medical_hits"]
        assert report["session_error"] is None
        assert report["matcher_engine"].startswith("aho-corasick")
    finally:
        # keep the source tree clean
        for p in reports:
            p.unlink(missing_ok=True)


def test_auto_injection_fires_per_final_segment(tmp_path, monkeypatch):
    """No hotkeys/countdown: every finalized segment is pasted immediately."""
    pasted = []

    class FakeInjector:
        def reset_partial(self):
            pass

        def paste_text(self, text, add_rtl_mark=False):
            pasted.append((text, add_rtl_mark))
            return True

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    monkeypatch.setattr(microphone_module, "MicrophoneRecorder", FakeMic)
    monkeypatch.setattr(app_module, "audio_source", fake_audio_source)
    monkeypatch.setattr(app_module, "create_injector", lambda enabled: FakeInjector())
    monkeypatch.setattr(sys, "argv", [
        "app.py", "--language", "fa", "--no-overlay",
    ])
    install_fake_sdk(monkeypatch, {
        "script": [
            ("partial", "بیمار در سی سی یو"),
            ("final", "بیمار در سی سی یو است"),
            ("final", "فشار خون بالا دارد"),
        ],
    })

    code = asyncio.run(app_module.main())
    assert code == 0

    # both finalized segments were auto-injected during the session,
    # canonicalized and with a trailing space separator
    assert [t for t, _ in pasted] == [
        "بیمار در CCU است ",
        "HTN دارد ",
    ]
    assert all(mark for _, mark in pasted)


def test_injection_disabled_with_no_inject_flag(tmp_path, monkeypatch):
    calls = []

    class FakeInjector:
        def reset_partial(self):
            calls.append("reset")

        def paste_text(self, text, add_rtl_mark=False):
            calls.append(("paste", text))
            return True

    def fake_create(enabled):
        assert enabled is False  # --no-inject must reach the factory
        calls.append(("create", enabled))
        return None

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    monkeypatch.setattr(microphone_module, "MicrophoneRecorder", FakeMic)
    monkeypatch.setattr(app_module, "audio_source", fake_audio_source)
    monkeypatch.setattr(app_module, "create_injector", fake_create)
    monkeypatch.setattr(sys, "argv", [
        "app.py", "--language", "fa", "--no-overlay", "--no-inject",
    ])
    install_fake_sdk(monkeypatch, {
        "script": [("final", "بیمار در سی سی یو است")],
    })

    assert asyncio.run(app_module.main()) == 0
    assert not any(c == "reset" or (isinstance(c, tuple) and c[0] == "paste") for c in calls)


def test_ctrl_c_preserves_transcript_and_report(tmp_path, monkeypatch):
    """Regression: Ctrl+C used to destroy the finished transcript.

    SIGINT raised KeyboardInterrupt inside the event loop, which escaped
    ``main()`` entirely, so the canonicalization/cleanliness/report stages
    never ran and everything the clinician had dictated was lost. Ctrl+C
    must instead STOP THE RECORDING and complete the pipeline.
    """
    import os
    import signal
    import threading

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    monkeypatch.setattr(microphone_module, "MicrophoneRecorder", FakeMic)
    monkeypatch.setattr(sys, "argv", [
        "app.py", "--language", "fa", "--no-overlay", "--no-inject",
        "--save-report",
    ])

    sigint_sent = threading.Event()

    async def endless_audio(recorder, max_seconds, stop_event=None):
        """Streams until the SIGINT-driven stop_event is set."""
        for _ in range(2):
            yield b"\x00" * 6400
        if not sigint_sent.is_set():
            sigint_sent.set()
            os.kill(os.getpid(), signal.SIGINT)
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            await asyncio.sleep(0.01)
            yield b"\x00" * 6400

    monkeypatch.setattr(app_module, "audio_source", endless_audio)
    install_fake_sdk(monkeypatch, {
        "script": [("final", "بیمار در سی سی یو است")],
    })

    code = asyncio.run(app_module.main())
    assert code == 0, "Ctrl+C must not abort the pipeline"

    reports = sorted((app_module.ROOT / "results").glob("session_*.json"))
    assert reports, "the report must still be written after Ctrl+C"
    try:
        report = json.loads(reports[-1].read_text(encoding="utf-8"))
        # the dictated text survived and was fully processed
        assert report["final_transcript_raw"] == "بیمار در سی سی یو است"
        assert "CCU" in report["final_transcript_canonical"]
    finally:
        for p in reports:
            p.unlink(missing_ok=True)


def test_sigint_handler_is_removed_after_the_session(tmp_path, monkeypatch):
    """The app must not leave a global SIGINT handler installed."""
    import signal

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    monkeypatch.setattr(microphone_module, "MicrophoneRecorder", FakeMic)
    monkeypatch.setattr(app_module, "audio_source", fake_audio_source)
    monkeypatch.setattr(sys, "argv", [
        "app.py", "--language", "fa", "--no-overlay", "--no-inject",
    ])
    install_fake_sdk(monkeypatch, {"script": [("final", "سی تی اسکن")]})

    before = signal.getsignal(signal.SIGINT)
    assert asyncio.run(app_module.main()) == 0
    assert signal.getsignal(signal.SIGINT) is before
