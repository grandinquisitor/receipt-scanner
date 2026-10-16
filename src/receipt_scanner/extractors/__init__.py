"""Pluggable receipt field extractors.

Pick one with RECEIPT_EXTRACTOR (default "anthropic"). To add a backend, write a class with a
`name` attribute and an `extract(jpeg_bytes) -> (ReceiptFields, raw_text)` method and register it below.
"""

import os
from collections.abc import Callable

from .base import ExtractionError, Extractor, ReceiptFields

__all__ = ["ExtractionError", "Extractor", "ReceiptFields", "get_extractor", "REGISTRY"]


def _anthropic() -> Extractor:
    from .anthropic_extractor import AnthropicExtractor
    return AnthropicExtractor()


def _ollama() -> Extractor:
    from .ollama_extractor import OllamaExtractor
    return OllamaExtractor()


REGISTRY: dict[str, Callable[[], Extractor]] = {
    "anthropic": _anthropic,
    "ollama": _ollama,
}


def get_extractor(name: str | None = None) -> Extractor:
    name = (name or os.environ.get("RECEIPT_EXTRACTOR", "anthropic")).lower()
    try:
        return REGISTRY[name]()
    except KeyError:
        raise ValueError(f"Unknown extractor {name!r}; choose from {sorted(REGISTRY)}") from None
