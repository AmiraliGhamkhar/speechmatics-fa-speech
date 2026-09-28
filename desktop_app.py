"""Floating Start/Stop desktop UI for SwiftMedics.

This is the entry point intended for the Windows .exe build.  It keeps the
initial surface tiny (one floating Start button), expands to a live transcript
panel while recording, and returns to the Start state after Stop while the
session controller gracefully drains already-finalized injection jobs.
"""
from __future__ import annotations

import ctypes
import logging
import platform
from pathlib import Path
from typing import Callable

from speechmatics_test.desktop_config import (
    ConfigError,
    app_data_dir,
    load_desktop_config,
    log_dir,
)
from speechmatics_test.session_controller import (
    DictationSession,
    DictationSettings,
    SessionCallbacks,
    SessionSummary,
)
from speechmatics_test.ui_queue import TkUiDispatcher

try:
    import tkinter as tk
    import tkinter.font as tkfont
    _TK_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - GUI dependency
    tk = None  # type: ignore[assignment]
    tkfont = None  # type: ignore[assignment]
    _TK_IMPORT_ERROR = exc

log = logging.getLogger(__name__)
_SYSTEM = platform.system().lower()
_WIN32_CONFIGURED = False


def _user32():
    """Return user32 with HWND-safe ctypes prototypes installed."""

    if _SYSTEM != "windows":
        return None
    global _WIN32_CONFIGURED
    user32 = ctypes.windll.user32
    if not _WIN32_CONFIGURED:
        from ctypes import wintypes

        long_ptr = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = long_ptr
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, long_ptr]
        user32.SetWindowLongPtrW.restype = long_ptr
        _WIN32_CONFIGURED = True
    return user32


class FloatingDictationApp:
    """Small always-on-top control for starting/stopping dictation."""

    def __init__(self) -> None:
        if tk is None:
            raise RuntimeError(f"Tkinter is required for the desktop app: {_TK_IMPORT_ERROR}")
        self.root = tk.Tk()
        self.root.title("SwiftMedics")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)

        self._font_family = self._best_font()
        self._session: DictationSession | None = None
        self._last_target_hwnd: int | None = None
        self._drag_origin: tuple[int, int, int, int] | None = None
        self._state = "ready"
        self._final_text = ""
        self._partial_text = ""
        self._last_error = ""
        #: Cross-thread UI hand-off. Started here, on the Tk thread, before
        #: mainloop(); session callbacks only ever put work on its queue.
        self._ui_queue = TkUiDispatcher(self.root, logger=log)

        self._build_ui()
        self._build_menu()
        self._set_ready_ui()
        self.root.geometry("+96+96")
        self._ui_queue.start()
        self.root.after(100, self._apply_nonactivating_styles)
        self.root.after(150, self._remember_foreground_target)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ setup

    def _best_font(self) -> str:
        preferred = [
            "Vazirmatn",
            "Vazir",
            "IRANSans",
            "B Yekan",
            "B Nazanin",
            "Segoe UI",
            "Tahoma",
            "Arial",
        ]
        try:
            available = set(tkfont.families(self.root))
            for font in preferred:
                if font in available:
                    return font
        except Exception:
            pass
        return "Tahoma" if _SYSTEM == "windows" else "Arial"

    def _build_ui(self) -> None:
        self.shell = tk.Frame(self.root, bg="#313244", padx=1, pady=1)
        self.shell.pack(fill="both", expand=True)
        self.container = tk.Frame(self.shell, bg="#1e1e2e", padx=10, pady=9)
        self.container.pack(fill="both", expand=True)

        self.button = tk.Button(
            self.container,
            text="Start",
            command=self._on_start,
            cursor="hand2",
            fg="#11111b",
            bg="#a6e3a1",
            activeforeground="#11111b",
            activebackground="#94e2d5",
            relief="flat",
            bd=0,
            padx=18,
            pady=8,
            font=(self._font_family, 11, "bold"),
        )
        self.button.pack()

        self.panel = tk.Frame(self.container, bg="#1e1e2e")
        self.status = tk.Label(
            self.panel,
            text="آماده",
            fg="#a6adc8",
            bg="#1e1e2e",
            font=(self._font_family, 10, "bold"),
            anchor="e",
            justify="right",
        )
        self.status.pack(fill="x", pady=(8, 4))

        self.transcript = tk.Label(
            self.panel,
            text="",
            fg="#cdd6f4",
            bg="#1e1e2e",
            font=(self._font_family, 13),
            wraplength=430,
            width=42,
            height=6,
            justify="right",
            anchor="ne",
        )
        self.transcript.pack(fill="both", expand=True)

        self.hint = tk.Label(
            self.panel,
            text="برای دیکته، ابتدا محل تایپ را انتخاب کنید؛ سپس Start را بزنید.",
            fg="#6c7086",
            bg="#1e1e2e",
            font=(self._font_family, 8),
            anchor="e",
            justify="right",
            wraplength=430,
        )
        self.hint.pack(fill="x", pady=(5, 0))

        # Borderless window dragging.  Binding to the shell/container/labels
        # keeps the button click itself dedicated to Start/Stop.
        for widget in (self.shell, self.container, self.panel, self.status, self.transcript, self.hint):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

    def _build_menu(self) -> None:
        """Right-click menu for exiting a borderless floating window."""

        self.menu = tk.Menu(self.root, tearoff=False, bg="#1e1e2e", fg="#cdd6f4")
        self.menu.add_command(label="Exit", command=self._on_close)
        for widget in (self.root, self.shell, self.container, self.button, self.panel, self.status, self.transcript, self.hint):
            widget.bind("<Button-3>", self._show_menu)

    def _show_menu(self, event) -> None:
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ------------------------------------------------------------------ states

    def _set_ready_ui(self) -> None:
        self._state = "ready"
        self.button.config(
            text="Start",
            command=self._on_start,
            state="normal",
            bg="#a6e3a1",
            activebackground="#94e2d5",
        )
        self.panel.pack_forget()
        self.root.update_idletasks()

    def _set_recording_ui(self) -> None:
        self._state = "recording"
        self._final_text = ""
        self._partial_text = ""
        self._last_error = ""
        self.button.config(
            text="Stop",
            command=self._on_stop,
            state="normal",
            bg="#f38ba8",
            activebackground="#eba0ac",
        )
        self.status.config(text="در حال اتصال...", fg="#f9e2af")
        self.transcript.config(text="", fg="#cdd6f4")
        if not self.panel.winfo_ismapped():
            self.panel.pack(fill="both", expand=True)
        self.root.update_idletasks()

    def _set_stopping_ui(self) -> None:
        self._state = "stopping"
        self.button.config(text="Stopping...", state="disabled", bg="#fab387")
        self.status.config(text="در حال توقف؛ متن‌های نهایی در صف تزریق تکمیل می‌شوند...", fg="#fab387")

    def _show_error(self, message: str) -> None:
        self._last_error = message
        log.error("Desktop UI error: %s", message)
        self.button.config(
            text="Start",
            command=self._on_start,
            state="normal",
            bg="#a6e3a1",
            activebackground="#94e2d5",
        )
        if not self.panel.winfo_ismapped():
            self.panel.pack(fill="both", expand=True)
        self.status.config(text="خطا", fg="#f38ba8")
        self.transcript.config(text=message, fg="#f38ba8", justify="left", anchor="nw")
        self.root.update_idletasks()

    # ---------------------------------------------------------------- buttons

    def _on_start(self) -> None:
        if self._session is not None and self._session.is_running:
            return
        self._restore_last_target()
        try:
            loaded = load_desktop_config(create_template=True)
            settings = DictationSettings.from_desktop_config(loaded.config)
        except ConfigError as exc:
            self._show_error(str(exc))
            return
        except Exception as exc:
            self._show_error(f"Could not load SwiftMedics config: {exc}")
            return

        callbacks = SessionCallbacks(
            on_status=lambda status: self._ui(self._handle_status, status),
            on_partial=lambda text: self._ui(self._handle_partial, text),
            on_final=lambda text: self._ui(self._handle_final, text),
            on_injection=lambda record: self._ui(self._handle_injection, record),
            on_error=lambda message: self._ui(self._handle_session_error, message),
            on_stopped=lambda summary: self._ui(self._handle_stopped, summary),
        )
        self._session = DictationSession(settings, callbacks)
        self._set_recording_ui()
        try:
            self._session.start()
        except Exception as exc:
            self._show_error(f"Could not start dictation: {exc}")

    def _on_stop(self) -> None:
        session = self._session
        if session is None or not session.is_running:
            self._set_ready_ui()
            return
        self._restore_last_target()
        self._set_stopping_ui()
        session.request_stop()

    def _on_close(self) -> None:
        session = self._session
        if session is not None and session.is_running:
            self._set_stopping_ui()
            session.request_stop()
            self.root.after(250, self._wait_then_destroy)
        else:
            self.root.destroy()

    def _wait_then_destroy(self) -> None:
        session = self._session
        if session is not None and session.is_running:
            self.root.after(250, self._wait_then_destroy)
            return
        self.root.destroy()

    # ------------------------------------------------------------ callbacks

    def _ui(self, fn: Callable, *args) -> None:
        """Hand a UI update to the Tk thread. Safe from the session thread.

        Tk is not thread-safe: the old ``root.after(0, ...)`` from the
        dictation session thread could block, corrupt Tcl state, or raise
        "main thread is not in main loop" while the window was closing - and
        the blanket ``except`` hid it. The update is now queued and executed
        by the pump running on the Tk thread.
        """
        dispatcher = getattr(self, "_ui_queue", None)
        if dispatcher is None:
            log.debug("Dropped UI update: dispatcher not started")
            return
        dispatcher.submit(lambda: fn(*args))

    def _handle_status(self, status: str) -> None:
        if status == "starting":
            self.status.config(text="در حال اتصال...", fg="#f9e2af")
        elif status == "listening":
            self.status.config(text="● در حال شنیدن...", fg="#a6e3a1")
        elif status == "listening_unarmed":
            self.status.config(
                text="● در حال شنیدن... محافظ فوکوس فعال نشد؛ محل تایپ را انتخاب نگه دارید.",
                fg="#fab387",
            )
        elif status == "stopping":
            self._set_stopping_ui()
        elif status == "stopped":
            self.status.config(text="متوقف شد", fg="#a6adc8")

    def _handle_partial(self, text: str) -> None:
        self._partial_text = text
        display = self._compose_transcript()
        self.transcript.config(text=display or text, fg="#cdd6f4", justify="right", anchor="ne")

    def _handle_final(self, text: str) -> None:
        self._final_text = text
        self._partial_text = ""
        self.transcript.config(text=text or "", fg="#f9e2af", justify="right", anchor="ne")
        self.status.config(text="✓ متن نهایی شد", fg="#f9e2af")

    def _handle_injection(self, record: dict) -> None:
        if record.get("success"):
            self.status.config(text="✓ تایپ شد", fg="#89b4fa")
        else:
            self.status.config(text="تزریق انجام نشد؛ فوکوس مقصد را بررسی کنید.", fg="#fab387")

    def _handle_session_error(self, message: str) -> None:
        self._last_error = message
        self.status.config(text="خطای نشست", fg="#f38ba8")
        if message:
            self.transcript.config(text=message, fg="#f38ba8", justify="left", anchor="nw")

    def _handle_stopped(self, summary: SessionSummary) -> None:
        log.info(
            "Desktop session stopped: canonical_chars=%s injected=%s error=%s report=%s",
            len(summary.canonical or ""),
            len(summary.injected_segments),
            summary.error,
            summary.report_path,
        )
        self._session = None
        if self._last_error or summary.error:
            message = self._last_error or summary.error or "Session stopped with an error."
            if summary.report_path:
                message += f"\nReport: {summary.report_path}"
            self._show_error(message)
            return
        self._set_ready_ui()

    def _compose_transcript(self) -> str:
        if self._final_text and self._partial_text:
            return self._final_text + "\n" + self._partial_text
        return self._partial_text or self._final_text

    # ------------------------------------------------------------- dragging

    def _drag_start(self, event) -> None:
        self._drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _drag_move(self, event) -> None:
        if self._drag_origin is None:
            return
        start_x, start_y, win_x, win_y = self._drag_origin
        dx = event.x_root - start_x
        dy = event.y_root - start_y
        self.root.geometry(f"+{win_x + dx}+{win_y + dy}")

    # ---------------------------------------------------------- focus/window

    def _apply_nonactivating_styles(self) -> None:
        if _SYSTEM != "windows":
            return
        try:
            user32 = _user32()
            if user32 is None:
                return
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOPMOST = 0x00000008
            WS_EX_TOOLWINDOW = 0x00000080
            hwnd = self._window_hwnd()
            if not hwnd:
                return
            style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongPtrW(
                hwnd,
                GWL_EXSTYLE,
                style | WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
            )
        except Exception:
            log.exception("Could not apply non-activating window style")

    def _window_hwnd(self) -> int | None:
        if _SYSTEM != "windows":
            return None
        try:
            user32 = _user32()
            if user32 is None:
                return None
            raw = int(self.root.winfo_id())
            parent = int(user32.GetParent(raw))
            return parent or raw
        except Exception:
            return None

    def _remember_foreground_target(self) -> None:
        if _SYSTEM == "windows":
            try:
                user32 = _user32()
                if user32 is None:
                    return
                hwnd = int(user32.GetForegroundWindow())
                own = self._window_hwnd()
                if hwnd and hwnd != own and hwnd != int(self.root.winfo_id()):
                    self._last_target_hwnd = hwnd
            except Exception:
                pass
        self.root.after(250, self._remember_foreground_target)

    def _restore_last_target(self) -> None:
        if _SYSTEM != "windows" or not self._last_target_hwnd:
            return
        try:
            user32 = _user32()
            if user32 is None:
                return
            hwnd = self._last_target_hwnd
            if user32.IsWindow(hwnd):
                user32.SetForegroundWindow(hwnd)
        except Exception:
            log.debug("Could not restore previous foreground target", exc_info=True)



def setup_logging() -> Path:
    target_dir = log_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "swiftmedics.log"
    logging.basicConfig(
        filename=path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    log.info("SwiftMedics desktop app starting; app_data=%s", app_data_dir())
    return path



def main() -> int:
    if tk is None:
        raise SystemExit(f"Tkinter is required for the desktop app: {_TK_IMPORT_ERROR}")
    setup_logging()
    app = FloatingDictationApp()
    app.root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
