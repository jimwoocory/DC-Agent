# 飞书群组文档自动同步系统 - Method C 完整设置指南

## 📋 系统概述

**Method C** 是一个企业级文档自动收集系统，采用以下工作流：

```
员工在飞书群组分享文档
         ↓
feishu_sync_method_c.py 监听群组消息
         ↓
自动提取消息中的文档链接
         ↓
下载文件到 NAS 按部门分类
         ↓
watcher.py 自动检测并上传
         ↓
AstrBot 知识库索引完成
         ↓
机器人可以回答相关问题 ✓
```

**优势**：
- ✅ 员工无需额外操作，只需在群组分享即可
- ✅ 自动按部门分类，方便管理
- ✅ 实时同步，消息分享后立即下载
- ✅ 支持多种文件格式（PDF、Word、表格等）
- ✅ 无需升级 Feishu 付费版本

---

## 🔧 前置条件

### 1. 环境检查

```bash
# 确认 NAS 已挂载
ls -la /Users/dianchi/nas_kb

# 确认 watcher.py 已运行
ps aux | grep watcher.py

# 确认 Feishu API 权限已获得
# 需要以下权限：
# - drive:drive:readonly（云空间文件查看）
# - drive:file:download（文件下载）
# - docx:document:readonly（文档内容）
# - im:message（群组消息读取）✨ NEW
# - contact:user.email:readonly（用户信息）✨ NEW
```

### 2. 创建飞书群组

在飞书中创建一个专用群组用于文档分享：

```
群组名称: 📚 文档自动同步库
群组描述: 员工在此群组分享文档链接，系统自动同步到知识库
```

记下群组 ID（后续会用到）。获取方式：
- 打开群组
- 进入群组信息 → 更多
- 查看群组 ID（URL 中的 `chat_id` 或 ID 字段）

### 3. 部门映射配置

准备用户 ID 到部门的映射表。格式：
```json
{
  "ou_xxx1": "中台运营项目",
  "ou_xxx2": "品宣运营项目",
  "ou_xxx3": "品牌规范",
  "ou_xxx4": "营销素材",
  "ou_xxx5": "其他"
}
```

**获取用户 ID 的方法**：
1. 在 Feishu 后台查看员工信息
2. 或从历史消息中获取（API 返回的 `sender.id`）

---

## ⚙️ 安装和配置

### 第 1 步：检查依赖包

```bash
cd /Users/dianchi/DC-Agent/nas_sync

# 确保已安装所需包
pip install -r requirements.txt

# 如果缺少包，运行
pip install requests pyyaml watchdog
```

### 第 2 步：编辑 config.yaml

在 `config.yaml` 中添加 Feishu 和部门映射配置：

```yaml
# 在文件最后添加以下内容（保持 YAML 缩进）

# ============================================================
# Feishu Method C 配置（群组文档自动同步）
# ============================================================
feishu:
  # 群组 chat_id（从群组 URL 或群组信息中获取）
  # 例如: oc_xxx123456789
  group_chat_id: "oc_1234567890abcdef"

  # 用户 ID 到部门的映射
  # 获取用户 ID: 在 Feishu 后台查看或从 API 返回的 sender.id 获取
  department_mapping:
    # 示例：将不同的员工 ID 映射到各自的部门
    # 实际使用时替换为真实的用户 ID
    "ou_user001": "中台运营项目"
    "ou_user002": "品宣运营项目"
    "ou_user003": "品牌规范"
    "ou_user004": "营销素材"
    "ou_user005": "其他"
```

### 第 3 步：设置 Feishu App Secret

```bash
# 方式 A：环境变量（推荐，每次运行需要设置）
export FEISHU_APP_SECRET='your_feishu_app_secret'

# 然后执行同步
python feishu_sync_method_c.py --dry-run

# 或将其添加到 ~/.bashrc 或 ~/.zshrc（持久化）
echo "export FEISHU_APP_SECRET='your_feishu_app_secret'" >> ~/.zshrc
source ~/.zshrc
```

---

## 🚀 使用方法

### 1. 测试模式（不下载，只检查）

```bash
export FEISHU_APP_SECRET='your_feishu_app_secret'
cd /Users/dianchi/DC-Agent/nas_sync
python feishu_sync_method_c.py --dry-run
```

输出示例：
```
2026-04-21 16:40:00 - __main__ - INFO - ============================================================
2026-04-21 16:40:00 - __main__ - INFO - 开始从群组消息同步文档...
2026-04-21 16:40:01 - __main__ - INFO - 正在认证飞书应用...
2026-04-21 16:40:01 - __main__ - INFO - 认证成功
2026-04-21 16:40:02 - __main__ - INFO - 正在获取群组 oc_xxx 的消息...
2026-04-21 16:40:03 - __main__ - INFO - 找到 3 条消息
2026-04-21 16:40:03 - __main__ - INFO - 在消息中找到 2 个文件链接
2026-04-21 16:40:03 - __main__ - INFO - [测试模式] 将下载到: /Users/dianchi/nas_kb/inbox/中台运营项目/2026Q2方案_张三_20260421.md
2026-04-21 16:40:03 - __main__ - INFO - ============================================================
2026-04-21 16:40:03 - __main__ - INFO - 同步完成: 成功 1, 失败 0, 跳过 1
```

### 2. 执行一次同步

```bash
export FEISHU_APP_SECRET='your_feishu_app_secret'
python feishu_sync_method_c.py
```

这将：
- 连接到飞书 API
- 获取群组中的所有消息
- 提取消息中的文档链接
- 下载文件到 NAS
- 按部门分类存储
- 更新同步状态，下次不会重复下载

### 3. 监听模式（持续同步）

```bash
export FEISHU_APP_SECRET='your_feishu_app_secret'
python feishu_sync_method_c.py --watch --interval 300
```

- `--watch`：启用监听模式，持续运行
- `--interval 300`：每 300 秒（5 分钟）检查一次
- 按 `Ctrl+C` 停止

**在后台运行**：
```bash
nohup python feishu_sync_method_c.py --watch --interval 300 > feishu_sync_method_c.log 2>&1 &
```

---

## ⏰ 定时运行（Cron）

### 每小时同步一次

```bash
crontab -e
```

添加以下行：
```cron
# 每小时的第 0 分钟执行一次飞书 Method C 同步
0 * * * * cd /Users/dianchi/DC-Agent/nas_sync && export FEISHU_APP_SECRET='your_feishu_app_secret' && python feishu_sync_method_c.py >> feishu_sync_method_c.log 2>&1
```

### 每 5 分钟同步一次

```cron
# 每 5 分钟执行一次
*/5 * * * * cd /Users/dianchi/DC-Agent/nas_sync && export FEISHU_APP_SECRET='your_feishu_app_secret' && python feishu_sync_method_c.py >> feishu_sync_method_c.log 2>&1
```

### 查看日志

```bash
# 实时查看
tail -f /Users/dianchi/DC-Agent/nas_sync/feishu_sync_method_c.log

# 查看最后 50 行
tail -50 /Users/dianchi/DC-Agent/nas_sync/feishu_sync_method_c.log
```

---

## 📁 输出目录结构

同步后，NAS 上的目录结构如下：

```
/Users/dianchi/nas_kb/
├── inbox/
│   ├── 中台运营项目/
│   │   ├── 2026Q2方案_张三_20260421.md
│   │   └── 运营报告_李四_20260420.pdf
│   ├── 品宣运营项目/
│   │   ├── 品宣策略_王五_20260421.md
│   │   └── 海报设计_赵六_20260420.pdf
│   ├── 品牌规范/
│   │   └── VI指南_孙七_20260421.pdf
│   ├── 营销素材/
│   │   └── 文案库_周八_20260421.md
│   ├── 其他/
│   │   └── 杂项文件_其他员工_20260421.pdf
│   ├── processed/
│   │   └── （watcher.py 自动移入已处理的文件）
│   └── archive/
│       └── （手动归档的文件）
```

**文件命名规则**：`原始文件名_发送者姓名_YYYYMMDD.扩展名`

---

## 🔍 同步状态跟踪

脚本在 `.feishu_method_c_state.json` 中记录已处理的消息：

```json
{
  "processed_messages": {
    "om_xxx1234567890": {
      "timestamp": "2026-04-21T16:40:03.123456",
      "file_tokens": ["abc123", "def456"]
    },
    "om_xxx0987654321": {
      "timestamp": "2026-04-21T16:35:00.654321",
      "file_tokens": ["ghi789"]
    }
  }
}
```

### 强制重新同步所有消息

```bash
# 删除状态文件（会重新处理所有消息）
rm /Users/dianchi/DC-Agent/nas_sync/.feishu_method_c_state.json
python feishu_sync_method_c.py
```

---

## 🛠️ 故障排查

### 问题 1：找不到群组消息

**症状**：`找到 0 条消息`

**排查步骤**：
1. 确认 `group_chat_id` 配置正确
   ```bash
   # 在 Feishu 群组中查看 URL 或群组信息
   ```

2. 确认应用有 `im:message` 权限
   ```
   Feishu 后台 → openclaw 应用 → 权限管理 → 查找 "im:message"
   ```

3. 测试 API 连接
   ```bash
   python -c "from feishu_sync_method_c import *; \
   config = Config(); \
   feishu = FeishuClient(config.feishu_app_id, config.feishu_app_secret); \
   print(feishu.list_group_messages(config.group_chat_id))"
   ```

### 问题 2：无法识别文档链接

**症状**：`消息中没有找到文件链接`

**解决**：
- 确认分享的是飞书文件（drive/docx/sheet 等）
- 确认链接格式是标准 Feishu URL（以 `feishu.cn` 开头）
- 检查日志中是否有链接提取错误

### 问题 3：下载失败

**症状**：`下载失败 (xxx_token): ...`

**排查步骤**：
1. 确认应用有 `drive:file:download` 权限
2. 确认 NAS 目录权限
   ```bash
   chmod 755 /Users/dianchi/nas_kb/inbox
   ```
3. 检查网络连接
4. 查看详细错误日志

### 问题 4：部门分类不正确

**症状**：所有文件都进入 "其他" 目录

**解决**：
1. 检查 `config.yaml` 中的 `department_mapping` 配置
2. 确认用户 ID 正确（从 Feishu 后台或 API 返回确认）
3. 添加更多用户映射
   ```yaml
   department_mapping:
     "ou_xxx": "正确的部门名"
   ```

### 问题 5：认证失败

**症状**：`认证失败: invalid app_secret`

**解决**：
1. 确认 `FEISHU_APP_SECRET` 环境变量设置正确
   ```bash
   echo $FEISHU_APP_SECRET
   ```
2. 确认从 Feishu 后台复制的密钥没有多余空格
3. 如果使用 cron，确认环境变量设置在 crontab 中

---

## 📊 完整工作流示例

### 场景：同步中台运营部门的 Q2 方案

**步骤 1**：员工（张三）在飞书群组分享文档
```
在 "📚 文档自动同步库" 群组中：
[link] https://xxx.feishu.cn/docx/abc123xyz
这是我们的 2026 Q2 运营方案，请大家审阅
```

**步骤 2**：系统自动检测
```
feishu_sync_method_c.py --watch
→ 扫描到新消息
→ 提取文档链接
→ 获取张三的信息 → 部门：中台运营项目
→ 下载文档并转换为 Markdown
```

**步骤 3**：文件出现在 NAS
```
/Users/dianchi/nas_kb/inbox/中台运营项目/2026Q2方案_张三_20260421.md
```

**步骤 4**：watcher.py 自动检测
```
watcher.py 发现新文件
→ 上传到 AstrBot 知识库
→ 建立索引
→ 状态: ✓ 已处理，移入 processed/ 目录
```

**步骤 5**：机器人可以回答问题
```
用户: @bot Q2 的运营方案是什么？
机器人: 根据知识库，Q2 运营方案包括...
        (来自: 2026Q2方案_张三_20260421.md)
```

---

## 🔐 安全建议

1. **App Secret 保护**
   - 不要将 App Secret 写入代码
   - 使用环境变量或密钥管理服务
   - 不要提交到 Git 代码库

2. **群组访问控制**
   - 只有需要的员工加入文档同步群组
   - 定期检查群组成员
   - 删除离职员工

3. **文件权限**
   ```bash
   # 确保 NAS 目录权限适当
   chmod 755 /Users/dianchi/nas_kb/inbox
   chmod 700 /Users/dianchi/DC-Agent/nas_sync
   ```

4. **日志管理**
   - 定期清理日志（包含 App Secret）
   - 不要在日志中输出 App Secret
   - 限制日志访问权限

---

## 💡 常见问题

**Q: 能否只同步特定部门的消息？**
A: 可以。修改 `config.yaml` 中的 `department_mapping`，只保留需要的部门。

**Q: 支持哪些文件格式？**
A: 支持所有 Feishu 支持的格式：
- 普通文件：PDF、Word、纯文本
- Word 文档：自动导出为 Markdown
- 表格、多维表格：自动导出为 CSV
- 最新版文档（DOCX）：导出为 Markdown

**Q: 如何修改导出格式？**
A: 编辑 `feishu_sync_method_c.py` 中的 `FILE_TYPE_MAP`：
```python
FILE_TYPE_MAP = {
    'docx': 'pdf',    # Word 改为导出 PDF
    'sheet': 'xlsx',  # 表格改为 Excel
}
```

**Q: 能否同时运行多个群组同步？**
A: 可以。创建多个 cron 任务，使用不同的 `group_chat_id`。

**Q: 群组消息删除后会怎样？**
A: 系统只同步消息存在时的文件。如果消息被删除，已下载的文件不受影响。

**Q: 能否暂停同步？**
A: 可以。按 `Ctrl+C` 停止 watch 模式，或从 cron 中删除任务。

---

## 📞 获取帮助

如遇到问题，检查以下内容：

1. **日志文件**
   ```bash
   tail -f /Users/dianchi/DC-Agent/nas_sync/feishu_sync_method_c.log
   ```

2. **同步状态**
   ```bash
   cat /Users/dianchi/DC-Agent/nas_sync/.feishu_method_c_state.json
   ```

3. **Feishu 权限**
   - 后台地址：https://o0ain5w98jh.feishu.cn/admin/
   - 应用管理 → openclaw → 权限管理
   - 确认已获得必要权限

4. **NAS 连接**
   ```bash
   mount | grep nas_kb
   ```

---

**最后更新**: 2026-04-21
**版本**: Method C v1.0
