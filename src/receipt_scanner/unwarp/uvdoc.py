"""UVDoc document unwarping (Verhoeven et al., "UVDoc: Neural Grid-based Document Unwarping",
SIGGRAPH Asia 2023; https://github.com/tanguymagne/UVDoc, MIT licence).

The network predicts a coarse 2D grid saying, for each point of the flattened page, where it lies in
the input photo; we upsample that grid and resample the image with it. The ONNX export of the
network is downloaded once, on first start, from the `docuwarp` package on PyPI, pinned by hash.
"""

import hashlib
import io
import logging
import tempfile
import threading
import zipfile
from pathlib import Path

import cv2
import httpx
import numpy as np

from . import UnwarpUnavailable

log = logging.getLogger(__name__)

MODEL_FILE = "uvdoc.onnx"
MODEL_SHA256 = "98f7a03bbe37640055a70221592c3d2b1c9d43559ab57e3d4e4e0796adc2bc41"
WHEEL_URL = (
    "https://files.pythonhosted.org/packages/bc/33/d8e2eb0474df899c634a04251eb36a903af07ea4869b2885731f8b4ea016/"
    "docuwarp-1.0.2-py3-none-any.whl"
)
WHEEL_SHA256 = "da0d3838bb13b2fcf768919af94a0f1354321ae0a49e774cf5dde9f65bc0a5e9"
WHEEL_MEMBER = "docuwarp/artifacts/uvdoc.onnx"

INPUT_W, INPUT_H = 488, 712  # the network's fixed (portrait) input size


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_model(dest: Path) -> None:
    """Download the wheel, verify it, and extract the verified model to `dest` atomically."""
    log.info("downloading UVDoc model (~15 MB) from PyPI…")
    r = httpx.get(WHEEL_URL, timeout=120, follow_redirects=True)
    r.raise_for_status()
    if _sha256(r.content) != WHEEL_SHA256:
        raise RuntimeError("downloaded docuwarp wheel failed its SHA-256 check")
    model = zipfile.ZipFile(io.BytesIO(r.content)).read(WHEEL_MEMBER)
    if _sha256(model) != MODEL_SHA256:
        raise RuntimeError("extracted UVDoc model failed its SHA-256 check")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False, suffix=".part") as tmp:
        tmp.write(model)
    Path(tmp.name).chmod(0o644)
    Path(tmp.name).replace(dest)
    log.info("UVDoc model saved to %s", dest)


class UVDocUnwarper:
    name = "uvdoc"

    def __init__(self, model_dir: Path):
        self.model_path = model_dir / MODEL_FILE
        self._session = None
        self._error: str | None = None
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._load, name="uvdoc-load", daemon=True).start()

    def _load(self) -> None:
        try:
            import onnxruntime as ort  # imported lazily so "none" works without it

            ort.disable_telemetry_events()
            if not self.model_path.exists() or _sha256(self.model_path.read_bytes()) != MODEL_SHA256:
                fetch_model(self.model_path)
            self._session = ort.InferenceSession(str(self.model_path), providers=["CPUExecutionProvider"])
            log.info("UVDoc unwarper ready")
        except Exception as e:  # remembered, so we don't retry on every upload
            self._error = f"{type(e).__name__}: {e}"
            log.warning("UVDoc unwarper disabled: %s", self._error)

    def unwarp(self, grey: np.ndarray) -> np.ndarray:
        if self._session is None:
            raise UnwarpUnavailable(self._error or "UVDoc model is still loading")
        # The network expects portrait input; handle sideways crops without changing their orientation.
        if grey.shape[1] > grey.shape[0]:
            out = self._unwarp_portrait(cv2.rotate(grey, cv2.ROTATE_90_CLOCKWISE))
            return cv2.rotate(out, cv2.ROTATE_90_COUNTERCLOCKWISE)
        return self._unwarp_portrait(grey)

    def _unwarp_portrait(self, grey: np.ndarray) -> np.ndarray:
        h, w = grey.shape
        small = cv2.resize(grey, (INPUT_W, INPUT_H), interpolation=cv2.INTER_AREA)
        rgb = np.repeat(small[None, None, :, :], 3, axis=1).astype(np.float16) / 255
        grid = self._session.run(None, {"input": rgb})[0][0].astype(np.float32)  # (2, gh, gw), x/y in [-1, 1]

        # Upsample the coarse grid to the output size with align-corners semantics
        # (cv2.remap samples at integer pixel centres, so linspace over grid indices gives exactly that).
        gh, gw = grid.shape[1:]
        sx = np.tile(np.linspace(0, gw - 1, w, dtype=np.float32), (h, 1))
        sy = np.tile(np.linspace(0, gh - 1, h, dtype=np.float32)[:, None], (1, w))
        gx = cv2.remap(grid[0], sx, sy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        gy = cv2.remap(grid[1], sx, sy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

        # Normalised [-1, 1] -> source pixel coordinates (align_corners=True), then resample.
        map_x = (gx + 1) / 2 * (w - 1)
        map_y = (gy + 1) / 2 * (h - 1)
        return cv2.remap(grey, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
