const fs = require("fs");
const path = require("path");

const ALLOWED_DEMO_FILES = new Set([
  "$__DefaultTiddlers.tid",
  "$__SiteTitle.tid",
  "$__SiteSubtitle.tid",
  "$__plugins_mblackman_media-importer.json",
  "KojimaMovies.json",
]);

const dir = path.join(__dirname, "..", "demo", "tiddlers");
if (fs.existsSync(dir)) {
  for (const file of fs.readdirSync(dir)) {
    if (!ALLOWED_DEMO_FILES.has(file)) {
      try {
        fs.unlinkSync(path.join(dir, file));
      } catch (e) {
        // ignore
      }
    }
  }
}
