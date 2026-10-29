#!/usr/bin/env node
/**
 * Builds the Kojima Movie Tracker plugin ($:/plugins/mblackman/kojima-media)
 * into build/kojima-media.tid, build/kojima-media-plugin.json, and dist/kojima-media-plugin.json.
 */

const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");

const ROOT_DIR = path.resolve(__dirname, "..");
const BUILD_DIR = path.join(ROOT_DIR, "build");
const DIST_DIR = path.join(ROOT_DIR, "dist");

function main() {
  fs.mkdirSync(BUILD_DIR, { recursive: true });
  fs.mkdirSync(DIST_DIR, { recursive: true });

  const twBin = path.join(ROOT_DIR, "node_modules", ".bin", "tiddlywiki");
  execSync(`"${twBin}" demo --output build --build kojima-media`, {
    stdio: "inherit",
    cwd: ROOT_DIR,
    env: { ...process.env, TIDDLYWIKI_PLUGIN_PATH: "plugins" },
  });

  const rawJsonPath = path.join(BUILD_DIR, "kojima-media-plugin.json");
  if (fs.existsSync(rawJsonPath)) {
    const pluginObj = JSON.parse(fs.readFileSync(rawJsonPath, "utf8"));
    const dragDropBundle = Array.isArray(pluginObj) ? pluginObj : [pluginObj];
    fs.writeFileSync(
      path.join(DIST_DIR, "kojima-media-plugin.json"),
      JSON.stringify(dragDropBundle, null, 2),
      "utf8"
    );
  }

  console.log("✅ Built build/kojima-media.tid and dist/kojima-media-plugin.json");
}

if (require.main === module) {
  main();
}
