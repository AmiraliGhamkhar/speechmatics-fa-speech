"""ClinicalEntityGuard regressions: review metadata must be deterministic only."""

from speechmatics_test.entity_guard import ClinicalEntityGuard


def test_bp_systolic_less_than_diastolic():
    guard = ClinicalEntityGuard()
    flags = guard.scan("BP 80/120")
    assert any(f.entity_type == "bp" and "impossible" in f.issue for f in flags)


def test_temperature_truncation():
    guard = ClinicalEntityGuard()
    flags = guard.scan("Temp 36")
    assert any(f.entity_type == "temperature" and "decimal" in f.issue for f in flags)


def test_valid_bp():
    guard = ClinicalEntityGuard()
    flags = guard.scan("BP 120/80")
    assert not any(f.entity_type == "bp" for f in flags)


def test_invalid_iv_gauge():
    guard = ClinicalEntityGuard()
    flags = guard.scan("IV-Line 40G فیکس شد")
    assert any(f.entity_type == "iv_gauge" for f in flags)


def test_valid_iv_gauge():
    guard = ClinicalEntityGuard()
    flags = guard.scan("IV-Line 20G فیکس شد")
    assert not any(f.entity_type == "iv_gauge" for f in flags)


def test_time_inconsistency():
    guard = ClinicalEntityGuard()
    flags = guard.scan("ورود در ساعت 10:30 ... اطلاع داده شد ساعت 10:15")
    assert any(f.entity_type == "clock_time" for f in flags)


def test_mixed_language():
    guard = ClinicalEntityGuard()
    flags = guard.scan("SpO2 96% می‌باشد")
    assert not any(f.severity == "critical" for f in flags)


def test_idempotent():
    guard = ClinicalEntityGuard()
    text = "آقای 40 ساله با BP 100/80"
    flags1 = guard.scan(text)
    flags2 = guard.scan(text)
    assert flags1 == flags2


def test_non_destructive():
    guard = ClinicalEntityGuard()
    text = "آقای 40 ساله"
    guard.scan(text)
    assert text == "آقای 40 ساله"


def test_known_asr_sensitive_age_is_review_only_info():
    flags = ClinicalEntityGuard().scan("آقای 40 ساله")
    assert any(flag.entity_type == "age" and flag.severity == "info" for flag in flags)


def test_persian_digits_keep_original_character_offsets():
    guard = ClinicalEntityGuard()
    text = "دمای ۳۶ درجه"
    flag = next(f for f in guard.scan(text) if f.entity_type == "temperature")
    assert flag.value == "36"
    assert flag.position == text.index("۳")
    assert flag.to_dict()["type"] == "temperature"


def test_invalid_scores_and_concentration_are_flagged_without_rewriting():
    guard = ClinicalEntityGuard()
    text = "Morse 126 و Braden 5 و نرمال سالین 3%"
    flags = guard.scan(text)
    assert {flag.entity_type for flag in flags} >= {"morse", "braden", "concentration"}
    assert text == "Morse 126 و Braden 5 و نرمال سالین 3%"
