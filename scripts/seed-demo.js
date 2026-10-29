#!/usr/bin/env node
/**
 * Seeds `demo/tiddlers/KojimaMovies.json` from `plugins/mblackman/kojima-media/data/seed-recommendations.json`
 * and copies the latest built `media-importer.json` plugin bundle if available so the demo wiki
 * has both `media-importer` and `kojima-media` pre-populated for immediate inspection.
 */

const fs = require("fs");
const path = require("path");

const ROOT_DIR = path.resolve(__dirname, "..");
const SEED_JSON_PATH = path.join(ROOT_DIR, "demo", "seed-recommendations.json");
const DEMO_TIDDLERS_DIR = path.join(ROOT_DIR, "demo", "tiddlers");
const MEDIA_IMPORTER_DIST = path.resolve(
  ROOT_DIR,
  "..",
  "tiddlywiki-media-importer",
  "dist",
  "media-importer.json"
);
const MEDIA_IMPORTER_SYSTEM_DIR = path.resolve(
  ROOT_DIR,
  "..",
  "tiddlywiki-media-importer",
  "wiki",
  "tiddlers",
  "system"
);
const SAMPLE_MEDIA_FILES = [
  "Books.json",
  "Manga.json",
  "Logs.json",
  "EnableLibraryToolbar.tid",
  "Media Picker.tid",
  "Dated Media Picker.tid",
  "Media Summary.tid",
  "Date Media Summary.tid",
  "$__media-log_2026-01-01.tid",
];

function formatBracketList(items) {
  if (!items) return "";
  const arr = Array.isArray(items)
    ? items
    : String(items)
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
  return arr.map((entry) => `[[${entry}]]`).join(" ");
}

const ALLOWED_DEMO_FILES = new Set([
  "$__DefaultTiddlers.tid",
  "$__SiteTitle.tid",
  "$__SiteSubtitle.tid",
  "$__plugins_mblackman_media-importer.json",
  "KojimaMovies.json",
  ...SAMPLE_MEDIA_FILES,
]);

function cleanRuntimeTiddlers() {
  if (!fs.existsSync(DEMO_TIDDLERS_DIR)) return;
  for (const file of fs.readdirSync(DEMO_TIDDLERS_DIR)) {
    if (!ALLOWED_DEMO_FILES.has(file)) {
      fs.rmSync(path.join(DEMO_TIDDLERS_DIR, file), { force: true });
    }
  }
}

function main() {
  fs.mkdirSync(DEMO_TIDDLERS_DIR, { recursive: true });
  cleanRuntimeTiddlers();

  // 1. Copy media-importer plugin bundle and sample library items if present in parent/sibling repo
  if (fs.existsSync(MEDIA_IMPORTER_DIST)) {
    fs.copyFileSync(
      MEDIA_IMPORTER_DIST,
      path.join(DEMO_TIDDLERS_DIR, "$__plugins_mblackman_media-importer.json")
    );
  }
  if (fs.existsSync(MEDIA_IMPORTER_SYSTEM_DIR)) {
    for (const sampleFile of SAMPLE_MEDIA_FILES) {
      const srcPath = path.join(MEDIA_IMPORTER_SYSTEM_DIR, sampleFile);
      if (fs.existsSync(srcPath)) {
        fs.copyFileSync(srcPath, path.join(DEMO_TIDDLERS_DIR, sampleFile));
      }
    }
  }

  // 2. Convert seed recommendations into canonical Movie + Kojima tiddlers
  const rawItems = JSON.parse(fs.readFileSync(SEED_JSON_PATH, "utf8"));
  const seenTitles = new Set([
    "2001: A Space Odyssey",
    "High and Low",
    "Taxi Driver",
    "Mad Max 2",
    "Blade Runner",
  ]);

  const tiddlers = rawItems.map((item) => {
    const year = String(item.year || "0000");
    const finalTitle = `${item.title} (${year})`;
    const status = seenTitles.has(item.title) ? "Completed" : "Backlog";
    return {
      title: finalTitle,
      englishTitle: item.title,
      year: year,
      dataSource: "TMDB",
      url: item.tmdb_id ? `https://www.themoviedb.org/movie/${item.tmdb_id}` : "",
      id: String(item.tmdb_id || ""),
      director: formatBracketList(item.director),
      genres: formatBracketList(item.genres),
      runtime: String(item.runtime || ""),
      onlineRating: item.kojima_is_dismissal === "yes" ? "2.4" : "4.3",
      image: item.poster || "",
      "cover-storage": item.poster ? "external-uri" : "",
      status: status,
      tags: "[[$:/tags/media-importer/Media]] Movie Kojima",
      "media-type": "Movie",
      plot: item.plot || "",
      description: item.plot || "",
      "kojima-score": String(item.kojima_score),
      "kojima-tier": item.kojima_tier || "",
      "kojima-review": item.kojima_review || "",
      "kojima-word-count": String(item.kojima_word_count || 0),
      "kojima-is-dismissal": item.kojima_is_dismissal || "no",
      "kojima-sentiment-summary": item.kojima_sentiment_summary || "",
      "kojima-score-breakdown": item.kojima_score_breakdown || "",
      "kojima-primary-url": item.kojima_primary_url || "",
      "kojima-primary-source": item.kojima_primary_source || "",
      "kojima-recommended-at": item.kojima_recommended_at || "",
      "kojima-sources-json": item.kojima_sources_json || "[]",
    };
  });

  fs.writeFileSync(
    path.join(DEMO_TIDDLERS_DIR, "KojimaMovies.json"),
    JSON.stringify(tiddlers, null, 2),
    "utf8"
  );
  console.log(`✅ Seeded ${tiddlers.length} Kojima Movie tiddlers into demo/tiddlers/KojimaMovies.json`);
}

if (require.main === module) {
  main();
}

module.exports = { main };
