import path from "node:path";
import os from "node:os";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const repoRoot = path.resolve(path.dirname(__filename), "..");
const profileDir = path.join(repoRoot, "data", "temp", "playwright", "dreamina-profile");
const targetUrl = "https://jimeng.jianying.com/ai-tool/home/?type=audio";
const voiceName = process.argv[2] || "明媚女声";
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
  await page.waitForTimeout(7000);

  const audioButton = page.getByText("音色", { exact: true }).first();
  await audioButton.click({ timeout: 10000 });
  await page.waitForTimeout(3000);

  const bodyText = await page.locator("body").innerText({ timeout: 10000 });
  const voiceLocator = page.getByText(voiceName, { exact: false }).first();
  const voiceCount = await voiceLocator.count();
  let selected = false;
  if (voiceCount > 0) {
    await voiceLocator.click({ timeout: 10000 });
    await page.waitForTimeout(2000);
    selected = true;
  }

  const afterText = await page.locator("body").innerText({ timeout: 10000 });
  const buttons = await page.locator("button").evaluateAll((nodes) => (
    nodes.map((button) => ({
      text: (button.innerText || button.getAttribute("aria-label") || "").trim(),
      disabled: button.disabled || button.getAttribute("aria-disabled") === "true",
    })).filter((button) => button.text || button.disabled).slice(0, 60)
  ));

  console.log(JSON.stringify({
    url: page.url(),
    requestedVoice: voiceName,
    selected,
    hasLoginEntry: afterText.includes("登录"),
    hasRequestedVoice: afterText.includes(voiceName),
    visibleVoiceHints: afterText
      .split("\n")
      .filter((line) => /收藏|音色|女声|男声|明媚|声音|配音|克隆/.test(line))
      .slice(0, 80),
    buttons,
    bodyBeforeVoiceSearch: bodyText
      .split("\n")
      .filter((line) => /收藏|音色|女声|男声|明媚|声音|配音|克隆/.test(line))
      .slice(0, 80),
  }, null, 2));
} finally {
  await context.close();
}
