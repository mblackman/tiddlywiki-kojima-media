const fs = require("fs");
const path = require("path");

const ALLOWED_DEMO_FILES = new Set([
  "$__DefaultTiddlers.tid",
  "$__DemoStylesheet.tid",
  "$__SiteTitle.tid",
  "$__SiteSubtitle.tid",
  "$__plugins_mblackman_media-importer.json",
  "KojimaMovies.json",
]);

const rootDir = path.join(__dirname, "..");
const dir = path.join(rootDir, "demo", "tiddlers");
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

for (const extraDir of ["debug", "data"]) {
  const target = path.join(rootDir, extraDir);
  if (fs.existsSync(target)) {
    fs.rmSync(target, { recursive: true, force: true });
  }
}
