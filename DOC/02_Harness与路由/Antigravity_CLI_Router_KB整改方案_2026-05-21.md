# Antigravity CLI 接入后的 Router、知识库与记忆整改方案

日期：2026-05-21  
适用范围：DC-Agent / AstrBot / Router / Harness / NAS 知识库 / Claude Code 培训材料

## 1. 核心结论

Antigravity CLI 可以接入 AstrBot，但它的定位必须明确：

- 它是 **官方 Gemini Flash 多模态理解通道**。
- 当前只按 `Gemini-3.5-flash` 使用。
- 它适合轻量对话、分类、摘要、图片理解、视频理解。
- 它不适合作为深度分析主模型，也不是生图、生视频模型。

因此，本次整改不是单独改 router，而是要同时整改：

1. Router 场景分流。
2. CLI runner。
3. 配额与 fallback。
4. NAS 主存储与 AstrBot KB 索引同步。
5. 图片/视频理解结果的 manifest 化。
6. 长期记忆与短期记忆的可信输入边界。

当前落地状态（2026-05-21）：

- 已新增 `cli/antigravity/gemini-3.5-flash` 路由能力。
- 闲聊 `casual` 已优先尝试 Antigravity CLI。
- 多模态附件理解已优先尝试 Antigravity CLI，失败后自动回落 `aihubmix/gemini-3-flash-preview`。
- 生图、生视频、静态图转视频、深度分析、代码任务未改走 Antigravity。
- Antigravity CLI 命令采用可配置方式：默认命令名为 `agy`，默认参数为 `--print <prompt> --print-timeout <seconds>`；也可通过 `DC_ANTIGRAVITY_CLI_BIN` 和 `DC_ANTIGRAVITY_CLI_ARGS` 指定真实命令和参数。
- 当前 macOS 机器已检测到 `/Users/dianchi/.local/bin/agy`，并已完成 `agy --print` 极短消息测试。

## 2. Antigravity CLI 的系统定位

统一 provider 命名建议：

```text
cli/antigravity/gemini-3.5-flash
```

推荐定位：

| 场景 | 是否使用 Antigravity CLI | 说明 |
| --- | --- | --- |
| 日常闲聊 | 可以 | 轻量、快速、额度清晰 |
| 小助手普通问答 | 可以 | 需要真实性约束 |
| router 意图分类 | 可以 | Flash 很适合分类和轻量判定 |
| 简短总结 | 可以 | 文本、图片、短视频摘要都可用 |
| 图片理解 | 可以 | 作为官方 Gemini 多模态理解入口 |
| 视频理解 | 可以 | 用于视频总结、关键画面、口播识别、初步质检 |
| 音频/视频口播提取 | 可以 | 作为轻量理解入口 |
| Hermes Agent 深度分析 | 不作为主力 | Claude/Codex/Pro 模型优先 |
| 深度商业判断 | 不作为主力 | 需要更强推理和证据链 |
| 长文案深度生成 | 不作为主力 | 可作为素材理解前置，不作为最终生成主力 |
| 代码任务 | 不作为主力 | Codex CLI / Claude CLI 优先 |
| 生图 | 不使用 | 继续走 `gpt_image_plugin`，必要时其他图像模型兜底 |
| 图生视频/文生视频 | 不使用 | 继续走 `dreamina_plugin` |

## 3. Router 整改方案

Router 需要新增 Antigravity provider，但只挂到轻量和多模态理解场景。

### 3.1 新增 provider

新增 provider id：

```text
cli/antigravity/gemini-3.5-flash
```

runner 能力：

```text
text_input=true
image_input=true
audio_input=true
video_input=true
file_input=true
text_output=true
image_generation=false
video_generation=false
deep_reasoning=false
```

注意：以上三个 `false` 只代表 Antigravity CLI 这个 provider 不承担生图、生视频和深度推理；不是关闭系统能力。系统里的生图仍走 `gpt_image_plugin`，即梦作为兜底；生视频和静态图转视频仍走 `dreamina_plugin`；深度推理仍走 Claude CLI / Codex CLI / Hermes Agent 深度任务链路。

### 3.2 推荐路由表

| Router 场景 | 主路由 | 备用 |
| --- | --- | --- |
| casual_chat | `cli/antigravity/gemini-3.5-flash` | AIHubMix Flash |
| daily_qa | `cli/antigravity/gemini-3.5-flash` | AIHubMix Flash |
| intent_classify | `cli/antigravity/gemini-3.5-flash` | 本地规则/AIHubMix Flash |
| short_summary | `cli/antigravity/gemini-3.5-flash` | AIHubMix Flash |
| image_understanding | `cli/antigravity/gemini-3.5-flash` | AIHubMix 多模态 |
| video_understanding | `cli/antigravity/gemini-3.5-flash` | AIHubMix 多模态/人工补资料 |
| audio_understanding | `cli/antigravity/gemini-3.5-flash` | AIHubMix 多模态 |
| simple_writing | AIHubMix Flash 或 Antigravity | AIHubMix Pro |
| business_writing | AIHubMix Pro | Claude/Codex |
| deep_insight | Claude CLI / Codex CLI | AIHubMix Pro |
| simple_code | Codex CLI / gpt-5.4 | Codex OAuth |
| deep_code | Codex CLI / gpt-5.5-xhigh | Claude CLI |
| image_generation | `gpt_image_plugin` | dreamina_plugin |
| image_to_video | dreamina_plugin | 人工排队 |
| text_to_video | dreamina_plugin | 人工排队 |

### 3.3 禁止误路由

以下场景禁止直接落到 Antigravity CLI：

- `deep_research`
- `deep_insight`
- `hermes_agent_deep_analysis`
- `strategic_report`
- `legal_or_financial_final_answer`
- `image_generation`
- `video_generation`
- `production_code_patch`

## 4. CLI Runner 整改

新增：

```text
run_antigravity()
```

必须支持：

1. 文本输入。
2. 文件路径输入。
3. 图片路径输入。
4. 视频路径输入。
5. 超时控制。
6. 排队识别。
7. 额度不足识别。
8. stderr/stdout 结构化捕获。
9. 结果写入 Router decision trace。

错误归类建议：

| 错误类型 | 系统动作 |
| --- | --- |
| quota_exceeded | 立即 fallback |
| rate_limited | 等待或 fallback |
| queue_busy | fallback 到备用模型 |
| unsupported_file | 提醒员工换格式或上传原文件 |
| timeout | fallback，并记录事件 |
| auth_error | watchdog 告警 |
| empty_result | 重试一次，仍失败则 fallback |

## 5. 配额与 fallback 整改

Antigravity CLI 需要独立配额桶，不要和 Gemini CLI 旧桶混用。

建议记录：

```json
{
  "provider": "cli/antigravity/gemini-3.5-flash",
  "rpm_limit": 15,
  "rpd_limit": 1500,
  "minute_used": 0,
  "day_used": 0,
  "video_used": 0,
  "image_used": 0,
  "fallback_count": 0,
  "last_error": ""
}
```

20 人使用时的理解：

- 1500 次/天平均到 20 人，是 75 次/人/天。
- 日常聊天够用，但不能把全部闲聊、视频分析、总结、公告、检索都无节流打到它身上。
- 视频理解要单独限流，因为一次请求虽然按“次数”算，但上下文与处理负载更高。

建议策略：

1. 文本轻任务优先 Antigravity。
2. 视频任务每人每日限额。
3. 群聊中连续触发视频理解时排队。
4. 超过阈值自动 fallback。
5. 不让员工直接看到 CLI 错误，统一由小助手温和解释。

## 6. NAS 与 AstrBot 知识库整改

原则：

```text
NAS = 主存储
AstrBot KB = 索引层
长期记忆 = 经过筛选的稳定事实/偏好/流程
短期记忆 = 当前会话和近期任务上下文
```

不要把 AstrBot KB 当文件主库，也不要把视频原文件直接塞进普通文本知识库。

### 6.1 NAS 目录约定

建议结构：

```text
/Users/dianchi/nas_kb/
  inbox/                      # 待处理文件
  processed/                  # 已入库文件
  archive/                    # 归档和隔离
    dedupe_quarantine/         # 去重隔离，不直接删除
  manifests/
    documents/
    images/
    videos/
    memories/
```

### 6.2 Manifest 标准

每个可进入知识库或记忆系统的文件，都应生成 manifest。

视频 manifest 示例：

```json
{
  "asset_id": "video_20260521_xxx",
  "source": "feishu_upload|nas_manual|chat_upload",
  "nas_path": "/Users/dianchi/nas_kb/...",
  "original_filename": "xxx.mp4",
  "uploaded_by": "employee_id",
  "uploaded_at": "2026-05-21T10:00:00+08:00",
  "content_sha256": "...",
  "media_type": "video",
  "analysis_provider": "cli/antigravity/gemini-3.5-flash",
  "analysis_status": "completed|failed|needs_review",
  "summary": "视频内容摘要",
  "tags": ["产品", "口播", "门店", "售后"],
  "detected_entities": ["五菱", "柳汽"],
  "truth_level": "source_observed",
  "human_confirmed": false,
  "kb_sync": {
    "synced": true,
    "kb_id": "xxx",
    "doc_id": "xxx"
  },
  "memory_policy": {
    "short_term": true,
    "long_term_candidate": false
  }
}
```

### 6.3 AstrBot KB 同步规则

进入 AstrBot KB 的内容应是：

- 文档文本。
- 图片/视频/音频的结构化摘要。
- 标签。
- 来源路径。
- 真实性状态。

不应进入普通 KB 的内容：

- 大视频原文件。
- 大量重复素材。
- 未确认的模型猜测。
- 无来源的公司事实。

## 7. 真实性与记忆整改

Antigravity 的视频理解输出必须区分三类：

1. **source_observed**：模型从文件里直接看到/听到。
2. **model_inferred**：模型推测，不能当公司事实。
3. **human_confirmed**：员工或老板确认过，可以进入长期记忆候选。

小助手回复要避免：

- “视频证明了公司已经……”
- “这个客户一定是……”
- “这个素材肯定适合投放……”

推荐回复口径：

```text
我先按视频里能直接看到和听到的内容帮你整理，涉及公司结论或投放判断的部分，我会标出来等你确认，避免后面知识库学偏。
```

长期记忆只接收：

- 人工确认的公司事实。
- 稳定流程。
- 已定稿 SOP。
- 项目长期偏好。
- 反复验证过的素材标签规则。

短期记忆可以接收：

- 当前对话。
- 当前任务目标。
- 当前文件摘要。
- 当前员工补充说明。

## 8. 去重整改

当前已落地的原则：

- NAS 去重只按完整内容哈希判定，不按文件名猜测。
- 重复文件不直接删除，移动到 `archive/dedupe_quarantine/`。
- 隔离区不再参与后续知识库入库与去重审计。
- AstrBot KB 需要和 NAS ingest state 对账。

必须防止：

- 同名文件覆盖。
- 同一文件重复进入 `inbox`。
- `processed` 里的已入库文件再次被同步。
- 视频摘要重复写入 KB。
- 长期记忆重复沉淀同一事实。

## 9. Watchdog 整改

现有 watchdog 要增加/保持以下任务：

| 任务 | 频率 | 目的 |
| --- | --- | --- |
| NAS mount heartbeat | 1 分钟 | 保证 NAS 在线，但不误重挂载 |
| feishu_sync heartbeat | 1 小时 | 飞书资料进入 NAS |
| kb_inbox | 15 分钟 | inbox 文档进入 AstrBot KB |
| kb_reconcile | 15 分钟 | NAS ingest state 与 AstrBot KB 对账 |
| dedupe audit | 低峰手动/定时 | 分目录精确去重 |
| video manifest reconcile | 后续新增 | 视频摘要与 KB/记忆对账 |

全量 NAS 去重不能在工作时段硬跑。影视素材目录文件量大，应在低峰分目录执行。

## 10. Claude Code 培训与测试口径

Claude Code 写培训和测试时，必须按以下口径讲：

### 10.1 员工培训重点

员工要理解：

1. 上传文件是为了让小助手基于真实资料工作。
2. 小助手不会凭空编公司事实。
3. 视频可以让小助手总结和打标签，但关键业务结论要员工确认。
4. NAS 是公司知识主库。
5. AstrBot KB 是检索索引，不是文件仓库。
6. 重复文件会影响检索和记忆，所以不要反复上传同一文件。

### 10.2 老板培训重点

老板要理解：

1. Antigravity CLI 增强的是轻量多模态理解能力。
2. 深度分析仍由 Hermes Agent + Claude/Codex/Pro 模型承担。
3. 视频资产可以被结构化，但不会自动变成公司结论。
4. 知识库治理决定小助手长期效果。

### 10.3 测试题建议

题目示例：

1. 员工上传一个视频，小助手应该直接生成视频还是先做视频理解？
   - 正确答案：先做视频理解；生成视频仍走 dreamina_plugin。

2. Antigravity CLI 是否可以作为 Hermes 深度分析主模型？
   - 正确答案：不可以，只作为 Gemini Flash 多模态理解通道。

3. NAS 和 AstrBot KB 谁是主库？
   - 正确答案：NAS 是主库，AstrBot KB 是索引层。

4. 模型从视频里推测出的结论能否直接进入长期记忆？
   - 正确答案：不能，需要人工确认。

5. 如果 Antigravity CLI 超额度，小助手应该怎么做？
   - 正确答案：自动 fallback，并用温和话术说明正在换备用通道处理。

## 11. 验收标准

工程验收：

- Router 中存在 `cli/antigravity/gemini-3.5-flash`。
- Antigravity 只进入轻量和多模态理解场景。
- 生图、生视频不走 Antigravity。
- 深度分析不默认走 Antigravity。
- CLI runner 能识别 quota、timeout、queue、auth 错误。
- `kb_reconcile` 能输出 NAS 与 AstrBot KB 是否一致。
- 视频分析结果能写 manifest。
- KB 中不出现重复文档。

业务验收：

- 员工上传文件后，小助手会先要求或引用真实材料。
- 视频可以被总结、打标签、找关键点。
- 小助手明确区分“文件中看到”和“模型推测”。
- 老板查询知识库时，结果能追溯到 NAS 原文件。
- 重复文件不会继续污染 KB 和长期记忆。
