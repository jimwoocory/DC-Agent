# Feishu Assistant Menu and Task Card Design

## Objective

Replace the overlapping `工作台 / 发起任务 / 我的任务` menu with three intent-based menus: `创作 / 办公 / 任务`. Keep the task-card direction, but give each task type a dedicated intake flow instead of reusing one generic form.

The experience must remain deterministic and must not call an LLM while users browse menus, select task types, fill forms, or preview confirmation details. Model or tool execution begins only after the explicit confirmation action.

## Product Boundaries

### Menu responsibilities

The bottom menu is a compact navigation layer. It must help a colleague answer one question: “What do I want to do now?” It does not contain status summaries, instructions, or duplicated shortcuts.

The approved menu structure is:

- `创作`
  - `写文案/方案`
  - `生成图片`
  - `生成视频`
- `办公`
  - `查资料/分析`
  - `处理文件`
- `任务`
  - `继续最近`
  - `进行中`
  - `待补充`
  - `已完成`

`工作台` is removed from the bottom menu. Its useful responsibility becomes the `任务中心` card, opened through the `任务` menu. No menu item opens a generic start page before opening the actual task form.

### Card responsibilities

Cards collect task-specific information, confirm execution, show task status, and present the next useful action. Cards must not reproduce the full bottom-menu hierarchy.

Every intake flow has three stages:

1. A dedicated task form.
2. A deterministic confirmation card showing the normalized request.
3. Explicit `确认并开始执行`, which is the model/tool execution boundary.

## Dedicated Task Cards

### Copy and plan card

The `写文案/方案` card collects:

- Content type: public-account article, activity plan, report, notice, social copy, or custom.
- Purpose and core request.
- Audience or publishing channel.
- Source material: recent attachment, Feishu document link, pasted content, or none.
- Delivery specification: length, tone, structure, and deadline.

The placeholder and examples must describe writing work only.

### Image generation card

The `生成图片` card borrows the useful information hierarchy from Image2 Playground while remaining compact enough for Feishu:

- Template: brand visual, infographic, social cover, video first frame, or custom.
- Visual prompt: subject, purpose, composition, lighting, material, text-safe area, and exclusions.
- Reference source: recent image attachment, image URL, or none.
- Aspect ratio: `1:1`, `3:4`, `9:16`, or `16:9`.
- Output count and quality preset.

If the user needs to upload a reference image, the card explains that the image should be sent in the chat and then reused as the most recent attachment. The card does not pretend to provide a file-upload control that Feishu cards do not support.

### Video generation card

The `生成视频` card collects:

- Generation mode: text to video, image to video, or first/last frame.
- Shot description: subject motion, camera motion, rhythm, and scene transition.
- Reference source: recent attachment, image URL, or none.
- Duration preset: `5s`, `10s`, or `15s` when supported by the selected execution route.
- Aspect ratio: `9:16`, `16:9`, or `1:1`.
- Resolution or quality preset supported by the execution route.

Image and video remain separate task types. They may share low-level builders only where fields and behavior are genuinely identical.

### Research and analysis card

The `查资料/分析` card collects:

- Question or decision to support.
- Scope, time range, and preferred sources.
- Required output: short answer, comparison table, analysis memo, or recommendation.
- Deadline and evidence requirements.

### File processing card

The `处理文件` card collects:

- Source file: recent attachment or Feishu document link.
- Operation: summarize, extract, rewrite, convert, compare, or organize.
- Output format and delivery requirements.

## Task Center

The `任务中心` card replaces the old workbench and task-list duplication. It contains only real task information:

- A compact count for in-progress, waiting-for-input, and recently completed tasks.
- Up to three relevant task rows for the selected status.
- Per-task actions such as `继续`, `补充资料`, `查看结果`, or `重试` when supported.
- A truthful empty state when no matching tasks exist.

The task center must read from the existing task source used by DC-Agent. It must never fabricate counts or placeholder task records. If task persistence cannot provide a requested view, the card shows an explicit empty or unavailable state.

The task center does not include `发起新任务`; new work starts from `创作` or `办公`.

## Routing and Data Flow

1. A Feishu menu click sends a deterministic label or menu event.
2. The assistant router maps the label directly to one dedicated card builder.
3. Menu navigation, form display, form preview, and task-center refresh set `should_call_llm(False)`.
4. The confirmation action carries a trusted normalized payload that is checked by the shared card-action router.
5. Only `start_task` resumes a normal task request into the agent pipeline.
6. Media tasks preserve their structured image/video parameters when entering the media route; they are not flattened into copywriting fields.

## Error Handling

- Card callbacks acknowledge Feishu immediately, while business processing continues asynchronously.
- Missing required fields keep the user in the current flow and explain exactly what is missing.
- Unsupported image/video parameters are removed or replaced with route-supported presets before confirmation.
- Missing reference material produces a clear instruction to send or link the material; it does not silently run without it when the selected mode requires a reference.
- Card-send failure falls back to a concise text response without invoking an LLM.
- Repeated clicks are idempotent where possible and must not create duplicate execution tasks.

## Compatibility and Migration

Legacy labels such as `工作台`, `当前状态`, `发起任务`, `媒体生成`, `文案资料`, and `图片/视频` remain deterministic aliases during migration, but they no longer appear in the published menu.

The Feishu developer-console menu is updated only after the new card routes pass local verification. The final menu configuration is published as a new app version and validated in the native Feishu client.

## Acceptance Criteria

1. The published bottom menu contains `创作 / 办公 / 任务` and no `工作台` entry.
2. Copy, image, video, research, and file menu items open different dedicated cards.
3. No image or video card displays copywriting examples or copywriting-only field labels.
4. Image and video remain independent task types through confirmation and execution routing.
5. Menu browsing, form filling, preview, and task-center views do not call an LLM.
6. Only explicit confirmation starts model or tool execution.
7. The task center shows real task data or a truthful empty state.
8. Card callbacks return a valid response within Feishu's required window.
9. Legacy labels continue to route deterministically during the menu migration.
10. Focused unit tests and `scripts/agent-check.sh --profile targeted` pass before native-client validation.

## Verification

- Card-builder tests assert task-specific labels, placeholders, controls, and callback payloads.
- Routing tests assert every new and legacy menu label maps to the expected card without LLM execution.
- Confirmation tests assert structured image/video parameters survive the preview boundary.
- Task-center tests assert real records are filtered by requester and status, with no fabricated counts.
- Native Feishu validation covers one copy task, one image task, one video task, and one task-center status view.
