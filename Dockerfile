# syntax=docker/dockerfile:1.7
# Multi-stage: the builder holds compilers and build headers that must not
# reach the runtime image. Playwright's base image already carries the browser
# and its (substantial) system library set, which is why it is used rather than
# installing Chromium into a slim Python image by hand.

# --- Builder --------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY app ./app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# All three provider SDKs, so switching LLM_PROVIDER stays a config change
# rather than an image rebuild. They are small pure-Python clients.
RUN pip install --upgrade pip && pip install ".[gemini,anthropic,openai]"

# --- Runtime --------------------------------------------------------------
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY app ./app
COPY alembic ./alembic
COPY scripts ./scripts
COPY alembic.ini pyproject.toml ./

# Chromium refuses to run as root without --no-sandbox, and running a browser
# that renders untrusted third-party pages as root is a poor idea regardless.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=4).status < 500 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
