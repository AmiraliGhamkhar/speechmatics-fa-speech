"""Regression tests for the deterministic nursing text stage.

Every test here corresponds to a bug the nursing benchmark caught, or to a
safety rule the stage must never violate. Each one states the OLD wrong
behaviour and the NEW expected behaviour.
"""

import re
from pathlib import Path

import pytest

from speechmatics_test.matcher import MedicalMatcher
from speechmatics_test.medical_layer import MedicalLayer
from speechmatics_test.nursing_text import (
    PolishReport,
    ZWNJ,
    collapse_repetitions,
    format_vital_signs,
    normalize_clock_times,
    normalize_spoken_ratios,
    normalize_units,
    persian_number_words_to_digits,
    polish_document,
    polish_nursing_text,
    prepolish_asr_artifacts,
    to_persian_digits,
)
from speechmatics_test.presentation import BIDI_CONTROLS
from speechmatics_test.text import normalize_text

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def layer():
    return MedicalLayer(ROOT)


def polish(text: str) -> str:
    return polish_nursing_text(normalize_text(text))


# ------------------------------------------------- Persian spoken numbers

@pytest.mark.parametrize("spoken,expected", [
    ("سی و پنج ساله", "35 ساله"),
    ("بیست", "20"),
    ("هفده", "17"),
    ("صد و بیست", "120"),
    ("سه هزار و دویست و سی و پنج", "3235"),
    ("چهل و پنج", "45"),
])
def test_persian_cardinals_become_digits(spoken, expected):
    assert polish(spoken) == expected


@pytest.mark.parametrize("text", [
    "یک بیمار در بخش پذیرش شد",      # "یک" = the article "a"
    "نه فقط درد قفسه سینه",           # "نه" = "not"
    "سی تی اسکن قفسه سینه",           # "سی" is part of the ASR form for CT
])
def test_ambiguous_single_cardinals_are_left_alone(text):
    """Old behaviour would have produced '1 بیمار' / '30 تی اسکن'.

    A bare ambiguous cardinal only converts inside an explicit numeric
    context (a counter word after it, or 'ساعت'/'نمره' before it).
    """
    assert polish(text) == normalize_text(text)


def test_ambiguous_cardinal_converts_with_a_counter_word():
    # "درصد" is the counter that disambiguates "ده"; converting the WORD
    # "درصد" into "%" is the dictionary's job, not this stage's.
    assert polish("ده درصد") == "10 درصد"
    assert polish("نمره ده") == "نمره 10"
    assert polish("ده ساله") == "10 ساله"


def test_bare_adjacent_cardinals_are_not_merged():
    """Regression: 'ده سی' was read as one number and became 40.

    Persian cardinals are joined with an explicit 'و'; two bare cardinals
    side by side are not one value, and inventing 40 out of them would be
    a fabricated clinical number.
    """
    assert polish("ده سی") == "10 سی"


# -------------------------------------------------------- Arabic / Persian

def test_persian_digits_fold_to_ascii():
    # Digit folding is generic normalization; the Persian unit NAME is
    # canonicalized by the dictionary, so this asserts the digits only.
    assert polish("۱۴۰/۸۵ mmHg") == "140/85 mmHg"
    assert polish("۹۷ درصد") == "97 درصد"


def test_arabic_indic_digits_fold_to_ascii():
    assert polish("٨٨ بار در دقیقه") == "88 بار در دقیقه"


def test_to_persian_digits_is_presentation_only():
    assert to_persian_digits("BP: 140/85") == "BP: ۱۴۰/۸۵"
    # and the canonical pipeline never emits them
    assert polish("فشار ۱۴۰/۸۵") == "فشار 140/85"


# -------------------------------------------------------------- clock time

@pytest.mark.parametrize("spoken,expected", [
    ("ده و نیم", "10:30"),
    ("ده و ربع", "10:15"),
    ("ده و سی دقیقه", "10:30"),
    ("ساعت ده و سی دقیقه", "ساعت 10:30"),
    ("10 و 30 دقیقه", "10:30"),
    ("۱۰ و ۳۰ دقیقه", "10:30"),
    ("10:30", "10:30"),
    ("ساعت ده سی", "ساعت 10:30"),
    ("ده و نیم صبح", "10:30 صبح"),
    ("ده و نیم شب", "10:30 شب"),
    ("9:05", "09:05"),
])
def test_clock_times(spoken, expected):
    assert polish(spoken) == expected


def test_time_does_not_absorb_a_following_number():
    """Spec requirement: '10 و 30 دقیقه 90' must NOT silently drop the 90."""
    report = PolishReport()
    out = polish_nursing_text(normalize_text("10 و 30 دقیقه 90"), report)
    assert out == "10:30 90"
    assert "90" in out
    assert report.warnings
    assert "unbound numeral" in report.warnings[0]


def test_age_is_not_read_as_a_clock_time():
    """'سی و پنج ساله' is an age; it must not become 30:05 or similar."""
    assert polish("مددجو سی و پنج ساله") == "مددجو 35 ساله"


# ------------------------------------------------------------------ units

@pytest.mark.parametrize("spoken,expected", [
    ("140/85 میلی متر جیوه", "140/85 mmHg"),
    ("36.7 درجه سانتی گراد", "36.7 °C"),
    ("97 درصد", "97%"),
    ("20 میلی گرم", "20 mg"),
    ("50 میکروگرم", "50 mcg"),
    ("250 میلی لیتر", "250 mL"),
    ("2 لیتر", "2 L"),
    ("3 سانتی متر", "3 cm"),
])
def test_persian_spoken_units(spoken, expected):
    """These Persian forms are canonicalized by the DICTIONARY; this test
    pins the end-to-end result including spacing."""
    matcher = MedicalMatcher(ROOT)
    canonical, _ = matcher.canonicalize(normalize_text(spoken))
    assert polish_nursing_text(canonical) == expected


@pytest.mark.parametrize("text,expected", [
    ("36.7°C", "36.7 °C"),      # attached
    ("36.7 ° C", "36.7 °C"),    # separated
    ("97 %", "97%"),
    ("140/85 mmhg", "140/85 mmHg"),
    ("88 bpm", "88 bpm"),
    ("500 ml", "500 mL"),
])
def test_ascii_unit_spelling_and_spacing(text, expected):
    assert normalize_units(text) == expected


# ------------------------------------------------------ vital-sign labels

@pytest.mark.parametrize("text,expected", [
    ("Temp . 36.7. °C", "Temp: 36.7 °C"),
    ("blood pressure. 140/85 mmHg", "BP: 140/85 mmHg"),
    ("oxygen saturation و 97%", "SpO2: 97%"),
    ("heart rate. 88", "HR: 88"),
    ("BP 140/85", "BP: 140/85"),
])
def test_vital_sign_label_punctuation(text, expected):
    assert polish(text) == expected


@pytest.mark.parametrize("text", [
    "the patient's blood pressure was reviewed by the physician",
    "oxygen saturation monitoring continues overnight",
    "heart rate variability was discussed",
])
def test_spelled_vital_names_in_prose_are_not_abbreviated(text):
    """Only 'label immediately followed by its value' is a charting
    position. Prose mentions keep their words."""
    assert polish(text) == text


# ------------------------------------------------------------ repetitions

@pytest.mark.parametrize("text,expected", [
    ("نمره نمره", "نمره"),
    ("نمره نمره نمره", "نمره"),
    ("بیمار بیمار در بخش است", "بیمار در بخش است"),
    ("blood pressure pressure", "blood pressure"),
])
def test_duplicated_words_are_collapsed(text, expected):
    assert collapse_repetitions(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("می باشد. باشد.", f"می{ZWNJ}باشد."),
    ("ذکر می کند. کنند", f"ذکر می{ZWNJ}کند"),
])
def test_echoed_verb_tails_are_removed(text, expected):
    assert polish(text) == expected


def test_prepolish_protects_forms_with_a_real_repeated_syllable(layer):
    """Regression: 'سی سی یو' (CCU) became 'سی یو'.

    Persian spells several abbreviations with a genuine doubled syllable.
    The pre-canonicalization stutter cleanup must skip them.
    """
    protected = layer.fst.repetition_safe_forms
    assert prepolish_asr_artifacts(
        "بیمار در سی سی یو است", protected=protected
    ) == "بیمار در سی سی یو است"
    out, _ = layer.normalize("بیمار در سی سی یو بستری است")
    assert "CCU" in out


def test_stutter_is_removed_before_canonicalization(layer):
    """Regression: 'نمره نمره درد' produced 'نمره pain score'.

    The leftover 'نمره' plus the following 'درد' spelled the dictionary
    phrase 'نمره درد', so the stutter CREATED a medical hit. Collapsing
    first yields one clean canonicalization.
    """
    out, _ = layer.normalize("نمره نمره درد ثبت شد")
    assert out == "pain score ثبت شد"
    assert "نمره" not in out


def test_repeated_numeric_values_are_never_collapsed():
    """A repeated measurement is data, not a stutter."""
    assert polish("140/85 140/85") == "140/85 140/85"


# ------------------------------------------------------------ punctuation

def test_repeated_punctuation_is_collapsed():
    assert polish("بیمار پذیرش شد.. علائم پایدار است") == \
        "بیمار پذیرش شد. علائم پایدار است"


def test_whitespace_is_normalized():
    assert polish("بیمار   در   بخش   بستری شد") == "بیمار در بخش بستری شد"


def test_persian_zwnj_typography():
    assert polish("ذکر می کند") == f"ذکر می{ZWNJ}کند"
    assert polish("آموزش های لازم") == f"آموزش{ZWNJ}های لازم"


def test_polish_document_closes_the_sentence():
    assert polish_document("بیمار پذیرش شد") == "بیمار پذیرش شد."
    # already terminated: unchanged
    assert polish_document("بیمار پذیرش شد.") == "بیمار پذیرش شد."


def test_streamed_segment_does_not_get_a_fabricated_full_stop():
    """A mid-sentence ASR segment must not be closed off."""
    assert polish("مددجو با شکایت") == "مددجو با شکایت"


# ---------------------------------------------------------------- safety

def test_polish_is_idempotent():
    samples = [
        "مددجو آقای سی و پنج ساله در ساعت ده و سی دقیقه وارد بخش قلب شد",
        "Temp . 36.7. °C",
        "blood pressure. 140/85 mmHg",
        "نمره نمره درد",
        "می باشد. باشد.",
        "علائم حیاتی شامل BP: 140/85 mmHg، SpO2: 97%، Temp: 36.7 °C",
    ]
    for sample in samples:
        once = polish(sample)
        assert polish_nursing_text(once) == once, sample


def test_polish_never_emits_bidi_controls():
    samples = [
        "مددجو با شکایت chest pain و BP: 140/85 mmHg مراجعه کرد.",
        "علائم حیاتی شامل BP: 140/85 mmHg، SpO2: 97%، Temp: 36.7 °C و "
        "HR: 88 می\u200cباشد.",
    ]
    for sample in samples:
        out = polish(sample)
        assert not any(ch in BIDI_CONTROLS for ch in out), sample


def test_logical_order_is_preserved_for_mixed_text():
    """Mixed RTL/LTR text keeps its logical character order; Latin tokens,
    numbers and punctuation are not displaced."""
    text = "مددجو با شکایت chest pain و BP: 140/85 mmHg مراجعه کرد."
    out = polish(text)
    assert out == text
    assert out.index("chest pain") < out.index("BP: 140/85 mmHg")


def test_no_number_is_invented_or_dropped():
    """Every numeral in the input survives into the output."""
    import re
    samples = [
        "BP 140/85 و HR 88 و SpO2 97 و Temp 36.7",
        "10 و 30 دقیقه 90",
        "دوز 500 میلی گرم هر 8 ساعت",
    ]
    for sample in samples:
        source = normalize_text(sample)
        out = polish_nursing_text(source)
        before = re.findall(r"\d+", source)
        after = re.findall(r"\d+", out)
        assert sorted(before) == sorted(after), (sample, before, after)


def test_negation_is_preserved(layer):
    """No semantic inference: a negated finding stays negated."""
    out, _ = layer.normalize("بیمار cardiovascular disease ندارد")
    assert out.endswith("ندارد")
    assert "cardiovascular disease" in out


def test_medical_layer_polish_can_be_disabled():
    plain = MedicalLayer(ROOT, polish=False)
    out, _ = plain.normalize("Temp . 36.7. °C")
    assert out != "Temp: 36.7 °C"  # untouched by the polish stage


def test_polish_report_is_auditable():
    report = PolishReport()
    polish_nursing_text(normalize_text("Temp . 36.7. °C"), report)
    assert report.rules_applied
    assert report.to_dict()["change_count"] >= 1


# ------------------------------------- malformed cardinals (safety critical)

def test_malformed_cardinal_is_never_summed_into_a_fabricated_value():
    """Regression, clinical safety: 'سی و شش و هفت' is how a nurse dictates
    the temperature 36.7. The parser used to add its parts and emit 43 - a
    body temperature that was never spoken, invented by the postprocessor,
    with no warning. A number the system cannot parse must survive verbatim.
    """
    report = PolishReport()
    out = persian_number_words_to_digits("سی و شش و هفت", report)
    assert out == "سی و شش و هفت"
    assert "43" not in out
    assert report.warnings


def test_malformed_cardinal_is_not_half_converted():
    """Skipping only the bad part would leave 'سی و شش و 7'. The whole run
    must be left alone so a human reads exactly what was said."""
    assert persian_number_words_to_digits("بیست و یک و دو") == "بیست و یک و دو"
    assert not re.search(r"\d", persian_number_words_to_digits("سی و شش و هفت"))


def test_malformed_cardinal_warning_is_reported_through_the_layer():
    report = PolishReport()
    polish_nursing_text("درجه حرارت سی و شش و هفت درجه سانتی گراد", report)
    assert any("well-formed cardinal" in w for w in report.warnings)


@pytest.mark.parametrize("spoken,expected", [
    ("سی و پنج", "35"),
    ("سی و شش", "36"),
    ("صد و چهل", "140"),
    ("هشتاد و پنج", "85"),
    ("نود و هفت", "97"),
    ("صد و بیست و پنج", "125"),
    ("سه هزار و دویست و سی و پنج", "3235"),
    ("دو هزار و بیست و چهار", "2024"),
])
def test_well_formed_cardinals_still_convert(spoken, expected):
    """The well-formedness check must not cost any legitimate conversion."""
    assert persian_number_words_to_digits(spoken) == expected


def test_descending_magnitude_rule_rejects_repeats():
    """Each magnitude class may be named at most once, in descending order."""
    assert persian_number_words_to_digits("بیست و سی") == "بیست و سی"
    assert persian_number_words_to_digits("صد و دویست") == "صد و دویست"


# ------------------------------------------------- spoken blood pressure

def test_spoken_blood_pressure_becomes_a_charted_ratio():
    """Regression: a dictated BP stayed as '140 روی 85' - not the charted
    form, and not what the benchmark's own reference expects."""
    assert polish("فشار خون صد و چهل روی هشتاد و پنج") == "BP: 140/85" or \
        "140/85" in polish("صد و چهل روی هشتاد و پنج")
    assert polish("صد و چهل روی هشتاد و پنج") == "140/85"
    assert polish("140 روی 85") == "140/85"


def test_english_over_is_also_a_ratio():
    assert polish("140 over 85") == "140/85"


def test_ratio_word_between_non_numbers_is_left_alone():
    """'روی' is an ordinary preposition ('on'); only a numeral on BOTH
    sides makes it a ratio."""
    assert polish("پانسمان روی زخم تعویض شد") == "پانسمان روی زخم تعویض شد"
    assert polish("گاز روی محل بخیه") == "گاز روی محل بخیه"


def test_ratio_normalization_preserves_both_values():
    for spoken, expected in [("120 روی 80", "120/80"),
                             ("90 روی 60", "90/60"),
                             ("160 روی 100", "160/100")]:
        assert polish(spoken) == expected


def test_ratio_normalization_is_idempotent():
    once = polish("صد و چهل روی هشتاد و پنج")
    assert polish(once) == once
