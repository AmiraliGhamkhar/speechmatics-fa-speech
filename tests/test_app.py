"""App-level tests: in-memory audio source and wiring helpers."""

import asyncio
from pathlib import Path

import pytest

import app as app_module


class FakeRecorder:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def read(self):
        if not self.chunks:
            raise RuntimeError("fake recorder exhausted")
        return self.chunks.pop(0)


async def collect_n(gen, n):
    out = []
    try:
        async for chunk in gen:
            out.append(chunk)
            if len(out) == n:
                break
    finally:
        await gen.aclose()
    return out


def test_audio_source_yields_chunks_in_memory_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rec = FakeRecorder([b"chunk-1", b"chunk-2"])
    got = asyncio.run(collect_n(app_module.audio_source(rec, max_seconds=10.0), 2))
    assert got == [b"chunk-1", b"chunk-2"]
    # nothing was written anywhere
    assert list(tmp_path.iterdir()) == []


def test_audio_source_stops_after_max_seconds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # with max_seconds=0 the generator must stop without yielding
    rec = FakeRecorder([b"chunk-1"])
    got = asyncio.run(collect_n(app_module.audio_source(rec, max_seconds=0.0), 1))
    assert got == []
    assert list(tmp_path.iterdir()) == []


def test_audio_source_aclose_safe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rec = FakeRecorder([b"chunk-1", b"chunk-2"])
    gen = app_module.audio_source(rec, max_seconds=10.0)

    async def run():
        out = []
        async for chunk in gen:
            out.append(chunk)
            if len(out) == 1:
                break
        await gen.aclose()  # must not raise mid-stream
        await gen.aclose()  # idempotent
        return out

    assert asyncio.run(run()) == [b"chunk-1"]
    assert list(tmp_path.iterdir()) == []


def test_load_benchmark(tmp_path):
    known = app_module.load_benchmark("mix_ct_lesion")
    assert "CT scan" in known["expected"]
    with pytest.raises(RuntimeError, match="Unknown test ID"):
        app_module.load_benchmark("does-not-exist")


def test_curated_speechmatics_vocab_is_bounded_and_keeps_sounds_like():
    from speechmatics_test.realtime import SpeechmaticsRealtime

    vocab = SpeechmaticsRealtime._clean_vocab(app_module.load_vocab())
    by_content = {item["content"]: item for item in vocab if isinstance(item, dict)}
    assert len(vocab) < 100  # ASR biasing vocabulary, not the local dictionary
    assert {
        "metformin", "CT scan", "HbA1c", "right lung", "C3-C4", "mL"
    } <= set(by_content)
    assert "M R I" in by_content["MRI"]["sounds_like"]
    assert "ام آر آی" in by_content["MRI"]["sounds_like"]
    assert "H B A one C" in by_content["HbA1c"]["sounds_like"]
    assert "C three C four" in by_content["C3-C4"]["sounds_like"]
    assert "میلی لیتر" in by_content["mL"]["sounds_like"]


def test_no_audio_persistence_references_in_app():
    src = Path(app_module.__file__).read_text(encoding="utf-8")
    assert ".wav" not in src
    assert "recordings" not in src
    assert "audio_path" not in src


def test_overlay_has_final_api():
    from overlay import TranscriptOverlay
    assert hasattr(TranscriptOverlay, "set_final")
    assert hasattr(TranscriptOverlay, "set_partial")
    assert hasattr(TranscriptOverlay, "set_done")


# ------------------------------------------------- overlay smart direction

def test_detect_direction_persian_dominant_is_rtl():
    from overlay import detect_direction
    assert detect_direction("بیمار در CCU بستری است") == "rtl"
    assert detect_direction("فشار خون بالا") == "rtl"
    assert detect_direction("") == "rtl"  # product default


def test_detect_direction_english_dominant_is_ltr():
    from overlay import detect_direction
    assert detect_direction("CT scan completed successfully") == "ltr"


def test_detect_direction_mixed_respects_ratio_not_first_char():
    """A Persian-dominant sentence that starts with Latin stays RTL -
    smarter than the old first-strong-character rule."""
    from overlay import detect_direction
    assert detect_direction("CT scan بیمار نشان می دهد لیژن وجود ندارد") == "rtl"
    assert detect_direction("BP 120/80") == "ltr"


def test_shape_for_display_is_safe_without_bidi():
    from overlay import shape_for_display
    # with or without python-bidi installed this must not raise and must
    # return a non-empty string for non-empty input
    assert shape_for_display("بیمار CT scan")
    assert shape_for_display("") == ""


def test_overlay_close_schedules_destroy_after_marking_closed():
    """Regression: close used to route through _ui and drop its own callback."""
    from overlay import TranscriptOverlay

    events = []

    class Root:
        def after(self, delay, callback):
            events.append(("after", delay))
            callback()

        def destroy(self):
            events.append(("destroy",))

    overlay = TranscriptOverlay.__new__(TranscriptOverlay)
    overlay._root = Root()
    overlay._closed = False
    overlay._thread = None

    overlay.close()

    assert overlay._closed is True
    assert overlay._root is None
    assert events == [("after", 0), ("destroy",)]

    # Closing is idempotent and must not schedule another Tk operation.
    overlay.close()
    assert events == [("after", 0), ("destroy",)]
