import path from "node:path";
import os from "node:os";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const repoRoot = path.resolve(path.dirname(__filename), "..");
const profileDir = path.join(repoRoot, "data", "temp", "playwright", "dreamina-profile");
const targetUrl = "https://jimeng.jianying.com/ai-tool/home/?type=audio";
const testText = process.argv.slice(2).join(" ") || "这是一条 DC-Agent 后台配音链路测试文案。";
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
  const relevantRequests = [];
  page.on("request", (request) => {
    const url = request.url();
    if (
      url.includes("jimeng.jianying.com")
      && /audio|voice|sound|tts|dub|lip|skill|generate/i.test(url)
    ) {
      relevantRequests.push({ method: request.method(), url });
    }
  });

  await page.goto(targetUrl, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(7000);

  const textInputSelector = [
    '[contenteditable="true"]:visible',
    "textarea:visible",
    'input:not([type="file"]):visible',
  ].join(", ");
  const editable = page.locator(textInputSelector).first();
  const textboxCount = await page.locator(textInputSelector).count();
  let filled = false;
  if (textboxCount > 0) {
    await editable.click({ timeout: 10000 });
    await page.keyboard.type(testText);
    await page.waitForTimeout(2000);
    filled = true;
  }

  const bodyText = await page.locator("body").innerText({ timeout: 10000 });
  const editables = await page.locator('[contenteditable="true"], textarea, input').evaluateAll((nodes) => (
    nodes.map((node) => {
      const rect = node.getBoundingClientRect();
      return {
        tag: node.tagName.toLowerCase(),
        type: node.getAttribute("type") || "",
        placeholder: node.getAttribute("placeholder") || "",
        ariaLabel: node.getAttribute("aria-label") || "",
        text: (node.innerText || node.value || "").trim(),
        visible: rect.width > 0 && rect.height > 0,
        rect: {
          x: Math.round(rect.x),
          y: Math.round(rect.y),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        },
      };
    }).filter((item) => item.visible).slice(0, 20)
  ));
  const buttons = await page.locator("button").evaluateAll((nodes) => (
    nodes.map((button) => ({
      text: (button.innerText || button.getAttribute("aria-label") || "").trim(),
      disabled: button.disabled || button.getAttribute("aria-disabled") === "true",
      rect: (() => {
        const rect = button.getBoundingClientRect();
        return {
          x: Math.round(rect.x),
          y: Math.round(rect.y),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        };
      })(),
    })).filter((button) => button.text || button.disabled).slice(0, 40)
  ));
  const submitButton = buttons.find((button) => /生成|开始|提交|发送/.test(button.text));

  console.log(JSON.stringify({
    url: page.url(),
    hasLoginEntry: bodyText.includes("登录"),
    hasAudioMode: bodyText.includes("配音生成"),
    textboxCount,
    filled,
    editables,
    submitButton: submitButton ?? null,
    buttons,
    visibleHints: bodyText
      .split("\n")
      .filter((line) => /配音|音色|生成|输入|登录|文案|旁白|声音/.test(line))
      .slice(0, 30),
    relevantRequests: relevantRequests.slice(-20),
  }, null, 2));
} finally {
  await context.close();
}
