"""SQLite storage for receipts."""

from __future__ import annotations  # annotations below say `list[...]`, which DB.list would otherwise shadow

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

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE  -- "Business" and "business" are the same tag
);

CREATE TABLE IF NOT EXISTS receipt_tags (
    receipt_id INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (receipt_id, tag_id)
);
"""

MAX_TAG_LEN = 40

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
        c.execute("PRAGMA foreign_keys = ON")
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
            return self._with_tags(c, [dict(row)])[0] if row else None

    def list(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM receipts ORDER BY id DESC").fetchall()
            return self._with_tags(c, [dict(r) for r in rows])

    @staticmethod
    def _with_tags(c, rows: list[dict]) -> list[dict]:
        tags: dict[int, list[str]] = {}
        for r in c.execute(
            "SELECT rt.receipt_id, t.name FROM receipt_tags rt JOIN tags t ON t.id = rt.tag_id "
            "ORDER BY t.name COLLATE NOCASE"
        ):
            tags.setdefault(r["receipt_id"], []).append(r["name"])
        for row in rows:
            row["tags"] = tags.get(row["id"], [])
        return rows

    # ---- tags ----

    @staticmethod
    def clean_tag(name: str) -> str:
        name = " ".join(name.split())  # trim and collapse whitespace
        if not name:
            raise ValueError("tag is empty")
        if len(name) > MAX_TAG_LEN:
            raise ValueError(f"tag is longer than {MAX_TAG_LEN} characters")
        return name

    def all_tags(self) -> list[dict]:
        """Every tag in use, most-used first."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT t.name, COUNT(*) AS count FROM tags t JOIN receipt_tags rt ON rt.tag_id = t.id "
                "GROUP BY t.id ORDER BY count DESC, t.name COLLATE NOCASE"
            ).fetchall()
            return [dict(r) for r in rows]

    def add_tag(self, rid: int, name: str) -> None:
        name = self.clean_tag(name)
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
            tag_id = c.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
            c.execute("INSERT OR IGNORE INTO receipt_tags (receipt_id, tag_id) VALUES (?, ?)", (rid, tag_id))

    def remove_tag(self, rid: int, name: str) -> None:
        with self.conn() as c:
            c.execute(
                "DELETE FROM receipt_tags WHERE receipt_id = ? AND tag_id = (SELECT id FROM tags WHERE name = ?)",
                (rid, name.strip()),
            )
            self._prune_tags(c)

    @staticmethod
    def _prune_tags(c) -> None:
        """Forget tags no receipt uses any more, so typos don't linger in suggestions."""
        c.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM receipt_tags)")

    def update(self, rid: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE receipts SET {cols} WHERE id = ?", (*fields.values(), rid))

    def delete(self, rid: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM receipts WHERE id = ?", (rid,))
            self._prune_tags(c)
