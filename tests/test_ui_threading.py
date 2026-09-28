"""Cross-thread Tkinter UI updates must never touch Tk off the Tk thread.

Tkinter is not thread-safe. Both UI surfaces (the CLI overlay and the desktop
app) are updated from background threads - the Speechmatics receive thread
and the dictation session thread - so they hand work to a ``queue.Queue``
that a repeating ``after`` pump drains on the Tk thread.

These tests pin the invariant from three sides: the mechanism itself, the two
consumers, and a source-level guard against reintroducing a direct Tk call.
"""

from __future__ import annotations

import ast
import gc
import inspect
import textwrap
import threading
import time

import pytest

from speechmatics_test.ui_queue import TkUiDispatcher


def _function_ast(func) -> ast.FunctionDef:
    """The function's AST, with its docstring removed.

    The docstring explains the old ``root.after(...)`` hazard in prose, so a
    plain source scan would match the explanation instead of the code.
    """
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source).body[0]
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        tree.body = tree.body[1:]
    return tree


def _tk_calls_in(func) -> list[str]:
    """Names of Tk scheduling calls made directly inside ``func``."""
    calls = []
    for node in ast.walk(_function_ast(func)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"after", "after_idle", "event_generate",
                                   "destroy", "update", "update_idletasks"}
        ):
            calls.append(node.func.attr)
    return calls


def _called_names(func) -> set[str]:
    return {
        node.func.attr
        for node in ast.walk(_function_ast(func))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


class FakeRoot:
    """Minimal stand-in for ``tk.Tk`` that refuses off-thread Tk calls.

    ``tk`` calls from a foreign thread are exactly what must never happen, so
    the fake raises instead of silently accepting it - the real interpreter
    would block, corrupt state, or raise "main thread is not in main loop".
    """

    def __init__(self, tk_thread: threading.Thread) -> None:
        self.tk_thread = tk_thread
        self.scheduled: list[tuple[int, object]] = []
        self.violations: list[str] = []
        self.destroyed = False

    def after(self, delay, callback):
        if threading.current_thread() is not self.tk_thread:
            self.violations.append(f"after() from {threading.current_thread().name}")
            raise AssertionError("Tk called from a non-Tk thread")
        self.scheduled.append((delay, callback))
        return f"after#{len(self.scheduled)}"

    def destroy(self):
        if threading.current_thread() is not self.tk_thread:
            self.violations.append("destroy() off-thread")
            raise AssertionError("Tk called from a non-Tk thread")
        self.destroyed = True


@pytest.fixture()
def tk_thread() -> threading.Thread:
    """The thread that 'owns' the fake Tk interpreter (the main thread)."""
    return threading.current_thread()


# --------------------------------------------------------------- mechanism


def test_submit_from_a_foreign_thread_never_touches_tk(tk_thread):
    root = FakeRoot(tk_thread)
    dispatcher = TkUiDispatcher(root)
    dispatcher.start()
    ran = []

    def worker():
        for index in range(5):
            assert dispatcher.submit(lambda i=index: ran.append(i)) is True

    thread = threading.Thread(target=worker, name="asr-receive")
    thread.start()
    thread.join()

    assert root.violations == []
    assert ran == []            # nothing may run off the Tk thread
    assert dispatcher.pending == 5

    dispatcher.pump()           # the Tk thread drains
    assert ran == [0, 1, 2, 3, 4]
    assert dispatcher.pending == 0


def test_pump_backs_off_while_idle_and_returns_to_the_active_interval(tk_thread):
    """An idle window must not keep a 16 ms timer alive.

    Dictation is idle between phrases, so a fixed fast timer would wake the
    Tcl event loop ~225,000 times an hour for nothing. After a few consecutive
    empty ticks the pump lengthens its interval; the first submission puts it
    straight back to the active rate.
    """
    root = FakeRoot(tk_thread)
    dispatcher = TkUiDispatcher(
        root, interval_ms=16, idle_interval_ms=100, idle_ticks=3
    )
    dispatcher.start()
    assert root.scheduled[-1][0] == 16, "arming starts at the active rate"

    # Six consecutive empty ticks: two more at the active rate, then back off.
    delays = []
    for _ in range(6):
        seen = len(root.scheduled)
        dispatcher.pump()
        delays.append(root.scheduled[seen][0])

    assert delays == [16, 16, 100, 100, 100, 100]

    # Work resets the backoff, and the tick that runs it stays active.
    ran = []
    dispatcher.submit(lambda: ran.append("update"))
    delays.clear()
    seen = len(root.scheduled)
    dispatcher.pump()
    delays.append(root.scheduled[seen][0])

    assert ran == ["update"]
    assert delays == [16]

    # ...and the following idle tick is back at the active rate too.
    seen = len(root.scheduled)
    dispatcher.pump()
    assert root.scheduled[seen][0] == 16


def test_backoff_never_goes_below_the_active_interval(tk_thread):
    """A misconfigured idle interval must not make idle slower than active."""
    root = FakeRoot(tk_thread)
    dispatcher = TkUiDispatcher(
        root, interval_ms=50, idle_interval_ms=10, idle_ticks=1
    )
    dispatcher.start()
    for _ in range(4):
        seen = len(root.scheduled)
        dispatcher.pump()
        assert root.scheduled[seen][0] == 50


def test_pump_reschedules_itself_on_the_tk_thread(tk_thread):
    root = FakeRoot(tk_thread)
    dispatcher = TkUiDispatcher(root, interval_ms=16)
    dispatcher.start()

    assert len(root.scheduled) == 1
    delay, callback = root.scheduled[-1]
    assert delay == 16
    assert callback == dispatcher.pump

    dispatcher.pump()
    assert len(root.scheduled) == 2      # re-armed after every tick
    assert dispatcher.running is True


def test_concurrent_producers_lose_and_duplicate_nothing(tk_thread):
    """The real shape of the load: partials, finals and injection results all
    arrive from different threads while the pump keeps draining."""
    root = FakeRoot(tk_thread)
    dispatcher = TkUiDispatcher(root, max_per_tick=7)
    dispatcher.start()
    executed: list[int] = []

    producers = [
        threading.Thread(
            target=lambda base=base: [
                dispatcher.submit(lambda i=base * 100 + n: executed.append(i))
                for n in range(50)
            ],
            name=f"producer{base}",
        )
        for base in range(4)
    ]
    for producer in producers:
        producer.start()
    for producer in producers:
        producer.join()

    assert root.violations == []
    assert dispatcher.pending == 200

    # The Tk thread drains in capped batches until the queue is empty.
    ticks = 0
    while dispatcher.pending:
        dispatcher.pump()
        ticks += 1
        assert ticks < 100, "pump stopped draining"

    assert sorted(executed) == sorted(
        base * 100 + n for base in range(4) for n in range(50)
    )
    assert len(executed) == 200  # executed exactly once each


def test_pump_survives_a_failing_callback(tk_thread):
    root = FakeRoot(tk_thread)
    dispatcher = TkUiDispatcher(root)
    dispatcher.start()
    ran = []

    def broken():
        raise RuntimeError("overlay widget is gone")

    dispatcher.submit(broken)
    dispatcher.submit(lambda: ran.append("after the failure"))

    dispatcher.pump()  # must not raise

    assert ran == ["after the failure"]
    assert root.violations == []


def test_pump_never_starves_the_event_loop(tk_thread):
    root = FakeRoot(tk_thread)
    dispatcher = TkUiDispatcher(root, max_per_tick=2)
    dispatcher.start()
    ran = []
    for index in range(5):
        dispatcher.submit(lambda i=index: ran.append(i))

    dispatcher.pump()
    assert ran == [0, 1]                 # capped
    assert root.scheduled[-1][0] == 0     # backlog drained immediately
    dispatcher.pump()
    assert ran == [0, 1, 2, 3]
    dispatcher.pump()
    assert ran == [0, 1, 2, 3, 4]


def test_dispatcher_never_keeps_the_tk_root_alive():
    """The overlay object lives on the ASR thread; a strong reference to the
    root here would let the Tcl interpreter be deallocated off-thread - the
    leaked-interpreter warning the overlay shutdown path is written to avoid.
    """
    root = FakeRoot(threading.current_thread())
    dispatcher = TkUiDispatcher(root)
    root_ref = dispatcher._root_ref

    del root
    gc.collect()

    assert root_ref() is None
    assert dispatcher.running is False
    dispatcher.pump()  # no Tk call: the root is gone
    assert dispatcher.submit(lambda: None) is False


def test_stop_discards_pending_updates(tk_thread):
    root = FakeRoot(tk_thread)
    dispatcher = TkUiDispatcher(root)
    dispatcher.start()
    ran = []
    dispatcher.submit(lambda: ran.append("stale"))
    dispatcher.stop()

    assert dispatcher.running is False
    assert dispatcher.pending == 0
    assert dispatcher.submit(lambda: ran.append("after stop")) is False
    dispatcher.pump()
    assert ran == []


# ------------------------------------------------------- overlay consumer


def _overlay_with_dispatcher(root):
    from overlay import TranscriptOverlay

    overlay = TranscriptOverlay.__new__(TranscriptOverlay)
    overlay.enabled = True
    overlay._closed = False
    overlay._ui_dispatcher = TkUiDispatcher(root, logger=None)
    overlay._ui_dispatcher.start()
    return overlay


def test_overlay_updates_from_a_foreign_thread_are_queued_not_executed(tk_thread):
    root = FakeRoot(tk_thread)
    overlay = _overlay_with_dispatcher(root)

    class Label:
        def __init__(self):
            self.configs = []

        def config(self, **kwargs):
            self.configs.append(kwargs)

    label = Label()
    overlay._label = label
    overlay._status = None
    overlay._wraplength = 420
    overlay._font_family = "Tahoma"

    errors = []

    def worker():
        try:
            overlay.set_partial("بیمار")
            overlay.set_final("بیمار در CCU است")
            overlay.set_done("بیمار در CCU است")
            overlay.set_idle()
        except BaseException as exc:  # pragma: no cover - failure detail
            errors.append(exc)

    thread = threading.Thread(target=worker, name="injection-worker")
    thread.start()
    thread.join()

    assert errors == []
    assert root.violations == []                 # Tk untouched off-thread
    assert label.configs == []                   # nothing rendered yet
    assert overlay._ui_dispatcher.pending == 4

    overlay._ui_dispatcher.pump()                # Tk thread renders
    texts = [config["text"] for config in label.configs if "text" in config]
    assert len(texts) == 4
    assert "بیمار" in texts[0]
    assert "CCU" in texts[1] and "CCU" in texts[2]


def test_overlay_destroy_root_stops_the_dispatcher(tk_thread):
    """Teardown must stop the pump before the widgets it updates are dropped.

    ``_destroy_root`` runs on every overlay close, on the abort-after-timeout
    path, and in ``_run``'s finally block. If it did not stop the dispatcher,
    the pump would keep re-arming against a destroyed root and any callback
    still queued would run against widgets that are already gone. The queue
    is seeded first so the assertion proves ``stop()`` discarded it, rather
    than merely observing an empty queue.
    """
    root = FakeRoot(tk_thread)
    overlay = _overlay_with_dispatcher(root)
    overlay._root = root
    overlay._label = object()
    overlay._status = object()

    dispatcher = overlay._ui_dispatcher
    dispatcher.submit(lambda: pytest.fail("queued update ran after teardown"))
    assert dispatcher.pending == 1
    assert dispatcher.running is True

    overlay._destroy_root(root)

    assert dispatcher.running is False
    assert dispatcher.pending == 0
    # A stray pump tick must be a no-op now, and further submissions are
    # refused rather than queued against a dead root.
    dispatcher.pump()
    assert dispatcher.submit(lambda: pytest.fail("accepted after teardown")) is False

    # The rest of the teardown still happened, on the Tk thread.
    assert root.destroyed is True
    assert root.violations == []
    assert overlay._root is None
    assert overlay._label is None
    assert overlay._status is None


def test_overlay_destroy_root_without_a_dispatcher_is_safe(tk_thread):
    """Startup can fail before the dispatcher exists; teardown must cope."""
    from overlay import TranscriptOverlay

    root = FakeRoot(tk_thread)
    overlay = TranscriptOverlay.__new__(TranscriptOverlay)
    overlay._root = root
    overlay._label = object()
    overlay._status = object()

    overlay._destroy_root(root)

    assert root.destroyed is True
    assert overlay._root is None


def test_overlay_drops_updates_once_closed(tk_thread):
    root = FakeRoot(tk_thread)
    overlay = _overlay_with_dispatcher(root)
    overlay.set_partial("متن")
    overlay._closed = True

    overlay.set_partial("بعد از بستن")

    assert overlay._ui_dispatcher.pending == 1  # only the pre-close update
    overlay._ui_dispatcher.pump()


def test_overlay_ui_helper_contains_no_direct_tk_call():
    """Regression guard: ``_ui`` used to call ``root.after`` from any thread."""
    from overlay import TranscriptOverlay

    assert not _tk_calls_in(TranscriptOverlay._ui)
    assert _called_names(TranscriptOverlay._ui) & {"submit"}


# ----------------------------------------------------- desktop app consumer


def test_desktop_app_updates_from_a_foreign_thread_are_queued(tk_thread):
    import desktop_app

    root = FakeRoot(tk_thread)
    app = desktop_app.FloatingDictationApp.__new__(desktop_app.FloatingDictationApp)
    app.root = root
    app._ui_queue = TkUiDispatcher(root, logger=None)
    app._ui_queue.start()

    seen = []
    errors = []

    def worker():
        try:
            app._ui(seen.append, "final")
            app._ui(seen.append, "partial")
        except BaseException as exc:  # pragma: no cover - failure detail
            errors.append(exc)

    thread = threading.Thread(target=worker, name="desktop-dictation-session")
    thread.start()
    thread.join()

    assert errors == []
    assert root.violations == []
    assert seen == []

    app._ui_queue.pump()
    assert seen == ["final", "partial"]   # submission order preserved


def test_desktop_app_ui_helper_contains_no_direct_tk_call():
    import desktop_app

    method = desktop_app.FloatingDictationApp._ui
    assert not _tk_calls_in(method)
    assert _called_names(method) & {"submit"}


# ------------------------------------------------- real Tk interpreter


def test_real_tk_round_trip_from_a_foreign_thread():
    """The same round trip against a REAL Tcl interpreter.

    The other tests use a fake root that enforces the threading rule; this
    one proves the whole path works with the interpreter the app actually
    ships with - queue hand-off, the ``after`` pump, and a widget update
    driven from a foreign thread.

    Skipped wherever a window cannot be created (no tkinter module, or no
    display). It DOES run on the Windows CI runner, where Tk needs no X
    server, so the threading fix is verified on its real target platform.
    """
    tkinter = pytest.importorskip("tkinter")
    try:
        root = tkinter.Tk()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no usable Tk display: {exc}")

    dispatcher = TkUiDispatcher(root)
    try:
        root.withdraw()
        label = tkinter.Label(root, text="")
        label.pack()
        dispatcher.start()

        errors = []

        def worker():
            try:
                for index in range(5):
                    dispatcher.submit(
                        lambda i=index: label.config(text=str(i))
                    )
            except BaseException as exc:  # pragma: no cover - failure detail
                errors.append(exc)

        thread = threading.Thread(target=worker, name="real-tk-producer")
        thread.start()
        thread.join()

        assert errors == []
        # The Tk thread never ran while the producer was submitting, so the
        # widget cannot have been touched from the other thread.
        assert dispatcher.pending == 5
        assert label.cget("text") == ""

        # Drive the event loop the way mainloop() would, until the pump has
        # drained everything.
        deadline = time.time() + 5.0
        while dispatcher.pending and time.time() < deadline:
            root.update()

        assert dispatcher.pending == 0
        assert label.cget("text") == "4"
    finally:
        dispatcher.stop()
        root.destroy()
