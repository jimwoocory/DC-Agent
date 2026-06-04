# 🚀 Method C 快速开始指南（5分钟内启动）

## 简单来说，Method C 是什么？

员工在飞书群组分享文档链接 → **自动下载** → **按部门分类** → **知识库自动索引** ✓

```
群组消息: [link] https://xxx.feishu.cn/docx/abc123
              ↓ (自动)
        NAS: /inbox/中台运营项目/文档_员工名_日期.md
              ↓ (自动)
        知识库: ✓ 索引完成 → 机器人可以回答相关问题
```

---

## ⚡ 最快 5 分钟启动

### 第 1 步：准备环境（1分钟）

```bash
# 1. 进入目录
cd /Users/dianchi/DC-Agent/nas_sync

# 2. 检查依赖
pip install requests pyyaml

# 3. 设置 App Secret
export FEISHU_APP_SECRET='your_feishu_app_secret'
```

### 第 2 步：获取配置（2分钟）

有两种方式：

**方式 A：使用自动配置工具（推荐）**
```bash
python method_c_setup.py
```
这个工具会：
- ✓ 自动连接 Feishu API
- ✓ 列出所有群组，让你选择
- ✓ 提取群组成员和部门信息
- ✓ 自动生成配置并保存到 config.yaml

**方式 B：手动配置**
1. 打开 `method_c_config_template.yaml`
2. 按照注释填写你的群组 ID 和用户映射
3. 复制内容到 `config.yaml` 末尾

### 第 3 步：测试（1分钟）

```bash
# 运行测试，检查配置是否正确（不会下载任何文件）
python feishu_sync_method_c.py --dry-run
```

预期输出：
```
✓ 认证成功
✓ 正在获取群组 oc_xxx 的消息...
✓ 找到 3 条消息
✓ 在消息中找到 2 个文件链接
✓ 同步完成: 成功 1, 失败 0, 跳过 1
```

### 第 4 步：启动（1分钟）

```bash
# 一次性同步
python feishu_sync_method_c.py

# 或启动监听模式（推荐用于生产环境）
python feishu_sync_method_c.py --watch --interval 300
```

---

## 📋 配置详解（供参考）

如果使用自动配置工具，会自动生成以下内容：

```yaml
feishu:
  group_chat_id: "oc_1234567890abcdef"
  
  department_mapping:
    "ou_user001": "中台运营项目"
    "ou_user002": "中台运营项目"
    "ou_user003": "品宣运营项目"
    "ou_user004": "品牌规范"
    "ou_user005": "营销素材"
```

其中：
- `group_chat_id`：群组 ID（从群组 URL 获取）
- `department_mapping`：员工 ID → 部门名称的映射

---

## 🎯 完整工作流示例

### 场景：同步员工的 Q2 方案

**1️⃣ 员工操作（在飞书群组）**
```
在 "📚 文档自动同步库" 群组中发送：

这是我们的 2026 Q2 运营方案，请审阅：
https://xxx.feishu.cn/docx/abc123xyz
```

**2️⃣ 系统自动处理**
```bash
# 你的 cron 任务或监听进程自动运行：
python feishu_sync_method_c.py --watch
```

系统会：
- ✓ 扫描到新消息
- ✓ 提取文档链接
- ✓ 查询发送者信息 → "张三，中台运营项目"
- ✓ 下载文件并转换为 Markdown
- ✓ 保存到：`/nas_kb/inbox/中台运营项目/2026Q2方案_张三_20260421.md`

**3️⃣ watcher.py 自动摄入**
```
NAS 有新文件 → watcher.py 检测到
           → 上传到 AstrBot 知识库
           → 建立索引
           → 删除原文件（或移入 processed）
```

**4️⃣ 用户提问**
```
用户: @bot Q2 的运营方案是什么？

机器人: 根据知识库，Q2 运营方案包括：
      1. 目标：提升销售 30%
      2. 策略：优化产品线
      3. 预算：500万
      
      (来源: 2026Q2方案_张三_20260421.md)
```

---

## ⏰ 生产环境配置

### 监听模式运行（推荐）

```bash
# 在后台持续运行，每 5 分钟检查一次
nohup python feishu_sync_method_c.py --watch --interval 300 > feishu_sync_method_c.log 2>&1 &
```

### 定时任务运行（Cron）

```bash
# 编辑 crontab
crontab -e

# 添加以下行：
# 每小时的第 0 分钟执行（例如 9:00, 10:00, 11:00）
0 * * * * cd /Users/dianchi/DC-Agent/nas_sync && export FEISHU_APP_SECRET='your_feishu_app_secret' && python feishu_sync_method_c.py >> feishu_sync_method_c.log 2>&1

# 或每 5 分钟执行一次
*/5 * * * * cd /Users/dianchi/DC-Agent/nas_sync && export FEISHU_APP_SECRET='your_feishu_app_secret' && python feishu_sync_method_c.py >> feishu_sync_method_c.log 2>&1
```

查看日志：
```bash
tail -f /Users/dianchi/DC-Agent/nas_sync/feishu_sync_method_c.log
```

---

## ❓ 最常见的问题

### Q1: 怎么获取群组 ID？

**方式 1：从 URL 复制**
- 打开飞书群组
- 查看浏览器地址栏
- 找到 `/im/chat/oc_xxx` 部分
- `oc_xxx` 就是群组 ID

**方式 2：从群组信息查看**
- 右键点击群组名
- 查看群组信息
- ID 字段就是群组 ID

**方式 3：运行自动配置工具**
```bash
python method_c_setup.py
# 工具会列出所有群组，你只需选择
```

### Q2: 怎么获取用户 ID？

**方式 1：运行自动配置工具**
```bash
python method_c_setup.py
# 工具会列出所有成员及其 ID
```

**方式 2：从 Feishu 后台查看**
- 后台地址：https://o0ain5w98jh.feishu.cn/admin/
- 进入"通讯录" → 员工列表
- 每个员工的"用户 ID"字段

**方式 3：从消息中获取**
运行一次同步后，查看日志或消息内容，API 会返回发送者的 user_id

### Q3: 找不到文件怎么办？

**检查清单**：
1. ✓ 群组 ID 配置正确？
   ```bash
   cat config.yaml | grep group_chat_id
   ```

2. ✓ App Secret 设置正确？
   ```bash
   echo $FEISHU_APP_SECRET
   ```

3. ✓ 群组中有消息吗？
   - 在群组中发一条带链接的消息测试

4. ✓ 消息中有 Feishu 链接吗？
   - 确保是 `https://xxx.feishu.cn/` 开头的链接

5. ✓ NAS 目录可写？
   ```bash
   ls -la /Users/dianchi/nas_kb/inbox
   ```

### Q4: 消息很多，同步很慢怎么办？

目前系统处理策略：
- 首次运行会扫描所有消息
- 之后只处理新消息
- 已处理消息会记录在 `.feishu_method_c_state.json`

加速方法：
1. 增加检查间隔（减少 API 调用）
   ```bash
   python feishu_sync_method_c.py --watch --interval 600  # 10分钟检查一次
   ```

2. 定期清理状态（重新扫描）
   ```bash
   rm .feishu_method_c_state.json
   python feishu_sync_method_c.py
   ```

---

## 📂 文件组织

同步后，你的 NAS 会自动按部门组织：

```
/Users/dianchi/nas_kb/
├── inbox/
│   ├── 中台运营项目/
│   │   ├── 文档名_员工名_20260421.md
│   │   └── 报告_员工名_20260420.pdf
│   ├── 品宣运营项目/
│   │   └── 方案_员工名_20260421.md
│   ├── 品牌规范/
│   │   └── VI指南_员工名_20260420.pdf
│   ├── 营销素材/
│   │   └── 文案_员工名_20260421.md
│   ├── 其他/  # 未配置部门的员工文件
│   │   └── 文件_员工名_20260421.pdf
│   └── processed/  # 已摄入知识库的文件
└── ...
```

**文件名格式**：`原始名_发送者_日期.扩展名`

---

## 🔧 故障排查（快速版）

| 问题 | 检查 | 解决 |
|------|------|------|
| 认证失败 | `echo $FEISHU_APP_SECRET` | 重新 `export` |
| 找不到消息 | 群组 ID、权限 | 用 `method_c_setup.py` 验证 |
| 下载失败 | NAS 权限、网络 | `chmod 755 /Users/dianchi/nas_kb` |
| 重复下载 | 查看 `.feishu_method_c_state.json` | 删除状态文件重新开始 |
| 部门分类错误 | `config.yaml` 中的 `department_mapping` | 用 `method_c_setup.py` 重新生成 |

---

## 🎓 进阶用法

### 修改导出格式

编辑 `feishu_sync_method_c.py`，找到 `FILE_TYPE_MAP`：

```python
FILE_TYPE_MAP = {
    'docx': 'markdown',    # Word 改为 PDF：'pdf'
    'sheet': 'csv',        # 表格改为 Excel：'xlsx'
}
```

### 限制特定文件类型

编辑 `config.yaml`：

```yaml
watch:
  supported_extensions:
    - .pdf
    - .md
    - .docx
    # 移除不需要的类型
```

### 同时监听多个群组

创建多个 cron 任务，分别指向不同的配置或群组 ID

---

## 📞 获得帮助

**查看日志**：
```bash
tail -f /Users/dianchi/DC-Agent/nas_sync/feishu_sync_method_c.log
```

**完整文档**：
```bash
cat /Users/dianchi/DC-Agent/nas_sync/METHOD_C_SETUP.md
```

**检查配置**：
```bash
cat config.yaml | tail -20
```

**检查权限**：
```bash
# 在 Feishu 后台查看：
# https://o0ain5w98jh.feishu.cn/admin/
# 应用 → openclaw → 权限管理
# 确认已获得：drive:*, im:message, contact:user.email:readonly
```

---

## ✅ 检查清单

启动前确认：

- [ ] Python 3.6+ 已安装
- [ ] 依赖包已安装：`pip install requests pyyaml`
- [ ] NAS 已挂载：`ls /Users/dianchi/nas_kb`
- [ ] `config.yaml` 已配置（或用 `method_c_setup.py` 生成）
- [ ] `FEISHU_APP_SECRET` 已设置：`echo $FEISHU_APP_SECRET`
- [ ] 群组中有测试消息（带 Feishu 链接）
- [ ] `watcher.py` 已运行或即将启动
- [ ] NAS inbox 目录可写

---

## 🎉 启动！

所有准备就绪？运行：

```bash
export FEISHU_APP_SECRET='your_feishu_app_secret'
cd /Users/dianchi/DC-Agent/nas_sync

# 测试
python feishu_sync_method_c.py --dry-run

# 启动监听
python feishu_sync_method_c.py --watch --interval 300
```

完成！现在员工在群组分享的任何文档都会**自动同步到知识库**。

---

**版本**: Method C v1.0  
**最后更新**: 2026-04-21  
**状态**: ✅ 完全可执行
