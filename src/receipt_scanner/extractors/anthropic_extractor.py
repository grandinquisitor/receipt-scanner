"""Extractor backed by the Claude API."""

import base64
import os

import anthropic

from .base import JSON_SCHEMA, PROMPT, ExtractionError, ReceiptFields


class AnthropicExtractor:
    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("RECEIPT_ANTHROPIC_MODEL", "claude-opus-5-5")
        self.name = f"anthropic:{self.model}"
        # Fail in minutes, not the SDK default (10 min x 3 attempts). Generous enough for the
        # server-side refusal fallback, which runs inside the same call.
        self.client = anthropic.Anthropic(
            timeout=float(os.environ.get("RECEIPT_ANTHROPIC_TIMEOUT", "90")), max_retries=1,
        )

    def extract(self, jpeg: bytes) -> tuple[ReceiptFields, str]:
        image = base64.standard_b64encode(jpeg).decode("ascii")
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                # If a safety classifier declines, the API retries on a fallback model in the same call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": JSON_SCHEMA},
                },
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image}},
                        {"type": "text", "text": PROMPT},
                    ],
                }],
            )
        except TypeError as e:
            if "authentication" not in str(e):
                raise
            raise ExtractionError("No Anthropic credentials: set ANTHROPIC_API_KEY") from e
        except anthropic.AuthenticationError as e:
            raise ExtractionError(f"Anthropic auth failed (set ANTHROPIC_API_KEY): {e.message}") from e
        except anthropic.APITimeoutError as e:
            raise ExtractionError("Anthropic API timed out; try Re-extract") from e
        except anthropic.RateLimitError as e:
            raise ExtractionError("Anthropic rate limit hit; try again shortly") from e
        except anthropic.APIStatusError as e:
            raise ExtractionError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ExtractionError(f"Could not reach Anthropic API: {e}") from e

        if response.stop_reason == "refusal":
            raise ExtractionError("Model declined to process this image")
        if response.stop_reason == "max_tokens":
            raise ExtractionError("Model output was truncated")

        raw = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return ReceiptFields.model_validate_json(raw), raw
        except ValueError as e:
            raise ExtractionError(f"Could not parse model output: {raw[:200]}") from e
