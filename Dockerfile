FROM python:3.12-slim

LABEL org.opencontainers.image.title="Kojima Movie Sync Service" \
      org.opencontainers.image.description="Hideo Kojima Movie Recommendation Sync, Multi-Factor Enthusiasm Scoring & RSS Scraper Service for TiddlyWiki" \
      org.opencontainers.image.source="https://github.com/mblackman/tiddlywiki-kojima-media" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8765 \
    KOJIMA_DB_PATH=/data/kojima_reviews.db \
    LLM_PROVIDER=heuristic

WORKDIR /app

RUN groupadd -r kojima && useradd -r -g kojima -d /app kojima \
    && mkdir -p /data \
    && chown -R kojima:kojima /app /data

COPY --chown=kojima:kojima kojima_sync_service.py /app/kojima_sync_service.py

VOLUME ["/data"]

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3).read()"]

USER kojima

CMD ["python3", "/app/kojima_sync_service.py"]
