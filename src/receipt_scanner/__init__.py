import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# Load settings (ANTHROPIC_API_KEY etc.) from the project's .env; real environment variables win.
load_dotenv(Path.cwd() / ".env")
load_dotenv(Path(__file__).resolve().parents[2] / ".env")  # project root, if run from elsewhere


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    uvicorn.run(
        "receipt_scanner.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),  # reachable from a phone on the same network
        port=int(os.environ.get("PORT", "8000")),
    )
