FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BOOKSHELF_DB=/data/bookshelf.db \
    BOOKSHELF_WEB_DIR=/app/web

WORKDIR /app

# Dependencies first so code edits don't invalidate the layer.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY web ./web

# Fly mounts the volume at /data; create it so the image also runs without one.
RUN mkdir -p /data && useradd --create-home --uid 10001 app && chown -R app /data /app
USER app

EXPOSE 8080

# Migrations run on boot so a volume created by an older image upgrades in place.
CMD ["sh", "-c", "bookshelf init && exec uvicorn bookshelf.server:app --host 0.0.0.0 --port ${PORT:-8080}"]
