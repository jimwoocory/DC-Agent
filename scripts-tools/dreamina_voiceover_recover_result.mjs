import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const repoRoot = path.resolve(path.dirname(__filename), "..");
const profileDir = path.join(repoRoot, "data", "temp", "playwright", "dreamina-profile");
const outputDir = process.env.DREAMINA_AUDIO_OUTPUT_DIR
  || path.join(os.homedir(), "nas_kb", "inbox", "AIVoiceover");
const workspace = process.argv[2] || "13974015230220";
const targetUrl = `https://jimeng.jianying.com/ai-tool/generate?workspace=${workspace}`;
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

function collectUrls(value, urls = new Set(), keyPath = "") {
  if (typeof value === "string") {
    const explicit = value.match(/https?:\/\/[^\s"'<>\\]+(?:mp3|wav|m4a|aac|ogg|flac)(?:[^\s"'<>\\]*)?/gi);
    for (const match of explicit ?? []) urls.add(match);
    if (/audio|voice|sound|dub|tts|speech|play_url|download_url|material_url|result/i.test(keyPath)
      && /^https?:\/\//i.test(value)
      && !/\.(?:jpg|jpeg|png|webp|gif|image)(?:[?#]|$)/i.test(value)) {
      urls.add(value);
    }
    return urls;
  }
  if (Array.isArray(value)) {
    for (const item of value) collectUrls(item, urls, keyPath);
    return urls;
  }
  if (value && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) {
      collectUrls(item, urls, keyPath ? `${keyPath}.${key}` : key);
    }
  }
  return urls;
}

async function saveUrl(page, url, index) {
  const response = await page.request.get(url).catch(() => null);
  if (!response || !response.ok()) return null;
  const contentType = response.headers()["content-type"] || "";
  if (!/audio|mpeg|wav|ogg|m4a|aac|flac/i.test(contentType)) return null;
  const body = await response.body();
  if (body.byteLength < 1024) return null;
  const ext = extensionFrom(contentType, url);
  const filename = `dreamina_voiceover_result_${new Date().toISOString().replace(/[-:]/g, "").replace(/\..+/, "")}_${index}${ext}`;
  const outputPath = path.join(outputDir, filename);
  await fs.writeFile(outputPath, body);
  return outputPath;
}

const { chromium } = await loadPlaywright();
await fs.mkdir(outputDir, { recursive: true });
const context = await chromium.launchPersistentContext(profileDir, {
  channel: "chrome",
  headless: true,
  viewport: { width: 1440, height: 1000 },
});

const urls = new Set();
const savedPaths = [];

try {
  const page = context.pages()[0] ?? await context.newPage();
  page.on("response", async (response) => {
    try {
      const url = response.url();
      const contentType = response.headers()["content-type"] || "";
      if (url.includes("jimeng.jianying.com") && /json/i.test(contentType)) {
        const json = await response.json().catch(() => null);
        collectUrls(json, urls);
      }
      if (/audio|mpeg|wav|ogg|m4a|aac|flac/i.test(contentType)
        && !/effect|faceu|voice_clone|voice_sample|preview/i.test(url)) {
        const body = await response.body();
        if (body.byteLength > 1024) {
          const ext = extensionFrom(contentType, url);
          const filename = `dreamina_voiceover_result_${new Date().toISOString().replace(/[-:]/g, "").replace(/\..+/, "")}_response${ext}`;
          const outputPath = path.join(outputDir, filename);
          await fs.writeFile(outputPath, body);
          savedPaths.push(outputPath);
        }
      }
    } catch {
      // Keep passive network capture best-effort.
    }
  });

  await page.goto(targetUrl, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(15000);

  const domUrls = await page.evaluate(() => {
    const found = [];
    for (const audio of document.querySelectorAll("audio")) {
      if (audio.currentSrc) found.push(audio.currentSrc);
      if (audio.src) found.push(audio.src);
    }
    for (const source of document.querySelectorAll("audio source")) {
      if (source.src) found.push(source.src);
    }
    const html = document.documentElement.innerHTML;
    for (const match of html.matchAll(/https?:\/\/[^"'<>\\]+(?:mp3|wav|m4a|aac|ogg|flac)[^"'<>\\]*/gi)) {
      found.push(match[0]);
    }
    return found;
  });
  for (const url of domUrls) urls.add(url);

  let index = 1;
  for (const url of urls) {
    if (/effect|faceu|voice_clone|voice_sample|preview/i.test(url)) continue;
    const saved = await saveUrl(page, url, index++);
    if (saved) savedPaths.push(saved);
  }

  const bodyText = await page.locator("body").innerText({ timeout: 10000 }).catch(() => "");
  console.log(JSON.stringify({
    ok: savedPaths.length > 0,
    workspace,
    targetUrl,
    outputDir,
    savedPaths: [...new Set(savedPaths)],
    candidateUrls: [...urls].slice(0, 30),
    statusHints: bodyText.split("\n").filter((line) => /生成|配音|音频|下载|失败|排队|明媚|风格特征|人声/.test(line)).slice(0, 100),
  }, null, 2));
} finally {
  await context.close();
}
