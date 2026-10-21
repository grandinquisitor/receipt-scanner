FROM python:3.13-slim

# uv installs exactly what uv.lock pins
COPY --from=ghcr.io/astral-sh/uv:0.12.2 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

WORKDIR /app
# Dependencies first, so code changes don't reinstall them
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH=/app/.venv/bin:$PATH \
    RECEIPT_DATA_DIR=/app/data \
    HOST=0.0.0.0 \
    PORT=8088
EXPOSE 8088
CMD ["receipt-scanner"]
