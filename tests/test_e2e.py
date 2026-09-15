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
            ("final", "بیمار در سی سی یو است", [
                {"type": "word", "start_time": 0.0, "end_time": 0.2,
                 "alternatives": [{"content": "بیمار", "confidence": 0.98, "language": "fa"}]},
                {"type": "word", "start_time": 0.2, "end_time": 0.3,
                 "alternatives": [{"content": "در", "confidence": 0.99, "language": "fa"}]},
                {"type": "word", "start_time": 0.3, "end_time": 0.4,
                 "alternatives": [{"content": "سی", "confidence": 0.91, "language": "fa"}]},
                {"type": "word", "start_time": 0.4, "end_time": 0.5,
                 "alternatives": [{"content": "سی", "confidence": 0.52, "language": "fa"}]},
                {"type": "word", "start_time": 0.5, "end_time": 0.7,
                 "alternatives": [{"content": "یو", "confidence": 0.93, "language": "fa"}]},
                {"type": "word", "start_time": 0.7, "end_time": 0.8,
                 "alternatives": [{"content": "است", "confidence": 0.97, "language": "fa"}]},
            ]),
            ("final", "فشار خون بالا دارد", [
                {"type": "word", "start_time": 0.8, "end_time": 1.0,
                 "alternatives": [{"content": "فشار", "confidence": 0.95, "language": "fa"}]},
                {"type": "word", "start_time": 1.0, "end_time": 1.1,
                 "alternatives": [{"content": "خون", "confidence": 0.96, "language": "fa"}]},
                {"type": "word", "start_time": 1.1, "end_time": 1.2,
                 "alternatives": [{"content": "بالا", "confidence": 0.95, "language": "fa"}]},
                {"type": "word", "start_time": 1.2, "end_time": 1.3,
                 "alternatives": [{"content": "دارد", "confidence": 0.99, "language": "fa"}]},
            ]),
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
        # New structured fields coexist with the established report contract.
        assert report["speechmatics"] == {
            "domain": "medical", "model": "enhanced", "max_delay": 2.0,
            "max_delay_mode": "flexible",
        }
        assert report["word_results"][3] == {
            "content": "سی", "confidence": 0.52, "language": "fa",
            "start_time": 0.4, "end_time": 0.5,
        }
        assert report["confidence_summary"]["word_count"] == 10
        assert report["confidence_summary"]["low_confidence_count"] == 1
        assert report["confidence_summary"]["language_counts"] == {
            "Persian": 10, "English": 0, "unknown": 0,
        }
        assert report["medical_canonicalization"] == {
            "hit_count": len(report["medical_hits"]), "changed": True,
        }
    finally:
        # keep the source tree clean
        for p in reports:
            p.unlink(missing_ok=True)


def test_no_vocab_and_medical_flags_remain_independent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    monkeypatch.setattr(microphone_module, "MicrophoneRecorder", FakeMic)
    monkeypatch.setattr(app_module, "audio_source", fake_audio_source)
    monkeypatch.setattr(sys, "argv", [
        "app.py", "--language", "fa", "--no-overlay", "--no-inject",
        "--no-vocab", "--no-medical-layer", "--save-report",
    ])
    registry = install_fake_sdk(monkeypatch, {
        "script": [("final", "سی تی اسکن")],
    })

    assert asyncio.run(app_module.main()) == 0
    reports = sorted((app_module.ROOT / "results").glob("session_*.json"))
    assert reports
    try:
        report = json.loads(reports[-1].read_text(encoding="utf-8"))
        assert "additional_vocab" not in registry["configs"][0]
        assert registry["configs"][0]["domain"] == "medical"
        assert report["medical_vocab_enabled"] is False
        assert report["medical_layer_enabled"] is False
        assert report["final_transcript_normalized"] == "سی تی اسکن"
        assert report["final_transcript_canonical"] == "سی تی اسکن"
    finally:
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
