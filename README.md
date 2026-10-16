# Receipt Scanner

Snap a photo of a receipt from your phone. The app saves it as greyscale, finds the receipt and straightens it, has an AI model read the **vendor, total and date**, and stores everything in SQLite.

## Run

```sh
cp .env.example .env            # then put your ANTHROPIC_API_KEY in .env
uv run receipt-scanner          # serves on http://0.0.0.0:8000
```

Settings are read from `.env`, which git ignores. Any variable already set in your shell overrides the file.

Open `http://<your-computer's-LAN-IP>:8000` on your phone and tap **Take a photo**. The upload uses a plain
`<input capture>`, so it works over plain HTTP; no certificate is needed.

## How it works

1. **Image** (`imaging.py`): applies the EXIF rotation, converts to greyscale, and finds the receipt outline
   (edge contour, falling back to a brightness contour). It then perspective-warps the receipt flat and boosts
   contrast (CLAHE). Both the uncropped and the corrected image are saved as JPEGs with a long edge of at most
   1600px, typically 80–200 KB each. If no receipt outline is found, only the original is kept. You can switch
   between the cropped and uncropped image for each receipt in the UI.
2. **Extraction** (`extractors/`): runs in the background after upload. If it fails (no key, network error),
   the photo and row are kept with `status = 'error'`, and you can press **Re-extract**.
3. **Storage**: `data/receipts.db` (table `receipts`), with images in `data/images/`. Totals are stored as
   integer cents. Every field can be edited in the UI, and there is a CSV export at `/api/receipts.csv`.

## Choosing an extractor

| `RECEIPT_EXTRACTOR` | Settings | Notes |
|---|---|---|
| `anthropic` (default) | `ANTHROPIC_API_KEY`, `RECEIPT_ANTHROPIC_MODEL` (default `claude-opus-5-5`), `RECEIPT_ANTHROPIC_TIMEOUT` (default 90s, 1 retry) | Uses structured JSON output with low effort. Server-side refusal fallback is on. |
| `ollama` | `RECEIPT_OLLAMA_MODEL` (default `qwen2.5vl:7b`), `OLLAMA_HOST` | Fully local. Needs a vision model: `ollama pull qwen2.5vl:7b` |

To add a backend, write a class with a `name` and `extract(jpeg_bytes) -> (ReceiptFields, raw_text)`, then
register it in `extractors/__init__.py`.

Other settings: `RECEIPT_DATA_DIR` (default `./data`), `HOST`, `PORT`.

## API

| Method | Path | |
|---|---|---|
| POST | `/api/receipts` | multipart `file`; returns the new row (`status: pending`) |
| GET | `/api/receipts`, `/api/receipts/{id}` | list / fetch |
| PATCH | `/api/receipts/{id}` | `vendor`, `total_cents`, `currency`, `receipt_date`, `use_scan` |
| POST | `/api/receipts/{id}/extract` | re-run extraction on the selected image |
| DELETE | `/api/receipts/{id}` | deletes the row and its images |
| GET | `/api/receipts/{id}/image/{original\|scan}` | JPEG |
| GET | `/api/receipts.csv` | export |
