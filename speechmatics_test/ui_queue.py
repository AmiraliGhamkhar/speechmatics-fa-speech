"""Thread-safe marshalling of UI updates onto the Tk thread.

Tkinter is not thread-safe. Calling into it from a thread other than the one
that created the interpreter can block, corrupt Tcl state, or raise
``RuntimeError: main thread is not in main loop`` during shutdown. Both UI
surfaces in this project (the CLI overlay and the desktop app) are updated
from the Speechmatics receive thread or the dictation session thread, so
neither may touch Tk directly.

The pattern used here is the standard one:

* producers (:meth:`TkUiDispatcher.submit`) only do ``queue.put`` - no Tk call
  at all, from any thread;
* a repeating ``after`` pump, started and owned by the Tk thread, drains the
  queue in submission order and runs the callbacks there.

The pump re-arms itself at the active rate (16 ms) while updates keep arriving
and backs off to a longer interval once the queue has been empty for a few
consecutive ticks. A dictation session is idle between phrases, and a fixed
16 ms timer would keep the Tcl event loop waking ~225,000 times an hour for
nothing; the backoff costs at most 100 ms of latency on the first update after
an idle period, and any submission resets the pump to the active rate.

The dispatcher holds only a *weak* reference to the Tk root. The overlay
object is owned by the ASR thread while the root belongs to the UI thread, so
a strong reference here would let the Tcl interpreter be deallocated on the
wrong thread - the exact "interpreter is leaked because it was deallocated in
a thread other than the one it was created in" warning the overlay's shutdown
path is written to avoid.
"""

from __future__ import annotations

import logging
import queue
import weakref
from typing import Any, Callable, Optional

__all__ = [
    "TkUiDispatcher",
    "DEFAULT_INTERVAL_MS",
    "DEFAULT_IDLE_INTERVAL_MS",
    "DEFAULT_IDLE_TICKS",
    "DEFAULT_MAX_PER_TICK",
]

#: How often the Tk thread drains the queue while there is work to do. ~60 Hz
#: keeps the added latency imperceptible (partials are rendered live).
DEFAULT_INTERVAL_MS = 16

#: Interval used once the queue has been empty for a while. A dictation
#: session is idle between phrases, and a fixed 16 ms timer would keep the
#: Tcl event loop waking ~225,000 times an hour for nothing. 100 ms still
#: leaves the first update after an idle period feeling instant.
DEFAULT_IDLE_INTERVAL_MS = 100

#: Consecutive empty ticks before backing off. A few active-rate ticks are
#: kept so a burst of partials (which arrive in bursts) never pays the
#: wake-up latency penalty.
DEFAULT_IDLE_TICKS = 3

#: Upper bound on callbacks executed per tick, so a burst of updates can
#: never starve the Tk event loop (which also drives window following and
#: user input). Whatever is left is picked up by an immediate re-schedule.
DEFAULT_MAX_PER_TICK = 256

log = logging.getLogger("medical-stt.ui_queue")


class TkUiDispatcher:
    """Run callables on the Tk thread, submitted from any thread."""

    def __init__(
        self,
        root: Any,
        *,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        idle_interval_ms: int = DEFAULT_IDLE_INTERVAL_MS,
        idle_ticks: int = DEFAULT_IDLE_TICKS,
        max_per_tick: int = DEFAULT_MAX_PER_TICK,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._root_ref = weakref.ref(root)
        self._interval_ms = max(1, int(interval_ms))
        # Never back off below the active interval: that would make an idle
        # dispatcher slower than a busy one.
        self._idle_interval_ms = max(self._interval_ms, int(idle_interval_ms))
        self._idle_ticks_before_backoff = max(1, int(idle_ticks))
        self._max_per_tick = max(1, int(max_per_tick))
        self._log = logger or log
        self._queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._running = False
        #: Consecutive ticks that found nothing to do. Reset by any activity.
        self._idle_ticks = 0

    # ------------------------------------------------------------ producer
    # Callable from any thread: touches no Tk state whatsoever.

    def submit(self, fn: Callable[[], None]) -> bool:
        """Queue ``fn`` for execution on the Tk thread.

        Returns False when the dispatcher is not running (the window is gone
        or was never started), in which case the update is dropped instead of
        being executed on the wrong thread.
        """
        if not self._running:
            return False
        self._queue.put(fn)
        # A producer cannot touch Tk to wake the loop earlier, but it can make
        # sure the next tick runs at the active rate instead of the backed-off
        # one. (Worst case this is a single attribute store racing a tick that
        # is already draining, which only costs one extra active tick.)
        self._idle_ticks = 0
        return True

    @property
    def running(self) -> bool:
        return self._running

    @property
    def pending(self) -> int:
        """Number of queued callbacks not yet executed (diagnostics/tests)."""
        return self._queue.qsize()

    # ---------------------------------------------------------- Tk thread

    def start(self) -> bool:
        """Begin pumping. Must be called on the Tk thread."""
        if self._root_ref() is None:
            return False
        self._running = True
        self._idle_ticks = 0
        self._schedule(self._interval_ms)
        return True

    def stop(self, *, discard: bool = True) -> None:
        """Stop pumping and drop anything still queued (thread-safe)."""
        self._running = False
        if discard:
            self.discard()

    def discard(self) -> int:
        """Drop every queued callback; returns how many were dropped."""
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return dropped
            dropped += 1

    def pump(self) -> None:
        """Execute queued callbacks and re-arm. Runs on the Tk thread."""
        if not self._running:
            return
        if self._root_ref() is None:
            # The root was destroyed: never touch Tk again.
            self._running = False
            return

        processed = 0
        while processed < self._max_per_tick:
            try:
                fn = self._queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            try:
                fn()
            except Exception:
                # One broken update must not discard the rest of the batch
                # or stop the pump - that would freeze the whole UI.
                self._log.exception("queued UI update failed")

        # A capped batch means more work is waiting: come back immediately.
        if processed >= self._max_per_tick:
            self._idle_ticks = 0
            self._schedule(0)
            return

        # Adaptive interval: stay at the active rate while updates keep
        # arriving, and back off once the queue has been empty for a few
        # consecutive ticks so an idle window does not spin the event loop.
        if processed:
            self._idle_ticks = 0
        else:
            self._idle_ticks += 1
        self._schedule(
            self._idle_interval_ms
            if self._idle_ticks >= self._idle_ticks_before_backoff
            else self._interval_ms
        )

    def _schedule(self, delay_ms: int) -> None:
        root = self._root_ref()
        if root is None:
            self._running = False
            return
        try:
            root.after(delay_ms, self.pump)
        except Exception:
            # The event loop is gone (shutdown race). Give up quietly: the
            # caller thread must never be the one to touch Tk.
            self._running = False
