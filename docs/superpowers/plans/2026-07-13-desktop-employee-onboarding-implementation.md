# Dianchi Desktop Employee Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留现有 Vercel 官网和官方飞书 Messenger 桌面界面的前提下，交付“飞书 OAuth → 公司员工校验 → 已安装直接打开 / 未安装下载 → 一次性授权 → 宠物身份绑定”的可部署闭环。

**Architecture:** DC-Agent 本机员工目录生成带签名的最小授权快照，Vercel `dianchi` 使用 Upstash Redis 持久化并在 OAuth 回调中默认拒绝未授权账号。Vercel 只把 60 秒一次性授权码放入 `dianchi://`，桌面端交换得到短期访问会话、轮换刷新凭证和宠物绑定断言；Windows/macOS 构建由各自平台的 CI 生成，再上传不可变版本到 Vercel Blob。

**Tech Stack:** Python 3.10+、SQLite、Quart、Vercel Python Functions、Upstash Redis REST、PySide6/QtWebEngine、keyring、PyInstaller、Inno Setup、GitHub Actions、Vercel Blob。

---

## Worktree and commit safety

三个仓库当前都包含用户已有的未提交改动。执行每个任务前后都运行：

```bash
git -C <repo> status --short
git -C <repo> diff -- <touched-paths>
git -C <repo> diff --cached --name-only
```

只有全新文件或能确认暂存区只包含本任务新增 hunk 时才提交。已存在且原本脏的文件不得整文件暂存；这些局部修改完成测试后保留在工作区，并在交付时列出。禁止使用 `git reset --hard`、`git checkout --` 或覆盖线上旧文件。

## File map

### `/Users/dianchi/DC-Agent`

- `harness/contracts/desktop_employee_onboarding.json`：端到端验收契约。
- `tests/harness/test_desktop_employee_onboarding_contract.py`：校验契约结构和命令。
- `tests/test_desktop_employee_identity_policy.py`：锁定杨国民与蔡挺的身份分层。
- `data/config/employee_identity_overrides.json`：修正蔡挺为 `manager`，保留系统管理员事实。
- `scripts-tools/employee_auth_sync.py`：读取本地 SQLite、生成快照、HMAC 签名并推送 Vercel。
- `tests/scripts_tools/test_employee_auth_sync.py`：快照、排序、签名和请求测试。
- `astrbot/dashboard/routes/pet_live.py`：验证短时桌面绑定断言并使用稳定 `employee_id`。
- `tests/test_pet_live_route.py`：绑定断言、过期和身份接管回归。

### `/Users/dianchi/dianchi`

- `api/_kv.py`：增加严格持久化、TTL 和原子 `GETDEL` 操作。
- `api/_employee_access.py`：授权快照解析和公司员工判定。
- `api/internal/employee_access/sync.py`：接收 DC-Agent 签名快照。
- `api/auth/feishu/callback.py`、`api/_admin.py`、`api/me.py`：局部替换 SQLite 授权路径。
- `api/_desktop_auth.py`：一次性码、访问会话和刷新凭证。
- `api/desktop/open.py`、`exchange.py`、`refresh.py`：桌面授权 API。
- `api/_desktop_probe.py`、`api/desktop/probe/start.py`、`heartbeat.py`、`status.py`：协议探测。
- `api/_desktop_release.py`、`api/desktop/latest.py`、`api/internal/desktop/release.py`：安装包版本清单。
- `desktop.html`：复用现有页面，增加探测、下载和状态机。
- `tests/`：纯 Python 单元和 HTTP handler 测试。

### `/Users/dianchi/dianchi-desktop`

- `src/desktop_session.py`：解析 action/code，使用系统凭据库存储会话。
- `src/api_client.py`：交换/刷新授权、探测心跳、独立宠物 API 地址。
- `src/app.py`、`src/main_window.py`：处理 `probe` 和 `authorize` 深链。
- `src/config.py`、`requirements.txt`：会话、发布与平台配置。
- `tests/test_desktop_session.py`、`tests/test_desktop_onboarding.py`：桌面授权回归。
- `DianchiDesktopAssistant.spec`：PyInstaller 跨平台入口。
- `installer/windows/DianchiDesktopAssistant.iss`：Windows 每用户安装器和 URL protocol。
- `scripts/build_windows.ps1`、`scripts/build_macos.sh`：平台构建脚本。
- `scripts/upload_release.mjs`、`package.json`、`package-lock.json`：上传 Vercel Blob 并发布清单。
- `.github/workflows/desktop-release.yml`：Windows/macOS 构建和灰度发布。
- `tests/test_packaging_contract.py`：安装器静态契约。

## Task 1: Lock the employee identity and onboarding contract

**Files:**
- Create: `/Users/dianchi/DC-Agent/harness/contracts/desktop_employee_onboarding.json`
- Create: `/Users/dianchi/DC-Agent/tests/harness/test_desktop_employee_onboarding_contract.py`
- Create: `/Users/dianchi/DC-Agent/tests/test_desktop_employee_identity_policy.py`
- Modify: `/Users/dianchi/DC-Agent/data/config/employee_identity_overrides.json`

- [ ] **Step 1: Write the failing identity and contract tests**

```python
def test_company_has_one_boss_and_cai_ting_is_manager_admin() -> None:
    overrides = json.loads(OVERRIDES.read_text(encoding="utf-8"))["employees"]
    by_name = {item["display_name"]: item for item in overrides}
    assert [item["display_name"] for item in overrides if item["relation_type"] == "boss"] == ["杨国民"]
    assert by_name["蔡挺"]["relation_type"] == "manager"
    assert by_name["蔡挺"]["department"] == "AI应用部"
    assert by_name["蔡挺"]["role"] == "AI应用部门负责人"
    assert "system_role" not in by_name["蔡挺"]
```

The override file is the organization-identity layer and therefore must not carry
the system permission field. The one-time SQLite verification in Step 4 proves
that the separate `employee_accounts.role` remains `admin`; the existing
`organization_permission_model` contract continues to govern `admins_id`.

The contract test must assert that the contract contains acceptance IDs for employee gating, one-time handoff, installer detection, release integrity, pet identity, and cross-platform packaging, and that every verification command is non-empty.

- [ ] **Step 2: Run the tests and verify the identity test fails**

Run:

```bash
uv run pytest tests/test_desktop_employee_identity_policy.py tests/harness/test_desktop_employee_onboarding_contract.py -q
```

Expected: FAIL because 蔡挺 is currently `boss`, and because the new contract does not exist.

- [ ] **Step 3: Add the minimal contract and correct the override**

The 蔡挺 record must become:

```json
{
  "open_id": "ou_129defaa7d62fdb15ffc1eab436791d6",
  "display_name": "蔡挺",
  "relation_type": "manager",
  "department": "AI应用部",
  "role": "AI应用部门负责人",
  "preferred_address": "蔡挺",
  "honorific_policy": "formal",
  "is_system_tester": true,
  "chat_ids": ["oc_74d80810c33223bc9115b9d36db16543"],
  "communication_style": "AI应用部门负责人兼系统超级管理员，组织身份与系统权限分开判断，直接称呼蔡挺即可。"
}
```

The contract must point to the exact tests added by the following tasks and preserve the existing `organization_permission_model`, `feishu_desktop_messenger`, and `pet_live_system` contracts.

- [ ] **Step 4: Update the current local SQLite facts once**

Run the scoped statements after copying `data/employees.db` to `/private/tmp/employees.db.before-desktop-onboarding`:

```sql
UPDATE employees
SET relation_type='manager', department='AI应用部', role='AI应用部门负责人', preferred_address='蔡挺', honorific_policy='formal'
WHERE open_id='ou_129defaa7d62fdb15ffc1eab436791d6';

UPDATE employee_accounts
SET relation_type='manager', department='AI应用部', title='AI应用部门负责人', role='admin', updated_at=datetime('now')
WHERE employee_id='emp_c2cd707125e2';
```

Verify with a read-only query that only 杨国民 remains `boss` and 蔡挺 is `manager/admin`.

- [ ] **Step 5: Run focused tests and safely commit new files**

Run:

```bash
uv run pytest tests/test_desktop_employee_identity_policy.py tests/harness/test_desktop_employee_onboarding_contract.py dc_engines/tests/test_org_permissions.py -q
```

Expected: PASS. Commit only new test/contract files if the existing config file cannot be safely staged without unrelated hunks:

```bash
git add harness/contracts/desktop_employee_onboarding.json tests/harness/test_desktop_employee_onboarding_contract.py tests/test_desktop_employee_identity_policy.py
git commit -m "test: define desktop employee onboarding contract"
```

## Task 2: Export and sign the DC-Agent employee authorization snapshot

**Files:**
- Create: `/Users/dianchi/DC-Agent/scripts-tools/employee_auth_sync.py`
- Create: `/Users/dianchi/DC-Agent/tests/scripts_tools/test_employee_auth_sync.py`

- [ ] **Step 1: Write failing snapshot tests**

Create a temporary SQLite database with `employee_accounts` and `employee_identities`. Assert that `build_snapshot()`:

```python
snapshot = build_snapshot(db_path, generated_at="2026-07-13T10:00:00+00:00")
assert snapshot["schema_version"] == 1
assert snapshot["generated_at"] == "2026-07-13T10:00:00+00:00"
assert snapshot["records"][0]["employee_id"] == "emp_active"
assert snapshot["records"][0]["status"] == "active"
assert "email" not in snapshot["records"][0]
assert snapshot["digest"] == snapshot_digest(snapshot["records"])
assert sign_snapshot(snapshot, "sync-secret") == sign_snapshot(snapshot, "sync-secret")
```

Also assert records are sorted by `employee_id`, only documented fields are exported, and a missing identity table fails with a clear `RuntimeError`.

- [ ] **Step 2: Verify the test fails**

Run:

```bash
uv run pytest tests/scripts_tools/test_employee_auth_sync.py -q
```

Expected: FAIL with `ModuleNotFoundError` for `employee_auth_sync`.

- [ ] **Step 3: Implement the exporter and signed POST**

Implement these public functions with Google-style docstrings:

```python
def build_snapshot(db_path: Path, *, generated_at: str | None = None) -> dict:
    """Build a deterministic employee authorization snapshot.

    Args:
        db_path: SQLite employee directory path.
        generated_at: Optional fixed UTC timestamp for deterministic tests.

    Returns:
        Versioned snapshot containing sorted authorization records.

    Raises:
        RuntimeError: If the required identity tables are unavailable.
    """

def snapshot_digest(records: list[dict]) -> str:
    """Return the SHA-256 digest of canonical JSON records."""

def sign_snapshot(snapshot: dict, secret: str) -> str:
    """Return a hex HMAC-SHA256 signature for canonical snapshot JSON."""

def push_snapshot(endpoint: str, snapshot: dict, secret: str) -> dict:
    """POST the signed snapshot to the Vercel internal sync endpoint."""
```

The CLI accepts `--db`, `--endpoint`, and environment variable `DESKTOP_EMPLOYEE_SYNC_SECRET`; it prints only version, count, digest, and server result, never employee identifiers or the secret.

- [ ] **Step 4: Run tests and a dry local snapshot**

Run:

```bash
uv run pytest tests/scripts_tools/test_employee_auth_sync.py -q
uv run python scripts-tools/employee_auth_sync.py --db data/employees.db --dry-run
```

Expected: tests PASS; dry run reports 27 records without changing Vercel.

- [ ] **Step 5: Commit**

```bash
git add scripts-tools/employee_auth_sync.py tests/scripts_tools/test_employee_auth_sync.py
git commit -m "feat: export employee authorization snapshot"
```

## Task 3: Add strict Upstash primitives and the Vercel authorization mirror

**Files:**
- Modify: `/Users/dianchi/dianchi/api/_kv.py`
- Create: `/Users/dianchi/dianchi/api/_employee_access.py`
- Create: `/Users/dianchi/dianchi/api/internal/employee_access/sync.py`
- Create: `/Users/dianchi/dianchi/tests/test_employee_access.py`

- [ ] **Step 1: Write failing mirror tests**

Tests must cover:

```python
result = resolve_employee(
    {"tenant_key": "tenant_company", "user_id": "u_1", "union_id": "", "open_id": "ou_1"},
    snapshot=active_snapshot,
    expected_tenant="tenant_company",
)
assert result["allowed"] is True
assert result["employee"]["employee_id"] == "emp_1"

assert resolve_employee(external_user, snapshot=active_snapshot, expected_tenant="tenant_company")["reason"] == "tenant_mismatch"
assert resolve_employee(unknown_user, snapshot=active_snapshot, expected_tenant="tenant_company")["reason"] == "employee_not_found"
assert resolve_employee(pending_user, snapshot=pending_snapshot, expected_tenant="tenant_company")["reason"] == "status_pending"
```

Add tests for HMAC mismatch, old snapshot version, snapshot age over 24 hours, production without persistent KV, TTL SET, and one-use `GETDEL`.

- [ ] **Step 2: Verify tests fail**

Run from `/Users/dianchi/dianchi`:

```bash
python3 -m unittest tests.test_employee_access -v
```

Expected: FAIL because `_employee_access` and strict KV operations do not exist.

- [ ] **Step 3: Extend `_kv.py` without breaking current wallet callers**

Keep existing defaults and add explicit strict options:

```python
def set_value(key: str, value: str, *, ttl_seconds: int | None = None, strict: bool = False) -> None:
    command = ["SET", key, value]
    if ttl_seconds is not None:
        command.extend(["EX", str(ttl_seconds)])
    # strict=True raises instead of falling back to process memory.

def get_value(key: str, default: Any = None, *, strict: bool = False) -> Any:
    # strict=True raises when Redis is unavailable.

def getdel_value(key: str, default: Any = None, *, strict: bool = False) -> Any:
    # Use Redis GETDEL and an atomic pop in _MemoryStore.
```

Wallet and message code continues using `strict=False`. Employee authorization, handoff codes, probes and refresh tokens use `strict=True` in Vercel production.

- [ ] **Step 4: Implement snapshot resolution and the internal sync handler**

`_employee_access.py` must expose:

```python
SNAPSHOT_KEY = "dianchi:employee-access:snapshot"
MAX_SNAPSHOT_AGE_SECONDS = 24 * 60 * 60

def resolve_employee(user: dict, *, snapshot: dict | None = None, expected_tenant: str | None = None) -> dict:
    """Resolve one Feishu OAuth identity against the read-only employee mirror."""

def store_snapshot(snapshot: dict) -> None:
    """Atomically replace the authorization snapshot after validation."""
```

The sync handler reads the raw request body, verifies headers `X-Dianchi-Timestamp` and `X-Dianchi-Signature` with `DESKTOP_EMPLOYEE_SYNC_SECRET`, rejects timestamps outside five minutes, validates digest/schema/version, then stores the full JSON snapshot under one Redis key. It returns `{ok, version, count, digest}` only.

- [ ] **Step 5: Run tests and commit only safe files**

```bash
python3 -m unittest tests.test_employee_access -v
```

Expected: PASS. Commit new files; leave `_kv.py` unstaged if it contains unrelated prior work.

## Task 4: Gate the existing OAuth callback without rebuilding the homepage

**Files:**
- Modify: `/Users/dianchi/dianchi/api/auth/feishu/callback.py`
- Modify: `/Users/dianchi/dianchi/api/_admin.py`
- Modify: `/Users/dianchi/dianchi/api/me.py`
- Modify: `/Users/dianchi/dianchi/index.html`
- Create: `/Users/dianchi/dianchi/tests/test_oauth_employee_gate.py`

- [ ] **Step 1: Write failing OAuth decision tests**

Patch Feishu exchange/user-info calls and the mirror resolver. Assert:

```python
assert active_response.status == 302
assert active_response.headers["Location"] == "/desktop.html"
assert "dc_feishu_session=" in active_response.headers.get_all("Set-Cookie")

assert outsider_response.status == 403
assert "不是公司内部员工" in outsider_response.body.decode("utf-8")
assert "dc_feishu_session=" not in outsider_response.headers.get_all("Set-Cookie")
```

Add equivalent assertions for `pending`, `disabled`, tenant mismatch, missing snapshot, and a valid existing session whose employee has since been disabled.

- [ ] **Step 2: Run and confirm the old behavior fails**

```bash
python3 -m unittest tests.test_oauth_employee_gate -v
```

Expected: FAIL because the callback still opens local SQLite and signs a pending session.

- [ ] **Step 3: Replace only the authorization decision**

In the existing callback keep state verification, token exchange, safe `next`, cookie flags and HTML style. Replace `resolve_feishu_user(user)` with `resolve_employee(user)`. Before signing a session:

```python
if not auth["allowed"]:
    self.send_header("Set-Cookie", clear_cookie_header(STATE_COOKIE))
    render_message(
        self,
        "员工身份未通过",
        denial_message(auth["reason"]),
        status=HTTPStatus.FORBIDDEN,
    )
    return
```

Do not auto-create a pending account. `_admin.resolve_auth()` must re-resolve the signed user against the current mirror before granting `allowed=True`. `/api/me` keeps the existing shape and returns the mirror-backed `allowed`, `auth_status`, `employee`, and `identity` fields.

- [ ] **Step 4: Preserve the homepage and only update entry routing**

Keep all layout, video and brand sections. The login links remain `/api/auth/feishu/start?app=agent&next=/desktop.html`; a logged-in active employee is routed to `/desktop.html`, while a rejected employee never sees a desktop link.

- [ ] **Step 5: Run tests and inspect the exact diff**

```bash
python3 -m unittest tests.test_employee_access tests.test_oauth_employee_gate -v
git diff -- index.html api/auth/feishu/callback.py api/_admin.py api/me.py
```

Expected: tests PASS; no unrelated homepage markup or visual CSS changes appear in this task's diff.

## Task 5: Replace the session deep link with one-time exchange and refresh rotation

**Files:**
- Create: `/Users/dianchi/dianchi/api/_desktop_auth.py`
- Modify: `/Users/dianchi/dianchi/api/desktop/open.py`
- Create: `/Users/dianchi/dianchi/api/desktop/exchange.py`
- Create: `/Users/dianchi/dianchi/api/desktop/refresh.py`
- Create: `/Users/dianchi/dianchi/tests/test_desktop_auth.py`

- [ ] **Step 1: Write failing handoff tests**

```python
code = issue_handoff(active_auth)
first = exchange_handoff(code, installation_id="install-a")
assert first["session"]
assert first["refresh_token"]
assert first["employee"]["employee_id"] == "emp_1"

with self.assertRaisesRegex(DesktopAuthError, "invalid_or_expired_code"):
    exchange_handoff(code, installation_id="install-a")

rotated = rotate_refresh(first["refresh_token"], installation_id="install-a")
assert rotated["refresh_token"] != first["refresh_token"]
```

Also test 60-second handoff expiry, refresh installation mismatch, disabled employee, 30-day refresh expiry, and that serialized deep links never contain `session=` or a Feishu token.

- [ ] **Step 2: Verify tests fail**

```bash
python3 -m unittest tests.test_desktop_auth -v
```

Expected: FAIL because `_desktop_auth` does not exist.

- [ ] **Step 3: Implement hashed Redis records and rotation**

Use random 32-byte URL-safe values and hash keys before storage:

```python
HANDOFF_TTL_SECONDS = 60
REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60

def _token_key(prefix: str, token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"dianchi:desktop:{prefix}:{digest}"
```

`exchange_handoff()` uses strict `GETDEL`. `rotate_refresh()` atomically consumes the old refresh token before issuing the next one. Both re-run `resolve_employee()` and sign an eight-hour `dc_feishu_session` that includes `employee_id` in both `user` and `employee` payloads.

- [ ] **Step 4: Implement the three endpoints**

- `GET /api/desktop/open`: require active auth, idempotently call existing activation, issue handoff, redirect to `dianchi://authorize?code=<code>`.
- `POST /api/desktop/exchange`: body `{code, installation_id}`, return session/refresh/employee and configured `DESKTOP_PET_API_URL`.
- `POST /api/desktop/refresh`: body `{refresh_token, installation_id}`, rotate and return a fresh pair.

Every JSON response uses `Cache-Control: no-store`; logs must not include codes, sessions or refresh tokens.

- [ ] **Step 5: Run tests**

```bash
python3 -m unittest tests.test_desktop_auth tests.test_oauth_employee_gate -v
```

Expected: PASS.

## Task 6: Add protocol detection and the Vercel Blob release manifest

**Files:**
- Create: `/Users/dianchi/dianchi/api/_desktop_probe.py`
- Create: `/Users/dianchi/dianchi/api/desktop/probe/start.py`
- Create: `/Users/dianchi/dianchi/api/desktop/probe/heartbeat.py`
- Create: `/Users/dianchi/dianchi/api/desktop/probe/status.py`
- Create: `/Users/dianchi/dianchi/api/_desktop_release.py`
- Create: `/Users/dianchi/dianchi/api/desktop/latest.py`
- Create: `/Users/dianchi/dianchi/api/internal/desktop/release.py`
- Modify: `/Users/dianchi/dianchi/desktop.html`
- Create: `/Users/dianchi/dianchi/tests/test_desktop_bootstrap.py`

- [ ] **Step 1: Write failing probe and manifest tests**

Assert a probe begins as pending, heartbeat changes it to installed, status is idempotent, and expiry returns `expired`. Assert the release endpoint returns only an active employee's matching platform/architecture artifact and rejects malformed SHA-256, non-versioned paths and old manifest versions.

```python
probe_id = start_probe()
assert probe_status(probe_id) == "pending"
record_probe_heartbeat(probe_id)
assert probe_status(probe_id) == "installed"
```

- [ ] **Step 2: Verify tests fail**

```bash
python3 -m unittest tests.test_desktop_bootstrap -v
```

Expected: FAIL because probe/release modules do not exist.

- [ ] **Step 3: Implement strict short-lived probes and release validation**

Probe IDs use Redis TTL 90 seconds. The stable release manifest uses key `dianchi:desktop:release:stable` and contains:

```json
{
  "version": "0.2.0",
  "channel": "stable",
  "published_at": "2026-07-13T10:00:00Z",
  "artifacts": {
    "windows-x64": {"url": "https://...blob.vercel-storage.com/desktop/windows/x64/0.2.0/DianchiDesktopAssistantSetup.exe", "size": 1, "sha256": "64-hex", "signed": true},
    "macos-arm64": {"url": "https://...blob.vercel-storage.com/desktop/macos/arm64/0.2.0/DianchiDesktopAssistant.dmg", "size": 1, "sha256": "64-hex", "signed": true}
  }
}
```

The internal release endpoint uses a separate `DESKTOP_RELEASE_SECRET` HMAC and refuses a `stable` manifest if either artifact has `signed=false`; unsigned builds may publish only to `gray`.

- [ ] **Step 4: Turn the existing `desktop.html` into a small state machine**

Keep its current branded card and actions. Implement these states only:

```text
checking-login -> probing -> opening-installed
                         -> downloading-installer
                         -> download-blocked (manual retry button)
denied / pending / disabled / service-unavailable
```

The page calls probe start, launches `dianchi://probe?probe_id=...`, polls status for three seconds, and redirects to `/api/desktop/open` when installed. Otherwise it fetches `/api/desktop/latest`, starts one download, and keeps a visible retry link. It must never claim to scan installed programs or auto-run the downloaded file.

- [ ] **Step 5: Run tests and verify homepage isolation**

```bash
python3 -m unittest tests.test_desktop_bootstrap tests.test_desktop_auth -v
git diff -- desktop.html index.html
```

Expected: tests PASS; this task changes `desktop.html` only, not homepage presentation.

## Task 7: Teach the desktop app to probe, exchange and refresh securely

**Files:**
- Modify: `/Users/dianchi/dianchi-desktop/requirements.txt`
- Modify: `/Users/dianchi/dianchi-desktop/src/config.py`
- Modify: `/Users/dianchi/dianchi-desktop/src/desktop_session.py`
- Modify: `/Users/dianchi/dianchi-desktop/src/api_client.py`
- Modify: `/Users/dianchi/dianchi-desktop/src/app.py`
- Modify: `/Users/dianchi/dianchi-desktop/src/main_window.py`
- Modify: `/Users/dianchi/dianchi-desktop/tests/test_desktop_session.py`
- Create: `/Users/dianchi/dianchi-desktop/tests/test_desktop_onboarding.py`

- [ ] **Step 1: Replace the old session-link test with failing action tests**

```python
assert parse_desktop_link("dianchi://probe?probe_id=p_1") == {"action": "probe", "probe_id": "p_1"}
assert parse_desktop_link("dianchi://authorize?code=c_1") == {"action": "authorize", "code": "c_1"}
assert parse_desktop_link("https://example.com") == {}
assert "session" not in parse_desktop_link("dianchi://open?session=leak")
```

Mock `keyring` and assert access/refresh values are stored under service `com.dianchi.desktop`, while the non-secret installation ID and pet API URL remain in the existing application data directory.

- [ ] **Step 2: Verify tests fail**

```bash
python3 -m unittest tests.test_desktop_session tests.test_desktop_onboarding -v
```

Expected: FAIL because the app still expects `session=` in the URL.

- [ ] **Step 3: Implement credential storage and API calls**

Add `keyring>=25,<26` and expose:

```python
def parse_desktop_link(value: str) -> dict[str, str]: ...
def installation_id() -> str: ...
def load_access_session() -> str: ...
def persist_credentials(session: str, refresh_token: str) -> None: ...
def clear_credentials() -> None: ...
```

`ApiClient` adds `record_probe_heartbeat`, `exchange_desktop_code`, and `refresh_desktop_session`. Pet methods use a separately stored `pet_base_url`; all non-pet company APIs continue using Vercel.

- [ ] **Step 4: Handle both deep-link actions without blocking the UI**

`app._collect_initial_desktop_links()` only queues URLs; it never puts secrets in environment variables. `MainWindow.handle_desktop_link()` behaves as follows:

```python
payload = parse_desktop_link(url)
if payload.get("action") == "probe":
    self.api_client.record_probe_heartbeat(payload["probe_id"], self._on_probe_recorded)
elif payload.get("action") == "authorize":
    self.api_client.exchange_desktop_code(payload["code"], self._on_desktop_authorized)
```

On successful exchange, persist credentials, set the returned pet API URL, refresh `/api/me`, bind the pet, and show the existing official Feishu message page. On startup with no valid access token but a refresh token, rotate once; if rotation fails, clear credentials and open the existing browser OAuth entry only after the employee clicks login.

- [ ] **Step 5: Run the entire no-GUI desktop suite**

```bash
python3 -m unittest discover -s tests -q
```

Expected: PASS without starting Qt windows or mutating real Feishu conversations.

## Task 8: Bind the stable employee identity to the existing pet API

**Files:**
- Modify: `/Users/dianchi/DC-Agent/astrbot/dashboard/routes/pet_live.py`
- Modify: `/Users/dianchi/DC-Agent/tests/test_pet_live_route.py`
- Modify: `/Users/dianchi/dianchi/api/_desktop_auth.py`
- Modify: `/Users/dianchi/dianchi-desktop/src/api_client.py`

- [ ] **Step 1: Write failing binding assertion tests**

Add tests for a HMAC-SHA256 assertion containing `employee_id`, `feishu_open_id`, `desktop_session_id`, `nonce`, `iat`, and `exp`. Assert valid binding succeeds, an expired assertion fails, a modified employee ID fails, and an existing session cannot be rebound to another employee.

- [ ] **Step 2: Verify the focused tests fail**

```bash
uv run pytest tests/test_pet_live_route.py -q
```

Expected: new assertion tests FAIL while existing binding-token and signed-session tests remain green.

- [ ] **Step 3: Issue and verify the dedicated assertion**

Vercel signs the 60-second assertion with `DESKTOP_BINDING_SECRET` during handoff exchange. The desktop sends it only to the configured LAN pet API in header `X-Dianchi-Desktop-Assertion`. DC-Agent verifies the signature/expiry and takes employee identity only from the signed assertion, never from unsigned JSON fields.

Existing `PET_LIVE_BINDING_TOKEN` support remains for server-to-server administration. Existing session takeover checks remain unchanged.

- [ ] **Step 4: Keep message and pet failure domains separate**

If `DESKTOP_PET_API_URL` is missing or unreachable, `ApiClient` reports pet offline and skips state-changing pet requests. It must not clear the Vercel desktop session or prevent the official Feishu Messenger view from loading.

- [ ] **Step 5: Run pet and desktop tests**

```bash
uv run pytest tests/test_pet_live_route.py tests/test_pet_live_identity_smoke_script.py -q
python3 -m unittest discover -s /Users/dianchi/dianchi-desktop/tests -q
```

Expected: PASS.

## Task 9: Build per-user Windows and signed-ready macOS installers

**Files:**
- Create: `/Users/dianchi/dianchi-desktop/DianchiDesktopAssistant.spec`
- Create: `/Users/dianchi/dianchi-desktop/installer/windows/DianchiDesktopAssistant.iss`
- Create: `/Users/dianchi/dianchi-desktop/scripts/build_windows.ps1`
- Create: `/Users/dianchi/dianchi-desktop/scripts/build_macos.sh`
- Create: `/Users/dianchi/dianchi-desktop/tests/test_packaging_contract.py`
- Modify: `/Users/dianchi/dianchi-desktop/DESIGN.md`
- Modify: `/Users/dianchi/dianchi-desktop/README.md`

- [ ] **Step 1: Write failing packaging contract tests**

Tests inspect the build files and assert:

```python
assert "PrivilegesRequired=lowest" in inno_setup
assert "Software\\Classes\\dianchi" in inno_setup
assert 'Filename: "{app}\\DianchiDesktopAssistant.exe"; Parameters: ' in inno_setup
assert "--windowed" in build_contract
assert "CFBundleDisplayName" in mac_contract
assert "巅池桌面助手" in mac_contract
```

Also assert the packaged app contains the company logo, pet assets and QtWebEngine resources, and that neither installer contains `.env`, `employees.db`, session files or API secrets.

- [ ] **Step 2: Verify tests fail**

```bash
python3 -m unittest tests.test_packaging_contract -v
```

Expected: FAIL because the formal build files do not exist.

- [ ] **Step 3: Add the PyInstaller spec and platform scripts**

Use `onedir` builds for QtWebEngine reliability. Windows output is `DianchiDesktopAssistant.exe`; Inno Setup installs to `{localappdata}\Programs\DianchiDesktopAssistant`, registers `dianchi://` under HKCU, creates a Start Menu shortcut, and launches the app after install. It never requests elevation.

macOS builds an arm64 `.app`, applies the existing bundle identifier and company icon, then creates a DMG. If signing variables are present, the script runs `codesign --deep --force --options runtime` and `xcrun notarytool`; otherwise it marks the output `signed=false` for gray testing.

- [ ] **Step 4: Update outdated design decisions**

Change only packaging/distribution decisions in `DESIGN.md` and `README.md`: Vercel Blob replaces NAS installer storage, Windows is per-user without admin password, and unsigned artifacts are gray-only. Preserve all current Messenger and pet architecture notes.

- [ ] **Step 5: Run tests and build on the native platform**

Local macOS:

```bash
python3 -m unittest tests.test_packaging_contract -v
./scripts/build_macos.sh --unsigned-gray
```

Expected: a DMG and SHA-256 metadata under `dist/release/`. Windows build runs only on Windows CI because PyInstaller is not a cross-compiler.

## Task 10: Publish immutable artifacts to Vercel Blob from GitHub Actions

**Files:**
- Create: `/Users/dianchi/dianchi-desktop/package.json`
- Create: `/Users/dianchi/dianchi-desktop/package-lock.json`
- Create: `/Users/dianchi/dianchi-desktop/scripts/upload_release.mjs`
- Create: `/Users/dianchi/dianchi-desktop/.github/workflows/desktop-release.yml`
- Modify: `/Users/dianchi/dianchi-desktop/tests/test_packaging_contract.py`

- [ ] **Step 1: Extend the failing packaging tests for release safety**

Assert the workflow has separate `windows-latest` and `macos-14` build jobs, a dependent publish job, SHA-256 verification, immutable version paths, and no artifact upload through a Vercel Function.

- [ ] **Step 2: Install and lock the Blob SDK**

`package.json` contains only the release tool dependency and script:

```json
{
  "private": true,
  "type": "module",
  "scripts": {"release:upload": "node scripts/upload_release.mjs"},
  "dependencies": {"@vercel/blob": "^2.6.1"}
}
```

Run `npm install` to produce the exact lockfile, then `npm audit --omit=dev`.

- [ ] **Step 3: Implement direct multipart Blob upload**

The Node script uses `put()` with `access: "public"`, `addRandomSuffix: false`, `multipart: true`, and pathnames:

```text
desktop/windows/x64/<version>/DianchiDesktopAssistantSetup.exe
desktop/macos/arm64/<version>/DianchiDesktopAssistant.dmg
```

It reads both files, computes SHA-256 and size, uploads directly with `BLOB_READ_WRITE_TOKEN`, then HMAC-signs and POSTs the resulting manifest to `/api/internal/desktop/release`. It refuses to overwrite an existing version path.

- [ ] **Step 4: Add the release workflow**

The workflow triggers on `v*` tags and manual gray runs. Build jobs upload GitHub workflow artifacts; the publish job downloads both, verifies hashes, uploads to Blob, and publishes the manifest. Required secrets are `BLOB_READ_WRITE_TOKEN`, `DESKTOP_RELEASE_SECRET`, and `DIANCHI_RELEASE_ENDPOINT`; signing secrets are optional for gray but mandatory for stable.

- [ ] **Step 5: Verify workflow and tests**

```bash
python3 -m unittest tests.test_packaging_contract -v
npm ci
npm audit --omit=dev
```

Expected: PASS with no high/critical production dependency finding.

## Task 11: Run cross-repository verification and a two-machine gray test

**Files:**
- Modify only if failures require scoped fixes in files already listed above.

- [ ] **Step 1: Run DC-Agent focused verification**

```bash
make clean-pyc
uv run pytest tests/test_desktop_employee_identity_policy.py tests/scripts_tools/test_employee_auth_sync.py tests/test_pet_live_route.py tests/harness/test_desktop_employee_onboarding_contract.py -q
scripts/agent-check.sh --profile targeted
make check-clean
```

Expected: all focused tests and targeted profile PASS; hygiene reports no newly generated runtime artifacts.

- [ ] **Step 2: Run Vercel project tests**

```bash
cd /Users/dianchi/dianchi
python3 -m unittest discover -s tests -v
```

Expected: PASS. Start the local site server with test KV and verify `/`, `/desktop.html`, `/api/me`, OAuth rejection, probe, exchange, refresh, and latest endpoints.

- [ ] **Step 3: Run the complete desktop no-GUI suite**

```bash
cd /Users/dianchi/dianchi-desktop
python3 -m unittest discover -s tests -q
```

Expected: PASS.

- [ ] **Step 4: Deploy only after environment validation**

Before Vercel deployment verify these production variables exist without printing values:

```text
SITE_SESSION_SECRET
FEISHU_AGENT_APP_ID
FEISHU_AGENT_APP_SECRET
FEISHU_REDIRECT_URI
FEISHU_COMPANY_TENANT_KEY
KV_REST_API_URL
KV_REST_API_TOKEN
DESKTOP_EMPLOYEE_SYNC_SECRET
DESKTOP_RELEASE_SECRET
DESKTOP_BINDING_SECRET
DESKTOP_PET_API_URL
BLOB_READ_WRITE_TOKEN
```

Push the initial employee snapshot, then deploy `dianchi`. Confirm the live homepage is unchanged visually, `/desktop.html` returns 200, unknown employees receive 403, and `/api/desktop/open` never contains `session=`.

- [ ] **Step 5: Complete gray acceptance**

Test one clean Windows x64 machine and one macOS arm64 machine:

1. Active employee OAuth succeeds.
2. No installed app produces the correct installer download.
3. Installer registers `dianchi://` and auto-launches.
4. The same browser session authorizes without a second Feishu scan.
5. Official Feishu Messenger opens in the existing 25/75 layout.
6. Pet binds to the same `employee_id`; pet API outage shows offline without breaking Messenger.
7. Unknown, pending and disabled test identities receive no installer authorization.

Record version, SHA-256, platform, result and screenshots without recording OAuth codes, cookies or refresh tokens.

## References

- Design: `docs/superpowers/specs/2026-07-13-desktop-oauth-installer-distribution-design.md`
- Vercel Blob CLI: `https://vercel.com/docs/cli/blob`
- Vercel Blob multipart guidance: `https://vercel.com/docs/vercel-blob`
- PyInstaller platform builds: `https://pyinstaller.org/en/stable/`
- GitHub-hosted runner images: `https://docs.github.com/en/actions/reference/runners/github-hosted-runners`
