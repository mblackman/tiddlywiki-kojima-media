# 🎬 Kojima Movie Tracker (`$:/plugins/mblackman/kojima-media`)

TiddlyWiki 5 plugin for [`tiddlywiki-media-importer`](https://github.com/mblackman/tiddlywiki-media-importer) that imports, ranks, and tracks movies recommended or reviewed by **Hideo Kojima**.

- **Live Demo**: [https://mblackman.github.io/tiddlywiki-kojima-media/](https://mblackman.github.io/tiddlywiki-kojima-media/)
- **JSON Feed**: [`https://mblackman.github.io/tiddlywiki-kojima-media/api/kojima-recommendations.json`](https://mblackman.github.io/tiddlywiki-kojima-media/api/kojima-recommendations.json)

---

## Features

- **Serverless Sync**: Connects out of the box to the hosted GitHub Pages JSON feed (updated daily via GitHub Actions). Click **🔄 Sync Kojima Feed** in the dashboard to pull the latest recommendations with zero server setup.
- **5-Level Enthusiasm Rating**: Ranks every film from **★★★★★ Canon** to **★☆☆☆☆ Dismissed** based on Kojima's curated lists and X (`@HIDEO_KOJIMA_EN`) posts.
- **`media-importer` Integration**: Creates canonical `Movie` tiddlers tagged with `Movie` and `Kojima` while preserving your watch status (`Completed`, `Active`, `Dropped`, `Backlog`), `favorite`, and `rating` across syncs.
- **Filter & Sort Dashboard**: Filter by watch status or enthusiasm level, search titles/directors/reviews, and sort by enthusiasm, recommendation date, release year, review length, runtime, title, or director.

---

## Enthusiasm Ratings

Each movie receives one of five levels based on **where Kojima recommended it** and **what he wrote about it** (strongest evidence wins; short blurbs never downgrade a curated list pick):

| Level | Label | Criteria | Example |
| :--- | :--- | :--- | :--- |
| **★★★★★ 5** | **Canon** | Appears on an all-time list: GQ Top 4, Rotten Tomatoes Top 5, core MGS influences, Junker HQ Top 30, or Criterion Closet picks | *2001: A Space Odyssey*, *Taxi Driver* |
| **★★★★☆ 4** | **Loved** | Appears on a year-end/mid-year top list or Junker HQ runners-up, **or** his X post is a rave (*masterpiece*, *history of cinema*, *made me cry*) | *Hamnet*, *Crime 101*, *Deadpool* |
| **★★★☆☆ 3** | **Liked** | Clearly positive X review, a rewatch, or purchasing a physical edition (4K UHD / Blu-ray / box set) | *Interstellar*, *Split Second* |
| **★★☆☆☆ 2** | **Noted** | Neutral, purely descriptive/analytical commentary, or a viewing announcement without a verdict | *Supergirl* |
| **★☆☆☆☆ 1** | **Dismissed** | His pointed one-sentence non-review (≤15 words, e.g. *"Saw X at the theater."*) and on no curated list | *Madame Web* |

Hover over any star badge in the dashboard or movie tiddler to view the exact rating breakdown. Standalone X posts are classified by keywords by default, or via LLM when `LLM_PROVIDER` (`gemini`, `openai`, or `ollama`) is configured.

---

## Data Pipeline

| File | Purpose |
| :--- | :--- |
| [`catalog/kojima-canon.jsonl`](./catalog/kojima-canon.jsonl) | Curated JSONL source catalog (all-time lists, MGS influences, Criterion picks, annual top lists, and notable X reviews). |
| [`demo/seed-recommendations.json`](./demo/seed-recommendations.json) | Git-persisted ledger of scored movies, posters, metadata, and `content_hash`. Updated incrementally by `npm run generate-feed`. |

---

## Local Development

```bash
npm install

# Update recommendations feed & seed demo tiddlers
npm run seed-demo

# Start live local TiddlyWiki server at http://localhost:8080
npm run dev

# Run Python & TiddlyWiki test suites
npm test

# Build plugin (dist/kojima-media-plugin.json), demo wiki (build/index.html), and JSON feed
npm run build-all
```
