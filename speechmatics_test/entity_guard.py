"""Deterministic, non-destructive validation of clinical entities.

The guard deliberately sits *after* lexical canonicalization.  It never
changes a transcript or proposes a replacement; it only returns review flags
for values that are implausible, incomplete, or internally inconsistent.
This makes the checks auditable and keeps responsibility for transcription
with the upstream ASR service and the clinician.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

__all__ = ["ClinicalEntityGuard", "EntityFlag"]


# The input usually passed by the application has already gone through
# normalize_text(), but using a one-to-one digit translation here makes the
# public guard equally safe for direct callers with Persian/Arabic digits.
_DIGITS_TO_ASCII = str.maketrans(
    {chr(0x06F0 + index): str(index) for index in range(10)}
    | {chr(0x0660 + index): str(index) for index in range(10)}
)

_BP_RE = re.compile(r"(?<!\d)(\d{2,3})\s*/\s*(\d{2,3})(?!\d)")
_TEMPERATURE_RE = re.compile(
    r"(?:\btemp(?:erature)?\b|دمای(?:\s+بدن)?|حرارت(?:\s+بدن)?)"
    r"\s*[:=]?\s*(\d{1,2}(?:[.,]\d+)?)",
    re.IGNORECASE,
)
_SPO2_RE = re.compile(
    r"(?:\bspo[₂2]\b|\bo2\s*sat(?:uration)?\b|اشباع(?:\s+اکسیژن)?)"
    r"\s*[:=]?\s*(\d{1,3})(\s*%)?",
    re.IGNORECASE,
)
_PULSE_RE = re.compile(
    r"(?:\bpr\b|\bpulse\b|نبض)\s*[:=]?\s*(\d{1,3})",
    re.IGNORECASE,
)
_AGE_RE = re.compile(
    r"(?:(\d{1,3})\s*ساله|\bage\s*:?\s*(\d{1,3}))",
    re.IGNORECASE,
)
_GAUGE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[Gg]\b")
_PERCENT_RE = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*%(?!\w)")
_CLOCK_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
_MORSE_RE = re.compile(
    r"(?:\bmorse(?:\s+score)?\b|مورس(?:\s+(?:اسکور|نمره))?)"
    r"\s*[:=]?\s*(\d{1,3})",
    re.IGNORECASE,
)
_BRADEN_RE = re.compile(
    r"(?:\bbraden(?:\s+score)?\b|برادن(?:\s+(?:اسکور|نمره))?)"
    r"\s*[:=]?\s*(\d{1,3})",
    re.IGNORECASE,
)

# A gauge only becomes an IV gauge when the nearby text says that it is one.
_IV_CONTEXT_RE = re.compile(
    r"\b(?:iv(?:[-\s]?line)?|intravenous)\b|(?:لاین|خط)\s*وریدی|وریدی",
    re.IGNORECASE,
)
_SOLUTION_CONTEXT_RE = re.compile(
    r"\b(?:normal\s+saline|saline|solution)\b|نرمال\s+سالین|سالین|محلول",
    re.IGNORECASE,
)

_STANDARD_GAUGES = frozenset({14, 16, 18, 20, 22, 24})
_STANDARD_CONCENTRATIONS = frozenset({0.45, 0.9, 5.0, 10.0, 20.0, 50.0})


@dataclass(frozen=True)
class EntityFlag:
    """A review-only finding emitted by :class:`ClinicalEntityGuard`.

    ``position`` is a zero-based character offset in the original input.  The
    guard only translates digit glyphs internally and that translation is
    one-to-one, so positions remain valid for Persian and Arabic numerals.
    """

    entity_type: str
    value: str
    position: int
    issue: str
    severity: str

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON-report shape (``type`` is externally named)."""
        return {
            "type": self.entity_type,
            "value": self.value,
            "position": self.position,
            "issue": self.issue,
            "severity": self.severity,
        }


class ClinicalEntityGuard:
    """Non-destructive, deterministic clinical entity validation.

    ``scan`` is intentionally stateless: calling it twice with the same text
    returns equal flags, and it cannot accidentally compare one dictation
    session with another.  For clock-time consistency, pass the whole
    session's canonical transcript to one call; the application does exactly
    that for the final report.
    """

    def scan(self, text: str) -> list[EntityFlag]:
        """Return review flags for suspicious entities in ``text``.

        The input object is never mutated and no replacement text is produced.
        Invalid/missing values are not guessed.  Output is position-sorted to
        make the result stable for JSON reports and tests.
        """
        source = text or ""
        scanned = source.translate(_DIGITS_TO_ASCII)
        flags: list[EntityFlag] = []

        flags.extend(self._scan_bp(scanned))
        flags.extend(self._scan_temperature(scanned))
        flags.extend(self._scan_spo2(scanned))
        flags.extend(self._scan_pulse(scanned))
        flags.extend(self._scan_age(scanned))
        flags.extend(self._scan_iv_gauge(scanned))
        flags.extend(self._scan_concentration(scanned))
        flags.extend(self._scan_scores(scanned))
        flags.extend(self._scan_clock_times(scanned))

        return sorted(
            flags,
            key=lambda flag: (
                flag.position,
                flag.entity_type,
                flag.value,
                flag.issue,
                flag.severity,
            ),
        )

    # ---------------------------------------------------------- vital signs

    @staticmethod
    def _scan_bp(text: str) -> Iterator[EntityFlag]:
        for match in _BP_RE.finditer(text):
            systolic, diastolic = int(match.group(1)), int(match.group(2))
            value = f"{match.group(1)}/{match.group(2)}"
            position = match.start(1)
            if not 70 <= systolic <= 200:
                yield EntityFlag(
                    "bp", value, position,
                    f"Systolic BP {systolic} is outside the valid range 70-200",
                    "critical",
                )
            if not 40 <= diastolic <= 120:
                yield EntityFlag(
                    "bp", value, position,
                    f"Diastolic BP {diastolic} is outside the valid range 40-120",
                    "critical",
                )
            if systolic <= diastolic:
                yield EntityFlag(
                    "bp", value, position,
                    f"BP {value} is impossible: systolic must be greater than diastolic",
                    "critical",
                )
            # This does not declare a diagnosis and does not replace the
            # value. It simply surfaces a boundary reading called out in the
            # clinical-review requirements.
            elif systolic <= 100:
                yield EntityFlag(
                    "bp", value, position,
                    f"Systolic {systolic} is at a low boundary — verify against chart",
                    "warning",
                )

    @staticmethod
    def _scan_temperature(text: str) -> Iterator[EntityFlag]:
        for match in _TEMPERATURE_RE.finditer(text):
            raw_value = match.group(1)
            value = float(raw_value.replace(",", "."))
            position = match.start(1)
            if not 35.0 <= value <= 41.0:
                yield EntityFlag(
                    "temperature", raw_value, position,
                    f"Temperature {raw_value} is outside the valid range 35.0-41.0",
                    "critical",
                )
            elif "." not in raw_value and "," not in raw_value:
                yield EntityFlag(
                    "temperature", raw_value, position,
                    "Temperature without decimal — possible truncation",
                    "warning",
                )

    @staticmethod
    def _scan_spo2(text: str) -> Iterator[EntityFlag]:
        for match in _SPO2_RE.finditer(text):
            value = int(match.group(1))
            if not 90 <= value <= 100:
                severity = "critical" if value < 90 else "warning"
                yield EntityFlag(
                    "spo2", match.group(1), match.start(1),
                    f"SpO2 {value}% is outside the valid range 90-100",
                    severity,
                )

    @staticmethod
    def _scan_pulse(text: str) -> Iterator[EntityFlag]:
        for match in _PULSE_RE.finditer(text):
            value = int(match.group(1))
            if not 30 <= value <= 220:
                yield EntityFlag(
                    "pulse", match.group(1), match.start(1),
                    f"Pulse {value} is outside the valid range 30-220",
                    "critical" if value < 30 or value > 220 else "warning",
                )

    @staticmethod
    def _scan_age(text: str) -> Iterator[EntityFlag]:
        for match in _AGE_RE.finditer(text):
            value_text = match.group(1) or match.group(2)
            value = int(value_text)
            position = match.start(1) if match.group(1) is not None else match.start(2)
            if not 0 <= value <= 120:
                yield EntityFlag(
                    "age", value_text, position,
                    f"Age {value} is outside the valid range 0-120",
                    "warning",
                )
            # This is deliberately an informational review cue, not a claim
            # that a clinically valid age is wrong. The upstream-ASR incident
            # corpus repeatedly substituted age 40 for another age, so the
            # exact fallback value is surfaced for chart verification.
            elif value == 40:
                yield EntityFlag(
                    "age", value_text, position,
                    "Age 40 is an ASR-sensitive value — verify against chart",
                    "info",
                )

    # ------------------------------------------------ IV / concentration

    @staticmethod
    def _near_context(pattern: re.Pattern[str], text: str, start: int, end: int) -> bool:
        """Whether a clinical context occurs close enough to an entity.

        Limiting the local window prevents unrelated prose elsewhere in a
        long note from turning every ``20G`` or percent into a clinical entity.
        """
        window_start = max(0, start - 32)
        window_end = min(len(text), end + 32)
        return pattern.search(text[window_start:window_end]) is not None

    def _scan_iv_gauge(self, text: str) -> Iterator[EntityFlag]:
        for match in _GAUGE_RE.finditer(text):
            if not self._near_context(_IV_CONTEXT_RE, text, match.start(), match.end()):
                continue
            value = int(match.group(1))
            if value not in _STANDARD_GAUGES:
                yield EntityFlag(
                    "iv_gauge", match.group(1), match.start(1),
                    f"IV gauge {value}G is not a standard size",
                    "warning",
                )

    def _scan_concentration(self, text: str) -> Iterator[EntityFlag]:
        # ``SpO2 96% ... saline`` may put both labels in a short window. A
        # saturation value is never reclassified as an IV-fluid percentage.
        spo2_value_spans = [
            (match.start(1), match.end(1)) for match in _SPO2_RE.finditer(text)
        ]
        for match in _PERCENT_RE.finditer(text):
            if any(start <= match.start(1) < end for start, end in spo2_value_spans):
                continue
            if not self._near_context(_SOLUTION_CONTEXT_RE, text, match.start(), match.end()):
                continue
            raw_value = match.group(1)
            value = float(raw_value.replace(",", "."))
            if value not in _STANDARD_CONCENTRATIONS:
                yield EntityFlag(
                    "concentration", raw_value + "%", match.start(1),
                    f"IV fluid concentration {raw_value}% is not a standard concentration",
                    "warning",
                )

    # -------------------------------------------------------------- scoring

    @staticmethod
    def _scan_scores(text: str) -> Iterator[EntityFlag]:
        for pattern, entity_type, low, high, display in (
            (_MORSE_RE, "morse", 0, 125, "Morse"),
            (_BRADEN_RE, "braden", 6, 23, "Braden"),
        ):
            for match in pattern.finditer(text):
                value = int(match.group(1))
                if not low <= value <= high:
                    yield EntityFlag(
                        entity_type, match.group(1), match.start(1),
                        f"{display} score {value} is outside the valid range {low}-{high}",
                        "warning",
                    )

    @staticmethod
    def _scan_clock_times(text: str) -> Iterator[EntityFlag]:
        times: list[tuple[str, int]] = []
        for match in _CLOCK_RE.finditer(text):
            hour, minute = int(match.group(1)), int(match.group(2))
            value = f"{match.group(1)}:{match.group(2)}"
            if hour > 23 or minute > 59:
                yield EntityFlag(
                    "clock_time", value, match.start(1),
                    f"Clock time {value} is not a valid 24-hour time",
                    "warning",
                )
                continue
            times.append((value, match.start(1)))

        # The method is stateless by design, so this comparison is always
        # strictly intra-document/intra-session. The first and final reading
        # are enough to expose a contradictory repeated time without trying to
        # infer dates, event identities, or a 'correct' time.
        if len(times) > 1 and times[0][0] != times[-1][0]:
            first, last = times[0], times[-1]
            yield EntityFlag(
                "clock_time", last[0], last[1],
                f"Clock times are inconsistent within this session ({first[0]} vs {last[0]})",
                "warning",
            )
