# 升级冒烟测试报告 — AstrBot v4.24.2 + Hermes

**日期：** 2026-05-09
**触发：** 用户在 code 模式完成 astrbot/Hermes 大版本升级后，担心定制功能代码被合并冲突误删，要求一次端到端测试。
**结论：** 系统整体可用。发现并修复 1 个真实回归（无损上下文功能未接线），1 个测试硬编码路径问题。

---

## 测试执行情况

| 测试项 | 范围 | 结果 |
|---|---|---|
| Python 环境 | sandbox 重建 venv（macOS .venv 不可用） | ✅ |
| Harness 单元测试 | 8 个文件 / 47 个用例 | ✅ 全通过 |
| 无损上下文 + Router 意图 | 2 个文件 / 104 个用例 | ✅ 全通过（修路径后） |
| 自定义插件加载 | hermes_bridge / onboarding_guide / dreamina_plugin / silent_observer | ✅ 4/4 |
| 全量单元测试 | tests/unit/ 全部 | ✅ **657 passed**（修补丁后） |
| 启动冒烟 | scripts/smoke_startup_check.py | ✅ 启动并绑定 6185 |

---

## 修复 1：无损上下文功能未接线（真实回归）

**问题：** v4.24.2 合并时，git 三路自动合并悄悄丢掉了 `astr_main_agent.py` 中两个辅助函数和 `agent_runner.reset()` 的 `custom_compressor=` 参数。结果是：你之前在 `core_lifecycle.py` 恢复的 `lossless_context_enabled` 字段虽然存在，但**整条链路实际没接通** — 配置开 `True` 也不会真正启用无损压缩，会静默回退到默认的 turn-truncation。

**触发测试：** `tests/unit/test_astr_main_agent.py::TestBuildMainAgent::test_build_custom_compressor_uses_active_provider_for_lossless`，错误为 `AttributeError: module 'astrbot.core.astr_main_agent' has no attribute '_build_custom_compressor'`。

**修复（`astrbot/core/astr_main_agent.py`，+65 行）：**

1. 恢复 `_lossless_enabled(config)` — 同时尊重环境变量 `ASTRBOT_EXPERIMENTAL_LOSSLESS_CONTEXT` 和 `MainAgentBuildConfig.lossless_context_enabled`。
2. 恢复 `_build_custom_compressor(config, plugin_context, provider, conversation_id)` — 构造 `LosslessSummaryCompressor`，注入 `lossless_store` 和 `LosslessAssembler`；导入采用延迟方式以避免功能关闭时拖累冷启动。
3. 在 `build_main_agent()` 调用 `agent_runner.reset(...)` 时传入 `custom_compressor=_build_custom_compressor(...)`。

**验证：** 失败用例修复后通过；全量 657 个单元测试通过；`smoke_startup_check.py` 能正常启动。

> 这条修复跟你 commit `76e84e774`（恢复 fields）是一对儿 — 你那次恢复了"数据"，本次恢复了"行为"。建议作为一个跟进 commit 提交。

---

## 修复 2：测试用例硬编码 macOS 路径

**问题：** `tests/unit/test_router_intents.py` 第 502 行和 550 行硬编码了 `Path("/Users/dianchi/DC-Agent/astrbot/core/router_config.yaml")`。这意味着只要不在你那台 mac mini 上跑，39 个 router 测试全部 setup 失败。

**修复（`tests/unit/test_router_intents.py`，+8 行）：**

改成基于 `__file__` 的相对路径：
```python
_ROUTER_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "astrbot" / "core" / "router_config.yaml"
)
```

**验证：** 39 个 router 用例全部从 ERROR 转为 PASS。

> 这跟本次升级无关，是历史遗留问题。但既然碰到了就顺手修了，方便以后在 CI / 别人的机器上跑测试。

---

## 改动清单

```
astrbot/core/astr_main_agent.py    | +65 行  （新增 _lossless_enabled, _build_custom_compressor, 接线）
tests/unit/test_router_intents.py  |  +8 行  （路径相对化）
```

两处改动都已写到工作目录，未做 git commit / push（按照惯例留给你 review）。

---

## 没问题的部分（明确确认）

- **Harness 任务系统**全套（task_store / engine / memory_store / cognition / workflows / runtime_bridge / commands / completion）— 47 个用例全过。
- **`Context()` 签名兼容性** — 你 commit `76e84e774` 把 `harness_engine` / `harness_store` 加成 keyword-only with default `None`，所以测试 fixture 和老插件都还能用旧签名调用。
- **3 个自定义插件**（hermes_bridge / onboarding_guide / dreamina_plugin）+ silent_observer 都能正常 import 并注册 Star 子类；dreamina 的 3 个 LLM 工具（`dreamina_generate_image` / `dreamina_animate_image` / `dreamina_generate_video`）都正常注册。
- **HermesBridge 的 SessionRouter** 全套 + IntentRouter 全套（路径修好后）— 104 个用例全过。
- **启动链路** — `InitialLoader → core_lifecycle → harness 初始化 → 平台适配器注册` 整条路径，从 cold start 到绑定 6185 端口约 5 秒，无 ERROR/WARNING。

---

## 建议跟进

1. **提交本次修复**（两个文件），commit message 建议 `fix: re-wire lossless context compressor lost in v4.24.2 auto-merge`。
2. **未来合并 upstream 大版本时**，对 `astr_main_agent.py` / `core_lifecycle.py` / `internal.py` 这几个长期被 upstream 重构的文件，建议手工 diff 而不是依赖 git 三路合并。可以在 `AGENTS.md` 加一条 merge checklist。
3. **CI 集成**：`scripts/smoke_startup_check.py` 已经存在且能用，可以加到本地 pre-push hook，每次合并前跑一次。
