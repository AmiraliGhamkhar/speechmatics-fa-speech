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

    # The vocabulary is derived from the dictionary's speechmatics:true
    # entries (single source of truth), not read from a separate file.
    from speechmatics_test.medical_layer import MedicalLayer

    vocab = SpeechmaticsRealtime._clean_vocab(
        MedicalLayer(app_module.ROOT).additional_vocab
    )
    by_content = {item["content"]: item for item in vocab if isinstance(item, dict)}
    # budgeted biasing list, not the local dictionary (shared budget:
    # tests/test_dictionary.py::test_vocabulary_stays_bounded_not_the_full_dictionary)
    assert len(vocab) <= 150
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


# ------------------------------------ FinalStreamCanonicalizer (H3 fix)

def make_accumulator(no_medical=False):
    from speechmatics_test.medical_layer import MedicalLayer

    return app_module.FinalStreamCanonicalizer(
        None if no_medical else MedicalLayer(app_module.ROOT)
    )


def test_accumulator_holds_measurement_phrase_for_numeric_context_only():
    acc = make_accumulator()
    from speechmatics_test.text import normalize_text

    first = acc.add(normalize_text("فشار خون"), [])
    # The phrase may still receive a value in the next tiny ASR final.
    assert first is None
    second = acc.add(normalize_text("بالا دارد"), [])
    # No measurement followed: ordinary Persian narrative remains Persian.
    assert second == "فشار خون بالا دارد"
    assert acc.canonical_text == "فشار خون بالا دارد"
    assert acc.hits == []


def test_accumulator_flush_emits_standalone_measurement_phrase_verbatim():
    acc = make_accumulator()
    from speechmatics_test.text import normalize_text

    assert acc.add(normalize_text("فشار خون"), []) is None
    assert acc.flush() == "فشار خون"
    assert acc.canonical_text == "فشار خون"


def test_accumulator_emits_standalone_complete_rule_without_delay():
    """A complete, non-extendable one-token rule (no longer sibling rule
    starts with the same token) must be injected immediately instead of
    being buffered forever waiting for a continuation no rule defines.
    """
    acc = make_accumulator()
    from speechmatics_test.text import normalize_text

    # "iv" is a complete rule on its own; no rule form begins "iv ...", so
    # nothing could ever extend it into a longer match.
    assert acc.add(normalize_text("iv"), []) == "IV"
    assert acc.canonical_text == "IV"
    # A trailing flush (end of session) has nothing left buffered.
    assert acc.flush() is None


def test_accumulator_does_not_buffer_ordinary_persian_dictionary_phrases():
    """Rules suppressed by conservative mode must not delay live injection."""
    acc = make_accumulator()
    from speechmatics_test.text import normalize_text

    first = acc.add(normalize_text("بیمار در سی تی اسکن"), [])
    second = acc.add(normalize_text("لیژن در رایت لانگ"), [])
    tail = acc.flush()
    assert first == "بیمار در CT scan"
    assert second == "لیژن در رایت لانگ"
    assert tail is None
    assert acc.canonical_text == "بیمار در CT scan لیژن در رایت لانگ"


def test_accumulator_emitted_pieces_never_duplicate_text():
    acc = make_accumulator()
    from speechmatics_test.text import normalize_text

    emissions = [
        acc.add(normalize_text(s), [])
        for s in ["بیمار در سی تی اسکن", "لیژن در رایت لانگ"]
    ]
    emissions.append(acc.flush())
    joined = " ".join(e.strip() for e in emissions if e).strip()
    # every emitted piece survives exactly once, in order
    assert joined == acc.canonical_text
    assert acc.canonical_text == "بیمار در CT scan لیژن در رایت لانگ"


def test_accumulator_without_medical_layer_passes_text_through():
    acc = make_accumulator(no_medical=True)
    from speechmatics_test.text import normalize_text

    assert acc.add(normalize_text("سی تی اسکن"), []) == "سی تی اسکن"
    assert acc.add(normalize_text("فشار خون"), []) == "فشار خون"
    assert acc.canonical_text == "سی تی اسکن فشار خون"
    assert acc.hits == []


def test_accumulator_empty_adds_are_ignored():
    acc = make_accumulator(no_medical=True)
    assert acc.add("", []) is None
    assert acc.flush() is None
    assert acc.canonical_text == ""


def test_accumulator_combines_only_fragmented_numeric_tail():
    """Tiny finals form one age without retaining the surrounding session."""
    acc = make_accumulator()
    from speechmatics_test.text import normalize_text

    assert acc.add(normalize_text("مددجو آقای"), []) == "مددجو آقای"
    assert acc.add(normalize_text("پنجاه"), []) is None
    assert acc.add(normalize_text("و هشت"), []) is None
    assert acc.add(normalize_text("ساله با شکایت"), []) == "58 ساله با شکایت"
    assert acc.flush() is None
    assert acc.canonical_text == "مددجو آقای 58 ساله با شکایت"


def test_accumulator_combines_fragmented_measurement_without_loss():
    acc = make_accumulator()
    from speechmatics_test.text import normalize_text

    emissions = [
        acc.add(normalize_text(segment), [])
        for segment in ("فشار خون", "صد و چهل", "روی هشتاد", "ثبت شد")
    ]
    emissions.append(acc.flush())
    assert " ".join(part for part in emissions if part) == "BP 140/80 ثبت شد"
    assert acc.canonical_text == "BP 140/80 ثبت شد"


def test_accumulator_emits_standalone_narrative_immediately():
    acc = make_accumulator()
    assert acc.add("ارزیابی اولیه پرستاری", []) == "ارزیابی اولیه پرستاری"
    assert acc.flush() is None


def test_numeric_tail_buffer_is_bounded_and_preserves_every_token():
    acc = make_accumulator()
    emissions = [acc.add("دو", []) for _ in range(20)]
    assert len(acc._buffer.split()) <= 12
    emissions.append(acc.flush())
    joined = " ".join(part for part in emissions if part)
    assert joined == " ".join(["دو"] * 20)
    assert joined == acc.canonical_text


# ------------------------- cross-segment chart-fragment glue (10: / 0. / /90)

def test_accumulator_glues_clock_time_split_across_finals():
    """ASR finalized "۱۰:" and "۳۰" separately; the value must reassemble."""
    acc = make_accumulator()
    from speechmatics_test.text import normalize_text

    first = acc.add(normalize_text("با پای خود در ساعت ۱۰:"), [])
    # The cut stops before the trailing "10:" token, so only the chart
    # fragment stays buffered; the prose is injected immediately.
    assert first == "با پای خود در ساعت"
    second = acc.add(normalize_text("۳۰ وارد بخش قلب شد"), [])
    assert second == "10:30 وارد بخش قلب شد"
    assert acc.flush() is None
    assert acc.canonical_text == "با پای خود در ساعت 10:30 وارد بخش قلب شد"


def test_accumulator_glues_decimal_split_across_finals():
    acc = make_accumulator()
    from speechmatics_test.text import normalize_text

    assert acc.add(normalize_text("نرمال سالین ۰."), []) == "نرمال سالین"
    assert acc.add(normalize_text("۹ درصد وصل شد"), []) == "0.9 % وصل شد"
    assert acc.canonical_text == "نرمال سالین 0.9 % وصل شد"


def test_accumulator_glues_bp_ratio_split_across_finals():
    acc = make_accumulator()
    from speechmatics_test.text import normalize_text

    assert acc.add(normalize_text("فشار خون ۱۴۵"), []) is None
    assert acc.add(normalize_text("/۹۰ بود"), []) == "BP 145/90 بود"
    assert acc.canonical_text == "BP 145/90 بود"


def test_accumulator_glued_output_equals_unsplit_output():
    """A split value must canonicalize exactly like the unsplit one."""
    from speechmatics_test.text import normalize_text

    def run(segments):
        acc = make_accumulator()
        for segment in segments:
            acc.add(normalize_text(segment), [])
        acc.flush()
        return acc.canonical_text

    split = run(("فشار خون ۱۴۵", "/۹۰ بود"))
    whole = run(("فشار خون ۱۴۵/۹۰ بود",))
    assert split == whole == "BP 145/90 بود"


def test_accumulator_dangling_chart_fragment_flushes_verbatim():
    """A value whose continuation never arrives is not invented or dropped."""
    acc = make_accumulator()
    from speechmatics_test.text import normalize_text

    assert acc.add(normalize_text("ساعت ۱۰:"), []) == "ساعت"
    assert acc.flush() == "10:"
    assert acc.canonical_text == "ساعت 10:"


def test_accumulator_does_not_glue_prose_after_a_value():
    """A plain spoken time followed by a word must not be touched."""
    acc = make_accumulator()
    from speechmatics_test.text import normalize_text

    assert acc.add(normalize_text("قرص را ساعت 8"), []) == "قرص را"
    assert acc.add(normalize_text("خورد"), []) == "ساعت 8 خورد"
    assert acc.canonical_text == "قرص را ساعت 8 خورد"


# ---------------------------------------------- InjectionWorker (H2 fix)

class FakeInjector:
    def __init__(self, fail_on=None, delay=0.0):
        self.pasted = []
        self.resets = 0
        self.fail_on = fail_on
        self.delay = delay

    def arm_target(self):
        return True

    def reset_partial(self):
        self.resets += 1

    def paste_text(self, text, add_rtl_mark=False):
        if self.delay:
            import time
            time.sleep(self.delay)
        self.pasted.append((text, add_rtl_mark))
        return self.fail_on != text


def test_injection_worker_preserves_final_order_and_records_results():
    injector = FakeInjector()
    worker = app_module.InjectionWorker(injector)
    worker.submit("یک")
    worker.submit("دو")
    worker.submit("سه")
    worker.shutdown()
    assert injector.pasted == [("یک ", True), ("دو ", True), ("سه ", True)]
    assert injector.resets == 3
    assert worker.records == [
        {"text": "یک", "success": True},
        {"text": "دو", "success": True},
        {"text": "سه", "success": True},
    ]


def test_injection_worker_records_failed_pastes_and_keeps_going():
    injector = FakeInjector(fail_on="bad ")
    results = []
    worker = app_module.InjectionWorker(injector, on_result=results.append)
    worker.submit("bad")
    worker.submit("good")
    worker.shutdown()
    assert [r["success"] for r in worker.records] == [False, True]
    assert [r["text"] for r in worker.records] == ["bad", "good"]
    assert results == worker.records


def test_injection_worker_shutdown_is_safe_without_jobs():
    worker = app_module.InjectionWorker(FakeInjector())
    worker.shutdown()
    assert worker.records == []
    worker.shutdown()  # idempotent
    assert worker.records == []


# ------------------------------------------------- overlay lifecycle (M7)

def test_overlay_close_never_destroys_tcl_root_on_callback_failure():
    from overlay import TranscriptOverlay

    destroyed = []

    class Root:
        def after(self, delay, callback):
            raise RuntimeError("event loop is racing shutdown")

        def destroy(self):
            destroyed.append(True)

    overlay = TranscriptOverlay.__new__(TranscriptOverlay)
    overlay._root = Root()
    overlay._label = object()
    overlay._status = object()
    overlay._closed = False
    overlay._thread = None

    overlay.close()
    assert destroyed == []  # caller thread must not deallocate Tcl
    overlay._tick_follow()  # represents the already scheduled UI-thread tick
    assert destroyed == [True]
    assert overlay._root is None
    assert overlay._label is None
    assert overlay._status is None


def test_overlay_abort_destroys_root_created_after_shutdown():
    from overlay import TranscriptOverlay

    destroyed = []

    class Root:
        def destroy(self):
            destroyed.append(True)

    overlay = TranscriptOverlay.__new__(TranscriptOverlay)
    overlay._closed = True
    assert overlay._abort_if_closed(Root()) is True
    assert destroyed == [True]

    overlay._closed = False
    assert overlay._abort_if_closed(Root()) is False
    assert destroyed == [True]  # live overlay: nothing destroyed
