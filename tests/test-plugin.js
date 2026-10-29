#!/usr/bin/env node
/**
 * Headless TiddlyWiki verification test suite for `$:/plugins/mblackman/kojima-media`.
 * Verifies:
 * 1. Plugin loading, stylesheet token contract (`MASTER.md`), and zero unparsed macros
 * 2. `tm-sync-kojima-seed` startup action & watch status (`Completed`) preservation
 * 3. `Kojima Movie Recommendations` dashboard rendering, summary counts, and filter controls
 * 4. Individual Movie tiddler ViewTemplate Kojima panel rendering
 */

const assert = require("assert");
const path = require("path");
const { TiddlyWiki } = require("tiddlywiki");
const { main: seedDemo } = require("../scripts/seed-demo");

const ROOT_DIR = path.resolve(__dirname, "..");

function bootWiki() {
  seedDemo();
  process.env.TIDDLYWIKI_PLUGIN_PATH = path.join(ROOT_DIR, "plugins");
  const $tw = TiddlyWiki();
  $tw.boot.argv = [path.join(ROOT_DIR, "demo")];
  $tw.boot.boot();
  if ($tw.syncadaptor) {
    $tw.syncadaptor.saveTiddler = (_tiddler, cb) => cb && cb(null);
    $tw.syncadaptor.deleteTiddler = (_title, cb) => cb && cb(null);
  }
  if ($tw.syncer) {
    $tw.syncer.isDirty = () => false;
    $tw.syncer.syncFromServer = () => {};
    $tw.syncer.enqueueLoadTiddler = () => {};
  }
  return $tw;
}

function runTests() {
  const $tw = bootWiki();

  // 1. Verify plugin & stylesheet tiddlers are registered, and no seed data is bundled in the plugin
  const pluginTiddlers = $tw.wiki.filterTiddlers(
    "[all[tiddlers+shadows]prefix[$:/plugins/mblackman/kojima-media]]"
  );
  assert(
    pluginTiddlers.length >= 6,
    `Expected at least 6 plugin shadow tiddlers, found ${pluginTiddlers.length}`
  );
  assert(
    !pluginTiddlers.includes("$:/plugins/mblackman/kojima-media/data/seed-recommendations.json"),
    "Plugin should not bundle seed-recommendations.json"
  );

  // Verify official media-importer plugin is bundled without demo Books, Manga, or Logs
  const mediaImporterPlugin = $tw.wiki.getTiddler("$:/plugins/mblackman/media-importer");
  assert(mediaImporterPlugin, "Official $:/plugins/mblackman/media-importer plugin should be loaded");
  const demoBooksOrManga = $tw.wiki.filterTiddlers(
    "[tag[$:/tags/media-importer/Media]!field:media-type[Movie]] [tag[$:/tags/media-importer/Log]]"
  );
  assert.strictEqual(
    demoBooksOrManga.length,
    0,
    `Demo wiki should not import demo Books, Manga, or Logs from media-importer, found: ${demoBooksOrManga.join(", ")}`
  );

  // Verify clean DefaultTiddlers on the demo page
  const defaultTiddlers = $tw.wiki.getTiddlerText("$:/DefaultTiddlers", "").trim();
  assert.strictEqual(
    defaultTiddlers,
    "[[Kojima Movie Recommendations]] [[$:/plugins/mblackman/kojima-media]]",
    "DefaultTiddlers should show Kojima Movie Recommendations and the plugin tiddler below it"
  );

  // Verify default feed URL points to the hosted GitHub Pages JSON feed
  const defaultSyncUrl = $tw.wiki
    .getTiddlerText("$:/plugins/mblackman/kojima-media/configs/kojima-sync-url", "")
    .trim();
  assert.strictEqual(
    defaultSyncUrl,
    "https://mblackman.github.io/tiddlywiki-kojima-media/api/kojima-recommendations.json",
    "Default sync URL should point to the GitHub Pages JSON feed"
  );

  // Verify settings panel renders concisely without any TMDB API Key field
  const configHtml = $tw.wiki.renderTiddler(
    "text/html",
    "$:/plugins/mblackman/kojima-media/config"
  );
  assert(configHtml.includes("Recommendations Feed URL"), "Settings panel should render Feed URL label");
  assert(configHtml.includes("Official GitHub Pages Feed"), "Settings panel should render GitHub Pages preset chip");
  assert(!configHtml.includes("TMDB API Key"), "Settings panel should not ask for a TMDB API Key");

  // 2. Verify MASTER.md CSS compliance on Kojima stylesheets
  const tokensCss = $tw.wiki.renderTiddler(
    "text/plain",
    "$:/plugins/mblackman/kojima-media/styles/suite-tokens"
  );
  const kojimaCss = $tw.wiki.renderTiddler(
    "text/plain",
    "$:/plugins/mblackman/kojima-media/styles/kojima"
  );
  assert(tokensCss.includes("color-mix(in oklab,"), "suite-tokens must use oklab color-mix");
  assert(tokensCss.includes("--suite-border-control:"), "suite-tokens must define --suite-border-control");
  assert(!tokensCss.includes("<<colour"), "suite-tokens must evaluate all <<colour>> macros");
  assert(kojimaCss.includes("container: kj-dashboard / inline-size"), "kojima.tid must declare container query context");
  assert(kojimaCss.includes("font-variant-numeric: tabular-nums"), "kojima.tid must use tabular-nums");

  // 3. Verify `Kojima Movie Recommendations` dashboard renders seeded movies sorted by kojima-score
  const seenMovies = $tw.wiki.filterTiddlers("[tag[Movie]tag[Kojima]field:status[Completed]]");
  assert.strictEqual(
    seenMovies.length,
    0,
    `All demo movies should default to Backlog (0 Completed), found: ${seenMovies.join(", ")}`
  );
  const dashboardHtml = $tw.wiki.renderTiddler(
    "text/html",
    "Kojima Movie Recommendations"
  );
  assert(dashboardHtml.includes("Hideo Kojima Movie Recommendations"), "Dashboard heading should render");
  assert(dashboardHtml.includes("Taxi Driver (1976)"), "Dashboard should render Taxi Driver (1976)");
  assert(dashboardHtml.includes("2001: A Space Odyssey (1968)"), "Dashboard should render 2001: A Space Odyssey (1968)");
  assert(dashboardHtml.includes("Madame Web (2024)"), "Dashboard should render Madame Web (2024) when dismissals are shown");
  assert(
    /aria-label="5 of 5 stars"[^>]*>★★★★★<\/span>\s*Canon/.test(dashboardHtml),
    "Dashboard should render 5-star Canon badge"
  );
  assert(
    /class="kj-score-badge kj-score-badge--d-tier"[\s\S]*?aria-label="1 of 5 stars"[^>]*>★☆☆☆☆<\/span>\s*Dismissed/.test(dashboardHtml),
    "Dashboard should render 1-star Dismissed badge"
  );
  assert(!dashboardHtml.includes("/100"), "Dashboard should no longer render 0-100 scores");

  // 4. Verify dashboard does not render a Hide 1-Sentence Dismissals checkbox and filters by level instead
  assert(
    !dashboardHtml.includes("Hide 1-Sentence Dismissals"),
    "Dashboard should not render the Hide 1-Sentence Dismissals checkbox"
  );
  $tw.wiki.setText("$:/state/kojima-dashboard/level", "text", null, "Canon");
  const filteredHtml = $tw.wiki.renderTiddler("text/html", "Kojima Movie Recommendations");
  assert(!filteredHtml.includes("Madame Web (2024)"), "Madame Web (2024) should be hidden when level filter is Canon");
  assert(filteredHtml.includes("2001: A Space Odyssey (1968)"), "Canon movies should remain visible");
  $tw.wiki.setText("$:/state/kojima-dashboard/level", "text", null, "All");

  // 5. Verify `tm-sync-kojima` preserves existing watch status (`Completed`) and `favorite`
  $tw.wiki.addTiddler(
    new $tw.Tiddler($tw.wiki.getTiddler("Conclave (2024)"), {
      title: "Conclave (2024)",
      status: "Completed",
      favorite: "yes",
    })
  );
  $tw.rootWidget.dispatchEvent({
    type: "tm-sync-kojima",
    paramObject: {
      items: [
        {
          title: "Conclave",
          year: 2024,
          tmdb_id: 974576,
          kojima_score: 4,
          kojima_tier: "Loved",
        },
      ],
    },
  });
  const conclaveAfterSync = $tw.wiki.getTiddler("Conclave (2024)");
  assert.strictEqual(
    conclaveAfterSync.fields.status,
    "Completed",
    "Re-syncing must preserve user watch status Completed"
  );
  assert.strictEqual(
    conclaveAfterSync.fields.favorite,
    "yes",
    "Re-syncing must preserve user favorite field"
  );

  // 6. Verify individual Movie tiddler ViewTemplate renders Kojima Enthusiasm & Commentary panel
  const viewTplHtml = $tw.wiki.renderTiddler(
    "text/html",
    "$:/plugins/mblackman/kojima-media/view-template",
    { variables: { currentTiddler: "2001: A Space Odyssey (1968)" } }
  );
  assert(viewTplHtml.includes("Hideo Kojima Recommendation"), "ViewTemplate should render Kojima panel");
  assert(viewTplHtml.includes('aria-label="5 of 5 stars"'), "ViewTemplate should render an accessible 5-star rating");
  assert(viewTplHtml.includes("GQ All-Time Top 4"), "ViewTemplate should render source chip from kojima-sources-json");
  assert(viewTplHtml.includes("2025-05-14"), "ViewTemplate should render recommendation date 2025-05-14");

  // 7. Verify dashboard sort options (recommended-desc, year-asc, words-desc, runtime-asc)
  $tw.wiki.setText("$:/state/kojima-dashboard/sort", "text", null, "recommended-desc");
  const sortedRecDesc = $tw.wiki.renderTiddler("text/html", "Kojima Movie Recommendations");
  assert(
    sortedRecDesc.indexOf("Avatar: Fire and Ash (2025)") < sortedRecDesc.indexOf("The Great Escape (1963)"),
    "Sorting by recommended-desc should place 2025-12-29 recommendations before 2008-06-12 recommendations"
  );

  $tw.wiki.setText("$:/state/kojima-dashboard/sort", "text", null, "year-asc");
  const sortedYearAsc = $tw.wiki.renderTiddler("text/html", "Kojima Movie Recommendations");
  assert(
    sortedYearAsc.indexOf("North by Northwest (1959)") < sortedYearAsc.indexOf("Blade Runner (1982)"),
    "Sorting by year-asc should place 1959 classics before 1982 films"
  );

  $tw.wiki.setText("$:/state/kojima-dashboard/sort", "text", null, "runtime-asc");
  const sortedRuntimeAsc = $tw.wiki.renderTiddler("text/html", "Kojima Movie Recommendations");
  assert(
    sortedRuntimeAsc.indexOf("Django (1966)") < sortedRuntimeAsc.indexOf("Avatar: Fire and Ash (2025)"),
    "Sorting by runtime-asc should place 91-min Django before 192-min Avatar"
  );
  $tw.wiki.setText("$:/state/kojima-dashboard/sort", "text", null, "score-desc");

  console.log("✅ All 7 TiddlyWiki Kojima Movie Tracker test suites passed!");
}

runTests();
