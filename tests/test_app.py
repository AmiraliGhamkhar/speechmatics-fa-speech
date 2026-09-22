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
    # Bounded ASR-biasing vocabulary, not a dump of the local dictionary.
    # The "< 100" literal this assertion used to carry was stale: the shipped
    # dictionary already exported 146 entries at the previous commit, so the
    # test could only ever pass while the dictionary failed to load at all.
    # The invariant that actually matters is the RATIO - the vocabulary must
    # stay a small curated subset - plus a hard ceiling well inside the
    # Speechmatics additional_vocab limit.
    assert len(vocab) < 300
    assert len(vocab) < 0.25 * len(MedicalLayer(app_module.ROOT).fst.terms)
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


def test_accumulator_holds_phrase_until_it_completes_across_segments():
    acc = make_accumulator()
    from speechmatics_test.text import normalize_text

    first = acc.add(normalize_text("فشار خون"), [])
    # "فشار خون" can still extend to the longest rule "فشار خون بالا":
    # nothing may be injected yet
    assert first is None
    second = acc.add(normalize_text("بالا دارد"), [])
    assert second == "HTN دارد"
    assert acc.canonical_text == "HTN دارد"
    assert [h["canonical"] for h in acc.hits if h["canonical"] == "HTN"]


def test_accumulator_flush_emits_the_remaining_tail():
    acc = make_accumulator()
    from speechmatics_test.text import normalize_text

    assert acc.add(normalize_text("فشار خون"), []) is None
    assert acc.flush() == "BP"
    assert acc.canonical_text == "BP"


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


def test_accumulator_still_holds_genuinely_ambiguous_compound_tail():
    """Guard against over-correcting the standalone-rule fix: a compound
    whose last token is itself the leading token of an unrelated, longer
    sibling rule (here "لانگ" also starts "لانگ ساوندز" = lung sounds) must
    still be held across the segment boundary, because it is genuinely at
    risk of extending into that other rule.
    """
    acc = make_accumulator()
    from speechmatics_test.text import normalize_text

    first = acc.add(normalize_text("بیمار در سی تی اسکن"), [])
    second = acc.add(normalize_text("لیژن در رایت لانگ"), [])
    tail = acc.flush()
    assert acc.canonical_text == "بیمار در CT scan lesion در right lung"
    joined = " ".join(e.strip() for e in (first, second, tail) if e).strip()
    assert joined == acc.canonical_text


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
    assert acc.canonical_text == "بیمار در CT scan lesion در right lung"


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


@pytest.mark.parametrize("segments, expected", [
    (("سی و", "پنج"), "35"),
    (("ساعت ده", "و سی دقیقه"), "ساعت 10:30"),
    (("ساعت ده", "سی دقیقه"), "ساعت 10:30"),
    (("صد و چهل روی", "هشتاد و پنج"), "140/85"),
])
def test_accumulator_keeps_cross_final_nursing_constructs(segments, expected):
    from speechmatics_test.text import normalize_text
    acc = make_accumulator()
    emissions = [acc.add(normalize_text(segment), []) for segment in segments]
    emissions.append(acc.flush())
    assert " ".join(item for item in emissions if item) == expected
    assert make_accumulator().add(normalize_text(" ".join(segments)), []) is None


def test_accumulator_collapses_cross_final_stutter_before_matching():
    from speechmatics_test.text import normalize_text
    acc = make_accumulator()
    assert acc.add(normalize_text("نمره"), []) is None
    emitted = acc.add(normalize_text("نمره درد"), [])
    tail = acc.flush()
    assert " ".join(item for item in (emitted, tail) if item) == "pain score"


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


# ------------------------------- streaming cut guards (session hardening)

def _stream(text, boundary):
    """Feed ``text`` as two final segments split at token ``boundary``."""
    from speechmatics_test.text import normalize_text

    acc = make_accumulator()
    tokens = normalize_text(text).split()
    acc.add(" ".join(tokens[:boundary]), [])
    acc.add(" ".join(tokens[boundary:]), [])
    acc.flush()
    return acc.canonical_text


def test_charted_group_keeps_label_with_its_value():
    """``LABEL: value unit`` must be emitted as one piece.

    Regression: the cut landed between a vital-sign label and its number,
    so a chart line arrived as two fragments and the value lost the label
    it belonged to - the single most dangerous streaming defect here.
    """
    assert _stream("ضربان قلب 88 در دقیقه", 2) == _stream(
        "ضربان قلب 88 در دقیقه", 4)


def test_charted_group_pulls_back_a_label_with_no_value_yet():
    """A trailing label alone must be held, not emitted bare.

    Regression: 'heart rate.' was emitted, then '88' arrived as its own
    orphan piece.
    """
    from speechmatics_test.text import normalize_text

    acc = make_accumulator()
    first = acc.add(normalize_text("ضربان قلب"), [])
    assert first is None, "a bare vital label must never be emitted alone"
    acc.add(normalize_text("88 است"), [])
    acc.flush()
    assert "88" in acc.canonical_text


def test_cut_moves_outside_an_already_matched_multi_token_rule():
    """The cut must not fall INSIDE a rule that already matched.

    Regression: 'سی بی سی' was cut into 'سی بی' + 'سی', which canonicalized
    to two unrelated fragments instead of CBC.
    """
    text = "سی بی سی درخواست شد"
    assert _stream(text, 2) == _stream(text, 5)
    assert "CBC" in _stream(text, 2)


def test_intra_buffer_stutter_is_not_split_across_the_cut():
    """A repeated token pair must stay in one piece so it can be collapsed.

    Regression: 'نمره نمره درد' split into 'نمره' + 'نمره درد', which
    defeated the de-duplication and emitted the word twice.
    """
    produced = _stream("نمره نمره درد سه", 1)
    assert produced.count("pain score") <= 1
    assert produced.split().count("نمره") <= 1


def test_fully_emittable_buffer_is_not_clipped():
    """The 'nothing held' short-circuit must precede the pull-back guards.

    Regression: applying the guards to a buffer with nothing pending
    clipped the cut to 0 and stalled emission entirely.
    """
    from speechmatics_test.text import normalize_text

    acc = make_accumulator()
    acc.add(normalize_text("بیمار هوشیار است"), [])
    acc.flush()
    assert acc.canonical_text.strip()


def test_streamed_output_never_loses_the_final_token():
    from speechmatics_test.text import normalize_text

    text = "درد قفسه سینه دارد"
    tokens = normalize_text(text).split()
    for boundary in range(1, len(tokens)):
        assert _stream(text, boundary).strip(), boundary
