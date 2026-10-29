#!/usr/bin/env node
/**
 * Live development server for Kojima Movie Tracker ($:/plugins/mblackman/kojima-media).
 */

const fs = require("fs");
const path = require("path");
const { spawn, execFileSync } = require("child_process");
const { main: seedDemo } = require("./seed-demo");

const rootDir = path.resolve(__dirname, "..");
const pluginDir = path.join(rootDir, "plugins", "mblackman", "kojima-media");
const demoDir = path.join(rootDir, "demo");
const debugDir = path.join(rootDir, "debug");
const twBin = path.join(rootDir, "node_modules", "tiddlywiki", "tiddlywiki.js");
const port = process.env.PORT || "8080";
const host = process.env.HOST || "0.0.0.0";

let tiddlyWikiProcess = null;

function syncDebugWiki() {
  try {
    execFileSync("python3", ["kojima_sync_service.py", "--sync-static"], {
      cwd: rootDir,
      stdio: "inherit",
    });
  } catch (err) {
    console.warn("[warn] Could not run kojima_sync_service.py --sync-static, using existing demo/seed-recommendations.json");
  }
  seedDemo();
  fs.rmSync(debugDir, { recursive: true, force: true });
  fs.cpSync(demoDir, debugDir, { recursive: true });
}

function startServer() {
  if (tiddlyWikiProcess) return;
  syncDebugWiki();
  console.log(`🌐 Starting Kojima Movie Tracker dev server on http://${host}:${port} ...`);
  tiddlyWikiProcess = spawn(
    process.execPath,
    [twBin, "debug", "--listen", `port=${port}`, `host=${host}`],
    {
      stdio: "inherit",
      cwd: rootDir,
      env: { ...process.env, TIDDLYWIKI_PLUGIN_PATH: "plugins" },
    }
  );
  tiddlyWikiProcess.on("exit", () => {
    tiddlyWikiProcess = null;
  });
}

startServer();
