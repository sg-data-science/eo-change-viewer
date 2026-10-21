"""Long-lived threads for reading remote images with GDAL.

On Windows, a thread that has read over HTTP with GDAL deadlocks when it
exits: GDAL's per-thread curl connections are cleaned up as the thread ends,
and that hangs while holding the GIL, which stops the whole app. The Dash
server starts a thread per browser connection and ends it when the
connection closes, so remote reads must not run on those threads. `run` does
the work on one of a fixed set of threads that never exit while the app runs.

The threads are daemons so they aren't joined, and so don't exit, when Python
shuts down; the process ends with them still waiting for work.
"""

import queue
import threading
from concurrent.futures import Future

from change_viewer import config

_jobs = queue.SimpleQueue()
_started = False
_start_lock = threading.Lock()


def _work():
    while True:
        future, fn, args, kwargs = _jobs.get()
        if not future.set_running_or_notify_cancel():
            continue
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as e:
            future.set_exception(e)
        # Drop references so a finished job's data isn't kept until the next.
        del future, fn, args, kwargs


def _start():
    global _started
    with _start_lock:
        if _started:
            return
        for i in range(config.GDAL_WORKERS):
            threading.Thread(target=_work, name=f"gdal-{i}", daemon=True).start()
        _started = True


def run(fn, *args, **kwargs):
    """Call `fn(*args, **kwargs)` on a worker thread and return its result.

    Blocks until it's done; an exception raised by `fn` is raised here. Don't
    call `run` from inside `fn`: with every worker waiting, nothing would run.
    """
    _start()
    future = Future()
    _jobs.put((future, fn, args, kwargs))
    return future.result()
