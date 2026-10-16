"""Image handling: EXIF orientation, greyscale, receipt detection + perspective correction."""

import io
import logging

import cv2
import numpy as np
from PIL import Image, ImageOps

DETECT_EDGE = 1000   # long edge used for contour detection
MAX_EDGE = 1600      # long edge of stored / sent images
JPEG_QUALITY = 82
MIN_AREA_FRAC = 0.15  # receipt must cover at least this fraction of the frame

# Detection settings. Without an unwarper the crop must already be a clean rectangle; with one, a loose
# crop (with a little margin so the unwarper can see the receipt's edges) is enough.
STRICT = {"min_fill": 0.85, "margin": 0.0}
LOOSE = {"min_fill": 0.7, "margin": 0.05}

log = logging.getLogger(__name__)


def load_grey(data: bytes) -> np.ndarray:
    """Decode an uploaded image, honour EXIF rotation, return a greyscale uint8 array."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)  # phones store rotation in EXIF; cv2 would ignore it
    return np.array(img.convert("L"))


def limit_size(grey: np.ndarray, max_edge: int = MAX_EDGE) -> np.ndarray:
    h, w = grey.shape
    scale = max_edge / max(h, w)
    if scale >= 1:
        return grey
    return cv2.resize(grey, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)


def to_jpeg(grey: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", grey, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise ValueError("JPEG encoding failed")
    return buf.tobytes()


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Return corners ordered TL, TR, BR, BL."""
    pts = pts.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()  # y - x
    return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]], dtype=np.float32)


def _candidate_masks(small: np.ndarray):
    blur = cv2.GaussianBlur(small, (5, 5), 0)
    rect = lambda k: cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    # 1) brightness — receipts are usually the brightest large object. Close over the printed text,
    #    then open to cut thin bridges to bright background patches (fabric highlights, other paper).
    _, bright = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, rect(9))
    yield cv2.morphologyEx(bright, cv2.MORPH_OPEN, rect(25))
    # 2) edges — for receipts on light, smooth backgrounds where brightness doesn't separate them
    edges = cv2.Canny(blur, 50, 150)
    yield cv2.morphologyEx(edges, cv2.MORPH_CLOSE, rect(5), iterations=2)


def find_receipt_quad(grey: np.ndarray, min_fill: float = 0.85, margin: float = 0.0) -> np.ndarray | None:
    """Find the receipt's four corners in full-resolution coordinates, or None.

    min_fill: how much of its rotated bounding box a non-rectangular outline must fill to be accepted.
    margin: grow the result by this fraction around its centre.
    """
    h, w = grey.shape
    scale = DETECT_EDGE / max(h, w)
    small = cv2.resize(grey, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else grey
    scale = min(scale, 1.0)
    frame_area = small.shape[0] * small.shape[1]

    for mask in _candidate_masks(small):
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
            area = cv2.contourArea(cnt)
            if area < MIN_AREA_FRAC * frame_area:
                break
            if area > 0.98 * frame_area:
                continue  # that's the whole frame, not a receipt
            hull = cv2.convexHull(cnt)
            approx = cv2.approxPolyDP(hull, 0.02 * cv2.arcLength(hull, True), True)
            if (len(approx) == 4 and cv2.isContourConvex(approx)
                    and cv2.contourArea(approx) >= 0.97 * cv2.contourArea(hull)):  # don't clip corners
                quad = approx
            else:
                # Not a clean quadrilateral (crumpled / curled); accept its rotated bounding box
                # only if the contour fills most of it.
                rect = cv2.minAreaRect(cnt)
                rw, rh = rect[1]
                if rw * rh == 0 or area / (rw * rh) < min_fill:
                    continue
                quad = cv2.boxPoints(rect)
            quad = np.asarray(quad, dtype=np.float32).reshape(4, 2) / scale
            centre = quad.mean(axis=0)
            return _order_corners(centre + (quad - centre) * (1 + margin))
    return None


def warp(grey: np.ndarray, quad: np.ndarray) -> np.ndarray:
    tl, tr, br, bl = quad
    width = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    height = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    dst = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32)
    m = cv2.getPerspectiveTransform(quad, dst)
    return cv2.warpPerspective(grey, m, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def enhance(grey: np.ndarray) -> np.ndarray:
    """Local contrast boost that keeps greys (thresholding to pure B/W tends to hurt OCR)."""
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(grey)


ROTATE_CW = cv2.ROTATE_90_CLOCKWISE

ROTATIONS = {  # which image edge the top of the receipt text faces -> rotation that makes it upright
    "right": cv2.ROTATE_90_COUNTERCLOCKWISE,
    "left": cv2.ROTATE_90_CLOCKWISE,
    "bottom": cv2.ROTATE_180,
}


def rotate_jpeg(data: bytes, code: int) -> bytes:
    grey = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    return to_jpeg(cv2.rotate(grey, code))


def scan_from_grey(grey: np.ndarray, unwarper=None) -> bytes | None:
    """Crop and straighten the receipt; if an unwarper is given (anything with `unwarp(grey) -> grey`),
    also flatten curls and creases with it. Returns None if no receipt was found."""
    quad = find_receipt_quad(grey, **(LOOSE if unwarper else STRICT))
    if quad is None:
        return None
    scan = warp(grey, quad)
    if unwarper is not None:
        try:
            scan = unwarper.unwarp(scan)
        except Exception as e:  # never lose the scan because the unwarper failed
            log.warning("unwarping skipped: %s", e)
    return to_jpeg(enhance(limit_size(scan)))


def process(data: bytes, unwarper=None) -> tuple[bytes, bytes | None]:
    """Return (original greyscale JPEG, corrected scan JPEG or None if no receipt was found)."""
    grey = load_grey(data)
    return to_jpeg(limit_size(grey)), scan_from_grey(grey, unwarper)
