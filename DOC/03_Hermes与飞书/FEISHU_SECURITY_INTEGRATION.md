# 飞书安全报告后台管理对接方案

**文档创建时间**：2026-04-24  
**计划实现时间**：2026-05-01 onwards  
**状态**：Planning Phase

## 概述

本文档规划将飞书（Feishu/Lark）的 OpenClaw 安全报告功能集成到 jw-bot 系统中，通过后台管理界面实现安全数据的监控、分析和管理。

## 背景信息

### 当前发现

1. **系统基础**：jw-bot 已完成 Lark/飞书平台集成
   - 平台源代码位置：`astrbot/core/platform/sources/lark/`
   - 文件列表：
     - `lark_adapter.py` - 适配器
     - `lark_event.py` - 事件定义
     - `server.py` - 服务实现

2. **飞书侧功能**：OpenClaw 是飞书官方的安全防护系统
   - 提供周期性安全报告（如：安全周报 2026/04/17 - 2026/04/23）
   - 报告内容包括：风险防护、链接控制、设备管理、文件安全等
   - 目前主要通过管理后台及频道消息展示

3. **API 能力**：飞书开放平台提供相关 API
   - 官方文档：https://open.feishu.cn/?lang=zh-CN
   - 支持审计日志、权限管理等接口
   - 需要企业级权限配置

## 对接方案对比

### 方案 1：通过飞书审计日志 API（推荐）⭐

**优点**：
- 官方 API 支持，稳定性好
- 可获取系统级安全事件
- 支持实时查询和历史数据

**缺点**：
- 需要申请特定权限
- 需要企业级飞书账户
- 数据量可能较大

**实现步骤**：
1. 在飞书开放平台申请权限：`audit:audit_log:read`
2. 创建服务应用，获取 `app_id` 和 `app_secret`
3. 实现 API 调用模块
4. 定时拉取安全数据（推荐间隔：1小时或1天）
5. 数据存储到数据库
6. 在 hermes-webui 中展示

**关键 API**：
- `GET /open-apis/audit/v1/events` - 获取审计日志事件
- `GET /open-apis/audit/v1/audit_rules` - 获取审计规则

### 方案 2：消息监听 + 数据解析

**优点**：
- 实现简单，无需额外权限
- 利用现有的消息接收能力

**缺点**：
- 依赖于飞书官方消息推送
- 无法主动查询历史数据
- 信息可能不完整

**实现步骤**：
1. 监听飞书频道中的安全报告消息
2. 实现消息内容解析（正则或 OCR）
3. 提取关键数据（风险数量、防护状态等）
4. 存储到数据库
5. 展示在后台管理界面

### 方案 3：企业级深度集成

**前提条件**：
- 需联系飞书官方
- 获取 OpenClaw 数据接口权限
- 企业级飞书环境

**优点**：
- 最完整的数据访问
- 官方全力支持

**缺点**：
- 需要企业级支持
- 集成成本较高

## 实现架构设计

### 系统组件

```
┌─────────────────────────────────────────────────┐
│           jw-bot System                          │
├─────────────────────────────────────────────────┤
│                                                   │
│  ┌──────────────────────────────────────────┐   │
│  │   Feishu Security Integration Module      │   │
│  │  (新增组件)                                │   │
│  │                                            │   │
│  │  ├─ API Client                           │   │
│  │  │  └─ Audit Log API                    │   │
│  │  │  └─ Permission API                   │   │
│  │  │                                        │   │
│  │  ├─ Data Fetcher (定时任务)              │   │
│  │  │  └─ Scheduler                        │   │
│  │  │  └─ Data Transformer                 │   │
│  │  │                                        │   │
│  │  └─ Storage Layer                        │   │
│  │     └─ Database Models                  │   │
│  └──────────────────────────────────────────┘   │
│              ↓                                    │
│  ┌──────────────────────────────────────────┐   │
│  │      hermes-webui Dashboard               │   │
│  │   (新增安全管理后台)                       │   │
│  │                                            │   │
│  │  ├─ Security Reports Dashboard           │   │
│  │  ├─ Risk Management Page                 │   │
│  │  ├─ Audit Log Viewer                     │   │
│  │  └─ Alert Configuration                  │   │
│  └──────────────────────────────────────────┘   │
│                                                   │
└─────────────────────────────────────────────────┘
         ↓
┌─────────────────────────────────────────────────┐
│        Feishu Platform (飞书)                    │
│                                                   │
│  ├─ OpenClaw Security System                   │
│  ├─ Audit Log Service                          │
│  └─ Permission Management                      │
└─────────────────────────────────────────────────┘
```

### 数据库模型设计

```python
# 安全报告表
class SecurityReport(Model):
    id: str                    # 唯一ID
    report_name: str           # 报告名称（如"安全周报"）
    report_period: DateRange   # 报告周期
    data: JSON                 # 报告详细数据
    created_at: DateTime       # 创建时间
    updated_at: DateTime       # 更新时间

# 安全事件表
class SecurityEvent(Model):
    id: str
    event_type: str           # 事件类型
    severity: str             # 严重级别
    description: str          # 描述
    source: str               # 来源
    timestamp: DateTime       # 发生时间
    processed: bool           # 是否已处理

# 审计日志表
class AuditLog(Model):
    id: str
    operator: str             # 操作者
    action: str               # 操作类型
    resource: str             # 资源
    result: str               # 结果
    timestamp: DateTime       # 时间
```

## 开发计划（2026年5月）

### 第一阶段：准备工作（5月1-5日）

- [ ] 审查飞书开放平台文档
- [ ] 完成权限申请流程
- [ ] 获取测试环境 API 凭证（app_id, app_secret）
- [ ] 搭建本地测试环境

### 第二阶段：核心模块开发（5月6-15日）

- [ ] 实现 Feishu API Client
  - [ ] 认证模块
  - [ ] 审计日志 API 调用
  - [ ] 错误处理和重试机制
  
- [ ] 实现数据采集器
  - [ ] 定时任务配置
  - [ ] 数据转换逻辑
  - [ ] 数据库存储

- [ ] 数据库模型初始化
  - [ ] 创建表结构
  - [ ] 索引优化

### 第三阶段：后台管理界面（5月16-25日）

- [ ] 设计 Dashboard UI
  - [ ] 安全报告概览
  - [ ] 关键指标展示
  - [ ] 时间序列图表

- [ ] 实现管理功能
  - [ ] 报告详情查看
  - [ ] 事件筛选和搜索
  - [ ] 导出功能

- [ ] 实现告警功能
  - [ ] 告警规则配置
  - [ ] 通知集成（飞书、邮件等）

### 第四阶段：测试和优化（5月26-31日）

- [ ] 单元测试
- [ ] 集成测试
- [ ] 性能测试和优化
- [ ] 文档补充

## 技术细节

### 认证流程

```python
# 示例：获取 tenant_access_token
POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
{
    "app_id": "your_app_id",
    "app_secret": "your_app_secret"
}

Response:
{
    "code": 0,
    "msg": "ok",
    "tenant_access_token": "t-xxx",
    "expire": 7200
}
```

### API 调用示例

```python
# 获取审计日志
GET https://open.feishu.cn/open-apis/audit/v1/events?
    start_time=1698768000&
    end_time=1698854400&
    page_size=50

Headers:
    Authorization: Bearer t-xxx
    Content-Type: application/json
```

### 配置示例

```json
{
    "feishu_security": {
        "enabled": true,
        "app_id": "${FEISHU_APP_ID}",
        "app_secret": "${FEISHU_APP_SECRET}",
        "sync_interval": "3600",
        "batch_size": 100,
        "retention_days": 90
    }
}
```

## 参考资源

### 官方文档
- [飞书开放平台](https://open.feishu.cn/?lang=zh-CN)
- [飞书安全与治理指南](https://www.feishu.cn/content/article/7615520954977881029)
- [飞书 API 教程](https://apifox.com/apiskills/how-to-use-feishu-api/)
- [飞书安全中心](https://www.feishu.cn/security)

### 相关项目代码
- Lark 适配器：`astrbot/core/platform/sources/lark/`
- WebUI 架构：`hermes-webui/`
- 插件示例：`astrbot/plugins/`

## 注意事项

1. **权限管理**
   - 妥善保管 `app_secret`，不要提交到版本控制
   - 使用环境变量或密钥管理系统存储敏感信息

2. **API 限流**
   - 飞书 API 有调用频率限制
   - 建议实现缓存和批量查询

3. **数据隐私**
   - 确保合规处理安全日志数据
   - 实现适当的访问控制

4. **监控告警**
   - 监控 API 调用成功率
   - 设置异常告警阈值

## 下一步行动

1. **现在（4月）**：完成需求分析和方案设计（已完成）
2. **5月初**：启动开发前准备
3. **5月中旬**：开始核心模块开发
4. **5月末**：完成初版功能
5. **6月**：根据反馈持续优化

---

**文档维护者**：Claude Code  
**最后更新**：2026-04-24
