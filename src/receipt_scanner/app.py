"""FastAPI app: upload a receipt photo, store it, extract fields, keep everything in SQLite."""

import csv
import io
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import imaging
from .db import DB, EDITABLE, utc_now
from .extractors import ExtractionError, Extractor, get_extractor
from .unwarp import Unwarper, get_unwarper

log = logging.getLogger("receipt_scanner")

DATA_DIR = Path(os.environ.get("RECEIPT_DATA_DIR", "data")).resolve()
IMAGE_DIR = DATA_DIR / "images"
IMAGE_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 30 * 1024 * 1024

db = DB(DATA_DIR / "receipts.db")


@lru_cache(maxsize=1)
def extractor() -> Extractor:
    return get_extractor()


@lru_cache(maxsize=1)
def unwarper() -> Unwarper | None:
    return get_unwarper(DATA_DIR / "models")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if (u := unwarper()) is not None:
        u.start()  # loads (and on first run downloads) the model in the background
    yield


app = FastAPI(title="Receipt Scanner", lifespan=lifespan)


def rotate_images(row: dict, code: int) -> None:
    """Rotate the stored original and scan in place (they share orientation)."""
    for key in ("original_path", "scan_path"):
        if row[key]:
            path = DATA_DIR / row[key]
            path.write_bytes(imaging.rotate_jpeg(path.read_bytes(), code))
    db.update(row["id"], image_version=row["image_version"] + 1)


def make_scan(row: dict) -> bool:
    """(Re)build the scan from the stored original. Returns False if no receipt was found,
    in which case any existing scan is left alone."""
    grey = imaging.load_grey((DATA_DIR / row["original_path"]).read_bytes())
    scan = imaging.scan_from_grey(grey, unwarper())
    if scan is None:
        return False
    scan_rel = row["scan_path"] or row["original_path"].replace("_original.jpg", "_scan.jpg")
    (DATA_DIR / scan_rel).write_bytes(scan)
    use_scan = row["use_scan"] if row["scan_path"] else 1
    db.update(row["id"], scan_path=scan_rel, use_scan=use_scan, image_version=row["image_version"] + 1)
    return True


def ensure_scan(row: dict) -> dict:
    """Retry receipt detection on the stored original if no scan exists yet."""
    if not row["scan_path"]:
        make_scan(row)
    return db.get(row["id"])


def run_extraction(rid: int) -> None:
    row = db.get(rid)
    if row is None:
        return
    try:
        row = ensure_scan(row)
    except Exception:
        log.exception("receipt detection failed for receipt %s", rid)
    path = row["scan_path"] if row["use_scan"] and row["scan_path"] else row["original_path"]
    try:
        ex = extractor()
        fields, raw = ex.extract((DATA_DIR / path).read_bytes())
    except ExtractionError as e:
        db.update(rid, status="error", error=str(e))
        return
    except Exception as e:  # never lose the receipt because extraction blew up
        log.exception("extraction failed for receipt %s", rid)
        db.update(rid, status="error", error=f"{type(e).__name__}: {e}")
        return
    db.update(
        rid,
        status="done",
        vendor=fields.vendor,
        total_cents=fields.total_cents(),
        currency=fields.currency,
        receipt_date=fields.date,
        description=fields.description,
        extractor=ex.name,
        raw_json=raw,
    )
    if fields.text_top in imaging.ROTATIONS:
        rotate_images(db.get(rid), imaging.ROTATIONS[fields.text_top])


STALE_AFTER_S = 180  # longer than any extraction should take (Anthropic: 90s timeout x 2 attempts)


def is_stale(row: dict) -> bool:
    started = row.get("extract_started_at")
    if not started:
        return True
    age = datetime.now(timezone.utc) - datetime.fromisoformat(started.replace("Z", "+00:00"))
    return age.total_seconds() > STALE_AFTER_S


def public(row: dict) -> dict:
    rid = row["id"]
    out = {k: v for k, v in row.items() if k not in ("original_path", "scan_path", "raw_json")}
    v = f"?v={row['image_version']}"
    out["original_url"] = f"/api/receipts/{rid}/image/original{v}"
    out["scan_url"] = f"/api/receipts/{rid}/image/scan{v}" if row["scan_path"] else None
    return out


def get_or_404(rid: int) -> dict:
    row = db.get(rid)
    if row is None:
        raise HTTPException(404, "receipt not found")
    return row


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/receipts", status_code=201)
async def upload(background: BackgroundTasks, file: UploadFile = File(...)):
    started = time.monotonic()
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "image too large")
    try:
        original, scan = await run_in_threadpool(imaging.process, data, unwarper())
    except Exception as e:
        raise HTTPException(400, f"could not read image: {e}") from e

    stem = uuid.uuid4().hex
    original_rel = f"images/{stem}_original.jpg"
    (DATA_DIR / original_rel).write_bytes(original)
    scan_rel = None
    if scan is not None:
        scan_rel = f"images/{stem}_scan.jpg"
        (DATA_DIR / scan_rel).write_bytes(scan)

    rid = db.insert(original_rel, scan_rel)
    log.info("receipt %s: %d KB upload processed in %.1fs (cropped: %s)",
             rid, len(data) // 1024, time.monotonic() - started, scan is not None)
    background.add_task(run_extraction, rid)
    return public(db.get(rid))


@app.get("/api/receipts")
def list_receipts(response: Response):
    # Lets the browser measure elapsed extraction time without trusting the phone's clock.
    response.headers["X-Server-Time"] = utc_now()
    return [public(r) for r in db.list()]


@app.get("/api/receipts.csv")
def export_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "receipt_date", "vendor", "description", "total", "currency", "status", "created_at"])
    for r in db.list():
        total = f"{r['total_cents'] / 100:.2f}" if r["total_cents"] is not None else ""
        w.writerow([r["id"], r["receipt_date"] or "", r["vendor"] or "", r["description"] or "", total, r["currency"] or "", r["status"], r["created_at"]])
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=receipts.csv"},
    )


@app.get("/api/receipts/{rid}")
def get_receipt(rid: int):
    return public(get_or_404(rid))


class ReceiptPatch(BaseModel):
    vendor: str | None = None
    total_cents: int | None = None
    currency: str | None = None
    receipt_date: str | None = None
    description: str | None = None
    use_scan: bool | None = None


@app.patch("/api/receipts/{rid}")
def edit_receipt(rid: int, patch: ReceiptPatch):
    get_or_404(rid)
    fields = {k: v for k, v in patch.model_dump(exclude_unset=True).items() if k in EDITABLE}
    if "use_scan" in fields:
        fields["use_scan"] = int(bool(fields["use_scan"]))
    db.update(rid, **fields)
    return public(db.get(rid))


@app.post("/api/receipts/{rid}/extract")
def re_extract(rid: int, background: BackgroundTasks):
    row = get_or_404(rid)
    if row["status"] == "pending" and not is_stale(row):
        raise HTTPException(409, "extraction already running")
    db.update(rid, status="pending", error=None, extract_started_at=utc_now())
    background.add_task(run_extraction, rid)
    return public(db.get(rid))


@app.post("/api/receipts/{rid}/recrop")
async def recrop(rid: int):
    """Re-run receipt detection (and unwarping) on the stored original."""
    row = get_or_404(rid)
    if not await run_in_threadpool(make_scan, row):
        raise HTTPException(422, "couldn't find the receipt's edges in this photo")
    return public(db.get(rid))


@app.post("/api/receipts/{rid}/rotate")
def rotate(rid: int):
    """Rotate the stored images 90° clockwise (manual fix when auto-orientation gets it wrong)."""
    rotate_images(get_or_404(rid), imaging.ROTATE_CW)
    return public(db.get(rid))


@app.delete("/api/receipts/{rid}", status_code=204)
def delete_receipt(rid: int):
    row = get_or_404(rid)
    for key in ("original_path", "scan_path"):
        if row[key]:
            (DATA_DIR / row[key]).unlink(missing_ok=True)
    db.delete(rid)


@app.get("/api/receipts/{rid}/image/{kind}")
def image(rid: int, kind: str):
    row = get_or_404(rid)
    key = {"original": "original_path", "scan": "scan_path"}.get(kind)
    if key is None or not row[key]:
        raise HTTPException(404, "no such image")
    return FileResponse(DATA_DIR / row[key], media_type="image/jpeg")
