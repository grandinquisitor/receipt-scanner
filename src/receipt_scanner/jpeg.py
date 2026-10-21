"""Quick operations on the stored greyscale JPEGs that the web server does itself: thumbnails and
rotation. They use only Pillow, so the server never loads OpenCV/numpy/onnxruntime (see worker.py)."""

import io

from PIL import Image

THUMB_WIDTH = 360    # list thumbnails are shown ~110px wide; this stays sharp on 3x phone screens
THUMB_QUALITY = 75
THUMB_MAX_ASPECT = 150 / 110  # height/width of the list's thumbnail box, which shows the top of the receipt
JPEG_QUALITY = 82    # matches imaging.JPEG_QUALITY

ROTATE_CW = Image.Transpose.ROTATE_270  # Pillow's ROTATE_* turn counter-clockwise

ROTATIONS = {  # which image edge the top of the receipt text faces -> rotation that makes it upright
    "right": Image.Transpose.ROTATE_90,   # 90° counter-clockwise
    "left": Image.Transpose.ROTATE_270,   # 90° clockwise
    "bottom": Image.Transpose.ROTATE_180,
}


def _encode(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def make_thumb(jpeg: bytes) -> bytes:
    """Shrink a stored JPEG to list-thumbnail size, keeping only the top part that the list's thumbnail
    box shows (long receipts would otherwise make tall, heavy thumbnails)."""
    img = Image.open(io.BytesIO(jpeg))
    img.draft("L", (THUMB_WIDTH, THUMB_WIDTH))  # let the JPEG decoder downscale cheaply first
    img = img.convert("L")
    w, h = img.size
    if h > w * THUMB_MAX_ASPECT:
        img = img.crop((0, 0, w, round(w * THUMB_MAX_ASPECT)))
        h = img.height
    if w > THUMB_WIDTH:
        img = img.resize((THUMB_WIDTH, round(h * THUMB_WIDTH / w)), Image.LANCZOS)
    return _encode(img, THUMB_QUALITY)


def rotate_jpeg(data: bytes, rotation: Image.Transpose) -> bytes:
    img = Image.open(io.BytesIO(data))
    return _encode(img.convert("L").transpose(rotation), JPEG_QUALITY)
