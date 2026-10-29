/*\
title: $:/plugins/mblackman/kojima-media/startup/kojima-sync.js
type: application/javascript
module-type: startup

Kojima Movie Tracker Startup Module.
Registers `tm-sync-kojima` to import Hideo Kojima movie recommendations from the
configured JSON feed URL (GitHub Pages by default) while preserving existing watch status.
\*/
(function() {
  "use strict";

  exports.name = "kojima-media-startup";
  exports.platforms = ["browser", "node"];
  exports.after = ["startup", "media-manager-startup"];
  exports.synchronous = true;

  var SYNC_STATE_TIDDLER = "$:/state/kojima-sync/status";
  var ENDPOINT_CONFIG_TIDDLER = "$:/plugins/mblackman/kojima-media/configs/kojima-sync-url";
  var LEGACY_ENDPOINT_TIDDLER = "$:/plugins/mblackman/media-importer/configs/kojima-sync-url";
  var DEFAULT_FEED_URL = "https://mblackman.github.io/tiddlywiki-kojima-media/api/kojima-recommendations.json";

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
   * when no TMDB API key is configured on the client, while preserving existing user
   * status (`Completed`, `Active`, `Dropped`), `favorite`, and `rating`.
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
   * Imports an array of enriched Kojima recommendation items directly from the feed payload.
   */
  function processKojimaItems(items, sourceLabel) {
    for (var i = 0; i < items.length; i++) {
      upsertKojimaMovieDirectly(items[i]);
    }
    $tw.wiki.setText(
      SYNC_STATE_TIDDLER,
      "text",
      null,
      "Synced " + items.length + " Kojima movies from " + sourceLabel
    );
  }

  exports.startup = function() {
    if ($tw._kojimaSyncRegistered) return;
    $tw._kojimaSyncRegistered = true;

    $tw.rootWidget.addEventListener("tm-sync-kojima", function(event) {
      if (event && event.paramObject && Array.isArray(event.paramObject.items)) {
        processKojimaItems(event.paramObject.items, "Direct Payload");
        return;
      }

      var syncUrl =
        ($tw.wiki.getTiddlerText(ENDPOINT_CONFIG_TIDDLER, "") || "").trim() ||
        ($tw.wiki.getTiddlerText(LEGACY_ENDPOINT_TIDDLER, "") || "").trim() ||
        DEFAULT_FEED_URL;

      // When a local endpoint is configured and accessed over LAN (e.g. http://192.168.x.x:8080),
      // rewrite localhost/127.0.0.1 to the host IP
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
        $tw.wiki.setText(
          SYNC_STATE_TIDDLER,
          "text",
          null,
          "Sync requires browser fetch support to connect to " + syncUrl
        );
        return;
      }

      $tw.wiki.setText(SYNC_STATE_TIDDLER, "text", null, "Fetching recommendations from " + syncUrl + "...");

      var sourceLabel = syncUrl.indexOf("github.io") !== -1 ? "GitHub Pages Feed" : "Sync Endpoint";

      fetch(syncUrl)
        .then(function(res) {
          if (!res.ok) throw new Error("HTTP " + res.status);
          return res.json();
        })
        .then(function(payload) {
          var items = payload.items || (Array.isArray(payload) ? payload : []);
          processKojimaItems(items, sourceLabel);
        })
        .catch(function(err) {
          $tw.wiki.setText(
            SYNC_STATE_TIDDLER,
            "text",
            null,
            "Sync error (" + err.message + ") when fetching " + syncUrl
          );
        });
    });
  };
})();
