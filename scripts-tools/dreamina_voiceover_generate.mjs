import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const repoRoot = path.resolve(path.dirname(__filename), "..");
const profileDir = path.join(repoRoot, "data", "temp", "playwright", "dreamina-profile");
const targetUrl = "https://jimeng.jianying.com/ai-tool/home/?type=audio";
const outputDir = process.env.DREAMINA_AUDIO_OUTPUT_DIR
  || path.join(os.homedir(), "nas_kb", "inbox", "AIVoiceover");
const voiceName = process.env.DREAMINA_VOICE_NAME || "明媚女声";
const text = process.argv.slice(2).join(" ").trim()
  || "风格特征：浓郁的欧洲民间风情，特别是法国香颂风格。虽然很多时候主乐器是手风琴，但口琴版本的悠扬感完美符合欧洲民间曲风。常见场景：电视剧里只要切到浪漫的欧洲街景、咖啡馆，或者男女主角轻松浪漫的互动，这首旋律的变奏就会响起。";
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

function extensionFrom(contentType, url) {
  if (/wav/i.test(contentType) || /\.wav(?:[?#]|$)/i.test(url)) return ".wav";
  if (/mpeg|mp3/i.test(contentType) || /\.mp3(?:[?#]|$)/i.test(url)) return ".mp3";
  if (/m4a|mp4/i.test(contentType) || /\.m4a(?:[?#]|$)/i.test(url)) return ".m4a";
  if (/ogg/i.test(contentType) || /\.ogg(?:[?#]|$)/i.test(url)) return ".ogg";
  if (/aac/i.test(contentType) || /\.aac(?:[?#]|$)/i.test(url)) return ".aac";
  if (/flac/i.test(contentType) || /\.flac(?:[?#]|$)/i.test(url)) return ".flac";
  return ".mp3";
}

function collectAudioUrls(value, urls = new Set(), keyPath = "") {
  if (typeof value === "string") {
    const matches = value.match(/https?:\/\/[^\s"'<>\\]+(?:mp3|wav|m4a|aac|ogg|flac)(?:[^\s"'<>\\]*)?/gi);
    for (const match of matches ?? []) urls.add(match);
    if (/audio|voice|sound|dub|tts|speech|play_url|download_url/i.test(keyPath)
      && /^https?:\/\//i.test(value)
      && !/\.(?:jpg|jpeg|png|webp|gif|image)(?:[?#]|$)/i.test(value)) {
      urls.add(value);
    }
    return urls;
  }
  if (Array.isArray(value)) {
    for (const item of value) collectAudioUrls(item, urls, keyPath);
    return urls;
  }
  if (value && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) {
      collectAudioUrls(item, urls, keyPath ? `${keyPath}.${key}` : key);
    }
  }
  return urls;
}

async function clickVisibleText(page, textToClick) {
  const locator = page.getByText(textToClick, { exact: false }).first();
  await locator.click({ timeout: 15000 });
}

async function chooseVoice(page) {
  await page.getByText("音色", { exact: true }).first().click({ timeout: 15000 });
  await page.waitForTimeout(2000);
  await clickVisibleText(page, voiceName);
  await page.waitForTimeout(1000);
}

async function fillVoiceoverText(page) {
  const textarea = page.locator('textarea[placeholder*="说话内容"]:visible').first();
  await textarea.click({ timeout: 15000 });
  await textarea.fill(text);
  await page.waitForTimeout(1000);
}

async function clickGenerate(page) {
  const clicked = await page.locator("button").evaluateAll((buttons) => {
    const candidates = buttons
      .map((button, index) => {
        const rect = button.getBoundingClientRect();
        return {
          index,
          disabled: button.disabled || button.getAttribute("aria-disabled") === "true",
          visible: rect.width > 0 && rect.height > 0,
          x: rect.x,
          y: rect.y,
          width: rect.width,
          height: rect.height,
          text: (button.innerText || button.getAttribute("aria-label") || "").trim(),
        };
      })
      .filter((button) => (
        button.visible
        && !button.disabled
        && button.x > 1050
        && button.y < 380
        && button.width >= 24
        && button.height >= 24
      ))
      .sort((a, b) => (a.y - b.y) || (b.x - a.x));
    const target = candidates[0];
    if (!target) return false;
    buttons[target.index].click();
    return true;
  });
  if (!clicked) {
    throw new Error("未找到可点击的生成按钮，可能还没有选中音色或文案未生效");
  }
}

async function saveAudioResponse(response, index) {
  const contentType = response.headers()["content-type"] || "";
  const url = response.url();
  if (!/audio|mpeg|wav|ogg|m4a|aac|flac/i.test(contentType)
    && !/\.(mp3|wav|m4a|aac|ogg|flac)(?:[?#]|$)/i.test(url)) {
    return null;
  }
  const body = await response.body();
  if (body.byteLength < 1024) return null;
  const ext = extensionFrom(contentType, url);
  const filename = `dreamina_voiceover_${new Date().toISOString().replace(/[-:]/g, "").replace(/\..+/, "")}_${index}${ext}`;
  const outputPath = path.join(outputDir, filename);
  await fs.writeFile(outputPath, body);
  return outputPath;
}

function isResultAudioUrl(url) {
  return /^https?:\/\//i.test(url)
    && /audio_mpeg|mime_type=audio|\.mp3|\.wav|\.m4a|\.aac|\.ogg|\.flac/i.test(url)
    && !/effect|faceu|voice_clone|voice_sample|preview/i.test(url)
    && !/\.(?:jpg|jpeg|png|webp|gif|image)(?:[?#]|$)/i.test(url);
}

function audioQualityScore(url) {
  const brMatch = url.match(/[?&]br=(\d+)/i);
  const btMatch = url.match(/[?&]bt=(\d+)/i);
  return Math.max(
    brMatch ? Number(brMatch[1]) : 0,
    btMatch ? Number(btMatch[1]) : 0,
  );
}

function chooseFirstHighQualityResult(urls) {
  const candidates = Array.from(urls).filter(isResultAudioUrl);
  for (let index = 0; index < candidates.length; index += 2) {
    const pair = candidates.slice(index, index + 2);
    if (pair.length === 0) continue;
    return pair.sort((left, right) => audioQualityScore(right) - audioQualityScore(left))[0];
  }
  return null;
}

const { chromium } = await loadPlaywright();
await fs.mkdir(outputDir, { recursive: true });

const context = await chromium.launchPersistentContext(profileDir, {
  channel: "chrome",
  headless: true,
  viewport: { width: 1440, height: 1000 },
});

const foundUrls = new Set();
const savedPaths = [];
let collectResultUrls = false;

try {
  const page = context.pages()[0] ?? await context.newPage();
  page.on("response", async (response) => {
    try {
      const url = response.url();
      const contentType = response.headers()["content-type"] || "";
      if (collectResultUrls && url.includes("jimeng.jianying.com") && /json/i.test(contentType)) {
        const json = await response.json().catch(() => null);
        collectAudioUrls(json, foundUrls);
      }
    } catch {
      // Network listeners should never fail the main generation flow.
    }
  });

  await page.goto(targetUrl, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(7000);
  await chooseVoice(page);
  await fillVoiceoverText(page);
  collectResultUrls = true;
  await clickGenerate(page);

  const deadline = Date.now() + 180000;
  while (Date.now() < deadline && chooseFirstHighQualityResult(foundUrls) === null) {
    await page.waitForTimeout(3000);
    const domUrls = await page.evaluate(() => {
      const urls = [];
      for (const audio of document.querySelectorAll("audio")) {
        if (audio.currentSrc) urls.push(audio.currentSrc);
        if (audio.src) urls.push(audio.src);
      }
      for (const source of document.querySelectorAll("audio source")) {
        if (source.src) urls.push(source.src);
      }
      return urls;
    });
    for (const url of domUrls) collectAudioUrls(url, foundUrls);
  }

  const selectedUrl = chooseFirstHighQualityResult(foundUrls);
  if (selectedUrl) {
    const response = await page.request.get(selectedUrl).catch(() => null);
    if (response && response.ok()) {
      const contentType = response.headers()["content-type"] || "";
      const body = await response.body();
      if (/audio|mpeg|wav|ogg|m4a|aac|flac/i.test(contentType) && body.byteLength >= 1024) {
        const ext = extensionFrom(contentType, selectedUrl);
        const filename = `dreamina_voiceover_${new Date().toISOString().replace(/[-:]/g, "").replace(/\..+/, "")}${ext}`;
        const outputPath = path.join(outputDir, filename);
        await fs.writeFile(outputPath, body);
        savedPaths.push(outputPath);
      }
    }
  }

  if (savedPaths.length === 0) {
    for (const url of foundUrls) {
      if (savedPaths.length > 0) break;
      if (!isResultAudioUrl(url)) continue;
      const response = await page.request.get(url).catch(() => null);
      if (!response || !response.ok()) continue;
      const contentType = response.headers()["content-type"] || "";
      const body = await response.body();
      if (!/audio|mpeg|wav|ogg|m4a|aac|flac/i.test(contentType)) continue;
      if (body.byteLength < 1024) continue;
      const ext = extensionFrom(contentType, url);
      const filename = `dreamina_voiceover_${new Date().toISOString().replace(/[-:]/g, "").replace(/\..+/, "")}_url${ext}`;
      const outputPath = path.join(outputDir, filename);
      await fs.writeFile(outputPath, body);
      savedPaths.push(outputPath);
    }
  }

  const bodyText = await page.locator("body").innerText({ timeout: 10000 }).catch(() => "");
  console.log(JSON.stringify({
    ok: savedPaths.length > 0,
    outputDir,
    savedPaths,
    selectedUrl,
    foundUrls: Array.from(foundUrls).slice(0, 10),
    url: page.url(),
    hasLoginEntry: bodyText.includes("登录"),
    statusHints: bodyText.split("\n").filter((line) => /生成|配音|音频|下载|失败|排队|明媚/.test(line)).slice(0, 80),
  }, null, 2));
} finally {
  await context.close();
}
