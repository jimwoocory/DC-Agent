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
  headless: true,
  viewport: { width: 1440, height: 1000 },
});

try {
  const page = context.pages()[0] ?? await context.newPage();
  await page.goto(targetUrl, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(5000);

  const bodyText = await page.locator("body").innerText({ timeout: 10000 });
  const cookies = await context.cookies("https://jimeng.jianying.com");
  const cookieNames = cookies.map((cookie) => cookie.name).sort();
  const hasLoginEntry = bodyText.includes("登录");
  const hasAudioMode = bodyText.includes("配音生成");

  console.log(JSON.stringify({
    profileDir,
    url: page.url(),
    hasAudioMode,
    hasLoginEntry,
    cookieNames,
  }, null, 2));
} finally {
  await context.close();
}
