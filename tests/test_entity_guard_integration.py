"""App wiring for entity review metadata and stage dumps."""

import asyncio
import json
import sys

import app as app_module
import speechmatics_test.microphone as microphone_module
from tests.test_e2e import FakeMic, fake_audio_source
from tests.test_realtime import install_fake_sdk


def test_entity_guard_runs_after_canonicalization_and_stage_dump_is_written(tmp_path, monkeypatch):
    """Flags are saved as metadata while the canonical transcript is intact."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    monkeypatch.setattr(microphone_module, "MicrophoneRecorder", FakeMic)
    monkeypatch.setattr(app_module, "audio_source", fake_audio_source)
    monkeypatch.setattr(sys, "argv", [
        "app.py", "--language", "fa", "--no-overlay", "--no-inject",
        "--save-report", "--dump-stages",
    ])
    install_fake_sdk(monkeypatch, {
        "script": [
            ("final", "آقای 40 ساله با BP 100/80 و Temp 36 و IV-Line 40G"),
        ],
    })

    assert asyncio.run(app_module.main()) == 0
    reports = sorted((app_module.ROOT / "results").glob("session_*.json"))
    dumps = sorted((app_module.ROOT / "results").glob("stages_*.json"))
    assert reports and dumps
    try:
        report = json.loads(reports[-1].read_text(encoding="utf-8"))
        stage_dump = json.loads(dumps[-1].read_text(encoding="utf-8"))
        flag_types = {flag["type"] for flag in report["entity_flags"]}

        assert report["entity_guard_enabled"] is True
        assert {"bp", "temperature", "iv_gauge"} <= flag_types
        assert "Temp 36" in report["final_transcript_canonical"]
        assert stage_dump["segments"][0]["final_transcript_raw"].startswith("آقای 40")
        assert {flag["type"] for flag in stage_dump["segments"][0]["entity_flags"]} >= {
            "bp", "temperature", "iv_gauge",
        }
    finally:
        for path in reports + dumps:
            path.unlink(missing_ok=True)
