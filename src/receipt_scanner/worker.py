"""Run the heavy image processing (OpenCV, numpy, the UVDoc model) in a short-lived child process.

Those libraries and the model take ~85 MB once loaded, but are only needed for the few seconds after an
upload or re-crop. Each job gets a fresh process that exits when it's done, so the web server itself stays
small while idle. Jobs run one at a time, which also caps peak memory. The cost is ~1-2 s per job to start
the process and load the libraries.
"""

import multiprocessing
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

_one_at_a_time = threading.Lock()  # caps peak memory: never two image jobs at once
_spawn = multiprocessing.get_context("spawn")  # a clean interpreter, not a fork of the threaded server


def _run(fn, *args):
    with _one_at_a_time:
        # A fresh single-use pool per job: the child exits as soon as the job is done, and no idle
        # process is kept waiting for the next one.
        with ProcessPoolExecutor(max_workers=1, mp_context=_spawn) as pool:
            try:
                return pool.submit(fn, *args).result()
            except BrokenProcessPool:
                raise RuntimeError("the image-processing process crashed (out of memory?)") from None


# ---- the jobs; these functions run in the child process ----

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
