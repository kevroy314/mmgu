# Hallkeeper: one image runs the web app, the Discord bot and the scheduler.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MMGU_DATA_DIR=/data \
    MMGU_HOST=0.0.0.0 \
    MMGU_PORT=8420

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --upgrade pip && pip install ".[postgres,anthropic,mcp]"

RUN useradd --create-home --uid 1000 hall && mkdir -p /data && chown hall:hall /data
USER hall
VOLUME ["/data"]
EXPOSE 8420
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8420/healthz', timeout=4).status == 200 else 1)"
CMD ["mmgu", "serve"]
