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


# ------------------------------------------------ clipboard restoration (M1)

def make_mock_windows_injector(monkeypatch, previous="user data",
                               set_lands=True, set_results=None):
    """A TextInjector with the Win32 clipboard/keystroke calls faked.

    ``state["current"]`` simulates the real clipboard: ``fake_set`` updates
    it only when the set succeeds AND ``set_lands`` is true (a set can
    "succeed" while the content silently never lands for verification).
    """
    inj = TextInjector(restore_clipboard=True, paste_settle_seconds=0)
    state = {"sets": [], "keystrokes": 0, "current": previous}
    results = list(set_results if set_results is not None else [True, True])

    def fake_set(text):
        state["sets"].append(text)
        ok = results.pop(0) if results else True
        if ok and set_lands:
            state["current"] = text
        return ok

    monkeypatch.setattr(inj, "_get_windows_clipboard", lambda: state["current"])
    monkeypatch.setattr(inj, "_focus_guard_ok", lambda: True)
    monkeypatch.setattr(inj, "_set_windows_clipboard", fake_set)
    def fake_keystroke():
        state["keystrokes"] += 1
        return True

    monkeypatch.setattr(inj, "_send_paste_keystroke", fake_keystroke)
    return inj, state


def test_paste_windows_restores_clipboard_after_successful_paste(monkeypatch):
    inj, state = make_mock_windows_injector(monkeypatch, previous="user data")
    assert inj._paste_windows("سلام") is True
    assert state["keystrokes"] == 1
    # last clipboard write restored the user's previous content
    assert state["sets"][-1] == "user data"
    assert state["sets"][0] == "سلام"


def test_paste_windows_restores_clipboard_when_keystroke_fails(monkeypatch):
    inj, state = make_mock_windows_injector(monkeypatch, previous="user data")
    monkeypatch.setattr(inj, "_send_paste_keystroke", lambda: False)
    assert inj._paste_windows("سلام") is False
    # the failed paste must not leave transcript data in the clipboard
    assert state["sets"][-1] == "user data"


def test_paste_windows_restores_clipboard_on_verification_failure(monkeypatch):
    # sets report success but the content never actually lands, so the
    # read-back verification fails after the retry -> paste must fail yet
    # still restore the user's previous clipboard
    inj, state = make_mock_windows_injector(
        monkeypatch, previous="user data", set_lands=False
    )
    monkeypatch.setattr(injector_module.time, "sleep", lambda s: None)
    assert inj._paste_windows("سلام") is False
    assert state["sets"] == ["سلام", "سلام", "user data"]


def test_paste_windows_does_not_restore_when_setting_fails(monkeypatch):
    inj, state = make_mock_windows_injector(
        monkeypatch, previous="user data", set_results=[False]
    )
    assert inj._paste_windows("سلام") is False
    # the clipboard was never changed, so there is nothing to restore
    assert state["sets"] == ["سلام"]


def test_paste_windows_restores_clipboard_after_partial_set_failure(monkeypatch):
    """Regression: ``EmptyClipboard()`` can succeed and then a LATER step
    inside ``_set_windows_clipboard`` (GlobalAlloc/GlobalLock/
    SetClipboardData) can fail. That function then returns False, but the
    previous clipboard content is ALREADY destroyed - restoration must
    still happen instead of being skipped because the setter "failed".
    """
    inj = TextInjector(restore_clipboard=True, paste_settle_seconds=0)
    state = {"sets": [], "current": "user data"}

    def fake_partial_failure_set(text):
        # Simulate the real _set_windows_clipboard: EmptyClipboard()
        # succeeds (destroying the previous content, hence marking
        # _clipboard_touched) but a later step fails, so it returns False
        # without ever making `text` the new clipboard content.
        state["sets"].append(text)
        inj._clipboard_touched = True
        state["current"] = None  # EmptyClipboard() succeeded: now empty
        return False

    monkeypatch.setattr(inj, "_get_windows_clipboard", lambda: state["current"])
    monkeypatch.setattr(inj, "_focus_guard_ok", lambda: True)
    monkeypatch.setattr(inj, "_set_windows_clipboard", fake_partial_failure_set)

    assert inj._paste_windows("سلام") is False
    # restoration must still have been attempted despite the setter
    # returning False on its first (and only) call
    assert state["sets"] == ["سلام", "user data"]


def test_paste_fallback_restores_clipboard_when_hotkey_fails(monkeypatch):
    import sys
    calls = {"copies": [], "hotkeys": 0}

    fake_pyperclip = type(sys)("fake_pyperclip")
    fake_pyperclip.paste = lambda: "user data"
    fake_pyperclip.copy = lambda text: calls["copies"].append(text)

    fake_pyautogui = type(sys)("fake_pyautogui")
    def broken_hotkey(*args):
        calls["hotkeys"] += 1
        raise RuntimeError("no display")
    fake_pyautogui.hotkey = broken_hotkey

    monkeypatch.setitem(sys.modules, "pyautogui", fake_pyautogui)
    monkeypatch.setitem(sys.modules, "pyperclip", fake_pyperclip)
    monkeypatch.setattr(injector_module.time, "sleep", lambda s: None)

    inj = TextInjector(restore_clipboard=True, paste_settle_seconds=0)
    assert inj._paste_fallback("سلام") is False
    assert calls["hotkeys"] == 1
    # restoration still ran after the hotkey raised
    assert calls["copies"] == ["سلام", "user data"]


# -------------------------------------------------- modifier hygiene (M2)

def test_paste_key_sequence_skips_ctrl_when_user_holds_it():
    # user-held Ctrl: only V is tapped; no synthetic Ctrl down/up may
    # corrupt the logical modifier state
    assert injector_module.TextInjector._paste_key_sequence(True) == [
        "v_down", "v_up",
    ]


def test_paste_key_sequence_full_when_ctrl_is_up():
    assert injector_module.TextInjector._paste_key_sequence(False) == [
        "ctrl_down", "v_down", "v_up", "ctrl_up",
    ]


def test_control_is_not_treated_as_an_interfering_modifier():
    # Ctrl is handled by the paste sequence itself, never released/restored
    assert "VK_CONTROL" not in TextInjector._INTERFERING_MODIFIERS


# ------------------------------------------------------ focus guard (H4)

def test_arm_target_arms_current_foreground_window(monkeypatch):
    inj = TextInjector(paste_settle_seconds=0)
    monkeypatch.setattr(
        inj, "get_foreground_window_info",
        lambda: {"hwnd": 4321, "title": "EMR", "pid": 7},
    )
    assert inj.arm_target() is True
    assert inj.armed_target == 4321
    assert inj._focus_guard_ok() is True


def test_focus_guard_blocks_when_another_window_is_focused(monkeypatch):
    inj = TextInjector(paste_settle_seconds=0)
    monkeypatch.setattr(
        inj, "get_foreground_window_info",
        lambda: {"hwnd": 4321, "title": "EMR", "pid": 7},
    )
    inj.arm_target()
    monkeypatch.setattr(
        inj, "get_foreground_window_info",
        lambda: {"hwnd": 99, "title": "Terminal", "pid": 2},
    )
    assert inj._focus_guard_ok() is False


def test_paste_windows_aborts_before_touching_the_clipboard(monkeypatch):
    inj, state = make_mock_windows_injector(monkeypatch, previous="user data")
    monkeypatch.setattr(inj, "_focus_guard_ok", lambda: False)
    assert inj._paste_windows("سلام") is False
    assert state["sets"] == []
    assert state["keystrokes"] == 0


def test_paste_windows_rechecks_focus_immediately_before_keystroke(monkeypatch):
    inj, state = make_mock_windows_injector(monkeypatch, previous="user data")
    decisions = iter([True, False])
    monkeypatch.setattr(inj, "_focus_guard_ok", lambda: next(decisions))
    assert inj._paste_windows("سلام") is False
    assert state["keystrokes"] == 0
    # Clipboard was touched but the user's value was restored on refusal.
    assert state["sets"][-1] == "user data"


def test_unarmed_injector_is_never_blocked(monkeypatch):
    inj, state = make_mock_windows_injector(monkeypatch, previous="user data")
    assert inj.armed_target is None
    assert inj._paste_windows("سلام") is True


# ------------------------- required focus guard (Session 5 fix)

def test_required_guard_refuses_paste_until_a_target_is_armed(monkeypatch):
    """With the guard required, "not armed YET" must mean "paste nowhere" -
    not "paste into whatever is focused" (which is the SwiftMedics console
    while the user has not clicked a target yet)."""
    inj = TextInjector(restore_clipboard=True, paste_settle_seconds=0)
    state = {"current": "user data", "sets": []}

    def fake_set(text):
        state["sets"].append(text)
        state["current"] = text
        return True

    monkeypatch.setattr(inj, "_get_windows_clipboard",
                        lambda: state["current"])
    monkeypatch.setattr(inj, "_set_windows_clipboard", fake_set)
    monkeypatch.setattr(inj, "_send_paste_keystroke", lambda: True)
    monkeypatch.setattr(
        inj, "get_foreground_window_info",
        lambda: {"hwnd": 111, "title": "Console", "pid": 1},
    )

    inj.enable_focus_guard()
    assert inj.armed_target is None
    assert inj._focus_guard_ok() is False
    # refused BEFORE the clipboard is touched at all
    assert inj._paste_windows("سلام") is False
    assert state["sets"] == []

    # after arming (the armed window is focused) the paste proceeds
    inj.arm_target()
    assert inj.armed_target == 111
    assert inj._paste_windows("سلام") is True
    assert state["sets"][0] == "سلام"


def test_required_guard_refusal_is_explained_to_the_user(monkeypatch, capsys):
    inj = TextInjector(paste_settle_seconds=0)
    monkeypatch.setattr(
        inj, "get_foreground_window_info",
        lambda: {"hwnd": 111, "title": "Console", "pid": 1},
    )
    inj.enable_focus_guard()
    assert inj._focus_guard_ok() is False
    out = capsys.readouterr().out
    assert "no paste target armed yet" in out
    assert "click the field" in out


def test_guard_not_required_keeps_the_permissive_legacy_behavior(monkeypatch):
    """--no-focus-guard / non-Windows: unarmed still means permissive."""
    inj = TextInjector(paste_settle_seconds=0)
    monkeypatch.setattr(
        inj, "get_foreground_window_info",
        lambda: {"hwnd": 111, "title": "Console", "pid": 1},
    )
    assert inj._guard_required is False
    assert inj._focus_guard_ok() is True
    # arming still works and still guards once armed (legacy contract)
    inj.arm_target()
    assert inj._focus_guard_ok() is True


# ------------------------------------------ TargetSelector (Session 5 fix)

class FakeWindowInjector:
    """Injector stand-in with a scripted/mutable foreground window."""

    def __init__(self, foreground):
        self.foreground = dict(foreground)
        self.armed = None
        self.arm_calls = 0

    def get_foreground_window_info(self):
        return dict(self.foreground)

    def arm_target(self):
        self.arm_calls += 1
        hwnd = self.foreground.get("hwnd")
        if not hwnd:
            return False
        self.armed = int(hwnd)
        return True


def run_selector(selector, timeout=5.0):
    """Wait until the selector armed something (it runs on its own thread)."""
    assert selector.wait(timeout), "the selector did not arm a target"
    selector.stop()


def test_target_selector_arms_the_first_foreign_window():
    """Console foreground at start -> user clicks Word (hwnd 222, another
    process) -> THAT window is armed. This is the exact regression behind
    'armed hwnd != current hwnd' / 'auto-injected 0/34'."""
    from injector import TargetSelector

    fake = FakeWindowInjector({"hwnd": 111, "title": "Console", "pid": 1})
    selector = TargetSelector(fake, poll_seconds=0.01)
    assert selector.start() is True
    assert selector.armed is False
    # the user clicks the external target
    fake.foreground = {"hwnd": 222, "title": "Document - Word", "pid": 999}
    run_selector(selector)

    assert fake.armed == 222          # the external window, never the console
    assert fake.arm_calls == 1        # armed exactly once


def test_target_selector_ignores_windows_of_its_own_process():
    """A click on the always-on-top overlay (own PID) must not arm it."""
    from injector import TargetSelector

    fake = FakeWindowInjector({"hwnd": 111, "title": "Console", "pid": 1})
    selector = TargetSelector(fake, poll_seconds=0.01)
    assert selector.start() is True
    # overlay window: different hwnd but OUR process
    fake.foreground = {"hwnd": 555, "title": "SwiftMedics STT Overlay",
                       "pid": __import__("os").getpid()}
    selector.wait(0.2)
    assert fake.armed is None
    # the user then clicks the real target
    fake.foreground = {"hwnd": 222, "title": "EMR", "pid": 42}
    run_selector(selector)
    assert fake.armed == 222


def test_target_selector_ignores_focus_returning_to_the_console():
    from injector import TargetSelector

    fake = FakeWindowInjector({"hwnd": 111, "title": "Console", "pid": 1})
    selector = TargetSelector(fake, poll_seconds=0.01)
    assert selector.start() is True
    fake.foreground = {"hwnd": 111, "title": "Console", "pid": 1}  # no change
    selector.wait(0.2)
    assert fake.armed is None
    fake.foreground = {"hwnd": 333, "title": "Browser", "pid": 7}
    run_selector(selector)
    assert fake.armed == 333


def test_target_selector_start_fails_without_a_foreground_window():
    from injector import TargetSelector

    fake = FakeWindowInjector({"hwnd": None, "title": "", "pid": None})
    selector = TargetSelector(fake, poll_seconds=0.01)
    # no HWND API (non-Windows): selection unavailable, nothing started
    assert selector.start() is False
    assert selector.armed is False
    selector.stop()  # must be a safe no-op


def test_target_selector_stop_ends_the_watch_without_arming():
    from injector import TargetSelector

    fake = FakeWindowInjector({"hwnd": 111, "title": "Console", "pid": 1})
    selector = TargetSelector(fake, poll_seconds=0.01)
    assert selector.start() is True
    selector.stop()
    assert selector._thread is not None
    selector._thread.join(timeout=2.0)
    assert not selector._thread.is_alive()
    assert fake.armed is None
    # late foreign focus must not arm anything after stop()
    fake.foreground = {"hwnd": 222, "title": "Word", "pid": 9}
    selector.wait(0.1)
    assert fake.armed is None


def test_target_selector_is_one_shot_after_arming():
    """First transition wins: later focus changes never re-arm (the guard
    protects the armed target for the whole session)."""
    from injector import TargetSelector

    fake = FakeWindowInjector({"hwnd": 111, "title": "Console", "pid": 1})
    selector = TargetSelector(fake, poll_seconds=0.01)
    assert selector.start() is True
    fake.foreground = {"hwnd": 222, "title": "Word", "pid": 9}
    run_selector(selector)
    fake.foreground = {"hwnd": 333, "title": "Mail", "pid": 10}
    selector.wait(0.1)
    assert fake.armed == 222
    assert fake.arm_calls == 1


# --------------------------- non-text clipboard notice (Session 5 fix)

def test_paste_warns_once_when_clipboard_content_is_not_restorable(monkeypatch,
                                                                   capsys):
    """A non-text clipboard (image/files) cannot be restored: the loss must
    be reported once, not silently discovered later."""
    inj, state = make_mock_windows_injector(monkeypatch, previous=None)
    monkeypatch.setattr(inj, "_clipboard_has_content", lambda: True)
    assert inj._paste_windows("سلام") is True
    assert inj._paste_windows("دوم") is True
    out = capsys.readouterr().out
    assert out.count("non-text clipboard content") == 1
    assert "cannot be restored" in out


def test_paste_does_not_warn_when_the_clipboard_is_empty(monkeypatch, capsys):
    inj, state = make_mock_windows_injector(monkeypatch, previous=None)
    monkeypatch.setattr(inj, "_clipboard_has_content", lambda: False)
    assert inj._paste_windows("سلام") is True
    assert "non-text clipboard" not in capsys.readouterr().out


def test_paste_with_unreadable_clipboard_does_not_restore_anything(monkeypatch):
    """previous=None (unreadable/non-text): no restoration write happens."""
    inj, state = make_mock_windows_injector(monkeypatch, previous=None)
    monkeypatch.setattr(inj, "_clipboard_has_content", lambda: True)
    assert inj._paste_windows("سلام") is True
    # only the transcript text was ever written; nothing was "restored"
    assert state["sets"] == ["سلام"]
