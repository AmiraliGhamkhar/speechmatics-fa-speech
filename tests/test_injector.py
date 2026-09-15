"""TextInjector tests (platform-independent parts).

The injector had no test coverage at all, even though it is the component
that actually writes into the clinician's EMR field. These tests pin the
pure-logic behaviour that is reachable off-Windows: streaming revision
handling, UTF-16 backspace counting, grapheme-safe split points, and the
dry-run contract.
"""

import injector as injector_module
from injector import TextInjector


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
    """Regression: append-only mode used to freeze after the first revision.

    ``_last_partial`` was only updated on the pure-append path, so once the
    STT revised a word every later partial was diffed against stale text and
    nothing was ever typed again - dictation died silently mid-sentence.
    """
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


# ------------------------------------------------------------------ dry run

def test_dry_run_never_touches_the_system(capsys):
    inj = make_injector()
    assert inj.paste_text("سلام CT scan") is True
    assert inj.type_text("abc") is True
    assert inj.send_backspaces(3) is True
    out = capsys.readouterr().out
    assert "DRY_RUN paste" in out and "DRY_RUN type" in out


def test_empty_text_is_rejected():
    inj = make_injector()
    assert inj.paste_text("") is False
    assert inj.type_text("") is False
    # zero/negative backspaces are a no-op success
    assert inj.send_backspaces(0) is True


def test_rtl_mark_is_opt_in_and_not_duplicated(capsys):
    inj = make_injector()
    inj.paste_text("سلام", add_rtl_mark=True)
    first = capsys.readouterr().out
    assert "\\u200f" in first or "\u200f" in first

    # Already-marked text must not get a second RLM
    inj.paste_text("\u200fسلام", add_rtl_mark=True)
    second = capsys.readouterr().out
    assert second.count("\\u200f") <= 1


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
