"""Back up the receipts database and photos into one zip file.

    python3 scripts/backup.py [--data-dir data] [--out backups]

Safe to run while the server is up: the database is copied with SQLite's online backup API, and only the
photos that copy refers to are added, so the zip is always consistent. Thumbnails and the downloaded model
are left out; the app rebuilds them. Uses only the standard library, so it runs without `uv sync`.

To restore, stop the server and unzip into an empty data directory: `unzip receipts-....zip -d data`.
"""

import argparse
import os
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def warn(msg: str):
    print(msg, file=sys.stderr)


def snapshot(db_path: Path, dest: Path):
    """Copy the live database to dest, waiting out any write in progress."""
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
        if (check := out.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise SystemExit(f"database copy failed its integrity check: {check}")
    finally:
        out.close()
        src.close()


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data-dir", type=Path, default=Path(os.environ.get("RECEIPT_DATA_DIR", ROOT / "data")),
                   help="the app's data directory (default: $RECEIPT_DATA_DIR or ./data)")
    p.add_argument("--out", type=Path, default=ROOT / "backups", help="where to write the zip (default: ./backups)")
    args = p.parse_args()

    db_path = args.data_dir / "receipts.db"
    if not db_path.is_file():
        raise SystemExit(f"no database at {db_path}")
    args.out.mkdir(parents=True, exist_ok=True)
    zip_path = args.out / f"receipts-{datetime.now():%Y%m%d-%H%M%S}.zip"

    with tempfile.TemporaryDirectory() as tmp:
        db_copy = Path(tmp) / "receipts.db"
        snapshot(db_path, db_copy)
        with sqlite3.connect(db_copy) as c:
            rows = c.execute("SELECT original_path, scan_path FROM receipts").fetchall()
        images = sorted({path for row in rows for path in row if path})

        part = zip_path.with_suffix(".zip.part")  # renamed when complete, so a half-written zip never looks done
        missing = 0
        with zipfile.ZipFile(part, "w") as z:
            z.write(db_copy, "receipts.db", compress_type=zipfile.ZIP_DEFLATED)
            for rel in images:  # JPEGs are already compressed; store them as is
                if (args.data_dir / rel).is_file():
                    z.write(args.data_dir / rel, rel, compress_type=zipfile.ZIP_STORED)
                else:  # deleted since the snapshot, or lost before it
                    warn(f"skipping missing image {rel}")
                    missing += 1
        part.rename(zip_path)

    size_mb = zip_path.stat().st_size / 1e6
    print(f"{zip_path}: {len(rows)} receipts, {len(images) - missing} images, {size_mb:.1f} MB")


if __name__ == "__main__":
    main()
