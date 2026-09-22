from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import signal
import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from speechmatics_test.cleanliness import inspect_text
from speechmatics_test.evaluation import evaluate_stages
from speechmatics_test.medical_layer import MedicalLayer
from speechmatics_test.nursing_text import (
    MAX_PENDING_TAIL_TOKENS,
    duplicate_boundary_tokens,
    is_vital_label,
    pending_tail_tokens,
)
from speechmatics_test.realtime import (
    DEFAULT_MAX_DELAY,
    DEFAULT_MAX_DELAY_MODE,
    DEFAULT_MODEL,
    MAX_MAX_DELAY,
    MEDICAL_DOMAIN_MODELS,
    MIN_MAX_DELAY,
    VALID_DOMAINS,
    confidence_summary,
    resolve_domain,
)
from speechmatics_test.text import normalize_text


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="SwiftMedics Speechmatics mixed Persian/English medical ASR "
                    "with Aho-Corasick post-processing and automatic text injection"
    )
    p.add_argument("--language", choices=["fa", "en"], default="fa")
    p.add_argument("--test-id")
    p.add_argument("--no-vocab", action="store_true")
    p.add_argument("--no-medical-layer", action="store_true")
    p.add_argument("--no-text-polish", action="store_true",
                   help="Disable the deterministic nursing text stage "
                        "(numbers, clock times, units, charted vital-sign "
                        "punctuation, ASR-stutter and Persian typography "
                        "cleanup). The lexical medical layer stays active.")
    p.add_argument("--inject", dest="inject", action="store_true", default=True,
                   help="Automatically inject each finalized segment into the "
                        "focused field (default). No hotkeys, no countdown.")
    p.add_argument("--no-inject", dest="inject", action="store_false",
                   help="Disable automatic injection.")
    p.add_argument("--no-overlay", action="store_true")
    p.add_argument("--device-index", type=int,
                   help="PyAudio input device index (default: system default device)")
    p.add_argument("--max-seconds", type=float, default=300.0)
    p.add_argument("--model", choices=["standard", "enhanced"], default=DEFAULT_MODEL,
                   help="Speechmatics model (default: %(default)s)")
    p.add_argument("--max-delay", type=float, default=DEFAULT_MAX_DELAY,
                   help=f"Final-transcript delay in seconds, valid {MIN_MAX_DELAY}-{MAX_MAX_DELAY} "
                        f"(default: %(default)s, docs-recommended)")
    p.add_argument("--max-delay-mode", choices=["fixed", "flexible"],
                   default=DEFAULT_MAX_DELAY_MODE,
                   help="flexible lets the engine finish spoken entities (numbers) "
                        "(default: %(default)s)")
    p.add_argument("--domain", choices=list(VALID_DOMAINS), default="auto",
                   help="auto: send domain=medical only for languages where "
                        "Speechmatics documents the Enhanced Medical model "
                        "(fa is NOT one of them); medical: force it; none: "
                        "never send a domain (default: %(default)s)")
    p.add_argument("--no-focus-guard", action="store_true",
                   help="Disable the injection focus guard (Windows: paste "
                        "only while the armed target window is focused)")
    p.add_argument("--save-report", action="store_true",
                   help="Save the text/metadata session report as JSON "
                        "(always saved when --test-id is given)")
    return p.parse_args()


def load_benchmark(test_id: str | None) -> dict | None:
    if not test_id:
        return None
    data = json.loads((ROOT / "benchmark" / "sentences.json").read_text(encoding="utf-8"))
    for x in data:
        if x.get("id") == test_id:
            return x
    raise RuntimeError(f"Unknown test ID: {test_id}")


async def audio_source(recorder, max_seconds: float, stop_event=None):
    """Yield microphone chunks in memory only - never stored on disk.

    ``stop_event`` (a ``threading.Event``) lets Ctrl+C end the stream
    *gracefully*: the generator simply stops yielding, the realtime session
    closes normally, and the transcript/report is still produced. Raising
    KeyboardInterrupt through the event loop instead used to abort the whole
    pipeline and throw the dictated text away.
    """
    started = time.perf_counter()
    while time.perf_counter() - started < max_seconds:
        if stop_event is not None and stop_event.is_set():
            return
        chunk = await asyncio.to_thread(recorder.read)
        if stop_event is not None and stop_event.is_set():
            return
        if chunk:
            yield chunk


def create_overlay(disabled: bool):
    if disabled:
        return None
    try:
        from overlay import TranscriptOverlay
        return TranscriptOverlay(enabled=True)
    except Exception as exc:
        print(f"[overlay disabled] {exc}")
        return None


def create_injector(enabled: bool):
    if not enabled:
        return None
    try:
        from injector import TextInjector
        return TextInjector(
            enable_smart_rewrite=True,
            restore_clipboard=True,
            paste_settle_seconds=0.25,
            add_bidi_marks=True,
        )
    except Exception as exc:
        print(f"[injector disabled] {exc}")
        return None


#: Canonical unit tokens that belong to the numeric value before them.
#: Used only to decide whether a charted "LABEL: value" pair is still
#: incomplete at a final-segment boundary (see
#: ``FinalStreamCanonicalizer._vital_label_hold_index``).
_VALUE_UNIT_CANONICALS = frozenset({
    "%", "mmHg", "mg", "mL", "L", "kg", "g", "cm", "mm", "mcg", "mEq",
    "bpm", "\u00b0C", "\u00b0F",
})

#: Tokens that can sit between a vital label and its value ("SpO2 و 97%",
#: "BP برابر با 140/85") - see ``nursing_text._VITAL_SEPARATOR``.
#: plus the spoken ratio separators ("140 \u0631\u0648\u06cc 85", "140 over 85").
_VALUE_CONNECTORS = frozenset({
    "\u0648", "\u0628\u0631\u0627\u0628\u0631", "\u0628\u0627",
    "\u0631\u0648\u06cc", "\u0628\u0631", "over",
})


class FinalStreamCanonicalizer:
    """One canonicalization state shared by injection AND the final report.

    Fixes the segment/report divergence: a medical phrase that spans
    Speechmatics final-segment boundaries (e.g. "فشار خون" + "بالا دارد")
    used to be canonicalized per segment at injection time ("BP بالا") and
    over the joined transcript in the report ("HTN دارد"), so the pasted
    text could not be reproduced from the report.

    Raw final segments are accumulated and canonicalized incrementally.
    Only the longest prefix that no future final can still complete is
    emitted for injection; the short unresolved tail stays buffered until
    the next final (or the end-of-session flush). "Unresolved" covers two
    independent kinds of cross-boundary construct:

    * a MEDICAL one, whose leading tokens start a longer matcher rule
      ("فشار خون" -> "فشار خون بالا"), and
    * a NURSING one, whose trailing tokens are a demonstrably incomplete
      number / clock / ratio construct ("سی و" + "پنج" -> "35",
      "ساعت ده" + "و سی دقیقه" -> "ساعت 10:30") - see
      :func:`speechmatics_test.nursing_text.pending_tail_tokens`.

    Incoming segments are additionally checked for a stutter that straddles
    the boundary ("نمره" + "نمره درد"), which the intra-segment cleanup in
    ``prepolish_asr_artifacts`` cannot see.

    Both tails are hard-bounded (a handful of tokens), so the buffer never
    grows with the session.

    The report's canonical stage is built from the exact emitted pieces,
    so the report always equals what was injected.
    """

    def __init__(self, medical: MedicalLayer | None) -> None:
        self._medical = medical
        # Buffered, not-yet-emitted text as ordered pieces (normalized final
        # segments, or the leftover of a segment split by an emission cut).
        self._pieces: list[dict] = []
        self._buffer = ""                # " ".join(piece texts), derived
        #: Last few tokens already seen in the stream (emitted or buffered),
        #: used only for the bounded cross-segment stutter check.
        self._recent_tail = ""
        self.parts: list[str] = []       # emitted canonical pieces, in order
        self.hits: list[dict] = []       # medical hits of the emitted pieces

    @property
    def canonical_text(self) -> str:
        """The canonical transcript exactly as it was (is being) injected."""
        return " ".join(self.parts).strip()

    def add(self, normalized_text: str, words: list[dict]) -> str | None:
        """Add one normalized final segment; return the text to inject now."""
        if not normalized_text:
            return None
        segment = self._strip_boundary_stutter(normalized_text)
        if not segment:
            return None
        # Word-level ASR evidence is positional, so it is dropped (never
        # misaligned) when the boundary cleanup moved characters.
        evidence = list(words) if segment == normalized_text else []
        if self._buffer:
            self._buffer += " " + segment
        else:
            self._buffer = segment
        self._pieces.append({"text": segment, "words": evidence})
        self._remember_tail(segment)
        return self._emit(self._safe_cut())

    def _strip_boundary_stutter(self, segment: str) -> str:
        """Drop leading tokens that only repeat the end of the stream."""
        protected = (
            self._medical.fst.repetition_safe_forms
            if self._medical is not None and self._medical.polish else None
        )
        if protected is None:
            return segment
        repeated = duplicate_boundary_tokens(
            self._recent_tail, segment, protected
        )
        if not repeated:
            return segment
        return " ".join(segment.split()[repeated:])

    def _remember_tail(self, segment: str) -> None:
        tokens = (self._recent_tail + " " + segment).split()
        self._recent_tail = " ".join(tokens[-MAX_PENDING_TAIL_TOKENS:])

    def flush(self) -> str | None:
        """Emit everything still buffered (end of session)."""
        return self._emit(len(self._buffer))

    # ------------------------------------------------------------ internals

    def _safe_cut(self) -> int:
        """Character length of the longest prefix safe to emit now.

        The emission stops at the earliest token that either the medical
        matcher (:meth:`_medical_hold_index`) or the nursing normalizer
        (:meth:`_nursing_hold_index`) may still need the NEXT final segment
        to resolve. Both windows are bounded, so the held-back tail stays
        small no matter how long the session runs.
        """
        tokens = self._buffer.split()
        if not tokens:
            return len(self._buffer)
        hold = min(
            self._medical_hold_index(tokens),
            self._nursing_hold_index(tokens),
        )
        if hold >= len(tokens):
            return len(self._buffer)   # nothing held back: emit everything
        hold = self._charted_group_start(tokens, hold)
        hold = self._outside_matched_rule(tokens, hold)
        hold = self._keep_stutter_together(tokens, hold)
        if hold <= 0:
            return 0
        return sum(len(token) + 1 for token in tokens[:hold]) - 1

    def _keep_stutter_together(self, tokens: list[str], hold: int) -> int:
        """Never emit the first half of an intra-buffer stutter on its own.

        ``prepolish_asr_artifacts`` collapses ``نمره نمره`` only when both
        copies are in the same emitted piece. Cutting between them emits the
        stutter verbatim and hands the second copy to the next emission,
        where it recombines with its neighbour into a phrase the speaker
        never said (``نمره`` + ``درد`` = "pain score"). Moving the cut in
        front of the pair keeps the existing cleanup effective.
        """
        if hold <= 0 or hold >= len(tokens):
            return hold
        protected = (
            self._medical.fst.repetition_safe_forms
            if self._medical is not None and self._medical.polish else None
        )
        if protected is None:
            return hold
        repeated = duplicate_boundary_tokens(
            " ".join(tokens[:hold]), " ".join(tokens[hold:]), protected
        )
        return hold - repeated if repeated else hold

    def _outside_matched_rule(self, tokens: list[str], hold: int) -> int:
        """Pull ``hold`` back out of a rule the matcher already matches.

        The hold scan looks for suffixes that START a rule, which can land
        inside a rule that is ALREADY complete earlier in the buffer:
        ``... سی بی سی و`` holds at the trailing ``و`` (the start of longer
        rules), cutting ``سی بی سی`` (CBC) into ``سی بی`` + ``سی``. Splitting
        a matched term is always wrong - a term cut in half canonicalizes to
        nothing - so the cut moves to the term's own start.

        Bounded by the longest rule, so the walk is O(max_rule_tokens).
        """
        if self._medical is None or hold <= 0:
            return hold
        span = min(self._medical.max_rule_tokens, hold)
        for size in range(span, 1, -1):
            start = hold - size + 1
            if start < 0:
                continue
            for end in range(hold + 1, min(len(tokens), start + size) + 1):
                if end - start < 2:
                    continue
                if self._medical.is_rule_token_prefix(tokens[start:end]) \
                        and not self._medical.is_strict_rule_token_prefix(
                            tokens[start:end]):
                    return start
        return hold

    def _charted_group_start(self, tokens: list[str], hold: int) -> int:
        """Pull ``hold`` back so a charted construct is never split in half.

        A cut is only legal between two complete constructs. Number/unit and
        ``LABEL: value`` formatting are applied to ONE emitted piece at a
        time, so a cut landing inside such a group ("Temp" | "36.7 \u00b0C",
        "97" | "%") leaves it permanently unformatted - and unit words are
        themselves dictionary rules, so the medical hold naturally wants to
        stop right in front of them.

        Walking back over the value tokens and then over the label in front
        of them keeps the whole group in the same emission. The walk is
        bounded by ``MAX_PENDING_TAIL_TOKENS``.
        """
        limit = max(0, hold - MAX_PENDING_TAIL_TOKENS)
        start = hold
        while start > limit and self._is_value_token(tokens[start - 1]):
            start -= 1
        for size in (3, 2, 1):
            if start - size < limit:
                continue
            if self._is_vital_label(tokens[start - size:start]):
                return start - size
        return start

    def _is_vital_label(self, tokens: list[str]) -> bool:
        """Whether ``tokens`` canonicalize into a charted vital-sign label.

        The lookup runs on the CANONICALIZED text because the matcher is
        what turns "\u062f\u0645\u0627\u06cc \u0628\u062f\u0646" into
        the label ``Temp``.
        """
        text = " ".join(tokens)
        if self._medical is not None:
            text, _ = self._medical.fst.canonicalize(text)
        return is_vital_label(text)

    def _is_value_token(self, token: str) -> bool:
        """Part of a charted value: a numeral, a unit, or a value connector."""
        if any(character.isdigit() for character in token):
            return True
        if token in _VALUE_CONNECTORS:
            return True
        if self._medical is None:
            return False
        canonical, _ = self._medical.fst.canonicalize(token)
        return canonical in _VALUE_UNIT_CANONICALS

    def _nursing_hold_index(self, tokens: list[str]) -> int:
        """First token the nursing stage may still need the next final for.

        ``len(tokens)`` (= hold nothing) when the buffer ends on a complete
        construct. Only meaningful while the nursing polish stage is on;
        with ``--no-text-polish`` nothing downstream would join the halves.
        """
        if self._medical is None or not self._medical.polish:
            return len(tokens)
        return min(
            len(tokens) - pending_tail_tokens(self._buffer),
            # A charted "LABEL: value" group sitting at the very end is also
            # unresolved: the next final may still carry the value's unit,
            # and the label must travel with it.
            self._charted_group_start(tokens, len(tokens)),
        )

    def _medical_hold_index(self, tokens: list[str]) -> int:
        """First token whose suffix could still complete a longer rule.

        A future final can only invalidate already-emitted text if some
        rule form's leading tokens match a suffix of the buffered text
        (that phrase could still complete across the boundary), so the
        emission stops right before the earliest such suffix.

        The whole-buffer case (``i == 0``) is special-cased with the STRICT
        prefix check (``is_strict_rule_token_prefix``): when the ENTIRE
        buffer already equals a complete, non-extendable rule by itself
        (e.g. the standalone abbreviation "iv" with no longer sibling rule
        "iv ..."), it has nothing left to grow into and must not be held
        back forever waiting for a continuation no rule defines - that
        buffer must be emitted rather than returning 0. For ``i > 0`` the
        plain (non-strict) check is kept: a mid-buffer suffix that merely
        equals some OTHER complete rule (e.g. the second token of a
        two-token compound like "رایت لانگ") can still be genuinely
        ambiguous with a sibling rule sharing that same tail token (e.g.
        "لانگ ساوندز"), so the whole compound must stay held together until
        the ambiguity resolves; only the never-completes-into-anything
        whole-buffer edge case is safe to special-case here.
        """
        if self._medical is None:
            return len(tokens)
        for i in range(len(tokens)):
            suffix = tokens[i:]
            if not self._medical.is_rule_token_prefix(suffix):
                continue
            if i == 0 and not self._medical.is_strict_rule_token_prefix(suffix):
                # The ENTIRE remaining buffer is a complete rule that no
                # longer rule can extend ("جی سی اس" = GCS, "لاین وریدی" =
                # IV line). Emit all of it. Scanning on instead used to find
                # a LATER suffix that merely starts some other rule and cut
                # THERE - i.e. in the middle of the rule that already
                # matched at i == 0, splitting "جی سی اس" into "جی سی" +
                # "اس" and losing the GCS canonicalization entirely.
                return len(tokens)
            return i
        return len(tokens)

    def _emit(self, cut: int) -> str | None:
        emitted_text = self._buffer[:cut].strip()
        if not emitted_text:
            return None

        # Walk the pieces against the cut: fully covered pieces are emitted
        # with their word evidence. A cut inside a piece emits that piece's
        # evidence with the text it annotates and keeps only its leftover
        # characters buffered (evidence is optional ASR annotation).
        words: list[dict] = []
        consumed = 0            # pieces fully inside the emitted text
        partial_index: int | None = None
        position = 0
        for index, piece in enumerate(self._pieces):
            end = position + len(piece["text"])
            if end <= cut:
                words.extend(piece["words"])
                consumed = index + 1
                position = end + 1  # +1: the joining space
            else:
                partial_index = index
                break

        if partial_index is not None:
            # The partial piece's evidence travels with its emitted text.
            words.extend(self._pieces[partial_index]["words"])

        if self._medical is None:
            canonical, hits = emitted_text, []
        else:
            canonical, hits = self._medical.canonicalize(emitted_text, words)

        remaining = self._pieces[consumed:]
        if partial_index is not None:
            partial = self._pieces[partial_index]
            leftover = self._buffer[cut:position + len(partial["text"])].strip()
            remaining = (
                ([{"text": leftover, "words": []}] if leftover else [])
                + self._pieces[partial_index + 1:]
            )
        self._pieces = remaining
        self._buffer = " ".join(piece["text"] for piece in self._pieces)
        self.parts.append(canonical)
        self.hits.extend(hits)
        return canonical


class InjectionWorker:
    """Serialized FIFO injection performed OFF the SDK receive thread.

    ``paste_text`` blocks on clipboard retries, modifier keys and the paste
    settle delay. It used to run inside the synchronous Speechmatics receive
    callback, stalling websocket message dispatch (partials/finals arrived
    late). Jobs are queued and executed in submission order, so final
    ordering is preserved exactly.
    """

    #: Upper bound for draining pending pastes at shutdown; pastes normally
    #: take ~0.3s each (settle delay), so this covers long sessions.
    SHUTDOWN_TIMEOUT_SECONDS = 60.0

    def __init__(self, injector, on_result=None) -> None:
        self._injector = injector
        self._on_result = on_result or (lambda record: None)
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self.records: list[dict] = []
        self._thread = threading.Thread(
            target=self._run, name="injection-worker", daemon=True
        )
        self._thread.start()

    def submit(self, canonical: str) -> None:
        self._queue.put(canonical)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self._injector.reset_partial()
            ok = self._injector.paste_text(item + " ", add_rtl_mark=True)
            record = {"text": item, "success": bool(ok)}
            self.records.append(record)
            self._on_result(record)

    def shutdown(self) -> None:
        """Stop after draining everything submitted so far."""
        self._queue.put(None)
        self._thread.join(timeout=self.SHUTDOWN_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            print(
                "[injector] warning: injection worker did not finish in time; "
                "injection results in the report may be incomplete"
            )


def print_cleanliness(label: str, text: str) -> dict:
    report = inspect_text(text).to_dict()
    print()
    print(f"{label} CLEANLINESS")
    print("-" * 72)
    print(f"Clean           : {'YES' if report['is_clean'] else 'NO'}")
    print(f"Arrows          : {report['arrows'] or 'none'}")
    print(f"Bidi marks      : {report['bidi_marks'] or 'none'}")
    print(f"Control chars   : {report['control_characters'] or 'none'}")
    print(f"Replacement char: {report['replacement_characters']}")
    print(f"ZWNJ            : {report['zwnj_count']}")
    print(f"Newlines        : {report['newline_count']}")
    print(f"Tabs            : {report['tab_count']}")
    print(f"Other symbols   : {report['non_ascii_symbols'] or 'none'}")
    return report


async def main() -> int:
    args = parse_args()
    api_key = os.getenv("SPEECHMATICS_API_KEY", "").strip()

    if not api_key:
        print("ERROR: SPEECHMATICS_API_KEY is missing from .env")
        return 1

    from speechmatics_test.microphone import MicrophoneRecorder
    from speechmatics_test.realtime import SpeechmaticsRealtime

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    json_path = (
        ROOT / "results" / f"session_{stamp}_{args.language}.json"
        if (args.save_report or args.test_id) else None
    )

    # --no-medical-layer must be fully independent of --no-vocab: it disables
    # the local Aho-Corasick canonicalization layer, so the dictionary is not
    # even loaded (no matcher build, no warnings, no additional_vocab). Do
    # NOT construct MedicalLayer(ROOT) unconditionally here - that would load
    # and compile the whole dictionary even when the flag says not to.
    medical = (
        None if args.no_medical_layer
        else MedicalLayer(ROOT, polish=not args.no_text_polish)
    )
    # Bounded Speechmatics vocabulary, derived once from the dictionary's
    # speechmatics-eligible entries (medical_knowledge/medical_dictionary.json
    # is the single source of truth; the generated
    # speechmatics_additional_vocab.json artifact mirrors it for inspection).
    # --no-vocab and --no-medical-layer are independently effective: vocab is
    # also empty when the medical layer itself is off (nothing to derive it
    # from), but --no-vocab alone leaves the local matcher/canonicalization
    # fully active.
    vocab = [] if (args.no_vocab or medical is None) else medical.additional_vocab
    benchmark = load_benchmark(args.test_id)
    overlay = create_overlay(args.no_overlay)
    injector = create_injector(args.inject)

    # The actually-sent Speechmatics domain (see resolve_domain: the medical
    # domain is only documented for a fixed language set, which does NOT
    # include Persian).
    effective_domain = resolve_domain(args.language, args.domain, args.model)

    result = None
    injected_segments: list[dict] = []

    print("=" * 72)
    print(" SwiftMedics / Speechmatics Mixed Medical ASR v4")
    print(" Aho-Corasick post-processing + automatic injection (no hotkeys)")
    print("=" * 72)
    print(f"Language stream : {args.language}")
    print(f"Domain          : {args.domain}" +
          (f" (sending: {effective_domain})" if effective_domain
           else " (sending: none)"))
    print(f"Model           : {args.model}")
    print(f"Max delay       : {args.max_delay:.1f}s ({args.max_delay_mode})")
    print(f"Medical vocab   : {'ON' if vocab else 'OFF'}")
    print(f"Matcher engine  : {medical.engine if medical is not None else 'OFF'}")
    print(f"Text polish     : "
          f"{'ON' if (medical is not None and medical.polish) else 'OFF'}")
    print(f"Device index    : {args.device_index if args.device_index is not None else 'default'}")
    print("Audio storage   : NONE (in-memory streaming only)")
    print(f"Report          : {json_path.name if json_path else 'not saved (use --save-report)'}")
    print(f"Max duration    : {args.max_seconds:.1f} sec")
    print(f"Auto-injection  : {'ON — every finalized segment is pasted at the cursor' if injector else 'OFF'}")
    print("=" * 72)
    if args.domain == "auto" and effective_domain is None:
        reason = (
            f"the Medical domain is a variant of the enhanced model, not "
            f"'{args.model}'"
            if args.model not in MEDICAL_DOMAIN_MODELS
            else f"Speechmatics does not document it for language "
                 f"'{args.language}'"
        )
        print(
            f"[config] The Enhanced Medical domain was not requested: {reason}"
            f" — continuing WITHOUT a domain on the {args.model} model. Use "
            f"--domain medical to force it explicitly."
        )
    if injector:
        print("Click the field where the transcript must go ONCE, then dictate.")
        print("Every finalized segment is pasted automatically. No keys to press.")
    print("Speak naturally. Press Ctrl+C to stop recording.")
    print()

    def on_partial(text: str):
        # UI/overlay only. Partials are never treated as final text.
        clean = normalize_text(text)
        print("\r[partial] " + clean[:200].ljust(200), end="", flush=True)
        if overlay:
            overlay.set_partial(clean)

    def on_injection_result(record: dict):
        # Runs on the injection worker thread.
        if overlay and record["success"]:
            overlay.set_done(record["text"])

    # One canonicalization state for injection AND the report: finals are
    # accumulated so medical phrases that span Speechmatics final-segment
    # boundaries canonicalize identically on both sides (see the class
    # docstring). Injection runs on a FIFO worker thread because paste_text
    # blocks (clipboard retries + settle delay) and must never stall the
    # synchronous SDK receive callback.
    accumulator = FinalStreamCanonicalizer(medical)
    worker = (
        InjectionWorker(injector, on_result=on_injection_result)
        if injector else None
    )
    def on_final(text: str):
        # Finalized segment. The pipeline is fixed and single-pass:
        #
        #   Speechmatics final -> normalize_text -> accumulated
        #     MedicalLayer.canonicalize (cross-segment safe)
        #       -> emitted canonical -> overlay.set_final
        #                           -> InjectionWorker.submit (ordered)
        #
        # Emitted canonical text is clean LOGICAL Unicode: it carries no
        # RLM/RLE/PDF. The overlay and the injector each add their own
        # presentation controls on top of it; neither rewrites the medical
        # content.
        clean = normalize_text(text)
        if not clean:
            return
        segment = stt.result.final_segments[-1]
        final_words = stt.result.word_results[
            segment["word_start_index"]:segment["word_end_index"]
        ]
        emitted = accumulator.add(clean, final_words)
        if emitted:
            print("\n[final]   " + emitted)
        else:
            print("\n[final]   (segment buffered — the next final may still "
                  "complete a cross-segment medical phrase)")
        if overlay:
            overlay.set_final(accumulator.canonical_text)

        if worker and emitted:
            worker.submit(emitted)

    def finish_injection() -> list[dict]:
        """Flush the canonical tail, drain the worker, return its records."""
        # Flush regardless of injection: the report's canonical stage is
        # built from these emissions, with or without a worker.
        tail = accumulator.flush()
        if tail:
            print("\n[final]   " + tail + "  (flushed at end of session)")
            if overlay:
                overlay.set_final(accumulator.canonical_text)
        if worker is None:
            return []
        if tail:
            worker.submit(tail)
        worker.shutdown()
        return worker.records

    # Ctrl+C must STOP THE RECORDING, not kill the program: everything after
    # this point (canonicalization, cleanliness, report) is exactly what the
    # user is dictating for. Previously SIGINT raised KeyboardInterrupt inside
    # the event loop, which escaped main() and threw the finished transcript
    # away.
    stop_event = threading.Event()
    loop = asyncio.get_running_loop()
    previous_sigint = None
    installed_signal_handler = False

    def request_stop() -> None:
        if not stop_event.is_set():
            stop_event.set()
            print("\n[session] Ctrl+C received - finishing the session "
                  "(transcript is preserved)...")

    try:
        loop.add_signal_handler(signal.SIGINT, request_stop)
        installed_signal_handler = True
    except (NotImplementedError, RuntimeError, ValueError):
        # Windows/ProactorEventLoop has no add_signal_handler: fall back to a
        # plain signal handler that is thread-safe enough for setting a flag.
        try:
            previous_sigint = signal.signal(
                signal.SIGINT, lambda *_: request_stop()
            )
            installed_signal_handler = True
        except (ValueError, OSError):
            previous_sigint = None

    try:
        try:
            # Constructed INSIDE the cleanup boundary: an initialization
            # failure (no device, PyAudio missing) must still remove the
            # SIGINT handler and close the overlay below.
            recorder = MicrophoneRecorder(device_index=args.device_index)
        except Exception as exc:
            print(f"\n[microphone error] {type(exc).__name__}: {exc}")
            print(
                "A microphone is required for dictation. Check that a device "
                "is connected and PyAudio is installed "
                "(Linux: sudo apt install portaudio19-dev python3-dev first)."
            )
            return 1
        with recorder:
            stt = SpeechmaticsRealtime(
                api_key=api_key,
                language=args.language,
                additional_vocab=vocab,
                max_delay=args.max_delay,
                model=args.model,
                max_delay_mode=args.max_delay_mode,
                domain=args.domain,
            )
            audio = audio_source(recorder, args.max_seconds, stop_event)
            # Arm the injection target BEFORE any audio is streamed. Arming
            # on the first emitted canonical text instead was unsafe: that
            # text can be buffered for several segments (cross-boundary
            # medical/nursing constructs), during which the user may have
            # focused a different window - and the guard would then protect
            # the WRONG one. The window focused when dictation starts is the
            # one the user clicked for dictation.
            if injector and not args.no_focus_guard:
                injector.arm_target()
            try:
                result = await stt.run(audio, on_partial, on_final)
            except KeyboardInterrupt:
                # Defensive: keep whatever the session already captured.
                print("\n[session] Ctrl+C received - stopping.")
                result = stt.result
            except Exception as exc:
                # A network/SDK failure must not discard an already dictated
                # transcript; report the error and continue to the report.
                print(f"\n[session error] {type(exc).__name__}: {exc}")
                result = stt.result
            finally:
                await audio.aclose()
                # Flush the buffered canonical tail even when the session
                # failed, drain the worker (ordered pastes), and collect the
                # injection records for the report — before the overlay and
                # the report are finalized.
                injected_segments = finish_injection()
    finally:
        if installed_signal_handler:
            try:
                loop.remove_signal_handler(signal.SIGINT)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
            if previous_sigint is not None:
                try:
                    signal.signal(signal.SIGINT, previous_sigint)
                except (ValueError, OSError):
                    pass
        if overlay:
            overlay.close()

    # ------------------------------------------------------------------ text
    # Stage 1: true Speechmatics RAW final transcript (no normalization).
    raw = (result.final_text or "").strip() if result is not None else ""
    # Stage 2: generic text normalization.
    normalized = normalize_text(raw)
    # Stage 3: deterministic medical Aho-Corasick canonicalization.
    #
    # The canonical stage comes from the SAME accumulated state that fed the
    # injection worker, so the report always matches the pasted text — even
    # when a medical phrase spans Speechmatics final-segment boundaries.
    # (Re-canonicalizing the joined transcript here used to disagree with the
    # injected segments.)
    word_results = getattr(result, "word_results", []) if result is not None else []
    canonical = accumulator.canonical_text
    medical_hits = accumulator.hits

    raw_clean = print_cleanliness("RAW TRANSCRIPT", raw)
    normalized_clean = print_cleanliness("NORMALIZED TRANSCRIPT", normalized)
    canonical_clean = print_cleanliness("CANONICALIZED TRANSCRIPT", canonical)

    evaluation_result = (
        # Stage names describe what the text ACTUALLY is. The last stage is
        # not purely the medical matcher's output: with the polish stage on
        # (the default) it has also been through nursing normalization, so
        # calling it "fst_canonical" misrepresented it.
        evaluate_stages(benchmark["expected"], {
            "raw": raw,
            "normalized": normalized,
            "canonical": canonical,
        })
        if benchmark else None
    )

    if json_path is not None:
        report = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "language": args.language,
            "test_id": args.test_id,
            "medical_vocab_enabled": bool(vocab),
            "medical_layer_enabled": medical is not None,
            "matcher_engine": medical.engine if medical is not None else None,
            # Keep the established top-level settings and add one compact,
            # self-contained Speechmatics block for benchmark comparisons.
            "model": args.model,
            "max_delay": args.max_delay,
            "max_delay_mode": args.max_delay_mode,
            "speechmatics": {
                # What was actually sent (None = no domain key was sent).
                "domain": effective_domain,
                "domain_requested": args.domain,
                "model": args.model,
                "max_delay": args.max_delay,
                "max_delay_mode": args.max_delay_mode,
            },
            "device_index": args.device_index,
            "first_partial_latency_ms": getattr(result, "first_partial_ms", None),
            "session_error": getattr(result, "error", None),
            "parse_warnings": getattr(result, "warnings", []) if result else [],
            "audio_overflow_events": getattr(recorder, "overflow_events", None),
            "partials": getattr(result, "partials", []),
            "final_segments": getattr(result, "final_segments", []),
            "word_results": word_results,
            "confidence_summary": confidence_summary(word_results),
            # "changed" means the LEXICAL medical stage actually rewrote
            # something (a dictionary hit was applied). It used to be
            # `canonical != normalized`, which is also true when only the
            # nursing stage reformatted a number ("سی و پنج" -> "35") and
            # no medical term was involved at all.
            "medical_canonicalization": {
                "hit_count": len(medical_hits),
                "changed": (
                    medical is not None
                    and medical.medical_replacement_count > 0
                ),
                "replacement_count": (
                    medical.medical_replacement_count
                    if medical is not None else 0
                ),
            },
            "final_transcript_raw": raw,
            "final_transcript_normalized": normalized,
            "final_transcript_canonical": canonical,
            "medical_hits": medical_hits,
            "medical_warnings": medical.warnings if medical is not None else [],
            # Deterministic nursing-text stage: what it did, and every
            # ambiguity it refused to resolve (e.g. a numeral left unbound
            # next to a clock time) instead of guessing.
            "text_polish_enabled": bool(medical is not None and medical.polish),
            "text_polish_warnings": (
                medical.polish_warnings if medical is not None else []
            ),
            "cleanliness": {
                "raw": raw_clean,
                "normalized": normalized_clean,
                "canonical": canonical_clean,
            },
            "injection": {
                "auto": True,
                "enabled": bool(injector),
                "segments": injected_segments,
            },
            "benchmark_expected": benchmark["expected"] if benchmark else None,
            "evaluation": evaluation_result,
        }
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if injector and injected_segments:
        n_ok = sum(1 for s in injected_segments if s["success"])
        print()
        print(f"[injector] auto-injected {n_ok}/{len(injected_segments)} finalized segments.")

    print()
    print("=" * 72)
    print("FINAL RAW:")
    print(raw or "[empty]")
    print()
    print("FINAL NORMALIZED:")
    print(normalized or "[empty]")
    print()
    print("FINAL CANONICAL:")
    print(canonical or "[empty]")

    if evaluation_result:
        print()
        print("EVALUATION (raw / normalized / canonical):")
        print(json.dumps(evaluation_result, ensure_ascii=False, indent=2))

    print()
    if json_path is not None:
        print(f"JSON: {json_path}")
    else:
        print("No report saved (use --save-report or --test-id to save).")

    # The transcript, the injection and the report are all preserved above
    # even when the realtime session failed - but the PROCESS must not claim
    # success. A script that pipes dictation into a chart had no way to tell
    # a clean session apart from one that died on an expired API key.
    session_error = getattr(result, "error", None) if result is not None else None
    if session_error:
        print()
        print(f"[session] FAILED: {session_error}")
        print("[session] The transcript captured before the failure was kept "
              "(see above / the report JSON), but the exit code reports the "
              "failure.")
        return 2

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0)
