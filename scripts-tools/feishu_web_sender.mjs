#!/usr/bin/env node
/**
 * Feishu Web RPA Sender — headless Playwright, sends AS the logged-in user.
 *
 * Uses persistent browser profile (login session already saved).
 * No desktop client needed. No search needed (bot already in conversation list).
 *
 * Usage:
 *   node feishu_web_sender.mjs --text "消息内容"
 *   node feishu_web_sender.mjs --text "消息内容" --target "巅池-Agent小助手" --verbose
 */

import { createRequire } from "module";
import { fileURLToPath } from "url";
import path from "path";
import fs from "fs/promises";

const require = createRequire(
  "/Users/dianchi/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/"
);
const { chromium } = require("playwright");

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const ROOT = path.resolve(__dirname, "..");

// --- CLI args ---
const args = process.argv.slice(2);
let text = args.find(a => a.startsWith("--text="))?.split("=")[1];
if (!text) { const i = args.indexOf("--text"); if (i !== -1) text = args[i + 1]; }
const target = args.find(a => a.startsWith("--target="))?.split("=")[1]
  || (args.indexOf("--target") !== -1 ? args[args.indexOf("--target") + 1] : null)
  || "巅池-Agent小助手";
const verbose = args.includes("--verbose") || args.includes("-v");
const headless = !args.includes("--headed");

if (!text) {
  console.error('Usage: node feishu_web_sender.mjs --text "消息" [--target "会话名"] [--verbose] [--headed]');
  process.exit(1);
}

const FEISHU_URL = "https://o0ain5w98jh.feishu.cn/next/messenger";
const PROFILE_DIR = path.join(ROOT, "data", "feishu-rpa-persistent-profile");
const HOST_RULES = [
  "MAP open.feishu.cn 139.177.246.206",
  "MAP msg-frontier.feishu.cn 34.120.84.45",
  "MAP o0ain5w98jh.feishu.cn 139.177.246.206",
  "MAP www.feishu.cn 139.177.246.206",
  "EXCLUDE localhost", "EXCLUDE 127.0.0.1",
].join(",");

function log(...a) { console.log("[FeishuWeb]", ...a); }
function dbg(...a) { if (verbose) console.log("[FeishuWeb][dbg]", ...a); }

// Strip proxy env vars so Playwright goes direct to Feishu IPs
function cleanProxyEnv() {
  for (const k of ["HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy","NO_PROXY","no_proxy"])
    delete process.env[k];
}

async function dismissOverlays(page) {
  for (let i = 0; i < 3; i++) {
    await page.keyboard.press("Escape").catch(() => {});
    await page.waitForTimeout(200);
  }
  // Remove search/feedback modals
  await page.evaluate(() => {
    for (const node of document.querySelectorAll(".larklet-modal-container, .quickJump_hint")) {
      node.closest("[role='dialog'], [class*='modal'], .larklet-modal-container")?.remove();
    }
  }).catch(() => {});
}

async function findComposer(page) {
  const selectors = [
    'div[class*="composer"] [contenteditable="true"]',
    'div[class*="chat-input"] [contenteditable="true"]',
    'div[class*="input"] [contenteditable="true"]',
    '[placeholder*="发送给"]',
    '[role="textbox"]',
    'div[contenteditable="true"]',
  ];
  const vp = page.viewportSize() || { width: 1280, height: 800 };
  for (const sel of selectors) {
    const items = page.locator(sel);
    const count = await items.count().catch(() => 0);
    for (let i = count - 1; i >= 0; i--) {
      const el = items.nth(i);
      const box = await el.boundingBox().catch(() => null);
      if (!box) continue;
      const visible = await el.isVisible().catch(() => false);
      if (!visible) continue;
      // Composer must be in the right panel (x > 35%) and near bottom (y > 55%)
      if (box.x > vp.width * 0.35 && box.y > vp.height * 0.55 && box.width > 120) {
        return { el, sel, box };
      }
    }
  }
  return { el: null, sel: "", box: null };
}

async function main() {
  const t0 = Date.now();
  log(`Sending to "${target}": ${text.slice(0, 60)}...`);

  cleanProxyEnv();
  await fs.mkdir(PROFILE_DIR, { recursive: true });

  const browser = await chromium.launchPersistentContext(PROFILE_DIR, {
    headless,
    args: [
      "--disable-blink-features=AutomationControlled",
      "--no-proxy-server",
      `--host-resolver-rules=${HOST_RULES}`,
    ],
    viewport: { width: 1280, height: 800 },
  });

  const page = browser.pages()[0] || await browser.newPage();
  let screenshotPath = null;
  let ok = false;

  try {
    // 1. Navigate to messenger
    log("Opening Feishu messenger...");
    if (!page.url().includes("/next/messenger")) {
      await page.goto(FEISHU_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
    }
    await page.waitForTimeout(3000);

    // 2. Click target conversation in the left sidebar list (no search needed)
    log(`Clicking "${target}" in conversation list...`);
    await dismissOverlays(page);

    const targetItem = page.locator(`text="${target}"`).first();
    const targetCount = await targetItem.count().catch(() => 0);
    if (targetCount > 0) {
      await targetItem.click({ timeout: 5000 }).catch(async () => {
        await targetItem.click({ timeout: 5000, force: true }).catch(() => {});
      });
      log("Clicked conversation item");
    } else {
      // Fallback: use search
      log(`"${target}" not found in list, trying search...`);
      await page.keyboard.press("Meta+k").catch(() => {});
      await page.waitForTimeout(800);
      await page.keyboard.press("Meta+a").catch(() => {});
      await page.waitForTimeout(200);
      await page.keyboard.type(target, { delay: 50 });
      await page.waitForTimeout(1200);
      const sr = page.getByText(target, { exact: true }).first();
      if (await sr.count().catch(() => 0) > 0) {
        await sr.click({ timeout: 3000 }).catch(() => {});
      } else {
        await page.keyboard.press("Enter");
      }
    }
    await page.waitForTimeout(2500);
    await dismissOverlays(page);

    // 3. Verify chat panel loaded
    log("Waiting for chat panel...");
    const bodyText = await page.locator("body").innerText({ timeout: 8000 }).catch(() => "");
    if (!bodyText.includes(target)) {
      throw new Error(`Chat panel did not load — "${target}" not found in page text`);
    }
    dbg("Chat panel confirmed");

    // 4. Find and click composer
    const { el: composer, sel, box } = await findComposer(page);
    if (composer) {
      dbg(`Composer found: ${sel} box=${JSON.stringify(box)}`);
      await composer.click({ timeout: 5000 });
      await page.waitForTimeout(300);
      await composer.fill("");
      await page.keyboard.type(text, { delay: 30 });
    } else {
      log("Composer not found via selector, using fallback keyboard");
      await page.keyboard.type(text, { delay: 30 });
    }
    await page.waitForTimeout(500);

    // 5. Send
    await page.keyboard.press("Enter");
    log("Enter pressed");
    await page.waitForTimeout(3000);

    // 6. Verify
    const afterText = await page.locator("body").innerText({ timeout: 5000 }).catch(() => "");
    const msgVisible = afterText.includes(text.slice(0, 30));
    ok = msgVisible;
    if (ok) {
      log("Verified: message visible in chat");
    } else {
      log("Warning: message not found in DOM (may still have been sent)");
    }

    // 7. Screenshot
    const ts = Date.now();
    screenshotPath = path.join("/tmp", `feishu-web-${ts}.png`);
    await page.screenshot({ path: screenshotPath, fullPage: false }).catch(() => {});
    log(`Screenshot: ${screenshotPath}`);

  } catch (err) {
    log("Error:", err.message || err);
    const ts = Date.now();
    screenshotPath = path.join("/tmp", `feishu-web-error-${ts}.png`);
    await page.screenshot({ path: screenshotPath, fullPage: false }).catch(() => {});
  }

  const duration = Date.now() - t0;
  await browser.close();

  const result = {
    ok,
    method: "rpa-playwright-web",
    target,
    text,
    screenshot: screenshotPath,
    duration_ms: duration,
  };
  console.log(JSON.stringify(result, null, 2));
  process.exit(ok ? 0 : 1);
}

main();
