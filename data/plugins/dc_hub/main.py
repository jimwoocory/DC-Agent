"""DC-HUB integration socket for DC-Agent AstrBot plugins.

V0.1.0 is a non-destructive management layer. It catalogs existing plugins,
persists HUB-level feature switches, and exposes one product-level command/API.
Existing plugins can gradually migrate to read ``context.dc_hub_is_enabled``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

PLUGIN_ID = "dc_hub"
PLUGIN_VERSION = "0.1.0"


@dataclass(frozen=True)
class HubModule:
    plugin_id: str
    title: str
    category: str
    role: str
    default_enabled: bool = True
    migration: str = "managed-external"
    destructive_actions: bool = False
    repo_url: str = ""


CATEGORY_LABELS: dict[str, str] = {
    "assistant_core": "巅池-Agent 小助手 · 核心链路",
    "assistant_feishu": "巅池-Agent 小助手 · 飞书协作",
    "workflow_harness": "任务与 Harness",
    "content_media": "内容与多模态",
    "ops_system": "运维与系统入口",
    "independent": "独立功能",
}

CATEGORY_FAMILIES: dict[str, str] = {
    "assistant_core": "巅池-Agent 小助手",
    "assistant_feishu": "巅池-Agent 小助手",
    "workflow_harness": "DC-Agent 工作流",
    "content_media": "DC-Agent 能力插件",
    "ops_system": "DC-Agent 运维系统",
    "independent": "独立功能",
}


DEFAULT_MODULES: tuple[HubModule, ...] = (
    HubModule(
        "dc_hub",
        "DC-HUB 系统总入口",
        "ops_system",
        "统一纳管 DC-Agent 系统插件、生命周期动作和分类展示。",
        migration="hub-core",
        destructive_actions=False,
    ),
    HubModule(
        "dc_router",
        "LLM 智能路由",
        "assistant_core",
        "小助手主入口、意图识别、模型选择、排队和深度任务分流。",
        migration="hub-core-candidate",
    ),
    HubModule(
        "concierge_plugin",
        "公司接待机器人",
        "assistant_core",
        "员工身份档案、接待引导和长期记忆注入。",
        migration="hub-core-candidate",
    ),
    HubModule(
        "ai_inbox_plugin",
        "AI Inbox",
        "assistant_core",
        "员工请求收件箱和 Case 自动承接。",
        migration="hub-core-candidate",
    ),
    HubModule(
        "employee_onboarding",
        "员工入职引导",
        "assistant_core",
        "飞书卡片身份采集、教程和准入测试。",
        migration="hub-core-candidate",
    ),
    HubModule(
        "onboarding_guide",
        "首次对话引导",
        "assistant_core",
        "旧版 ABC 角色引导；employee_onboarding 启用时自动让路，仅作备用入口。",
    ),
    HubModule(
        "feishu_doc_fetcher",
        "飞书文档直读",
        "assistant_feishu",
        "Wiki/Docx 内容读取工具，供小助手基于真实资料回答。",
    ),
    HubModule(
        "feishu_resource_plugin",
        "飞书资料查询",
        "assistant_feishu",
        "按链接或标题查询飞书资料，凭证缺失时回退元信息匹配。",
    ),
    HubModule(
        "feishu_channel_control",
        "飞书通道治理",
        "assistant_feishu",
        "OpenClaw 风格的飞书准入、配对、群策略、路由元数据和卡片回调防伪。",
    ),
    HubModule(
        "daily_card_renderer",
        "飞书长回复卡片",
        "assistant_feishu",
        "把 LLM 长回复渲染成飞书 interactive card。",
    ),
    HubModule(
        "group_summary_plugin",
        "项目群聊总结",
        "assistant_feishu",
        "项目群聊记录摘要和复盘输出。",
    ),
    HubModule(
        "chat_creator_plugin",
        "飞书群协作助手",
        "assistant_feishu",
        "通过巅池-Agent 小助手提供飞书建群、群内邀请和进群指引。",
    ),
    HubModule(
        "assistant_distillation_plugin",
        "小助手学习候选",
        "assistant_core",
        "管理员审批学习候选并热更新小助手规则。",
    ),
    HubModule(
        "task_cli_plugin",
        "Harness 任务 CLI",
        "workflow_harness",
        "手动创建、查看、推进和审批 Harness 任务。",
    ),
    HubModule(
        "workflow_intent_plugin",
        "Workflow 意图识别",
        "workflow_harness",
        "旧版关键词建任务入口；可配置委托给 department_workflow_plugin。",
    ),
    HubModule(
        "department_workflow_plugin",
        "部门 Workflow 匹配",
        "workflow_harness",
        "部门场景匹配并转为 Harness 任务。",
    ),
    HubModule(
        "task_extractor_plugin",
        "任务提取提醒",
        "workflow_harness",
        "从员工对话中识别待办和跟进事项。",
    ),
    HubModule(
        "harness_state_injector",
        "Harness 状态硬约束",
        "workflow_harness",
        "向小助手注入任务状态，约束假完成和假分析。",
    ),
    HubModule(
        "harness_sensor_plugin",
        "Harness Sensor",
        "workflow_harness",
        "采集任务状态和自动完成信号。",
    ),
    HubModule(
        "case_plugin",
        "Case 聚合层",
        "workflow_harness",
        "提供 /case CLI 和业务 Case 聚合。",
    ),
    HubModule(
        "company_cognition_plugin",
        "公司认知健康检查",
        "workflow_harness",
        "员工、知识库、Case、Harness 和 Inbox 总账健康检查。",
    ),
    HubModule(
        "gpt_image_plugin",
        "GPT Image 2 生图",
        "content_media",
        "主用图像生成能力，支持 Dreamina 备用路径。",
    ),
    HubModule(
        "dreamina_plugin",
        "即梦多媒体",
        "content_media",
        "Dreamina 专用命令、视频和配音能力；生图主入口由 gpt_image_plugin/dc_router 承接。",
    ),
    HubModule(
        "document_intake_plugin",
        "文档入库",
        "content_media",
        "上传文档自动进入 NAS 和知识库导入流程。",
    ),
    HubModule(
        "department_training_quiz",
        "部门培训小测",
        "content_media",
        "部门培训题批改和飞书卡片反馈。",
    ),
    HubModule(
        "system_entries",
        "系统入口",
        "ops_system",
        "Hermes、OpenClaw、看门狗等服务入口和探活页。",
    ),
    HubModule(
        "devops_tools",
        "DevOps 工具集",
        "ops_system",
        "日志、watchdog、incident、git 和 launchctl 查询工具。",
    ),
    HubModule(
        "watchdog_status",
        "Watchdog 状态",
        "ops_system",
        "读取 cron 写入的 alerts.jsonl 并返回状态。",
    ),
    HubModule(
        "openclaw_on_demand",
        "OpenClaw 按需启动",
        "ops_system",
        "OpenClaw Control Center 按需启停。",
    ),
    HubModule(
        "hermes_bridge",
        "Hermes 双向桥",
        "ops_system",
        "AstrBot 与 Hermes 的双向桥接。",
    ),
    HubModule(
        "hermes_escalation_plugin",
        "Hermes 升级派发",
        "ops_system",
        "不满意或显式请求时派发到 Hermes 深度执行。",
    ),
    HubModule(
        "dianchi_tech",
        "巅池技术日报",
        "independent",
        "AI 资讯、agy 学习巡检和飞书/wiki 报告。",
    ),
    HubModule(
        "feishu_pet_assistant",
        "飞书工作宠物",
        "independent",
        "工作宠物状态卡、任务闭环和 SQLite 持久化。",
    ),
    HubModule(
        "silent_observer",
        "静默观察员",
        "independent",
        "灰度压力测试记录员，只记录不发言。",
    ),
)


@register(
    PLUGIN_ID,
    "dc_agent",
    "DC-HUB 集成插座：统一纳管小助手、飞书协作、Harness、内容和运维插件",
    PLUGIN_VERSION,
)
class DCHubPlugin(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        cfg = config or {}
        self.project_root = Path(__file__).resolve().parents[3]
        self.state_path = Path(
            cfg.get("state_path")
            or self.project_root / "data" / "config" / "dc_hub_state.json"
        )
        self.allow_state_mutation = bool(cfg.get("allow_state_mutation", True))
        self.modules = list(DEFAULT_MODULES)
        self._state = self._load_state()

    async def initialize(self) -> None:
        self.context.dc_hub = self
        self.context.dc_hub_is_enabled = self.is_enabled
        try:
            self.context.register_web_api(
                "/dc_hub/summary",
                self._api_summary,
                ["GET"],
                "DC-HUB 分类清单与 HUB 侧开关状态",
            )
            self.context.register_web_api(
                "/dc_hub/module",
                self._api_module,
                ["GET"],
                "DC-HUB 单个模块详情",
            )
            self.context.register_web_api(
                "/dc_hub/action",
                self._api_action,
                ["POST"],
                "DC-HUB 插件生命周期动作",
            )
            logger.info("[dc_hub] API ready: /api/plug/dc_hub/{summary,module,action}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dc_hub] register API failed: %s", exc)

    @filter.command(
        "dc-hub",
        desc=(
            "DC-HUB: /dc-hub status|category|show|enable|disable|reload|update|install|uninstall"
        ),
    )
    async def dc_hub_command(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        for prefix in ("/dc-hub", "dc-hub"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break
        parts = text.split(maxsplit=1)
        sub = parts[0].lower() if parts else "status"
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub in {"", "status", "ls", "list"}:
            self._reply(event, self._format_status())
            return
        if sub in {"category", "cat"}:
            self._reply(event, self._format_category(rest))
            return
        if sub == "show":
            self._reply(event, self._format_module(rest))
            return
        if sub in {"enable", "disable", "reload", "update"}:
            result = await self.lifecycle_action(sub, rest)
            self._reply(event, self._format_action_result(result))
            return
        if sub == "install":
            plugin_id, _, repo_url = rest.partition(" ")
            result = await self.lifecycle_action(
                "install",
                plugin_id.strip(),
                repo_url=repo_url.strip(),
            )
            self._reply(event, self._format_action_result(result))
            return
        if sub == "uninstall":
            plugin_id, _, confirm = rest.partition(" ")
            result = await self.lifecycle_action(
                "uninstall",
                plugin_id.strip(),
                confirm=confirm.strip().lower() in {"confirm", "yes", "确认"},
            )
            self._reply(event, self._format_action_result(result))
            return
        self._reply(event, self._usage())

    async def _api_summary(self, *args, **kwargs) -> dict[str, Any]:
        return {"status": "ok", "message": None, "data": self.summary()}

    async def _api_module(self, *args, **kwargs) -> dict[str, Any]:
        plugin_id = str(kwargs.get("plugin_id") or "")
        if not plugin_id:
            plugin_id = self._query_arg("plugin_id")
        module = self.get_module(plugin_id)
        if module is None:
            return {"status": "error", "message": "module not found", "data": None}
        return {"status": "ok", "message": None, "data": self.module_dict(module)}

    async def _api_action(self, *args, **kwargs) -> dict[str, Any]:
        payload: dict[str, Any] = dict(kwargs)
        try:
            from quart import request

            maybe_payload = await request.get_json(silent=True)
            if isinstance(maybe_payload, dict):
                payload.update(maybe_payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[dc_hub] request payload unavailable: %s", exc)
        result = await self.lifecycle_action(
            str(payload.get("action") or "").strip().lower(),
            str(payload.get("plugin_id") or "").strip(),
            confirm=_truthy(payload.get("confirm", False)),
            repo_url=str(payload.get("repo_url") or "").strip(),
            download_url=str(payload.get("download_url") or "").strip(),
            delete_config=_truthy(payload.get("delete_config", False)),
            delete_data=_truthy(payload.get("delete_data", False)),
        )
        if result["status"] == "error":
            return {"status": "error", "message": result["message"], "data": result}
        return {"status": "ok", "message": result["message"], "data": result}

    def summary(self) -> dict[str, Any]:
        categories = []
        for category, label in CATEGORY_LABELS.items():
            modules = [m for m in self.modules if m.category == category]
            categories.append(
                {
                    "id": category,
                    "label": label,
                    "family": CATEGORY_FAMILIES.get(category, "DC-Agent"),
                    "total": len(modules),
                    "enabled": sum(1 for m in modules if self.is_enabled(m.plugin_id)),
                    "desired_enabled": sum(
                        1 for m in modules if self.desired_enabled(m.plugin_id)
                    ),
                    "modules": [self.module_dict(m) for m in modules],
                }
            )
        return {
            "plugin": PLUGIN_ID,
            "version": PLUGIN_VERSION,
            "state_path": str(self.state_path),
            "mutation_enabled": self.allow_state_mutation,
            "total": len(self.modules),
            "enabled": sum(1 for m in self.modules if self.is_enabled(m.plugin_id)),
            "desired_enabled": sum(
                1 for m in self.modules if self.desired_enabled(m.plugin_id)
            ),
            "categories": categories,
        }

    def module_dict(self, module: HubModule) -> dict[str, Any]:
        data = asdict(module)
        data["category_label"] = CATEGORY_LABELS.get(module.category, module.category)
        data["family"] = CATEGORY_FAMILIES.get(module.category, "DC-Agent")
        data["installed"] = self._plugin_path(module.plugin_id).exists()
        star = self._registered_star(module.plugin_id)
        data["registered"] = star is not None
        data["activated"] = bool(star.activated) if star is not None else False
        data["runtime_enabled"] = self.is_enabled(module.plugin_id)
        data["desired_enabled"] = self.desired_enabled(module.plugin_id)
        data["enabled"] = data["runtime_enabled"]
        data["lifecycle"] = self._lifecycle_state(module.plugin_id)
        data["actions"] = self._available_actions(module)
        return data

    def get_module(self, plugin_id: str) -> HubModule | None:
        plugin_id = plugin_id.strip()
        if not plugin_id:
            return None
        return next((m for m in self.modules if m.plugin_id == plugin_id), None)

    def is_enabled(self, plugin_id: str) -> bool:
        module = self.get_module(plugin_id)
        if module is None:
            return True
        star = self._registered_star(plugin_id)
        if star is not None:
            return bool(star.activated)
        return False

    def desired_enabled(self, plugin_id: str) -> bool:
        module = self.get_module(plugin_id)
        if module is None:
            return True
        overrides = self._state.get("overrides", {})
        value = overrides.get(plugin_id)
        if value is None:
            return module.default_enabled
        return bool(value)

    def _record_override(self, plugin_id: str, enabled: bool) -> None:
        if not self.allow_state_mutation:
            return
        overrides = dict(self._state.get("overrides", {}))
        overrides[plugin_id] = enabled
        self._state["overrides"] = overrides
        self._save_state()

    def _toggle(self, plugin_id: str, *, enabled: bool) -> str:
        module = self.get_module(plugin_id)
        if module is None:
            return "未找到模块。用法：/dc-hub show <plugin_id>"
        if not self.allow_state_mutation:
            return "DC-HUB 状态写入已关闭。"
        self._record_override(module.plugin_id, enabled)
        state = "启用" if enabled else "停用"
        return (
            f"已在 DC-HUB 中{state}：{module.plugin_id}\n"
            "说明：V0.1.0 是 HUB 侧功能开关，不会删除或卸载原 AstrBot 插件。"
        )

    async def lifecycle_action(
        self,
        action: str,
        plugin_id: str,
        *,
        confirm: bool = False,
        repo_url: str = "",
        download_url: str = "",
        delete_config: bool = False,
        delete_data: bool = False,
    ) -> dict[str, Any]:
        module = self.get_module(plugin_id)
        if module is None:
            return self._action_error(action, plugin_id, "未找到模块。")
        manager = self._plugin_manager()
        if manager is None:
            return self._action_error(action, plugin_id, "AstrBot 插件管理器不可用。")
        if action not in {
            "enable",
            "disable",
            "reload",
            "update",
            "install",
            "uninstall",
        }:
            return self._action_error(action, plugin_id, "未知动作。")
        if plugin_id == PLUGIN_ID and action in {"disable", "uninstall"}:
            return self._action_error(action, plugin_id, "不能停用或卸载 DC-HUB 自己。")

        try:
            if action == "enable":
                await manager.turn_on_plugin(plugin_id)
                self._record_override(plugin_id, True)
                return self._action_ok(action, plugin_id, "已启用插件。")
            if action == "disable":
                await manager.turn_off_plugin(plugin_id)
                self._record_override(plugin_id, False)
                return self._action_ok(action, plugin_id, "已停用插件。")
            if action == "reload":
                success, message = await manager.reload(specified_plugin_name=plugin_id)
                if not success:
                    return self._action_error(
                        action, plugin_id, message or "重载失败。"
                    )
                return self._action_ok(action, plugin_id, "已重载插件。")
            if action == "update":
                await manager.update_plugin(plugin_id, download_url=download_url)
                return self._action_ok(action, plugin_id, "已触发插件更新。")
            if action == "install":
                return await self._install_module(
                    manager,
                    module,
                    repo_url=repo_url,
                    download_url=download_url,
                )
            if action == "uninstall":
                if not confirm:
                    return self._action_error(
                        action,
                        plugin_id,
                        "卸载需要确认：命令使用 /dc-hub uninstall <plugin_id> confirm。",
                    )
                await manager.uninstall_plugin(
                    plugin_id,
                    delete_config=delete_config,
                    delete_data=delete_data,
                )
                self._record_override(plugin_id, False)
                return self._action_ok(action, plugin_id, "已卸载插件目录。")
        except Exception as exc:  # noqa: BLE001
            return self._action_error(action, plugin_id, str(exc))

        return self._action_error(action, plugin_id, "动作未执行。")

    async def _install_module(
        self,
        manager,
        module: HubModule,
        *,
        repo_url: str = "",
        download_url: str = "",
    ) -> dict[str, Any]:
        if self._plugin_path(module.plugin_id).exists():
            success, message = await manager.load(specified_dir_name=module.plugin_id)
            if not success:
                return self._action_error(
                    "install",
                    module.plugin_id,
                    message or "本地插件加载失败。",
                )
            self._record_override(module.plugin_id, True)
            return self._action_ok("install", module.plugin_id, "已加载本地插件目录。")

        source_url = repo_url or module.repo_url
        if not source_url:
            return self._action_error(
                "install",
                module.plugin_id,
                "缺少安装源。请提供 repo_url，或先恢复本地插件目录。",
            )
        await manager.install_plugin(source_url, download_url=download_url)
        self._record_override(module.plugin_id, True)
        return self._action_ok("install", module.plugin_id, "已从安装源安装插件。")

    def _format_status(self) -> str:
        summary = self.summary()
        lines = [
            f"DC-HUB v{PLUGIN_VERSION}",
            f"- 纳管模块: {summary['total']} 个",
            f"- 运行中插件: {summary['enabled']} 个",
            f"- 期望启用: {summary['desired_enabled']} 个",
            "",
            "分类:",
        ]
        for category in summary["categories"]:
            lines.append(
                f"- {category['id']} / {category['label']}: "
                f"运行中 {category['enabled']}/{category['total']}；"
                f"期望启用 {category['desired_enabled']}/{category['total']}"
            )
        lines.extend(
            [
                "",
                "用法:",
                "  /dc-hub category <category_id>",
                "  /dc-hub show <plugin_id>",
                "  /dc-hub enable|disable <plugin_id>",
                "  /dc-hub reload|update <plugin_id>",
                "  /dc-hub install <plugin_id> [repo_url]",
                "  /dc-hub uninstall <plugin_id> confirm",
            ]
        )
        return "\n".join(lines)

    def _format_category(self, category_id: str) -> str:
        category_id = category_id.strip()
        if category_id not in CATEGORY_LABELS:
            choices = " | ".join(CATEGORY_LABELS)
            return f"未知分类。可选：{choices}"
        modules = [m for m in self.modules if m.category == category_id]
        lines = [f"{CATEGORY_LABELS[category_id]}（{category_id}）"]
        for module in modules:
            data = self.module_dict(module)
            runtime = "active" if data["runtime_enabled"] else data["lifecycle"]
            desired = "desired-on" if data["desired_enabled"] else "desired-off"
            installed = "installed" if data["installed"] else "missing"
            lines.append(
                f"- [{runtime}/{desired}/{installed}] "
                f"{module.plugin_id}: {module.title}"
            )
        return "\n".join(lines)

    def _format_module(self, plugin_id: str) -> str:
        module = self.get_module(plugin_id)
        if module is None:
            return "未找到模块。用法：/dc-hub show <plugin_id>"
        data = self.module_dict(module)
        state = "运行中" if data["runtime_enabled"] else "未运行"
        desired = "期望启用" if data["desired_enabled"] else "期望停用"
        installed = "已安装" if data["installed"] else "缺失"
        return "\n".join(
            [
                f"{module.title} / {module.plugin_id}",
                f"- 分类: {data['category_label']} ({module.category})",
                f"- 运行状态: {state}",
                f"- HUB 开关: {desired}",
                f"- 生命周期: {data['lifecycle']}",
                f"- 插件目录: {installed}",
                f"- 迁移阶段: {module.migration}",
                f"- 说明: {module.role}",
                f"- 可用动作: {', '.join(data['actions'])}",
            ]
        )

    def _usage(self) -> str:
        return (
            "用法：\n"
            "  /dc-hub status\n"
            "  /dc-hub category <category_id>\n"
            "  /dc-hub show <plugin_id>\n"
            "  /dc-hub enable <plugin_id>\n"
            "  /dc-hub disable <plugin_id>\n"
            "  /dc-hub reload <plugin_id>\n"
            "  /dc-hub update <plugin_id>\n"
            "  /dc-hub install <plugin_id> [repo_url]\n"
            "  /dc-hub uninstall <plugin_id> confirm"
        )

    def _reply(self, event: AstrMessageEvent, text: str) -> None:
        event.set_result(MessageEventResult().message(text).use_t2i(False))

    def _plugin_path(self, plugin_id: str) -> Path:
        if plugin_id == "hermes_bridge":
            return self.project_root / "data" / "plugins" / "hermes_bridge"
        return self.project_root / "data" / "plugins" / plugin_id

    def _query_arg(self, name: str) -> str:
        try:
            from quart import request

            return str(request.args.get(name) or "")
        except Exception as exc:  # noqa: BLE001
            logger.debug("[dc_hub] query arg unavailable: %s", exc)
            return ""

    def _plugin_manager(self):
        return getattr(self.context, "_star_manager", None)

    def _registered_star(self, plugin_id: str):
        get_registered_star = getattr(self.context, "get_registered_star", None)
        if callable(get_registered_star):
            return get_registered_star(plugin_id)
        return None

    def _lifecycle_state(self, plugin_id: str) -> str:
        if not self._plugin_path(plugin_id).exists():
            return "missing"
        star = self._registered_star(plugin_id)
        if star is None:
            return "installed-unloaded"
        if star.activated:
            return "active"
        return "disabled"

    def _available_actions(self, module: HubModule) -> list[str]:
        state = self._lifecycle_state(module.plugin_id)
        actions: list[str] = []
        if state == "active":
            actions.extend(["disable", "reload", "update"])
        elif state == "disabled":
            actions.extend(["enable", "reload", "update"])
        else:
            actions.append("install")
        if module.plugin_id != PLUGIN_ID and state != "missing":
            actions.append("uninstall")
        return actions

    def _action_ok(self, action: str, plugin_id: str, message: str) -> dict[str, Any]:
        module = self.get_module(plugin_id)
        return {
            "status": "ok",
            "action": action,
            "plugin_id": plugin_id,
            "message": message,
            "module": self.module_dict(module) if module else None,
        }

    def _action_error(
        self, action: str, plugin_id: str, message: str
    ) -> dict[str, Any]:
        return {
            "status": "error",
            "action": action,
            "plugin_id": plugin_id,
            "message": message,
        }

    def _format_action_result(self, result: dict[str, Any]) -> str:
        prefix = "完成" if result.get("status") == "ok" else "失败"
        return (
            f"{prefix}: {result.get('plugin_id') or '-'} "
            f"{result.get('action') or '-'}\n"
            f"{result.get('message') or ''}"
        )

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": PLUGIN_VERSION, "overrides": {}}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dc_hub] state load failed: %s", exc)
            return {"version": PLUGIN_VERSION, "overrides": {}}
        if not isinstance(payload, dict):
            return {"version": PLUGIN_VERSION, "overrides": {}}
        payload.setdefault("version", PLUGIN_VERSION)
        payload.setdefault("overrides", {})
        if not isinstance(payload["overrides"], dict):
            payload["overrides"] = {}
        return payload

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "确认"}
    return False
