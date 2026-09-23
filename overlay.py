"""
Always-on-top floating transcript overlay optimized for mixed Persian & English medical text.
- Supports Persian fonts (Vazirmatn, IRANSans, B Yekan, Tahoma, Segoe UI).
- Keeps ALL text in LOGICAL Unicode order and controls direction with the
  explicit Unicode controls (RLM + RLE ... PDF) instead of pre-reordering it.
- Smart RTL/LTR *base direction* detection based on the Persian/English ratio.
- Larger readable font with a safe fallback chain.
- Auto-clamps to screen borders so it never goes off-screen.
- Smooth Catppuccin dark theme with real-time status indicators.
- Graceful headless fallback when GUI / Tkinter is unavailable.

Why no ``python-bidi.get_display()``
------------------------------------
``get_display()`` converts logical order into *visual* order.  Tk (and the
platform text engine underneath it) already runs the Unicode BiDi algorithm
when it lays out a label, so feeding it visual-order text applies BiDi
twice: Persian runs come out reversed and embedded English medical terms
("MRI", "CT scan") get scattered.  The fix is to never reorder anything and
instead tell the renderer what the paragraph direction is.
"""
from __future__ import annotations

import logging
import platform
import threading
from typing import Optional

from speechmatics_test.presentation import (
    PDF,
    RLE,
    RLM,
    detect_direction,
    strip_bidi_controls,
    wrap_for_direction,
)

log = logging.getLogger("medical-stt.overlay")
_SYSTEM = platform.system().lower()

# Check Tkinter availability
_TK_AVAILABLE = False
try:
    import tkinter as tk
    import tkinter.font as tkfont
    _TK_AVAILABLE = True
except Exception:
    _TK_AVAILABLE = False


def display_text(text: str) -> str:
    """Return the presentation form of a LOGICAL string for the overlay.

    RTL-dominant:  ``RLM + RLE + logical_text + PDF``
    LTR-dominant:  ``logical_text``

    No characters are reordered, duplicated or dropped - the only difference
    from the canonical transcript is the direction controls, and applying
    the function twice yields the same string (idempotent).
    """
    return wrap_for_direction(text)


#: Backwards-compatible alias. The overlay no longer produces visual-order
#: text; this now returns the logical string plus explicit direction
#: controls, which is what every renderer in the stack expects.
shape_for_display = display_text


def _get_best_persian_font(root: "tk.Tk") -> str:
    """Find the best available Persian font on the host machine."""
    preferred_fonts = [
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
        available = set(tkfont.families(root))
        for font in preferred_fonts:
            if font in available:
                return font
    except Exception:
        pass
    return "Tahoma" if _SYSTEM == "windows" else "Arial"


class TranscriptOverlay:
    """
    Floating always-on-top window displaying real-time ASR transcripts
    and injection status.
    """

    def __init__(
        self,
        enabled: bool = True,
        offset_x: int = 20,
        offset_y: int = 24,
        wraplength: int = 420,
    ):
        self.enabled = enabled and _TK_AVAILABLE
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.wraplength = wraplength

        self._root: Optional[tk.Tk] = None
        self._label: Optional[tk.Label] = None
        self._status: Optional[tk.Label] = None
        self._font_family: str = "Tahoma"

        self._ready = threading.Event()
        self._closed = False
        # Foreground window before the overlay existed, captured BEFORE the
        # UI thread can create any window. Creating the overlay makes the
        # process activate its new top-level window while it still owns
        # foreground rights (even with WS_EX_NOACTIVATE), so the overlay
        # hands this window its focus back once it is visible.
        self._previous_fg_hwnd: Optional[int] = None
        if self.enabled and _SYSTEM == "windows":
            try:
                import ctypes

                self._previous_fg_hwnd = (
                    int(ctypes.windll.user32.GetForegroundWindow()) or None
                )
            except Exception:
                pass

        if not self.enabled:
            log.info("Overlay GUI disabled or Tkinter unavailable — running in console-only mode")
            self._ready.set()
            return

        self._thread = threading.Thread(target=self._run, name="overlay-ui", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=4.0):
            log.warning("Overlay UI startup timed out — falling back to console mode")
            self.enabled = False
            # The UI thread may still be mid-construction (or reach mainloop
            # later): mark closed so it destroys its own window instead of
            # leaking an invisible always-on-top Tk root.
            self.close()

    def _destroy_root(self, root: "tk.Tk") -> None:
        """Release every Tk object on the UI thread that created it."""
        # Widgets retain the Tcl interpreter.  Dropping these references here,
        # before the UI thread exits, prevents Tkapp.__del__ from running later
        # on the ASR/main thread (the source of Tcl's leaked-interpreter warning).
        self._label = None
        self._status = None
        try:
            root.destroy()
        except Exception:
            pass
        finally:
            if getattr(self, "_root", None) is root:
                self._root = None

    def _abort_if_closed(self, root: "tk.Tk") -> bool:
        """Destroy a root created after shutdown and report whether to abort."""
        if not self._closed:
            return False
        self._destroy_root(root)
        return True

    def _prevent_focus_steal(self) -> None:
        """Never let the overlay become the foreground window (Windows).

        WS_EX_NOACTIVATE stops the always-on-top window from taking focus
        when it is created or moved by the 40 ms follow tick.  Without it
        the overlay held foreground focus at dictation start, so
        ``injector.arm_target()`` armed the overlay itself and every paste
        was skipped by the focus guard ("[injector] focus changed - paste
        skipped").  The window still renders and updates normally; it just
        never accepts focus (clicks pass through to the window underneath
        as far as activation is concerned).
        """
        if _SYSTEM != "windows":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOPMOST = 0x00000008
            # For a Tk root the extended styles live on the wrapper window.
            hwnd = user32.GetParent(wintypes.HWND(self._root.winfo_id()))
            if not hwnd:
                hwnd = self._root.winfo_id()
            style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongPtrW(
                hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOPMOST
            )
        except Exception as exc:  # best effort: presentation only
            log.debug("overlay could not set WS_EX_NOACTIVATE: %s", exc)

    def _restore_foreground(self) -> None:
        """Hand the foreground focus back to the window the user was in.

        Called on the UI thread right after the window is mapped - exactly
        the moment this thread still OWNS the foreground window (the overlay
        it just activated), which is the one situation in which
        ``SetForegroundWindow`` reliably succeeds. Without this, the overlay
        remained the foreground window after startup and
        ``injector.arm_target()`` armed the overlay instead of the field
        being dictated into, so every paste landed in the void.
        """
        if _SYSTEM != "windows" or not self._previous_fg_hwnd:
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            if self._root is None:
                return
            wrapper = user32.GetParent(self._root.winfo_id())
            current = user32.GetForegroundWindow()
            if current not in (wrapper, self._root.winfo_id()):
                # The user (or something else) already switched focus after
                # the overlay appeared; their choice wins.
                return
            hwnd = wintypes.HWND(self._previous_fg_hwnd)
            if user32.IsWindow(hwnd):
                user32.SetForegroundWindow(hwnd)
        except Exception as exc:  # best effort: presentation only
            log.debug(
                "overlay could not restore the previous foreground window: %s", exc
            )

    def _run(self) -> None:
        try:
            self._root = tk.Tk()
            # A startup timeout may have abandoned this thread while Tk was
            # being constructed: never surface a window afterwards.
            if self._abort_if_closed(self._root):
                return
            self._root.title("SwiftMedics STT Overlay")
            self._root.overrideredirect(True)
            self._root.attributes("-topmost", True)

            # Detect best available Persian font
            self._font_family = _get_best_persian_font(self._root)

            # Transparency
            try:
                self._root.attributes("-alpha", 0.94)
            except tk.TclError:
                pass

            # Outer border frame (Catppuccin Surface0 #313244)
            border_frame = tk.Frame(self._root, bg="#313244", padx=1, pady=1)
            border_frame.pack(fill="both", expand=True)

            # Inner container frame (Catppuccin Base #1e1e2e)
            inner_frame = tk.Frame(border_frame, bg="#1e1e2e", padx=14, pady=12)
            inner_frame.pack(fill="both", expand=True)

            # Status Label
            self._status = tk.Label(
                inner_frame,
                text="● در حال شنیدن...",
                fg="#a6e3a1",
                bg="#1e1e2e",
                font=(self._font_family, 10, "bold"),
                anchor="e",
                justify="right",
            )
            self._status.pack(fill="x", pady=(0, 5))

            # Main Transcript Label (bigger font for at-a-glance readability)
            self._label = tk.Label(
                inner_frame,
                text="...",
                fg="#cdd6f4",
                bg="#1e1e2e",
                font=(self._font_family, 14),
                wraplength=self.wraplength,
                justify="right",
                anchor="ne",
            )
            self._label.pack(fill="both", expand=True)

            self._root.geometry("+100+100")
            if self._abort_if_closed(self._root):
                return
            # The extended style must be set AFTER the window is mapped: with
            # overrideredirect(True), Tk finalizes (re)creating the wrapper
            # window at map time and discards earlier style changes.
            try:
                self._root.wait_visibility()
            except Exception:
                pass
            self._prevent_focus_steal()
            self._restore_foreground()
            self._tick_follow()
            # Ready only once the event loop is actually processing
            # callbacks: marking ready before mainloop() let other threads
            # schedule UI updates into a window that was still starting up.
            self._root.after(0, self._ready.set)
            self._root.mainloop()
        except Exception as e:
            log.warning("Failed to initialize overlay window: %s", e)
            self.enabled = False
            self._ready.set()
        finally:
            # _run owns the Tcl interpreter, so normal shutdown and every
            # startup failure release it here rather than during later GC on
            # whichever thread happens to drop the overlay object.
            root = self._root
            if root is not None:
                self._destroy_root(root)
            else:
                self._label = None
                self._status = None

    def _tick_follow(self) -> None:
        """Keep the overlay hovering near the mouse pointer with screen edge clamping."""
        if self._closed:
            # close() may be unable to enqueue a Tk callback while shutdown is
            # racing.  This already-scheduled UI-thread tick is the safe
            # fallback; never call root.destroy() directly from the main thread.
            if self._root is not None:
                self._destroy_root(self._root)
            return
        if self._root is None:
            return
        try:
            pointer_x = self._root.winfo_pointerx()
            pointer_y = self._root.winfo_pointery()
            screen_w = self._root.winfo_screenwidth()
            screen_h = self._root.winfo_screenheight()
            win_w = self._root.winfo_reqwidth()
            win_h = self._root.winfo_reqheight()

            x = pointer_x + self.offset_x
            y = pointer_y + self.offset_y

            # Clamp to screen right/bottom edges so overlay remains visible
            if x + win_w > screen_w - 12:
                x = pointer_x - win_w - 10
            if y + win_h > screen_h - 12:
                y = pointer_y - win_h - 10

            self._root.geometry(f"+{max(6, x)}+{max(6, y)}")
        except Exception:
            pass
        if self._root is not None and not self._closed:
            self._root.after(40, self._tick_follow)

    def _ui(self, fn) -> None:
        if not self.enabled or self._root is None or self._closed:
            return
        try:
            self._root.after(0, fn)
        except Exception:
            pass

    def _apply_text_alignment(self, text: str, direction: Optional[str] = None) -> None:
        """Dynamically switch alignment based on the detected text direction."""
        if not self._label:
            return
        direction = direction or detect_direction(text)
        if direction == "rtl":
            self._label.config(anchor="ne", justify="right")
        else:
            self._label.config(anchor="nw", justify="left")

    def _render(self, text: str) -> tuple[str, str]:
        """Return ``(display_text, base_direction)`` for a LOGICAL string.

        ``text`` is the canonical transcript and is never reordered: only
        Unicode direction controls are added on top of it.
        """
        logical = strip_bidi_controls(text or "")
        direction = detect_direction(logical)
        return wrap_for_direction(logical, direction), direction

    def set_partial(self, text: str) -> None:
        """Real-time streaming hypothesis update (UI/overlay only)."""
        def _():
            raw = strip_bidi_controls(text or "")
            display, direction = self._render(raw) if raw else ("...", None)
            self._apply_text_alignment(raw, direction)
            if self._label:
                self._label.config(text=display, fg="#cdd6f4")
            if self._status:
                self._status.config(text="● در حال شنیدن...", fg="#a6e3a1", anchor="e")
        self._ui(_)

    def set_final(self, text: str, warnings: Optional[list[dict]] = None) -> None:
        """Display finalized canonical text and optional review warnings.

        Warnings are presentation-only metadata (currently low-confidence
        clinical entities). They never change the displayed logical text or
        the text sent to the injector. ``warnings=None`` preserves the
        established behavior and public call shape.
        """
        def _():
            raw = strip_bidi_controls(text or "")
            display, direction = self._render(raw) if raw else ("...", None)
            self._apply_text_alignment(raw, direction)
            if self._label:
                self._label.config(text=display, fg="#f9e2af")
            if self._status:
                if warnings:
                    entities = ", ".join(
                        str(item.get("content", "")) for item in warnings[:3]
                        if item.get("content")
                    )
                    suffix = f": {entities}" if entities else ""
                    self._status.config(
                        text=f"⚠ بررسی مقدار نامطمئن{suffix}",
                        fg="#fab387", anchor="e",
                    )
                else:
                    self._status.config(
                        text="✓ نهایی شد", fg="#f9e2af", anchor="e"
                    )
        self._ui(_)

    def set_done(self, text: str) -> None:
        """Final transcript successfully injected into cursor position."""
        def _():
            raw = strip_bidi_controls(text or "")
            display, direction = self._render(raw) if raw else ("...", None)
            self._apply_text_alignment(raw, direction)
            if self._label:
                self._label.config(text=display, fg="#89b4fa")
            if self._status:
                self._status.config(text="✓ تایپ شد", fg="#89b4fa", anchor="e")
        self._ui(_)

    def set_idle(self) -> None:
        """Idle waiting state."""
        def _():
            if self._label:
                self._label.config(text="...", fg="#6c7086", anchor="ne")
            if self._status:
                self._status.config(text="● آماده", fg="#a6adc8", anchor="e")
        self._ui(_)

    def close(self) -> None:
        """Cleanly close the overlay and wait briefly for its UI thread.

        ``_ui`` deliberately ignores work after ``_closed`` is set.  The old
        implementation set that flag *before* calling ``_ui``, so the destroy
        callback was always discarded.  The invisible consequence was that
        the always-on-top window kept following the mouse during target
        selection and could receive the click intended for the editor.  Text
        was then pasted into the previously focused window (often the
        terminal), which looked like injection had done nothing.
        """
        if self._closed:
            return

        root = self._root
        thread = getattr(self, "_thread", None)
        self._closed = True

        if root is not None:
            # Do not use _ui() here: it correctly rejects callbacks once the
            # overlay is closed.  Tk's queued callback makes destruction occur
            # on the UI thread rather than racing mainloop from the ASR thread.
            # The bound method (not a closure capturing ``root``) plus the
            # explicit ``root = None`` below hand the LAST reference to the
            # UI thread: the Tk object is then deallocated on the thread that
            # created it, instead of raising Tcl's leaked-interpreter warning
            # when close()'s local reference dies on the calling thread.
            try:
                root.after(0, self._destroy_scheduled_root)
            except Exception:
                # Never destroy a Tcl interpreter from this caller thread.
                # _tick_follow() is already scheduled on the UI thread and
                # observes _closed within 40 ms.
                pass

        root = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _destroy_scheduled_root(self) -> None:
        """UI-thread callback: destroy whatever root is currently owned."""
        root = self._root
        if root is not None:
            self._destroy_root(root)
