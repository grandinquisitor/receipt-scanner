import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import receipt_scanner.app as appmod
from receipt_scanner import worker
from receipt_scanner.db import DB
from receipt_scanner.extractors import ExtractionError, ReceiptFields

from conftest import make_jpeg


class FakeExtractor:
    name = "fake:1"

    def __init__(self):
        self.fields = ReceiptFields(vendor="Corner Shop", total="12.34", currency="USD", date="2026-10-01",
                                    description="milk and bread")
        self.error = None
        self.calls = 0

    def extract(self, jpeg):
        self.calls += 1
        if self.error:
            raise self.error
        return self.fields, '{"raw": true}'


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A fresh data dir and database per test, with the extractor and image worker stubbed out."""
    (tmp_path / "images").mkdir()
    (tmp_path / "thumbs").mkdir()
    monkeypatch.setattr(appmod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(appmod, "THUMB_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(appmod, "db", DB(tmp_path / "receipts.db"))
    ex = FakeExtractor()
    monkeypatch.setattr(appmod, "extractor", lambda: ex)
    ex.scan = make_jpeg(200, 600)  # what the "receipt detection" finds; None for none
    monkeypatch.setattr(worker, "process", lambda data, model_dir: (make_jpeg(400, 600), ex.scan))
    monkeypatch.setattr(worker, "scan", lambda data, model_dir: ex.scan)
    return ex


@pytest.fixture
def client(fake):
    return TestClient(appmod.app)  # not as a context manager, so the lifespan (model download) doesn't run


def upload(client) -> dict:
    r = client.post("/api/receipts", files={"file": ("r.jpg", make_jpeg(), "image/jpeg")})
    assert r.status_code == 201, r.text
    return client.get(f"/api/receipts/{r.json()['id']}").json()  # background extraction has run by now


def size(client, url) -> tuple[int, int]:
    r = client.get(url)
    assert r.status_code == 200
    return Image.open(io.BytesIO(r.content)).size


def test_upload_extracts_fields(client, fake):
    r = upload(client)
    assert r["status"] == "done"
    assert (r["vendor"], r["total_cents"], r["currency"], r["receipt_date"], r["description"]) == (
        "Corner Shop", 1234, "USD", "2026-10-01", "milk and bread")
    assert r["extractor"] == "fake:1"
    assert r["use_scan"] == 1 and r["scan_url"]
    assert "raw_json" not in r and "original_path" not in r


def test_upload_rejects_empty_file(client):
    r = client.post("/api/receipts", files={"file": ("r.jpg", b"", "image/jpeg")})
    assert r.status_code == 400


def test_upload_without_receipt_edges_keeps_only_original(client, fake):
    fake.scan = None
    r = upload(client)
    assert r["status"] == "done"
    assert r["use_scan"] == 0 and r["scan_url"] is None
    assert size(client, r["original_url"]) == (400, 600)


def test_extraction_error_keeps_the_receipt(client, fake):
    fake.error = ExtractionError("no API key")
    r = upload(client)
    assert (r["status"], r["error"]) == ("error", "no API key")
    fake.error = RuntimeError("boom")
    r = client.post(f"/api/receipts/{r['id']}/extract").json()
    r = client.get(f"/api/receipts/{r['id']}").json()
    assert (r["status"], r["error"]) == ("error", "RuntimeError: boom")


def test_sideways_text_is_turned_upright(client, fake):
    fake.fields.text_top = "right"
    r = upload(client)
    assert r["image_version"] == 1
    assert size(client, r["scan_url"]) == (600, 200)
    assert size(client, r["original_url"]) == (600, 400)


def test_rotate_turns_both_images_and_busts_cache(client):
    r = upload(client)
    r2 = client.post(f"/api/receipts/{r['id']}/rotate").json()
    assert r2["image_version"] == r["image_version"] + 1
    assert r2["scan_url"] != r["scan_url"]
    assert size(client, r2["scan_url"]) == (600, 200)


def test_re_extract_refuses_while_running_unless_stale(client, fake):
    rid = upload(client)["id"]
    appmod.db.update(rid, status="pending")
    assert client.post(f"/api/receipts/{rid}/extract").status_code == 409
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=appmod.STALE_AFTER_S + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    appmod.db.update(rid, extract_started_at=long_ago)
    assert client.post(f"/api/receipts/{rid}/extract").status_code == 200
    assert fake.calls == 2


def test_recrop(client, fake):
    fake.scan = None
    r = upload(client)
    assert client.post(f"/api/receipts/{r['id']}/recrop").status_code == 422
    fake.scan = make_jpeg(250, 700)
    r = client.post(f"/api/receipts/{r['id']}/recrop").json()
    assert r["use_scan"] == 1 and r["image_version"] == 1
    assert size(client, r["scan_url"]) == (250, 700)


def test_patch_edits_only_given_fields(client):
    rid = upload(client)["id"]
    r = client.patch(f"/api/receipts/{rid}", json={"vendor": "Renamed", "use_scan": False, "status": "error"}).json()
    assert (r["vendor"], r["use_scan"], r["status"], r["total_cents"]) == ("Renamed", 0, "done", 1234)
    r = client.patch(f"/api/receipts/{rid}", json={"total_cents": None}).json()
    assert r["total_cents"] is None and r["vendor"] == "Renamed"


def test_tags_and_filters(client):
    a, b, c = upload(client)["id"], upload(client)["id"], upload(client)["id"]
    client.post(f"/api/receipts/{a}/tags", json={"name": "business"})
    client.post(f"/api/receipts/{b}/tags", json={"name": "Business"})
    r = client.post(f"/api/receipts/{b}/tags", json={"name": "travel"}).json()
    assert r["tags"] == ["business", "travel"]
    assert client.post(f"/api/receipts/{c}/tags", json={"name": "  "}).status_code == 422

    listed = lambda q: [r["id"] for r in client.get(f"/api/receipts{q}").json()]
    assert listed("") == [c, b, a]
    assert listed("?tag=BUSINESS") == [b, a]
    assert listed("?tag=business&tag=travel") == [b]
    assert listed("?untagged=1") == [c]
    assert client.get("/api/tags").json() == [{"name": "business", "count": 2}, {"name": "travel", "count": 1}]

    r = client.delete(f"/api/receipts/{b}/tags/travel").json()
    assert r["tags"] == ["business"]
    assert [t["name"] for t in client.get("/api/tags").json()] == ["business"]


def test_csv_export(client, fake):
    a = upload(client)["id"]
    fake.fields = ReceiptFields(vendor='Comma, "Quote" Ltd', total=None)
    b = upload(client)["id"]
    client.post(f"/api/receipts/{a}/tags", json={"name": "z"})
    client.post(f"/api/receipts/{a}/tags", json={"name": "a"})

    r = client.get("/api/receipts.csv")
    assert r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [int(row["id"]) for row in rows] == [b, a]
    assert (rows[0]["vendor"], rows[0]["total"], rows[0]["tags"]) == ('Comma, "Quote" Ltd', "", "")
    assert (rows[1]["total"], rows[1]["currency"], rows[1]["tags"]) == ("12.34", "USD", "a; z")
    only_a = list(csv.DictReader(io.StringIO(client.get("/api/receipts.csv?tag=a").text)))
    assert [int(row["id"]) for row in only_a] == [a]


def test_thumbnails_are_cropped_and_cached(client, fake, tmp_path):
    fake.scan = make_jpeg(800, 4000)  # a long receipt
    r = upload(client)
    w, h = size(client, r["scan_thumb_url"])
    assert w == 360 and h < 600
    assert len(list((tmp_path / "thumbs").iterdir())) == 1


def test_delete_removes_images_and_thumbs(client, tmp_path):
    r = upload(client)
    client.get(r["scan_thumb_url"])
    client.get(r["original_thumb_url"])
    assert client.delete(f"/api/receipts/{r['id']}").status_code == 204
    assert list((tmp_path / "images").iterdir()) == []
    assert list((tmp_path / "thumbs").iterdir()) == []
    assert client.get(f"/api/receipts/{r['id']}").status_code == 404


@pytest.mark.parametrize("path", ["/api/receipts/999", "/api/receipts/999/image/scan", "/api/receipts/999/thumb/original"])
def test_missing_receipt_is_404(client, path):
    assert client.get(path).status_code == 404


def test_unknown_image_kind_is_404(client, fake):
    fake.scan = None
    rid = upload(client)["id"]
    assert client.get(f"/api/receipts/{rid}/image/raw").status_code == 404
    assert client.get(f"/api/receipts/{rid}/image/scan").status_code == 404


def test_index_and_robots(client):
    assert "<html" in client.get("/").text.lower()
    assert "Disallow: /" in client.get("/robots.txt").text
