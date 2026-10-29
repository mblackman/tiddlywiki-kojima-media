# 🎬 Kojima Movie Tracker (`$:/plugins/mblackman/kojima-media`)

TiddlyWiki 5 plugin for [`tiddlywiki-media-importer`](https://github.com/mblackman/tiddlywiki-media-importer) that imports, ranks, and tracks movies recommended or reviewed by **Hideo Kojima**.

- **Live Demo Wiki**: [https://mblackman.github.io/tiddlywiki-kojima-media/](https://mblackman.github.io/tiddlywiki-kojima-media/)
- **Hosted Recommendations JSON Feed (CORS-enabled)**: [`https://mblackman.github.io/tiddlywiki-kojima-media/api/kojima-recommendations.json`](https://mblackman.github.io/tiddlywiki-kojima-media/api/kojima-recommendations.json)
- **Optional Self-Hosted Docker Image (GHCR)**: `ghcr.io/mblackman/tiddlywiki-kojima-media:latest` (`linux/amd64`, `linux/arm64`)

---

## Features

- **Zero-Server Setup via GitHub Pages**:
  - A scheduled GitHub Actions workflow (`.github/workflows/ci.yml`) runs daily to score Kojima's movie recommendations and publish both the **interactive demo TiddlyWiki** (`https://mblackman.github.io/tiddlywiki-kojima-media/`) and the **public JSON feed** (`https://mblackman.github.io/tiddlywiki-kojima-media/api/kojima-recommendations.json`).
  - Installed copies of the plugin default to that GitHub Pages feed URL — clicking **🔄 Sync Kojima Feed** downloads the latest recommendations directly into your wiki with **no Docker container required**.
  - You can also point the feed URL in `$:/plugins/mblackman/kojima-media/configs/kojima-sync-url` to your own fork's GitHub Pages URL or a local server.
- **0–100 Evidence-Aware Kojima Enthusiasm Score**:
  1. **Elaboration & Selectivity Curve ($W$: `0–30 pts`)** — Log-scaled elaboration score with selectivity floors for curated canon/annual lists and automatic detection of his infamous 1-sentence `"Saw [Movie] at the theater."` dismissals (capped at $\le 20$).
  2. **Multi-Signal Sentiment, Craft & Creative Resonance ($S$: `0–35 pts`)** — Evaluates **Superlative Praise**, **Filmmaking Craft & Auteur Admiration**, and **Personal / Creative DNA** alongside the implicit selectivity of curated list selections.
  3. **Curated Prestige Bonus ($P$: `0–25 pts`)** — Weights GQ Top 4 (`25`), Rotten Tomatoes Top 5 (`25`), MGS Core DNA (`23`), Junker HQ Top 30 (`21`), Criterion Closet (`20`), Year-End Top Lists (`19`), and X Reviews (`10–14`).
  4. **Cross-Source & Canon Recurrence ($R$: `0–10 pts`)** — Rewards films cited across multiple independent interviews, essays, and lists.
- **Standalone & `media-importer` Compatible**:
  - Syncs directly from the configured JSON feed (`tm-sync-kojima`) and delegates canonical `Movie` tiddler creation to `media-importer` when a TMDB API key is configured.
  - Preserves your existing watch status (`Completed`, `Active`, `Dropped`, `Backlog`), `favorite`, and `rating` across re-syncs.
- **11 Dashboard Sort Modes**:
  - Sort by **Enthusiasm Score**, **Date Recommended (`kojima-recommended-at`)**, **Release Year**, **Review Depth (Word Count)**, **Runtime**, **Title**, or **Director**.

---

## Recommendations Catalog & Persistent Ledger

| File | Purpose | Shipped in plugin? |
| :--- | :--- | :---: |
| [`catalog/kojima-canon.jsonl`](./catalog/kojima-canon.jsonl) | Hand-curated source list (one JSON object per line): Junker HQ Top 30 + runners-up, 15 films that inspired Metal Gear, Criterion Closet picks, year-end lists (2015–2024), and one-sentence reviews. Add a line to add a movie. | No |
| [`demo/seed-recommendations.json`](./demo/seed-recommendations.json) | Git-persisted ledger of every scored movie (score, poster, metadata, `content_hash`) plus `last_updated`. Committed by the scheduled workflow only when something changes. | No |

`npm run generate-feed` (`python3 kojima_sync_service.py --sync-static`) is incremental:

- Movies already in the ledger are restored as-is, with no re-scoring and no network calls. Only new or edited catalog entries get scored (a `content_hash` mismatch triggers it; bumping `SCORING_ENGINE_VERSION` re-scores everything).
- Missing posters and plots come from Wikipedia (no API key needed), or from TMDB when `TMDB_API_KEY` is set.
- RSS posts dated before the ledger's `last_updated` are skipped.
- `last_updated` only changes when the item list changes, so unchanged runs produce no commits.
- Flags: `--force-rescore` (re-score every movie), `--no-enrich` (skip poster lookups).

---

## Local Demo & Plugin Development

You can generate the recommendations feed, populate the demo site (`demo/tiddlers/KojimaMovies.json` + `build/api/kojima-recommendations.json`), and test everything locally without Docker:

```bash
npm install

# 1. Run Python scoring engine & populate demo/tiddlers/KojimaMovies.json + build/api/kojima-recommendations.json
npm run seed-demo

# 2. Start the live local TiddlyWiki demo server on http://localhost:8080 (auto-seeds on startup)
npm run dev

# 3. Run Python + TiddlyWiki test suites
npm test

# 4. Build plugin bundle (dist/kojima-media-plugin.json), demo HTML (build/index.html), and static feed
npm run build-all
```

---

## Optional Self-Hosted Docker Setup (GitHub Container Registry)

If you prefer to run your own local sync service (for example, with custom RSS feeds or local Ollama/OpenAI/Gemini LLM scoring), a multi-arch (`linux/amd64` and `linux/arm64`) image is published to **`ghcr.io/mblackman/tiddlywiki-kojima-media:latest`**. In TiddlyWiki's **Control Panel → Settings → Kojima Movie Tracker**, click **🐳 Use Local Server (`localhost:8765`)** to point the plugin to your container.

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
