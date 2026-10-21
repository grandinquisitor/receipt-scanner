FROM python:3.13-slim

# uv installs exactly what uv.lock pins. Both uv itself and its download cache are mounted only while
# building, so neither ends up in the image (the cache alone would double its size).
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

WORKDIR /app
# Dependencies first, so code changes don't reinstall them
COPY pyproject.toml uv.lock ./
RUN --mount=from=ghcr.io/astral-sh/uv:0.12.2,source=/uv,target=/bin/uv --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=from=ghcr.io/astral-sh/uv:0.12.2,source=/uv,target=/bin/uv --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

ENV PATH=/app/.venv/bin:$PATH \
    RECEIPT_DATA_DIR=/app/data \
    HOST=0.0.0.0 \
    PORT=8088
EXPOSE 8088
CMD ["receipt-scanner"]
