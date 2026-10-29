# 🎬 Kojima Movie Tracker (`$:/plugins/mblackman/kojima-media`)

Companion plugin and Docker sync service for [`tiddlywiki-media-importer`](https://github.com/mblackman/tiddlywiki-media-importer) that imports, ranks, and tracks movies recommended or reviewed by **Hideo Kojima**.

---

## Features

- **0–100 Multi-Factor Kojima Enthusiasm Score**:
  1. **Word-Count / Elaboration Curve ($W$: `0–30 pts`)** — Log-scaled elaboration score with capsule-blurb floors for curated canon lists and automatic detection of his infamous 1-sentence `"Saw [Movie] at the theater."` non-reviews (capped at $\le 20$).
  2. **AI Sentiment & Depth ($S$: `0–35 pts`)** — Evaluates **Praise Intensity** (`0–15`), **Personal/Autobiographical Connection** (`0–10`), and **Creative/MGS DNA Influence** (`0–10`) via Gemini (`gemini-2.5-flash`), OpenAI (`gpt-4o-mini`), local Ollama, or a deterministic lexicon fallback.
  3. **Curated Prestige Bonus ($P$: `0–25 pts`)** — Weights GQ Top 4 (`25`), Rotten Tomatoes Top 5 (`25`), MGS Core DNA (`22`), Junker HQ Top 30 (`20`), Criterion Closet (`18`), Annual Top Lists (`16`), and X Reviews (`8`).
  4. **Cross-Source Recurrence ($R$: `0–10 pts`)** — Rewards films cited across multiple independent interviews and lists.
- **Canonical `Movie` Tiddler Delegation via `media-importer`**:
  - Dispatches `tm-fetch-Movie` (`TmdbMovieConfig.fetchFn`) to fetch cast, director, runtime, genres, plot, and poster art from TMDB while merging `Movie Kojima` tags and `kojima-*` fields and preserving existing watch status (`Completed`, `Active`, `Dropped`), `favorite`, and `rating`.
  - Includes a bundled 25-movie curated seed (`tm-sync-kojima-seed`) that works immediately offline or without a TMDB key.
- **`MASTER.md` 3-Tier Design System Compliance**:
  - Built on the Productivity Suite `--suite-*` CSS token contract (`color-mix(in oklab, ...)`, `color-scheme: light dark`, `--suite-border-control`, `font-variant-numeric: tabular-nums`, `@container kj-dashboard` responsive layout, and Forced-Colors resilience).

---

## Quick Start

### 1. Start the Docker Sync & Scraper Service
```bash
docker compose up -d
curl http://localhost:8765/health
```

### 2. Build & Test the TiddlyWiki Plugin
```bash
npm test
npm run build-all
```
- **Drag-and-drop plugin bundle**: `dist/kojima-media-plugin.json`
- **Live dev server**: `npm run dev` (`http://127.0.0.1:8080`)
