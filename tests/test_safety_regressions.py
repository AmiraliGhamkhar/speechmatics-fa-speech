"""Focused regressions for clean injection and non-destructive ASR review flags."""

from pathlib import Path

import app as app_module
from injector import RLM, TextInjector, contains_rtl
from speechmatics_test.medical_layer import MedicalLayer
from speechmatics_test.presentation import has_bidi_controls
from speechmatics_test.text import normalize_text


ROOT = Path(__file__).resolve().parents[1]


def test_default_injector_pastes_clean_logical_unicode():
    injector = TextInjector(dry_run=True, add_bidi_marks=False)
    logical = "بیمار SpO2 97 % دارد"

    payload = injector.prepare_mixed_text(logical)

    assert payload == logical
    assert not has_bidi_controls(payload)


def test_bidi_injection_remains_explicit_compatibility_opt_in():
    injector = TextInjector(dry_run=True, add_bidi_marks=True)
    payload = injector.prepare_mixed_text("بیمار SpO2 97 % دارد")

    assert has_bidi_controls(payload)
    assert injector.logical_payload(payload) == "بیمار SpO2 97 % دارد"


def test_injection_worker_pastes_clean_logical_unicode():
    """Regression: the live path pasted a stray RLM into every RTL segment.

    The worker called ``paste_text(..., add_rtl_mark=True)`` while the app
    builds the injector with ``add_bidi_marks=False``. ``prepare_mixed_text``
    therefore returned unwrapped text, the panic-mark branch fired, and every
    Persian segment was pasted with a leading U+200F - exactly the stored
    direction control the clean-injection policy exists to prevent. The
    existing guard only covered ``prepare_mixed_text``, never the worker's
    actual call, so this asserts on what the clipboard really receives.
    """
    payloads = []

    class RecordingInjector:
        def reset_partial(self):
            pass

        def paste_text(self, text, add_rtl_mark=False):
            # The real preparation step, so the payload under test is the one
            # the clipboard would really receive.
            prepared = injector.prepare_mixed_text(text)
            if add_rtl_mark and not prepared.startswith(RLM) and contains_rtl(prepared):
                prepared = RLM + prepared
            payloads.append(prepared)
            return True

    injector = TextInjector(dry_run=True, add_bidi_marks=False)
    worker = app_module.InjectionWorker(RecordingInjector())
    worker.submit("بیمار SpO2 97 % دارد")
    worker.submit("فشار خون 120/80")
    worker.shutdown()

    assert payloads == ["بیمار SpO2 97 % دارد ", "فشار خون 120/80 "]
    assert not any(has_bidi_controls(payload) for payload in payloads)


def test_injection_worker_survives_a_failing_result_callback():
    """A raising UI callback must not silently drop the remaining pastes."""
    seen = []

    def explode(_record):
        raise RuntimeError("overlay is gone")

    injector = TextInjector(dry_run=True, add_bidi_marks=False)
    worker = app_module.InjectionWorker(
        injector, on_result=lambda record: (seen.append(record), explode(record))
    )
    worker.submit("یک")
    worker.submit("دو")
    worker.shutdown()

    # Both pastes were still performed and recorded, in order.
    assert [record["text"] for record in worker.records] == ["یک", "دو"]
    assert len(seen) == 2
    assert not worker._thread.is_alive()


def test_low_confidence_entity_is_flagged_without_changing_text():
    accumulator = app_module.FinalStreamCanonicalizer(MedicalLayer(ROOT))
    text = normalize_text("Temp 45 ثبت شد")
    words = [
        {"content": "Temp", "confidence": 0.98, "language": "en",
         "start_time": 0.0, "end_time": 0.2},
        {"content": "45", "confidence": 0.42, "language": "fa",
         "start_time": 0.2, "end_time": 0.4},
        {"content": "ثبت", "confidence": 0.99, "language": "fa",
         "start_time": 0.4, "end_time": 0.6},
        {"content": "شد", "confidence": 0.99, "language": "fa",
         "start_time": 0.6, "end_time": 0.8},
    ]

    emitted = accumulator.add(text, words)

    assert emitted == "Temp 45 ثبت شد"
    assert accumulator.canonical_text == "Temp 45 ثبت شد"
    assert accumulator.last_flags == [{
        "kind": "low_confidence_entity",
        "content": "45",
        "confidence": 0.42,
        "language": "fa",
        "start_time": 0.2,
        "end_time": 0.4,
    }]
    assert accumulator.flags == accumulator.last_flags


def test_high_confidence_entity_has_no_review_flag():
    accumulator = app_module.FinalStreamCanonicalizer(MedicalLayer(ROOT))
    emitted = accumulator.add(normalize_text("SpO2 97 %"), [
        {"content": "SpO2", "confidence": 0.99, "language": "en"},
        {"content": "97", "confidence": 0.98, "language": "fa"},
        {"content": "%", "confidence": 0.98, "language": "fa"},
    ])

    assert emitted == "SpO2 97 %"
    assert accumulator.last_flags == []
    assert accumulator.flags == []
