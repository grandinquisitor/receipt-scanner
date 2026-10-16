"""SQLite storage for receipts."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    original_path TEXT NOT NULL,          -- greyscale, uncropped
    scan_path     TEXT,                   -- greyscale, perspective-corrected (NULL if no receipt edge found)
    use_scan      INTEGER NOT NULL DEFAULT 1,
    vendor        TEXT,
    total_cents   INTEGER,
    currency      TEXT,
    receipt_date  TEXT,                   -- ISO YYYY-MM-DD
    description   TEXT,                   -- vague summary of what was bought, e.g. "various hardware"
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | done | error
    error         TEXT,
    extractor     TEXT,                   -- e.g. "anthropic:claude-opus-5-5"
    raw_json      TEXT,                   -- raw extractor output, for debugging
    image_version INTEGER NOT NULL DEFAULT 0, -- bumped when stored images change (cache busting)
    extract_started_at TEXT                  -- when the current/last extraction started (UTC ISO)
);
"""

MIGRATIONS = {
    "image_version": "INTEGER NOT NULL DEFAULT 0",
    "extract_started_at": "TEXT",
    "description": "TEXT",
}

EDITABLE = {"description", "vendor", "total_cents", "currency", "receipt_date", "use_scan"}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(receipts)")}
            for name, decl in MIGRATIONS.items():  # columns added after the first release
                if name not in cols:
                    c.execute(f"ALTER TABLE receipts ADD COLUMN {name} {decl}")
            # Extraction runs in-process; anything still pending was interrupted by a restart.
            c.execute("UPDATE receipts SET status = 'error', error = 'interrupted (server restarted)' WHERE status = 'pending'")

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def insert(self, original_path: str, scan_path: str | None) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO receipts (original_path, scan_path, use_scan, extract_started_at) VALUES (?, ?, ?, ?)",
                (original_path, scan_path, 1 if scan_path else 0, utc_now()),
            )
            return cur.lastrowid

    def get(self, rid: int) -> dict | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM receipts WHERE id = ?", (rid,)).fetchone()
            return dict(row) if row else None

    def list(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM receipts ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]

    def update(self, rid: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE receipts SET {cols} WHERE id = ?", (*fields.values(), rid))

    def delete(self, rid: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM receipts WHERE id = ?", (rid,))
