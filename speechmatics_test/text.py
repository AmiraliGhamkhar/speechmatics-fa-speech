"""Generic text normalization and medical-aware tokenization.

This module is language/tooling plumbing only: it contains no medical
knowledge. It unifies Persian script, normalizes whitespace/ZWNJ, folds
Persian *spoken* numerals into ASCII digits where a unit/context word makes
the number a measured value, and offers a tokenizer that keeps medical values
intact (``20 mg``, ``120/80``, ``5.5``, ``O2``, ``q2h``, ``C3-C4``, ``U/A``,
routes, abbreviations).

Which contexts count as "a measured value" is deliberately NOT decided here:
the anchor words are supplied by the caller (see
``speechmatics_test.matcher.NUMERIC_CONTEXT``), so this module stays free of
domain vocabulary apart from the generic numeral lexicon.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

# Arabic -> Persian script unification + diacritic/hamza removal.
ARABIC_TO_PERSIAN = str.maketrans({
    "\u064a": "\u06cc", "\u0649": "\u06cc", "\u0643": "\u06a9",
    "\u0640": "", "\u064b": "", "\u064c": "", "\u064d": "",
    "\u064e": "", "\u064f": "", "\u0650": "", "\u0651": "", "\u0652": "",
})

#: Extended Arabic-Indic (Persian) U+06F0-06F9 and Arabic-Indic U+0660-0669
#: digits -> ASCII. Speechmatics returns Persian digits for Persian streams,
#: so a dose dictated as "20 mg" can come back as "۲۰ mg". Without folding
#: them the benchmark scored *correct* numbers as wrong (number_accuracy 0.0)
#: and inflated WER - the single most misleading metric for a dosage-critical
#: medical benchmark. This is generic script normalization, not medical
#: knowledge, so it belongs in this stage.
DIGITS_TO_ASCII = str.maketrans(
    {chr(0x06F0 + i): str(i) for i in range(10)}
    | {chr(0x0660 + i): str(i) for i in range(10)}
)

#: Arabic decimal separator / thousands separator used with Arabic-Indic digits.
_ARABIC_NUMERIC_PUNCT = str.maketrans({"\u066b": ".", "\u066c": ","})

#: Dotted meridiem charting forms ("A.M.", "a. m.", "P.M.") -> "AM"/"PM".
#: Only the INTERNAL dots are folded, so "at 8 A.M." becomes "at 8 AM." - the
#: stop is preserved (it doubles as the abbreviation dot), which keeps the
#: substitution idempotent and never deletes punctuation. Generic time
#: notation, not medical knowledge.
_MERIDIEM = re.compile(r"\b([AaPp])\.\s?([Mm])\b")

RTL_RE = re.compile(r"[\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff]")


def normalize_text(text: str) -> str:
    """Generic normalization: NFC, Persian script, digits, ZWNJ->space, spacing."""
    text = unicodedata.normalize("NFC", text or "")
    text = text.translate(ARABIC_TO_PERSIAN)
    text = text.replace("ي", "ی").replace("ك", "ک")
    text = text.translate(DIGITS_TO_ASCII)
    text = text.translate(_ARABIC_NUMERIC_PUNCT)
    text = re.sub(r"[ \t\u200c\u200d]+", " ", text).strip()
    text = re.sub(r"\s+([،؛؟,.!?])", r"\1", text)
    text = _MERIDIEM.sub(lambda match: match.group(1).upper() + "M", text)
    return text


def is_rtl(text: str) -> bool:
    for ch in (text or "").strip():
        if RTL_RE.match(ch):
            return True
        if ch.isascii() and ch.isalpha():
            return False
    return True


# ------------------------------------------------------------------ tokens

_WORD = r"[A-Za-z\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff]+"
_NUM = r"\d+(?:[.,]\d+)*"
# Atom: word with an optional numeric suffix and optional trailing letters:
#   mg | O2 | A1c | q2h
_ATOM = rf"{_WORD}(?:{_NUM})?(?:{_WORD})?"

# Token shapes (order matters):
#   1) numbers incl. decimals, comma decimals and BP-style ratios:
#        20 | 5.5 | 3,14 | 120/80
#   2) atoms optionally hyphen/slash joined:
#        mg | O2 | A1c | q2h | C3-C4 | U/A | right-lung
#   3) a single non-word, non-space character (punctuation, %, etc.)
# Whitespace is not a token (findall simply skips it).
_TOKEN = re.compile(
    rf"(?:{_NUM}(?:/{_NUM})*)"
    rf"|(?:{_ATOM}(?:[-/]{_ATOM})*)"
    r"|(?:[^\w\s])"
)


def tokens(text: str) -> list[str]:
    """Medical-aware tokenization for WER/number metrics on mixed text."""
    t = normalize_text(text)
    return _TOKEN.findall(t)


# --------------------------------------------------------- spoken numerals
#
# The word counterpart of ``DIGITS_TO_ASCII``: a dictated "25" and a dictated
# "بیست و پنج" must both end up as ``25``. This is a flat table plus one
# validation rule, NOT a number-language engine:
#
#   * hundreds are additive in Persian ("دویست و پنجاه" = 250), so a spoken
#     number is a run of these tokens whose values strictly descend, each pair
#     optionally joined by "و";
#   * anything that is not such a run (an increasing or repeated value, a
#     dangling "و", "هزار", a decimal) is left completely untouched;
#   * "یک" and "نه" are only folded inside a longer number: on their own they
#     are the ordinary words "a/one" and "no", so "یک ضایعه" and "نه ممنوع"
#     never become digits;
#   * even a valid number is only folded when an anchor word supplied by the
#     caller touches it, so arbitrary prose numerals stay natural.

#: Spoken numeral words and their values (zero to 999). ``هزار`` is NOT part of
#: this table: it is multiplicative (see ``SPOKEN_THOUSANDS``), so it belongs to
#: the run logic, not to the single-valued lexicon callers iterate over.
SPOKEN_NUMERALS: dict[str, int] = {
    "صفر": 0, "یک": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6,
    "هفت": 7, "هشت": 8, "نه": 9, "ده": 10, "یازده": 11, "دوازده": 12,
    "سیزده": 13, "چهارده": 14, "پانزده": 15, "شانزده": 16, "هفده": 17,
    "هجده": 18, "نوزده": 19,
    "بیست": 20, "سی": 30, "چهل": 40, "پنجاه": 50, "شصت": 60, "هفتاد": 70,
    "هشتاد": 80, "نود": 90,
    "صد": 100, "یکصد": 100, "صدو": 100, "دویست": 200, "سیصد": 300,
    "چهارصد": 400, "پانصد": 500, "ششصد": 600, "هفتصد": 700, "هشتصد": 800,
    "نهصد": 900,
}

#: The single word joining the parts of a Persian number.
SPOKEN_NUMERAL_JOINER = "و"

#: Spoken THOUSANDS. ``هزار`` multiplies the number group in front of it
#: ("دو هزار" = 2000, "صد و پنجاه هزار" = 150000 - the ordinary way platelets,
#: white cells, and large doses are dictated), or opens a group of its own
#: ("هزار و دویست" = 1200). It is deliberately NOT in ``SPOKEN_NUMERALS``:
#: that table is the single-valued 0-999 lexicon, and a thousand must always be
#: folded as part of a WHOLE group. Folding only the count in front of it
#: produced "دوز 2 هزار mg" for a dictated 2000 mg - a fabricated dose, and
#: exactly the half-converted number the all-or-nothing rule forbids.
SPOKEN_THOUSANDS: dict[str, int] = {"هزار": 1000}

#: Larger magnitudes ("میلیون" / "میلیارد"). These are deliberately NOT folded:
#: this module digitizes the thousands Persian dictation actually uses in
#: charts, and inventing a million-scale reading is a bigger risk than leaving
#: the value spoken. They still count as part of the number GROUP, so the
#: group is either converted whole or left completely alone - folding only the
#: number in front of them reported "پلاکت 4 میلیون" for a dictated platelet
#: count of four million, a value nobody said.
SPOKEN_LARGE_MULTIPLIERS = frozenset({"میلیون", "میلیارد", "تریلیون"})

#: Denominator words that name the fraction of a dictated decimal
#: ("یک و دو دهم" = 1.2, "سه و پنج صدم" = 3.05). They are consumed with the
#: value they spell out; leaving them behind wrote the contradictory
#: "1.2 دهم" for a dictated 1.2.
SPOKEN_FRACTION_DENOMINATORS: dict[str, int] = {
    "دهم": 10, "صدم": 100, "هزارم": 1000,
}

#: Magnitude class of a numeral word: 4 = thousands, 3 = hundreds,
#: 2 = tens (20-90), 1 = ten/teens (10-19), 0 = units (0-9).
#:
#: A Persian number is built by descending magnitude CLASS, not merely by
#: descending value: "صد و بیست و هشت" is 100 + 20 + 8, while "نهصد سیصد" is
#: two separate numbers that no speaker ever combines.  Comparing values alone
#: accepted the second one and silently reported 1200 - a dose/score nobody
#: dictated.  Only these transitions exist in the language.
_SPOKEN_NUMERAL_TRANSITIONS = frozenset(
    {(4, 3), (4, 2), (4, 1), (4, 0), (3, 2), (3, 1), (3, 0), (2, 0)}
)

#: Meaningless as a measurement on their own - never folded as a whole run.
SPOKEN_NUMERAL_UNSAFE_ALONE = frozenset({"یک", "نه"})


def _is_unit(value: Optional[int]) -> bool:
    """Whether ``value`` is a single-digit numeral (0-9)."""
    return value is not None and 0 <= value <= 9


def _half_tail(tokens: list[str], index: int,
               half_words: frozenset[str]) -> Optional[int]:
    """Index just past a spoken half ("و نیم" / "ونیم"), or ``None``.

    A bare "نیم" straight after a number is not a decimal in Persian ("هشت نیم"
    is not a reading anyone dictates), so the joiner is required - either as
    its own token or glued to the word, which recognition output does
    ("هشت ونیم").
    """
    if not half_words or index >= len(tokens):
        return None
    head = _bare(tokens[index])
    if head == SPOKEN_NUMERAL_JOINER:
        if index + 1 < len(tokens) and _bare(tokens[index + 1]) in half_words:
            return index + 2
        return None
    if head.startswith(SPOKEN_NUMERAL_JOINER) and len(head) > 1:
        if head[len(SPOKEN_NUMERAL_JOINER):] in half_words:
            return index + 1
    return None

def _fraction_denominator(tokens: list[str], index: int) -> Optional[tuple[int, int]]:
    """``(denominator, tokens_consumed)`` of a spoken fraction word, or ``None``.

    Only meaningful straight after a dictated decimal shape, where the
    denominator names what the spoken digits divide by: "یک و دو دهم" is
    1 + 2/10, "سه و پنج صدم" is 3 + 5/100.
    """
    if index >= len(tokens):
        return None
    denominator = SPOKEN_FRACTION_DENOMINATORS.get(_bare(tokens[index]))
    if denominator is None:
        return None
    return denominator, 1


#: ``after`` anchors that name a duration or a count of repetitions rather than
#: a measured quantity. A number that is NOTHING BUT a bare "هزار" is not
#: digitized in front of them: "هزار سال پیش" and "هزار بار گفتم" are ordinary
#: Persian prose, not a charted value. A counted thousand is a real number and
#: is unaffected ("دو هزار سال" -> "2000 سال", like the existing "پنج سال").
_WEAK_THOUSAND_ANCHORS = frozenset({"سال", "ساله", "بار"})


def _number_group_end(tokens: list[str], index: int, end: int) -> int:
    """End of the number GROUP that starts at ``index`` (folds nothing).

    A group is the run itself plus every ``و <run>`` behind it and every
    numeral or thousand the run refused to absorb. It is computed separately
    from ``spoken_number_at`` so a malformed sequence can be skipped as a whole
    even when the run itself was refused: folding only its head (or leaving its
    tail for the next loop iteration to fold) would report part of a number
    nobody dictated - "نمره هشت سه" must not become "نمره 8 سه", and a refused
    "دو هزار هزار" must not digitize its trailing "هزار".
    """
    group_end = end
    while group_end < len(tokens):
        if _bare(tokens[group_end]) in SPOKEN_LARGE_MULTIPLIERS:
            # A million/billion extends the group (and therefore blocks the
            # fold): "چهار میلیون" must not come out as "4 میلیون".
            group_end += 1
            continue
        if tokens[group_end] == SPOKEN_NUMERAL_JOINER:
            following = spoken_number_at(tokens, group_end + 1)
            if following is None:
                break
            group_end = group_end + 1 + following[0]
            continue
        if (_numeral(tokens[group_end]) is not None
                or _bare(tokens[group_end]) in SPOKEN_THOUSANDS):
            following = spoken_number_at(tokens, group_end,
                                         allow_single_unsafe=True)
            group_end += following[0] if following else 1
            continue
        break
    return group_end

#: Punctuation stripped from a token before it is compared with a table. The
#: trimmed characters are never consumed by a replacement, so a sentence stop
#: or a comma always survives the fold.
_ANCHOR_PUNCTUATION = ".,:;!?،؛؟«»()[]{}\"\""


@dataclass(frozen=True)
class NumericContext:
    """Which neighbouring words turn a spoken numeral into a measured value.

    Supplied by the medical layer (see ``matcher.NUMERIC_CONTEXT``) so this
    module keeps no domain vocabulary of its own beyond the numeral lexicon.
    """

    #: Words that may precede the number ("ساعت", "سن", "روی", ...).
    before: frozenset[str] = frozenset()
    #: Words that may follow the number ("ساله", "%", "روی", ...).
    after: frozenset[str] = frozenset()
    #: Spoken ratio connector, e.g. Persian "روی" ("120 over 80").
    ratio_connector: str = ""
    #: Word introducing a clock hour ("ساعت").
    clock_lead: str = ""
    #: Word naming the minute unit ("دقیقه").
    minute_unit: str = ""
    #: Spoken half / quarter of an hour.
    half_words: frozenset[str] = frozenset()
    quarter_words: frozenset[str] = frozenset()
    #: Day parts that anchor a clock hour ("AM", "PM", "ظهر", ...).
    day_parts: frozenset[str] = frozenset()
    #: Latin meridiem tags, used only to notice that a time is already written
    #: out numerically right after the spoken hour ("دوازده 12 PM").
    meridiem_words: frozenset[str] = frozenset({"am", "pm"})
    #: Words that turn a spoken decimal into part of a LARGER construction, so
    #: it must not be written as a decimal: an age range ("پنج و شش ساله" is
    #: "5-6 years old", not 5.6), a duration ("ساعت هشت و نیم ساعت") or a
    #: clock reading. Integers are unaffected - "سن سه سال" still folds to 3.
    decimal_blockers: frozenset[str] = frozenset()


def _bare(token: str) -> str:
    """``token`` without the punctuation wrapped around it by the ASR."""
    return token.strip(_ANCHOR_PUNCTUATION)


def _token_spans(text: str) -> tuple[list[str], list[tuple[int, int]]]:
    """``text.split(" ")`` plus the (start, end) slice of every token.

    Normalization guarantees single spaces, so the spans are exact and every
    rewrite is applied by slicing instead of re-joining (which would silently
    reformat unrelated whitespace).
    """
    tokens = text.split(" ")
    spans: list[tuple[int, int]] = []
    position = 0
    for token in tokens:
        spans.append((position, position + len(token)))
        position += len(token) + 1
    return tokens, spans


def _span_end(spans: list[tuple[int, int]], tokens: list[str], index: int) -> int:
    """Character end of ``tokens[index]``, excluding its trailing punctuation."""
    start, end = spans[index]
    return end - (len(tokens[index]) - len(tokens[index].rstrip(_ANCHOR_PUNCTUATION)))


def _anchor(token: Optional[str], anchors: frozenset[str]) -> bool:
    """Whether ``token`` is one of ``anchors``, ignoring punctuation and case.

    The dictionary re-writes matched spans with their canonical spelling
    ("SpO2", "AM"), while the raw normalized text is lower-case ("spo2"), so
    every anchor set is stored case-folded and compared case-insensitively.
    """
    return token is not None and _bare(token).casefold() in anchors


def _numeral(token: str) -> Optional[int]:
    return SPOKEN_NUMERALS.get(_bare(token))


def _magnitude(value: int) -> int:
    """Magnitude class of a numeral value (see ``_SPOKEN_NUMERAL_TRANSITIONS``)."""
    if value >= 1000:
        return 4
    if value >= 100:
        return 3
    if value >= 20:
        return 2
    if value >= 10:
        return 1
    return 0


def _continues_number(previous: int, value: int) -> bool:
    """Whether ``value`` may extend a numeral run whose last part was ``previous``."""
    return (_magnitude(previous), _magnitude(value)) in _SPOKEN_NUMERAL_TRANSITIONS


def spoken_number_at(tokens: list[str], index: int, *,
                     allow_single_unsafe: bool = False,
                     half_words: frozenset[str] = frozenset(),
                     ) -> Optional[tuple[int, float]]:
    """``(tokens_consumed, value)`` of the maximal valid numeral run at ``index``.

    ``None`` when no valid number starts there. A run interrupted by anything
    unexpected stops BEFORE it, and the caller then refuses to fold even a
    prefix, so a number is never half-converted.

    Parts must descend by magnitude CLASS (hundreds -> tens -> units), which is
    how Persian numbers are actually built. Two same-class numerals spoken back
    to back ("نهصد سیصد", "هشت سه") are two separate numbers and are never
    added together: reporting their sum would invent a dose or a score.

    Two decimal shapes are recognised, and only these two:

    * a single unit digit, "و", another single unit digit - "یک و هشت" (1.8),
      "نه و پنج" (9.5). Magnitude order alone rejects this, because two units
      never sum in a cardinal, but a doctor dictates it for a decimal;
    * a whole number followed by a spoken half - "سی و هشت و نیم" (38.5).

    Both need the half/decimal words, which are supplied by the caller as
    ``half_words`` so this module keeps no domain vocabulary of its own. A
    value is ``int`` unless a decimal shape was actually consumed, so an
    ordinary integer still renders as "45" and never as "45.0".

    A spoken thousand multiplies the parts in front of it and must stay inside
    ONE group: a run that meets a thousand it cannot consume refuses the whole
    run instead of returning a shorter prefix, so a count is never digitized
    next to the "هزار" that gives it its magnitude.
    """
    parts: list[int] = []
    previous: Optional[int] = None
    thousand_used = False
    cursor = index
    while cursor < len(tokens):
        token = tokens[cursor]
        if token == SPOKEN_NUMERAL_JOINER:
            if not parts or cursor + 1 >= len(tokens):
                break
            follower = _numeral(tokens[cursor + 1])
            if follower is None:
                # "و هزار" is not a number anyone dictates: the joiner is
                # followed by a pure multiplier, so the group is malformed and
                # is refused whole rather than folded up to the "و".
                if _bare(tokens[cursor + 1]) in SPOKEN_THOUSANDS:
                    return None
                break
            if previous is None or not _continues_number(previous, follower):
                # Only a lone unit digit may take a unit tenths digit this
                # way: "یک و هشت" is 1.8, while "نهصد و سیصد" stays two
                # separate numbers.
                if (len(parts) != 1 or not _is_unit(previous)
                        or not _is_unit(follower)):
                    break
                value = parts[0] + follower / 10
                consumed = cursor + 2 - index
                fraction = _fraction_denominator(tokens, cursor + 2)
                if fraction is not None:
                    # "یک و دو دهم" is one and two tenths: the denominator word
                    # is part of the value and is consumed with it.
                    denominator, extra = fraction
                    value = parts[0] + follower / denominator
                    consumed += extra
                return (consumed, value)
            value, cursor = follower, cursor + 2
        else:
            value = _numeral(token)
            if value is None:
                multiplier = SPOKEN_THOUSANDS.get(_bare(token))
                if multiplier is None:
                    break
                # A thousand is multiplicative and may appear once: "دو هزار"
                # (2000), "صد و پنجاه هزار" (150000), "هزار و دویست" (1200).
                # Anything else - a second thousand, a thousand with nothing
                # to multiply but no continuation - refuses the whole run.
                if thousand_used:
                    return None
                thousand_used = True
                if not parts:
                    # "هزار" with nothing in front of it is a value only in a
                    # measured context; the caller decides that, because a bare
                    # "هزار" is also the ordinary word for "a thousand" in
                    # prose ("هزار سال پیش").
                    parts.append(multiplier)
                else:
                    parts = [sum(parts) * multiplier]
                previous = parts[-1]
                cursor += 1
                continue
            if parts and (previous is None or not _continues_number(previous, value)):
                break
            cursor += 1
        parts.append(value)
        previous = value
    if not parts:
        return None
    total = sum(parts)
    if half_words:
        tail = _half_tail(tokens, cursor, half_words)
        if tail is not None:
            # A half makes the value a decimal whatever the integer part was,
            # so "یک و نیم" (1.5) and "نه و نیم" (9.5) are measurements even
            # though a bare "یک"/"نه" is the ordinary word.
            return tail - index, total + 0.5
    if (len(parts) == 1 and not allow_single_unsafe
            and tokens[index] in SPOKEN_NUMERAL_UNSAFE_ALONE):
        return None
    return cursor - index, total


def _number_at(tokens: list[str], index: int, maximum: int, *,
               allow_single_unsafe: bool = False) -> tuple[Optional[int], int]:
    """A written number or a spoken numeral run, bounded by ``maximum``."""
    token = _bare(tokens[index])
    if token.isdigit() and len(token) <= 3:
        value = int(token)
        return (value, 1) if value <= maximum else (None, 0)
    # No ``half_words`` here on purpose: this helper feeds the CLOCK pass,
    # which reads a half as minutes ("ساعت هشت و نیم" -> "8:30"). Letting a
    # decimal through would give an hour field of "8.5".
    run = spoken_number_at(tokens, index,
                           allow_single_unsafe=allow_single_unsafe)
    if run is None:
        return None, 0
    consumed, value = run
    if isinstance(value, float):
        return None, 0
    return (value, consumed) if value <= maximum else (None, 0)


def _rebuild(text: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply non-overlapping ``(start, end, replacement)`` edits left to right.

    An edit that starts inside the span of the previous one is DROPPED rather
    than concatenated.  Two overlapping edits share source characters, and
    emitting both wrote those characters twice: a chained "120 روی 80 روی 60"
    produced "120/8080/60", inventing a systolic value that was never spoken.
    Skipping the later edit leaves that part of the text exactly as dictated,
    which is always the safe direction.
    """
    out: list[str] = []
    cursor = 0
    for start, end, replacement in edits:
        if start < cursor:
            continue
        out.append(text[cursor:start])
        out.append(replacement)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def fold_spoken_numbers(text: str, context: NumericContext) -> str:
    """Fold anchor-backed spoken numerals into ASCII digits, in place."""
    if not text or not (context.before or context.after):
        return text
    tokens, spans = _token_spans(text)
    edits: list[tuple[int, int, str]] = []
    index = 0
    while index < len(tokens):
        run = spoken_number_at(tokens, index, half_words=context.half_words)
        if run is None:
            # A refused run is still part of a number GROUP. Advancing a
            # single token let the remainder be folded on its own: in "دو هزار
            # هزار" the refused first pair was skipped and the second "هزار"
            # was digitized as a standalone value. The whole unresolved group
            # is skipped instead - nothing inside it is folded.
            if (_numeral(tokens[index]) is not None
                    or _bare(tokens[index]) in SPOKEN_THOUSANDS):
                index = _number_group_end(tokens, index, index + 1)
            else:
                index += 1
            continue
        count, value = run
        end = index + count
        # All-or-nothing per NUMBER GROUP (a run plus every "و <run>" behind
        # it, plus any numeral juxtaposed straight after it): an unanchored
        # group, a dangling "و", or several runs that do not form one valid
        # number are skipped whole, so "پنج و شش" and "بیست و" never come out
        # as "5 و 6" / "20 و", and "نمره هشت سه" never becomes "نمره 8 سه".
        group_end = _number_group_end(tokens, index, end)
        incomplete = group_end > end
        before_token = tokens[index - 1] if index else None
        anchored_before = _anchor(before_token, context.before)
        if (anchored_before and context.ratio_connector
                and _bare(before_token or "") == context.ratio_connector):
            # "روی" only anchors the RIGHT side of a real numeric ratio.  It
            # must not digitize corrupt prose such as "MRI روی هشتاد و پنج".
            anchored_before = (
                index >= 2 and _is_plain_number(_bare(tokens[index - 2]))
            )
        anchored = anchored_before or _anchor(
            tokens[group_end] if group_end < len(tokens) else None,
            context.after,
        )
        if _meridiem_restated(tokens, group_end, context):
            anchored = False
        if (count == 1 and _bare(tokens[index]) in SPOKEN_THOUSANDS
                and _anchor(tokens[group_end] if group_end < len(tokens) else None,
                            _WEAK_THOUSAND_ANCHORS)):
            # A group that is nothing but "هزار" is a value only in front of a
            # measured unit ("هزار میلی گرم" = 1000 mg), never in front of a
            # duration or a count of repetitions ("هزار سال پیش").
            anchored = False
        if isinstance(value, float) and _anchor(
                tokens[group_end] if group_end < len(tokens) else None,
                context.decimal_blockers):
            # "پنج و شش ساله" is a five-to-six-year-old, not 5.6 years; a
            # decimal followed by one of these belongs to a range, a duration
            # or a clock reading, so it is left exactly as dictated.
            anchored = False
        if not incomplete and anchored:
            edits.append((spans[index][0], _span_end(spans, tokens, end - 1), str(value)))
        index = group_end
    return _rebuild(text, edits) if edits else text


def fold_clock_times(text: str, context: NumericContext) -> str:
    """Fold spoken clock hours (plus half/quarter/minute tails) into ``H:MM``.

    Deliberately narrow: it recognises only ``[ساعت] <hour> [و (نیم|ربع|
    <minutes> دقیقه)]`` when an anchor makes it a clock reading - "ساعت" before
    it, or a day part after it. There is no calendar parsing and no 12/24-hour
    arithmetic: the day part itself is the dictionary's business, and a number
    that is not a valid hour (or a duration such as "دو و نیم ساعت") is left
    exactly as spoken.
    """
    if not text or not (context.clock_lead or context.day_parts):
        return text
    tokens, spans = _token_spans(text)
    edits: list[tuple[int, int, str]] = []
    index = 0
    while index < len(tokens):
        lead = index > 0 and _bare(tokens[index - 1]) == context.clock_lead
        hour, consumed = _number_at(
            tokens, index, 23, allow_single_unsafe=bool(lead and context.clock_lead)
        )
        if hour is None:
            index += 1
            continue
        cursor = index + consumed
        tail = _clock_minutes(tokens, cursor, context, single_unsafe=bool(lead))
        if tail is None:
            if cursor < len(tokens) and tokens[cursor] == SPOKEN_NUMERAL_JOINER:
                index = cursor + 1   # "ساعت هشت و ..." that resolves to nothing
                continue
            minutes, extra, unit = 0, 0, ""
        else:
            minutes, extra, unit = tail
            cursor += extra
        anchored = bool(lead) or _anchor(
            tokens[cursor] if cursor < len(tokens) else None, context.day_parts
        )
        # "ساعت دوازده 12 PM" already states the hour in digits; folding the
        # spoken one too would write it twice.
        restated = _meridiem_restated(tokens, cursor, context)
        if not anchored or restated or _bare(
                tokens[cursor] if cursor < len(tokens) else "") == context.clock_lead:
            # The second test rejects durations ("... و نیم ساعت" = "and a half
            # hours"), which are not clock readings.
            index = max(cursor, index + 1)
            continue
        # A written hour keeps its own spelling ("ساعت 08" is a charted form,
        # not something to reformat); a spoken one becomes the digits.
        hour_text = _bare(tokens[index]) if _bare(tokens[index]).isdigit() else str(hour)
        replacement = f"{hour_text}:{minutes:02d}{unit}" if tail else hour_text
        edits.append((spans[index][0], _span_end(spans, tokens, cursor - 1), replacement))
        index = cursor
    return _rebuild(text, edits) if edits else text


def _clock_minutes(tokens: list[str], index: int, context: NumericContext, *,
                   single_unsafe: bool) -> Optional[tuple[int, int, str]]:
    """``(minutes, tokens_consumed, unit_to_keep)`` of a "و ..." minute tail.

    Recognition output commonly glues the joiner to the next word
    ("هشت وربع", "هشت ونیم"), so a tail token that merely *starts* with "و" is
    read as "و" + that word. A numeric tail additionally needs its unit word,
    and is refused when a second "و" follows ("ساعت هشت و ۳۰ و ۴۵ دقیقه").
    """
    if index >= len(tokens):
        return None
    head = _bare(tokens[index])
    if head == SPOKEN_NUMERAL_JOINER:
        if index + 1 >= len(tokens):
            return None
        follower, consumed = _bare(tokens[index + 1]), 2
    elif head.startswith(SPOKEN_NUMERAL_JOINER) and len(head) > 1:
        follower, consumed = head[len(SPOKEN_NUMERAL_JOINER):], 1
    else:
        return None
    if follower in context.half_words:
        return 30, consumed, ""
    if follower in context.quarter_words:
        return 15, consumed, ""
    if consumed != 2 or not context.minute_unit:
        return None
    value, used = _number_at(tokens, index + 1, 59,
                             allow_single_unsafe=single_unsafe)
    if value is None:
        return None
    after = index + 1 + used
    if after >= len(tokens) or _bare(tokens[after]) != context.minute_unit:
        return None
    if after + 1 < len(tokens) and tokens[after + 1] == SPOKEN_NUMERAL_JOINER:
        return None
    # "و" + the minute tokens + the unit token.  The unit is consumed because
    # the colon notation already states that the second field is minutes.
    return value, used + 2, ""


def _meridiem_restated(tokens: list[str], index: int,
                       context: NumericContext) -> bool:
    """True when ``<digits> AM|PM`` sits at ``index``.

    A spoken hour that a numeric meridiem already restates ("دوازده 12 PM")
    must not be folded too, or the hour would be written twice.
    """
    if not context.meridiem_words or index >= len(tokens):
        return False
    return (_bare(tokens[index]).isdigit()
            and index + 1 < len(tokens)
            and _bare(tokens[index + 1]).casefold() in context.meridiem_words)


def fold_spoken_ratio(text: str, context: NumericContext) -> str:
    """"<a> <connector> <b>" -> "a/b" between two adjacent numbers.

    Only digits on BOTH sides trigger it, so an ordinary prepositional "روی"
    ("درد روی سینه") is never turned into a slash and an existing "120/80" is
    left exactly as it is.
    """
    connector = context.ratio_connector
    if not text or not connector:
        return text
    tokens, spans = _token_spans(text)
    edits: list[tuple[int, int, str]] = []
    index = 1
    while index < len(tokens) - 1:
        if _bare(tokens[index]) != connector:
            index += 1
            continue
        left, right = _bare(tokens[index - 1]), _bare(tokens[index + 1])
        if not _is_plain_number(left) or not _is_plain_number(right):
            index += 1
            continue
        # A ratio is exactly two numbers. A CHAIN ("120 روی 80 روی 60") is not
        # a blood pressure at all, and its middle number belongs to both
        # halves: folding either half would have to duplicate it. The whole
        # chain is therefore left exactly as dictated - the reading is
        # ambiguous, so nothing may be asserted about it.
        chain_end = index + 1
        while (chain_end + 1 < len(tokens)
               and _bare(tokens[chain_end + 1]) == connector
               and chain_end + 2 < len(tokens)
               and _is_plain_number(_bare(tokens[chain_end + 2]))):
            chain_end += 2
        if chain_end > index + 1:
            index = chain_end + 1
            continue
        edits.append((spans[index - 1][0], _span_end(spans, tokens, index + 1),
                      f"{left}/{right}"))
        index += 2
    return _rebuild(text, edits) if edits else text


def _is_plain_number(token: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)?", token))


def fold_numeric_expressions(text: str, context: NumericContext) -> str:
    """Clock times first, then anchored spoken numerals, then spoken ratios.

    The order is fixed and the function is idempotent: every fold emits ASCII
    digits, which no later pass can reinterpret, and each pass sees exactly the
    text the previous one produced.
    """
    if not text:
        return text
    text = fold_clock_times(text, context)
    # Two bounded passes let the left side of a spoken ratio become digits
    # before "روی" is considered as context for the right side.  This avoids
    # treating an arbitrary "X روی eighty" fragment as a measurement.
    text = fold_spoken_numbers(text, context)
    text = fold_spoken_numbers(text, context)
    return fold_spoken_ratio(text, context)
