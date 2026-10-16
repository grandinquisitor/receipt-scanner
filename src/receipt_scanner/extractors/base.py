"""Common types for receipt field extractors."""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Protocol

from pydantic import BaseModel, field_validator

PROMPT = """\
This is a photo of a shopping receipt (greyscale, possibly cropped and deskewed).
Extract:
- vendor: the store / business name as printed (null if not visible)
- total: the final amount paid, including tax and tip, as a plain decimal string like "23.45" (null if not visible)
- currency: ISO 4217 code such as "USD" or "EUR", inferred from symbols or location (null if unclear)
- date: the transaction date as YYYY-MM-DD (null if not visible)
- description: a short, general label for what was bought, a few words at most, e.g. "groceries",
  "shipping supplies", "various hardware", "restaurant meal", "fuel". Don't itemize; summarize at whatever
  level the receipt supports, and stay vague rather than guess at specifics. Use "unknown" if you can't tell.
- text_top: which edge of the image the top of the printed text faces: "top" if the text reads normally,
  "right" / "left" if the receipt lies sideways, "bottom" if it is upside down (null if unsure)

Only report what is actually printed on the receipt. Use null rather than guessing.\
"""

TEXT_TOP = ("top", "right", "bottom", "left")
_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}

JSON_SCHEMA = {
    "type": "object",
    "properties": {
        **{k: _NULLABLE_STRING for k in ("vendor", "total", "currency", "date", "description")},
        "text_top": {"anyOf": [{"type": "string", "enum": list(TEXT_TOP)}, {"type": "null"}]},
    },
    "required": ["vendor", "total", "currency", "date", "description", "text_top"],
    "additionalProperties": False,
}


class ReceiptFields(BaseModel):
    vendor: str | None = None
    total: str | None = None
    currency: str | None = None
    date: str | None = None
    description: str | None = None
    text_top: str | None = None

    @field_validator("text_top")
    @classmethod
    def _known_edge(cls, v):
        v = v.strip().lower() if isinstance(v, str) else None
        return v if v in TEXT_TOP else None  # a bad orientation answer shouldn't sink the other fields

    @field_validator("date")
    @classmethod
    def _iso_date(cls, v):
        if v is None:
            return None
        try:
            return date.fromisoformat(v.strip()).isoformat()
        except ValueError:
            return None

    def total_cents(self) -> int | None:
        if self.total is None:
            return None
        cleaned = re.sub(r"[^\d.\-]", "", self.total.replace(",", "."))
        # "1.234.56" -> keep only the last dot as the decimal separator
        if cleaned.count(".") > 1:
            head, _, tail = cleaned.rpartition(".")
            cleaned = head.replace(".", "") + "." + tail
        try:
            return int((Decimal(cleaned) * 100).quantize(Decimal(1)))
        except InvalidOperation:
            return None


class ExtractionError(Exception):
    pass


class Extractor(Protocol):
    name: str  # e.g. "anthropic:claude-opus-5-5"

    def extract(self, jpeg: bytes) -> tuple[ReceiptFields, str]:
        """Return the parsed fields and the raw model output."""
        ...
