# OpenClaw AstrBot Hermes Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an engineering-grade full-stack control plane where the OpenClaw WebUI can observe and safely control AstrBot, Hermes, and OpenClaw from one console.

**Architecture:** Keep the browser single-origin against OpenClaw Control Center on `:4312`. Add a Node backend integration gateway inside `openclaw-control-center` that talks to AstrBot `:6185`, OpenClaw on-demand manager `:9120`, Hermes Gateway `:8644`, Hermes response bridge `:8645`, and Hermes WebUIs `:9119/:8787`. Mutations are protected by the existing local-token gate and wrapped in explicit service methods with audit logs.

**Tech Stack:** TypeScript, Node `http`, existing OpenClaw server renderer, AstrBot Python plugins, Hermes Bridge HTTP webhooks, local shell scripts for service control, Vitest/Node test runner via existing `npm test`.

---

## Current State

- OpenClaw Control Center now lives at `/Users/dianchi/DC-Agent/openclaw-control-center`.
- AstrBot starts OpenClaw through `/Users/dianchi/DC-Agent/data/plugins/openclaw_on_demand/main.py` on `127.0.0.1:9120`.
- AstrBot dashboard runs on `127.0.0.1:6185`.
- Hermes bridge config lives at `/Users/dianchi/DC-Agent/data/config/hermes_bridge_config.json`.
- System entry config lives at `/Users/dianchi/DC-Agent/data/config/system_entries_config.json`.
- OpenClaw Control Center currently has no first-class AstrBot/Hermes integration APIs.

## Design Principles

- Browser never calls AstrBot or Hermes directly.
- OpenClaw backend owns all integration calls and normalizes responses.
- Read endpoints are safe without mutation privileges; write endpoints require the existing `x-local-token` / bearer token gate.
- No raw secrets are returned to the UI.
- Every mutation writes to the existing operation audit timeline.
- Integration status is degraded-but-visible: one failed subsystem must not blank the entire console.

## File Structure

- Create `/Users/dianchi/DC-Agent/openclaw-control-center/src/integrations/types.ts`
  - Shared status, service, action, and health result types.
- Create `/Users/dianchi/DC-Agent/openclaw-control-center/src/integrations/http-probe.ts`
  - Timeout-safe HTTP probe helper.
- Create `/Users/dianchi/DC-Agent/openclaw-control-center/src/integrations/astrbot-client.ts`
  - AstrBot status, dashboard probe, and optional restart hooks.
- Create `/Users/dianchi/DC-Agent/openclaw-control-center/src/integrations/hermes-client.ts`
  - Hermes gateway, response bridge, official WebUI, third-party WebUI, and bridge config status.
- Create `/Users/dianchi/DC-Agent/openclaw-control-center/src/integrations/openclaw-on-demand-client.ts`
  - OpenClaw on-demand manager status, kick, and stop calls.
- Create `/Users/dianchi/DC-Agent/openclaw-control-center/src/integrations/control-plane.ts`
  - Aggregates AstrBot/Hermes/OpenClaw into one read model and action dispatcher.
- Modify `/Users/dianchi/DC-Agent/openclaw-control-center/src/config.ts`
  - Add env-configurable integration URLs and DC-Agent root.
- Modify `/Users/dianchi/DC-Agent/openclaw-control-center/src/ui/server.ts`
  - Add `/api/control-plane/*` routes.
- Modify `/Users/dianchi/DC-Agent/openclaw-control-center/src/ui/server.ts` or split UI into a new renderer file if needed.
  - Add “系统控制台” section with Chinese labels and actionable controls.
- Modify `/Users/dianchi/DC-Agent/data/plugins/system_entries/dc-dashboard-quick-entries.js`
  - Make the AstrBot floating nav open the OpenClaw console section with `?section=control-plane`.
- Test files:
  - `/Users/dianchi/DC-Agent/openclaw-control-center/test/control-plane-api.test.ts`
  - `/Users/dianchi/DC-Agent/openclaw-control-center/test/control-plane-clients.test.ts`
  - `/Users/dianchi/DC-Agent/tests/test_dashboard_quick_entries.py`

---

### Task 1: Add Integration Configuration

**Files:**
- Modify: `/Users/dianchi/DC-Agent/openclaw-control-center/src/config.ts`
- Test: `/Users/dianchi/DC-Agent/openclaw-control-center/test/control-plane-clients.test.ts`

- [ ] **Step 1: Write failing config test**

```ts
import assert from "node:assert/strict";
import { test } from "node:test";

test("control plane config exposes local integration defaults", async () => {
  const config = await import("../src/config");
  assert.equal(config.ASTRBOT_BASE_URL, "http://127.0.0.1:6185");
  assert.equal(config.OPENCLAW_ON_DEMAND_BASE_URL, "http://127.0.0.1:9120");
  assert.equal(config.HERMES_GATEWAY_BASE_URL, "http://127.0.0.1:8644");
  assert.equal(config.HERMES_RESPONSE_BASE_URL, "http://127.0.0.1:8645");
  assert.equal(config.HERMES_OFFICIAL_WEBUI_URL, "http://127.0.0.1:9119");
  assert.equal(config.HERMES_THIRD_PARTY_WEBUI_URL, "http://127.0.0.1:8787");
  assert.equal(config.DC_AGENT_ROOT, "/Users/dianchi/DC-Agent");
});
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
cd /Users/dianchi/DC-Agent/openclaw-control-center
npm test -- test/control-plane-clients.test.ts
```

Expected: FAIL because config exports do not exist.

- [ ] **Step 3: Add config exports**

Add to `/Users/dianchi/DC-Agent/openclaw-control-center/src/config.ts`:

```ts
export const DC_AGENT_ROOT = readStringEnv(process.env.DC_AGENT_ROOT, "/Users/dianchi/DC-Agent");
export const ASTRBOT_BASE_URL = readStringEnv(process.env.ASTRBOT_BASE_URL, "http://127.0.0.1:6185");
export const OPENCLAW_ON_DEMAND_BASE_URL = readStringEnv(
  process.env.OPENCLAW_ON_DEMAND_BASE_URL,
  "http://127.0.0.1:9120",
);
export const HERMES_GATEWAY_BASE_URL = readStringEnv(
  process.env.HERMES_GATEWAY_BASE_URL,
  "http://127.0.0.1:8644",
);
export const HERMES_RESPONSE_BASE_URL = readStringEnv(
  process.env.HERMES_RESPONSE_BASE_URL,
  "http://127.0.0.1:8645",
);
export const HERMES_OFFICIAL_WEBUI_URL = readStringEnv(
  process.env.HERMES_OFFICIAL_WEBUI_URL,
  "http://127.0.0.1:9119",
);
export const HERMES_THIRD_PARTY_WEBUI_URL = readStringEnv(
  process.env.HERMES_THIRD_PARTY_WEBUI_URL,
  "http://127.0.0.1:8787",
);
```

- [ ] **Step 4: Run test and build**

Run:

```bash
cd /Users/dianchi/DC-Agent/openclaw-control-center
npm test -- test/control-plane-clients.test.ts
npm run build
```

Expected: PASS and TypeScript build succeeds.

---

### Task 2: Add Typed Integration Clients

**Files:**
- Create: `/Users/dianchi/DC-Agent/openclaw-control-center/src/integrations/types.ts`
- Create: `/Users/dianchi/DC-Agent/openclaw-control-center/src/integrations/http-probe.ts`
- Create: `/Users/dianchi/DC-Agent/openclaw-control-center/src/integrations/astrbot-client.ts`
- Create: `/Users/dianchi/DC-Agent/openclaw-control-center/src/integrations/hermes-client.ts`
- Create: `/Users/dianchi/DC-Agent/openclaw-control-center/src/integrations/openclaw-on-demand-client.ts`
- Test: `/Users/dianchi/DC-Agent/openclaw-control-center/test/control-plane-clients.test.ts`

- [ ] **Step 1: Add shared types**

```ts
export type IntegrationState = "online" | "offline" | "degraded" | "unknown";

export type IntegrationHealth = {
  id: string;
  name: string;
  state: IntegrationState;
  url?: string;
  checkedAt: string;
  latencyMs?: number;
  detail: string;
};

export type OpenClawOnDemandStatus = {
  kickPort: number;
  openclawPort: number;
  openclawPid: number | null;
  openclawListening: boolean;
  idleSeconds: number | null;
  idleTimeoutSeconds: number;
};
```

- [ ] **Step 2: Add timeout-safe probe helper**

```ts
export async function probeHttp(
  url: string,
  options: { method?: string; timeoutMs?: number } = {},
): Promise<{ ok: boolean; status?: number; latencyMs: number; error?: string }> {
  const startedAt = Date.now();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeoutMs ?? 2500);
  try {
    const response = await fetch(url, {
      method: options.method ?? "GET",
      signal: controller.signal,
    });
    return { ok: response.ok, status: response.status, latencyMs: Date.now() - startedAt };
  } catch (error) {
    return {
      ok: false,
      latencyMs: Date.now() - startedAt,
      error: error instanceof Error ? error.message : "unknown error",
    };
  } finally {
    clearTimeout(timeout);
  }
}
```

- [ ] **Step 3: Add client tests with local fake server**

```ts
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { test } from "node:test";
import { probeHttp } from "../src/integrations/http-probe";

test("probeHttp returns online status for HTTP 200", async () => {
  const server = createServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true }));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.equal(typeof address, "object");
  const result = await probeHttp(`http://127.0.0.1:${address!.port}/health`);
  server.close();
  assert.equal(result.ok, true);
  assert.equal(result.status, 200);
});
```

- [ ] **Step 4: Implement AstrBot client**

```ts
import type { IntegrationHealth } from "./types";
import { probeHttp } from "./http-probe";

export async function readAstrBotHealth(baseUrl: string): Promise<IntegrationHealth> {
  const url = `${baseUrl.replace(/\/$/, "")}/api/stat/start-time`;
  const probe = await probeHttp(url);
  return {
    id: "astrbot",
    name: "AstrBot",
    state: probe.ok ? "online" : "offline",
    url: baseUrl,
    checkedAt: new Date().toISOString(),
    latencyMs: probe.latencyMs,
    detail: probe.ok ? "AstrBot API 正常" : `AstrBot API 不可用：${probe.error ?? probe.status ?? "unknown"}`,
  };
}
```

- [ ] **Step 5: Implement OpenClaw on-demand client**

```ts
import type { IntegrationHealth, OpenClawOnDemandStatus } from "./types";

export async function readOpenClawOnDemandStatus(baseUrl: string): Promise<OpenClawOnDemandStatus> {
  const response = await fetch(`${baseUrl.replace(/\/$/, "")}/status`);
  if (!response.ok) throw new Error(`OpenClaw on-demand status failed: ${response.status}`);
  const body = await response.json() as {
    kick_port: number;
    openclaw_port: number;
    openclaw_pid: number | null;
    openclaw_listening: boolean;
    idle_seconds?: number;
    idle_timeout_seconds: number;
  };
  return {
    kickPort: body.kick_port,
    openclawPort: body.openclaw_port,
    openclawPid: body.openclaw_pid,
    openclawListening: body.openclaw_listening,
    idleSeconds: body.idle_seconds ?? null,
    idleTimeoutSeconds: body.idle_timeout_seconds,
  };
}

export async function readOpenClawOnDemandHealth(baseUrl: string): Promise<IntegrationHealth> {
  try {
    const status = await readOpenClawOnDemandStatus(baseUrl);
    return {
      id: "openclaw-on-demand",
      name: "OpenClaw 按需管理器",
      state: status.openclawListening ? "online" : "degraded",
      url: baseUrl,
      checkedAt: new Date().toISOString(),
      detail: status.openclawListening ? "OpenClaw WebUI 已启动" : "按需管理器在线，WebUI 当前未启动",
    };
  } catch (error) {
    return {
      id: "openclaw-on-demand",
      name: "OpenClaw 按需管理器",
      state: "offline",
      url: baseUrl,
      checkedAt: new Date().toISOString(),
      detail: error instanceof Error ? error.message : "OpenClaw 按需管理器不可用",
    };
  }
}
```

- [ ] **Step 6: Implement Hermes client**

```ts
import { readFile } from "node:fs/promises";
import type { IntegrationHealth } from "./types";
import { probeHttp } from "./http-probe";

export async function readHermesHealth(input: {
  gatewayBaseUrl: string;
  responseBaseUrl: string;
  officialWebuiUrl: string;
  thirdPartyWebuiUrl: string;
  bridgeConfigPath: string;
}): Promise<IntegrationHealth[]> {
  const [gateway, response, official, thirdParty, config] = await Promise.all([
    probeHttp(`${input.gatewayBaseUrl.replace(/\/$/, "")}/health`, { timeoutMs: 2000 }),
    probeHttp(`${input.responseBaseUrl.replace(/\/$/, "")}/`, { timeoutMs: 2000 }),
    probeHttp(input.officialWebuiUrl, { timeoutMs: 2000 }),
    probeHttp(input.thirdPartyWebuiUrl, { timeoutMs: 2000 }),
    readBridgeConfigSummary(input.bridgeConfigPath),
  ]);

  return [
    toHealth("hermes-gateway", "Hermes Gateway", input.gatewayBaseUrl, gateway, "Hermes webhook 网关"),
    toHealth("hermes-response", "Hermes → AstrBot 回调", input.responseBaseUrl, response, "AstrBot 回调通道"),
    toHealth("hermes-official-webui", "Hermes 官方 WebUI", input.officialWebuiUrl, official, "官方 WebUI"),
    toHealth("hermes-third-party-webui", "Hermes 第三方 WebUI", input.thirdPartyWebuiUrl, thirdParty, "第三方 WebUI"),
    {
      id: "hermes-bridge-config",
      name: "Hermes Bridge 配置",
      state: config.ok ? "online" : "degraded",
      checkedAt: new Date().toISOString(),
      detail: config.detail,
    },
  ];
}

async function readBridgeConfigSummary(path: string): Promise<{ ok: boolean; detail: string }> {
  try {
    const raw = await readFile(path, "utf8");
    const body = JSON.parse(raw.replace(/^\uFEFF/, "")) as Record<string, unknown>;
    const hasTaskWebhook = typeof body.task_webhook_url === "string" && body.task_webhook_url !== "";
    const hasResponsePort = typeof body.response_port === "number";
    return {
      ok: hasTaskWebhook && hasResponsePort,
      detail: hasTaskWebhook && hasResponsePort ? "桥接配置可读" : "桥接配置缺少 task_webhook_url 或 response_port",
    };
  } catch (error) {
    return { ok: false, detail: error instanceof Error ? error.message : "桥接配置不可读" };
  }
}

function toHealth(
  id: string,
  name: string,
  url: string,
  probe: { ok: boolean; status?: number; latencyMs: number; error?: string },
  label: string,
): IntegrationHealth {
  return {
    id,
    name,
    state: probe.ok ? "online" : "offline",
    url,
    checkedAt: new Date().toISOString(),
    latencyMs: probe.latencyMs,
    detail: probe.ok ? `${label} 正常` : `${label} 不可用：${probe.error ?? probe.status ?? "unknown"}`,
  };
}
```

- [ ] **Step 7: Run tests and build**

Run:

```bash
cd /Users/dianchi/DC-Agent/openclaw-control-center
npm test -- test/control-plane-clients.test.ts
npm run build
```

Expected: PASS.

---

### Task 3: Add Control Plane Aggregator and API Routes

**Files:**
- Create: `/Users/dianchi/DC-Agent/openclaw-control-center/src/integrations/control-plane.ts`
- Modify: `/Users/dianchi/DC-Agent/openclaw-control-center/src/ui/server.ts`
- Test: `/Users/dianchi/DC-Agent/openclaw-control-center/test/control-plane-api.test.ts`

- [ ] **Step 1: Create aggregator**

```ts
import {
  ASTRBOT_BASE_URL,
  DC_AGENT_ROOT,
  HERMES_GATEWAY_BASE_URL,
  HERMES_OFFICIAL_WEBUI_URL,
  HERMES_RESPONSE_BASE_URL,
  HERMES_THIRD_PARTY_WEBUI_URL,
  OPENCLAW_ON_DEMAND_BASE_URL,
} from "../config";
import { appendOperationAudit } from "../runtime/operation-audit";
import { readAstrBotHealth } from "./astrbot-client";
import { readHermesHealth } from "./hermes-client";
import {
  readOpenClawOnDemandHealth,
  readOpenClawOnDemandStatus,
} from "./openclaw-on-demand-client";
import type { IntegrationHealth, OpenClawOnDemandStatus } from "./types";

export type ControlPlaneSnapshot = {
  generatedAt: string;
  services: IntegrationHealth[];
  openclaw: OpenClawOnDemandStatus | null;
};

export async function readControlPlaneSnapshot(): Promise<ControlPlaneSnapshot> {
  const [astrbot, openclawHealth, hermes, openclawStatus] = await Promise.all([
    readAstrBotHealth(ASTRBOT_BASE_URL),
    readOpenClawOnDemandHealth(OPENCLAW_ON_DEMAND_BASE_URL),
    readHermesHealth({
      gatewayBaseUrl: HERMES_GATEWAY_BASE_URL,
      responseBaseUrl: HERMES_RESPONSE_BASE_URL,
      officialWebuiUrl: HERMES_OFFICIAL_WEBUI_URL,
      thirdPartyWebuiUrl: HERMES_THIRD_PARTY_WEBUI_URL,
      bridgeConfigPath: `${DC_AGENT_ROOT}/data/config/hermes_bridge_config.json`,
    }),
    readOpenClawOnDemandStatus(OPENCLAW_ON_DEMAND_BASE_URL).catch(() => null),
  ]);

  return {
    generatedAt: new Date().toISOString(),
    services: [astrbot, openclawHealth, ...hermes],
    openclaw: openclawStatus,
  };
}

export async function runControlPlaneAction(action: "openclaw-kick" | "openclaw-stop"): Promise<{ ok: boolean; detail: string }> {
  const baseUrl = OPENCLAW_ON_DEMAND_BASE_URL.replace(/\/$/, "");
  const path = action === "openclaw-kick" ? "/kick" : "/stop";
  const response = await fetch(`${baseUrl}${path}`, { redirect: "manual" });
  const ok = response.ok || response.status === 302;
  const detail = ok ? `${action} 已执行` : `${action} 失败：HTTP ${response.status}`;
  await appendOperationAudit({
    action,
    source: "control-plane",
    ok,
    requestId: `control-plane-${Date.now()}`,
    detail,
  });
  return { ok, detail };
}
```

- [ ] **Step 2: Add API routes in `startUiServer`**

Add near other `/api/*` route blocks:

```ts
      if (method === "GET" && path === "/api/control-plane/status") {
        sendJson(res, 200, await readControlPlaneSnapshot());
        return;
      }

      if (method === "POST" && path === "/api/control-plane/actions/openclaw-kick") {
        assertMutationAuthorized(req, "/api/control-plane/actions/openclaw-kick");
        sendJson(res, 200, await runControlPlaneAction("openclaw-kick"));
        return;
      }

      if (method === "POST" && path === "/api/control-plane/actions/openclaw-stop") {
        assertMutationAuthorized(req, "/api/control-plane/actions/openclaw-stop");
        sendJson(res, 200, await runControlPlaneAction("openclaw-stop"));
        return;
      }
```

Also import:

```ts
import { readControlPlaneSnapshot, runControlPlaneAction } from "../integrations/control-plane";
```

- [ ] **Step 3: Add API test**

```ts
import assert from "node:assert/strict";
import { test } from "node:test";
import { createToolClient } from "../src/clients/factory";
import { startUiServer } from "../src/ui/server";

test("control plane status route returns a service list", async () => {
  const server = startUiServer(0, createToolClient(), { localTokenAuthRequired: false });
  await new Promise<void>((resolve) => server.once("listening", resolve));
  const address = server.address();
  assert.equal(typeof address, "object");
  const response = await fetch(`http://127.0.0.1:${address!.port}/api/control-plane/status`);
  server.close();
  assert.equal(response.status, 200);
  const body = await response.json() as { services: unknown[] };
  assert.ok(Array.isArray(body.services));
});
```

- [ ] **Step 4: Run tests**

Run:

```bash
cd /Users/dianchi/DC-Agent/openclaw-control-center
npm test -- test/control-plane-api.test.ts
npm run build
```

Expected: PASS.

---

### Task 4: Build Chinese Control Plane UI

**Files:**
- Modify: `/Users/dianchi/DC-Agent/openclaw-control-center/src/ui/server.ts`
- Optional create: `/Users/dianchi/DC-Agent/openclaw-control-center/src/ui/control-plane.ts`
- Test: `/Users/dianchi/DC-Agent/openclaw-control-center/test/ui-render-smoke.test.ts`

- [ ] **Step 1: Add route selector**

Support `?section=control-plane` and render a top-level “系统控制台” view.

- [ ] **Step 2: Render service cards/table in Chinese**

Required text:

```text
系统控制台
服务
状态
地址
说明
操作
刷新
启动 OpenClaw
停止 OpenClaw
AstrBot
Hermes Gateway
Hermes 官方 WebUI
Hermes 第三方 WebUI
OpenClaw 按需管理器
```

- [ ] **Step 3: Add client-side refresh and actions**

Use same-origin fetch only:

```js
async function refreshControlPlane() {
  const response = await fetch("/api/control-plane/status");
  const snapshot = await response.json();
  renderControlPlane(snapshot);
}

async function runControlAction(action) {
  const token = localStorage.getItem("openclawLocalToken") || "";
  const response = await fetch("/api/control-plane/actions/" + action, {
    method: "POST",
    headers: token ? { "x-local-token": token } : {},
  });
  const result = await response.json();
  await refreshControlPlane();
  showToast(result.detail || "操作已执行");
}
```

- [ ] **Step 4: Make unauthenticated mutations visibly disabled**

If no token is set, show buttons disabled with label:

```text
需要本地控制 token
```

- [ ] **Step 5: Run UI smoke test**

Run:

```bash
cd /Users/dianchi/DC-Agent/openclaw-control-center
npm test -- test/ui-render-smoke.test.ts
npm run build
```

Expected: PASS.

---

### Task 5: Wire AstrBot Floating Navigation to the New Control Plane

**Files:**
- Modify: `/Users/dianchi/DC-Agent/data/config/system_entries_config.json`
- Modify: `/Users/dianchi/DC-Agent/data/plugins/system_entries/dc-dashboard-quick-entries.js`
- Test: `/Users/dianchi/DC-Agent/tests/test_dashboard_quick_entries.py`

- [ ] **Step 1: Update OpenClaw entry URL**

Change OpenClaw entry to:

```json
{
  "name": "OpenClaw",
  "url": "http://localhost:4312/?section=control-plane",
  "probe_host": "127.0.0.1",
  "probe_port": 4312,
  "hint": "Watch-Dog / AstrBot / Hermes 统一控制台",
  "icon": "🖥️",
  "on_demand_kick": "http://localhost:9120/kick"
}
```

- [ ] **Step 2: Ensure quick entry uses `on_demand_kick`**

When entry has `on_demand_kick`, the button should open kick URL but pass intended target as:

```text
http://localhost:9120/kick?next=%2F%3Fsection%3Dcontrol-plane
```

If the current plugin does not support `next`, implement it in Task 6.

- [ ] **Step 3: Run dashboard tests**

Run:

```bash
cd /Users/dianchi/DC-Agent
python3 -m pytest tests/test_dashboard_quick_entries.py -q
```

Expected: PASS.

---

### Task 6: Let AstrBot OpenClaw On-Demand Plugin Redirect to a Target Section

**Files:**
- Modify: `/Users/dianchi/DC-Agent/data/plugins/openclaw_on_demand/main.py`
- Test: `/Users/dianchi/DC-Agent/tests/test_dashboard_quick_entries.py`

- [ ] **Step 1: Add `next` query support**

In `_handle_kick`, after OpenClaw is ready:

```py
next_path = request.query.get("next", "/")
if not next_path.startswith("/"):
    next_path = "/"
raise web.HTTPFound(f"http://localhost:{self.openclaw_port}{next_path}")
```

- [ ] **Step 2: Add plugin test or compile check**

Run:

```bash
cd /Users/dianchi/DC-Agent
python3 -m py_compile data/plugins/openclaw_on_demand/main.py
```

Expected: PASS.

- [ ] **Step 3: Restart AstrBot**

Run:

```bash
/Users/dianchi/DC-Agent/scripts-tools/safe_restart.sh astrbot
```

Expected: AstrBot ready.

---

### Task 7: Add Service Control Actions Safely

**Files:**
- Create: `/Users/dianchi/DC-Agent/openclaw-control-center/src/integrations/service-actions.ts`
- Modify: `/Users/dianchi/DC-Agent/openclaw-control-center/src/integrations/control-plane.ts`
- Test: `/Users/dianchi/DC-Agent/openclaw-control-center/test/control-plane-api.test.ts`

- [ ] **Step 1: Implement allowlisted shell action runner**

Only allow these commands:

```ts
export type ServiceAction =
  | "restart-astrbot"
  | "restart-hermes-gateway"
  | "openclaw-kick"
  | "openclaw-stop";
```

Map shell actions to fixed argv arrays, never interpolate user input:

```ts
const ACTIONS: Record<ServiceAction, readonly string[]> = {
  "restart-astrbot": ["/Users/dianchi/DC-Agent/scripts-tools/safe_restart.sh", "astrbot"],
  "restart-hermes-gateway": ["/Users/dianchi/DC-Agent/scripts-tools/safe_restart.sh", "hermes-gateway"],
  "openclaw-kick": [],
  "openclaw-stop": [],
};
```

- [ ] **Step 2: Require mutation auth for all service actions**

Routes:

```text
POST /api/control-plane/actions/restart-astrbot
POST /api/control-plane/actions/restart-hermes-gateway
POST /api/control-plane/actions/openclaw-kick
POST /api/control-plane/actions/openclaw-stop
```

- [ ] **Step 3: Add audit payload**

Every action writes:

```ts
{
  action,
  source: "control-plane",
  ok,
  requestId,
  detail,
}
```

- [ ] **Step 4: Add negative auth test**

With `localTokenAuthRequired: true`, POST without token returns `401`.

---

### Task 8: Add Operational Verification

**Files:**
- Create: `/Users/dianchi/DC-Agent/openclaw-control-center/scripts/control-plane-smoke.ts`
- Modify: `/Users/dianchi/DC-Agent/openclaw-control-center/package.json`

- [ ] **Step 1: Add smoke script**

The script checks:

```text
GET http://127.0.0.1:4312/healthz
GET http://127.0.0.1:4312/api/control-plane/status
GET http://127.0.0.1:9120/status
GET http://127.0.0.1:6185/api/stat/start-time
```

It prints a Chinese summary and exits nonzero only if OpenClaw backend or AstrBot are unavailable.

- [ ] **Step 2: Add npm script**

```json
"smoke:control-plane": "node --import tsx scripts/control-plane-smoke.ts"
```

- [ ] **Step 3: Run final verification**

Run:

```bash
cd /Users/dianchi/DC-Agent/openclaw-control-center
npm run build
npm run smoke:control-plane
```

Expected: build passes and smoke prints service status.

---

## Rollout

1. Implement Tasks 1-4 in OpenClaw Control Center.
2. Run `npm run build`.
3. Implement Tasks 5-6 in AstrBot plugin/config.
4. Restart AstrBot with `/Users/dianchi/DC-Agent/scripts-tools/safe_restart.sh astrbot`.
5. Open `http://localhost:9120/kick?next=%2F%3Fsection%3Dcontrol-plane`.
6. Confirm OpenClaw process cwd is `/Users/dianchi/DC-Agent/openclaw-control-center`.
7. Confirm the AstrBot floating nav opens the Chinese “系统控制台”.
8. Enable service mutation buttons only after token is entered.

## Acceptance Criteria

- AstrBot floating nav has a clear OpenClaw/Watch-Dog control entry.
- Clicking it opens OpenClaw Control Center directly on `?section=control-plane`.
- The console shows AstrBot, Hermes Gateway, Hermes WebUIs, Hermes callback, and OpenClaw on-demand manager in Chinese.
- Read status works without exposing secrets.
- Mutations require local token and write audit events.
- OpenClaw start/stop works from the console.
- AstrBot/Hermes restart actions are allowlisted and never interpolate user input.
- If Hermes is down, the page still renders AstrBot/OpenClaw status.
- `npm run build` passes.
- Python plugin compile check passes.

## Risks

- Hermes Gateway may not expose a stable `/health` endpoint. If so, treat TCP/HTTP root probe as best-effort and label it “端口在线，健康接口未知”.
- Current OpenClaw `server.ts` is large. If UI changes become noisy, split the renderer into `src/ui/control-plane.ts` instead of expanding `server.ts` further.
- Local token naming in the current UI may differ. Confirm existing storage key before wiring buttons.
- The copied OpenClaw repo contains a nested `.git`; do not delete it during this plan unless the user explicitly asks.

## Self-Review

- Spec coverage: The plan covers backend integration, UI, AstrBot plugin wiring, service controls, auth, audit, tests, and rollout.
- Placeholder scan: No task uses “TBD” or unspecified implementation steps.
- Type consistency: `IntegrationHealth`, `OpenClawOnDemandStatus`, and action names are defined before use and reused consistently.
