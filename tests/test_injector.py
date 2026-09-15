"""TextInjector tests (platform-independent parts).

Covers the streaming revision logic, UTF-16 backspace counting,
grapheme-safe split points, the dry-run contract, the mixed Persian/English
payload preparation (spacing/ZWNJ cleanup + RLE/PDF/RLM BiDi wrap), and
thread-safety plumbing.
"""

import threading

import injector as injector_module
from injector import RLE, RLM, PDF, TextInjector, clean_payload_spacing, contains_rtl


def make_injector(**kwargs):
    kwargs.setdefault("dry_run", True)
    return TextInjector(**kwargs)


class RecordingInjector(TextInjector):
    """Captures what would have been typed/erased instead of sending input."""

    def __init__(self, **kwargs):
        kwargs.setdefault("dry_run", False)
        super().__init__(**kwargs)
        self.typed = []
        self.backspaces = []

    def type_text(self, text: str) -> bool:
        self.typed.append(text)
        return True

    def send_backspaces(self, count: int) -> bool:
        self.backspaces.append(count)
        return True


# ------------------------------------------------------------- utf16 lengths

def test_utf16_len_bmp():
    assert TextInjector._utf16_len("abc") == 3
    # Persian letters are BMP: one UTF-16 unit each
    assert TextInjector._utf16_len("سلام") == 4


def test_utf16_len_counts_surrogate_pairs_as_two():
    """Windows deletes UTF-16 code units, not Python code points."""
    emoji = "\U0001F600"  # non-BMP -> surrogate pair
    assert len(emoji) == 1
    assert TextInjector._utf16_len(emoji) == 2


# ------------------------------------------------------- streaming revisions

def test_append_only_delta():
    inj = RecordingInjector()
    inj.type_delta_from_partial("hello")
    inj.type_delta_from_partial("hello world")
    assert inj.typed == ["hello", " world"]
    assert inj.backspaces == []


def test_revision_backspaces_only_the_changed_tail():
    inj = RecordingInjector()
    inj.type_delta_from_partial("hello world")
    inj.type_delta_from_partial("hello word")
    # common prefix "hello wor" -> erase "ld" (2), type "d"
    assert inj.backspaces == [2]
    assert inj.typed[-1] == "d"


def test_identical_partial_is_a_noop():
    inj = RecordingInjector()
    inj.type_delta_from_partial("same")
    inj.typed.clear()
    inj.type_delta_from_partial("same")
    assert inj.typed == []
    assert inj.backspaces == []


def test_reset_partial_clears_streaming_state():
    inj = RecordingInjector()
    inj.type_delta_from_partial("abc")
    inj.reset_partial()
    inj.type_delta_from_partial("abc")
    # after a reset the next hypothesis is typed in full, with no backspaces
    assert inj.typed == ["abc", "abc"]
    assert inj.backspaces == []


def test_append_only_mode_recovers_after_a_revision():
    """Regression: append-only mode used to freeze after the first revision."""
    inj = RecordingInjector(enable_smart_rewrite=False)
    inj.type_delta_from_partial("hello wor")
    inj.type_delta_from_partial("hello world")
    inj.type_delta_from_partial("hello word")        # revision: cannot append
    inj.type_delta_from_partial("hello word today")  # must resume appending

    assert inj.backspaces == []          # append-only never erases
    assert inj.typed[-1] == " today"     # streaming resumed
    assert inj._last_partial == "hello word today"


def test_append_only_mode_never_sends_backspaces():
    inj = RecordingInjector(enable_smart_rewrite=False)
    for partial in ["aaa", "bbb", "ccc ddd", "ccc"]:
        inj.type_delta_from_partial(partial)
    assert inj.backspaces == []


def test_streaming_is_serialized_on_the_lock():
    """Realtime callbacks from another thread must not interleave."""
    inj = RecordingInjector()
    threads = [
        threading.Thread(target=inj.type_delta_from_partial, args=(f"word {i} جمله",))
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # no exception, and state is coherent (last writer wins)
    assert inj._last_partial == "word 7 جمله"


# --------------------------------------------------------- grapheme safety

def test_split_point_does_not_orphan_zwnj():
    """A ZWNJ boundary must not be cut in half (Persian نیم‌فاصله)."""
    old = "می\u200cرود"   # م ی ZWNJ ر و د
    new = "می\u200cرویم"
    # Index 3 sits immediately after the ZWNJ: splitting there would orphan
    # the joiner, so the boundary must be pulled back before it.
    idx = TextInjector._safe_split_point(old, new, 3)
    assert idx < 2
    assert "\u200c" not in old[:idx]


def test_split_point_keeps_boundaries_away_from_zwnj_on_both_sides():
    old = "می\u200cرود"
    for index in (2, 3):
        idx = TextInjector._safe_split_point(old, old, index)
        # never leaves a split directly adjacent to the joiner
        assert idx < 2


def test_split_point_allows_plain_append():
    """`"" in combining` is True in Python: the empty end-of-string case
    must not be treated as a mid-grapheme cut (it emitted a bogus backspace).
    """
    assert TextInjector._safe_split_point("abc", "abcd", 3) == 3


def test_plain_append_emits_no_backspace():
    inj = RecordingInjector()
    inj.type_delta_from_partial("abc")
    inj.type_delta_from_partial("abcd")
    assert inj.backspaces == []
    assert inj.typed[-1] == "d"


def test_split_point_does_not_split_surrogate_pair():
    old = "a\U0001F600b"
    new = "a\U0001F600c"
    # index 2 sits right after the astral char (a single Python code point)
    assert TextInjector._safe_split_point(old, new, 2) == 2


# ----------------------------------------------------- payload preparation

def test_clean_payload_spacing_collapses_whitespace():
    assert clean_payload_spacing("سلام   دنیا\n\tاینجا") == "سلام دنیا اینجا"
    assert clean_payload_spacing("  a   b  ") == "a b"


def test_clean_payload_spacing_fixes_zwnj():
    # ASR loves emitting "می ‌خواهم" with a stray space by the ZWNJ
    assert clean_payload_spacing("می \u200cخواهم") == "می\u200cخواهم"
    assert clean_payload_spacing("می\u200c خواهم") == "می\u200cخواهم"
    # stray ZWNJ at the edges is dropped
    assert clean_payload_spacing("\u200cسلام\u200c") == "سلام"


def test_contains_rtl():
    assert contains_rtl("سلام CT scan")
    assert not contains_rtl("CT scan 120/80")


def test_prepare_mixed_text_wraps_rtl_payload():
    inj = make_injector()
    out = inj.prepare_mixed_text("بیمار در CCU است")
    assert out == RLM + RLE + "بیمار در CCU است" + PDF


def test_prepare_mixed_text_leaves_ltr_payload_unwrapped():
    inj = make_injector()
    assert inj.prepare_mixed_text("CT scan done") == "CT scan done"


def test_prepare_mixed_text_keeps_trailing_space_inside_wrap():
    inj = make_injector()
    out = inj.prepare_mixed_text("سلام دنیا ")
    # trailing separator preserved, inside the embedding
    assert out == RLM + RLE + "سلام دنیا " + PDF


def test_prepare_mixed_text_is_idempotent():
    inj = make_injector()
    once = inj.prepare_mixed_text("بیمار CT scan")
    twice = inj.prepare_mixed_text(once)
    assert once == twice
    assert once.count(RLM) == 1 and once.count(RLE) == 1 and once.count(PDF) == 1


def test_prepare_mixed_text_can_disable_bidi_marks():
    inj = make_injector(add_bidi_marks=False)
    assert inj.prepare_mixed_text("سلام") == "سلام"


# ------------------------------------------------------------------ dry run

def test_dry_run_never_touches_the_system(capsys):
    inj = make_injector()
    assert inj.paste_text("سلام CT scan") is True
    assert inj.type_text("abc") is True
    assert inj.send_backspaces(3) is True
    out = capsys.readouterr().out
    assert "DRY_RUN paste" in out and "DRY_RUN type" in out


def test_dry_run_paste_shows_bidi_wrapped_payload(capsys):
    """The pasted payload visibly includes the RLE/PDF/RLM marks."""
    inj = make_injector()
    inj.paste_text("بیمار در CCU است")
    out = capsys.readouterr().out
    assert "\\u202b" in out and "\\u202c" in out and "\\u200f" in out


def test_empty_text_is_rejected():
    inj = make_injector()
    assert inj.paste_text("") is False
    assert inj.type_text("") is False
    # zero/negative backspaces are a no-op success
    assert inj.send_backspaces(0) is True


def test_rtl_mark_is_not_duplicated():
    """Already-marked text must not get a second RLM."""
    inj = make_injector()
    payload = inj.prepare_mixed_text("\u200fسلام")
    assert payload.count(RLM) == 1


def test_injector_does_not_add_arrows(capsys):
    """The injector must paste exactly what it is given (no decoration)."""
    payload = "در CT scan یک lesion در right lung"
    inj = make_injector()
    inj.paste_text(payload)
    out = capsys.readouterr().out
    for arrow in ("→", "←", "->", "=>"):
        assert arrow not in out


def test_foreground_window_info_is_safe_off_windows():
    inj = make_injector()
    if injector_module._SYSTEM != "windows":
        info = inj.get_foreground_window_info()
        assert info == {"hwnd": None, "title": "", "pid": None}
