"""Deterministic nursing-documentation normalization (post-canonicalization).

Pipeline position
-----------------

    Speechmatics final
        -> normalize_text()            generic script/whitespace normalization
        -> MedicalMatcher              lexical medical canonicalization
        -> polish_nursing_text()       THIS MODULE
        -> canonical nursing prose

This stage is the "grammar/format normalization layer" of the design: it is
explicit-rule based, deterministic, idempotent and auditable. It contains
**no clinical reasoning** and **no generative rewriting**. Every transform is
one of a small, enumerated set:

* spoken Persian cardinal numbers -> digits (conservative: only in contexts
  that are unambiguously numeric);
* spoken clock expressions -> ``HH:MM``;
* unit spelling/spacing (``mmHg``, ``%``, ``°C``, ``mg`` ...);
* vital-sign label punctuation (``Temp . 36.7. °C`` -> ``Temp: 36.7 °C``);
* duplicated word / duplicated short-phrase cleanup (ASR stutter);
* Persian ZWNJ typography for the very common verb prefixes (``می``/``نمی``);
* punctuation and whitespace hygiene.

Safety contract
---------------

* Numbers are never invented and never deleted. A number that the time
  normalizer does not consume stays in the text verbatim, and the fact that
  it was left unbound is reported as a warning (see ``PolishReport``). The
  benchmark asserts this: ``10 و 30 دقیقه 90`` must not silently become
  ``10:30``.
* The output stays in LOGICAL Unicode order. No RLM/RLE/PDF is ever emitted
  here; direction controls belong to the overlay and the injector only.
* ASCII digits are used throughout, matching ``normalize_text`` (which folds
  Persian/Arabic-Indic digits to ASCII). ``to_persian_digits`` is available
  as a PRESENTATION helper for the overlay and is never applied to canonical
  text.
* Sentence-final punctuation is only added by ``polish_document`` (the whole
  finished transcript), never by ``polish_nursing_text`` (a single streamed
  segment) - a mid-sentence ASR segment must not be given a fabricated
  full stop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "PolishReport",
    "MAX_PENDING_TAIL_TOKENS",
    "pending_tail_tokens",
    "is_vital_label",
    "vital_label_prefix_tokens",
    "number_run_start",
    "duplicate_boundary_tokens",
    "prepolish_asr_artifacts",
    "polish_nursing_text",
    "polish_document",
    "persian_number_words_to_digits",
    "normalize_clock_times",
    "normalize_units",
    "collapse_repetitions",
    "normalize_spoken_ratios",
    "normalize_punctuation",
    "format_vital_signs",
    "to_persian_digits",
    "ZWNJ",
]

ZWNJ = "\u200c"


# --------------------------------------------------------------- reporting


@dataclass
class PolishReport:
    """Auditable record of what the polish stage did (and refused to do)."""

    #: One entry per applied rule: ``(rule_name, before, after)``.
    changes: list[tuple[str, str, str]] = field(default_factory=list)
    #: Human-readable warnings, e.g. a numeral the time rule did not absorb.
    warnings: list[str] = field(default_factory=list)

    def record(self, rule: str, before: str, after: str) -> str:
        if before != after:
            self.changes.append((rule, before, after))
        return after

    def warn(self, message: str) -> None:
        """Record an ambiguity the stage refused to resolve.

        Deduplicated: the same ambiguity reached twice (the pipeline is
        idempotent and may be re-run on its own output) is still one fact.
        """
        if message not in self.warnings:
            self.warnings.append(message)

    @property
    def rules_applied(self) -> list[str]:
        return [rule for rule, _, _ in self.changes]

    def to_dict(self) -> dict:
        return {
            "rules_applied": self.rules_applied,
            "change_count": len(self.changes),
            "warnings": list(self.warnings),
        }


# ------------------------------------------------------- Persian numerals

#: Spoken Persian cardinals. Deliberately NOT a medical dictionary concern
#: (numbers must not live in the Aho-Corasick term list, see the dictionary
#: audit): they are pure language, handled here by explicit rules.
_UNITS = {
    "صفر": 0, "یک": 1, "دو": 2, "سه": 3, "چهار": 4,
    "پنج": 5, "شش": 6, "شیش": 6, "هفت": 7, "هشت": 8, "نه": 9,
}
_TEENS = {
    "ده": 10, "یازده": 11, "دوازده": 12, "سیزده": 13, "چهارده": 14,
    "پانزده": 15, "پونزده": 15, "شانزده": 16, "شونزده": 16,
    "هفده": 17, "هیفده": 17, "هجده": 18, "هیجده": 18, "نوزده": 19,
}
_TENS = {
    "بیست": 20, "سی": 30, "چهل": 40, "پنجاه": 50,
    "شصت": 60, "هفتاد": 70, "هشتاد": 80, "نود": 90,
}
_HUNDREDS = {
    "صد": 100, "یکصد": 100, "دویست": 200, "سیصد": 300, "چهارصد": 400,
    "پانصد": 500, "پونصد": 500, "ششصد": 600, "شیشصد": 600,
    "هفتصد": 700, "هشتصد": 800, "نهصد": 900,
}
_SCALES = {"هزار": 1000, "میلیون": 1000000}

_NUMBER_WORDS: dict[str, int] = {}
_NUMBER_WORDS.update(_UNITS)
_NUMBER_WORDS.update(_TEENS)
_NUMBER_WORDS.update(_TENS)
_NUMBER_WORDS.update(_HUNDREDS)

#: Words that are ONLY safe to convert as part of a multi-word number
#: ("سی و پنج"), or when an explicit numeric context follows. On their own
#: they are ordinary Persian words far more often than they are numbers:
#:   یک  = "a/an"        نه  = "no/not"      شش/شیش = also a noun
#:   سی  = also the ASR form of "CT" (سی تی)  ده = also "village"
_UNSAFE_ALONE = frozenset({"یک", "نه", "سی", "ده", "شش", "شیش", "صد", "دو"})

#: Counter/measure words that make a preceding bare cardinal unambiguous.
#: "سی ساله" is an age, "ده درصد" is a percentage - never "CT" or "village".
_NUMERIC_CONTEXT_AFTER = frozenset({
    "ساله", "ساله\u200cای", "سالگی", "روزه", "ماهه", "هفته", "بار", "مرتبه",
    "درصد", "دقیقه", "ثانیه", "ساعته", "میلی", "میکرو", "سانتی",
    "گرم", "لیتر", "متر", "کیلو", "واحد", "قطره", "عدد", "نفر", "نمره",
    "امتیاز", "درجه", "سی", "cc", "mg", "ml", "mL", "kg", "g", "L",
})

#: Words that make a FOLLOWING bare cardinal unambiguous ("ساعت ده").
_NUMERIC_CONTEXT_BEFORE = frozenset({
    "ساعت", "نمره", "امتیاز", "شماره", "اتاق", "تخت", "درجه", "حدود",
    "تقریبا", "تقریباً", "روز", "دوز", "تعداد", "سن", "بخش",
})

_PERSIAN_WORD_RE = re.compile(r"[\u0600-\u06ff\u200c]+")


def _tokenize_keep_space(text: str) -> list[str]:
    """Split into tokens and the exact separators between them.

    Returns an alternating list ``[sep, token, sep, token, ..., sep]`` so
    joining it reproduces the input byte for byte. Rewriting a token never
    disturbs surrounding spacing or punctuation.
    """
    parts: list[str] = []
    last = 0
    for match in _PERSIAN_WORD_RE.finditer(text):
        parts.append(text[last:match.start()])
        parts.append(match.group())
        last = match.end()
    parts.append(text[last:])
    return parts


#: Magnitude class of each cardinal word, used to reject malformed runs.
#: A well-formed Persian cardinal names each class at most once and in
#: strictly descending order: hundreds, then tens, then units/teens.
_CLASS_HUNDREDS, _CLASS_TENS, _CLASS_UNITS = 3, 2, 1


def _magnitude_class(word: str) -> int:
    if word in _HUNDREDS:
        return _CLASS_HUNDREDS
    if word in _TENS:
        return _CLASS_TENS
    return _CLASS_UNITS  # units and teens share the lowest rank


def _parse_number_words(words: list[str]) -> Optional[int]:
    """Parse a whole Persian cardinal phrase; ``None`` when it is not one.

    Handles the standard additive/multiplicative structure:
    ``سه هزار و دویست و سی و پنج`` -> 3235.

    The phrase must be WELL FORMED. Persian names each magnitude class at
    most once, in descending order (hundreds, tens, units), so ``سی و شش``
    is 36 but ``سی و شش و هفت`` is not a number at all - it is how a nurse
    dictates the decimal 36.7, or two separate values the ASR ran together.

    Rejecting it is a safety requirement, not a nicety. Summing it blindly
    produced ``43`` for a spoken body temperature of 36.7 C: a fabricated
    clinical value, invented by the postprocessor, that no warning flagged.
    Malformed runs are left verbatim for a human to read instead.
    """
    if not words:
        return None
    total = 0
    current = 0
    seen_value = False
    last_class: Optional[int] = None
    for word in words:
        if word == "و":
            continue
        if word in _SCALES:
            scale = _SCALES[word]
            # "هزار" with no preceding multiplier means 1 x 1000.
            current = (current or 1) * scale
            total += current
            current = 0
            seen_value = True
            last_class = None  # a new group starts after a scale word
            continue
        value = _NUMBER_WORDS.get(word)
        if value is None:
            return None
        rank = _magnitude_class(word)
        if last_class is not None and rank >= last_class:
            # Same or rising magnitude: not one cardinal phrase.
            return None
        last_class = rank
        current += value
        seen_value = True
    if not seen_value:
        return None
    return total + current


def persian_number_words_to_digits(
    text: str, report: Optional[PolishReport] = None
) -> str:
    """Convert spoken Persian cardinals to ASCII digits, conservatively.

    A run of number words is converted when EITHER
      * it is a genuine multi-word cardinal (``سی و پنج``, ``صد و بیست``), or
      * it is a single cardinal that is unambiguous on its own
        (``بیست``, ``چهل``, ``هفده`` - none of which are ordinary words), or
      * it is a single ambiguous cardinal (``یک``/``ده``/``سی``/``نه``/``دو``)
        standing in an explicit numeric context: preceded by a word such as
        ``ساعت``/``نمره``, or followed by a counter such as
        ``ساله``/``درصد``/``دقیقه``.

    Everything else is left untouched. ``یک بیمار`` ("a patient") and the
    ASR form ``سی تی`` ("CT") are therefore never turned into ``1 بیمار``
    or ``30 تی``.
    """
    parts = _tokenize_keep_space(text)
    # Token positions inside ``parts`` are the odd indices.
    token_indices = list(range(1, len(parts), 2))
    if not token_indices:
        return text

    out = list(parts)
    i = 0
    while i < len(token_indices):
        idx = token_indices[i]
        word = parts[idx]
        if word not in _NUMBER_WORDS and word not in _SCALES:
            i += 1
            continue

        # Extend the run. Persian cardinals are written with an explicit
        # "و" between components ("سی و پنج"), EXCEPT before a scale word
        # ("سه هزار"). Two bare cardinals side by side ("ده سی") are NOT a
        # single number - treating them as one used to fabricate the value
        # 40 out of what is really an ambiguous spoken time.
        j = i
        last_numeric = i
        while j + 1 < len(token_indices):
            if parts[token_indices[j] + 1].strip() != "":
                break  # punctuation between: not one number phrase
            nxt = parts[token_indices[j + 1]]
            if nxt in _SCALES:
                j += 1
                last_numeric = j
                continue
            if nxt == "و":
                if (j + 2 < len(token_indices)
                        and (parts[token_indices[j + 2]] in _NUMBER_WORDS
                             or parts[token_indices[j + 2]] in _SCALES)
                        and parts[token_indices[j + 1] + 1].strip() == ""):
                    j += 2
                    last_numeric = j
                    continue
                break
            break

        span = [parts[token_indices[k]] for k in range(i, last_numeric + 1)]
        value = _parse_number_words(span)

        numeric_words = [w for w in span if w != "و"]
        if value is None or not numeric_words:
            if value is None and len(numeric_words) > 1:
                # A malformed multi-word run (e.g. "سی و شش و هفت", which is
                # how 36.7 or two separate values get dictated). Skip the
                # WHOLE run - converting only its tail would emit a
                # half-digitised "سی و شش و 7" - and say so, because a human
                # has to decide what was meant. Never guess a clinical value.
                if report is not None:
                    report.warn(
                        "ambiguous spoken number left unchanged: "
                        f"{' '.join(span)!r} is not a well-formed cardinal "
                        "(it may be a decimal or two separate values)"
                    )
                i = last_numeric + 1
                continue
            i += 1
            continue

        if len(numeric_words) == 1 and numeric_words[0] in _UNSAFE_ALONE:
            before_token = (
                parts[token_indices[i - 1]] if i > 0 else ""
            )
            after_token = (
                parts[token_indices[last_numeric + 1]]
                if last_numeric + 1 < len(token_indices) else ""
            )
            if (before_token not in _NUMERIC_CONTEXT_BEFORE
                    and after_token not in _NUMERIC_CONTEXT_AFTER):
                i = last_numeric + 1
                continue

        out[token_indices[i]] = str(value)
        for k in range(i + 1, last_numeric + 1):
            out[token_indices[k]] = ""
            out[token_indices[k] - 1] = ""
        i = last_numeric + 1

    result = "".join(out)
    result = re.sub(r"[ \t]{2,}", " ", result)
    if report is not None:
        report.record("persian_number_words", text, result)
    return result


# -------------------------------------------------------------- clock time

_HALF = "نیم"
_QUARTER = "ربع"

#: Hour/minute values spoken as words, resolved BEFORE the generic cardinal
#: pass. Without this ordering "ساعت ده و سی دقیقه" would first collapse into
#: the single cardinal 40 ("ده و سی" = 10 + 30) and the clock reading would
#: be lost - the exact bug this pass exists to prevent.
_TIME_LEAD = "ساعت"
_MINUTE_WORD = "دقیقه"


def _spoken_run_value(words: list[str]) -> Optional[int]:
    """Value of a pure number-word run, or ``None`` if it is not one."""
    if not words or any(
        w not in _NUMBER_WORDS and w not in _SCALES and w != "و" for w in words
    ):
        return None
    return _parse_number_words(words)


def _normalize_spoken_times(
    text: str, report: Optional[PolishReport] = None
) -> str:
    """Persian spoken clock expressions -> ``HH:MM``, before cardinals run.

    Recognized shapes (``ساعت`` optional unless noted)::

        ساعت ده و سی دقیقه   ->  ساعت 10:30
        ده و سی دقیقه        ->  10:30
        ده و نیم             ->  10:30
        ده و ربع             ->  10:15
        ساعت ده سی           ->  ساعت 10:30      (no "و": needs the lead)

    A phrase only fires when it is unambiguously a time: either ``دقیقه``
    follows, or the fraction word ``نیم``/``ربع`` is used, or the explicit
    ``ساعت`` lead is present. ``سی و پنج ساله`` (an age) therefore never
    becomes a clock value.
    """
    original = text
    parts = _tokenize_keep_space(text)
    token_indices = list(range(1, len(parts), 2))

    def tok(k: int) -> str:
        return parts[token_indices[k]] if 0 <= k < len(token_indices) else ""

    def clean_sep(k: int) -> bool:
        """True when only whitespace separates token k and k+1."""
        if not (0 <= k < len(token_indices) - 1):
            return False
        return parts[token_indices[k] + 1].strip() == ""

    out = list(parts)
    i = 0
    while i < len(token_indices):
        lead = tok(i) == _TIME_LEAD and clean_sep(i)
        start = i + 1 if lead else i

        # --- hour: a one- or two-word cardinal run
        hour = _spoken_run_value([tok(start)])
        hour_end = start
        if hour is None:
            i += 1
            continue
        if (clean_sep(start) and tok(start + 1) == "و" and clean_sep(start + 1)
                and _spoken_run_value([tok(start + 2)]) is not None
                and tok(start + 3) not in (_MINUTE_WORD,)):
            # "بیست و یک" style compound hour, only when no minute follows it
            combined = _spoken_run_value([tok(start), "و", tok(start + 2)])
            if combined is not None and combined <= 23 and (
                tok(start + 3) == _MINUTE_WORD or tok(start + 3) == ""
            ):
                pass  # ambiguous, handled by the minute branch below
        if hour > 23:
            i += 1
            continue

        matched_end = None
        minute = None

        # --- "<hour> و نیم|ربع"
        if (clean_sep(hour_end) and tok(hour_end + 1) == "و"
                and clean_sep(hour_end + 1)
                and tok(hour_end + 2) in (_HALF, _QUARTER)):
            minute = 30 if tok(hour_end + 2) == _HALF else 15
            matched_end = hour_end + 2

        # --- "<hour> و <minute> [دقیقه]"
        elif (clean_sep(hour_end) and tok(hour_end + 1) == "و"
                and clean_sep(hour_end + 1)):
            m_words = [tok(hour_end + 2)]
            m_end = hour_end + 2
            if (clean_sep(m_end) and tok(m_end + 1) == "و"
                    and clean_sep(m_end + 1)
                    and _spoken_run_value([tok(m_end + 2)]) is not None):
                m_words += ["و", tok(m_end + 2)]
                m_end += 2
            value = _spoken_run_value(m_words)
            has_minute_word = clean_sep(m_end) and tok(m_end + 1) == _MINUTE_WORD
            if value is not None and 0 <= value <= 59 and has_minute_word:
                minute = value
                matched_end = m_end + 1

        # --- "ساعت <hour> <minute>"  (no "و", requires the explicit lead)
        elif lead and clean_sep(hour_end):
            value = _spoken_run_value([tok(hour_end + 1)])
            if value is not None and 0 <= value <= 59 and value >= 10:
                minute = value
                matched_end = hour_end + 1

        if minute is None or matched_end is None:
            i += 1
            continue

        out[token_indices[start]] = f"{hour:02d}:{minute:02d}"
        for k in range(start + 1, matched_end + 1):
            out[token_indices[k]] = ""
            out[token_indices[k] - 1] = ""
        i = matched_end + 1

    text = "".join(out)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if report is not None:
        report.record("spoken_clock_time", original, text)
    return text

#: ``ساعت 10 و 30 دقیقه`` / ``ساعت 10 و نیم`` / ``10:30`` / ``ساعت ده سی``.
#: Digits only - ``persian_number_words_to_digits`` runs first.
_TIME_HOUR_MINUTE = re.compile(
    r"(?P<lead>ساعت\s+)?"
    r"(?P<hour>\b(?:[01]?\d|2[0-3]))"
    r"\s*و\s*"
    r"(?P<minute>[0-5]?\d)"
    r"\s*دقیقه"
)
_TIME_HALF = re.compile(
    r"(?P<lead>ساعت\s+)?"
    r"(?P<hour>\b(?:[01]?\d|2[0-3]))"
    rf"\s*و\s*(?P<frac>{_HALF}|{_QUARTER})"
)
_TIME_BARE = re.compile(
    r"(?P<lead>ساعت\s+)"
    r"(?P<hour>\b(?:[01]?\d|2[0-3]))"
    r"\s+(?P<minute>[0-5]\d)\b(?!\s*[:./])"
)
_TIME_COLON = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")

#: Numeral immediately AFTER a normalized clock time, e.g. the benchmark's
#: ``10 و 30 دقیقه 90``. It is intentionally left in the text; the warning
#: makes the unbound value visible instead of silently dropping it.
_TRAILING_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)?)")


#: Spoken separators between the two halves of a blood pressure reading.
#: Persian nurses dictate "صد و چهل روی هشتاد و پنج"; English speakers say
#: "140 over 85". Both mean the charted ratio 140/85.
_RATIO_WORDS = ("روی", "بر", "over")

_SPOKEN_RATIO_RE = re.compile(
    r"(?<![\d/])(\d{1,3})\s+(?:" + "|".join(_RATIO_WORDS) + r")\s+(\d{1,3})"
    r"(?![\d/])"
)


def normalize_spoken_ratios(
    text: str, report: Optional[PolishReport] = None
) -> str:
    """Join a spoken blood-pressure pair into the charted ``140/85`` form.

    Only a numeral-separator-numeral pattern is rewritten, and only when
    both sides are 1-3 digit integers that are not already part of a
    ratio. The separator words are otherwise ordinary Persian prepositions
    ("روی" = "on"), so requiring numerals on BOTH sides is what keeps
    "پانسمان روی زخم" ("dressing on the wound") untouched.

    Purely presentational: both numbers are preserved exactly: no value is
    created, dropped or rounded.
    """
    if not text:
        return text or ""
    original = text
    result = _SPOKEN_RATIO_RE.sub(r"\1/\2", text)
    if report is not None:
        report.record("spoken_ratio", original, result)
    return result


def normalize_clock_times(
    text: str, report: Optional[PolishReport] = None
) -> str:
    """Normalize spoken clock expressions to a single ``HH:MM`` form.

    Deterministic mapping::

        ساعت ده و سی دقیقه   ->  ساعت 10:30      (after word->digit)
        ساعت 10 و 30 دقیقه   ->  ساعت 10:30
        ده و نیم             ->  10:30
        ده و ربع             ->  10:15
        ساعت ده سی           ->  ساعت 10:30
        10:30                ->  10:30           (already canonical)

    A numeral that merely FOLLOWS a clock expression is never absorbed into
    it: ``10 و 30 دقیقه 90`` becomes ``10:30 90`` and raises a warning, so
    the ``90`` is neither deleted nor silently merged into the time.
    """
    original = text

    def _emit(lead: str, hour: int, minute: int) -> str:
        return f"{lead or ''}{hour:02d}:{minute:02d}"

    def _hm(match: re.Match) -> str:
        return _emit(match.group("lead"), int(match.group("hour")),
                     int(match.group("minute")))

    def _half(match: re.Match) -> str:
        minute = 30 if match.group("frac") == _HALF else 15
        return _emit(match.group("lead"), int(match.group("hour")), minute)

    text = _TIME_HOUR_MINUTE.sub(_hm, text)
    text = _TIME_HALF.sub(_half, text)
    text = _TIME_BARE.sub(_hm, text)
    # Canonical zero padding for an already-numeric "9:05"/"9:5" form.
    text = _TIME_COLON.sub(lambda m: f"{int(m.group(1)):02d}:{m.group(2)}", text)

    if report is not None:
        for match in _TIME_COLON.finditer(text):
            tail = text[match.end():]
            extra = _TRAILING_NUMBER.match(tail)
            if extra:
                report.warn(
                    f"unbound numeral {extra.group(1)!r} directly follows the "
                    f"normalized time {match.group(0)!r}; it was preserved "
                    f"verbatim and NOT absorbed into the time"
                )
        report.record("clock_time", original, text)
    return text


# ------------------------------------------------------------------ units

#: Canonical spacing/spelling for the units nursing documentation uses.
#: The Persian spoken forms are canonicalized by the medical dictionary
#: (``میلی متر جیوه`` -> ``mmHg``); this pass only fixes SPACING and the
#: handful of ASCII spellings ASR produces for an already-Latin unit.
_UNIT_SPELLING = {
    "mmhg": "mmHg", "mm hg": "mmHg",
    "ml": "mL",
    "mcg": "mcg", "ug": "mcg",
    "bpm": "bpm",
    "meq": "mEq",
}

#: ``36.7 °C`` / ``97 %`` / ``140/85 mmHg`` - one ASCII space before a
#: letter unit, and NO space before "%" (the typographic convention used in
#: Persian clinical charts and in the project's own examples).
_NUMBER_UNIT = re.compile(
    r"(?<![\w.])(\d+(?:[./]\d+)*)\s*"
    r"(mmHg|mmhg|°C|°F|bpm|mEq|meq|mcg|mL|ml|mg|kg|cm|mm|Fr|L|g|%)"
    r"(?![\w])"
)
_DEGREE_SPACING = re.compile(r"(\d)\s*°\s*([CF])\b")


def normalize_units(text: str, report: Optional[PolishReport] = None) -> str:
    """Canonical number/unit spacing and unit spelling."""
    original = text

    def _unit(match: re.Match) -> str:
        value, unit = match.group(1), match.group(2)
        unit = _UNIT_SPELLING.get(unit.lower(), unit)
        if unit == "%":
            return f"{value}%"
        return f"{value} {unit}"

    text = _DEGREE_SPACING.sub(r"\1 °\2", text)
    text = _NUMBER_UNIT.sub(_unit, text)
    # "میلی متر جیوه" that survived canonicalization (e.g. matcher disabled).
    text = re.sub(r"\bmm\s+Hg\b", "mmHg", text)
    if report is not None:
        report.record("units", original, text)
    return text


# --------------------------------------------------------- vital-sign labels

#: Vital-sign labels that take ``LABEL: value`` in charted documentation.
#: Restricted to canonical labels the medical dictionary already produces,
#: so this pass never invents a clinical label of its own.
_VITAL_LABELS = (
    "BP", "HR", "PR", "RR", "SpO2", "Temp", "GCS", "BG", "FBS", "BS",
    "O2 sat", "pain score",
)

#: Spelled-out vital-sign names -> their charted abbreviation. Applied ONLY
#: when the name is immediately followed by its numeric value, i.e. in an
#: unambiguous charting position. A prose mention ("the patient's blood
#: pressure was reviewed", "oxygen saturation monitoring") has no number
#: after it and is therefore never abbreviated. This deliberately lives
#: here rather than in the dictionary: as a GLOBAL lexical rule
#: "blood pressure -> BP" would rewrite ordinary clinical prose, which the
#: matcher-restriction rule forbids.
_SPELLED_VITAL_LABELS = {
    "blood pressure": "BP",
    "heart rate": "HR",
    "pulse rate": "PR",
    "respiratory rate": "RR",
    "oxygen saturation": "SpO2",
    "temperature": "Temp",
    "blood glucose": "BG",
    "blood sugar": "BS",
}

#: ``Temp . 36.7`` / ``blood pressure. 140/85`` / ``SpO2 و 97%`` ...
#: The separator alternatives are exactly the artifacts observed in the
#: Speechmatics nursing transcripts: a stray period, a comma, an equals
#: sign, a Persian "و" ("and"), or nothing at all.
_ALL_VITAL_LABELS = tuple(_VITAL_LABELS) + tuple(_SPELLED_VITAL_LABELS)
_VITAL_SEPARATOR = re.compile(
    r"(?<![\w])(?P<label>%s)"
    r"(?P<sep>\s*(?:[.:،,=]\s*)?(?:و\s+)?(?:برابر\s+(?:با\s+)?)?)"
    r"(?=(?P<value>\d))"
    % "|".join(
        re.escape(label)
        for label in sorted(_ALL_VITAL_LABELS, key=len, reverse=True)
    ),
    re.IGNORECASE,
)
#: A stray period glued to a decimal value: "36.7." -> "36.7".
_TRAILING_VALUE_DOT = re.compile(r"(?<=\d)\.(?=\s*(?:°|%|[A-Za-z\u0600-\u06ff]))")


def format_vital_signs(
    text: str, report: Optional[PolishReport] = None
) -> str:
    """Give charted vital signs the standard ``LABEL: value`` punctuation.

    ``Temp . 36.7. °C``          -> ``Temp: 36.7 °C``
    ``blood pressure. 140/85``   -> ``BP: 140/85``  (label via the matcher)
    ``oxygen saturation و 97%``  -> ``SpO2: 97%``   (label via the matcher)

    Only the SEPARATOR between a recognized label and its numeric value is
    rewritten. The label itself and the value are never changed, so no
    clinical content can be fabricated here.
    """
    original = text

    def _label(match: re.Match) -> str:
        raw = match.group("label")
        canonical = _SPELLED_VITAL_LABELS.get(raw.lower())
        if canonical is None:
            # Already an abbreviation: keep its exact charted casing.
            canonical = next(
                (lab for lab in _VITAL_LABELS if lab.lower() == raw.lower()),
                raw,
            )
        return f"{canonical}: "

    text = _VITAL_SEPARATOR.sub(_label, text)
    text = _TRAILING_VALUE_DOT.sub("", text)
    if report is not None:
        report.record("vital_sign_labels", original, text)
    return text


# ---------------------------------------------------------- repetitions

#: ``نمره نمره`` / ``the the`` - an identical adjacent token repeated.
_REPEATED_WORD = re.compile(
    r"\b(?P<word>[A-Za-z\u0600-\u06ff\u200c]{2,})"
    r"(?P<gap>[ \t]+)"
    r"(?P=word)\b",
    re.IGNORECASE,
)
#: ``می باشد. باشد.`` / ``ذکر می کند. کنند`` - a sentence tail restated after
#: a stray full stop. Only fires when the restated fragment is a short
#: SUFFIX of what came immediately before, which is the exact ASR stutter
#: shape; an ordinary repeated clinical statement is never touched.
_ECHOED_TAIL = re.compile(
    r"(?P<head>[A-Za-z\u0600-\u06ff\u200c]{2,})"
    r"\s*[.،,]\s*"
    r"(?P<tail>[A-Za-z\u0600-\u06ff\u200c]{2,})"
    r"(?=[\s.،,؛!?]|$)"
)

#: Persian verb stems whose inflected echo counts as the same word for the
#: stutter rule: "باشد"/"باشند", "کند"/"کنند", "شد"/"شدند", "دارد"/"دارند".
_INFLECTION_PAIRS = (
    ("باشد", "باشند"), ("کند", "کنند"), ("شود", "شوند"),
    ("دارد", "دارند"), ("شد", "شدند"), ("است", "هست"),
    ("گردد", "گردند"), ("یابد", "یابند"),
)


def _same_stem(head: str, tail: str) -> bool:
    if head == tail:
        return True
    for a, b in _INFLECTION_PAIRS:
        if {head, tail} == {a, b}:
            return True
    return False


def collapse_repetitions(
    text: str,
    report: Optional[PolishReport] = None,
    protected: Optional[frozenset] = None,
) -> str:
    """Remove ASR stutter: duplicated words and echoed sentence tails.

    ``نمره نمره``            -> ``نمره``
    ``می باشد. باشد.``       -> ``می باشد.``  (ZWNJ applied by a later rule)
    ``ذکر می کند. کنند``     -> ``ذکر می کند``
    ``blood pressure pressure`` -> ``blood pressure``

    Only ADJACENT repetition is collapsed, and only when both occurrences
    are the identical token (or a known verb-inflection pair). A genuinely
    repeated clinical value such as ``140/85 140/85`` is numeric and is
    therefore never matched by these word patterns.

    ``protected`` is a set of case-folded strings that must never be
    de-duplicated. Persian spells several abbreviations with a real
    repeated syllable (``سی سی یو`` = CCU, ``آر آر`` = RR), and collapsing
    those destroys the term; the caller passes the matcher's rule forms so
    the rule can recognise and skip them.
    """
    original = text
    guard = protected or frozenset()

    def _dedupe(match: re.Match) -> str:
        word = match.group("word")
        whole = match.group(0)
        if whole.casefold() in guard:
            return whole  # "سی سی" is the start of the CCU form: keep it
        # Also keep it when the repetition plus the NEXT token spells a rule
        # form ("سی سی یو"): the doubled syllable is part of the term.
        tail = match.string[match.end():]
        next_word = tail.split()[0] if tail.split() else ""
        if next_word and f"{whole} {next_word}".casefold() in guard:
            return whole
        return word

    previous = None
    # Repeated application handles a triple ("نمره نمره نمره").
    while previous != text:
        previous = text
        text = _REPEATED_WORD.sub(_dedupe, text)

    if protected is not None:
        # Pre-canonicalization mode: only the identical-word rule is safe;
        # echoed verb tails are handled after the matcher has run.
        if report is not None:
            report.record("repetitions_prepolish", original, text)
        return text

    def _tail(match: re.Match) -> str:
        head, tail = match.group("head"), match.group("tail")
        if _same_stem(head, tail):
            return head
        return match.group(0)

    previous = None
    while previous != text:
        previous = text
        text = _ECHOED_TAIL.sub(_tail, text)

    if report is not None:
        report.record("repetitions", original, text)
    return text


# ------------------------------------------------------------ punctuation

#: Persian verb prefixes that take a ZWNJ rather than a space.
_ZWNJ_PREFIX = re.compile(r"\b(ن?می)\s+(?=[\u0600-\u06ff])")
#: ``ها``/``تر``/``ترین`` suffixes after a Persian word.
_ZWNJ_SUFFIX = re.compile(r"(?<=[\u0600-\u06ff])\s+(ها|های|هایی|تر|ترین)\b")

_REPEATED_PUNCT = re.compile(r"([.،؛,;:!?])\s*(?:\1\s*)+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([.،؛,;:!?%؟])")
_SPACE_AFTER_PUNCT = re.compile(r"([.،؛,;:!?؟])(?=[^\s\d.،؛,;:!?؟)\]])")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
#: A period wedged between a Latin label and its value, "Temp . 36" - the
#: vital-sign pass handles known labels; this catches the generic shape.
_ORPHAN_PERIOD = re.compile(r"(?<=[A-Za-z\u0600-\u06ff])\s+\.\s+")


def normalize_punctuation(
    text: str, report: Optional[PolishReport] = None
) -> str:
    """Whitespace, repeated punctuation and Persian ZWNJ typography."""
    original = text
    text = _ORPHAN_PERIOD.sub(". ", text)
    text = _REPEATED_PUNCT.sub(r"\1 ", text)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = _SPACE_AFTER_PUNCT.sub(r"\1 ", text)
    text = _ZWNJ_PREFIX.sub(rf"\1{ZWNJ}", text)
    text = _ZWNJ_SUFFIX.sub(rf"{ZWNJ}\1", text)
    text = _MULTI_SPACE.sub(" ", text)
    text = text.strip()
    if report is not None:
        report.record("punctuation", original, text)
    return text


# ---------------------------------------------------------------- pipeline


def prepolish_asr_artifacts(
    text: str,
    report: Optional[PolishReport] = None,
    protected: Optional[frozenset] = None,
) -> str:
    """Remove ASR stutter BEFORE the medical matcher runs.

    Duplicated-word cleanup has to happen on the pre-canonicalization text,
    because a stutter can otherwise fabricate a phrase the speaker never
    said. Real example caught by the benchmark (``gram_repeated_word``)::

        spoken     نمره نمره درد ثبت شد        ("score score pain was recorded")
        matcher    نمره [نمره درد -> pain score] ثبت شد
        result     نمره pain score ثبت شد      <-- a term nobody uttered

    The second ``نمره`` plus the following ``درد`` happen to spell the
    dictionary phrase ``نمره درد`` ("pain score"), so the stutter *created*
    a medical hit. Collapsing the repetition first yields ``نمره درد ثبت شد``
    and the intended single canonicalization.

    The rule is deliberately NARROWER than ``collapse_repetitions``:

    * only the identical-adjacent-word pattern is applied (echoed sentence
      tails are a post-canonicalization concern);
    * a repeated token is only collapsed when the repetition is NOT itself
      part of a dictionary form. Persian spells several abbreviations with
      a genuine repeated syllable - ``سی سی یو`` ("CCU"), ``آر آر`` ("RR"),
      ``تی تی`` ("TT") - and blindly de-duplicating them destroyed the term
      (``سی سی یو`` -> ``سی یو``). ``protected`` receives those forms from
      the matcher so they are skipped.
    """
    if not text:
        return text or ""
    return collapse_repetitions(text, report, protected=protected)


# ------------------------------------------------- streaming boundaries

#: Hard bound on how much text the streaming accumulator may hold back while
#: waiting for the next final segment. Every construct this module can join
#: across a boundary is shorter than this (``ساعت بیست و سه و چهل و پنج`` is
#: the longest realistic one), so the buffer can never grow with the session.
MAX_PENDING_TAIL_TOKENS = 6

#: Bound on the cross-segment stutter window (``نمره`` + ``نمره درد``).
MAX_BOUNDARY_DUPLICATE_TOKENS = 2

_DIGITS_ONLY = re.compile(r"\d+(?:[.,/]\d+)*")

#: Persian verb prefixes that take a ZWNJ with the word AFTER them (see
#: ``_ZWNJ_PREFIX``): a segment ending on one is mid-word, not mid-sentence.
_VERB_PREFIXES = frozenset({"\u0645\u06cc", "\u0646\u0645\u06cc"})


def _is_number_token(token: str) -> bool:
    """True for a spoken cardinal, a scale word or an already-digit value."""
    return (
        token in _NUMBER_WORDS
        or token in _SCALES
        or bool(_DIGITS_ONLY.fullmatch(token))
    )


def _number_run_start(tokens: list[str], end: int) -> int:
    """Index where the number run that ENDS at ``tokens[end]`` begins.

    A run is a chain of cardinal/digit tokens, optionally joined by the
    Persian connector ``و`` (``صد و چهل``, ``سه هزار و دویست``).
    """
    start = end
    while start - 1 >= 0:
        previous = tokens[start - 1]
        if _is_number_token(previous):
            start -= 1
        elif (previous == "و" and start - 2 >= 0
                and _is_number_token(tokens[start - 2])):
            start -= 2
        else:
            break
    return start


#: Leading token -> full token count, for the multi-word spelled labels.
_VITAL_LABEL_HEADS: dict[str, int] = {}
for _label in _ALL_VITAL_LABELS:
    _label_tokens = _label.casefold().split()
    if len(_label_tokens) > 1:
        _VITAL_LABEL_HEADS[_label_tokens[0]] = max(
            _VITAL_LABEL_HEADS.get(_label_tokens[0], 0), len(_label_tokens)
        )


def vital_label_prefix_tokens(text: str) -> int:
    """Trailing tokens of ``text`` that begin, but do not finish, a label.

    ``blood pressure`` / ``oxygen saturation`` / ``heart rate`` are spelled
    labels ``format_vital_signs`` rewrites, and they are not medical-matcher
    rules, so nothing else stops a final segment from ending on ``blood``.
    Returns 0 when the tail is not a partial label.
    """
    # A trailing ASR artifact ("blood pressure." before the value) is part
    # of the separator the vital-sign pass rewrites, not of the label.
    tokens = [token.rstrip(".:،,= ").casefold()
              for token in (text or "").split()]
    for size in range(1, min(len(tokens), 3) + 1):
        tail = tokens[len(tokens) - size:]
        full = _VITAL_LABEL_HEADS.get(tail[0])
        if full is None or size >= full:
            continue
        phrase = " ".join(tail)
        if any(label.casefold().startswith(phrase + " ")
               for label in _ALL_VITAL_LABELS):
            return size
    return 0


def is_vital_label(text: str) -> bool:
    """True when ``text`` is exactly a vital-sign label ``format_vital_signs``
    punctuates (``BP``, ``SpO2``, ``pain score``, ``oxygen saturation``, ...).

    The streaming accumulator uses this to keep a label and its value in one
    emission: ``LABEL: value`` can only be applied when both halves are in
    the same text, so ``oxygen saturation`` emitted apart from ``97 درصد``
    would never become ``SpO2: 97%``.
    """
    # A trailing ASR artifact ("heart rate." before its value) belongs to the
    # separator the vital-sign pass rewrites, not to the label itself.
    phrase = " ".join((text or "").split()).rstrip(".:،,= ").casefold()
    if not phrase:
        return False
    return any(phrase == label.casefold() for label in _ALL_VITAL_LABELS)


def number_run_start(tokens: list[str]) -> Optional[int]:
    """Index where a trailing cardinal/digit run starts, else ``None``.

    Exposed for the streaming accumulator: a value at the end of a segment
    may still need the label in FRONT of it (which the matcher has to
    canonicalize first) to be formatted correctly.
    """
    if not tokens or not _is_number_token(tokens[-1]):
        return None
    return _number_run_start(tokens, len(tokens) - 1)


def pending_tail_tokens(text: str) -> int:
    """How many trailing tokens of ``text`` a NEXT segment may still complete.

    Speechmatics finalizes on its own timing, so a single spoken construct
    can be split across two final segments (``سی و`` + ``پنج``). Emitting the
    first half immediately makes the normalization impossible: ``سی و`` stays
    verbatim and ``پنج`` becomes a lone ``5``.

    Only a *demonstrably incomplete* trailing window is held - never a merely
    "could still grow" one - so ordinary dictation is not delayed:

    * a cardinal run left hanging on the connector ``و``   (``سی و``);
    * a ratio separator with only its left value           (``صد و چهل روی``);
    * a bare clock lead, or a clock lead plus its hour
      (``ساعت``, ``ساعت ده`` + ``و سی دقیقه`` -> ``ساعت 10:30``).

    A finished construct such as ``140/85`` or ``Temp 36`` returns 0 and is
    emitted at once. The result is always ``<= MAX_PENDING_TAIL_TOKENS``
    (0 when a longer window would be needed - give up rather than buffer
    unboundedly), which is what keeps the streaming buffer small.
    """
    tokens = (text or "").split()
    if not tokens:
        return 0
    count = len(tokens)
    last = tokens[-1]

    if last == _TIME_LEAD:
        return 1
    if last in _VERB_PREFIXES:
        return 1  # "می"/"نمی" binds by ZWNJ to the verb in the next segment
    partial_label = vital_label_prefix_tokens(text)
    if partial_label:
        return partial_label
    if (last == "و" or last in _RATIO_WORDS) and count >= 2 \
            and _is_number_token(tokens[-2]):
        start = _number_run_start(tokens, count - 2)
    elif _is_number_token(last):
        # A value at the very end is always still open: the next final may
        # carry its minutes ("ده" + "و نیم"), its unit ("97" + "درصد") or
        # the second half of a ratio.
        start = _number_run_start(tokens, count - 1)
    else:
        return 0

    if start > 0 and tokens[start - 1] == _TIME_LEAD:
        start -= 1
    elif start >= 2 and tokens[start - 1] in _RATIO_WORDS \
            and _is_number_token(tokens[start - 2]):
        # The left half of a ratio belongs to the same construct: cutting
        # between "140 روی" and "85" leaves the pair unjoinable.
        start = _number_run_start(tokens, start - 2)
    held = count - start
    return held if held <= MAX_PENDING_TAIL_TOKENS else 0


def _is_stutterable_token(token: str) -> bool:
    """Only word tokens stutter; a repeated numeric value is real data."""
    return len(token) >= 2 and not any(ch.isdigit() for ch in token)


def duplicate_boundary_tokens(
    previous: str, segment: str, protected: Optional[frozenset] = None
) -> int:
    """Leading tokens of ``segment`` that merely repeat the end of ``previous``.

    ``prepolish_asr_artifacts`` removes a stutter INSIDE one final segment,
    but the repetition can straddle the boundary::

        final 1   نمره
        final 2   نمره درد

    Neither segment looks duplicated on its own, yet the stream says
    ``نمره نمره درد`` and the second ``نمره`` combines with ``درد`` into the
    dictionary phrase ``نمره درد`` ("pain score") the speaker never said.

    Comparison is deterministic and exact (case-folded whole tokens, at most
    ``MAX_BOUNDARY_DUPLICATE_TOKENS``); there is no fuzzy or semantic
    matching. ``protected`` is the matcher's ``repetition_safe_forms``, so
    terms that legitimately repeat a syllable (``سی سی`` + ``یو`` = CCU) are
    never collapsed.
    """
    previous_tokens = (previous or "").split()
    segment_tokens = (segment or "").split()
    if not previous_tokens or not segment_tokens:
        return 0
    guard = protected or frozenset()

    seam = f"{previous_tokens[-1]} {segment_tokens[0]}".casefold()
    if seam in guard:
        return 0

    limit = min(
        MAX_BOUNDARY_DUPLICATE_TOKENS, len(previous_tokens), len(segment_tokens)
    )
    for size in range(limit, 0, -1):
        tail = previous_tokens[-size:]
        head = segment_tokens[:size]
        if not all(_is_stutterable_token(token) for token in head):
            continue
        if [t.casefold() for t in tail] != [h.casefold() for h in head]:
            continue
        if " ".join(tail + head).casefold() in guard:
            continue
        return size
    return 0


def polish_nursing_text(
    text: str, report: Optional[PolishReport] = None
) -> str:
    """Apply the full deterministic polish pass to ONE canonical segment.

    Idempotent: ``polish(polish(x)) == polish(x)`` for every fixture in the
    benchmark (asserted by the test suite). Sentence-final punctuation is
    deliberately NOT added here - see ``polish_document``.
    """
    if not text:
        return text or ""
    # Spoken times resolve FIRST: "ساعت ده و سی دقیقه" must become 10:30, not
    # the cardinal 40 that a generic "ده و سی" number pass would produce.
    text = _normalize_spoken_times(text, report)
    text = persian_number_words_to_digits(text, report)
    text = normalize_spoken_ratios(text, report)
    text = normalize_clock_times(text, report)
    text = format_vital_signs(text, report)
    text = normalize_units(text, report)
    text = collapse_repetitions(text, report)
    text = normalize_punctuation(text, report)
    return text


_SENTENCE_END = re.compile(r"[.!?،؛:؟]\s*$")


def polish_document(
    text: str, report: Optional[PolishReport] = None
) -> str:
    """Polish a COMPLETE transcript and close the final sentence.

    The only difference from ``polish_nursing_text`` is the terminal full
    stop, which is safe on a finished document but would be a fabrication
    on a mid-sentence streamed segment.
    """
    text = polish_nursing_text(text, report)
    if not text:
        return text
    if not _SENTENCE_END.search(text):
        after = text + "."
        if report is not None:
            report.record("sentence_final_punctuation", text, after)
        text = after
    return text


# ------------------------------------------------------ presentation only

_ASCII_TO_PERSIAN_DIGITS = str.maketrans(
    {str(i): chr(0x06F0 + i) for i in range(10)}
)


def to_persian_digits(text: str) -> str:
    """PRESENTATION helper: render ASCII digits as Persian digits.

    The canonical/logical transcript always uses ASCII digits (matching
    ``normalize_text``, which folds Persian digits to ASCII so that numeric
    comparison, the benchmark and the medical layer all agree on one
    representation). Persian-digit rendering is a display choice and is
    applied by the overlay only - never to canonical text, and never before
    injection into a downstream EMR field.
    """
    return (text or "").translate(_ASCII_TO_PERSIAN_DIGITS)
