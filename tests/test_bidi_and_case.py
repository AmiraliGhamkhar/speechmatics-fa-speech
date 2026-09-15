"""Regression tests for the two production bugs:

1. medical canonicalization was case-sensitive, so an ASR result of "MRI"
   never matched the lowercase rule "mri";
2. the overlay pre-shaped text with ``python-bidi.get_display()``, which
   double-applied the BiDi algorithm on top of the renderer's own layout.

They also lock in the data/presentation separation: the canonical medical
transcript must never contain RLM/RLE/PDF, and the presentation wrappers
must be applied exactly once.
"""

from pathlib import Path

import pytest

import overlay as overlay_mod
from injector import TextInjector
from speechmatics_test.fst import casefold_preserving
from speechmatics_test.medical_layer import MedicalLayer
from speechmatics_test.presentation import (
    PDF,
    RLE,
    RLM,
    BIDI_CONTROLS,
    detect_direction,
    strip_bidi_controls,
    wrap_for_direction,
)
from speechmatics_test.text import normalize_text

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def medical() -> MedicalLayer:
    return MedicalLayer(ROOT)


def canon(medical: MedicalLayer, text: str) -> str:
    return medical.canonicalize(normalize_text(text))[0]


# ------------------------------------------------------- casefold helper


@pytest.mark.parametrize("text", [
    "بیمار MRI انجام داد",
    "فشار خون 120/80",
    "mixed بیمار CT scan 20 mg",
    "",
    "ß ﬀ İ",
])
def test_casefold_preserving_keeps_length(text):
    """Index i of the folded projection must be index i of the original."""
    assert len(casefold_preserving(text)) == len(text)


def test_casefold_does_not_change_persian():
    persian = "بیمار برای سی تی اسکن مراجعه کرد و نیم‌فاصله"
    assert casefold_preserving(persian) == persian


# ------------------------------------------------------- canonicalization


@pytest.mark.parametrize("form,expected", [
    ("MRI", "MRI"),
    ("mri", "MRI"),
    ("Mri", "MRI"),
    ("CT", "CT"),
    ("ct", "CT"),
    ("CT scan", "CT scan"),
    ("ct scan", "CT scan"),
    ("Ct Scan", "CT scan"),
    ("ECG", "ECG"),
    ("ecg", "ECG"),
    ("ABG", "ABG"),
    ("abg", "ABG"),
    ("IV", "IV"),
    ("iv", "IV"),
])
def test_case_insensitive_matching_keeps_canonical_case(medical, form, expected):
    assert canon(medical, f"بیمار {form} انجام داد") == f"بیمار {expected} انجام داد"


@pytest.mark.parametrize("text,expected", [
    ("بیمار MRI انجام داد", "بیمار MRI انجام داد"),
    ("بیمار mri انجام داد", "بیمار MRI انجام داد"),
    ("بیمار CT scan انجام داد", "بیمار CT scan انجام داد"),
    ("بیمار ct scan انجام داد", "بیمار CT scan انجام داد"),
    ("بیمار mri دارد", "بیمار MRI دارد"),
    ("بیمار MRI دارد", "بیمار MRI دارد"),
])
def test_expected_examples(medical, text, expected):
    assert canon(medical, text) == expected


def test_persian_to_english_replacement(medical):
    assert canon(medical, "بیمار فشار خون دارد") == "بیمار BP دارد"


def test_longest_phrase_wins(medical):
    # "ct scan" (longer) must beat the shorter "ct" rule.
    assert canon(medical, "ct scan") == "CT scan"
    assert canon(medical, "ct") == "CT"
    # Persian longest-match: "فشار خون بالا" -> HTN, not "BP بالا".
    assert canon(medical, "فشار خون بالا") == "HTN"


@pytest.mark.parametrize("text", [
    "action",       # contains "ct"
    "octopus",      # contains "ct"
    "scrimping",    # contains "iv"? no - guard against odd substrings
    "delivery",     # contains "iv"
    "amrit",        # contains "mri"
    "recognise",    # contains "ecg"
])
def test_no_unsafe_substring_replacement(medical, text):
    """A rule form inside a longer token must never fire (token boundaries)."""
    assert canon(medical, f"the {text} here") == f"the {text} here"


def test_token_boundaries_with_punctuation(medical):
    assert canon(medical, "بیمار (mri) و ct, انجام شد") == "بیمار (MRI) و CT, انجام شد"


def test_mixed_persian_english_sentence(medical):
    out = canon(
        medical,
        "بیمار برای mri و ct scan مراجعه کرد و ecg و abg انجام شد و فشار خون 120/80 بود.",
    )
    for token in ("MRI", "CT scan", "ECG", "ABG", "BP"):
        assert token in out
    assert "120/80" in out
    assert not any(ch in BIDI_CONTROLS for ch in out)


def test_canonicalization_is_deterministic(medical):
    text = "بیمار mri و CT Scan و ecg"
    assert canon(medical, text) == canon(medical, text)


def test_case_insensitive_matches_reference_scanner(medical):
    fst = medical.fst
    for text in ["بیمار MRI انجام داد", "ct SCAN و Ecg", "iv و ABG"]:
        normalized = normalize_text(text)
        assert fst._scan(normalized) == fst._scan_reference(normalized)


# ------------------------------------------------------ RTL presentation


@pytest.mark.parametrize("logical", [
    "بیمار بستری شد",                                  # Persian only
    "بیمار MRI انجام داد",                             # Persian + MRI
    "بیمار CT scan انجام داد",                         # Persian + CT scan
    "بیمار ECG شد",                                    # Persian + ECG
    "بیمار 120/80 بود",                                # Persian + numbers
    "بیمار 20 mg گرفت",                                # Persian + dosage
    "بیمار MRI و CT scan و 120/80 داشت",               # Persian + English + numbers
])
def test_rtl_presentation_wrap(logical):
    display = wrap_for_direction(logical)
    assert display == RLM + RLE + logical + PDF
    # Nothing but the controls was added: the logical text is untouched.
    assert strip_bidi_controls(display) == logical


def test_ltr_text_is_not_wrapped():
    logical = "patient had an MRI and a CT scan"
    assert detect_direction(logical) == "ltr"
    assert wrap_for_direction(logical) == logical


def test_wrap_is_idempotent():
    logical = "بیمار MRI انجام داد"
    once = wrap_for_direction(logical)
    twice = wrap_for_direction(once)
    assert once == twice
    assert once.count(RLM) == 1
    assert once.count(RLE) == 1
    assert once.count(PDF) == 1


def test_overlay_uses_logical_order_not_visual():
    logical = "بیمار MRI و CT scan انجام داد"
    display = overlay_mod.display_text(logical)
    assert display == RLM + RLE + logical + PDF
    # The Persian must NOT be reversed, and the English term must stay intact.
    assert "MRI" in display and "CT scan" in display
    assert strip_bidi_controls(display) == logical


def test_overlay_render_returns_direction():
    ov = overlay_mod.TranscriptOverlay.__new__(overlay_mod.TranscriptOverlay)
    display, direction = ov._render("بیمار MRI انجام داد")
    assert direction == "rtl"
    assert display.startswith(RLM + RLE) and display.endswith(PDF)
    display, direction = ov._render("patient MRI done")
    assert direction == "ltr"
    assert display == "patient MRI done"


def test_no_get_display_in_overlay_path():
    source = (ROOT / "overlay.py").read_text(encoding="utf-8")
    # Only the module docstring may mention it (to explain why it is gone).
    code = source.split('"""', 2)[-1]
    assert "get_display" not in code
    assert "bidi.algorithm" not in code


def test_canonical_transcript_has_no_bidi_controls(medical):
    out = canon(medical, "بیمار mri و ct scan و ecg انجام داد")
    assert RLM not in out and RLE not in out and PDF not in out


# ------------------------------------------------------------- injection


@pytest.fixture()
def injector() -> TextInjector:
    return TextInjector(dry_run=True, add_bidi_marks=True)


def test_injected_payload_equals_canonical(medical, injector):
    canonical = canon(medical, "بیمار برای mri و ct scan مراجعه کرد")
    payload = injector.prepare_mixed_text(canonical)
    assert injector.logical_payload(payload) == canonical
    assert payload == RLM + RLE + canonical + PDF


def test_injector_does_not_rewrite_medical_content(medical, injector):
    """The injector must not run a second medical replacement pass."""
    payload = injector.prepare_mixed_text("بیمار mri انجام داد")
    assert "mri" in injector.logical_payload(payload)  # untouched, not "MRI"


def test_prepare_mixed_text_is_idempotent(injector):
    canonical = "بیمار MRI انجام داد"
    once = injector.prepare_mixed_text(canonical)
    twice = injector.prepare_mixed_text(once)
    thrice = injector.prepare_mixed_text(twice)
    assert once == twice == thrice
    assert once.count(RLM) == 1
    assert once.count(RLE) == 1
    assert once.count(PDF) == 1


def test_trailing_space_stays_inside_the_embedding(injector):
    canonical = "بیمار MRI انجام داد"
    payload = injector.prepare_mixed_text(canonical + " ")
    assert payload == RLM + RLE + canonical + " " + PDF
    # Idempotent even with the separator.
    assert injector.prepare_mixed_text(payload) == payload


def test_paste_text_add_rtl_mark_does_not_duplicate(injector, capsys):
    canonical = "بیمار MRI انجام داد"
    assert injector.paste_text(canonical + " ", add_rtl_mark=True) is True
    out = capsys.readouterr().out
    assert out.count("\\u200f") == 1
    assert out.count("\\u202b") == 1
    assert out.count("\\u202c") == 1


def test_ltr_payload_is_not_wrapped(injector):
    canonical = "patient MRI done"
    assert injector.prepare_mixed_text(canonical) == canonical


def test_end_to_end_raw_canonical_injected(medical, injector):
    raw = "بیمار برای mri و ct scan مراجعه کرد و ecg و abg انجام شد و فشار خون 120/80 بود."
    canonical = canon(medical, raw)
    injected = injector.prepare_mixed_text(canonical + " ")
    assert injected == RLM + RLE + canonical + " " + PDF
    assert injector.logical_payload(injected).strip() == canonical
    for token in ("MRI", "CT scan", "ECG", "ABG", "BP"):
        assert token in canonical
