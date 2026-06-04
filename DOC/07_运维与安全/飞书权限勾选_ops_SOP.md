# 飞书 App 权限勾选 SOP（ops 操作手册）

> 这份文档给 ops 去飞书开放平台勾权限 + 填 DC-Agent 凭证。完成后 DC-Agent 几个关键功能从 "mode=disabled / v0" 升级到 "mode=v1 真 API"。

---

## 一、需要的权限 scope（按业务功能分组）

去 **[飞书开放平台](https://open.feishu.cn/app)** → 找到 DC-Agent 应用 → 「权限管理」→ 按下面对照勾。**勾完点"申请发布"提交审核**。

### 1. 资料查询（W3 / `feishu_resource_plugin`）

| 权限 scope | 用途 |
|---|---|
| `docx:document:read` | 读云文档全文（用户 @bot 查"客户应标"，bot 真读文档内容评分） |
| `bitable:app:read` | 读多维表格记录（同上） |
| `wiki:wiki:read`（可选） | 如果用户问的资料在 知识空间 里，启用这个 |

### 2. 临时建群 / 拉人（P2 / `chat_creator_plugin`）

| 权限 scope | 用途 |
|---|---|
| `im:chat` | `/chat new <群名> <成员...>` 真建群 |
| `im:chat.member` | `/chat invite @用户` 拉人进当前群 |

### 3. 员工目录同步（P3 / `concierge_plugin` 的 `/employees sync`）

| 权限 scope | 用途 |
|---|---|
| `contact:user.base:read` | 读员工基础信息（姓名/部门/岗位） |
| `contact:user.id:read` | 读员工 open_id（主键，必需） |
| `contact:department.base:read` | 读部门树（递归遍历部门拿全员） |

### 4. 已有的（建议确认还在）

这些 ops 之前应该勾过了，跑业务流的基本盘：

| 权限 scope | 用途 |
|---|---|
| `im:message` | 读群消息 |
| `im:message:send_as_bot` | 以 bot 身份发消息 |
| `im:resource` | 读群里附件/图片 |

---

## 二、勾完之后：填凭证到 yaml

权限审核通过后，飞书后台「凭证与基础信息」拿 `App ID` + `App Secret`，填进：

```bash
# 第一次填，先 copy 模板
cp /Users/dianchi/DC-Agent/data/feishu_whitelist.example.yaml \
   /Users/dianchi/DC-Agent/data/feishu_whitelist.yaml

# 编辑这两行（其他保持默认）
nvim /Users/dianchi/DC-Agent/data/feishu_whitelist.yaml
# feishu:
#   app_id: cli_xxxxxxxxxxxx
#   app_secret: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
#   enable: true
```

按业务需要继续填白名单：

```yaml
documents:
  - doc_token: docxXXXXXXXXXXXX      # 客户应标文档
    label: "客户应标"
  - doc_token: docxYYYYYYYYYYYY
    label: "标书模板"

tables:
  - app_token: bascnXXXXXXXX
    table_id: tblXXXXXXX
    label: "渠道清单"
```

---

## 三、重启 + 验证

```bash
launchctl unload ~/Library/LaunchAgents/io.astrbot.bot.plist
launchctl load   ~/Library/LaunchAgents/io.astrbot.bot.plist
sleep 10
grep -E "feishu_resource|chat_creator|concierge" /Users/dianchi/DC-Agent/astrbot.log | tail -10
```

**期望看到的关键行**：

```
[feishu_resource] 启动 mode=v1 白名单 X 条（doc N / table N / folder N）
[chat_creator] 启动 mode=v1（凭证已就绪，建群可用）
[concierge] FeishuClient 启动 → /employees sync 可用
```

> 关键词：**mode=v1**（不是 v0）+ **凭证已就绪**（不是 disabled）+ **/employees sync 可用**（不是 凭证缺失）

---

## 四、业务侧 smoke test（5 分钟）

在飞书群里 @ DC-Agent，验证 4 件事：

| 命令 / 触发 | 期望响应 |
|---|---|
| `@DC-Agent 查 客户应标` | 返回带文档内容 snippet 的 ★★★★ 评分（不再是元信息匹配） |
| `/chat new 测试群 ou_xxx ou_yyy` | 返回新群 chat_id + 飞书链接 |
| `/chat invite ou_zzz`（已在某群里发）| 在当前群把 ou_zzz 拉进来 |
| `/employees sync`（在 bot 私聊里发）| 返回 "扫描部门 N 个、新增 X、更新 Y、跳过 Z"，dashboard 进 plugin → concierge_plugin → 员工目录页 能看到全员 |
| `/me`（任何员工 @bot 发）| 返回该员工档案 + 长期记忆 |

---

## 五、常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `[feishu_resource] 启动 mode=v0` | `data/feishu_whitelist.yaml` 不存在 / `enable: false` | 改 yaml，重启 |
| `/employees sync` 返回 "未拿到任何部门" | 缺 `contact:department.base:read` 权限 | 飞书后台勾上 + 重申请发布 |
| `/chat new` 返回 "建群失败 99991403" | 缺 `im:chat` 或 app 未授权该群可见 | 飞书后台勾 `im:chat` + 把 app 加入相关群 |
| dashboard "插件管理" 看不到 hermes_bridge | _conf_schema.json 缺 | 这块已修，参考 [DOC/待办备忘_2026-05.md](待办备忘_2026-05.md) |

---

## 六、目前架构里依赖飞书的位置

| Plugin / Engine | 飞书 API 调用 | 凭证缺失时 fallback |
|---|---|---|
| `feishu_resource_plugin` | `docx.v1.document_block.alist` + `bitable.v1.app_table_record.alist` | mode=v0 仅元信息匹配（不读全文）|
| `chat_creator_plugin` | `im.v1.chat.acreate` + `im.v1.chat_members.acreate` | 命令返回 "凭证未就绪" 错误 |
| `concierge_plugin` | `contact.v3.department.alist` + `contact.v3.user.alist`（仅 `/employees sync` 调用） | `/employees sync` 不可用；其他功能继续用员工自我介绍 regex 抽取 |

---

**更新时间**：2026-05-14
**当前状态**：3 个 plugin 全部 mode=disabled / v0，等 ops 完成上面四步后会自动升到 v1。
