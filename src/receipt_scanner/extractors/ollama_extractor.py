"""Extractor backed by a local vision model served by Ollama (https://ollama.com)."""

import base64
import os

import httpx

from .base import JSON_SCHEMA, PROMPT, ExtractionError, ReceiptFields


class OllamaExtractor:
    def __init__(self, model: str | None = None, host: str | None = None):
        self.model = model or os.environ.get("RECEIPT_OLLAMA_MODEL", "qwen2.5vl:7b")
        self.host = (host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.name = f"ollama:{self.model}"

    def extract(self, jpeg: bytes) -> tuple[ReceiptFields, str]:
        payload = {
            "model": self.model,
            "stream": False,
            "format": JSON_SCHEMA,  # Ollama structured outputs
            "options": {"temperature": 0},
            "messages": [{
                "role": "user",
                "content": PROMPT,
                "images": [base64.standard_b64encode(jpeg).decode("ascii")],
            }],
        }
        try:
            r = httpx.post(f"{self.host}/api/chat", json=payload, timeout=300)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise ExtractionError(f"Ollama error {e.response.status_code}: {e.response.text[:200]}") from e
        except httpx.HTTPError as e:
            raise ExtractionError(f"Could not reach Ollama at {self.host}: {e}") from e

        raw = r.json().get("message", {}).get("content", "")
        try:
            return ReceiptFields.model_validate_json(raw), raw
        except ValueError as e:
            raise ExtractionError(f"Could not parse model output: {raw[:200]}") from e
