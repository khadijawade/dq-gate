FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Locked runtime dependencies first, so code changes do not reinstall them.
COPY requirements.lock .
RUN pip install -r requirements.lock

COPY pyproject.toml README.md alembic.ini ./
COPY app ./app
COPY migrations ./migrations
RUN pip install --no-deps . && useradd --system --uid 10001 dq

USER dq
EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=10 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]

# Single node: apply migrations, then serve. Alembic takes no action when already at head.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.api:app --host 0.0.0.0 --port 8000"]
