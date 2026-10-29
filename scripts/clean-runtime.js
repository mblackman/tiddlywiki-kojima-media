const fs = require("fs");
const path = require("path");

const dir = path.join(__dirname, "..", "demo", "tiddlers");
if (fs.existsSync(dir)) {
  for (const file of fs.readdirSync(dir)) {
    if (
      file.startsWith("$__StoryList") ||
      file.startsWith("$__HistoryList") ||
      file.startsWith("$__UpgradeLibrary") ||
      file.startsWith("$__info_browser_window") ||
      file.startsWith("$__temp_") ||
      file.startsWith("$__state_")
    ) {
      try {
        fs.unlinkSync(path.join(dir, file));
      } catch (e) {
        // ignore
      }
    }
  }
}
