import path from "node:path";
import os from "node:os";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const repoRoot = path.resolve(path.dirname(__filename), "..");
const profileDir = path.join(repoRoot, "data", "temp", "playwright", "dreamina-profile");
const targetUrl = "https://jimeng.jianying.com/ai-tool/home/?type=audio";
const bundledNodeModules = path.join(
  os.homedir(),
  ".cache",
  "codex-runtimes",
  "codex-primary-runtime",
  "dependencies",
  "node",
  "node_modules",
  ".pnpm",
  "playwright@1.60.0",
  "node_modules",
  "playwright",
  "package.json",
);

async function loadPlaywright() {
  try {
    return await import("playwright");
  } catch {
    const require = createRequire(bundledNodeModules);
    return require("playwright");
  }
}

const { chromium } = await loadPlaywright();

const context = await chromium.launchPersistentContext(profileDir, {
  channel: "chrome",
  headless: false,
  viewport: { width: 1440, height: 1000 },
});

const page = context.pages()[0] ?? await context.newPage();
await page.goto(targetUrl, { waitUntil: "domcontentloaded" });

console.log(`Dreamina profile: ${profileDir}`);
console.log("Log in to Dreamina in the opened Chrome window.");
console.log("When login is done, close the browser window or press Ctrl+C here.");

context.on("close", () => {
  console.log("Dreamina browser closed. Login state has been saved in the profile.");
});

await new Promise((resolve) => context.on("close", resolve));
