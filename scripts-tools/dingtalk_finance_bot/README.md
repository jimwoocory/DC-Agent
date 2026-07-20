# DingTalk Finance Bot

本目录用于推进“财务报销机器人”：先接通钉钉报销审批与附件读取，再逐步补发票 OCR、查重、预存发票和抵扣台账。

## Approval Workflow Role

机器人在钉钉审批流里承担“财务初审”节点，不是只做导出统计。目标流程：

```text
员工发起报账 -> 杨总审批 -> 财务报销机器人初审 -> 财务复审 -> 出纳/支付
```

机器人初审职责：

1. 读取当前审批实例、表单字段、原始附件和评论区补传附件。
2. OCR 识别发票，核对发票金额与报账金额是否一致。
3. 检查发票是否重复上传、重复报销，必要时对照员工预存发票和抵扣台账。
4. 全部正常时，调用钉钉审批任务接口自动同意，让流程进入财务复审。
5. 存在缺票、金额不一致、重复票或识别失败时，不自动通过；通知财务，让员工在评论区补充上传新发票或说明。
6. 员工补传后，机器人再次读取 `operation_records[].attachments[]` 里的评论附件，复检通过后再同意。

当前落地方式先不把应用本身塞进审批人列表，而是固定由覃献芳登录这个本地工具。工具使用她的钉钉授权身份读取“日常报销”里当前等她处理的审批任务，只处理已经过杨总同意、并且流程走到覃献芳节点的单据。后续 OCR 和查重规则接好以后，再由工具辅助她完成初审动作。

## Local setup

```bash
cd scripts-tools/dingtalk_finance_bot
cp .env.example .env
```

把 `.env` 里的 `DINGTALK_APP_SECRET` 和时间范围改成本地真实值。`DINGTALK_PROCESS_CODE` 已按后台“日常报销”表单 ID 填为 `PROC-DF1F74D9-C010-4683-B8F8-9C441A2F60AD`。默认频道名称是“财务报销机器人”。`.env` 和 `output/` 已被 `.gitignore` 排除，不要提交真实 Secret 或附件。

下载审批钉盘附件时还需要本地配置初审机器人账号：

- `DINGTALK_OPERATOR_USER_ID`：授权下载审批附件用的 `userId`。
- `DINGTALK_OPERATOR_UNION_ID`：获取存储下载地址用的 `unionId`。

覃献芳专用工具还需要以下配置：

- `DINGTALK_FINANCE_REVIEWER_NAME=覃献芳`：网页登录只接受这个财务审核人。
- `DINGTALK_FINANCE_REVIEWER_UNION_ID`：建议填覃献芳的 `unionId`，这样能严格拒绝其他账号。
- `DINGTALK_FINANCE_REVIEWER_USER_ID`：自动轮询时必填；网页手动扫描可以在覃献芳 OAuth 登录后自动用 `unionId` 换取。
- `DINGTALK_FINANCE_GENERAL_MANAGER_KEYWORDS=杨国民,杨总`：用于判断杨总节点是否已同意。

本地测试时，先让覃献芳在钉钉授权页登录。若误用其他同事账号，工具会拒绝进入扫描页。

## NAS Web Tool

部署成 NAS 内网工具时，启动：

```bash
scripts-tools/dingtalk_finance_tool.sh
```

默认监听：

```text
http://0.0.0.0:6192/
```

在 NAS 上可以通过内网 IP 打开：

```text
http://<NAS-IP>:6192/
```

网页工具提供：

1. 扫描覃献芳当前待审报销单。
2. 按员工、金额或关键词筛选。
3. 查看最近初审报告。
4. 导出财务初审汇总表。
5. 下载本地 JSON 报告和导出的 Excel 汇总包。

网页入口使用钉钉账号授权登录，不让财务记本地口令。部署时在 `.env` 配置工具访问地址：

```bash
DINGTALK_FINANCE_TOOL_AUTH_MODE=dingtalk_oauth
DINGTALK_FINANCE_TOOL_BASE_URL=http://<NAS-IP>:6192
```

然后到钉钉开放平台应用的登录/分享或 OAuth 配置里登记回调地址：

```text
http://<NAS-IP>:6192/auth/dingtalk/callback
```

如果只允许指定财务账号访问，可以把这些账号的 `unionId` 逗号分隔写入：

```bash
DINGTALK_FINANCE_ALLOWED_UNION_IDS=unionid_a,unionid_b
```

`DINGTALK_FINANCE_TOOL_TOKEN` 只作为脚本/header 应急兜底，不会出现在网页登录界面；不要把它提交到仓库。

如果只是本地开发测试、暂时没有覃献芳账号，可以临时跳过网页登录：

```bash
DINGTALK_FINANCE_TOOL_AUTH_MODE=open
```

`open` 模式下，“扫描待审”会直接使用 `.env` 里的 `DINGTALK_FINANCE_REVIEWER_USER_ID` 和 `DINGTALK_FINANCE_REVIEWER_UNION_ID`。部署到 NAS 给财务使用前，把它改回 `dingtalk_oauth`。

如需改端口：

```bash
DINGTALK_FINANCE_TOOL_PORT=6194 scripts-tools/dingtalk_finance_tool.sh
```

## DingTalk Stream Event Trigger

推荐触发方式是在钉钉开发者后台的应用详情页打开 **开发配置 > 事件订阅**，选择 `Stream模式推送`。NAS 容器会通过 `dingtalk-stream` 和钉钉建立长连接，不需要公网回调地址，也不会像轮询那样定时消耗审批列表接口。

容器默认用 `serve-tool --stream-events` 同时启动 Web 工具和 Stream 监听；也可以在 `.env` 中显式控制：

```bash
DINGTALK_FINANCE_STREAM_ENABLED=1
```

后台事件触发后，工具只会读取对应审批实例并执行发票初审；仍然要求“覃献芳待审”和“杨总已同意”两个条件同时成立。它不会自动同意或拒绝真实 OA 审批。

部署后可用 `/api/status` 查看 `stream_events` 状态，例如是否 running、最近收到的事件、最近处理的审批实例和错误信息。第一次连接时，需要在钉钉开发者后台点击“已完成接入，验证连接通道”，保存推送方式后再订阅审批相关事件。

如需让 NAS 常驻进程定时轮询，先确认钉钉应用 API 配额足够。轮询只是不能接入 OA 回调时的兜底方式，默认关闭，避免没有待审单时也持续消耗接口调用量：

```bash
DINGTALK_FINANCE_AUTO_SCAN_SECONDS=1800 \
DINGTALK_FINANCE_AUTO_SCAN_SKIP_REVIEWED=0 \
scripts-tools/dingtalk_finance_tool.sh
```

默认 `DINGTALK_FINANCE_AUTO_SCAN_SECONDS=0`，只在网页里手动扫描，不会自动拉取审批。

自动轮询没有浏览器登录态，所以必须预先配置 `DINGTALK_FINANCE_REVIEWER_USER_ID`。网页手动扫描则以覃献芳当前登录账号解析出来的 `userId` 为准。

启动脚本会读取 `scripts-tools/dingtalk_finance_bot/.env`，因此也可以直接把以下值写在本地 `.env` 里：

```bash
DINGTALK_FINANCE_TOOL_AUTH_MODE=open
DINGTALK_FINANCE_OCR_URL=
DINGTALK_FINANCE_OCR_TIMEOUT_SECONDS=30
DINGTALK_FINANCE_STREAM_ENABLED=1
DINGTALK_FINANCE_AUTO_SCAN_SECONDS=0
DINGTALK_FINANCE_AUTO_SCAN_LIMIT=20
DINGTALK_FINANCE_AUTO_SCAN_SKIP_REVIEWED=0
```

`AUTO_SCAN_SKIP_REVIEWED=0` 是为了反复检查已经生成过报告的单据，方便捕捉员工在评论区补传的新发票。只有显式把 `DINGTALK_FINANCE_AUTO_SCAN_SECONDS` 设为大于 0 时，后台服务才会启动后立刻扫一次，然后再按间隔继续扫。生产环境优先使用 Stream 事件订阅，其次才是 OA 自动化 HTTP 回调或网页手动扫描。

## DingTalk Work Notifications

工具可以在生成初审报告后，通过企业内部应用给覃献芳发送钉钉工作通知。这个通知只提示财务查看报告，不会自动同意或拒绝审批。

```bash
DINGTALK_FINANCE_NOTIFY_ENABLED=1
DINGTALK_FINANCE_NOTIFY_USER_IDS=replace_with_qin_xianfang_user_id
DINGTALK_FINANCE_NOTIFY_ON=issues
```

`DINGTALK_FINANCE_NOTIFY_ON=issues` 表示只有发现阻塞项或待处理问题时才推送，避免正常单据刷屏；改成 `all` 会在每次新报告生成时都推送。若没有填写 `DINGTALK_FINANCE_NOTIFY_USER_IDS`，工具会默认发给 `DINGTALK_FINANCE_REVIEWER_USER_ID`。

自动轮询会重复检查单据补票情况，所以通知会为每个审批实例写入 `notify_PROC-XXXXX.json` 标记，避免同一份报告按轮询间隔重复发送。

## Local OCR Service

发票 OCR 只接本地或内网自建服务，不把发票图片发送到外部云服务。财务工具侧只认一个简单 HTTP 协议：

```bash
DINGTALK_FINANCE_OCR_URL=http://127.0.0.1:6194/ocr
DINGTALK_FINANCE_OCR_TIMEOUT_SECONDS=30
```

启动本地 OCR 包装服务：

```bash
scripts-tools/dingtalk_finance_ocr_service.sh
```

默认监听：

```text
http://127.0.0.1:6194/
```

协议：

```text
POST /ocr
Content-Type: multipart/form-data
file=<invoice image/pdf bytes>
```

返回 JSON 任一格式都可以：

```json
{"text": "增值税电子普通发票\n价税合计(小写) ￥1315.98"}
```

或：

```json
{"ocr_text": "增值税电子普通发票\n价税合计(小写) ￥1315.98"}
```

工具下载附件后，会把本地文件发给这个 OCR 地址；OCR 文本写入报告里的附件记录，再进入金额核对。若 OCR 服务失败，报告会出现 `invoice_ocr_failed`，不会自动通过。

OCR 服务本体只调用本地命令。没有配置命令时，只支持上传 `text/plain` 做协议自测；真实图片/PDF 需要配置本机命令：

```bash
DINGTALK_FINANCE_OCR_COMMAND="tesseract {input} stdout -l chi_sim+eng --psm 6"
```

PDF 会先通过本地 `pdftoppm` 转成 PNG，再交给同一个 OCR 命令；没有 `pdftoppm` 时会尝试本地 ImageMagick `magick`。NAS 上建议安装：

```bash
# macOS/Homebrew
brew install tesseract tesseract-lang poppler

# Debian/Ubuntu NAS
apt install tesseract-ocr tesseract-ocr-chi-sim poppler-utils
```

也可以后续替换成 NAS 上的 PaddleOCR 包装命令。服务不会主动访问外部 OCR API。

## Reviewer Queue

本工具的主路径是“覃献芳待审队列”，不是全量导出。命令行也可以跑同一套筛选逻辑：

```bash
uv run python scripts-tools/dingtalk_finance_bot/finance_bot.py reviewer-queue \
  --reviewer-user-id replace_with_qin_xianfang_user_id \
  --limit 20
```

筛选条件：

1. 审批实例属于当前 `DINGTALK_PROCESS_CODE` 和时间范围。
2. 详情里存在分配给覃献芳 `userId` 的待处理任务。
3. 操作记录里能匹配到 `杨国民` 或 `杨总` 的同意记录。
4. 通过后才生成 `review_PROC-XXXXX.json` 初审报告；不符合条件的单据会写入 `reviewer_queue_YYYYMMDD_HHMMSS.json` 的 skipped 列表。

## Probe API

首个接口测试使用只读能力探针。它只验证应用鉴权、审批实例列表和一条审批详情，并报告附件下载身份、本地 OCR、历史发票台账、财务复核人和 Stream 配置；不会下载附件，也不会同意或拒绝审批：

```bash
uv run python scripts-tools/dingtalk_finance_bot/finance_bot.py capabilities
```

只有 `app_access_token`、`workflow_instance_read` 和（有样本时）`approval_detail_read` 通过，才说明当前应用至少具备首轮只读联调条件。`ready_for_initial_review=true` 还要求补齐附件下载身份、本地 OCR、财务复核人和有效的本地查重台账。

完整审批详情与评论附件字段探针：

```bash
uv run python scripts-tools/dingtalk_finance_bot/finance_bot.py probe
```

## Probe Comment Uploads

用于验证“审批评论区补传发票图片”是否会出现在审批实例详情里：

```bash
uv run python scripts-tools/dingtalk_finance_bot/finance_bot.py probe \
  --employee 蔡挺 \
  --keyword 采购网络设备及测试 \
  --amount 1315.98 \
  --comment-keyword giftgaff \
  --limit 20
```

探针会把匹配实例的原始详情和疑似附件字段写到 `output/probe_*/`。如果评论图片只返回文件 ID、下载信息不足，后台可能还需要补开 `Storage.DownloadInfo.Read`。

后台权限页目前没有单独的“审批评论读取”权限；已开通的 `Workflow.Instance.Read` 会先用于读取审批详情和操作记录。真实判断以这条探针输出为准：如果 `operationRecords` 或相邻字段里包含评论补传附件，机器人就能继续下载/识别；如果详情里完全没有评论图片字段，则需要改走消息/事件或钉盘文件下载链路。

已实测蔡挺关联的原报销单：评论补传附件会出现在 `operation_records[].attachments[]`，字段包含 `file_id`、`file_name`、`file_type`、`file_size`。因此机器人可以识别“财务要求员工在评论区补传的新票据”。

## Resolve Approval Users

下载接口需要同时使用审批里的 `userId` 和通讯录里的 `unionId`。可先对单个审批实例或筛选结果运行：

```bash
uv run python scripts-tools/dingtalk_finance_bot/finance_bot.py users \
  --instance-id PROC-XXXXX
```

也可以沿用评论附件探针的筛选条件：

```bash
uv run python scripts-tools/dingtalk_finance_bot/finance_bot.py users \
  --employee 蔡挺 \
  --keyword 采购网络设备及测试 \
  --limit 20
```

该命令只读取审批和通讯录详情，用于确认哪个 `userId/unionId` 可以作为机器人初审节点的操作身份。

文件下载链路已按官方接口接入到客户端：

1. `Workflow.Instance.Write`：普通审批附件下载，`POST /v1.0/workflow/processInstances/spaces/files/urls/download`。该接口可下载表单附件。
2. `Workflow.Instance.Write`：授权下载审批钉盘文件，`POST /v1.0/workflow/processInstances/spaces/files/authDownload`。
3. `Storage.DownloadInfo.Read`：获取文件下载信息，`POST /v1.0/storage/spaces/{spaceId}/dentries/{dentryId}/downloadInfos/query`。
4. 使用返回的 `downloadUri` 或 `resourceUrls` 下载到本地，再进入 OCR 和查重。

如果评论附件没有返回 `spaceId`，客户端会先调用 `POST /v1.0/workflow/processInstances/spaces/infos/query` 获取审批钉盘空间。

普通版“下载审批附件”接口的官方说明是：评论中上传的附件 `fileId` 暂不支持获取下载链接。OA 高级版接口 `POST /v1.0/workflow/premium/processInstances/spaces/files/urls/download` 支持评论附件下载，并需要 `Premium.Workflow.ReadWrite.All`。当前真实探针已验证：评论区附件元数据可读，但普通版下载会受到限制，需后续确认是否开通 OA 高级版或改走人工补充/其他文件来源。

自动初审通过的接口也已按官方路径封装：`POST /v1.0/workflow/processInstances/execute`。该动作会改变真实审批状态，当前脚本没有暴露命令行入口，后续接 OCR/查重规则后再由机器人流程调用。

## Initial Review Dry Run

用于单个审批实例的“财务初审”预检，不会调用真实同意/拒绝接口：

```bash
uv run python scripts-tools/dingtalk_finance_bot/finance_bot.py review \
  --instance-id PROC-XXXXX
```

该命令会：

1. 读取审批详情和附件字段。
2. 下载能通过当前权限下载的附件。
3. 对本地附件计算 SHA-256。
4. 与本地 `invoice_ledger.local.json` 或 `--ledger` 指定台账做重复哈希检查。
5. 读取附件旁边的 OCR 文本 sidecar，提取发票金额并与审批报销金额核对。
6. 生成 `output/review_PROC-XXXXX.json`，标记是否有缺附件、下载失败、重复票据、评论补传附件下载受限、OCR 未配置、金额不一致等问题。

本阶段 `can_auto_approve` 固定为 `false`。等发票号码/代码查重接好之后，再把“无异常自动同意”接到已封装的审批任务接口。

本地还没有云 OCR 时，可以先给下载后的附件放一个同名 OCR 文本文件来测试金额核对：

```text
attachments/PROC-XXXXX/invoice.pdf
attachments/PROC-XXXXX/invoice.ocr.txt
```

`invoice.ocr.txt` 示例：

```text
增值税电子普通发票
价税合计(小写) ￥1315.98
```

也支持 `invoice.pdf.txt` 或 `invoice.txt`。后续接云 OCR 时，只要把识别文本写入附件字段 `ocr_text`，同一套金额核对逻辑会继续生效。

本地查重台账示例：

```json
{
  "records": [
    {
      "sha256": "replace_with_invoice_file_sha256",
      "source_instance_id": "PROC-OLD",
      "employee": "蔡挺"
    }
  ]
}
```

## OA Automation Callback

钉钉 OA 自动化的“HTTP 请求”动作可以作为备选触发器：当“日常报销”流程到达指定节点时，请求 DC-Agent 的回调地址，由本工具读取该审批实例并生成初审报告。

本地启动回调服务：

```bash
uv run python scripts-tools/dingtalk_finance_bot/finance_bot.py serve-callback \
  --host 0.0.0.0 \
  --port 6193
```

默认接收路径：

```text
POST /dingtalk/finance/oa-callback
```

建议在 `.env` 配置 `DINGTALK_OA_CALLBACK_TOKEN`，然后在钉钉 OA 自动化 HTTP 请求里用以下任一方式携带：

```text
https://your-public-host/dingtalk/finance/oa-callback?token=replace_with_local_shared_callback_token
```

或请求头：

```text
X-Dingtalk-Finance-Token: replace_with_local_shared_callback_token
```

请求体至少要包含审批实例 ID，字段名可以是 `processInstanceId`、`process_instance_id`、`procInsId` 或 `instance_id`。示例：

```json
{
  "source": "dingtalk_oa_automation",
  "processInstanceId": "PROC-XXXXX"
}
```

回调只会执行 `review` 预检并返回 `status`、`issue_codes`、`recommendation` 和 `report_path`。它不会自动同意审批；自动通过要等 OCR 金额核对和发票号码/代码查重接好后再开启。

注意：钉钉后台发起 HTTP 请求时需要访问一个钉钉云端可达的 URL，`localhost` 或内网地址通常不可用。如果不准备开放公网入口，可以先把 OA 自动化配置为发通知，机器人侧使用网页手动扫描做初审；只有在确认 API 配额可承受时才启用低频定时轮询。

## Local Polling Fallback

如果公司暂时不能开放公网回调地址，就先使用本地轮询。该模式由 DC-Agent 主动拉取“日常报销”审批实例，匹配到目标单据后执行同一套 dry-run 初审，不需要让钉钉云端访问本机。

扫描当前配置时间范围内最近 20 条：

```bash
uv run python scripts-tools/dingtalk_finance_bot/finance_bot.py poll-once
```

也可以先按员工、金额或关键词缩小范围：

```bash
uv run python scripts-tools/dingtalk_finance_bot/finance_bot.py poll-once \
  --employee 蔡挺 \
  --keyword 采购网络设备及测试 \
  --limit 20
```

`poll-once` 会生成：

1. 每个匹配审批实例的 `output/review_PROC-XXXXX.json`。
2. 本次扫描汇总 `output/poll_YYYYMMDD_HHMMSS.json`。

默认会重复检查最近单据，方便捕捉员工在评论区补传的新发票。如果只是想跳过已有 review 报告的单据，可以加：

```bash
uv run python scripts-tools/dingtalk_finance_bot/finance_bot.py poll-once --skip-reviewed
```

## Export Excel

```bash
uv run python scripts-tools/dingtalk_finance_bot/finance_bot.py export
```

也可以指定单个审批实例：

```bash
uv run python scripts-tools/dingtalk_finance_bot/finance_bot.py export --instance-id PROC-XXXXX
```
