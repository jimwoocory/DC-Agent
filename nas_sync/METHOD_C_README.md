# 飞书群组文档自动同步系统 (Method C)

## 📘 文档导航

本系统包含以下文件和文档：

### 📖 快速开始（选择你的速度）

| 文件 | 说明 | 适合人群 |
|------|------|--------|
| **QUICKSTART_METHOD_C.md** | ⚡ 5分钟快速启动 | 想快速上手的 |
| **METHOD_C_SETUP.md** | 📋 完整详细文档 | 需要全面了解的 |

### 🔧 配置文件和工具

| 文件 | 说明 |
|------|------|
| `feishu_sync_method_c.py` | 核心同步脚本（完全可执行）|
| `method_c_setup.py` | 自动配置助手（推荐使用）|
| `method_c_config_template.yaml` | 配置模板（手动配置参考）|
| `config.yaml` | 主配置文件（需要添加 feishu 部分）|

### 🔍 原始文档（供参考）

| 文件 | 说明 |
|------|------|
| `FEISHU_SYNC_README.md` | Method A 文档（原始方案）|
| `feishu_sync.py` | Method A 脚本（原始方案）|

---

## 🎯 System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Feishu 飞书                           │
│  ┌──────────────────────────────────────────────────┐   │
│  │  群组: "📚 文档自动同步库"                       │   │
│  │  员工发送消息 + 文档链接                        │   │
│  └──────────────────────────────────────────────────┘   │
└──────────────────────┬──────────────────────────────────┘
                       │ Feishu API (im:message)
                       ▼
┌─────────────────────────────────────────────────────────┐
│         feishu_sync_method_c.py（本脚本）             │
│  ┌──────────────────────────────────────────────────┐   │
│  │  1. 监听群组消息                                │   │
│  │  2. 提取文档链接                                │   │
│  │  3. 查询发送者和部门                            │   │
│  │  4. 下载文件 (Drive API)                        │   │
│  │  5. 按部门分类存储                              │   │
│  └──────────────────────────────────────────────────┘   │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              NAS SMB 挂载点                             │
│  /Users/dianchi/nas_kb/inbox/                           │
│  ├── 中台运营项目/                                      │
│  ├── 品宣运营项目/                                      │
│  ├── 品牌规范/                                          │
│  ├── 营销素材/                                          │
│  └── 其他/                                              │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼ watcher.py 监听
┌─────────────────────────────────────────────────────────┐
│           AstrBot 知识库管理系统                        │
│  ┌──────────────────────────────────────────────────┐   │
│  │  HTTP API: http://localhost:6185/api             │   │
│  │  - 上传文件                                      │   │
│  │  - 建立索引                                      │   │
│  │  - 相似度搜索                                    │   │
│  └──────────────────────────────────────────────────┘   │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              QQ/Feishu 聊天机器人                      │
│  用户提问 → 搜索知识库 → 生成回答                      │
└─────────────────────────────────────────────────────────┘
```

---

## ✨ 核心特性

### 自动化程度高
- ✅ 无需员工额外操作，只需在群组分享即可
- ✅ 自动提取、下载、分类、索引
- ✅ 支持多种文件格式自动转换

### 企业级可靠性
- ✅ 增量同步，不重复下载
- ✅ 状态跟踪（`.feishu_method_c_state.json`）
- ✅ 支持 Cron 定时执行或 Watch 持续监听
- ✅ 完整日志记录

### 灵活的部门管理
- ✅ 自动按部门分类存储
- ✅ 支持用户映射配置
- ✅ 支持多部门共存

### 无需付费升级
- ✅ 基于免费的 Drive API 权限
- ✅ 兼容 Feishu 商业版
- ✅ 成本低廉

---

## 🚀 30秒快速开始

```bash
# 1. 进入目录
cd /Users/dianchi/DC-Agent/nas_sync

# 2. 设置 App Secret
export FEISHU_APP_SECRET='your_feishu_app_secret'

# 3. 自动配置（推荐）
python method_c_setup.py
# → 选择要监听的群组
# → 选择是否保存配置

# 4. 测试
python feishu_sync_method_c.py --dry-run

# 5. 启动
python feishu_sync_method_c.py --watch --interval 300
```

完成！现在 Method C 系统已启动。

---

## 📋 前置条件检查

启动前确保满足以下条件：

```bash
# ✓ Python 已安装
python3 --version

# ✓ 依赖包已安装
pip install requests pyyaml

# ✓ NAS 已挂载
ls -la /Users/dianchi/nas_kb/inbox

# ✓ Feishu App 权限已获取
# 在 https://o0ain5w98jh.feishu.cn/admin/ 查看
# 需要：drive:drive:readonly, drive:file:download, im:message

# ✓ 群组已创建
# 在 Feishu 中创建 "📚 文档自动同步库" 群组

# ✓ watcher.py 已运行或即将启动
ps aux | grep watcher.py
```

---

## 🔧 三种配置方式

### 方式 1：自动配置（最推荐）⭐

```bash
python method_c_setup.py
```
- 优点：完全自动化，无需手动输入
- 缺点：无
- 推荐指数：★★★★★

### 方式 2：半自动配置

```bash
# 1. 编辑 config.yaml
nano config.yaml

# 2. 按照 method_c_config_template.yaml 的格式添加配置
# 3. 保存后运行测试
python feishu_sync_method_c.py --dry-run
```
- 优点：有完全控制权
- 缺点：需要知道用户 ID
- 推荐指数：★★★☆☆

### 方式 3：完全手动配置

1. 查看 `method_c_config_template.yaml`
2. 从 Feishu 后台查询群组 ID 和用户 ID
3. 编辑 `config.yaml`
4. 保存并测试

---

## 📊 运行模式

### 测试模式（不下载）
```bash
python feishu_sync_method_c.py --dry-run
```
用于验证配置是否正确，不会下载任何文件。

### 一次性同步
```bash
python feishu_sync_method_c.py
```
执行一次同步，然后退出。

### 监听模式（推荐用于生产）
```bash
# 每 5 分钟检查一次
python feishu_sync_method_c.py --watch --interval 300

# 或每 10 分钟检查一次（减少 API 调用）
python feishu_sync_method_c.py --watch --interval 600
```
持续监听群组消息，有新消息时立即下载。

### 后台运行
```bash
nohup python feishu_sync_method_c.py --watch --interval 300 \
  > feishu_sync_method_c.log 2>&1 &
```

### Cron 定时运行
```bash
# 编辑 crontab
crontab -e

# 添加任务（例如：每小时检查一次）
0 * * * * cd /Users/dianchi/DC-Agent/nas_sync && \
  export FEISHU_APP_SECRET='...' && \
  python feishu_sync_method_c.py >> feishu_sync_method_c.log 2>&1
```

---

## 🔍 监控和日志

### 实时查看日志
```bash
tail -f /Users/dianchi/DC-Agent/nas_sync/feishu_sync_method_c.log
```

### 查看同步状态
```bash
cat /Users/dianchi/DC-Agent/nas_sync/.feishu_method_c_state.json | jq
```

### 检查 NAS 文件
```bash
ls -la /Users/dianchi/nas_kb/inbox/

# 查看特定部门
ls -la /Users/dianchi/nas_kb/inbox/中台运营项目/
```

### 查看 watcher.py 日志
```bash
tail -f /Users/dianchi/DC-Agent/nas_sync/watcher.log
```

---

## 🆘 常见问题

### Q: 如何修改检查间隔？
A: 修改 `--interval` 参数（单位：秒）
```bash
python feishu_sync_method_c.py --watch --interval 600  # 10分钟
```

### Q: 如何重新扫描所有消息？
A: 删除状态文件后重新运行
```bash
rm .feishu_method_c_state.json
python feishu_sync_method_c.py
```

### Q: 如何修改导出格式？
A: 编辑 `feishu_sync_method_c.py` 中的 `FILE_TYPE_MAP`
```python
FILE_TYPE_MAP = {
    'docx': 'pdf',      # 改为导出 PDF
    'sheet': 'xlsx',    # 改为导出 Excel
}
```

### Q: 支持哪些文件格式？
A: 所有 Feishu 支持的格式
- Word 文档：导出为 Markdown/PDF
- 表格：导出为 CSV/Excel
- 新版文档：导出为 Markdown
- 普通文件：直接下载

### Q: 如何只监听某些部门的消息？
A: 在 `config.yaml` 中只配置需要的部门

### Q: 可以同时运行多个实例吗？
A: 可以，建议使用不同的 cron 任务或后台进程

---

## 📁 文件结构

```
/Users/dianchi/DC-Agent/nas_sync/
├── feishu_sync_method_c.py          # ⭐ 核心脚本
├── method_c_setup.py                # ⭐ 自动配置助手
├── method_c_config_template.yaml    # 配置模板
├── config.yaml                      # 主配置（需编辑）
├── QUICKSTART_METHOD_C.md           # ⭐ 快速开始
├── METHOD_C_SETUP.md                # 完整文档
├── METHOD_C_README.md               # 本文件
├── watcher.py                       # 知识库摄入脚本
├── feishu_sync.py                   # Method A（备用）
├── FEISHU_SYNC_README.md            # Method A 文档
├── .feishu_sync_state.json          # 同步状态（A）
├── .feishu_method_c_state.json      # 同步状态（C）
├── feishu_sync.log                  # 日志（A）
├── feishu_sync_method_c.log         # 日志（C）
└── mount.sh                         # NAS 挂载脚本
```

---

## 🔐 安全建议

1. **保护 App Secret**
   - 使用环境变量，不要写入代码
   - 不要提交到 Git
   - 定期轮换

2. **权限管理**
   - 只授予必要的 API 权限
   - 定期审查群组成员
   - 删除离职员工

3. **日志安全**
   - 定期清理日志
   - 不要在日志中输出敏感信息
   - 限制日志文件访问

---

## 💡 最佳实践

### 部门命名
使用明确的部门名称，与公司组织结构一致：
```yaml
department_mapping:
  "ou_001": "中台运营项目"       # 清晰
  "ou_002": "team_a"             # ❌ 避免英文缩写
  "ou_003": "品宣运营项目"       # ✓ 
```

### 群组使用
- 只在 "📚 文档自动同步库" 群组分享文档链接
- 不要分享非文档链接（会被忽略）
- 文件共享后，文件保持共享权限（以便下载）

### 定期检查
- 每周查看同步日志
- 每月检查 NAS 存储使用情况
- 定期验证知识库索引效果

---

## 🎓 进阶配置

### 多群组监听
创建多个 cron 任务：
```bash
# group_1
0 * * * * export FEISHU_APP_SECRET='...' && \
  python method_c_launcher.py --group-id 'oc_xxx1'

# group_2
0 * * * * export FEISHU_APP_SECRET='...' && \
  python method_c_launcher.py --group-id 'oc_xxx2'
```

### 自定义文件处理
修改 `feishu_sync_method_c.py` 的 `_download_file_by_token` 方法以添加自定义处理逻辑。

### 与其他系统集成
使用 webhook 或事件总线与其他系统集成（例如：通知 Slack、更新数据库等）。

---

## 🚀 下一步

**立即启动**：
```bash
python method_c_setup.py
```

**需要详细帮助**：
```bash
cat QUICKSTART_METHOD_C.md      # 5分钟快速指南
cat METHOD_C_SETUP.md           # 完整详细文档
```

**遇到问题**：
```bash
tail -f feishu_sync_method_c.log  # 查看实时日志
```

---

## 📞 支持和反馈

- 查看日志：`feishu_sync_method_c.log`
- 检查配置：`config.yaml`
- 查看同步状态：`.feishu_method_c_state.json`
- Feishu 后台：https://o0ain5w98jh.feishu.cn/admin/

---

**版本**: Method C v1.0  
**状态**: ✅ 完全可执行  
**最后更新**: 2026-04-21  
**维护者**: AstrBot 团队
