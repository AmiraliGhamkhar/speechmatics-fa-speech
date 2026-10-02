"""Single-instance guard for the Windows desktop build.

The floating control is a small borderless button with no taskbar presence
(``WS_EX_NOACTIVATE | WS_EX_TOOL_WINDOW``), so a second copy of the exe is
invisible to the user while it competes for the SAME microphone and injects
into the focused window at the same time. Two live instances degrade
recognition and garble injected text **without raising an error**.

Mechanism
---------
* A named mutex (``Local\\...``) marks the primary process. It is created
  with ``bInitialOwner=False`` and ownership is decided by
  ``GetLastError() == ERROR_ALREADY_EXISTS``. The kernel drops the name when
  the last handle closes, so a crashed primary frees the name automatically -
  there is no stale lock file to clean up.
* ``Local\\`` scopes the name to one Windows logon session, so two clinicians
  signed into the same machine over RDP each get their own primary.

Bringing the existing window forward
------------------------------------
``SetForegroundWindow`` is deliberately restricted by Windows: a process may
not steal focus unless the current foreground owner allowed it. Two things
make this reliable rather than best-effort:

1. The primary calls ``AllowSetForegroundWindow(ASFW_ANY)`` once its window
   exists and keeps re-asserting it, so any later launcher is permitted.
2. The secondary attaches its thread input to the foreground thread's before
   calling ``BringWindowToTop``/``SetForegroundWindow``, the documented
   workaround for the cross-process case.

Both the handles and the platform check are injectable, so the decision logic
is exercised by tests with fakes on any operating system.
"""
from __future__ import annotations

import logging
import platform
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

#: Must match the Tk window title set in ``desktop_app.FloatingDictationApp``.
WINDOW_TITLE = "SwiftMedics"

#: ``Local\\`` keeps the name per Windows logon session rather than machine
#: wide, so two RDP sessions on one PC do not lock each other out.
MUTEX_NAME = r"Local\SwiftMedics.Desktop.SingleInstance"

_ERROR_ALREADY_EXISTS = 183
_SW_RESTORE = 9
_ASFW_ANY = 0xFFFFFFFF

#: The secondary may start before the primary has mapped its window, so the
#: lookup retries briefly instead of giving up on the first miss.
_FOCUS_ATTEMPTS = 25
_FOCUS_RETRY_SECONDS = 0.1

__all__ = [
    "MUTEX_NAME",
    "WINDOW_TITLE",
    "SingleInstance",
    "acquire",
    "activate_existing_window",
    "allow_foreground_grab",
]


def _is_windows(platform_name: str | None = None) -> bool:
    name = platform_name if platform_name is not None else platform.system()
    return str(name).lower() == "windows"


def _win32() -> tuple[Any, Any]:
    """Return ``(user32, kernel32)`` with HWND-safe prototypes.

    Returns ``(None, None)`` off Windows, where ``ctypes.windll`` does not
    exist. Every prototype is declared explicitly: without them ctypes
    assumes ``int`` for handles, which truncates 64-bit HWND/HANDLE values
    and silently targets the wrong object.
    """
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPDWORD]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    user32.AllowSetForegroundWindow.restype = wintypes.BOOL

    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = wintypes.DWORD
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    return user32, kernel32


def activate_existing_window(
    user32: Any,
    kernel32: Any,
    title: str = WINDOW_TITLE,
    *,
    attempts: int = _FOCUS_ATTEMPTS,
    retry_seconds: float = _FOCUS_RETRY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Bring the primary's window to the foreground. ``True`` if it was found.

    Retries because a launcher can win the race against a primary that has
    created its mutex but not yet mapped its window.
    """
    for attempt in range(max(1, attempts)):
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            _raise_window(user32, kernel32, hwnd)
            return True
        if attempt + 1 < attempts:
            sleep(retry_seconds)
    log.warning("Primary instance holds the mutex but no window titled %r", title)
    return False


def _raise_window(user32: Any, kernel32: Any, hwnd: int) -> None:
    """Foreground ``hwnd`` from another process, past Windows' focus lock."""
    try:
        user32.ShowWindow(hwnd, _SW_RESTORE)
    except Exception:
        log.debug("ShowWindow failed while raising the primary window", exc_info=True)

    attached = False
    foreground = user32.GetForegroundWindow()
    if foreground and foreground != hwnd:
        try:
            foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
            current_thread = kernel32.GetCurrentThreadId()
            if foreground_thread and foreground_thread != current_thread:
                # While input queues are attached, our calls are treated as
                # coming from the foreground thread and are allowed to win.
                attached = bool(user32.AttachThreadInput(
                    current_thread, foreground_thread, True
                ))
        except Exception:
            log.debug("AttachThreadInput failed while raising window", exc_info=True)

    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        log.debug("Could not raise the primary window", exc_info=True)
    finally:
        if attached:
            try:
                user32.AttachThreadInput(current_thread, foreground_thread, False)
            except Exception:
                log.debug("AttachThreadInput detach failed", exc_info=True)


def allow_foreground_grab(user32: Any) -> None:
    """Primary-side: let any later launcher move focus to us.

    Windows resets this permission whenever foreground changes hands, so the
    desktop app re-asserts it on a timer rather than only at startup.
    """
    try:
        user32.AllowSetForegroundWindow(_ASFW_ANY)
    except Exception:
        log.debug("AllowSetForegroundWindow failed", exc_info=True)


class SingleInstance:
    """Handle for the process that owns the single-instance mutex.

    ``None`` as the handle means this object is a no-op stand-in (non-Windows
    platform, or the mutex could not be created). The app then always runs,
    because failing to *create* the mutex is not evidence that another
    instance exists - refusing to launch in that case would brick the app on
    a locked-down hospital desktop.
    """

    def __init__(self, handle: Any = None, user32: Any = None, kernel32: Any = None):
        self._handle = handle
        self._user32 = user32
        self._kernel32 = kernel32
        self._released = False

    @property
    def is_guarded(self) -> bool:
        """``True`` when a real mutex backs this decision."""
        return self._handle is not None

    def allow_foreground_grab(self) -> None:
        """Re-assert focus permission for future launchers."""
        if self._user32 is not None:
            allow_foreground_grab(self._user32)

    def release(self) -> None:
        """Close the mutex handle. Idempotent, and safe to call at teardown."""
        if self._released or self._handle is None:
            self._released = True
            return
        try:
            if self._kernel32 is not None:
                self._kernel32.CloseHandle(self._handle)
        except Exception:
            log.debug("CloseHandle failed while releasing the mutex", exc_info=True)
        self._released = True


def acquire(
    *,
    mutex_name: str = MUTEX_NAME,
    window_title: str = WINDOW_TITLE,
    platform_name: str | None = None,
    user32: Any = None,
    kernel32: Any = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SingleInstance | None:
    """Claim the single-instance lock.

    Returns a :class:`SingleInstance` when this process should run the app, or
    ``None`` when another instance already holds it - in which case that
    instance's window has been brought forward and the caller should exit.
    """
    if user32 is None or kernel32 is None:
        if not _is_windows(platform_name):
            return SingleInstance()
        try:
            user32, kernel32 = _win32()
        except Exception:
            # No Win32 access: run unguarded rather than refuse to start.
            log.warning("Win32 unavailable; running without a single-instance guard")
            return SingleInstance()

    try:
        # bInitialOwner=False -> we never hold ownership, we only observe
        # whether the name already exists. Ownership is not needed and would
        # add a ReleaseMutex path that can fail on an abnormal exit.
        handle = kernel32.CreateMutexW(None, False, mutex_name)
    except Exception:
        log.warning("CreateMutex failed; running without a single-instance guard",
                    exc_info=True)
        return SingleInstance()

    if not handle:
        # Fails when the session is locked down; not proof of a second copy.
        log.warning("CreateMutexW returned NULL; running without a single-instance guard")
        return SingleInstance()

    if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        log.info("Another SwiftMedics instance is already running; focusing it")
        activate_existing_window(user32, kernel32, window_title, sleep=sleep)
        return None

    log.info("Single-instance mutex acquired (%s)", mutex_name)
    return SingleInstance(handle, user32, kernel32)