import threading

import pytest

from change_viewer import config, workers


def test_run_returns_result():
    assert workers.run(lambda a, b=0: a + b, 2, b=3) == 5


def test_run_raises_the_error():
    def fail():
        raise ValueError("bad")

    with pytest.raises(ValueError, match="bad"):
        workers.run(fail)


def test_run_uses_long_lived_daemon_threads():
    def current():
        return threading.current_thread()

    threads = {workers.run(current) for _ in range(20)}
    assert threading.current_thread() not in threads
    assert len(threads) <= config.GDAL_WORKERS
    assert all(t.daemon and t.is_alive() for t in threads)


def test_runs_in_parallel():
    # Every worker must be running at once for the barrier to pass.
    barrier = threading.Barrier(config.GDAL_WORKERS, timeout=5)
    results = []

    def call():
        results.append(workers.run(barrier.wait))

    callers = [threading.Thread(target=call) for _ in range(config.GDAL_WORKERS)]
    for t in callers:
        t.start()
    for t in callers:
        t.join()
    assert sorted(results) == list(range(config.GDAL_WORKERS))
