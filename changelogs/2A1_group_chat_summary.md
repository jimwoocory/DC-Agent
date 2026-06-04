# 2A-1 项目群聊总结

**分支**：`feat/2a1-group-chat-summary` · **范围**：W1 第一件交付（Codex 问卷 82% / 9-11 员工最强需求）

## 做了什么

群里 `@DC-Agent` + 总结/汇总/聊天总结 关键词触发，从最近群聊里抽出**客户反馈 / 老板要求 / 执行问题 / 阶段结论**四段总结回到原群。低打扰：必须 @+ 关键词双满足，纯 @ 不抢答。

- `astrbot/core/group_summary/` 完整包：
  - `contracts.py`：`GroupMessage` TypedDict（normalized 跨平台消息）+ `GroupSummary` dataclass（含 raw_response 供调试）
  - `time_range.py`：`parse_time_range` 支持 "今天"/"昨天"/"前天"/"今天 14:30"/"最近 N 条"/"最近 N 天"/"最近 N 小时"/"最近一周"/"本周" + 英文 today/yesterday/last N/last week；无法识别走默认 12h × 50 条
  - `history_fetcher.py`：duck-typed 三平台 dispatch
    - aiocqhttp（QQ）→ `bot.call_action("get_group_msg_history", ...)`
    - Lark → `lark_api.im.v1.message.alist(ListMessageRequest)`，运行时按需 import
    - WebChat fallback → `message_history_manager.get(...)`
    - 任何平台错误抛 `UnsupportedPlatformError`（明确提示用户而非沉默）
    - `_filter_and_cap` 同时过滤时间窗 + 跳 bot 自己 + 跳触发消息 ID + 升序排 + max_count 保尾
  - `summarizer.py`：单次 LLM call，graceful：空输入不调 LLM、JSON 解析失败/code-fence/partial keys 都不抛错、超长输入按 200 条上限从头截断（保尾留 stage_conclusions）；list value coerced 成分号串
  - `formatter.py`：固定 markdown 模板（`## 群聊总结 | N 条 | range` + 四段）
- `router_config.yaml`：新增 group_summary skill_intent（confidence 0.96），9 个明确组合关键词避免误触发 "我今天总结了一下" 这种叙述
- `router_stage.py:_handle_group_summary`：完整管道（找 provider → 找 platform_inst → parse_time_range → fetch → summarize → format → 回群 → Case 软挂接 deliverable）；任何失败都给用户明确文案
- `tests/eval/router_bench_v0.yaml`：新增 5 个 group_summary 触发样本

## 设计原则

- Phase 1 骨架（cowork agent 写的）+ Phase 2 实现（主会话接手 cowork 卡死后续完成）的 review/继续模式
- Pure-ish 模块切分：4 个文件各 ~100 行，全部可独立单测，没有平台/DB 知识泄漏到 summarizer
- 容错优先：LLM 失败 / JSON malformed / 平台 API 不可用 → 都不抛错，给空 summary 或明确错误，user 永远收到回复
- Case 软挂接：成功总结后挂到 active case 作为 deliverable，失败不阻断回群

## 验收

- 新增 44 个单测（time_range 14 + formatter 4 + summarizer 8 + fetcher 14 + module 2 + system_prompt 2）全过
- 现有 master 测试 251 个回归全过；router bench v0 在 v0+5 样本（54 总）上仍 100%
- ruff check 干净；`scripts/smoke_startup_check.py` 通过

## 与上下游的衔接

- **W0 / 2A-0**（Case 聚合层）：复用 `case_engine.add_deliverable` 把总结结果挂到当前 case
- **W0 / G2**（Hermes Bridge 异步回群）：未直接复用——总结由当前会话 LLM 完成，不需走 Hermes 深度执行
- **W2 / 2A-2**（任务提取）：将复用本任务的 LLM-JSON 模式（system_prompt + _extract_json_object 容错）
- **W3 / 2A-3**（飞书资料读+写）：将参考本任务的平台 dispatch 设计

## 已知限制 / 后续优化

- v0 不做 token-level 截断，按消息条数 200 条硬限——超长群聊可能漏掉中间细节
- Lark API 字段抽取是 best-effort（lark-oapi 类型动态推断），可能漏 reaction / mention 等
- LLM JSON 容错完善但缺二级校验（e.g., LLM 把"客户反馈"错放到 "boss_requests"）—— 留给 R3 LLM-judge 补
- WebChat 分支用 message_history_manager.get(page=1, page_size=200)，超过 200 条要分页（v0 不做）
- Case soft-attach 用 in-memory path（`in-memory://summary-<ts>`），W3 接 NAS 归档后改为真实文件路径

## 时间线

- 2026-05-11 ~14:30  spawn_task 派 W1 cowork chip
- 2026-05-11  15:43  cowork Phase 1 骨架完成，停在 review 节点
- 2026-05-11  ~15:59 我批准 "继续 Phase 2"，但消息没回流到 cowork session
- 2026-05-11 ~16-23 时段  cowork 死寂，主仓无任何新改动
- 2026-05-12  09:20  user 发现 cowork 没在跑，启动主会话接手 Phase 2
- 2026-05-12  09:40  Phase 2 完成、267 测试通过、ruff 干净、smoke 通过

总计 cowork Phase 1 ~30min + 主会话 Phase 2 ~80min（涵盖 grep 探索 + 4 模块实现 + 44 测试 + router 集成 + ruff/smoke）。
