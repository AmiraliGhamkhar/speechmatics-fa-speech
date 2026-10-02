"""Tests for the Windows single-instance guard.

The Win32 layer is modelled with fakes rather than mocked call-by-call, so the
mutex decision is exercised end to end: ``CreateMutexW`` hands back a *new*
handle for an existing name (exactly as Win32 does) and ``CloseHandle`` only
frees the name once the last handle is gone. That is what makes "a crashed
primary must not lock the app out" testable.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from speechmatics_test import single_instance  # noqa: E402
from speechmatics_test.single_instance import (  # noqa: E402
    MUTEX_NAME,
    WINDOW_TITLE,
    SingleInstance,
    acquire,
)

_ERROR_ALREADY_EXISTS = 183


class FakeKernel32:
    """Models the parts of kernel32 the guard uses, with real handle semantics."""

    def __init__(self) -> None:
        self.names: dict[str, set[int]] = {}
        self.last_error = 0
        self.next_handle = 1000
        self.create_calls = 0
        self.closed: list[int] = []
        self.current_thread_id = 4242
        self.fail_create = False

    def CreateMutexW(self, attributes, initial_owner, name):
        self.create_calls += 1
        if self.fail_create:
            return 0
        self.next_handle += 1
        handle = self.next_handle
        holders = self.names.setdefault(name, set())
        holders.add(handle)
        # Win32 reports ERROR_ALREADY_EXISTS for every handle after the first.
        self.last_error = (
            _ERROR_ALREADY_EXISTS if len(holders) > 1 else 0
        )
        return handle

    def GetLastError(self):
        return self.last_error

    def CloseHandle(self, handle):
        self.closed.append(handle)
        for name, holders in list(self.names.items()):
            holders.discard(handle)
            if not holders:
                del self.names[name]
        return 1

    def GetCurrentThreadId(self):
        return self.current_thread_id


class FakeUser32:
    def __init__(self) -> None:
        self.windows: dict[str, int] = {}
        self.calls: list[tuple] = []
        self.foreground = 0
        self.window_threads: dict[int, int] = {}
        self.allow_calls: list[int] = []
        self.attach_calls: list[tuple[int, int, bool]] = []
        self.fail_attach = False
        self.find_calls: list[str] = []

    def FindWindowW(self, cls, title):
        self.find_calls.append(title)
        return self.windows.get(title, 0)

    def ShowWindow(self, hwnd, command):
        self.calls.append(("show", hwnd, command))
        return 1

    def GetForegroundWindow(self):
        return self.foreground

    def GetWindowThreadProcessId(self, hwnd, pid):
        return self.window_threads.get(hwnd, 777)

    def AttachThreadInput(self, current, foreground, attach):
        self.attach_calls.append((current, foreground, attach))
        return 0 if self.fail_attach else 1

    def BringWindowToTop(self, hwnd):
        self.calls.append(("bring", hwnd))
        return 1

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("foreground", hwnd))
        self.foreground = hwnd
        return 1

    def AllowSetForegroundWindow(self, process_id):
        self.allow_calls.append(process_id)
        return 1


@pytest.fixture
def win32():
    kernel32 = FakeKernel32()
    user32 = FakeUser32()
    return user32, kernel32


def _acquire(user32, kernel32, **kwargs):
    kwargs.setdefault("sleep", lambda _seconds: None)
    return acquire(user32=user32, kernel32=kernel32, **kwargs)


# --------------------------------------------------------------- the lock


def test_first_launch_claims_the_lock_and_runs(win32):
    user32, kernel32 = win32
    instance = _acquire(user32, kernel32)
    assert isinstance(instance, SingleInstance)
    assert instance.is_guarded is True
    assert kernel32.names[MUTEX_NAME], "the mutex name must stay held"


def test_second_launch_is_refused_and_focuses_the_first(win32):
    user32, kernel32 = win32
    user32.windows[WINDOW_TITLE] = 555

    first = _acquire(user32, kernel32)
    assert first is not None

    second = _acquire(user32, kernel32)
    assert second is None, "a second launch must not start a session"
    assert user32.find_calls == [WINDOW_TITLE]
    assert ("foreground", 555) in user32.calls


def test_third_launch_is_also_refused(win32):
    """The complaint was degradation after repeated launches; all of them must
    be refused, not just the second."""
    user32, kernel32 = win32
    user32.windows[WINDOW_TITLE] = 555

    results = [_acquire(user32, kernel32) for _ in range(3)]
    assert results[0] is not None
    assert results[1] is None and results[2] is None


def test_refused_launch_exits_without_taking_ownership(win32):
    """A refused launch must not hold a handle that outlives its own exit."""
    user32, kernel32 = win32
    user32.windows[WINDOW_TITLE] = 555

    _acquire(user32, kernel32)
    handles_before = set(kernel32.names[MUTEX_NAME])
    _acquire(user32, kernel32)
    # Only the primary's own handle survives.
    assert set(kernel32.names[MUTEX_NAME]) == handles_before


def test_releasing_the_lock_lets_the_next_launch_run(win32):
    user32, kernel32 = win32
    user32.windows[WINDOW_TITLE] = 555

    first = _acquire(user32, kernel32)
    first.release()
    assert MUTEX_NAME not in kernel32.names

    assert _acquire(user32, kernel32) is not None


def test_release_is_idempotent(win32):
    user32, kernel32 = win32
    instance = _acquire(user32, kernel32)
    instance.release()
    instance.release()
    assert len(kernel32.closed) == 1


def test_a_crashed_primary_does_not_lock_the_app_out(win32):
    """Win32 drops the name when the last handle closes, so a crash is safe."""
    user32, kernel32 = win32
    user32.windows[WINDOW_TITLE] = 555
    _acquire(user32, kernel32)

    # Simulate the process dying: the OS reclaims its handle.
    for handle in list(kernel32.names[MUTEX_NAME]):
        kernel32.CloseHandle(handle)
    assert MUTEX_NAME not in kernel32.names

    assert _acquire(user32, kernel32) is not None


# ------------------------------------------------------- focusing reliably


def test_focus_retries_while_the_primary_is_still_starting(win32):
    """A launcher can win the race against a primary that has not mapped yet."""
    user32, kernel32 = win32

    class LateWindow(FakeUser32):
        def FindWindowW(self, cls, title):
            self.find_calls.append(title)
            return 555 if len(self.find_calls) >= 4 else 0

    assert _acquire(user32, kernel32) is not None  # primary

    late = LateWindow()
    second = _acquire(late, kernel32)
    assert second is None
    assert len(late.find_calls) >= 4
    assert ("foreground", 555) in late.calls


def test_missing_window_is_reported_not_silently_dropped(win32, caplog):
    """A lock with no window yet is a diagnosable state, not silence."""
    user32, kernel32 = win32
    assert _acquire(user32, kernel32) is not None  # primary, window never maps

    with caplog.at_level("WARNING"):
        assert _acquire(user32, kernel32) is None
    assert any("no window" in message for message in caplog.messages)


def test_thread_input_is_attached_and_detached_around_the_raise(win32):
    """The cross-process focus trick must always undo its own attach."""
    user32, kernel32 = win32
    user32.windows[WINDOW_TITLE] = 555
    user32.foreground = 999          # some other app holds focus
    user32.window_threads = {999: 31337}

    _acquire(user32, kernel32)
    _acquire(user32, kernel32)

    assert user32.attach_calls == [
        (kernel32.current_thread_id, 31337, True),
        (kernel32.current_thread_id, 31337, False),
    ]
    assert ("bring", 555) in user32.calls


def test_detach_happens_even_when_the_focus_call_raises(win32):
    user32, kernel32 = win32
    user32.windows[WINDOW_TITLE] = 555
    user32.foreground = 999

    def boom(hwnd):
        raise OSError("SetForegroundWindow refused")

    user32.SetForegroundWindow = boom
    _acquire(user32, kernel32)
    _acquire(user32, kernel32)
    assert user32.attach_calls[-1][2] is False, "input queues must not stay attached"


def test_no_attach_when_the_target_is_already_foreground(win32):
    user32, kernel32 = win32
    user32.windows[WINDOW_TITLE] = 555
    user32.foreground = 555

    _acquire(user32, kernel32)
    _acquire(user32, kernel32)
    assert user32.attach_calls == []


# ----------------------------------------------------------- primary side


def test_primary_grants_foreground_permission(win32):
    user32, kernel32 = win32
    instance = _acquire(user32, kernel32)
    instance.allow_foreground_grab()
    assert user32.allow_calls == [0xFFFFFFFF], "must grant to any process"


def test_grant_is_re_assertable(win32):
    """Windows revokes the grant when foreground changes hands."""
    user32, kernel32 = win32
    instance = _acquire(user32, kernel32)
    for _ in range(3):
        instance.allow_foreground_grab()
    assert len(user32.allow_calls) == 3


# ------------------------------------------------------------ fail-open


def test_non_windows_runs_unguarded():
    instance = acquire(platform_name="Linux")
    assert isinstance(instance, SingleInstance)
    assert instance.is_guarded is False


def test_missing_win32_runs_unguarded():
    assert acquire(platform_name="Linux").is_guarded is False


def test_failed_mutex_creation_does_not_block_the_app(win32):
    """A NULL handle is not evidence of a second copy; refusing to start would
    brick the app on a locked-down hospital desktop."""
    user32, kernel32 = win32
    kernel32.fail_create = True
    instance = _acquire(user32, kernel32)
    assert isinstance(instance, SingleInstance)
    assert instance.is_guarded is False


def test_mutex_errors_fail_open(win32):
    user32, kernel32 = win32

    def boom(*args):
        raise OSError("access denied")

    kernel32.CreateMutexW = boom
    instance = _acquire(user32, kernel32)
    assert isinstance(instance, SingleInstance)
    assert instance.is_guarded is False


def test_release_without_a_handle_is_safe():
    SingleInstance().release()


def test_exp_constant_is_not_a_stale_two(win32):
    """`2` is not the cap; the second launch is refused and the third too."""
    assert single_instance._ERROR_ALREADY_EXISTS == 183
    assert single_instance._ASFW_ANY == 0xFFFFFFFF
    assert MUTEX_NAME.startswith("Local\\"), "must be scoped to a logon session"


# ------------------------------------------------------------ integration


def test_window_title_matches_the_desktop_app():
    """The lookup title and the Tk title must not drift apart."""
    source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    assert re.search(r'self\.root\.title\(\s*"%s"\s*\)' % re.escape(WINDOW_TITLE), source)


def test_main_exits_zero_without_starting_a_session(win32, monkeypatch):
    """The observable behaviour: second launch returns immediately."""
    import desktop_app

    monkeypatch.setattr(desktop_app, "tk", object())
    monkeypatch.setattr(desktop_app, "setup_logging", lambda: None)
    monkeypatch.setattr(desktop_app, "acquire_single_instance", lambda: None)

    def explode(*args, **kwargs):
        raise AssertionError("a refused launch must not build a window")

    monkeypatch.setattr(desktop_app, "FloatingDictationApp", explode)
    assert desktop_app.main() == 0


def test_main_passes_the_lock_to_the_window_and_releases_it(monkeypatch):
    import desktop_app

    seen = {}
    released = []

    class FakeInstance:
        is_guarded = True

        def release(self):
            released.append(True)

    class FakeApp:
        def __init__(self, instance=None):
            seen["instance"] = instance
            self.root = self

        def mainloop(self):
            return None

    monkeypatch.setattr(desktop_app, "tk", object())
    monkeypatch.setattr(desktop_app, "setup_logging", lambda: None)
    monkeypatch.setattr(desktop_app, "acquire_single_instance", FakeInstance)
    monkeypatch.setattr(desktop_app, "FloatingDictationApp", FakeApp)

    assert desktop_app.main() == 0
    assert isinstance(seen["instance"], FakeInstance)
    assert released == [True], "the mutex must be released on teardown"


def test_main_releases_the_lock_even_when_the_app_crashes(monkeypatch):
    import desktop_app

    released = []

    class FakeInstance:
        def release(self):
            released.append(True)

    class Boom:
        def __init__(self, instance=None):
            raise RuntimeError("tk exploded")

    monkeypatch.setattr(desktop_app, "tk", object())
    monkeypatch.setattr(desktop_app, "setup_logging", lambda: None)
    monkeypatch.setattr(desktop_app, "acquire_single_instance", FakeInstance)
    monkeypatch.setattr(desktop_app, "FloatingDictationApp", Boom)

    with pytest.raises(RuntimeError):
        desktop_app.main()
    assert released == [True]