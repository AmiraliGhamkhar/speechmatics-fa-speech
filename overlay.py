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

    def _abort_if_closed(self, root: "tk.Tk") -> bool:
        """Destroy a root created after shutdown and report whether to abort."""
        if not self._closed:
            return False
        try:
            root.destroy()
        except Exception:
            pass
        return True

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
            root = self._root
            self._root = None
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass

    def _tick_follow(self) -> None:
        """Keep the overlay hovering near the mouse pointer with screen edge clamping."""
        if self._closed or self._root is None:
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

    def set_final(self, text: str) -> None:
        """A finalized ASR segment (post-Aho-Corasick canonical).

        Final results must be displayed through this API, never through
        ``set_partial``: partials are revisable hypotheses, finals are not.
        """
        def _():
            raw = strip_bidi_controls(text or "")
            display, direction = self._render(raw) if raw else ("...", None)
            self._apply_text_alignment(raw, direction)
            if self._label:
                self._label.config(text=display, fg="#f9e2af")
            if self._status:
                self._status.config(text="✓ نهایی شد", fg="#f9e2af", anchor="e")
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
            def destroy() -> None:
                try:
                    root.destroy()
                except Exception:
                    pass
                finally:
                    self._root = None

            # Do not use _ui() here: it correctly rejects callbacks once the
            # overlay is closed.  Tk's queued callback makes destruction occur
            # on the UI thread rather than racing mainloop from the ASR thread.
            try:
                root.after(0, destroy)
            except Exception:
                destroy()

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
