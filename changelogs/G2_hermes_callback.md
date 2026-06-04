# G2 / Phase 0.3 — Hermes 异步回群链路实装

实装 Hermes → AstrBot 回调的可靠回群链路：HMAC 入站校验 +
指数退避重试（3 次：1s/2s/4s）+ 4xx/5xx 错误分类 + DLQ 落盘
(`data/hermes_dlq.jsonl`，10MB 单备份轮转)。

## 新增

- `astrbot/core/hermes_dlq_logger.py` — append-only JSONL DLQ，仿
  `router_decision_logger.py`。
- `astrbot/core/hermes_callback_dispatcher.py` — 重试调度器 +
  `Retriable`/`PermanentSendError` 分类 + `verify_hmac_signature`。
- `tests/unit/test_hermes_bridge_callback.py` — 17 个单测。
- `tests/integration/test_hermes_callback_integration.py` — 5 个端到端，
  参数化跑 v1 / v2 plugin。

## 修改

- `astrbot/plugins/hermes_bridge/__init__.py` — git-tracked 参考实装。
- `data/plugins/hermes_bridge/hermes_bridge.py` — 生产 v2 实装
  （路径被 `.gitignore`，需 ops 手动同步本次修改）。

验收：27/27 新测试通过，原有 650 项不变；ruff 干净；smoke_startup 通过。
