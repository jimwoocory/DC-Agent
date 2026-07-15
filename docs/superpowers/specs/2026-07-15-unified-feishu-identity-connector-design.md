# 飞书统一身份与授权连接器工程设计

## 1. 目标与边界

本设计把现有的员工目录、公司权限库、Vercel 员工授权镜像和多条飞书 OAuth 路径收敛为一套可审计的连接器契约，同时保留各系统的事实边界：

- 飞书是组织事实来源：员工、部门树、岗位、上下级和在职状态。
- DC-Agent 的 `permissions.db` 是业务授权事实来源：系统权限、数据范围、临时授权、来源和启停状态。
- `dianchi` 的 Upstash Redis 只保存签名投影、短期授权事务和用途隔离的 Token grant，不成为新的主数据源。
- 员工网页登录、飞书客户端 H5、公司桌面端和云文档授权共享身份关联，但 Cookie、桌面 Session、云文档 Token 互不复用。
- 不增加人工审批节点。确定性身份和组织事实自动同步；有冲突时失败关闭并进入审计/告警，不把部门或职务猜成业务权限。

## 2. 已确认问题

1. 生产 OAuth 读取 Upstash 快照，现网缺少稳定的每日补偿同步任务。
2. 快照 schema v1 只有身份、组织身份和旧 `role`，没有 `dc_permission_assignments`。
3. Vercel 管理鉴权仍接受 `employee_role=admin` 和硬编码 open_id，绕开公司权限库最终裁决。
4. `snapshot_missing`、`snapshot_invalid`、`snapshot_stale` 和 KV 故障共用身份拒绝页面，员工无法判断是账号问题还是系统可用性问题。
5. DC-Agent 云文档流程自己生成 state，却把回调指向要求签名 Cookie state 的公司主页；授权过期后可能落入员工桌面登录流程。
6. 云文档只保存 access token，没有 refresh、撤销、用途绑定和原 draft 回流的统一生命周期。

## 3. 目标架构

```text
飞书通讯录/组织事件                 DC-Agent permissions.db
        |                                   |
        | 组织事实                           | 业务授权事实
        v                                   v
DC-Agent 统一投影器 --签名快照/事件--> Vercel 统一连接器 + Upstash
        ^                                   |
        | 每日全量对账 + DB 变更触发          | purpose 路由
        |                                   v
   健康/审计状态        +--------------------+--------------------+
                       |                    |                    |
                员工网页登录          桌面一次性交接         云文档 grant
                独立 Cookie            独立 Session           独立 Token
                                                                  |
                                                                  v
                                                       原 draft/task 回流
```

### 3.1 统一身份键

规范主键为 `tenant_key + union_id`。投影记录同时维护：

- `employee_id`：公司内部稳定主键。
- `principal_key`：`feishu:<tenant_key>:<union_id>`；历史数据缺少 union_id 时使用带类型的 `user_id`/`open_id` 兼容键，并在健康状态中计数。
- `identities[]`：每个飞书应用的 `app_key`、`open_id`、`union_id`、`user_id` 和 provider subject。
- `device_bindings` 不进入公开员工投影，桌面安装实例只保存在短期/可撤销 grant 中。

现有库尚无 `app_key` 列时，schema v2 先把每条 `employee_identities` 记录投影为一项 identity，`provider` 作为兼容应用标签；后续迁移增加显式 `app_key`，不改变 `principal_key`。

### 3.2 授权上下文

每条员工记录带 `permissions[]`，字段与 `dc_permission_assignments` 保持一致：

```json
{
  "subject_id": "ou_xxx",
  "subject_type": "user",
  "permission": "dc_admin",
  "scope": "*",
  "source": "admins_id",
  "enabled": true,
  "updated_at": "2026-07-15T00:00:00+00:00"
}
```

投影器只把能够通过 `employee_id`、union_id、user_id 或应用 open_id 明确绑定到员工的用户权限纳入该员工记录。应用主体权限不投影给员工。授权判断必须同时满足：

1. 租户匹配；
2. 身份键匹配；
3. `status=active`；
4. 请求需要业务权限时，存在 subject 绑定正确、`enabled=true`、permission 和 scope 匹配的公司权限记录。

`department`、`relation_type` 和 `role` 只进入 RBAC/ABAC 上下文和展示，不直接产生 `dc_admin` 或工具权限。离职/停用优先于所有权限。

## 4. 同步协议

### 4.1 schema v2 快照

快照包含：

- `schema_version=2`
- `snapshot_id`：随机/时间有序同步 ID
- `generated_at`
- `sync_reason`：`scheduled_reconcile`、`identity_changed`、`permission_changed`、`manual_recovery`
- `records`
- `digest`

Vercel 在滚动发布期间可读取 v1，但只有 v2 能提供权限上下文。同步写入前校验 HMAC、时间偏差、字段白名单、主键唯一性、权限字段、摘要和版本顺序；Redis `SET` 完成单键原子替换。旧快照不能覆盖新快照，同一摘要重复提交幂等成功。

### 4.2 增量触发与全量对账

- `employees.db` 或 `permissions.db` 发生变化时由 launchd `WatchPaths` 触发一次完整小规模投影，达到事件级快速生效。
- `StartInterval=86400` 每 24 小时执行全量对账，修复漏事件和短暂网络失败。
- 同步 CLI 使用有限指数退避、互斥锁和结构化 JSON 日志；并发触发只允许一个进程推送。
- 真正的飞书通讯录事件到达后，先确定性更新组织镜像，再触发同一个投影器；事件处理和定时对账共享一个输出协议。
- 离职/停用事件优先执行，失败立即告警；现有 Session 在下一次受保护请求或刷新时重新检查状态。

## 5. OAuth purpose 与 Token 隔离

允许的 purpose：

| purpose | 入口 | 成功产物 | 禁止产物 |
|---|---|---|---|
| `employee_login` | 官方网页/公司主页 | 公司网页 Cookie | 云文档 refresh token、桌面 refresh token |
| `desktop_login` | 公司主页桌面入口 | 60 秒 handoff -> 桌面 Session/refresh | 网页 Cookie 深链、飞书 Token 深链 |
| `cloud_docs` | 飞书客户端 H5/办公工作台 | 60 秒 handoff -> 用途绑定 grant | 公司网页登录跳转、桌面 Session |

授权事务包含 `state`、`purpose`、`app_key`、`nonce`、`return_to`、`iat`、`exp`。规则：

- state 同时由 HttpOnly Cookie 签名并在 Upstash 保存一次性事务；回调使用 GETDEL 消费。
- `return_to` 必须命中 `FEISHU_OAUTH_RETURN_ORIGINS` 白名单，禁止开放跳转。
- 回调按 purpose 分流。无效/过期 state 只返回 OAuth 事务错误，不依据现有员工 Session 猜测并跳入桌面流程。
- `cloud_docs` 回调在连接器交换飞书 code 后，把 access/refresh token 写入用途绑定 grant，只把 60 秒 handoff code 带回原 draft。
- DC-Agent 后端消费 handoff，获得短期 access token 和不透明 grant token；refresh token 不离开连接器 KV。
- 刷新时旋转 grant token并重新检查员工状态；撤销删除 grant。Token scope 扩大必须重新 OAuth。

## 6. 云文档回流

```text
原 draft 点击云文档
  -> DC-Agent 生成 draft_nonce
  -> Vercel /auth/feishu/start?purpose=cloud_docs&return_to=<draft callback>
  -> 飞书 OAuth
  -> Vercel callback 消费统一 state，签发 handoff
  -> 回到原 draft callback?state=<draft_nonce>&handoff_code=<opaque>
  -> DC-Agent 后端消费 handoff
  -> ?cloud=1 回到原工作台并打开原选择器
```

access token 到期时，DC-Agent 使用 grant token 请求连接器刷新；刷新成功继续原请求。连接器或飞书临时不可用时保留现有 grant 并返回可重试的 503；只有 grant 无效、身份停用或刷新关系确定失效时才清理授权并从相同 draft 重新授权。draft/token 过期只提示原任务已过期，不跳转员工桌面登录。

## 7. 健康检查、错误码、审计与告警

### 7.1 健康状态

`/api/internal/employee-access/health` 只返回无个人信息的状态：

- `healthy`：快照存在、合法、低于告警年龄、KV 可用。
- `degraded`：快照超过 24 小时但仍小于 30 小时。
- `unavailable`：KV 故障、快照缺失/损坏/超过 30 小时。
- 字段：schema、age_seconds、record_count、permission_count、digest 前缀、last_sync_reason。

### 7.2 用户错误分级

| 类别 | HTTP | 标题 | 结构化码示例 |
|---|---:|---|---|
| 身份拒绝 | 403 | 员工账号未获授权 | `employee_not_found`、`tenant_mismatch` |
| 状态拒绝 | 403 | 员工账号不可用 | `status_pending`、`status_disabled` |
| 服务故障 | 503 | 员工授权服务暂不可用 | `snapshot_missing`、`snapshot_invalid`、`authorization_unavailable` |
| 数据过期 | 503 | 员工授权数据待恢复 | `snapshot_stale` |
| OAuth 事务 | 400/410 | 授权事务已失效 | `oauth_state_invalid`、`handoff_expired` |

页面展示结构化码和重试入口，日志记录 code、purpose、snapshot_id、匿名 subject hash 和 request id，不记录 access token、refresh token、Cookie 或 handoff code。

## 8. 回归矩阵

- 快照：缺失、损坏、过期、KV 故障、旧版本覆盖、幂等重传。
- 身份：tenant + union_id 主匹配、不同应用 open_id、兼容 user/open_id、冲突拒绝。
- 权限：启用/停用、scope、应用主体隔离、dc_admin 投影、部门变动不自动授予权限。
- 状态：active、pending、disabled、离职触发同步、现有 Session/桌面 refresh 撤权。
- 四入口：官方网页、飞书客户端 H5、公司主页 OAuth、桌面 handoff。
- 云文档：首次授权、access 刷新、refresh 失败、撤销、原 draft 回流、无效 state 不进入桌面流程。
- 运维：每日任务、DB 变更触发、并发锁、网络重试、健康接口和日志脱敏。

## 9. 发布顺序与回滚

1. 先发布兼容 v1/v2 的 Vercel 读取端、权限裁决、错误分级和健康接口。
2. 运行 v2 dry-run，核对员工数、身份键缺失数、权限绑定数和蔡挺的 `dc_admin:*`，不输出个人敏感值。
3. 手工推送首个 v2 快照并验证健康接口和登录。
4. 安装 launchd 的 WatchPaths + 每日补偿同步，验证一次按需运行并观察首个定时周期。
5. 发布 purpose OAuth、handoff/grant 刷新与 DC-Agent 云文档回流。
6. 灰度四入口和离职撤权，再扩大使用范围。

### 9.1 生产配置清单

Vercel `dianchi` 项目必须配置：

- `KV_REST_API_URL` + `KV_REST_API_TOKEN`，或等价 Upstash REST 变量；授权事务和 grant 禁止内存降级。
- `DESKTOP_EMPLOYEE_SYNC_SECRET`、`SITE_SESSION_SECRET`、`FEISHU_COMPANY_TENANT_KEY`。
- purpose 涉及的飞书应用 `APP_ID`/`APP_SECRET` 与统一 `FEISHU_REDIRECT_URI`。
- `FEISHU_OAUTH_RETURN_ORIGINS`：只列出实际 DC-Agent H5 origin，逗号分隔，不包含路径。
- 可选 `FEISHU_CLOUD_DOC_SCOPE`；默认 `offline_access drive:drive:readonly`，对应飞书应用必须已开通这些权限。

DC-Agent 运行机的 `~/.dc-agent.env` 必须配置：

- `DESKTOP_EMPLOYEE_SYNC_ENDPOINT=https://<company-origin>/api/internal/employee-access/sync`
- 与 Vercel 完全一致的 `DESKTOP_EMPLOYEE_SYNC_SECRET`
- `FEISHU_UNIFIED_CONNECTOR_BASE_URL=https://<company-origin>`
- 可选 `FEISHU_ATTACHMENT_OAUTH_APP=agent`

发布门禁按以下顺序执行，任一失败即停止，不安装同步任务：

1. Vercel 健康路由返回结构化 JSON，证明兼容 v1/v2 的读取端已经上线。
2. v2 dry-run 的记录数、身份数、权限绑定数与预期一致。
3. 手工推送 v2 后健康状态为 `healthy`，公司主页 active 员工登录和 `dc_admin:*` 裁决通过。
4. 安装 launchd 后验证一次 DB 变更触发、一次按需运行和首个每日周期。
5. 再启用 DC-Agent `FEISHU_UNIFIED_CONNECTOR_BASE_URL`，灰度云文档 purpose 与原 draft 回流。

回滚时按相反顺序：停用新调度、恢复 v1 exporter、Vercel 保留 v1 兼容读取；OAuth purpose 功能通过入口开关回退，旧员工登录与桌面路径不依赖云文档 grant。

## 10. 非目标

- 不把飞书部门归属直接转换为全部业务权限。
- 不把 SQLite 放入 Vercel 或网络共享供多进程写入。
- 不在 URL、浏览器 Cookie 或普通配置文件中传递长期飞书 Token。
- 不重做公司主页、桌面安装器或飞书消息界面。
- 不在本阶段新增人工审批流。
