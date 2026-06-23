# DC-Agent 四项能力落地实施计划

> **文档用途**：供 Codex 逐项审查并按优先级实施。
> **生成日期**：2026-06-22
> **基准代码版本**：本文档所有文件路径和代码引用均来自对当前仓库的静态读取，不依赖任何外部假设。
> **阅读前置**：先读 `docs/CODEX_WORKFLOW.md`，再读 `harness/contracts/` 对应合约，再看本文档。

---

## 一、现状快照（基于代码读取）

| 能力模块 | 现有核心文件 | 完成度 | 主要缺口 |
|---------|------------|--------|---------|
| **Agent Runtime** | `harness/hermes_bridge.py`、`harness/runtime_registry.py`、`harness/quota_gate.py`、`harness/task_state.py` | 80% | `gemini_cli` / `hermes_agent` adapter 未接线；无统一 tool schema；无 context budget 裁剪 |
| **模型路由** | `dc_router_core/entrypoint.py`（三层架构）、`dc_router_core/taxonomy.py`、`dc_router_core/classifier.py`（空壳） | 90% | `NoopRouterClassifier` 永远返回 FALLBACK；`QuotaGateArbiter` 缺 circuit-breaker 分支 |
| **Skills 执行** | `data/plugins/`（AstrBot plugin 体系）、`dc_engines/` | 60% | 无声明式 Skill schema；无权限模型；无沙箱隔离 |
| **本地加密** | 无 | **0%** | `data/config/` 明文存储飞书 token、模型 API key；`runtime_bootstrap.py` 直接读取明文配置 |

---

## 二、实施方案

---

### 模块 A：本地加密 🔴 最高优先级

**背景**：`data/config/` 视为敏感目录（见 README），但当前无任何加密保护。`runtime_bootstrap.initialize_runtime_bootstrap()` 在 `main.py` 第一行调用时直接读取明文配置，token 和 API key 以明文形式常驻磁盘。

#### A.1 新建目录结构

```
dc_security/
├── __init__.py
├── keystore.py          # macOS Keychain 封装（生产）/ 环境变量降级（CI）
├── crypto.py            # Fernet 静态加密，密钥由 keystore 管理
└── migrate_secrets.py   # 一次性迁移脚本（不进 git，仅本地运行）

scripts-tools/
└── migrate_config_secrets.py   # 入口脚本，调用 dc_security/migrate_secrets.py
```

#### A.2 `dc_security/keystore.py` 实现要点

```python
"""
优先使用 macOS Keychain（keyring 库）。
CI 环境（无 Keychain）时降级到环境变量 DC_SECRET_<KEY>。
密钥不接触磁盘，不进 git，不出现在日志里。
依赖：uv add keyring
"""
import os
import keyring

APP_NAME = "dc-agent"
_CI_MODE = os.getenv("DC_CI_MODE") == "1"

def save_secret(key: str, value: str) -> None:
    if _CI_MODE:
        raise RuntimeError("CI 模式下不允许写 keystore，请用环境变量注入")
    keyring.set_password(APP_NAME, key, value)

def load_secret(key: str) -> str | None:
    if _CI_MODE:
        return os.getenv(f"DC_SECRET_{key.upper().replace('.', '_')}")
    return keyring.get_password(APP_NAME, key)

def delete_secret(key: str) -> None:
    if _CI_MODE:
        return
    try:
        keyring.delete_password(APP_NAME, key)
    except keyring.errors.PasswordDeleteError:
        pass
```

#### A.3 `dc_security/crypto.py` 实现要点

```python
"""
用 cryptography.fernet.Fernet 加密配置文件快照。
密钥名：dc-agent-config-fernet-key（存 Keychain）。
首次调用自动生成密钥并存入 Keychain。
依赖：uv add cryptography
"""
from cryptography.fernet import Fernet
from dc_security.keystore import load_secret, save_secret

_KEY_NAME = "dc-agent-config-fernet-key"

def _fernet() -> Fernet:
    raw = load_secret(_KEY_NAME)
    if raw is None:
        key = Fernet.generate_key()
        save_secret(_KEY_NAME, key.decode())
        return Fernet(key)
    return Fernet(raw.encode())

def encrypt_bytes(plaintext: bytes) -> bytes:
    return _fernet().encrypt(plaintext)

def decrypt_bytes(ciphertext: bytes) -> bytes:
    return _fernet().decrypt(ciphertext)
```

#### A.4 迁移步骤（按序执行，一次性）

1. `uv add keyring cryptography`
2. 运行 `uv run scripts-tools/migrate_config_secrets.py`
   - 读取 `data/config/provider.json`（飞书 app_id/secret、各模型 api_key）
   - 逐个写入 Keychain：`save_secret("feishu.app_secret", "...")`
   - 原 JSON 里的明文值替换为占位符 `"__KEYCHAIN__"`
3. 修改 `runtime_bootstrap.py`：凡是读取 api_key / token / secret 的地方改为 `load_secret(...)`
4. 在 `data/config/.gitignore` 追加 `*.enc`

#### A.5 验收标准

- [ ] `dc_security/` 目录存在，`__init__.py` 导出 `save_secret`、`load_secret`、`encrypt_bytes`、`decrypt_bytes`
- [ ] `data/config/provider.json` 里不再出现真实 api_key 或 token 字符串（`grep -r "sk-" data/config/` 返回空）
- [ ] `uv run pytest tests/dc_security/ -q` 全部通过（需补写）
- [ ] `make check-clean` 无报错

---

### 模块 B：Skills 执行系统 🟠 第二优先级

**背景**：`dc_engines/` 有多个功能引擎（飞书读写、记忆治理、群聊总结等），但以命令式方式调用，无统一的 schema 声明、权限检查和沙箱保护。

#### B.1 新建目录结构

```
dc_skills/
├── __init__.py
├── schema.py            # SkillDef 数据类 + 全局注册表
├── permissions.py       # 权限声明 + ACL 检查
├── runner.py            # 执行入口：权限检查 → 超时沙箱 → 调用 handler
└── registry_init.py     # 将现有 dc_engines 引擎包装注册进来

data/config/
└── skill_acl.json       # 每个 skill 允许哪些 user_id / session_id 调用（模板）
```

#### B.2 `dc_skills/schema.py` 实现要点

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

@dataclass(frozen=True)
class SkillParam:
    name: str
    type: str          # "string" | "integer" | "boolean" | "object" | "array"
    description: str
    required: bool = True

@dataclass(frozen=True)
class SkillDef:
    name: str                           # 全局唯一，格式：<domain>.<action>，如 "feishu.send_message"
    description: str
    trigger_intents: tuple[str, ...]    # 关联 RouterIntent 值，如 ("ops_writing", "casual")
    params: tuple[SkillParam, ...] = ()
    required_permissions: tuple[str, ...] = ()
    sandboxed: bool = False
    handler: Callable[..., Awaitable[Any]] | None = None

    def to_tool_schema(self) -> dict[str, Any]:
        """生成 OpenAI function_calling 兼容 schema，供 HermesBridge prompt 使用"""
        ...

# 全局注册表
_REGISTRY: dict[str, SkillDef] = {}

def register(skill: SkillDef) -> None:
    if skill.name in _REGISTRY:
        raise ValueError(f"Skill 已注册: {skill.name}")
    _REGISTRY[skill.name] = skill

def resolve_by_intent(intent: str) -> list[SkillDef]:
    return [s for s in _REGISTRY.values() if intent in s.trigger_intents]

def get(name: str) -> SkillDef | None:
    return _REGISTRY.get(name)
```

#### B.3 `dc_skills/runner.py` 执行入口

```python
import asyncio
from dc_skills.schema import SkillDef, get
from dc_skills.permissions import check_permission, PermissionDenied

async def run_skill(
    skill_name: str,
    caller_id: str,
    kwargs: dict,
    timeout: float = 30.0,
) -> Any:
    skill = get(skill_name)
    if skill is None:
        raise KeyError(f"Skill 未注册: {skill_name}")

    # 权限检查
    for perm in skill.required_permissions:
        if not await check_permission(perm, caller_id):
            raise PermissionDenied(f"{caller_id} 缺少权限: {perm}")

    # 超时隔离（第一阶段沙箱）
    async with asyncio.timeout(timeout):
        return await skill.handler(**kwargs)
```

#### B.4 现有 dc_engines 迁移策略

不需要重写现有引擎，包一层注册即可：

```python
# dc_skills/registry_init.py
from dc_engines.feishu import send_feishu_message   # 现有函数
from dc_skills.schema import SkillDef, SkillParam, register

register(SkillDef(
    name="feishu.send_message",
    description="向飞书群或用户发送消息",
    trigger_intents=("ops_writing",),
    params=(
        SkillParam("chat_id", "string", "目标群或用户 open_id"),
        SkillParam("content", "string", "消息正文（Markdown）"),
    ),
    required_permissions=("feishu.write",),
    handler=send_feishu_message,
))
# ... 逐步补充其他引擎
```

#### B.5 验收标准

- [ ] `dc_skills/schema.py` 中 `SkillDef.to_tool_schema()` 返回符合 OpenAI function_calling spec 的 dict
- [ ] `dc_skills/runner.py` 对无权限调用抛 `PermissionDenied`，对超时调用抛 `asyncio.TimeoutError`
- [ ] `registry_init.py` 至少注册 3 个现有 dc_engines 引擎
- [ ] `uv run pytest tests/dc_skills/ -q` 全部通过
- [ ] `data/config/skill_acl.json` 模板存在，字段含 `skill_name`、`allowed_callers`

---

### 模块 C：模型路由补全 🟡 第三优先级

**背景**：`DCRouter` 三层架构（L1意图→L2路由→L3仲裁）已是工业级实现，`entrypoint.py` 的 `decide()` 单入口设计非常干净。需要补两个空洞。

#### C.1 实现 LLMRouterClassifier（替换 NoopRouterClassifier）

**当前问题**：`dc_router_core/classifier.py` 里的 `NoopRouterClassifier.classify()` 永远返回 `RouterIntent.FALLBACK`，导致所有走 classifier 路径的消息都落到 fallback 路由。

**修改文件**：`dc_router_core/classifier.py`

```python
class LLMRouterClassifier(RouterClassifier):
    """
    使用轻量模型做意图分类。
    - 超时受 entrypoint.py::CLASSIFIER_TIMEOUT_SECONDS = 12.0 控制
    - 置信度 < CLASSIFIER_CONFIDENCE_THRESHOLD (0.65) 时降级 FALLBACK
    - 推荐使用 aihubmix Gemini Flash（已在 L2 路由表里有对应 provider）
    """
    SYSTEM_PROMPT = """你是路由分类器。根据用户消息，从以下意图中选择最匹配的一个并给出置信度（0-1）。
意图列表：casual, work_preflight, ops_writing, multimodal, realtime, public_opinion,
          simple_code, creative, insight, deep_creative, deep_insight
返回 JSON：{"intent": "<intent>", "confidence": <float>}"""

    def __init__(self, client: Any) -> None:
        self._client = client   # 已有 provider client，从 AstrBot context 注入

    async def classify(self, text: str) -> ClassifierResult:
        response = await self._client.chat(
            model="gemini-flash",           # 轻量模型，低延迟
            messages=[
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": text[:500]},  # 只取前500字
            ],
            response_format={"type": "json_object"},
        )
        data = json.loads(response.content)
        return ClassifierResult(
            intent=RouterIntent(data["intent"]),
            confidence=float(data["confidence"]),
        )
```

**注入点**：`data/plugins/dc_router/dc_router_adapter.py` 实例化 `DCRouter` 时传入 `LLMRouterClassifier(client=...)`。

#### C.2 QuotaGateArbiter 补 circuit-breaker

**当前问题**：`harness/route_arbiter.py` 里 `QuotaGateArbiter.arbitrate()` 注释提到 "circuit open → 改 fallback provider"，但逻辑未实现。

**修改文件**：`harness/route_arbiter.py`

实现要点：
- circuit-breaker 状态存在 `QuotaGate` 的 aiosqlite（不引入 Redis）
- 新增表 `dc_circuit_state(provider TEXT PRIMARY KEY, fail_count INT, opened_at REAL)`
- 失败次数 ≥ 3 次且最近 1 次失败 ≤ 60 秒内 → circuit open
- open 状态下，`arbitrate()` 把 route 的 provider 替换为 fallback_provider（从 `provider_map` 取）

#### C.3 验收标准

- [ ] `NoopRouterClassifier` 仍保留（用于测试），但 `DCRouter` 默认实例改用 `LLMRouterClassifier`
- [ ] `uv run pytest tests/dc_router/ -q` 全部通过（包含 classifier mock 测试）
- [ ] `CLASSIFIER_TIMEOUT_SECONDS` 和 `CLASSIFIER_CONFIDENCE_THRESHOLD` 不被修改（已是调好的值）
- [ ] circuit-breaker 状态可通过 dashboard API 查询（`/api/v1/circuit-breaker/status`）

---

### 模块 D：Agent Runtime 补全 🟢 第四优先级

**背景**：`HermesBridge` 已有 `claude_cli` / `codex_cli` 完整 dispatch，`QuotaGate` aiosqlite 队列工业级稳定。需要补 gemini_cli adapter 和两个横切关注点。

#### D.1 接线 gemini_cli

**修改文件**：`harness/hermes_bridge.py`、`harness/runtime_registry.py`

在 `hermes_bridge.py` 的 `_run_request()` 里新增分支：

```python
elif runtime == "gemini_cli":
    result = await self._run_gemini_cli(request)
    ok = result.ok
    text = result.text
    raw = result.raw_events
    err = result.error or result.stderr or "Gemini CLI failed"
```

新增 `_run_gemini_cli()` 方法，参照 `_run_claude_cli()` 模式，调用 `gemini -p <prompt>` CLI。

在 `runtime_registry.py` 的 `from_env()` 里：
```python
# 改为 adapter_wired=True，加 env 开关
"gemini_cli": RuntimeStatus(
    runtime="gemini_cli",
    implemented=True,
    enabled=bool(env.get("DC_ENABLE_GEMINI_CLI")),
    reason="available" if env.get("DC_ENABLE_GEMINI_CLI") else "disabled_by_env",
    adapter_wired=True,   # ← 改这里
),
```

#### D.2 新建 `harness/tool_schema.py`（统一 tool 描述）

```python
"""
统一 tool/function schema，供 HermesBridge 向 claude_cli / codex_cli / gemini_cli
构造 prompt 时使用，格式兼容 OpenAI function_calling spec。
dc_skills.schema.SkillDef.to_tool_schema() 调用本模块。
"""
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True, slots=True)
class ToolParam:
    name: str
    type: str        # "string" | "integer" | "boolean" | "object" | "array"
    description: str
    required: bool = True

@dataclass(frozen=True, slots=True)
class ToolDef:
    name: str
    description: str
    params: tuple[ToolParam, ...] = ()

    def to_openai_schema(self) -> dict[str, Any]:
        properties = {
            p.name: {"type": p.type, "description": p.description}
            for p in self.params
        }
        required = [p.name for p in self.params if p.required]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }
```

#### D.3 新建 `dc_engines/context_manager.py`（context budget trimming）

```python
"""
在进入 HermesBridge.submit() 前裁剪对话历史，防止超 context window。
策略：保留 system prompt + 最新 N 条消息，始终保留最后一条 user 消息。
"""
from __future__ import annotations

# Claude 3.5 Sonnet: 200k tokens，保守取 180k
DEFAULT_MAX_CHARS = 180_000 * 3   # 粗略估算：1 token ≈ 3 chars

def trim_history(
    messages: list[dict],
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[dict]:
    if not messages:
        return messages

    system = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    total = sum(len(str(m.get("content", ""))) for m in system)
    kept: list[dict] = []

    # 从最新往最旧加，超出预算就停
    for msg in reversed(non_system):
        size = len(str(msg.get("content", "")))
        if total + size > max_chars and kept:
            break
        kept.insert(0, msg)
        total += size

    return system + kept
```

#### D.4 验收标准

- [ ] `DC_ENABLE_GEMINI_CLI=1 uv run python -c "from harness.runtime_registry import RuntimeRegistry; import os; r = RuntimeRegistry.from_env(os.environ); assert r.get('gemini_cli').adapter_wired"` 通过
- [ ] `harness/tool_schema.py` 的 `ToolDef.to_openai_schema()` 输出通过 JSON Schema 校验
- [ ] `dc_engines/context_manager.py` 的 `trim_history()` 有 pytest 单元测试，覆盖空列表、纯 system、超长历史三种情况
- [ ] `hermes_agent` runtime 仍保持 `adapter_wired=False`（未实现，不改动）

---

## 三、新增依赖

在 `pyproject.toml` 的 `[project.dependencies]` 或 `uv add` 安装：

```toml
keyring = ">=25.0"       # A 模块：macOS Keychain
cryptography = ">=42.0"  # A 模块：Fernet 加密
```

其余模块（`dc_skills/`、`harness/tool_schema.py`、`dc_engines/context_manager.py`）纯 stdlib + 已有依赖，**不引入新包**。

---

## 四、优先级与实施顺序

```
Week 1（本周，最高优先级）
└── 模块 A：本地加密
    1. uv add keyring cryptography
    2. 新建 dc_security/keystore.py + crypto.py
    3. 运行 migrate_config_secrets.py（一次性，本地执行）
    4. 修改 runtime_bootstrap.py 读取方式
    5. 写 tests/dc_security/test_keystore.py + test_crypto.py

Week 2
└── 模块 B：Skills 执行系统
    1. 新建 dc_skills/ 目录
    2. schema.py + permissions.py + runner.py
    3. registry_init.py 注册飞书、记忆、群聊总结引擎
    4. 写 tests/dc_skills/

Week 3
└── 模块 C：模型路由补全
    1. 实现 LLMRouterClassifier（用 Gemini Flash）
    2. QuotaGateArbiter circuit-breaker
    3. 扩展 tests/dc_router/test_classifier.py

Week 4
└── 模块 D：Agent Runtime 补全
    1. gemini_cli adapter（hermes_bridge.py）
    2. harness/tool_schema.py
    3. dc_engines/context_manager.py
```

---

## 五、不在本次范围内

- `hermes_agent` runtime 的完整实现（当前 `adapter_wired=False` 保持原样）
- AstrBot 上游升级（独立维护节奏）
- `nas_sync/` 知识流重构
- Dashboard 新路由（依赖 Skills 系统完成后再规划）

---

## 六、检查命令速查

```bash
# 运行所有新模块测试
uv run pytest tests/dc_security/ tests/dc_skills/ tests/dc_router/ -q

# 检查无明文密钥泄露
grep -r "sk-" data/config/ && echo "⚠️ 发现明文 key" || echo "✅ 无明文 key"
grep -rE "(app_secret|api_key)\s*[:=]\s*\"[^_]" data/config/ || echo "✅ 配置已清洗"

# 仓库卫生
make clean-pyc && make check-clean

# 完整检查（PR 前）
scripts/agent-check.sh --profile full
```

---

*本文档由 Tabbit 基于 DC-Agent 仓库代码静态分析生成，2026-06-22。*
*如有歧义，以 `harness/contracts/` 中对应合约为准。*
