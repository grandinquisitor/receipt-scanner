import sqlite3

import pytest

from receipt_scanner.db import DB, MAX_TAG_LEN


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "receipts.db")


def add(db, *tags) -> int:
    rid = db.insert(f"images/{len(db.list())}_original.jpg", None)
    for t in tags:
        db.add_tag(rid, t)
    return rid


def ids(rows):
    return [r["id"] for r in rows]


def test_insert_sets_use_scan_from_scan_path(db):
    with_scan = db.insert("images/a_original.jpg", "images/a_scan.jpg")
    without = db.insert("images/b_original.jpg", None)
    assert db.get(with_scan)["use_scan"] == 1
    assert db.get(without)["use_scan"] == 0
    assert db.get(with_scan)["status"] == "pending"
    assert db.get(999) is None


def test_list_is_newest_first(db):
    a, b, c = add(db), add(db), add(db)
    assert ids(db.list()) == [c, b, a]


def test_tags_are_case_insensitive_and_cleaned(db):
    rid = add(db, "  Home   office ")
    db.add_tag(rid, "home OFFICE")
    assert db.get(rid)["tags"] == ["Home office"]  # first spelling wins
    assert db.all_tags() == [{"name": "Home office", "count": 1}]


@pytest.mark.parametrize("name", ["", "   ", "x" * (MAX_TAG_LEN + 1)])
def test_bad_tags_are_rejected(db, name):
    with pytest.raises(ValueError):
        db.add_tag(add(db), name)


def test_all_tags_most_used_first(db):
    add(db, "travel")
    add(db, "business", "travel")
    add(db, "business", "Alpha")
    add(db, "business")
    assert db.all_tags() == [
        {"name": "business", "count": 3},
        {"name": "travel", "count": 2},
        {"name": "Alpha", "count": 1},
    ]


def test_filter_needs_every_tag(db):
    travel = add(db, "travel")
    both = add(db, "business", "travel")
    business = add(db, "business")
    plain = add(db)
    assert ids(db.list(["travel"])) == [both, travel]
    assert ids(db.list(["Travel", "BUSINESS"])) == [both]
    assert ids(db.list(["business", "business ", "Business"])) == [business, both]  # duplicates collapse
    assert ids(db.list(["travel", "nope"])) == []
    assert ids(db.list(untagged=True)) == [plain]
    assert ids(db.list(["", " "])) == [plain, business, both, travel]  # blank tags mean no filter


def test_unused_tags_are_forgotten(db):
    """Once no receipt uses a tag it's gone, so re-adding it takes the new spelling."""
    a = add(db, "Tpyo", "Keep")
    b = add(db, "Keep")
    db.remove_tag(a, "tpyo")
    db.add_tag(b, "TPYO")
    assert db.get(b)["tags"] == ["Keep", "TPYO"]
    db.delete(a)
    db.delete(b)
    db.add_tag(add(db), "keep")
    assert db.all_tags() == [{"name": "keep", "count": 1}]


def test_delete_removes_tag_links(db):
    rid = add(db, "x")
    db.delete(rid)
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM receipt_tags").fetchone()[0] == 0


def test_migrates_old_schema_and_fails_interrupted_extractions(tmp_path):
    path = tmp_path / "receipts.db"
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        original_path TEXT NOT NULL, scan_path TEXT, use_scan INTEGER NOT NULL DEFAULT 1,
        vendor TEXT, total_cents INTEGER, currency TEXT, receipt_date TEXT,
        status TEXT NOT NULL DEFAULT 'pending', error TEXT, extractor TEXT, raw_json TEXT)""")
    c.execute("INSERT INTO receipts (original_path, vendor) VALUES ('images/old_original.jpg', 'Old Shop')")
    c.execute("INSERT INTO receipts (original_path, status) VALUES ('images/done_original.jpg', 'done')")
    c.commit()
    c.close()

    db = DB(path)
    old, done = db.get(1), db.get(2)
    assert old["vendor"] == "Old Shop"
    assert (old["image_version"], old["extract_started_at"], old["description"]) == (0, None, None)
    assert old["status"] == "error" and "interrupted" in old["error"]
    assert done["status"] == "done"
    db.add_tag(1, "works")  # tag tables were created too
    assert db.get(1)["tags"] == ["works"]
