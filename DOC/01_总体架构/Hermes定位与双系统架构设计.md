# Hermes 定位与 AstrBot+Hermes 双系统架构设计

确立时间：2026-04-24

---

## Hermes 的本质定位

**Hermes 是执行引擎，不是对话机器人。**

Hermes 的设计起点是：接收一个结构化任务，调用工具（联网搜索、终端命令、文件操作、浏览器自动化），多步骤自主完成。这是它的唯一强项。

### Hermes 的短板

| 缺失能力 | 说明 |
|----------|------|
| 多用户会话隔离 | 无企业级身份管理 |
| 意图分类与任务路由 | 没有 Router 层 |
| QQ/飞书平台适配 | 不支持中国主流 IM |
| 任务生命周期管理 | 没有 Harness 机制 |
| 人格系统/权限控制 | 面向开发者 CLI，非员工界面 |
| **单任务执行** | session 模型线性，天然不支持多任务并发 |

### Hermes 与 OpenClaw 的本质区别

OpenClaw 是一体化系统，尝试把对话、路由、执行、平台适配全部包揽，每一块都做得有限。

Hermes 只做执行这一件事，但做得深（60 轮工具调用、联网搜索、持久记忆、技能积累）。

---

## 为什么用 AstrBot + Hermes 双系统

两个系统互补对方的短板：

| 能力缺口 | 由谁填补 |
|----------|----------|
| QQ/飞书平台适配 | AstrBot |
| 员工身份/多用户会话管理 | AstrBot |
| 意图分类和任务路由 | AstrBot Router |
| 即时回复和状态反馈 | AstrBot |
| 联网搜索/多步骤工具执行 | Hermes |
| 自主完成复杂任务 | Hermes |

**AstrBot 是前台（用户界面层），Hermes 是后台（执行引擎层）。**

---

## 核心架构：满意度驱动的升级机制

这是双系统协作的关键设计，也是 Hermes 被触发的判定逻辑。

```
员工发需求（QQ）
    ↓
AstrBot Router 分类 → Harness 创建任务
    ↓
AstrBot LLM 第一次尝试（快速、轻量）
    ↓
回复员工
    ↓
Harness 监听员工反馈
    ↓                         ↓
员工满意 ✅               员工不满意 ❌
任务闭环                  Harness 升级派发
                              ↓
                    Hermes 执行（联网搜索 + 多步推理）
                              ↓
                    更深度的结果 → AstrBot → QQ 员工
```

### 满意度判定信号

**显式信号：**
- 员工说"不够好"、"重新做"、"太泛了"、"再深入一点"、"这个不行"

**隐式信号：**
- 同一个任务下，员工继续追问相同方向（说明对上一次结果不满意）

### 为什么不让 Hermes 自己判定

Hermes 原生有自我升级触发条件（`nudge_interval`：每 N 轮触发记忆整合/技能提炼），这是 Hermes 单独运行时的机制。

在双系统环境下，触发判定权交给 **Harness**，原因：
- Harness 有完整的任务上下文和历史
- Harness 知道当前任务的 workflow_kind 和目标
- Hermes 的自我判定无法感知企业业务语境

---

## AstrBot 作为 Hermes 的多任务分流层（未来规划）

Hermes 的 session 模型是线性的——单 session 单执行线，天然不支持多任务并发。

但 AstrBot 的多 session 隔离 + Harness 任务队列，理论上可以实现：

```
用户 A → AstrBot session A → Harness task A → Hermes 执行
用户 B → AstrBot session B → Harness task B → Hermes 排队执行
用户 C → AstrBot session C → Harness task C → Hermes 排队执行
```

AstrBot 充当多频道分流层，让单任务的 Hermes 能够服务多用户。

**注意**：此功能需要重新做 Harness 与 Hermes 之间的任务队列整合，是独立的架构升级项目，暂不在当前交付范围内。

---

## 交付路线

| 阶段 | 内容 | 负责方 | 状态 |
|------|------|--------|------|
| 当前可交付 | 对话 → Router → Harness → AstrBot LLM 生成方案 | AstrBot | 🔧 完善中 |
| 升级触发 | Harness 满意度检测 → 不满意时派发 Hermes | AstrBot + Hermes | ⏳ 待开发 |
| 知识库增强 | 品牌规范/历史素材注入 AstrBot LLM prompt | AstrBot | ⏳ 待配置 |
| 多任务分流 | AstrBot 多 session → Hermes 任务队列 | 双系统整合 | 📋 未来规划 |

---

## 当前开发优先级

1. **Harness 满意度检测 + Hermes 升级触发**（形成完整闭环，可交付员工测试）
2. **知识库文档上传**（解决"方案太泛"问题，操作在 dashboard）
3. 测试后根据员工真实反馈决定 Hermes 深度集成方向
4. 多任务分流作为独立项目单独立项
