# Receipt Scanner

A small self-hosted web app for keeping receipts. Snap a photo on your phone (or upload one), and it:

- saves it as greyscale, finds the receipt in the photo, crops and straightens it, and flattens curls and
  creases with a neural unwarping model;
- has an AI model read the **vendor, total, date** and a short **description** of what was bought, and
  turns sideways photos upright;
- stores everything in SQLite, with editable fields, **tags**, tag filtering and CSV export.

It runs on your own computer; you use it from your phone's browser on the same network.

<p align="center">
  <img src="docs/screenshots/phone-light.png" width="260" alt="Receipt list on a phone">
  <img src="docs/screenshots/phone-filter.png" width="260" alt="Receipts filtered by the business and travel tags">
  <img src="docs/screenshots/phone-lightbox.png" width="260" alt="A cropped and straightened receipt in the image viewer">
</p>

<p align="center"><img src="docs/screenshots/desktop.png" width="800" alt="Receipt list in a desktop browser"></p>

<sub>Screenshots use made-up receipts.</sub>

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs the right Python (3.13) and
  all dependencies on first run. Plain pip works too: `pip install -e .`, then `receipt-scanner`.
- An [Anthropic API key](https://console.anthropic.com/), or a local [Ollama](https://ollama.com/) vision
  model (see [Choosing an extractor](#choosing-an-extractor)).

## Run

```sh
cp .env.example .env            # then put your ANTHROPIC_API_KEY in .env
uv run receipt-scanner          # serves on http://0.0.0.0:8000
```

Settings are read from `.env`, which git ignores. Any variable already set in your shell overrides the file.

Open `http://<your-computer's-LAN-IP>:8000` on your phone and tap **Take photo**, or **Upload photo** to pick
one or more existing photos (HEIC works too). On a computer you can also drag photos onto the page. Both use
plain file inputs, so it works over plain HTTP; no certificate is needed.

To get an app icon on your phone, use **Add to Home Screen** (Safari's share menu on iPhone, Chrome's menu
on Android). It then opens full-screen, without the browser's address bar. The icons are drawn by
`scripts/make_icons.py`; edit and rerun it (`uv run python scripts/make_icons.py`) to change them.

Each receipt can carry **tags** (e.g. "business", "home office"). Type in a receipt's tag box for suggestions
from tags you've used before, most-used first. Tags are case-insensitive, and a tag disappears once no
receipt uses it.

Tap tags in the **Filter** bar above the list to show only receipts with all the selected tags (or
"untagged" for receipts with none). The count and total above the list cover just the shown receipts, the
filter is kept in the page address so it survives a reload or bookmark, and **Export CSV** exports what's shown.

## How it works

1. **Image** (`imaging.py`): applies the EXIF rotation, converts to greyscale, and finds the receipt outline
   (brightness contour, falling back to an edge contour). It then perspective-warps the receipt flat,
   flattens curls and creases with the unwarper (see below), and boosts contrast (CLAHE). Both the
   uncropped and the corrected image are saved as JPEGs with a long edge of at most 1600px, typically
   80–200 KB each. If no receipt outline is found, only the original is kept. You can switch
   between the cropped and uncropped image for each receipt in the UI.
   This runs in a separate worker process (`worker.py`) that starts on the first photo, stays warm while
   more arrive, and exits after 5 idle minutes (`RECEIPT_WORKER_IDLE_S`), so OpenCV, numpy and the model
   are only in memory while you're scanning; the web server itself idles at well under 100 MB.
2. **Extraction** (`extractors/`): runs in the background after upload. If it fails (no key, network error),
   the photo and row are kept with `status = 'error'`, and you can press **Re-extract**.
3. **Storage**: `data/receipts.db` (table `receipts`), with images in `data/images/`. List thumbnails are
   made on demand in `data/thumbs/` and can be deleted at any time. Totals are stored as
   integer cents. Every field can be edited in the UI, and there is a CSV export at `/api/receipts.csv`.

## Choosing an extractor

| `RECEIPT_EXTRACTOR` | Settings | Notes |
|---|---|---|
| `anthropic` (default) | `ANTHROPIC_API_KEY`, `RECEIPT_ANTHROPIC_MODEL` (default `claude-opus-5-5`), `RECEIPT_ANTHROPIC_TIMEOUT` (default 90s, 1 retry) | Uses structured JSON output with low effort. Server-side refusal fallback is on. |
| `ollama` | `RECEIPT_OLLAMA_MODEL` (default `qwen2.5vl:7b`), `OLLAMA_HOST` | Fully local. Needs a vision model: `ollama pull qwen2.5vl:7b` |

To add a backend, write a class with a `name` and `extract(jpeg_bytes) -> (ReceiptFields, raw_text)`, then
register it in `extractors/__init__.py`.

## Unwarping

`RECEIPT_UNWARPER=uvdoc` (default) flattens curved or crumpled receipts with
[UVDoc](https://github.com/tanguymagne/UVDoc) (MIT), run with onnxruntime on the CPU in about 0.2 s per
receipt. On first start the server downloads the model's ONNX export (~15 MB, from the `docuwarp`
package on PyPI, verified by SHA-256) into `data/models/`. Until it's ready, or if it fails, receipts get
the plain crop. `RECEIPT_UNWARPER=none` turns it off. Backends live in `unwarp/`, and adding one works the
same way as adding an extractor.

Use **Re-crop** on a receipt to rerun detection and unwarping on its stored photo.

Other settings: `RECEIPT_DATA_DIR` (default `./data`), `HOST`, `PORT`.

## Security and privacy

- **There is no login.** Anyone who can reach the server can see, edit and delete every receipt. By default
  it listens on all network interfaces (`HOST=0.0.0.0`) so your phone can reach it over your home network.
  Only run it on a network you trust, and don't expose it to the internet (no port forwarding). Set
  `HOST=127.0.0.1` to allow only the computer it runs on.
- **Receipt photos go to Anthropic** when the `anthropic` extractor is used (the default). Use `ollama` to
  keep everything on your machine.
- Your data lives in `data/` (database, photos, downloaded model) and your key in `.env`; both are ignored
  by git. Back up `data/` if you care about the receipts.

## API

| Method | Path | |
|---|---|---|
| POST | `/api/receipts` | multipart `file`; returns the new row (`status: pending`) |
| GET | `/api/receipts`, `/api/receipts/{id}` | list (optional `?tag=a&tag=b` for receipts with all those tags, or `?untagged=1`) / fetch |
| PATCH | `/api/receipts/{id}` | `vendor`, `description`, `total_cents`, `currency`, `receipt_date`, `use_scan` |
| POST | `/api/receipts/{id}/extract` | re-run extraction on the selected image |
| POST | `/api/receipts/{id}/recrop` | re-run receipt detection + unwarping on the stored original |
| POST | `/api/receipts/{id}/rotate` | rotate the stored images 90° clockwise |
| GET | `/api/tags` | every tag in use with its receipt count, most-used first |
| POST | `/api/receipts/{id}/tags` | JSON `{"name": ...}`; adds a tag (created if new) |
| DELETE | `/api/receipts/{id}/tags/{name}` | removes a tag from the receipt |
| DELETE | `/api/receipts/{id}` | deletes the row and its images |
| GET | `/api/receipts/{id}/image/{original\|scan}` | JPEG |
| GET | `/api/receipts/{id}/thumb/{original\|scan}` | small JPEG for the list |
| GET | `/api/receipts.csv` | export; takes the same `tag` / `untagged` filters |

## License

[MIT](LICENSE). The UVDoc model it downloads is also MIT-licensed.
