"""Image handling: EXIF orientation, greyscale, receipt detection + perspective correction.

Heavy (OpenCV, numpy): only imported inside the short-lived image worker processes (see worker.py).
"""

import io
import logging

import cv2
import numpy as np
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener()  # iPhone photos uploaded from a computer are often HEIC

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


def _intersect(a: np.ndarray, b: np.ndarray) -> np.ndarray | None:
    """Intersection of the infinite lines through segments a and b (each x1, y1, x2, y2)."""
    p, r = a[:2], a[2:] - a[:2]
    q, s = b[:2], b[2:] - b[:2]
    denom = r[0] * s[1] - r[1] * s[0]
    if abs(denom) < 1e-6:
        return None
    t = ((q[0] - p[0]) * s[1] - (q[1] - p[1]) * s[0]) / denom
    return p + t * r


def _line_quad(small: np.ndarray) -> np.ndarray | None:
    """Fallback for low-contrast photos (white receipt on a white sheet) where neither mask yields a
    closed outline: build quads from straight edge segments and keep the one whose four sides are
    best supported by edges. Returns corners in `small`'s coordinates, or None."""
    h, w = small.shape
    rect = lambda k: cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    # Wipe out the printed text first (it's what makes line detection noisy), then use low Canny
    # thresholds: paper-against-bedsheet edges are faint.
    notext = cv2.morphologyEx(small, cv2.MORPH_CLOSE, rect(15))
    edges = cv2.Canny(cv2.GaussianBlur(notext, (5, 5), 0), 10, 60)
    segs = cv2.HoughLinesP(edges, 1, np.pi / 180, 30, minLineLength=0.06 * max(h, w), maxLineGap=15)
    if segs is None:
        return None
    segs = segs.reshape(-1, 4).astype(np.float32)
    dx, dy = np.abs(segs[:, 2] - segs[:, 0]), np.abs(segs[:, 3] - segs[:, 1])
    length = np.hypot(dx, dy)
    tan30 = np.tan(np.radians(30))
    longest = lambda sel: segs[sel][np.argsort(-length[sel])][:12]
    vert, horiz = longest(dx < dy * tan30), longest(dy < dx * tan30)
    if len(vert) < 2 or len(horiz) < 2:
        return None

    support = cv2.dilate(edges, rect(5)) > 0
    frame_area = h * w

    def side_support(p0, p1):
        n = max(int(np.hypot(*(p1 - p0)) / 3), 2)
        t = np.linspace(0, 1, n)[:, None]
        pts = np.rint(p0 + t * (p1 - p0)).astype(int)
        inside = (pts[:, 0] >= 0) & (pts[:, 0] < w) & (pts[:, 1] >= 0) & (pts[:, 1] < h)
        hits = np.zeros(n, bool)
        hits[inside] = support[pts[inside, 1], pts[inside, 0]]
        return hits.mean()

    best, best_score = None, 0.0
    slack = 0.02 * max(h, w)
    for i in range(len(vert)):
        for j in range(i + 1, len(vert)):
            for k in range(len(horiz)):
                for m in range(k + 1, len(horiz)):
                    corners = [_intersect(a, b) for a, b in
                               ((vert[i], horiz[k]), (vert[j], horiz[k]), (vert[j], horiz[m]), (vert[i], horiz[m]))]
                    if any(c is None for c in corners):
                        continue
                    quad = _order_corners(np.array(corners))
                    if (quad[:, 0].min() < -slack or quad[:, 1].min() < -slack
                            or quad[:, 0].max() > w + slack or quad[:, 1].max() > h + slack):
                        continue
                    if not cv2.isContourConvex(quad.reshape(-1, 1, 2)):
                        continue
                    area = cv2.contourArea(quad)
                    if not MIN_AREA_FRAC * frame_area <= area <= 0.98 * frame_area:
                        continue
                    sides = [(quad[n], quad[(n + 1) % 4]) for n in range(4)]
                    fracs = [side_support(*s) for s in sides]
                    if min(fracs) < 0.4:  # every side must really be an edge, not just a long sheet fold (torn edges score ~0.5)
                        continue
                    score = sum(f * np.hypot(*(b - a)) for f, (a, b) in zip(fracs, sides))
                    if score > best_score:
                        best, best_score = quad, score
    return best


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
            return _finish_quad(quad, scale, margin)
    quad = _line_quad(small)
    return None if quad is None else _finish_quad(quad, scale, margin)


def _finish_quad(quad, scale: float, margin: float) -> np.ndarray:
    """Scale detection-size corners back to full resolution, grow by `margin`, order TL, TR, BR, BL."""
    quad = np.asarray(quad, dtype=np.float32).reshape(4, 2) / scale
    centre = quad.mean(axis=0)
    return _order_corners(centre + (quad - centre) * (1 + margin))


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
