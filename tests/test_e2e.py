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
        # Persian is not documented for Enhanced Medical: auto sends none.
        assert report["speechmatics"] == {
            "domain": None, "domain_requested": "auto", "model": "enhanced",
            "max_delay": 2.0, "max_delay_mode": "flexible",
        }
        assert report["parse_warnings"] == []
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
        # Persian under --domain auto: the medical domain is not sent
        assert "domain" not in registry["configs"][0]
        assert report["speechmatics"]["domain"] is None
        assert report["medical_vocab_enabled"] is False
        assert report["medical_layer_enabled"] is False
        assert report["final_transcript_normalized"] == "سی تی اسکن"
        assert report["final_transcript_canonical"] == "سی تی اسکن"
    finally:
        for p in reports:
            p.unlink(missing_ok=True)


def test_no_medical_layer_never_constructs_the_dictionary_matcher(
    tmp_path, monkeypatch
):
    """--no-medical-layer must skip MedicalLayer(ROOT) entirely (Bug: it used
    to be built unconditionally before checking the flag, loading and
    compiling the whole dictionary even when told not to)."""
    import speechmatics_test.medical_layer as medical_layer_module

    calls = []
    real_init = medical_layer_module.MedicalLayer.__init__

    def spying_init(self, root):
        calls.append(root)
        return real_init(self, root)

    monkeypatch.setattr(medical_layer_module.MedicalLayer, "__init__", spying_init)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    monkeypatch.setattr(microphone_module, "MicrophoneRecorder", FakeMic)
    monkeypatch.setattr(app_module, "audio_source", fake_audio_source)
    monkeypatch.setattr(sys, "argv", [
        "app.py", "--language", "fa", "--no-overlay", "--no-inject",
        "--no-medical-layer",
    ])
    install_fake_sdk(monkeypatch, {"script": [("final", "سی تی اسکن")]})

    assert asyncio.run(app_module.main()) == 0
    assert calls == []  # MedicalLayer(ROOT) was never constructed


def test_no_vocab_alone_keeps_the_medical_layer_active(tmp_path, monkeypatch):
    """--no-vocab must only drop the ASR biasing vocab; the local matcher
    stays fully active and still canonicalizes."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    monkeypatch.setattr(microphone_module, "MicrophoneRecorder", FakeMic)
    monkeypatch.setattr(app_module, "audio_source", fake_audio_source)
    monkeypatch.setattr(sys, "argv", [
        "app.py", "--language", "fa", "--no-overlay", "--no-inject",
        "--no-vocab", "--save-report",
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
        assert report["medical_vocab_enabled"] is False
        assert report["medical_layer_enabled"] is True
        assert report["matcher_engine"]
        # The local matcher rewrote the Persian phrase to its canonical form.
        assert report["final_transcript_canonical"] == "CT scan"
    finally:
        for p in reports:
            p.unlink(missing_ok=True)


def test_auto_injection_fires_per_final_segment(tmp_path, monkeypatch):
    """No hotkeys/countdown: every finalized segment is pasted immediately."""
    pasted = []

    class FakeInjector:
        calls = []

        def arm_target(self):
            FakeInjector.calls.append("arm")
            return True

        def reset_partial(self):
            FakeInjector.calls.append("reset")

        def paste_text(self, text, add_rtl_mark=False):
            FakeInjector.calls.append("paste")
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

    FakeInjector.calls = []
    code = asyncio.run(app_module.main())
    assert code == 0

    # both finalized segments were auto-injected during the session,
    # canonicalized and with a trailing space separator
    assert [t for t, _ in pasted] == [
        "بیمار در CCU است ",
        "HTN دارد ",
    ]
    assert all(mark for _, mark in pasted)
    # the focus guard armed the target once, before the first paste
    assert FakeInjector.calls[0] == "arm"
    assert FakeInjector.calls[1:4] == ["reset", "paste", "reset"]


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


def test_cross_segment_medical_phrase_injects_and_reports_identically(
    tmp_path, monkeypatch
):
    """H3 regression: a phrase split across finals ("فشار خون" + "بالا دارد")
    must inject and report the same canonical text ("HTN دارد"), instead of
    injecting "BP بالا" while the report claimed "HTN دارد"."""
    pasted = []

    class FakeInjector:
        def arm_target(self):
            return True

        def reset_partial(self):
            pass

        def paste_text(self, text, add_rtl_mark=False):
            pasted.append(text)
            return True

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    monkeypatch.setattr(microphone_module, "MicrophoneRecorder", FakeMic)
    monkeypatch.setattr(app_module, "audio_source", fake_audio_source)
    monkeypatch.setattr(app_module, "create_injector", lambda enabled: FakeInjector())
    monkeypatch.setattr(sys, "argv", [
        "app.py", "--language", "fa", "--no-overlay", "--save-report",
    ])
    install_fake_sdk(monkeypatch, {
        "script": [
            ("final", "فشار خون"),
            ("final", "بالا دارد"),
        ],
    })

    assert asyncio.run(app_module.main()) == 0

    # the first final is buffered (it could still extend), then the phrase
    # resolves once and is injected exactly once
    assert pasted == ["HTN دارد "]

    reports = sorted((app_module.ROOT / "results").glob("session_*.json"))
    assert reports
    try:
        report = json.loads(reports[-1].read_text(encoding="utf-8"))
        # the report's canonical stage IS the injected text
        assert report["final_transcript_canonical"] == "HTN دارد"
        assert report["final_transcript_normalized"] == "فشار خون بالا دارد"
        injected = " ".join(t.strip() for t in pasted).strip()
        assert report["final_transcript_canonical"] == injected
        assert report["injection"]["segments"] == [
            {"text": "HTN دارد", "success": True}
        ]
        assert any(h["canonical"] == "HTN" for h in report["medical_hits"])
    finally:
        for p in reports:
            p.unlink(missing_ok=True)


def test_no_focus_guard_skips_arming(tmp_path, monkeypatch):
    armed = []

    class FakeInjector:
        def arm_target(self):
            armed.append(True)
            return True

        def reset_partial(self):
            pass

        def paste_text(self, text, add_rtl_mark=False):
            return True

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    monkeypatch.setattr(microphone_module, "MicrophoneRecorder", FakeMic)
    monkeypatch.setattr(app_module, "audio_source", fake_audio_source)
    monkeypatch.setattr(app_module, "create_injector", lambda enabled: FakeInjector())
    monkeypatch.setattr(sys, "argv", [
        "app.py", "--language", "fa", "--no-overlay", "--no-focus-guard",
    ])
    install_fake_sdk(monkeypatch, {"script": [("final", "سی تی اسکن")]})

    assert asyncio.run(app_module.main()) == 0
    assert armed == []  # guard disabled: nothing was armed


def test_microphone_failure_cleans_up_and_exits_with_error(
    tmp_path, monkeypatch, capsys
):
    """BUG 3 regression: microphone construction sat OUTSIDE the cleanup
    boundary, so a missing device/PyAudio skipped SIGINT-handler removal and
    overlay close. It must produce a clear message, exit code 1, and full
    cleanup."""
    import signal

    from speechmatics_test.microphone import MicrophoneError

    class FailingRecorder:
        def __init__(self, device_index=None, pyaudio_module=None):
            raise MicrophoneError("no audio device available")

    class FakeOverlay:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    fake_overlay = FakeOverlay()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    monkeypatch.setattr(microphone_module, "MicrophoneRecorder", FailingRecorder)
    monkeypatch.setattr(app_module, "create_overlay", lambda disabled: fake_overlay)
    monkeypatch.setattr(sys, "argv", [
        "app.py", "--language", "fa", "--no-inject",
    ])

    before = signal.getsignal(signal.SIGINT)
    assert asyncio.run(app_module.main()) == 1
    # the cleanup boundary ran: SIGINT handler removed, overlay closed
    assert signal.getsignal(signal.SIGINT) is before
    assert fake_overlay.closed is True
    assert "[microphone error]" in capsys.readouterr().out


def test_auto_injection_fifo_three_segments_no_hotkey(tmp_path, monkeypatch):
    """Normal flow, no hotkey: three finalized segments must be pasted
    automatically, in exactly the emission (FIFO) order, without any
    keypress, Enter/Space, Ctrl+V by the user, or manual trigger."""
    pasted = []

    class FakeInjector:
        calls = []

        def arm_target(self):
            FakeInjector.calls.append("arm")
            return True

        def reset_partial(self):
            pass

        def paste_text(self, text, add_rtl_mark=False):
            pasted.append(text)
            return True

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    monkeypatch.setattr(microphone_module, "MicrophoneRecorder", FakeMic)
    monkeypatch.setattr(app_module, "audio_source", fake_audio_source)
    monkeypatch.setattr(
        app_module, "create_injector", lambda enabled: FakeInjector())
    monkeypatch.setattr(sys, "argv", [
        "app.py", "--language", "fa", "--no-overlay",
    ])
    install_fake_sdk(monkeypatch, {
        "script": [
            ("final", "وضعیت بیمار پایدار است"),
            ("final", "درد قفسه سینه بررسی شد"),
            ("final", "بیمار ترخیص خواهد شد"),
        ],
    })

    assert asyncio.run(app_module.main()) == 0
    # canonicalized, each with the trailing separator, strictly in order
    assert pasted == [
        "وضعیت بیمار پایدار است ",
        "chest pain بررسی شد ",
        "بیمار hospital discharge خواهد شد ",
    ]
    # armed exactly once, before any transcription work
    assert FakeInjector.calls == ["arm"]


def test_buffered_first_final_is_auto_injected_once_completed(
    tmp_path, monkeypatch
):
    """The canonicalizer may hold a first final that can still extend
    ("فشار خون" awaits "بالا"). That buffering must NOT turn the app into
    preview-only: when the second final completes the phrase the completed
    text is automatically injected - still with no hotkey at any stage."""
    pasted = []

    class FakeInjector:
        def arm_target(self):
            return True

        def reset_partial(self):
            pass

        def paste_text(self, text, add_rtl_mark=False):
            pasted.append(text)
            return True

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    monkeypatch.setattr(microphone_module, "MicrophoneRecorder", FakeMic)
    monkeypatch.setattr(app_module, "audio_source", fake_audio_source)
    monkeypatch.setattr(
        app_module, "create_injector", lambda enabled: FakeInjector())
    monkeypatch.setattr(sys, "argv", [
        "app.py", "--language", "fa", "--no-overlay",
    ])
    install_fake_sdk(monkeypatch, {
        "script": [
            ("final", "سی و"),       # buffered: the number may continue
            ("final", "پنج ساله"),   # completes -> 35 -> auto-injected now
        ],
    })

    assert asyncio.run(app_module.main()) == 0
    assert pasted == ["35 ساله "]


def test_realtime_failure_exits_nonzero_and_preserves_partial_report(
    tmp_path, monkeypatch
):
    """A realtime session failure must produce a non-zero exit status while
    still preserving the transcript/report captured before the failure."""
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
        "fail_stop": RuntimeError("websocket closed unexpectedly"),
    })

    assert asyncio.run(app_module.main()) == 1

    reports = sorted((app_module.ROOT / "results").glob("session_*.json"))
    assert reports, "the partial report must still be written on failure"
    try:
        report = json.loads(reports[-1].read_text(encoding="utf-8"))
        assert report["session_error"] is not None
        # the finals captured before the failure are fully processed
        assert "CCU" in report["final_transcript_canonical"]
        assert "HTN" in report["final_transcript_canonical"]
        assert report["final_transcript_raw"] == \
            "بیمار در سی سی یو است فشار خون بالا دارد"
    finally:
        for p in reports:
            p.unlink(missing_ok=True)


def test_injection_failure_is_surfaced_and_not_silent(tmp_path, monkeypatch,
                                                      capsys):
    """An injection failure must be reported (console + report), must not be
    counted as delivered, and must not corrupt the FIFO stream after it."""
    pasted = []

    class FlakyInjector:
        def arm_target(self):
            return True

        def reset_partial(self):
            pass

        def paste_text(self, text, add_rtl_mark=False):
            pasted.append(text)
            return "دارد" not in text  # fail the second segment

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    monkeypatch.setattr(microphone_module, "MicrophoneRecorder", FakeMic)
    monkeypatch.setattr(app_module, "audio_source", fake_audio_source)
    monkeypatch.setattr(
        app_module, "create_injector", lambda enabled: FlakyInjector())
    monkeypatch.setattr(sys, "argv", [
        "app.py", "--language", "fa", "--no-overlay", "--save-report",
    ])
    install_fake_sdk(monkeypatch, {
        "script": [
            ("final", "بیمار در سی سی یو است"),
            ("final", "فشار خون بالا دارد"),
            ("final", "وضعیت پایدار است"),
        ],
    })

    # injection failure is not a session failure; exit status stays 0, but
    # the failure is loud and durable.
    assert asyncio.run(app_module.main()) == 0
    out = capsys.readouterr().out
    assert "AUTO-INJECTION FAILED" in out
    assert "WARNING" in out
    # all three pastes were attempted in FIFO order, none dropped
    assert pasted == [
        "بیمار در CCU است ",
        "HTN دارد ",
        "وضعیت پایدار است ",
    ]

    reports = sorted((app_module.ROOT / "results").glob("session_*.json"))
    assert reports
    try:
        report = json.loads(reports[-1].read_text(encoding="utf-8"))
        assert report["injection"]["segments"] == [
            {"text": "بیمار در CCU است", "success": True},
            {"text": "HTN دارد", "success": False},
            {"text": "وضعیت پایدار است", "success": True},
        ]
    finally:
        for p in reports:
            p.unlink(missing_ok=True)
