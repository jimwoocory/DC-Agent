#!/usr/bin/env node
/**
 * Snapshot Feishu admin cloud-space rows from a logged-in Chrome DevTools port.
 *
 * The user sorts the admin cloud-space table by Size. This script opens or
 * reuses that page, clicks the Size header until large files are first, then
 * emits the visible rows as JSON. With --write-targets it updates the local
 * watchdog queue while preserving per-target run metadata.
 */

const fs = require("fs");
const http = require("http");
const path = require("path");

const DEFAULT_CDP = "http://127.0.0.1:9223";
const CLOUD_URL = "https://o0ain5w98jh.feishu.cn/admin/drive/cloud-space";
const DEFAULT_TARGETS = "/Users/dianchi/DC-Agent/data/watchdog/feishu_cloud_targets.json";

function argValue(name, fallback = "") {
  const index = process.argv.indexOf(name);
  if (index < 0 || index + 1 >= process.argv.length) return fallback;
  return process.argv[index + 1];
}

function hasArg(name) {
  return process.argv.includes(name);
}

function getText(url) {
  return new Promise((resolve, reject) => {
    http
      .get(url, { agent: false }, (res) => {
        let data = "";
        res.on("data", (chunk) => {
          data += chunk;
        });
        res.on("end", () => resolve(data));
      })
      .on("error", reject);
  });
}

async function getJson(url) {
  return JSON.parse(await getText(url));
}

async function putJson(url) {
  return new Promise((resolve, reject) => {
    const req = http.request(url, { method: "PUT", agent: false }, (res) => {
      let data = "";
      res.on("data", (chunk) => {
        data += chunk;
      });
      res.on("end", () => resolve(JSON.parse(data)));
    });
    req.on("error", reject);
    req.end();
  });
}

async function getCloudPage(cdpBase) {
  let targets = await getJson(`${cdpBase}/json/list`);
  let page = targets.find((item) => item.type === "page" && item.url.includes("/admin/drive/cloud-space"));
  if (page) return page;
  await putJson(`${cdpBase}/json/new?${encodeURIComponent(CLOUD_URL)}`);
  await new Promise((resolve) => setTimeout(resolve, 3000));
  targets = await getJson(`${cdpBase}/json/list`);
  page = targets.find((item) => item.type === "page" && item.url.includes("/admin/drive/cloud-space"));
  if (!page) throw new Error("Feishu admin cloud-space page not found after opening it");
  return page;
}

async function withPage(page, callback) {
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 1;
  const pending = new Map();
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (!msg.id || !pending.has(msg.id)) return;
    const pair = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) pair.reject(new Error(JSON.stringify(msg.error)));
    else pair.resolve(msg.result);
  };
  await new Promise((resolve) => {
    ws.onopen = resolve;
  });

  function cdp(method, params = {}) {
    const callId = id++;
    ws.send(JSON.stringify({ id: callId, method, params }));
    return new Promise((resolve, reject) => pending.set(callId, { resolve, reject }));
  }

  try {
    await cdp("Runtime.enable");
    return await callback(cdp);
  } finally {
    ws.close();
  }
}

async function evaluate(cdp, expression) {
  const result = await cdp("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (result.exceptionDetails) {
    throw new Error(JSON.stringify(result.exceptionDetails));
  }
  return result.result.value;
}

function parseRows(rawRows, rankOffset = 0) {
  return rawRows.map((text, index) => {
    const parts = text
      .split("\n")
      .map((item) => item.trim())
      .filter((item) => item && item !== "\u00a0");
    const idIndex = parts.findIndex((item) => item.startsWith("ID: "));
    if (idIndex < 1 || parts.length < idIndex + 6) return null;
    return {
      size_rank: rankOffset + index + 1,
      title: parts[idIndex - 1],
      token: parts[idIndex].replace(/^ID:\s*/, ""),
      type: parts[idIndex + 1],
      owner: parts[idIndex + 2],
      status: parts[idIndex + 3],
      size: parts[idIndex + 4],
      modified_at: parts[idIndex + 5],
    };
  }).filter(Boolean);
}

function looksSizeDesc(rows) {
  if (!rows.length) return false;
  const first = rows[0].size || "";
  return /GB$/.test(first) && Number(first.split(" ")[0]) > 10;
}

function idFor(row) {
  const hint = row.token.slice(0, 3).toLowerCase();
  return `cloud-space-size-${String(row.size_rank).padStart(4, "0")}-${hint}`;
}

function targetFor(row, previousByToken) {
  const previous = previousByToken.get(row.token) || {};
  return {
    ...previous,
    id: idFor(row),
    url: previous.url || "",
    token: row.token,
    admin_row_id: row.token,
    type_hint: row.type,
    title_hint: row.title,
    owner_hint: row.owner,
    size_rank: row.size_rank,
    size_label: row.size,
    modified_at_label: row.modified_at,
    rank_hint: `admin-cloud-space-size-desc-row-${row.size_rank}`,
    status: previous.status && previous.status !== "pending_url_verification" ? previous.status : "queued",
  };
}

function writeTargets(rows, targetsPath) {
  const existing = fs.existsSync(targetsPath)
    ? JSON.parse(fs.readFileSync(targetsPath, "utf8"))
    : {};
  const previousByToken = new Map((existing.targets || []).map((item) => [item.token, item]));
  const targets = rows.map((row) => targetFor(row, previousByToken));
  const data = {
    version: 1,
    source: {
      kind: "feishu_admin_cloud_space",
      url: CLOUD_URL,
      sorting_hint: "Captured from the logged-in Feishu admin cloud-space page after clicking Size descending.",
    },
    policy: {
      identity_key: "admin_cloud_space_row_id",
      queue_order: "size_desc_from_admin_cloud_space",
      destination_share: "/Users/dianchi/nas_kb",
      archive_strategy: "project_first_then_type; low-confidence owner/initiator goes to projects/_待确认",
      memory_path: "DC-Agent nas_memory.db + harness_memory.db; AstrBot mirroring is optional",
    },
    targets,
    updated_at: new Date().toISOString(),
  };
  fs.mkdirSync(path.dirname(targetsPath), { recursive: true });
  fs.writeFileSync(targetsPath, `${JSON.stringify(data, null, 2)}\n`, "utf8");
  return data;
}

async function main() {
  const cdpBase = argValue("--cdp", DEFAULT_CDP).replace(/\/$/, "");
  const limit = Number(argValue("--limit", "20"));
  const pages = Number(argValue("--pages", "1"));
  const targetsPath = argValue("--targets", DEFAULT_TARGETS);
  const page = await getCloudPage(cdpBase);
  const rows = await withPage(page, async (cdp) => {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    await evaluate(cdp, `(() => {
      const first = Array.from(document.querySelectorAll('li'))
        .find((el) => /ud__pagination-item-1/.test(el.className || '') && !/active/.test(el.className || ''));
      if (!first) return false;
      first.click();
      return true;
    })()`);
    await new Promise((resolve) => setTimeout(resolve, 1500));

    const extractVisibleRows = async (rankOffset) => {
      const rawRows = await evaluate(cdp, `(() => Array.from(document.querySelectorAll('tr, [role="row"]'))
        .map((item) => item.innerText)
        .filter((text) => /ID:/.test(text))
        .slice(0, ${limit}))()`);
      return parseRows(rawRows, rankOffset);
    };

    for (let attempt = 0; attempt < 3; attempt += 1) {
      const parsed = await extractVisibleRows(0);
      if (looksSizeDesc(parsed)) {
        break;
      }
      await evaluate(cdp, `(() => {
        const th = Array.from(document.querySelectorAll('th')).find((item) => item.innerText.trim() === '大小');
        if (th) th.click();
        return Boolean(th);
      })()`);
      await new Promise((resolve) => setTimeout(resolve, 2500));
    }

    const allRows = [];
    const seenTokens = new Set();
    for (let pageIndex = 0; pageIndex < pages; pageIndex += 1) {
      const parsed = await extractVisibleRows(allRows.length);
      for (const row of parsed) {
        if (!row.token || seenTokens.has(row.token)) continue;
        seenTokens.add(row.token);
        allRows.push(row);
      }
      if (pageIndex + 1 >= pages) break;
      const clicked = await evaluate(cdp, `(() => {
        const candidates = Array.from(document.querySelectorAll('li, button, [role="button"]'));
        const visible = (el) => {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
        };
        const disabled = (el) => el.disabled || el.getAttribute('aria-disabled') === 'true' || /disabled/.test(el.className || '');
        const next = candidates.find((el) => visible(el) && !disabled(el) && (
          /ud__pagination-next/.test(el.className || '') ||
          /下一页|下页|next/i.test(el.innerText || '') ||
          /next/i.test(el.getAttribute('aria-label') || '') ||
          /right|next/i.test(el.className || '')
        ));
        if (!next) return false;
        (next.querySelector('button') || next).click();
        return true;
      })()`);
      if (!clicked) break;
      await new Promise((resolve) => setTimeout(resolve, 2500));
    }
    return allRows;
  });

  const output = hasArg("--write-targets") ? writeTargets(rows, targetsPath) : { rows };
  console.log(JSON.stringify(output, null, 2));
}

main().catch((error) => {
  console.error(error.stack || String(error));
  process.exit(1);
});
