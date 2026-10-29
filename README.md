# 🎬 Kojima Movie Tracker (`$:/plugins/mblackman/kojima-media`)

TiddlyWiki 5 plugin and self-hosted Docker sync service for [`tiddlywiki-media-importer`](https://github.com/mblackman/tiddlywiki-media-importer) that imports, ranks, and tracks movies recommended or reviewed by **Hideo Kojima**.

- **Live Demo Wiki**: [https://mblackman.github.io/tiddlywiki-kojima-media/](https://mblackman.github.io/tiddlywiki-kojima-media/)
- **Docker Image (GHCR)**: `ghcr.io/mblackman/tiddlywiki-kojima-media:latest` (`linux/amd64`, `linux/arm64`)

---

## Features

- **0–100 Evidence-Aware Kojima Enthusiasm Score**:
  1. **Elaboration & Selectivity Curve ($W$: `0–30 pts`)** — Log-scaled elaboration score with selectivity floors for curated canon/annual lists and automatic detection of his infamous 1-sentence `"Saw [Movie] at the theater."` dismissals (capped at $\le 20$).
  2. **Multi-Signal Sentiment, Craft & Creative Resonance ($S$: `0–35 pts`)** — Evaluates **Superlative Praise**, **Filmmaking Craft & Auteur Admiration**, and **Personal / Creative DNA** alongside the implicit selectivity of curated list selections.
  3. **Curated Prestige Bonus ($P$: `0–25 pts`)** — Weights GQ Top 4 (`25`), Rotten Tomatoes Top 5 (`25`), MGS Core DNA (`23`), Junker HQ Top 30 (`21`), Criterion Closet (`20`), Year-End Top Lists (`19`), and X Reviews (`10–14`).
  4. **Cross-Source & Canon Recurrence ($R$: `0–10 pts`)** — Rewards films cited across multiple independent interviews, essays, and lists.
- **Standalone & `media-importer` Compatible**:
  - Syncs directly from the Docker sync service (`tm-sync-kojima`) and delegates canonical `Movie` tiddler creation to `media-importer` when a TMDB API key is configured.
  - Preserves your existing watch status (`Completed`, `Active`, `Dropped`, `Backlog`), `favorite`, and `rating` across re-syncs.
- **11 Dashboard Sort Modes**:
  - Sort by **Enthusiasm Score**, **Date Recommended (`kojima-recommended-at`)**, **Release Year**, **Review Depth (Word Count)**, **Runtime**, **Title**, or **Director**.

---

## Docker Setup (GitHub Container Registry)

The sync service is published as a multi-arch (`linux/amd64` and `linux/arm64`) image to GitHub Container Registry at **`ghcr.io/mblackman/tiddlywiki-kojima-media:latest`**.

### Option 1: Docker Compose (Recommended)

Create a `docker-compose.yml` file (or use the included [`docker-compose.example.yml`](./docker-compose.example.yml)):

```yaml
services:
  kojima-movie-sync:
    image: ghcr.io/mblackman/tiddlywiki-kojima-media:latest
    container_name: kojima-movie-sync
    ports:
      - "8765:8765"
    volumes:
      - kojima_data:/data
    environment:
      - PORT=8765
      - KOJIMA_DB_PATH=/data/kojima_reviews.db
      # Optional: TMDB API Key to auto-resolve TMDB IDs for newly scraped X posts
      - TMDB_API_KEY=${TMDB_API_KEY:-}
      # Optional: Comma-separated RSS/Atom feed URLs (e.g. Nitter / RSSHub / Letterboxd)
      - KOJIMA_RSS_URLS=${KOJIMA_RSS_URLS:-}
      - SCRAPE_INTERVAL_SECONDS=${SCRAPE_INTERVAL_SECONDS:-3600}
    restart: unless-stopped

volumes:
  kojima_data:
```

Start the container and verify health:

```bash
docker compose up -d
curl http://localhost:8765/health
```

### Option 2: `docker run` One-Liner

```bash
docker run -d \
  --name kojima-movie-sync \
  -p 8765:8765 \
  -v kojima_data:/data \
  --restart unless-stopped \
  ghcr.io/mblackman/tiddlywiki-kojima-media:latest
```

### HTTP Endpoints

| Endpoint | Method | Description |
| :--- | :---: | :--- |
| `/health` | `GET` | Health status, SQLite item count, and last RSS scrape timestamp |
| `/api/kojima-recommendations` | `GET` | Ranked JSON feed consumed by TiddlyWiki's **Sync Kojima Reviews** button (`?rescore=1` forces re-scoring) |
| `/api/scrape` | `POST` | Triggers an immediate RSS/Atom feed poll across `KOJIMA_RSS_URLS` |
| `/api/ingest-post` | `POST` | Manually ingests a raw X post JSON (`{"text": "...", "url": "...", "tmdb_id": "..."}`) |

---

## Local Plugin Development

```bash
npm install
npm test
npm run build-all
```

- **Drag-and-drop plugin bundle**: `dist/kojima-media-plugin.json`
- **Local dev server**: `npm run dev` (`http://127.0.0.1:8080`)
