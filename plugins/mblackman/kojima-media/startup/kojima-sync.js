/*\
title: $:/plugins/mblackman/kojima-media/startup/kojima-sync.js
type: application/javascript
module-type: startup

Kojima Movie Tracker Startup Module.
Registers `tm-sync-kojima` (Docker HTTP sync) and `tm-sync-kojima-seed` (offline curated seed).
Delegates canonical Movie tiddler creation to `tiddlywiki-media-importer` (`tm-fetch-Movie`)
when a TMDB API key is configured, and provides a direct enrichment fallback when offline or
when no TMDB API key is set.
\*/
(function() {
  "use strict";

  exports.name = "kojima-media-startup";
  exports.platforms = ["browser", "node"];
  exports.after = ["startup", "media-manager-startup"];
  exports.synchronous = true;

  var SYNC_STATE_TIDDLER = "$:/state/kojima-sync/status";
  var ENDPOINT_CONFIG_TIDDLER = "$:/plugins/mblackman/media-importer/configs/kojima-sync-url";
  var TMDB_KEY_TIDDLER = "$:/plugins/mblackman/media-importer/configs/tmdb-api-key";
  var SEED_DATA_TIDDLER = "$:/plugins/mblackman/kojima-media/data/seed-recommendations.json";

  function mergeTagLists() {
    var seen = Object.create(null);
    var merged = [];
    for (var i = 0; i < arguments.length; i++) {
      var raw = arguments[i];
      if (!raw) continue;
      var parsed = Array.isArray(raw) ? raw : ($tw.utils.parseStringArray(String(raw)) || []);
      for (var j = 0; j < parsed.length; j++) {
        var tag = parsed[j];
        if (tag && !seen[tag]) {
          seen[tag] = true;
          merged.push(tag);
        }
      }
    }
    return $tw.utils.stringifyList(merged);
  }

  function formatBracketList(items) {
    if (!items) return "";
    var arr = Array.isArray(items)
      ? items
      : String(items)
          .split(",")
          .map(function(s) { return s.trim(); })
          .filter(Boolean);
    return arr.map(function(entry) { return "[[" + entry + "]]"; }).join(" ");
  }

  /**
   * Direct enrichment fallback: Creates or updates the canonical Movie + Kojima tiddler
   * when no TMDB API key is configured (or in headless/offline environments),
   * while preserving existing user status (`Completed`, `Active`, `Dropped`), `favorite`, and `rating`.
   */
  function upsertKojimaMovieDirectly(item) {
    var year = String(item.year || "0000");
    var finalTitle = item.title + " (" + year + ")";
    var existing = $tw.wiki.getTiddler(finalTitle);
    var existingFields = existing ? existing.fields : {};

    var status = existingFields.status || "Backlog";
    var created = existingFields.created || new Date();
    var tags = mergeTagLists(
      existingFields.tags,
      "$:/tags/media-importer/Media Movie Kojima"
    );

    var plot = item.plot || existingFields.plot || existingFields.description || "";
    var image = item.poster || existingFields.image || "";

    var fields = {
      title: finalTitle,
      englishTitle: existingFields.englishTitle || item.title,
      year: year,
      dataSource: existingFields.dataSource || "TMDB",
      url: existingFields.url || (item.tmdb_id ? "https://www.themoviedb.org/movie/" + item.tmdb_id : ""),
      id: String(item.tmdb_id || existingFields.id || ""),
      director: existingFields.director || formatBracketList(item.director),
      cast: existingFields.cast || "",
      genres: existingFields.genres || formatBracketList(item.genres),
      runtime: String(item.runtime || existingFields.runtime || ""),
      onlineRating: existingFields.onlineRating || "0",
      image: image,
      "cover-storage": image ? (String(image).indexOf("data:") === 0 ? "embedded" : "external-uri") : "",
      status: status,
      tags: tags,
      "media-type": "Movie",
      created: created,
      modified: new Date(),
      plot: plot,
      description: existingFields.description || plot,
      "kojima-score": String(item.kojima_score != null ? item.kojima_score : 0),
      "kojima-tier": item.kojima_tier || "",
      "kojima-review": item.kojima_review || "",
      "kojima-word-count": String(item.kojima_word_count || 0),
      "kojima-is-dismissal": item.kojima_is_dismissal || "no",
      "kojima-sentiment-summary": item.kojima_sentiment_summary || "",
      "kojima-score-breakdown": item.kojima_score_breakdown || "",
      "kojima-primary-url": item.kojima_primary_url || "",
      "kojima-primary-source": item.kojima_primary_source || "",
      "kojima-recommended-at": item.kojima_recommended_at || existingFields["kojima-recommended-at"] || "",
      "kojima-sources-json": item.kojima_sources_json || "[]"
    };

    if (existingFields.favorite) {
      fields.favorite = existingFields.favorite;
    }
    if (existingFields.rating) {
      fields.rating = existingFields.rating;
    }

    $tw.wiki.addTiddler(new $tw.Tiddler(existing, fields));
  }

  /**
   * Imports an array of enriched Kojima recommendation items.
   * If a TMDB API key is configured and `fetch` is available in the browser, delegates
   * base tiddler creation to `tm-fetch-Movie` (`TmdbMovieConfig`). Otherwise applies
   * direct enrichment so offline/seed syncs work immediately.
   */
  function processKojimaItems(items, sourceLabel) {
    var apiKey = ($tw.wiki.getTiddlerText(TMDB_KEY_TIDDLER, "") || "").trim();
    var canDelegateToTmdb = Boolean(apiKey && typeof fetch === "function" && $tw.browser);

    if (!canDelegateToTmdb) {
      for (var i = 0; i < items.length; i++) {
        upsertKojimaMovieDirectly(items[i]);
      }
      $tw.wiki.setText(
        SYNC_STATE_TIDDLER,
        "text",
        null,
        "Synced " + items.length + " Kojima movies from " + sourceLabel
      );
      return;
    }

    var index = 0;
    function processNext() {
      if (index >= items.length) {
        $tw.wiki.setText(
          SYNC_STATE_TIDDLER,
          "text",
          null,
          "Synced " + items.length + " Kojima movies via TMDB (" + new Date().toLocaleTimeString() + ")"
        );
        return;
      }

      var item = items[index++];
      if (!item.tmdb_id) {
        upsertKojimaMovieDirectly(item);
        processNext();
        return;
      }

      $tw.wiki.setText(
        SYNC_STATE_TIDDLER,
        "text",
        null,
        "Syncing " + index + " / " + items.length + ": " + (item.title || item.tmdb_id) + "..."
      );

      var tempTitle = "$:/temp/kojima/draft/" + item.tmdb_id;
      $tw.wiki.addTiddler(
        new $tw.Tiddler({
          title: tempTitle,
          draft_id: String(item.tmdb_id),
          draft_title: item.title,
          draft_year: String(item.year || "0000"),
          extra_tags: "Movie Kojima",
          silent: "yes",
          cleanup_draft: "yes",
          "kojima-score": String(item.kojima_score != null ? item.kojima_score : 0),
          "kojima-tier": item.kojima_tier || "",
          "kojima-review": item.kojima_review || "",
          "kojima-word-count": String(item.kojima_word_count || 0),
          "kojima-is-dismissal": item.kojima_is_dismissal || "no",
          "kojima-sentiment-summary": item.kojima_sentiment_summary || "",
          "kojima-score-breakdown": item.kojima_score_breakdown || "",
          "kojima-primary-url": item.kojima_primary_url || "",
          "kojima-primary-source": item.kojima_primary_source || "",
          "kojima-recommended-at": item.kojima_recommended_at || "",
          "kojima-sources-json": item.kojima_sources_json || "[]"
        })
      );

      $tw.rootWidget.dispatchEvent({
        type: "tm-fetch-Movie",
        param: tempTitle,
        paramObject: { silent: "yes", cleanupDraft: "yes", extraTags: "Movie Kojima" }
      });

      setTimeout(processNext, 200);
    }

    processNext();
  }

  exports.startup = function() {
    // 1. Offline / Bundled Seed Sync (`tm-sync-kojima-seed`)
    $tw.rootWidget.addEventListener("tm-sync-kojima-seed", function() {
      var rawSeed = $tw.wiki.getTiddlerText(SEED_DATA_TIDDLER, "[]");
      var items = [];
      try {
        var parsed = JSON.parse(rawSeed);
        items = Array.isArray(parsed) ? parsed : (parsed.items || []);
      } catch (err) {
        $tw.wiki.setText(SYNC_STATE_TIDDLER, "text", null, "Error parsing bundled seed JSON");
        return;
      }
      for (var i = 0; i < items.length; i++) {
        upsertKojimaMovieDirectly(items[i]);
      }
      $tw.wiki.setText(
        SYNC_STATE_TIDDLER,
        "text",
        null,
        "Loaded " + items.length + " curated Kojima movies"
      );
    });

    // 2. Docker HTTP Sync (`tm-sync-kojima`) — only register if not already registered by media-importer
    if ($tw._kojimaSyncRegistered) return;
    $tw._kojimaSyncRegistered = true;

    $tw.rootWidget.addEventListener("tm-sync-kojima", function() {
      var syncUrl =
        ($tw.wiki.getTiddlerText(ENDPOINT_CONFIG_TIDDLER, "") || "").trim() ||
        "http://localhost:8765/api/kojima-recommendations";

      // When accessed over LAN (e.g. http://192.168.x.x:8080), rewrite localhost/127.0.0.1 to the host IP
      if (
        typeof window !== "undefined" &&
        window.location &&
        window.location.hostname &&
        window.location.hostname !== "localhost" &&
        window.location.hostname !== "127.0.0.1"
      ) {
        syncUrl = syncUrl.replace(
          /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?/i,
          "http://" + window.location.hostname + "$2"
        );
      }

      if (typeof fetch !== "function") {
        $tw.rootWidget.dispatchEvent({ type: "tm-sync-kojima-seed" });
        return;
      }

      $tw.wiki.setText(SYNC_STATE_TIDDLER, "text", null, "Connecting to " + syncUrl + "...");

      fetch(syncUrl)
        .then(function(res) {
          if (!res.ok) throw new Error("HTTP " + res.status);
          return res.json();
        })
        .then(function(payload) {
          var items = payload.items || (Array.isArray(payload) ? payload : []);
          processKojimaItems(items, "Docker Sync Service");
        })
        .catch(function(err) {
          // Graceful fallback to bundled seed if Docker container is not reachable
          var rawSeed = $tw.wiki.getTiddlerText(SEED_DATA_TIDDLER, "");
          if (rawSeed) {
            try {
              var seedItems = JSON.parse(rawSeed);
              var list = Array.isArray(seedItems) ? seedItems : (seedItems.items || []);
              for (var i = 0; i < list.length; i++) {
                upsertKojimaMovieDirectly(list[i]);
              }
              $tw.wiki.setText(
                SYNC_STATE_TIDDLER,
                "text",
                null,
                "Docker offline (" + err.message + ") — synced " + list.length + " bundled Kojima movies"
              );
              return;
            } catch (_ignore) {}
          }
          $tw.wiki.setText(SYNC_STATE_TIDDLER, "text", null, "Error: " + err.message);
        });
    });
  };
})();
