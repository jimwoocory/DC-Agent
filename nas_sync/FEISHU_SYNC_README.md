# 飞书云文档同步脚本使用指南

## 功能说明

`feishu_sync.py` 自动从飞书云空间下载文件到 NAS 的 `inbox` 目录，然后由 `watcher.py` 自动摄入到 AstrBot 知识库。

### 支持的文件类型

- **普通文件**：PDF、Markdown、纯文本
- **Word 文档**：自动导出为 Markdown
- **表格**：自动导出为 CSV
- **多维表格**：自动导出为 CSV

### 工作流程

```
飞书云空间
    ↓ feishu_sync.py 下载
    ↓
NAS: /nas_kb/inbox
    ↓ watcher.py 监听
    ↓
AstrBot 知识库 ✓
```

---

## 前置条件

1. **NAS 已挂载**
   ```bash
   cd /Users/dianchi/DC-Agent/nas_sync
   ./mount.sh mount
   ```

2. **watcher.py 已运行**
   ```bash
   python watcher.py
   ```

3. **飞书应用已配置**
   - App ID: `cli_a939424636799bc9`
   - App Secret: 已申请并获得（见下文）

4. **Drive API 权限已获得**
   - ✅ `drive:drive:readonly`（云空间文件查看）
   - ✅ `drive:file:download`（文件下载）
   - ✅ `docx:document:readonly`（新版文档内容）

---

## 安装和配置

### 1. 安装依赖

```bash
cd /Users/dianchi/DC-Agent/nas_sync

# 更新 requirements.txt（如需要）
pip install -r requirements.txt
```

### 2. 设置 App Secret

**方式 A：环境变量（推荐）**
```bash
export FEISHU_APP_SECRET='your_feishu_app_secret'
```

**方式 B：修改脚本**
```python
# 编辑 feishu_sync.py 第 92 行
self.feishu_app_secret = os.getenv('FEISHU_APP_SECRET', 'CClhKDFnefk9rMNkreFGZgHemkTuKJIU')
```

---

## 使用方法

### 执行一次同步

```bash
export FEISHU_APP_SECRET='your_feishu_app_secret'
python feishu_sync.py
```

### 测试模式（不实际下载）

```bash
python feishu_sync.py --dry-run
```

输出示例：
```
2026-04-21 16:40:00 - __main__ - INFO - ============================================================
2026-04-21 16:40:00 - __main__ - INFO - 开始同步飞书文件...
2026-04-21 16:40:00 - __main__ - INFO - 正在认证飞书应用...
2026-04-21 16:40:01 - __main__ - INFO - 认证成功
2026-04-21 16:40:02 - __main__ - INFO - 正在列出飞书云文件...
2026-04-21 16:40:03 - __main__ - INFO - 找到 5 个文件
2026-04-21 16:40:03 - __main__ - INFO - 正在同步: 营销方案.docx
2026-04-21 16:40:03 - __main__ - INFO - [测试模式] 将下载到: /Users/dianchi/nas_kb/inbox/营销方案.md
...
2026-04-21 16:40:05 - __main__ - INFO - ============================================================
2026-04-21 16:40:05 - __main__ - INFO - 同步完成: 成功 5, 失败 0
```

### 监听模式（持续同步）

```bash
# 每 5 分钟同步一次
python feishu_sync.py --watch --interval 300
```

按 `Ctrl+C` 停止。

---

## 定时运行（Cron 任务）

### 设置每小时同步一次

```bash
crontab -e
```

添加以下行：
```cron
# 每小时执行一次飞书同步
0 * * * * cd /Users/dianchi/DC-Agent/nas_sync && export FEISHU_APP_SECRET='your_feishu_app_secret' && python feishu_sync.py >> feishu_sync.log 2>&1
```

### 查看日志

```bash
tail -f /Users/dianchi/DC-Agent/nas_sync/feishu_sync.log
```

---

## 同步状态跟踪

脚本会在 `.feishu_sync_state.json` 中记录已同步的文件：

```json
{
  "cli_a939424636799bc9_file_123": {
    "name": "营销方案.docx",
    "modified_time": "2026-04-21T15:30:00Z",
    "synced_at": "2026-04-21T16:40:05.123456"
  }
}
```

**作用**：
- 避免重复下载
- 检测文件更新时自动重新同步
- 追踪同步历史

### 强制重新同步

删除 `.feishu_sync_state.json` 后重新运行：
```bash
rm /Users/dianchi/DC-Agent/nas_sync/.feishu_sync_state.json
python feishu_sync.py
```

---

## 故障排查

### 认证失败

**问题**：`认证失败: app_id not found` 或 `invalid app_secret`

**解决**：
1. 检查 App ID 和 App Secret 是否正确
2. 确认 openclaw 应用在飞书开发者后台存在且已发布
3. 确认权限申请已被批准

### 文件列表为空

**问题**：`找到 0 个文件`

**解决**：
1. 确认飞书云空间有文件
2. 确认 `drive:drive:readonly` 权限已获得
3. 检查网络连接

### 下载失败

**问题**：`下载失败 (xxx_token): ...`

**解决**：
1. 确认 `drive:file:download` 权限已获得
2. 检查网络连接
3. 确认 NAS inbox 目录可写

### inbox 目录权限不足

**问题**：`Permission denied: /Users/dianchi/nas_kb/inbox`

**解决**：
```bash
# 重新挂载 NAS
cd /Users/dianchi/DC-Agent/nas_sync
./mount.sh umount
./mount.sh mount
```

---

## 高级配置

### 限制同步的文件类型

编辑 `config.yaml` 中的 `supported_extensions`：

```yaml
watch:
  supported_extensions:
    - .pdf
    - .md
    - .docx
```

### 自定义导出格式

编辑 `feishu_sync.py` 中的 `FILE_TYPE_MAP`：

```python
FILE_TYPE_MAP = {
    'file': 'file',           # 普通文件
    'docx': 'pdf',            # Word 导出为 PDF（默认 markdown）
    'sheet': 'xlsx',          # 表格导出为 Excel
    'bitable': 'csv',         # 多维表格导出为 CSV
}
```

---

## 完整工作流示例

### 场景：自动同步员工写的运营方案

1. **员工在飞书创建文档**
   ```
   飞书云空间 → 营销运营 → 2026年Q2方案.docx
   ```

2. **定时同步开始**
   ```bash
   python feishu_sync.py  # 运行一次或定时运行
   ```

3. **文件被下载到 NAS**
   ```
   /nas_kb/inbox/2026年Q2方案.md
   ```

4. **watcher.py 自动检测并上传**
   ```
   AstrBot 知识库 → 文件已索引
   ```

5. **机器人可以回答相关问题**
   ```
   用户：Q2 的运营方案是什么？
   openclaw 机器人：根据知识库，Q2 运营方案包括...
   ```

---

## 性能和限制

- **并发限制**：飞书 API 限制为 10 请求/秒
- **文件大小**：单文件下载超时时间为 30 秒
- **内存占用**：流式下载，适合大文件
- **网络要求**：需要能访问 open.feishu.cn

---

## 后续优化

将来可以添加的功能：

- [ ] 增量同步（只下载新增或修改的文件）
- [ ] 文件夹映射到不同知识库
- [ ] 自动删除已处理的文件
- [ ] Webhook 实时通知（文件创建时立即同步）
- [ ] 文件预处理（提取文本、生成摘要）
- [ ] 可视化同步仪表板

---

## 常见问题

**Q: 如何只同步某个文件夹？**

A: 飞书 API 支持按文件夹 token 过滤。编辑 `feishu_sync.py` 的 `sync_once()` 方法，添加 `folder_token` 参数。

**Q: 同步冲突了怎么办？**

A: 脚本会自动添加数字后缀避免覆盖。例如 `方案.md` → `方案_2.md`。

**Q: 能否只下载特定类型的文件？**

A: 可以，在 `config.yaml` 中修改 `supported_extensions`。

**Q: 下载下来的 Word 文档格式不对怎么办？**

A: 飞书默认将 Word 导出为 Markdown。如需 PDF，可修改 `FILE_TYPE_MAP`。

---

## 联系与支持

如有问题，请检查：
1. 日志文件：`feishu_sync.log`
2. 同步状态：`.feishu_sync_state.json`
3. 飞书权限：开发者后台 → openclaw 应用 → 权限管理

---

**最后更新**：2026-04-21
