#!/usr/bin/env node
/**
 * Seeds `demo/tiddlers/KojimaMovies.json` from `demo/seed-recommendations.json`
 * and bundles the official GitHub Release of `media-importer.json`
 * (`https://github.com/mblackman/tiddlywiki-media-importer/releases/latest/download/media-importer.json`)
 * without importing demo Books, Manga, or Logs.
 */

const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const ROOT_DIR = path.resolve(__dirname, "..");
const SEED_JSON_PATH = path.join(ROOT_DIR, "demo", "seed-recommendations.json");
const DEMO_TIDDLERS_DIR = path.join(ROOT_DIR, "demo", "tiddlers");
const MEDIA_IMPORTER_BUNDLE_PATH = path.join(
  DEMO_TIDDLERS_DIR,
  "$__plugins_mblackman_media-importer.json"
);
const MEDIA_IMPORTER_RELEASE_URL =
  "https://github.com/mblackman/tiddlywiki-media-importer/releases/latest/download/media-importer.json";

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
  "$__DemoStylesheet.tid",
  "$__SiteTitle.tid",
  "$__SiteSubtitle.tid",
  "$__plugins_mblackman_media-importer.json",
  "KojimaMovies.json",
]);

function cleanRuntimeTiddlers() {
  if (!fs.existsSync(DEMO_TIDDLERS_DIR)) return;
  for (const file of fs.readdirSync(DEMO_TIDDLERS_DIR)) {
    if (!ALLOWED_DEMO_FILES.has(file)) {
      fs.rmSync(path.join(DEMO_TIDDLERS_DIR, file), { force: true });
    }
  }
}

function syncOfficialMediaImporterRelease({ forceDownload = false } = {}) {
  if (!forceDownload && fs.existsSync(MEDIA_IMPORTER_BUNDLE_PATH)) {
    return;
  }
  try {
    const raw = execFileSync(
      "curl",
      ["-fsSL", "--max-time", "15", MEDIA_IMPORTER_RELEASE_URL],
      { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }
    );
    const parsed = JSON.parse(raw);
    const pluginObj = Array.isArray(parsed) ? parsed[0] : parsed;
    if (pluginObj && pluginObj.title === "$:/plugins/mblackman/media-importer") {
      fs.writeFileSync(
        MEDIA_IMPORTER_BUNDLE_PATH,
        JSON.stringify(pluginObj, null, 4) + "\n",
        "utf8"
      );
      console.log(
        `📦 Bundled official release of $:/plugins/mblackman/media-importer (v${pluginObj.version || "latest"})`
      );
    }
  } catch (err) {
    if (!fs.existsSync(MEDIA_IMPORTER_BUNDLE_PATH)) {
      throw new Error(
        `Failed to download official media-importer release from ${MEDIA_IMPORTER_RELEASE_URL}: ${err.message}`
      );
    }
  }
}

function main(options = {}) {
  fs.mkdirSync(DEMO_TIDDLERS_DIR, { recursive: true });
  cleanRuntimeTiddlers();

  // 1. Ensure official release of media-importer plugin is bundled (no demo media)
  syncOfficialMediaImporterRelease(options);

  // 2. Convert seed recommendations into canonical Movie + Kojima tiddlers
  const ledger = JSON.parse(fs.readFileSync(SEED_JSON_PATH, "utf8"));
  const rawItems = Array.isArray(ledger) ? ledger : ledger.items || [];
  const lastUpdated = Array.isArray(ledger) ? null : ledger.last_updated || null;

  const tiddlers = rawItems.map((item) => {
    const year = String(item.year || "0000");
    const finalTitle = `${item.title} (${year})`;
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
      status: "Backlog",
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

  // 3. Emit static API JSON feeds into build/ for GitHub Pages hosting
  const buildApiDir = path.join(ROOT_DIR, "build", "api");
  fs.mkdirSync(buildApiDir, { recursive: true });
  const apiPayload = JSON.stringify(
    {
      generated_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
      last_updated: lastUpdated,
      scoring_version: Array.isArray(ledger) ? "v2.1" : ledger.scoring_version || "v2.1",
      count: rawItems.length,
      items: rawItems.map(({ content_hash, ...item }) => item),
    },
    null,
    2
  ) + "\n";
  fs.writeFileSync(path.join(buildApiDir, "kojima-recommendations.json"), apiPayload, "utf8");
  fs.writeFileSync(path.join(buildApiDir, "kojima-recommendations"), apiPayload, "utf8");
  fs.writeFileSync(path.join(ROOT_DIR, "build", "kojima-recommendations.json"), apiPayload, "utf8");

  console.log(`✅ Seeded ${tiddlers.length} Kojima Movie tiddlers into demo/tiddlers/KojimaMovies.json & build/api/kojima-recommendations.json`);
}

if (require.main === module) {
  main({ forceDownload: true });
}

module.exports = { main };
