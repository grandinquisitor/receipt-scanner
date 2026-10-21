"""Run the heavy image processing (OpenCV, numpy, the UVDoc model) in a separate worker process.

Those libraries and the model take ~85 MB once loaded but are only needed while photos are being processed,
so the web server never imports them. The worker process is started on the first job, kept warm while
more photos arrive (so a batch only pays the start-up once), and exits after RECEIPT_WORKER_IDLE_S seconds
(default 300) without work, giving the memory back. Starting it costs several seconds on a Raspberry Pi
(imports, loading the model, onnxruntime's slow first run), which is why it isn't started per job.
Jobs run one at a time, which also caps peak memory.
"""

import functools
import logging
import multiprocessing
import os
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

log = logging.getLogger(__name__)

IDLE_S = float(os.environ.get("RECEIPT_WORKER_IDLE_S", "300"))

_lock = threading.Lock()  # held for each whole job: never two image jobs at once
_pool: ProcessPoolExecutor | None = None
_generation = 0           # bumped by every job, so a stale idle timer knows not to stop the worker
_timer: threading.Timer | None = None


def _run(fn, *args):
    global _pool, _generation, _timer
    with _lock:
        _generation += 1
        if _timer is not None:
            _timer.cancel()
        if _pool is None:
            # "spawn" starts a clean interpreter rather than forking the multi-threaded server.
            _pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
        try:
            return _pool.submit(fn, *args).result()
        except BrokenProcessPool:
            _pool = None  # the worker died (e.g. killed for lack of memory); the next job starts a new one
            raise RuntimeError("the image-processing process crashed (out of memory?)") from None
        finally:
            _timer = threading.Timer(IDLE_S, _stop_if_idle, args=(_generation,))
            _timer.daemon = True
            _timer.start()


def _stop_if_idle(generation: int) -> None:
    global _pool
    with _lock:
        if generation == _generation and _pool is not None:
            _pool.shutdown()
            _pool = None
            log.info("image worker stopped after %.0fs idle", IDLE_S)


def shutdown() -> None:
    global _pool
    with _lock:
        if _timer is not None:
            _timer.cancel()
        if _pool is not None:
            _pool.shutdown(cancel_futures=True)
            _pool = None


# ---- the jobs; these functions run in the child process ----

@functools.cache  # one per worker process, so the model stays loaded between jobs
def _unwarper(model_dir: Path):
    from .unwarp import get_unwarper  # reads RECEIPT_UNWARPER, inherited from the server's environment
    return get_unwarper(model_dir)


def _process(data: bytes, model_dir: Path) -> tuple[bytes, bytes | None]:
    from . import imaging
    return imaging.process(data, _unwarper(model_dir))


def _scan(original_jpeg: bytes, model_dir: Path) -> bytes | None:
    from . import imaging
    return imaging.scan_from_grey(imaging.load_grey(original_jpeg), _unwarper(model_dir))


# ---- what the server calls (blocking; call from a thread, not the event loop) ----

def process(data: bytes, model_dir: Path) -> tuple[bytes, bytes | None]:
    """Uploaded photo -> (greyscale original JPEG, cropped/straightened scan JPEG or None)."""
    return _run(_process, data, model_dir)


def scan(original_jpeg: bytes, model_dir: Path) -> bytes | None:
    """Re-run receipt detection and unwarping on a stored original. None if no receipt was found."""
    return _run(_scan, original_jpeg, model_dir)
