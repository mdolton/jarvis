# === Build stage ===
FROM python:3.12-slim AS builder

# Install uv for fast dependency resolution.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first (layer cache).
COPY pyproject.toml uv.lock ./

# Install dependencies into a venv.
RUN uv sync --frozen --no-dev --no-install-project

# Copy the application code.
COPY jarvis/ jarvis/
COPY alembic/ alembic/
COPY alembic.ini ./

# Install the project itself.
RUN uv sync --frozen --no-dev

# === Runtime stage ===
FROM python:3.12-slim

# Non-root user for security.
RUN groupadd -r jarvis && useradd -r -g jarvis -d /app jarvis

WORKDIR /app

# Copy the venv + app from build stage.
COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:$PATH"

# Copy entrypoint.
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Default config and data directories (mounted as volumes in compose).
RUN mkdir -p /app/config /app/data && chown -R jarvis:jarvis /app

USER jarvis

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
