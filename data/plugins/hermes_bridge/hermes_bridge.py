"""
AstrBot ↔ Hermes Agent 双向桥接插件（方案 C 协作架构）

职责：
1. 用户消息转发给 Hermes（仅 /hermes on 模式）
2. Router 创建的 Harness workflow 任务派发给 Hermes 执行
3. 接收 Hermes 结果 → 完成 Harness 任务 → 推送回原平台用户
4. 长期任务记忆注入 → 每次 LLM 调用前注入近期任务摘要到 system_prompt

配置（hermes_bridge section）：
  webhook_url:      Hermes 消息 Webhook（默认 http://localhost:8644/webhooks/astrbot_qq）
  task_webhook_url: Hermes 任务 Webhook（默认 http://localhost:8644/webhooks/astrbot_task）
  secret:           HMAC 签名密钥
  response_port:    本地响应监听端口（默认 8645）
  allowed_platforms: 允许转发的平台列表
  memory_inject_enabled: 是否注入长期任务记忆（默认 True）
  memory_inject_limit:   注入最近几条记忆（默认 5）
"""

import asyncio
import hashlib
import hmac
import json
import os
import re as _re_card
import shutil

# ── SessionRouter ─────────────────────────────────────────────────────────────
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import aiohttp
from dc_engines.card_runtime import finalize_card_via_runtime, send_card_via_runtime
from dc_engines.feishu_card_streamer import (
    build_deleted_skill_list_card,
    build_error_card,
    build_final_card,
    build_progress_card,
    build_skill_confirm_card,
    build_skill_detail_card,
    build_skill_list_card,
    build_skill_review_card,
    ensure_streamers_on_context,
)

# W0 + 重装后调整：业务引擎从 astrbot/core 迁到独立的 dc_engines 包
# 目的：升级 AstrBot 时不会再静默吃掉我们的代码
from dc_engines.harness import create_workflow_request
from dc_engines.harness.content_sop_runtime import settle_content_sop_result
from dc_engines.harness.contracts import HARNESS_TERMINAL_STATUSES
from dc_engines.harness.workflows import parse_workflow_result
from dc_engines.hermes_bridge_engine import (
    HermesCallbackDispatcher,
    HermesDLQLogger,
    PermanentSendError,
    RetriableSendError,
    classify_http_status,
    verify_hmac_signature,
)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register

# Hermes Agent 中间状态消息特征（这些不该当成最终结果，应该走 streamer.update）
_HERMES_INTERMEDIATE_RE = _re_card.compile(
    r"Still working|iteration \d+/\d+|Retrying in|attempt \d+/\d+|Max retries|"
    r"API call failed|"
    r"Compression model .* context is \d+|"  # gemini context mismatch warning
    r"Auto-lowered this session's threshold|"
    r"To make this permanent, edit config\.yaml|"
    r"Configured auxiliary .* provider .* is unavailable|"  # compression provider 配错警告
    r"context compression will drop middle turns",
    _re_card.IGNORECASE,
)
# Hermes Agent 最终失败特征
_HERMES_FINAL_FAILURE_RE = _re_card.compile(
    r"API failed after \d+ retries|Final error|Request timed out\.",
    _re_card.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CardHandleResult:
    handled: bool = False
    is_finalized: bool = False


@dataclass(frozen=True, slots=True)
class _DistillationTopic:
    task_id: str
    title: str


@dataclass(frozen=True, slots=True)
class SkillBundleWriteResult:
    kind: str
    name: str
    slug: str
    directory: Path
    files: tuple[str, ...]


class PlatformType(str, Enum):
    QQ = "qq"
    FEISHU = "feishu"
    WEBUI = "webui"
    WECHAT = "wechat"
    UNKNOWN = "unknown"

    @classmethod
    def from_string(cls, value: str) -> "PlatformType":
        try:
            return cls(value.lower())
        except ValueError:
            raise ValueError(f"Unknown platform: {value}")

    @classmethod
    def from_astrbot_platform_id(cls, platform_id: str) -> "PlatformType":
        _map = {
            "qq_official": cls.QQ,
            "lark": cls.FEISHU,
            "webchat": cls.WEBUI,
        }
        result = _map.get(platform_id.lower())
        if result is not None:
            return result
        try:
            return cls.from_string(platform_id)
        except ValueError:
            return cls.UNKNOWN


@dataclass
class PlatformUser:
    platform: PlatformType
    user_id: str


class SessionRouter:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_mappings (
                    platform TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (platform, user_id)
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def get_or_create_session(self, user: PlatformUser) -> str:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT session_key FROM session_mappings WHERE platform=? AND user_id=?",
                (user.platform.value, user.user_id),
            ).fetchone()
            if row:
                return row[0]
            key = f"{user.platform.value}_{user.user_id}_{uuid.uuid4().hex[:8]}"
            conn.execute(
                "INSERT INTO session_mappings (platform, user_id, session_key) VALUES (?,?,?)",
                (user.platform.value, user.user_id, key),
            )
            return key

    def get_platform_user_by_session(self, session_key: str) -> PlatformUser | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT platform, user_id FROM session_mappings WHERE session_key=?",
                (session_key,),
            ).fetchone()
            if row:
                return PlatformUser(platform=PlatformType(row[0]), user_id=row[1])
            return None


# ── Plugin ────────────────────────────────────────────────────────────────────


@register(
    "hermes_bridge",
    "hermes_bridge",
    "AstrBot ↔ Hermes 双向桥接（方案 C 协作架构）",
    version="2.0.0",
)
class HermesBridgePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)

        # AstrBot v4.24+ 把 plugin 配置存到 data/config/hermes_bridge_config.json，
        # 经由 _conf_schema.json 声明字段。优先用 plugin-scoped config；
        # 没拿到（老版本兼容）就退回 context.get_config() 里的 hermes_bridge 子段。
        if config is not None:
            hcfg = config
        else:
            hcfg = context.get_config().get("hermes_bridge", {})

        self.hermes_webhook_url: str = hcfg.get(
            "webhook_url", "http://localhost:8644/webhooks/astrbot_qq"
        )
        self.hermes_task_webhook_url: str = hcfg.get(
            "task_webhook_url", "http://localhost:8644/webhooks/astrbot_task"
        )
        self.hermes_secret = self._require_secret(hcfg)
        self.response_port: int = hcfg.get("response_port", 8645)
        self.allowed_platforms: list = hcfg.get(
            "allowed_platforms",
            ["巅池-推广01", "巅池-推广 01", "巅池1号"],
        )
        self.excluded_platforms: list = hcfg.get("excluded_platforms", ["巅池-技术"])
        self.memory_inject_enabled: bool = hcfg.get("memory_inject_enabled", True)
        self.memory_inject_limit: int = int(hcfg.get("memory_inject_limit", 5))
        self.direct_chat_enabled: bool = hcfg.get("direct_chat_enabled", False)
        self.topic_workflow_enabled: bool = hcfg.get("topic_workflow_enabled", True)
        self.topic_admin_only: bool = hcfg.get("topic_admin_only", True)
        self.topic_intro_on_new: bool = hcfg.get("topic_intro_on_new", True)
        self.topic_discussion_limit: int = int(hcfg.get("topic_discussion_limit", 30))
        self.topic_distill_enabled: bool = hcfg.get("topic_distill_enabled", True)
        self.conversation_distill_enabled: bool = hcfg.get(
            "conversation_distill_enabled",
            True,
        )
        self.skill_admin_only: bool = hcfg.get("skill_admin_only", True)
        self.skill_admin_user_ids: set[str] = {
            str(item).strip()
            for item in hcfg.get("skill_admin_user_ids", [])
            if str(item).strip()
        }
        self.protected_identity_names: set[str] = {
            self._normalize_identity_name(item)
            for item in hcfg.get("protected_identity_names", [])
            if self._normalize_identity_name(item)
        }
        self.protected_identity_user_ids: set[str] = {
            str(item).strip()
            for item in hcfg.get("protected_identity_user_ids", [])
            if str(item).strip()
        }
        self.skill_admin_user_ids.update(self.protected_identity_user_ids)
        self.skill_ops_require_confirm: bool = hcfg.get(
            "skill_ops_require_confirm",
            True,
        )

        # 持久化路径：默认仓库 data/ 目录，允许 hcfg.data_dir 覆盖（测试 / 私有部署）
        data_dir_override = hcfg.get("data_dir")
        if data_dir_override:
            data_dir = Path(data_dir_override)
            data_dir.mkdir(parents=True, exist_ok=True)
        else:
            data_dir = Path(__file__).resolve().parents[3] / "data"
        self.session_router = SessionRouter(str(data_dir / "hermes_sessions.db"))

        # session_key → unified_msg_origin 内存缓存（重启后首条消息重建）
        self._umo_cache: dict[str, str] = {}

        # 回群链路 DLQ + 重试调度器（G2 / Phase 0.3）
        self._dlq_logger = HermesDLQLogger(data_dir / "hermes_dlq.jsonl")
        self._callback_dispatcher = HermesCallbackDispatcher(
            sender=self._send_to_platform_strict,
            dlq_logger=self._dlq_logger,
        )

        # /hermes on 已启用的用户集合
        self.hermes_enabled_users: set[str] = set()
        self._topic_discussion_cache: dict[str, deque[dict]] = defaultdict(
            lambda: deque(maxlen=self.topic_discussion_limit)
        )
        self._conversation_discussion_cache: dict[str, deque[dict]] = defaultdict(
            lambda: deque(maxlen=self.topic_discussion_limit)
        )
        self._topic_rollcall: dict[str, dict] = {}

        self._webhook_app = None
        logger.info(
            "[HermesBridge] 初始化完成，消息 Webhook: %s", self.hermes_webhook_url
        )
        logger.info("[HermesBridge] 任务 Webhook: %s", self.hermes_task_webhook_url)

    def _require_secret(self, hcfg: dict) -> str:
        raw_secret = str(hcfg.get("secret") or "").strip()
        if raw_secret.startswith("${") and raw_secret.endswith("}"):
            # ${VAR} 占位符：env var 存在则 expand；不存在则保留字面量字符串
            # 与 hermes-config/config.yaml 的 yaml.safe_load 行为保持一致
            # （hermes 不 expand 占位符，所以 hermes 端 secret 也是字面量字符串）
            var_name = raw_secret[2:-1]
            expanded = os.environ.get(var_name, "").strip()
            if expanded:
                raw_secret = expanded
            # else: 保留 raw_secret = "${VAR}" 字面量，与 hermes 端 HMAC 对齐
        if not raw_secret:
            raise RuntimeError(
                "hermes bridge secret 未配置，请补 data/config/hermes_bridge_config.json"
            )
        return raw_secret

    async def initialize(self):
        await self._start_response_server()
        logger.info("[HermesBridge] 响应服务器启动，监听 port %s", self.response_port)

        # feishu_streamers 改走 lazy init（运行时 ensure_streamers_on_context 创建）
        # —— initialize 阶段 platform 还没加载完，提前建会拿到空 list

        # 自建 Harness sidecar（重装后 core_lifecycle 不再初始化它，由插件自己管）
        # 装到 context 上以便后续代码 `self.context.harness_engine` 仍可使用。
        if getattr(self.context, "harness_engine", None) is None:
            from dc_engines.harness import (
                HarnessEngine,
                HarnessMemoryPromoter,
                HarnessMemoryStore,
                HarnessTaskStore,
            )

            data_dir_override = (
                self.context.get_config().get("hermes_bridge", {}).get("data_dir")
            )
            if data_dir_override:
                data_dir = Path(data_dir_override)
            else:
                data_dir = Path(__file__).resolve().parents[3] / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            task_store = HarnessTaskStore(str(data_dir / "harness.db"))
            await task_store.initialize()
            memory_store = HarnessMemoryStore(str(data_dir / "harness_memory.db"))
            await memory_store.initialize()
            promoter = HarnessMemoryPromoter(memory_store)
            engine = HarnessEngine(task_store, memory_promoter=promoter)

            self.context.harness_engine = engine
            self.context.harness_store = task_store
            self.context.dispatch_task_to_hermes = self.dispatch_task_to_hermes
            logger.info(
                "[HermesBridge] 自建 Harness sidecar：%s + %s",
                data_dir / "harness.db",
                data_dir / "harness_memory.db",
            )
        else:
            self.context.dispatch_task_to_hermes = self.dispatch_task_to_hermes

    # ── 长期任务记忆注入（LLM 调用前钩子） ────────────────────────────────────

    @filter.on_llm_request()
    async def inject_harness_memory(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """
        长期记忆注入 — 参考 memory-lancedb-pro 的 hybrid recall 思路（简化版）。

        两层记忆架构：
          短期 = AstrBot 对话窗口（已有，protect last-N 理念来自 lossless-claw）
          长期 = Harness task memory（此处注入）

        排序策略（简化 hybrid score）：
          score = recency_weight * 0.5 + domain_relevance * 0.5
          recency_weight  : 越新越高（指数衰减，半衰期 7 天）
          domain_relevance: 当前消息关键词命中记忆 domain/title 越多得分越高
        """
        if not self.memory_inject_enabled:
            return

        try:
            engine = self.context.harness_engine
            if engine is None or engine.memory_promoter is None:
                return

            memory_store = engine.memory_promoter.store
            # 多取一些，由本地 scoring 再筛出 top-N
            candidates = await memory_store.list_for_session(
                event.unified_msg_origin,
                limit=self.memory_inject_limit * 3,
            )
            if not candidates:
                return

            scored = self._score_memories(event.message_str, candidates)
            top = scored[: self.memory_inject_limit]

            lines = [
                "# 工作记忆（历史任务摘要，供回答参考，无需向用户重复）",
            ]
            for mem, _score in top:
                date_str = mem.created_at[:10]
                lines.append(f"- [{date_str}][{mem.domain}] {mem.title}：{mem.summary}")

            memory_block = "\n".join(lines)
            req.system_prompt = (req.system_prompt or "") + f"\n\n{memory_block}\n"

            logger.debug(
                "[HermesBridge] 长期记忆注入：session=%s, 注入=%d条（候选=%d条）",
                event.unified_msg_origin,
                len(top),
                len(candidates),
            )

        except Exception as exc:
            logger.debug("[HermesBridge] 长期记忆注入失败（不影响正常流程）：%s", exc)

    def _score_memories(
        self,
        message: str,
        memories: list,
    ) -> list[tuple]:
        """
        对候选记忆打分，返回 [(memory, score), ...] 降序排列。

        score = 0.5 * recency + 0.5 * domain_relevance

        recency       : 指数衰减，半衰期 7 天（参考 Weibull decay 思路）
        domain_relevance: CJK 感知的 n-gram 子串匹配
                          提取消息中所有 2~4 字子串，检查在记忆文本中出现的比例
                          （避免中文无空格分词问题，参考 lossless-claw 的 CJK 处理）
        """
        import math
        from datetime import datetime, timezone

        msg = message.lower()
        # 提取 2~4 字 n-gram（兼容中英文混合）
        ngrams: set[str] = set()
        for n in (2, 3, 4):
            for i in range(len(msg) - n + 1):
                chunk = msg[i : i + n].strip()
                if chunk:
                    ngrams.add(chunk)

        now = datetime.now(timezone.utc)
        results = []

        for mem in memories:
            # ── Recency score（指数衰减，半衰期 7 天）──────────────────────────
            try:
                created = datetime.fromisoformat(mem.created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                days_old = max(0.0, (now - created).total_seconds() / 86400)
                recency = math.exp(-0.693 * days_old / 7.0)
            except Exception:
                recency = 0.5

            # ── Domain relevance score（n-gram 子串匹配）────────────────────
            target = f"{mem.domain} {mem.title} {mem.summary}".lower()
            if ngrams:
                hits = sum(1 for ng in ngrams if ng in target)
                domain_score = min(1.0, hits / max(1, len(ngrams)) * 5)
            else:
                domain_score = 0.0

            score = 0.5 * recency + 0.5 * domain_score
            results.append((mem, score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results

    # ── AstrBot LLM 回答后：完成 pending 任务 → 触发记忆写入 ─────────────────

    @filter.on_llm_response()
    async def complete_pending_harness_task(
        self,
        event: AstrMessageEvent,
        resp: LLMResponse,
    ) -> None:
        """
        AstrBot LLM 回答完毕后，将该 session 中 pending 状态的 Harness 任务
        标记为 completed，触发 HarnessMemoryPromoter 写入长期记忆。

        状态区分：
          pending    → AstrBot LLM 刚处理完，此处 complete → 写记忆 ✅
          in_progress → 已派发给 Hermes，跳过（等 Hermes 回传再 complete）⏭️
        """
        try:
            engine = self.context.harness_engine
            if engine is None:
                return

            tasks = await engine.store.list_tasks_for_session(
                event.unified_msg_origin,
                limit=1,
                statuses=("pending",),
            )
            if not tasks:
                return

            task = tasks[0]

            # 仅处理 RouterStage 创建、由 AstrBot LLM 直接处理的任务
            # source="router_intent"      → _handle_task_intent 创建，LLM 先答，此处 complete ✅
            # source="satisfaction_escalation" → 已被 mark_in_progress，status≠pending，不会走到这里
            # 其他 source（如手动 /task intake）→ 跳过
            source = task.payload.get("source", "")
            if source != "router_intent":
                return

            response_text = (resp.completion_text or "").strip()
            await engine.complete_task(
                task.task_id,
                result={
                    "summary": response_text[:200],
                    "response_preview": response_text[:500],
                    "source": "astrbot_llm",
                },
            )
            logger.debug(
                "[HermesBridge] AstrBot LLM 回答已完成 Harness 任务（#%s）→ 记忆已写入",
                task.task_id[:8],
            )

        except Exception as exc:
            logger.debug("[HermesBridge] 任务记忆写入失败（不影响正常流程）：%s", exc)

    # ── 消息入站处理 ──────────────────────────────────────────────────────────

    async def on_message(self, event: AstrMessageEvent) -> None:
        try:
            platform_id = str(event.get_platform_id() or "")
            if platform_id in self.excluded_platforms:
                return
            if self.allowed_platforms and platform_id not in self.allowed_platforms:
                return

            message_text = "".join(
                str(c.text) for c in event.get_messages() if isinstance(c, Plain)
            )
            if not message_text.strip():
                return

            user_id = str(event.get_sender_id())

            if user_id and user_id == str(event.get_self_id() or ""):
                return

            # 开关命令
            normalized = message_text.strip().lower()
            if (
                self.conversation_distill_enabled
                and await self._handle_conversation_distill_command(
                    event,
                    message_text.strip(),
                )
            ):
                event.stop_event()
                return

            if self.topic_workflow_enabled and await self._handle_topic_command(
                event,
                user_id,
                message_text.strip(),
            ):
                event.stop_event()
                return

            if normalized in ("/hermes on", "/hermes off", "/hermes status"):
                await self._handle_toggle(event, user_id, normalized)
                event.stop_event()
                return

            if self.topic_workflow_enabled and await self._handle_rollcall_reply(
                event,
                user_id,
                message_text.strip(),
            ):
                event.stop_event()
                return

            if self.conversation_distill_enabled:
                await self._record_conversation_discussion(
                    event,
                    user_id,
                    message_text,
                )

            if self.topic_workflow_enabled:
                await self._record_topic_discussion(event, user_id, message_text)

            if not self.direct_chat_enabled:
                return

            # 只有开启 Hermes 模式的用户才转发消息
            if user_id not in self.hermes_enabled_users:
                return

            try:
                platform_type = PlatformType.from_astrbot_platform_id(platform_id)
            except ValueError:
                platform_type = PlatformType.QQ

            session_key = self._get_or_create_session(user_id, platform_type)
            umo = event.unified_msg_origin
            self._umo_cache[session_key] = umo

            await self._send_to_hermes(
                {
                    "user_id": user_id,
                    "session_key": session_key,
                    "unified_msg_origin": umo,
                    "message": message_text,
                    "message_type": "group" if event.is_group() else "private",
                    "platform": platform_type.value,
                    "message_id": str(getattr(event, "message_id", "")),
                    "sender_nickname": event.get_sender_name() or user_id,
                }
            )

        except Exception as exc:
            logger.error("[HermesBridge] on_message 失败：%s", exc)

    # ── 任务派发（由 RouterStage 调用）──────────────────────────────────────

    async def dispatch_task_to_hermes(
        self,
        task_id: str,
        workflow_kind: str,
        brief: str,
        umo: str,
        cognitive_context: dict,
    ) -> bool:
        """将 Harness workflow 任务派发给 Hermes 执行。返回是否成功。"""
        payload = {
            "task_id": task_id,
            "workflow_kind": workflow_kind,
            "brief": brief,
            "session_id": umo,
            "unified_msg_origin": umo,
            "cognitive_context": cognitive_context,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.hermes_task_webhook_url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Webhook-Event": "harness_task",
                        "X-Task-ID": task_id,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status in (200, 201, 202):
                        logger.info(
                            "[HermesBridge] 任务 %s 已派发给 Hermes（%s）",
                            task_id,
                            self.hermes_task_webhook_url,
                        )
                        return True
                    logger.warning(
                        "[HermesBridge] Hermes 任务派发失败 HTTP %s: %s",
                        resp.status,
                        await resp.text(),
                    )
                    return False
        except Exception as exc:
            logger.warning("[HermesBridge] Hermes 任务派发异常：%s", exc)
            return False

    # ── Hermes 响应接收 ───────────────────────────────────────────────────────

    async def _start_response_server(self):
        try:
            from aiohttp import web

            app = web.Application()
            app.router.add_post("/hermes_response", self._handle_hermes_response)
            app.router.add_post("/task_result", self._handle_hermes_response)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", self.response_port).start()
            self._webhook_app = app
        except Exception as exc:
            logger.error("[HermesBridge] 响应服务器启动失败：%s", exc)

    async def _handle_hermes_response(self, request):
        from aiohttp import web

        try:
            body = await request.read()

            # HMAC 入站校验：若 header 带签名，必须校验通过；不带签名按现行行为放行（grace）
            sig_header = request.headers.get("X-Hub-Signature-256", "")
            if sig_header and not verify_hmac_signature(
                self.hermes_secret, body, sig_header
            ):
                logger.warning("[HermesBridge] X-Hub-Signature-256 校验失败，拒绝回调")
                return web.json_response({"status": "unauthorized"}, status=401)

            try:
                data = json.loads(body.decode("utf-8")) if body else {}
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.warning("[HermesBridge] 回调 body 解析失败：%s", exc)
                return web.json_response({"status": "bad_request"}, status=400)

            response_text = data.get("response", "") or data.get("message", "")
            task_id: str | None = data.get("task_id")
            session_key: str = data.get("session_key", "")

            if not response_text:
                logger.warning("[HermesBridge] 收到空响应：%s", data)
                return web.json_response({"status": "ok"})

            # ─── Dedup 锁：同一 task 已被推送过完整结果就不再发 ───
            # Hermes max_turns=60 时同一 task 可能多次推送，避免飞书消息洪水
            finalized = getattr(self, "_finalized_task_ids", None)
            if finalized is None:
                finalized = set()
                self._finalized_task_ids = finalized
            if task_id and task_id in finalized:
                logger.info(
                    "[HermesBridge] task=%s 已 finalize 过，跳过重复推送",
                    task_id[:8],
                )
                return web.json_response({"status": "ok", "via": "dedup_skip"})

            # ─── 飞书卡片流接管（中间状态 → update；最终结果 → finalize）───
            card_result = CardHandleResult()
            if task_id:
                card_result = await self._handle_response_via_card(
                    task_id, response_text
                )
            if card_result.handled:
                if task_id and card_result.is_finalized:
                    finalized.add(task_id)
                return web.json_response({"status": "ok", "via": "feishu_card"})

            # 解析回传地址
            umo: str | None = data.get("unified_msg_origin") or self._umo_cache.get(
                session_key
            )
            if not umo and session_key:
                pu = self.session_router.get_platform_user_by_session(session_key)
                if pu:
                    logger.warning(
                        "[HermesBridge] umo 缓存未命中 session_key=%s，响应可能丢失",
                        session_key,
                    )

            # 完成 Harness 任务
            if task_id:
                await self._complete_harness_task(task_id, response_text)

            # 推送给用户（带重试 + DLQ）
            if not umo:
                logger.error("[HermesBridge] 无法找到回传地址，写入 DLQ")
                await self._dlq_logger.log(
                    {
                        "ts": time.time(),
                        "task_id": task_id,
                        "target_umo": None,
                        "payload": {
                            "message": response_text,
                            "session_key": session_key,
                        },
                        "last_error": "umo_not_found",
                        "attempt_count": 0,
                    }
                )
                return web.json_response(
                    {"status": "queued_to_dlq", "reason": "umo_not_found"},
                    status=202,
                )

            outcome = await self._callback_dispatcher.send_with_retry(
                target_umo=umo,
                message=response_text,
                task_id=task_id,
                extra_payload={"session_key": session_key},
            )
            if outcome.success:
                # 推送成功 → 标 dedup（防 Hermes 多次推送同 task 导致洪水）
                if task_id:
                    finalized.add(task_id)
                logger.info(
                    "[HermesBridge] 已将 Hermes 结果推送至 %s (attempts=%d)",
                    umo,
                    outcome.attempts,
                )
                return web.json_response(
                    {"status": "ok", "umo": umo, "attempts": outcome.attempts}
                )

            logger.error(
                "[HermesBridge] 回群失败 umo=%s attempts=%d err=%s dlq=%s",
                umo,
                outcome.attempts,
                outcome.last_error,
                outcome.dlq_written,
            )
            return web.json_response(
                {
                    "status": "queued_to_dlq",
                    "umo": umo,
                    "attempts": outcome.attempts,
                    "last_error": outcome.last_error,
                    "dlq_written": outcome.dlq_written,
                },
                status=202,
            )

        except Exception as exc:
            logger.error("[HermesBridge] 处理响应失败：%s", exc)
            return web.json_response(
                {"status": "error", "message": str(exc)}, status=500
            )

    async def _handle_response_via_card(
        self, task_id: str, response_text: str
    ) -> CardHandleResult:
        """飞书卡片流接管 Hermes 响应。

        Returns:
            handled=True = 已通过卡片处理（短路旧流程，不再发重复文本）
            is_finalized=True = 卡片已成功进入终态，可加入 dedup
        """
        stream_map = getattr(self.context, "feishu_stream_map", None)
        streamers = ensure_streamers_on_context(self.context)
        if not stream_map or not streamers:
            return CardHandleResult()
        info = stream_map.get(task_id)
        if not info:
            return CardHandleResult()
        streamer = streamers.get(info.get("platform_id", ""))
        if streamer is None:
            return CardHandleResult()

        message_id = info.get("message_id", "")
        brief = info.get("brief", "深度分析")
        tier = info.get("reasoning_tier", "medium")

        # 判中间状态 vs 最终失败 vs 最终成功
        is_intermediate = bool(_HERMES_INTERMEDIATE_RE.search(response_text))
        is_final_failure = bool(_HERMES_FINAL_FAILURE_RE.search(response_text))
        # ⚠️ 关键修复：短文本（< 500 字）即便不含"Still working"关键词，
        # 也认为是中间状态。Hermes 中间会推短摘要，被误当最终结果导致 dedup
        # 锁把真正的长结果挡掉。Hermes 真最终方案通常 1k+ 字。
        if not is_intermediate and not is_final_failure and len(response_text) < 500:
            is_intermediate = True

        if is_intermediate and not is_final_failure:
            # 中间状态 → update 进度卡片（提取 iteration / stage 信息）
            stream = streamer.get_stream(message_id)
            elapsed = stream.elapsed_sec if stream else 0.0
            # 抽 iteration
            iters = None
            m_it = _re_card.search(r"iteration (\d+)/(\d+)", response_text)
            if m_it:
                iters = int(m_it.group(1))
            stage_match = _re_card.search(r"running:\s*([^)]+)", response_text)
            stage = stage_match.group(1).strip() if stage_match else None
            # 重试中显示在 stage
            if "Retrying" in response_text:
                stage = response_text[:120]

            new_card = build_progress_card(
                title="Hermes 深度分析",
                brief=brief,
                elapsed_sec=elapsed,
                current_stage=stage,
                iterations=iters,
                reasoning_tier=tier,
            )
            await streamer.update(message_id, new_card)
            return CardHandleResult(handled=True)

        if is_final_failure:
            # 最终失败 → finalize 错误卡片
            stream = streamer.get_stream(message_id)
            elapsed = stream.elapsed_sec if stream else 0.0
            err_card = build_error_card(
                title="Hermes 深度分析失败",
                error_msg=response_text[:1500],
                elapsed_sec=elapsed,
                retry_hint="可以让我换 Claude / Gemini 帮你过一遍，或检查网络后重试。",
            )
            try:
                finalized = await finalize_card_via_runtime(
                    streamer,
                    card_type="thinking_waiting",
                    message_id=message_id,
                    card=err_card,
                    platform_id="",
                    detail="hermes bridge failure finalized",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[HermesBridge] 卡片终态(失败) finalize 异常 task=%s: %s",
                    task_id[:8],
                    exc,
                )
                return CardHandleResult(handled=True)
            if not finalized:
                logger.warning(
                    "[HermesBridge] 卡片终态(失败) finalize 未成功 task=%s",
                    task_id[:8],
                )
                return CardHandleResult(handled=True)
            try:
                engine = getattr(self.context, "harness_engine", None)
                if engine:
                    await engine.fail_task(
                        task_id, reason="hermes_api_timeout_or_error"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[HermesBridge] fail_task 失败：%s", exc)
            stream_map.pop(task_id, None)
            logger.info(
                "[HermesBridge] 卡片终态(失败) task=%s elapsed=%.1fs",
                task_id[:8],
                elapsed,
            )
            return CardHandleResult(handled=True, is_finalized=True)

        # 最终成功 → finalize 结果卡片
        stream = streamer.get_stream(message_id)
        elapsed = stream.elapsed_sec if stream else 0.0
        final_card = build_final_card(
            title="Hermes 深度分析完成",
            result_md=response_text,
            elapsed_sec=elapsed,
            reasoning_tier=tier,
        )
        try:
            finalized = await finalize_card_via_runtime(
                streamer,
                card_type="thinking_waiting",
                message_id=message_id,
                card=final_card,
                platform_id="",
                detail="hermes bridge success finalized",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[HermesBridge] 卡片终态(成功) finalize 异常 task=%s: %s",
                task_id[:8],
                exc,
            )
            return CardHandleResult(handled=True)
        if not finalized:
            logger.warning(
                "[HermesBridge] 卡片终态(成功) finalize 未成功 task=%s",
                task_id[:8],
            )
            return CardHandleResult(handled=True)
        await self._complete_harness_task(task_id, response_text)
        stream_map.pop(task_id, None)
        logger.info(
            "[HermesBridge] 卡片终态(成功) task=%s elapsed=%.1fs len=%d",
            task_id[:8],
            elapsed,
            len(response_text),
        )
        return CardHandleResult(handled=True, is_finalized=True)

    async def _complete_harness_task(self, task_id: str, response_text: str) -> None:
        engine = getattr(self.context, "harness_engine", None)
        if engine is None:
            return
        try:
            task = None
            store = getattr(engine, "store", None)
            if store is not None:
                task = await store.get_task(task_id)
            if (
                task is not None
                and task.payload.get("workflow_kind") == "content_sop_workflow"
            ):
                await settle_content_sop_result(
                    engine,
                    task,
                    parse_workflow_result(response_text),
                )
                logger.info(
                    "[HermesBridge] Content SOP 任务 %s 已进入验收结算", task_id
                )
                return
            await engine.complete_task(
                task_id,
                result={
                    "summary": response_text[:200],
                    "response_preview": response_text[:500],
                    "source": "hermes",
                },
            )
            logger.info(
                "[HermesBridge] Harness 任务 %s 已通过 Hermes 结果完成", task_id
            )
            inbox_store = getattr(self.context, "ai_inbox_store", None)
            if inbox_store is not None:
                try:
                    item = await inbox_store.find_by_task_id(task_id)
                    if item is not None:
                        await inbox_store.update_item(
                            item.item_id,
                            status="delivered",
                            event_type="hermes_result_delivered",
                            event_payload={"task_id": task_id},
                        )
                except Exception as inbox_exc:
                    logger.debug(
                        "[HermesBridge] 更新 AI Inbox 交付状态失败：%s",
                        inbox_exc,
                    )
        except Exception as exc:
            logger.warning("[HermesBridge] 完成 Harness 任务 %s 失败：%s", task_id, exc)

    async def _send_to_platform_strict(self, umo: str, message: str) -> None:
        """通过 AstrBot platform adapter 发送，按错误类型抛 Retriable/Permanent。

        被 HermesCallbackDispatcher 调用。任何不属于"网络层可重试"的异常
        都抛 PermanentSendError，由 dispatcher 决定是否落 DLQ。
        """
        chain = MessageChain([Plain(message)])
        try:
            success = await self.context.send_message(umo, chain)
        except asyncio.TimeoutError as exc:
            raise RetriableSendError(f"timeout: {exc}") from exc
        except aiohttp.ServerDisconnectedError as exc:
            raise RetriableSendError(f"server_disconnected: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise RetriableSendError(f"connection: {exc}") from exc
        except aiohttp.ClientResponseError as exc:
            cls = classify_http_status(exc.status)
            if cls is RetriableSendError:
                raise RetriableSendError(f"http {exc.status}: {exc}") from exc
            raise PermanentSendError(f"http {exc.status}: {exc}") from exc
        except OSError as exc:
            raise RetriableSendError(f"os: {exc}") from exc
        if not success:
            raise PermanentSendError(
                f"context.send_message returned False for umo={umo}"
            )

    # ── 辅助方法 ──────────────────────────────────────────────────────────────

    async def _send_to_hermes(self, data: dict) -> None:
        try:
            body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            sig = hmac.new(
                self.hermes_secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.hermes_webhook_url,
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": f"sha256={sig}",
                        "X-Webhook-Event": "qq_message",
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        logger.error(
                            "[HermesBridge] Hermes 消息推送失败 HTTP %s", resp.status
                        )
        except Exception as exc:
            logger.error("[HermesBridge] _send_to_hermes 失败：%s", exc)

    async def _handle_toggle(
        self, event: AstrMessageEvent, user_id: str, cmd: str
    ) -> None:
        if cmd == "/hermes on" and not self.direct_chat_enabled:
            text = (
                "Hermes 直接接管模式当前已关闭。灰度测试请使用 /topic deep "
                "将当前话题交给 Hermes 后台深挖。"
            )
            await event.send(MessageChain([Plain(text)]))
            return

        if cmd == "/hermes on":
            self.hermes_enabled_users.add(user_id)
            text = "Hermes 模式已开启，消息将转发给 Hermes 处理。"
        elif cmd == "/hermes off":
            self.hermes_enabled_users.discard(user_id)
            text = "Hermes 模式已关闭，消息由 AstrBot 默认处理。"
        else:
            text = (
                "当前状态：Hermes 模式已开启。"
                if user_id in self.hermes_enabled_users
                else "当前状态：Hermes 模式已关闭。"
            )
        await event.send(MessageChain([Plain(text)]))

    # ── 灰度话题协作工作流 ────────────────────────────────────────────────────

    async def _handle_topic_command(
        self,
        event: AstrMessageEvent,
        user_id: str,
        text: str,
    ) -> bool:
        normalized = text.strip()
        lower = normalized.lower()
        if not (
            lower.startswith("/topic")
            or lower.startswith("/话题")
            or lower.startswith("/deep")
            or lower.startswith("/深挖")
        ):
            return False

        if self.topic_admin_only and not event.is_admin():
            await event.send(MessageChain([Plain("只有管理员可以操作灰度话题。")]))
            return True

        parts = normalized.split(maxsplit=2)
        root = parts[0].lower()
        action = ""
        body = ""
        if root in ("/deep", "/深挖"):
            action = "deep"
            body = normalized[len(parts[0]) :].strip()
        elif len(parts) >= 2:
            action = parts[1].lower()
            body = parts[2].strip() if len(parts) >= 3 else ""

        if action in ("new", "start", "发布", "新建"):
            await self._topic_new(event, body)
            return True
        if action in ("deep", "research", "深挖", "升级"):
            await self._topic_deep(event, body)
            return True
        if action in ("status", "状态"):
            await self._topic_status(event)
            return True
        if action in ("intro", "guide", "说明", "须知"):
            await self._topic_intro(event)
            return True
        if action in ("rollcall", "点名"):
            await self._topic_rollcall_command(event, body)
            return True
        if action in ("distill", "digest", "蒸馏", "提炼"):
            await self._topic_distill(event)
            return True
        if action in ("close", "done", "关闭", "完成"):
            await self._topic_close(event, user_id, body)
            return True

        await event.send(
            MessageChain(
                [
                    Plain(
                        "灰度话题指令：\n"
                        "- /topic new <话题>\n"
                        "- /topic deep [深挖原因]\n"
                        "- /topic status\n"
                        "- /topic intro\n"
                        "- /topic rollcall start\n"
                        "- /topic distill\n"
                        "- /topic close [结论]"
                    )
                ]
            )
        )
        return True

    async def _handle_conversation_distill_command(
        self,
        event: AstrMessageEvent,
        text: str,
    ) -> bool:
        normalized = text.strip()
        if normalized.startswith("__card_action__:"):
            return await self._handle_skill_card_action(event, normalized)

        lower = normalized.lower()
        if not (lower.startswith("/chat") or lower.startswith("/私聊")):
            return False

        parts = normalized.split(maxsplit=1)
        action_raw = parts[1].strip() if len(parts) >= 2 else "distill"
        action = action_raw.lower()
        if action in ("distill", "digest", "summary", "蒸馏", "提炼", "总结", ""):
            await self._conversation_distill(event)
            return True
        if action in ("list-bosses", "bosses", "老板列表"):
            await self._conversation_skill_list(event, kind="boss")
            return True
        if action in ("list-colleagues", "colleagues", "同事列表"):
            await self._conversation_skill_list(event, kind="colleague")
            return True
        if action in ("deleted-bosses", "boss-deleted", "已删除老板"):
            await self._conversation_deleted_skill_list(event, kind="boss")
            return True
        if action in ("deleted-colleagues", "colleague-deleted", "已删除同事"):
            await self._conversation_deleted_skill_list(event, kind="colleague")
            return True
        if action.startswith(("boss-rollback", "rollback-boss", "老板回滚")):
            await self._conversation_skill_rollback(
                event,
                kind="boss",
                args=self._extract_skill_target_name(
                    action_raw,
                    ("boss-rollback", "rollback-boss", "老板回滚"),
                ),
            )
            return True
        if action.startswith(("colleague-rollback", "rollback-colleague", "同事回滚")):
            await self._conversation_skill_rollback(
                event,
                kind="colleague",
                args=self._extract_skill_target_name(
                    action_raw,
                    ("colleague-rollback", "rollback-colleague", "同事回滚"),
                ),
            )
            return True
        if action.startswith(("inspect-boss", "boss-inspect", "查看老板")):
            await self._conversation_skill_inspect(
                event,
                kind="boss",
                slug_or_name=self._extract_skill_target_name(
                    action_raw,
                    ("inspect-boss", "boss-inspect", "查看老板"),
                ),
            )
            return True
        if action.startswith(("inspect-colleague", "colleague-inspect", "查看同事")):
            await self._conversation_skill_inspect(
                event,
                kind="colleague",
                slug_or_name=self._extract_skill_target_name(
                    action_raw,
                    ("inspect-colleague", "colleague-inspect", "查看同事"),
                ),
            )
            return True
        if action.startswith(("review-boss", "boss-review", "审阅老板")):
            await self._conversation_skill_review(
                event,
                kind="boss",
                slug_or_name=self._extract_skill_target_name(
                    action_raw,
                    ("review-boss", "boss-review", "审阅老板"),
                ),
            )
            return True
        if action.startswith(("review-colleague", "colleague-review", "审阅同事")):
            await self._conversation_skill_review(
                event,
                kind="colleague",
                slug_or_name=self._extract_skill_target_name(
                    action_raw,
                    ("review-colleague", "colleague-review", "审阅同事"),
                ),
            )
            return True
        if action.startswith(("correct-boss", "boss-correct", "纠正老板")):
            await self._conversation_skill_correct(
                event,
                kind="boss",
                args=self._extract_skill_target_name(
                    action_raw,
                    ("correct-boss", "boss-correct", "纠正老板"),
                ),
            )
            return True
        if action.startswith(("correct-colleague", "colleague-correct", "纠正同事")):
            await self._conversation_skill_correct(
                event,
                kind="colleague",
                args=self._extract_skill_target_name(
                    action_raw,
                    ("correct-colleague", "colleague-correct", "纠正同事"),
                ),
            )
            return True
        if action.startswith(("restore-boss", "boss-restore", "恢复老板")):
            await self._conversation_skill_restore(
                event,
                kind="boss",
                slug_or_name=self._extract_skill_target_name(
                    action_raw,
                    ("restore-boss", "boss-restore", "恢复老板"),
                ),
            )
            return True
        if action.startswith(("restore-colleague", "colleague-restore", "恢复同事")):
            await self._conversation_skill_restore(
                event,
                kind="colleague",
                slug_or_name=self._extract_skill_target_name(
                    action_raw,
                    ("restore-colleague", "colleague-restore", "恢复同事"),
                ),
            )
            return True
        if action.startswith(("delete-boss", "boss-delete", "删除老板")):
            await self._conversation_skill_delete(
                event,
                kind="boss",
                slug_or_name=self._extract_skill_target_name(
                    action_raw,
                    ("delete-boss", "boss-delete", "删除老板"),
                ),
            )
            return True
        if action.startswith(("delete-colleague", "colleague-delete", "删除同事")):
            await self._conversation_skill_delete(
                event,
                kind="colleague",
                slug_or_name=self._extract_skill_target_name(
                    action_raw,
                    ("delete-colleague", "colleague-delete", "删除同事"),
                ),
            )
            return True
        if action.startswith(("create-boss", "boss", "老板")):
            name = self._extract_skill_target_name(
                action_raw,
                ("create-boss", "boss", "老板"),
            )
            await self._conversation_skill_create(event, kind="boss", name=name)
            return True
        if action.startswith(("create-colleague", "colleague", "同事")):
            name = self._extract_skill_target_name(
                action_raw,
                ("create-colleague", "colleague", "同事"),
            )
            await self._conversation_skill_create(event, kind="colleague", name=name)
            return True

        await event.send(
            MessageChain(
                [
                    Plain(
                        "会话蒸馏指令：\n"
                        "- /chat distill\n"
                        "- /私聊 distill\n"
                        "- /chat create-boss <老板姓名>\n"
                        "- /chat create-colleague <同事姓名>\n"
                        "- /chat list-bosses\n"
                        "- /chat list-colleagues\n"
                        "- /chat deleted-bosses\n"
                        "- /chat deleted-colleagues\n"
                        "- /chat inspect-boss <slug>\n"
                        "- /chat inspect-colleague <slug>\n"
                        "- /chat review-boss <slug>\n"
                        "- /chat review-colleague <slug>\n"
                        "- /chat correct-boss <slug> <修正内容>\n"
                        "- /chat correct-colleague <slug> <修正内容>\n"
                        "- /chat boss-rollback <slug> <version>\n"
                        "- /chat colleague-rollback <slug> <version>\n"
                        "- /chat delete-boss <slug>\n"
                        "- /chat delete-colleague <slug>\n"
                        "- /chat restore-boss <slug>\n"
                        "- /chat restore-colleague <slug>\n"
                        "当前入口只处理普通群聊和私聊，不启用原灰度话题工作流。"
                    )
                ]
            )
        )
        return True

    def _extract_skill_target_name(
        self,
        action: str,
        prefixes: tuple[str, ...],
    ) -> str:
        for prefix in prefixes:
            lowered = action.lower()
            if lowered == prefix:
                return ""
            if lowered.startswith(prefix + " "):
                return action[len(prefix) :].strip()
        return ""

    def _is_trusted_skill_card_action(self, event: AstrMessageEvent) -> bool:
        msg = getattr(event, "message_obj", None)
        return (
            getattr(event, "is_card_action", False) is True
            or getattr(msg, "is_card_action", False) is True
        )

    def _skill_card_chat_id(self, event: AstrMessageEvent) -> tuple[str, str]:
        msg = getattr(event, "message_obj", None)
        payload = getattr(msg, "card_action_payload", {}) or {}
        chat_id = str(payload.get("open_chat_id") or "").strip()
        if not chat_id:
            raw = getattr(msg, "raw_message", None)
            chat_id = str(getattr(raw, "chat_id", None) or "").strip()
        if not chat_id:
            get_group_id = getattr(event, "get_group_id", None)
            group_id = str(get_group_id() or "") if callable(get_group_id) else ""
            chat_id = group_id or str(event.get_sender_id() or "")
        receive_id_type = "chat_id" if chat_id.startswith("oc_") else "open_id"
        return chat_id, receive_id_type

    async def _send_skill_card(
        self,
        event: AstrMessageEvent,
        card: dict,
    ) -> str | None:
        context = getattr(self, "context", None)
        if context is None:
            return None
        streamer = ensure_streamers_on_context(context).get(
            event.get_platform_id() or ""
        )
        if streamer is None:
            return None
        chat_id, receive_id_type = self._skill_card_chat_id(event)
        if not chat_id:
            return None
        stream = await send_card_via_runtime(
            streamer,
            card_type="skill_review",
            chat_id=chat_id,
            receive_id_type=receive_id_type,
            card=card,
            platform_id=event.get_platform_id() or "",
            event="start",
            detail="hermes skill card",
        )
        return stream.message_id if stream else None

    async def _handle_skill_card_action(
        self,
        event: AstrMessageEvent,
        text: str,
    ) -> bool:
        if not self._is_trusted_skill_card_action(event):
            logger.warning(
                "[HermesBridge] 拒绝非可信 Skill 卡片动作 sender=%s",
                str(event.get_sender_id() or "")[:12],
            )
            return True
        try:
            payload = json.loads(text[len("__card_action__:") :])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[HermesBridge] Skill 卡片 payload 解析失败：%s", exc)
            return True

        value = payload.get("value", {}) or {}
        if value.get("source") != "hermes_skill_card":
            return False

        action = str(value.get("action") or "").strip()
        kind = str(value.get("kind") or "").strip()
        slug = self._slugify_skill_name(str(value.get("slug") or ""))
        version = str(value.get("version") or "").strip()
        if kind not in {"boss", "colleague"}:
            await event.send(MessageChain([Plain("⚠️ 未识别的 skill 类型。")]))
            return True

        if action == "inspect":
            await self._conversation_skill_inspect(event, kind=kind, slug_or_name=slug)
        elif action == "review":
            await self._conversation_skill_review(event, kind=kind, slug_or_name=slug)
        elif action == "correct_request":
            await event.send(
                MessageChain(
                    [
                        Plain(
                            "纠错请直接回复命令："
                            f"/chat correct-{'boss' if kind == 'boss' else 'colleague'} "
                            f"{slug} <修正内容>"
                        )
                    ]
                )
            )
        elif action == "delete_request":
            await self._send_skill_delete_confirm_card(event, kind=kind, slug=slug)
        elif action == "rollback_request":
            await self._send_skill_rollback_confirm_card(
                event,
                kind=kind,
                slug=slug,
                version=version,
            )
        elif action == "delete_confirm":
            if not await self._require_skill_admin(event):
                return True
            ok, message = self._soft_delete_skill_bundle(kind, slug)
            await event.send(MessageChain([Plain(message if ok else f"⚠️ {message}")]))
        elif action == "rollback_confirm":
            if not await self._require_skill_admin(event):
                return True
            if not version:
                await event.send(MessageChain([Plain("⚠️ 回滚缺少 version。")]))
                return True
            ok, message = self._rollback_skill_bundle(kind, slug, version)
            await event.send(MessageChain([Plain(message if ok else f"⚠️ {message}")]))
        elif action == "restore":
            if not await self._require_skill_admin(event):
                return True
            ok, message = self._restore_deleted_skill_bundle(kind, slug)
            await event.send(MessageChain([Plain(message if ok else f"⚠️ {message}")]))
        elif action == "inspect_deleted_path":
            path = str(value.get("path") or "").strip() or "未记录路径"
            await event.send(MessageChain([Plain(f"已删除 skill 路径：{path}")]))
        elif action == "cancel":
            await event.send(MessageChain([Plain("已取消。")]))
        else:
            await event.send(MessageChain([Plain(f"⚠️ 未识别的卡片动作：{action}")]))
        return True

    async def _send_skill_delete_confirm_card(
        self,
        event: AstrMessageEvent,
        *,
        kind: str,
        slug: str,
    ) -> None:
        card = build_skill_confirm_card(
            operation="delete",
            kind=kind,
            slug=slug,
            operator_id=str(event.get_sender_id() or ""),
            risk_note="会把当前 Skill 移入回收站，恢复前不会再参与后续命中。",
        )
        if not await self._send_skill_card(event, card):
            await event.send(
                MessageChain([Plain("删除会把 skill 移入回收区，请确认后再点确认。")])
            )

    async def _send_skill_rollback_confirm_card(
        self,
        event: AstrMessageEvent,
        *,
        kind: str,
        slug: str,
        version: str,
    ) -> None:
        if not version:
            version = self._latest_skill_backup_version(kind, slug)
        if not version:
            await event.send(
                MessageChain(
                    [
                        Plain(
                            "⚠️ 当前没有可自动回滚的备份版本。"
                            "请用 /chat boss-rollback <slug> <version> --confirm "
                            "或 /chat colleague-rollback <slug> <version> --confirm 指定版本。"
                        )
                    ]
                )
            )
            return
        card = build_skill_confirm_card(
            operation="rollback",
            kind=kind,
            slug=slug,
            version=version,
            operator_id=str(event.get_sender_id() or ""),
            risk_note="会用备份版本覆盖当前 Skill，请确认版本号无误。",
        )
        if not await self._send_skill_card(event, card):
            await event.send(
                MessageChain([Plain("回滚会覆盖当前 skill，请确认版本后再点确认。")])
            )

    async def _conversation_skill_create(
        self,
        event: AstrMessageEvent,
        *,
        kind: str,
        name: str,
    ) -> None:
        discussion = self._conversation_discussion_snapshot(event)
        if not discussion:
            await event.send(
                MessageChain([Plain("当前会话还没有可生成 skill 的材料。")])
            )
            return

        target_name = name.strip() or ("老板" if kind == "boss" else "同事")
        if self._is_protected_skill_identity(target_name):
            await event.send(
                MessageChain(
                    [
                        Plain(
                            f"⚠️ {target_name} 是系统受保护身份，不能生成老板/同事 skill。"
                        )
                    ]
                )
            )
            return
        session_title = "当前群聊" if event.is_group() else "当前私聊"
        distillation = self._distill_grey_topic(
            _DistillationTopic(
                task_id=self._conversation_cache_key(event),
                title=session_title,
            ),
            discussion,
            source_scope="normal_chat",
            conversation_type="group" if event.is_group() else "private",
        )
        result = self._write_skill_bundle(
            kind=kind,
            name=target_name,
            distillation=distillation,
            discussion=discussion,
            event=event,
        )
        await event.send(
            MessageChain(
                [
                    Plain(
                        f"✅ 已生成 {result.kind} skill：{result.name}\n"
                        f"- slug: {result.slug}\n"
                        f"- 目录: {result.directory}\n"
                        f"- 文件: {', '.join(result.files)}"
                    )
                ]
            )
        )

    async def _conversation_skill_list(
        self,
        event: AstrMessageEvent,
        *,
        kind: str,
    ) -> None:
        bundles = self._list_skill_bundles(kind)
        title = "老板 skills" if kind == "boss" else "同事 skills"
        if not bundles:
            await event.send(MessageChain([Plain(f"当前还没有生成任何{title}。")]))
            return
        if await self._send_skill_card(
            event,
            build_skill_list_card(kind=kind, skills=bundles),
        ):
            return
        lines = [f"【{title}】"]
        for item in bundles[:30]:
            lines.append(
                f"- {item['name']} / {item['slug']} / {item['version']} / {item['path']}"
            )
        await event.send(MessageChain([Plain("\n".join(lines))]))

    async def _conversation_deleted_skill_list(
        self,
        event: AstrMessageEvent,
        *,
        kind: str,
    ) -> None:
        bundles = self._list_deleted_skill_bundles(kind)
        title = "已删除老板 skills" if kind == "boss" else "已删除同事 skills"
        if not bundles:
            await event.send(MessageChain([Plain(f"当前没有{title}。")]))
            return
        if await self._send_skill_card(
            event,
            build_deleted_skill_list_card(kind=kind, skills=bundles),
        ):
            return
        lines = [f"【{title}】"]
        for item in bundles[:30]:
            lines.append(
                f"- {item['name']} / {item['slug']} / {item['deleted_at']} / {item['path']}"
            )
        await event.send(MessageChain([Plain("\n".join(lines))]))

    async def _conversation_skill_rollback(
        self,
        event: AstrMessageEvent,
        *,
        kind: str,
        args: str,
    ) -> None:
        parts = args.split()
        if len(parts) < 2:
            await event.send(
                MessageChain(
                    [
                        Plain(
                            "回滚用法：/chat boss-rollback <slug> <version> --confirm "
                            "或 /chat colleague-rollback <slug> <version> --confirm"
                        )
                    ]
                )
            )
            return
        if not await self._require_skill_admin(event):
            return
        args, confirmed = self._split_skill_confirmation(args)
        parts = args.split()
        if self.skill_ops_require_confirm and not confirmed:
            await event.send(
                MessageChain(
                    [
                        Plain(
                            "回滚会覆盖当前 skill，请在命令末尾追加 --confirm 或 确认。"
                        )
                    ]
                )
            )
            return
        if len(parts) < 2:
            await event.send(
                MessageChain(
                    [
                        Plain(
                            "回滚用法：/chat boss-rollback <slug> <version> --confirm "
                            "或 /chat colleague-rollback <slug> <version> --confirm"
                        )
                    ]
                )
            )
            return
        slug = self._slugify_skill_name(parts[0])
        version = parts[1]
        ok, message = self._rollback_skill_bundle(kind, slug, version)
        await event.send(MessageChain([Plain(message if ok else f"⚠️ {message}")]))

    async def _conversation_skill_inspect(
        self,
        event: AstrMessageEvent,
        *,
        kind: str,
        slug_or_name: str,
    ) -> None:
        slug = self._slugify_skill_name(slug_or_name)
        if not slug or slug == "unknown":
            await event.send(
                MessageChain(
                    [
                        Plain(
                            "查看用法：/chat inspect-boss <slug> "
                            "或 /chat inspect-colleague <slug>"
                        )
                    ]
                )
            )
            return
        detail = self._skill_bundle_detail(kind, slug)
        if detail and await self._send_skill_card(
            event,
            build_skill_detail_card(kind=kind, skill=detail),
        ):
            return
        ok, message = self._inspect_skill_bundle(kind, slug)
        await event.send(MessageChain([Plain(message if ok else f"⚠️ {message}")]))

    async def _conversation_skill_review(
        self,
        event: AstrMessageEvent,
        *,
        kind: str,
        slug_or_name: str,
    ) -> None:
        slug = self._slugify_skill_name(slug_or_name)
        if not slug or slug == "unknown":
            await event.send(
                MessageChain(
                    [
                        Plain(
                            "审阅用法：/chat review-boss <slug> "
                            "或 /chat review-colleague <slug>"
                        )
                    ]
                )
            )
            return
        review = self._skill_bundle_review(kind, slug)
        if review and await self._send_skill_card(
            event,
            build_skill_review_card(kind=kind, review=review),
        ):
            return
        ok, message = self._review_skill_bundle(kind, slug)
        await event.send(MessageChain([Plain(message if ok else f"⚠️ {message}")]))

    async def _conversation_skill_correct(
        self,
        event: AstrMessageEvent,
        *,
        kind: str,
        args: str,
    ) -> None:
        if not await self._require_skill_admin(event):
            return
        slug, correction = self._split_skill_slug_and_text(args)
        if not slug or not correction:
            await event.send(
                MessageChain(
                    [
                        Plain(
                            "纠错用法：/chat correct-boss <slug> <修正内容> "
                            "或 /chat correct-colleague <slug> <修正内容>"
                        )
                    ]
                )
            )
            return
        ok, message = self._append_skill_correction(
            kind,
            self._slugify_skill_name(slug),
            correction,
            event,
        )
        await event.send(MessageChain([Plain(message if ok else f"⚠️ {message}")]))

    async def _conversation_skill_delete(
        self,
        event: AstrMessageEvent,
        *,
        kind: str,
        slug_or_name: str,
    ) -> None:
        if not await self._require_skill_admin(event):
            return
        slug_or_name, confirmed = self._split_skill_confirmation(slug_or_name)
        slug = self._slugify_skill_name(slug_or_name)
        if not slug or slug == "unknown":
            await event.send(
                MessageChain(
                    [
                        Plain(
                            "删除用法：/chat delete-boss <slug> --confirm "
                            "或 /chat delete-colleague <slug> --confirm"
                        )
                    ]
                )
            )
            return
        if self.skill_ops_require_confirm and not confirmed:
            await event.send(
                MessageChain(
                    [
                        Plain(
                            "删除会把 skill 移入回收区，请在命令末尾追加 --confirm 或 确认。"
                        )
                    ]
                )
            )
            return
        ok, message = self._soft_delete_skill_bundle(kind, slug)
        await event.send(MessageChain([Plain(message if ok else f"⚠️ {message}")]))

    async def _conversation_skill_restore(
        self,
        event: AstrMessageEvent,
        *,
        kind: str,
        slug_or_name: str,
    ) -> None:
        if not await self._require_skill_admin(event):
            return
        slug = self._slugify_skill_name(slug_or_name)
        if not slug or slug == "unknown":
            await event.send(
                MessageChain(
                    [
                        Plain(
                            "恢复用法：/chat restore-boss <slug> "
                            "或 /chat restore-colleague <slug>"
                        )
                    ]
                )
            )
            return
        ok, message = self._restore_deleted_skill_bundle(kind, slug)
        await event.send(MessageChain([Plain(message if ok else f"⚠️ {message}")]))

    async def _require_skill_admin(self, event: AstrMessageEvent) -> bool:
        if not getattr(self, "skill_admin_only", True):
            return True
        sender_id = str(event.get_sender_id() or "").strip()
        if sender_id and sender_id in getattr(self, "skill_admin_user_ids", set()):
            return True
        try:
            if bool(event.is_admin()):
                return True
        except Exception:  # noqa: BLE001
            pass
        await event.send(
            MessageChain([Plain("⚠️ 只有管理员可以执行 skill 回滚、删除或恢复。")])
        )
        return False

    def _split_skill_confirmation(self, raw: str) -> tuple[str, bool]:
        tokens = raw.split()
        cleaned: list[str] = []
        confirmed = False
        for token in tokens:
            if token.lower() in ("--confirm", "confirm") or token in (
                "确认",
                "确认执行",
            ):
                confirmed = True
                continue
            cleaned.append(token)
        return " ".join(cleaned), confirmed

    def _split_skill_slug_and_text(self, raw: str) -> tuple[str, str]:
        parts = raw.strip().split(maxsplit=1)
        if len(parts) < 2:
            return "", ""
        return parts[0].strip(), parts[1].strip()

    def _normalize_identity_name(self, value: object) -> str:
        return _re_card.sub(r"\s+", "", str(value or "")).lower()

    def _is_protected_skill_identity(self, name: str) -> bool:
        normalized = self._normalize_identity_name(name)
        if not normalized:
            return False
        return normalized in getattr(self, "protected_identity_names", set())

    async def _topic_new(self, event: AstrMessageEvent, brief: str) -> None:
        if not brief.strip():
            await event.send(
                MessageChain([Plain("请输入话题内容。用法：/topic new <话题>")])
            )
            return

        engine = self.context.harness_engine
        if engine is None:
            await event.send(MessageChain([Plain("Harness 引擎未初始化。")]))
            return

        conversation_id = await self._get_or_create_current_conversation_id(event)
        task = await engine.create_task(
            create_workflow_request(
                workflow_kind="project_followup",
                brief=brief.strip(),
                conversation_id=conversation_id,
                platform_id=event.get_platform_id(),
                session_id=event.unified_msg_origin,
                source="grey_topic",
                message_text=event.message_str,
            )
        )
        await engine.append_trace(
            task.task_id,
            "topic_opened",
            {
                "topic_id": task.task_id,
                "opened_by": event.get_sender_id(),
                "platform_id": event.get_platform_id(),
                "session_id": event.unified_msg_origin,
                "brief": brief.strip(),
            },
        )
        self._topic_discussion_cache[task.task_id].clear()
        self._topic_rollcall.pop(task.task_id, None)

        await event.send(
            MessageChain(
                [
                    Plain(
                        "已发布灰度话题：\n"
                        f"- topic_id: {task.task_id[:8]}\n"
                        f"- task_id: {task.task_id}\n"
                        "- status: discussing\n"
                        "群内后续讨论会挂到这个话题；需要后台深挖时发送 /topic deep。"
                    )
                ]
            )
        )
        if self.topic_intro_on_new:
            await event.send(MessageChain([Plain(self._grey_topic_intro_message())]))

    async def _topic_deep(self, event: AstrMessageEvent, reason: str) -> None:
        engine = self.context.harness_engine
        if engine is None:
            await event.send(MessageChain([Plain("Harness 引擎未初始化。")]))
            return

        task = await self._get_active_topic_task(event)
        if task is None:
            await event.send(MessageChain([Plain("当前群没有进行中的灰度话题。")]))
            return

        discussion = await self._topic_discussion_snapshot(task.task_id)
        deep_reason = reason.strip() or event.message_str.strip()
        distillation = self._distill_grey_topic(
            task,
            discussion,
            trigger_reason=deep_reason,
        )
        await engine.append_trace(
            task.task_id,
            "topic_deep_research_requested",
            {
                "requested_by": event.get_sender_id(),
                "reason": deep_reason,
                "discussion_count": len(discussion),
                "discussion": discussion,
                "distillation": distillation,
            },
        )
        await engine.mark_in_progress(task.task_id, note="topic_deep_research")

        cognitive_context = dict(task.payload.get("cognitive_context", {}) or {})
        cognitive_context["grey_topic"] = {
            "topic_id": task.task_id,
            "title": task.title,
            "status": "needs_deep_research",
            "trigger_reason": deep_reason,
            "discussion": discussion,
            "distillation": distillation,
        }
        cognitive_context["grey_topic_distillation"] = distillation
        ok = await self.dispatch_task_to_hermes(
            task.task_id,
            str(task.payload.get("workflow_kind") or "project_followup"),
            str(task.payload.get("brief") or task.title),
            event.unified_msg_origin,
            cognitive_context,
        )
        status = "Hermes 已开始后台深挖" if ok else "Hermes 派发失败，请稍后重试"
        await event.send(
            MessageChain(
                [
                    Plain(
                        f"{status}：\n"
                        f"- topic_id: {task.task_id[:8]}\n"
                        f"- task_id: {task.task_id}\n"
                        f"- collected_messages: {len(discussion)}"
                    )
                ]
            )
        )

    async def _topic_status(self, event: AstrMessageEvent) -> None:
        task = await self._get_active_topic_task(event)
        if task is None:
            await event.send(MessageChain([Plain("当前群没有进行中的灰度话题。")]))
            return
        discussion = await self._topic_discussion_snapshot(task.task_id)
        rollcall = self._topic_rollcall.get(task.task_id, {})
        checked_in = rollcall.get("checked_in", {})
        await event.send(
            MessageChain(
                [
                    Plain(
                        "当前灰度话题：\n"
                        f"- topic_id: {task.task_id[:8]}\n"
                        f"- task_id: {task.task_id}\n"
                        f"- status: {task.status}\n"
                        f"- title: {task.title}\n"
                        f"- collected_messages: {len(discussion)}\n"
                        f"- rollcall_checked_in: {len(checked_in)}"
                    )
                ]
            )
        )

    async def _topic_intro(self, event: AstrMessageEvent) -> None:
        await event.send(MessageChain([Plain(self._grey_topic_intro_message())]))

    def _grey_topic_intro_message(self) -> str:
        return (
            "【灰度测试说明】\n"
            "本群用于验证“话题讨论 + 后台深挖 + 满意度闭环”的协作流程。\n\n"
            "一、入群准备\n"
            "1. 请每位成员主动把群名片改成：姓名-部门-角色，例如“蔡挺-市场部-负责人”。\n"
            "2. 群名片用于推广 1 号识别发言角色、统计点名和整理讨论，不改名片会影响测试记录准确性。\n"
            "3. QQ 群和飞书群默认先禁言 10 分钟，用于成员进群、改名片和阅读说明。\n"
            "4. 禁言解除后，群管理会发起点名；请每个人回复 1 或 到，确认在场后再开始测试。\n\n"
            "参与角色：\n"
            "1. 老板/发起人：发布本轮话题，判断最终方向是否满意。\n"
            "2. 部门负责人：补充业务背景、约束条件、判断标准和风险点。\n"
            "3. 员工/评审成员：直接提出疑问、反例、补充资料和不满意点。\n"
            "4. 业务 bot（推广 1 号）：记录群内讨论；需要深挖时把当前话题交给后台 Hermes 处理。\n\n"
            "二、基础指令\n"
            "- 发布话题：/topic new <本轮要讨论的问题>\n"
            "- 发起点名：/topic rollcall start\n"
            "- 查看点名：/topic rollcall status\n"
            "- 结束点名：/topic rollcall end\n"
            "- 后台深挖：/topic deep <为什么还不满意或需要补充什么>\n"
            "- 查看话题：/topic status\n"
            "- 查看蒸馏摘要：/topic distill\n"
            "- 重发说明：/topic intro\n"
            "- 结束本轮：/topic close <最终结论>\n\n"
            "三、自然语言范围\n"
            "大家可以直接用正常工作语言讨论，例如：\n"
            "- 我觉得这个方案不落地，因为……\n"
            "- 这里缺少预算/时间/负责人/风险判断。\n"
            "- 老板要看的结论应该是……\n"
            "- 员工执行时可能会卡在……\n"
            "- 这个点需要 Hermes 后台继续深挖。\n\n"
            "四、测试规则\n"
            "1. 群内只保留推广 1 号一个业务 bot，其他成员都按真人身份发言。\n"
            "2. 不需要为了机器人改变说话方式；请像真实会议一样提出问题、分歧和不满意点。\n"
            "3. 如果只是继续讨论，直接发自然语言；如果明确要后台研究，请使用 /topic deep。\n"
            "4. 只有确认过的业务事实、流程、标准话术和产品资料会作为知识库候选。"
        )

    async def _topic_rollcall_command(
        self,
        event: AstrMessageEvent,
        body: str,
    ) -> None:
        task = await self._get_active_topic_task(event)
        if task is None:
            await event.send(MessageChain([Plain("当前群没有进行中的灰度话题。")]))
            return

        action = (body or "start").strip().lower()
        if action in ("start", "开始", ""):
            self._topic_rollcall[task.task_id] = {
                "active": True,
                "checked_in": {},
                "started_by": event.get_sender_id(),
            }
            await self._append_topic_event(
                task.task_id,
                "topic_rollcall_started",
                {
                    "started_by": event.get_sender_id(),
                    "session_id": event.unified_msg_origin,
                },
            )
            await event.send(
                MessageChain(
                    [
                        Plain(
                            "【点名开始】\n"
                            "禁言解除后，请所有参与灰度测试的成员回复：1 或 到。\n"
                            "推广 1 号会自动登记，登记完成后再开始正式测试。"
                        )
                    ]
                )
            )
            return

        if action in ("status", "状态"):
            rollcall = self._topic_rollcall.get(task.task_id, {})
            checked_in = rollcall.get("checked_in", {})
            lines = [
                "【点名状态】",
                f"- topic_id: {task.task_id[:8]}",
                f"- active: {bool(rollcall.get('active'))}",
                f"- checked_in: {len(checked_in)}",
            ]
            for name in list(checked_in.values())[:20]:
                lines.append(f"- {name}")
            await event.send(MessageChain([Plain("\n".join(lines))]))
            return

        if action in ("end", "stop", "结束", "停止"):
            rollcall = self._topic_rollcall.setdefault(
                task.task_id,
                {"checked_in": {}},
            )
            rollcall["active"] = False
            checked_in = rollcall.get("checked_in", {})
            await self._append_topic_event(
                task.task_id,
                "topic_rollcall_finished",
                {
                    "finished_by": event.get_sender_id(),
                    "checked_in_count": len(checked_in),
                    "checked_in": checked_in,
                },
            )
            await event.send(
                MessageChain(
                    [
                        Plain(
                            "【点名结束】\n"
                            f"已登记 {len(checked_in)} 人。可以开始本轮灰度测试。"
                        )
                    ]
                )
            )
            return

        await event.send(
            MessageChain(
                [
                    Plain(
                        "点名指令：\n"
                        "- /topic rollcall start\n"
                        "- /topic rollcall status\n"
                        "- /topic rollcall end"
                    )
                ]
            )
        )

    async def _handle_rollcall_reply(
        self,
        event: AstrMessageEvent,
        user_id: str,
        text: str,
    ) -> bool:
        if text.strip().lower() not in ("1", "到"):
            return False
        task = await self._get_active_topic_task(event)
        if task is None:
            return False
        rollcall = self._topic_rollcall.get(task.task_id)
        if not rollcall or not rollcall.get("active"):
            return False

        checked_in = rollcall.setdefault("checked_in", {})
        sender_name = event.get_sender_name() or user_id
        checked_in[user_id] = sender_name
        await self._append_topic_event(
            task.task_id,
            "topic_rollcall_checkin",
            {
                "sender_id": user_id,
                "sender_name": sender_name,
                "session_id": event.unified_msg_origin,
            },
        )
        await event.send(MessageChain([Plain(f"已登记：{sender_name}")]))
        return True

    async def _topic_distill(self, event: AstrMessageEvent) -> None:
        task = await self._get_active_topic_task(event)
        if task is None:
            await self._conversation_distill(event)
            return
        discussion = await self._topic_discussion_snapshot(task.task_id)
        distillation = self._distill_grey_topic(task, discussion)
        await self._append_topic_distillation(task.task_id, distillation)
        await event.send(MessageChain([Plain(self._format_distillation(distillation))]))

    async def _conversation_distill(self, event: AstrMessageEvent) -> None:
        discussion = self._conversation_discussion_snapshot(event)
        if not discussion:
            await event.send(MessageChain([Plain("当前会话还没有可蒸馏的聊天内容。")]))
            return

        session_title = "当前群聊" if event.is_group() else "当前私聊"
        task = _DistillationTopic(
            task_id=self._conversation_cache_key(event),
            title=session_title,
        )
        distillation = self._distill_grey_topic(
            task,
            discussion,
            source_scope="normal_chat",
            conversation_type="group" if event.is_group() else "private",
        )
        await event.send(MessageChain([Plain(self._format_distillation(distillation))]))

    def _write_skill_bundle(
        self,
        *,
        kind: str,
        name: str,
        distillation: dict,
        discussion: list[dict],
        event: AstrMessageEvent,
    ) -> SkillBundleWriteResult:
        slug = self._slugify_skill_name(name)
        base_dir = Path(
            getattr(self, "skill_bundle_base_dir", Path(__file__).resolve().parents[3])
        )
        root = base_dir / ("bosses" if kind == "boss" else "colleagues") / slug
        root.mkdir(parents=True, exist_ok=True)
        (root / "versions").mkdir(exist_ok=True)
        (root / "knowledge" / "messages").mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc).isoformat()
        existing_meta = self._load_skill_meta(root)
        next_version = self._next_skill_version(existing_meta)
        backup_dir = self._backup_existing_skill_bundle(root, existing_meta, now)
        if kind == "boss":
            files = self._write_boss_skill_bundle(
                root=root,
                name=name,
                slug=slug,
                distillation=distillation,
                discussion=discussion,
                event=event,
                now=now,
                version=next_version,
                existing_meta=existing_meta,
                backup_dir=backup_dir,
            )
        else:
            files = self._write_colleague_skill_bundle(
                root=root,
                name=name,
                slug=slug,
                distillation=distillation,
                discussion=discussion,
                event=event,
                now=now,
                version=next_version,
                existing_meta=existing_meta,
                backup_dir=backup_dir,
            )
        return SkillBundleWriteResult(
            kind="老板" if kind == "boss" else "同事",
            name=name,
            slug=slug,
            directory=root,
            files=tuple(files),
        )

    def _write_boss_skill_bundle(
        self,
        *,
        root: Path,
        name: str,
        slug: str,
        distillation: dict,
        discussion: list[dict],
        event: AstrMessageEvent,
        now: str,
        version: str,
        existing_meta: dict | None,
        backup_dir: Path | None,
    ) -> list[str]:
        judgment = self._render_boss_judgment(name, distillation)
        management = self._render_boss_management(name, distillation)
        persona = self._render_boss_persona(name, distillation)
        meta = self._render_skill_meta(
            kind="boss",
            name=name,
            slug=slug,
            distillation=distillation,
            event=event,
            now=now,
            version=version,
            existing_meta=existing_meta,
            backup_dir=backup_dir,
        )
        skill = self._render_boss_skill(name, slug, judgment, management, persona)
        files = {
            "judgment.md": judgment,
            "management.md": management,
            "persona.md": persona,
            "meta.json": json.dumps(meta, ensure_ascii=False, indent=2),
            "judgment_skill.md": self._render_section_skill(name, "judgment", judgment),
            "management_skill.md": self._render_section_skill(
                name,
                "management",
                management,
            ),
            "persona_skill.md": self._render_section_skill(name, "persona", persona),
            "SKILL.md": skill,
            "knowledge/messages/session_excerpt.md": self._render_discussion_excerpt(
                discussion
            ),
        }
        for path, content in files.items():
            (root / path).write_text(content.rstrip() + "\n", encoding="utf-8")
        return list(files)

    def _write_colleague_skill_bundle(
        self,
        *,
        root: Path,
        name: str,
        slug: str,
        distillation: dict,
        discussion: list[dict],
        event: AstrMessageEvent,
        now: str,
        version: str,
        existing_meta: dict | None,
        backup_dir: Path | None,
    ) -> list[str]:
        work = self._render_colleague_work(name, distillation)
        persona = self._render_colleague_persona(name, distillation)
        meta = self._render_skill_meta(
            kind="colleague",
            name=name,
            slug=slug,
            distillation=distillation,
            event=event,
            now=now,
            version=version,
            existing_meta=existing_meta,
            backup_dir=backup_dir,
        )
        skill = self._render_colleague_skill(name, slug, work, persona)
        files = {
            "work.md": work,
            "persona.md": persona,
            "meta.json": json.dumps(meta, ensure_ascii=False, indent=2),
            "SKILL.md": skill,
            "knowledge/messages/session_excerpt.md": self._render_discussion_excerpt(
                discussion
            ),
        }
        for path, content in files.items():
            (root / path).write_text(content.rstrip() + "\n", encoding="utf-8")
        return list(files)

    def _render_skill_meta(
        self,
        *,
        kind: str,
        name: str,
        slug: str,
        distillation: dict,
        event: AstrMessageEvent,
        now: str,
        version: str,
        existing_meta: dict | None = None,
        backup_dir: Path | None = None,
    ) -> dict:
        return {
            "name": name,
            "slug": slug,
            "kind": kind,
            "created_at": (existing_meta or {}).get("created_at") or now,
            "updated_at": now,
            "version": version,
            "previous_version_backup": str(backup_dir) if backup_dir else "",
            "source": "dc-agent conversation distillation",
            "conversation_type": distillation.get("conversation_type") or "",
            "platform_id": event.get_platform_id(),
            "session_id": event.unified_msg_origin,
            "message_count": distillation.get("discussion_count", 0),
            "speaker_count": distillation.get("speaker_count", 0),
            "knowledge_sources": ["knowledge/messages/session_excerpt.md"],
            "corrections_count": int(
                (existing_meta or {}).get("corrections_count") or 0
            ),
        }

    def _load_skill_meta(self, root: Path) -> dict | None:
        meta_path = root / "meta.json"
        if not meta_path.exists():
            return None
        try:
            raw = meta_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[HermesBridge] skill meta 读取失败 %s: %s", meta_path, exc)
            return None
        return data if isinstance(data, dict) else None

    def _next_skill_version(self, existing_meta: dict | None) -> str:
        if not existing_meta:
            return "v1"
        raw_version = str(existing_meta.get("version") or "v1").lstrip("vV")
        try:
            number = int(raw_version)
        except ValueError:
            number = 1
        return f"v{number + 1}"

    def _backup_existing_skill_bundle(
        self,
        root: Path,
        existing_meta: dict | None,
        now: str,
    ) -> Path | None:
        if not existing_meta or not (root / "SKILL.md").exists():
            return None
        safe_ts = now.replace(":", "").replace("+", "Z")
        version = str(existing_meta.get("version") or "v1")
        backup_dir = root / "versions" / f"{version}_{safe_ts}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for relative in (
            "SKILL.md",
            "meta.json",
            "judgment.md",
            "management.md",
            "judgment_skill.md",
            "management_skill.md",
            "persona_skill.md",
            "work.md",
            "persona.md",
            "knowledge/messages/session_excerpt.md",
            "knowledge/corrections.md",
        ):
            src = root / relative
            if not src.exists():
                continue
            dst = backup_dir / relative
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        return backup_dir

    def _list_skill_bundles(self, kind: str) -> list[dict]:
        root = self._skill_bundle_root(kind)
        if not root.exists():
            return []
        bundles: list[dict] = []
        for child in sorted(path for path in root.iterdir() if path.is_dir()):
            meta = self._load_skill_meta(child) or {}
            bundles.append(
                {
                    "name": meta.get("name") or child.name,
                    "slug": meta.get("slug") or child.name,
                    "version": meta.get("version") or "unknown",
                    "rollback_version": self._latest_skill_backup_version(
                        kind, child.name
                    ),
                    "updated_at": meta.get("updated_at") or "unknown",
                    "corrections_count": meta.get("corrections_count", 0),
                    "path": str(child),
                }
            )
        return bundles

    def _list_deleted_skill_bundles(self, kind: str) -> list[dict]:
        deleted_root = self._deleted_skill_bundle_root(kind)
        if not deleted_root.exists():
            return []
        prefix = f"{kind}_"
        bundles: list[dict] = []
        for child in sorted(path for path in deleted_root.iterdir() if path.is_dir()):
            if not child.name.startswith(prefix):
                continue
            meta = self._load_skill_meta(child) or {}
            slug, deleted_at = self._parse_deleted_skill_dir(kind, child)
            bundles.append(
                {
                    "name": meta.get("name") or slug,
                    "slug": meta.get("slug") or slug,
                    "version": meta.get("version") or "unknown",
                    "deleted_at": deleted_at,
                    "path": str(child),
                }
            )
        return bundles

    def _skill_bundle_detail(self, kind: str, slug: str) -> dict | None:
        root = self._skill_bundle_root(kind) / slug
        if not root.exists():
            return None
        meta = self._load_skill_meta(root) or {}
        visible_files = sorted(
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_file() and "versions" not in path.relative_to(root).parts
        )
        return {
            "name": meta.get("name") or slug,
            "slug": meta.get("slug") or slug,
            "kind": kind,
            "version": meta.get("version") or "unknown",
            "rollback_version": self._latest_skill_backup_version(kind, slug),
            "conversation_type": meta.get("conversation_type") or "unknown",
            "message_count": meta.get("message_count", 0),
            "speaker_count": meta.get("speaker_count", 0),
            "corrections_count": meta.get("corrections_count", 0),
            "updated_at": meta.get("updated_at") or "unknown",
            "knowledge_sources": list(meta.get("knowledge_sources") or []),
            "path": str(root),
            "files": visible_files,
        }

    def _inspect_skill_bundle(self, kind: str, slug: str) -> tuple[bool, str]:
        detail = self._skill_bundle_detail(kind, slug)
        if detail is None:
            return False, f"没有找到 skill：{slug}"
        title = "老板 skill" if kind == "boss" else "同事 skill"
        lines = [
            f"【{title}】{detail['name']}",
            f"- slug: {detail['slug']}",
            f"- version: {detail['version']}",
            f"- conversation_type: {detail['conversation_type']}",
            f"- message_count: {detail['message_count']}",
            f"- speaker_count: {detail['speaker_count']}",
            f"- corrections_count: {detail['corrections_count']}",
            f"- updated_at: {detail['updated_at']}",
            f"- path: {detail['path']}",
            "- files:",
        ]
        lines.extend(f"  - {file_path}" for file_path in detail["files"][:30])
        return True, "\n".join(lines)

    def _skill_bundle_review(self, kind: str, slug: str) -> dict | None:
        root = self._skill_bundle_root(kind) / slug
        if not root.exists():
            return None
        meta = self._load_skill_meta(root) or {}
        required_files = (
            ("SKILL.md", "judgment.md", "management.md", "persona.md")
            if kind == "boss"
            else ("SKILL.md", "work.md", "persona.md")
        )
        missing = [
            file_path for file_path in required_files if not (root / file_path).exists()
        ]
        text_parts = []
        for relative in required_files + (
            "knowledge/messages/session_excerpt.md",
            "knowledge/corrections.md",
        ):
            path = root / relative
            if path.exists():
                text_parts.append(path.read_text(encoding="utf-8", errors="ignore"))
        combined = "\n".join(text_parts)
        risks: list[str] = []
        passes: list[str] = []
        if missing:
            risks.append(f"缺少必要文件：{', '.join(missing)}")
        else:
            passes.append("必要文件完整")
        if int(meta.get("message_count") or 0) < 2:
            risks.append("生成样本偏少，建议继续积累对话后再升级版本")
        else:
            passes.append("样本数量可用于初步画像")
        if self._contains_any(combined, ("暂无稳定证据", "待确认", "unknown")):
            risks.append("存在待确认或证据不足内容，使用时需要标注来源")
        else:
            passes.append("未发现明显占位或未知字段")
        if self._contains_any(
            combined,
            ("私聊", "先别发群里", "只跟你说", "保密", "不方便公开"),
        ):
            risks.append("包含私聊/保密信号，引用到群聊前需要二次确认")
        else:
            passes.append("未发现明显私聊泄露信号")
        if kind == "boss":
            if self._contains_any(combined, ("判断标准", "风险", "下一步")):
                passes.append("老板 skill 已包含判断/风险/行动结构")
            else:
                risks.append("老板 skill 缺少判断标准、风险或下一步动作")
        elif self._contains_any(combined, ("工作能力", "协作", "私聊边界")):
            passes.append("同事 skill 已包含工作/协作边界结构")
        else:
            risks.append("同事 skill 缺少工作能力、协作方式或边界规则")
        score = max(0, 100 - len(risks) * 20)
        return {
            "name": meta.get("name") or slug,
            "slug": meta.get("slug") or slug,
            "kind": kind,
            "version": meta.get("version") or "unknown",
            "rollback_version": self._latest_skill_backup_version(kind, slug),
            "score": score,
            "corrections_count": meta.get("corrections_count", 0),
            "passes": passes,
            "risks": risks,
        }

    def _review_skill_bundle(self, kind: str, slug: str) -> tuple[bool, str]:
        review = self._skill_bundle_review(kind, slug)
        if review is None:
            return False, f"没有找到 skill：{slug}"
        title = "老板 skill 质量审阅" if kind == "boss" else "同事 skill 质量审阅"
        lines = [
            f"【{title}】{review['name']}",
            f"- slug: {review['slug']}",
            f"- version: {review['version']}",
            f"- score: {review['score']}/100",
            f"- corrections_count: {review['corrections_count']}",
            "",
            "通过项：",
            *(f"- {item}" for item in review["passes"][:8]),
            "",
            "风险项：",
            *(f"- {item}" for item in review["risks"][:8]),
        ]
        if not review["risks"]:
            lines.append("- 暂无明显风险")
        return True, "\n".join(lines)

    def _latest_skill_backup_version(self, kind: str, slug: str) -> str:
        candidates = self._sorted_skill_backup_dirs(kind, slug)
        if not candidates:
            return ""
        latest = candidates[-1].name
        if "_" not in latest:
            return latest
        version, _timestamp = latest.rsplit("_", 1)
        return version

    def _sorted_skill_backup_dirs(self, kind: str, slug: str) -> list[Path]:
        versions_dir = self._skill_bundle_root(kind) / slug / "versions"
        if not versions_dir.exists():
            return []
        candidates = [item for item in versions_dir.iterdir() if item.is_dir()]
        return sorted(candidates, key=self._skill_backup_sort_key)

    def _skill_backup_sort_key(self, path: Path) -> tuple[str, tuple[int, ...], str]:
        raw = path.name
        version_raw = raw
        timestamp = ""
        if "_" in raw:
            version_raw, timestamp = raw.rsplit("_", 1)
        version_numbers = tuple(
            int(part) for part in _re_card.findall(r"\d+", version_raw)
        )
        if not timestamp:
            try:
                timestamp = datetime.fromtimestamp(
                    path.stat().st_mtime,
                    timezone.utc,
                ).isoformat()
            except OSError:
                timestamp = ""
        return (timestamp, version_numbers, raw)

    def _append_skill_correction(
        self,
        kind: str,
        slug: str,
        correction: str,
        event: AstrMessageEvent,
    ) -> tuple[bool, str]:
        root = self._skill_bundle_root(kind) / slug
        if not root.exists():
            return False, f"没有找到 skill：{slug}"
        meta = self._load_skill_meta(root) or {}
        now = datetime.now(timezone.utc).isoformat()
        self._backup_existing_skill_bundle(root, meta or {"version": "current"}, now)
        corrections_path = root / "knowledge" / "corrections.md"
        corrections_path.parent.mkdir(parents=True, exist_ok=True)
        existing = (
            corrections_path.read_text(encoding="utf-8")
            if corrections_path.exists()
            else "# Corrections\n\n"
        )
        sender = event.get_sender_name() or event.get_sender_id() or "unknown"
        entry = "\n".join(
            [
                f"## {now}",
                f"- operator: {sender}",
                f"- source_session: {event.unified_msg_origin}",
                f"- correction: {correction.strip()}",
                "",
            ]
        )
        corrections_path.write_text(
            existing.rstrip() + "\n\n" + entry, encoding="utf-8"
        )
        knowledge_sources = list(meta.get("knowledge_sources") or [])
        if "knowledge/corrections.md" not in knowledge_sources:
            knowledge_sources.append("knowledge/corrections.md")
        meta.update(
            {
                "updated_at": now,
                "corrections_count": int(meta.get("corrections_count") or 0) + 1,
                "knowledge_sources": knowledge_sources,
                "last_correction_at": now,
                "last_correction_by": str(event.get_sender_id() or ""),
            }
        )
        (root / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2).rstrip() + "\n",
            encoding="utf-8",
        )
        return True, (
            f"✅ 已记录 skill 纠错：{slug}\n"
            f"- corrections_count: {meta['corrections_count']}\n"
            f"- 文件：{corrections_path}"
        )

    def _rollback_skill_bundle(
        self,
        kind: str,
        slug: str,
        version: str,
    ) -> tuple[bool, str]:
        root = self._skill_bundle_root(kind) / slug
        if not root.exists():
            return False, f"没有找到 skill：{slug}"
        versions_dir = root / "versions"
        if not versions_dir.exists():
            return False, f"{slug} 没有可回滚版本。"
        candidates = self._sorted_skill_backup_dirs(kind, slug)
        candidates = [
            item
            for item in candidates
            if item.name == version or item.name.startswith(f"{version}_")
        ]
        if not candidates:
            return False, f"没有找到 {slug} 的版本 {version}。"
        backup_dir = candidates[-1]
        current_meta = self._load_skill_meta(root)
        self._backup_existing_skill_bundle(
            root,
            current_meta or {"version": "current"},
            datetime.now(timezone.utc).isoformat(),
        )
        for src in backup_dir.rglob("*"):
            if not src.is_file():
                continue
            relative = src.relative_to(backup_dir)
            dst = root / relative
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        return True, f"✅ 已回滚 {slug} 到 {version}：{backup_dir}"

    def _soft_delete_skill_bundle(self, kind: str, slug: str) -> tuple[bool, str]:
        root = self._skill_bundle_root(kind) / slug
        if not root.exists():
            return False, f"没有找到 skill：{slug}"
        if root.parent != self._skill_bundle_root(kind):
            return False, "拒绝删除：路径不在受控 skill 目录下。"
        deleted_root = self._skill_bundle_root(kind).parent / ".deleted"
        deleted_root.mkdir(parents=True, exist_ok=True)
        safe_ts = (
            datetime.now(timezone.utc)
            .isoformat()
            .replace(":", "")
            .replace(
                "+",
                "Z",
            )
        )
        deleted_path = deleted_root / f"{kind}_{slug}_{safe_ts}"
        shutil.move(str(root), str(deleted_path))
        return True, f"✅ 已软删除 skill：{slug}\n- 备份目录：{deleted_path}"

    def _restore_deleted_skill_bundle(self, kind: str, slug: str) -> tuple[bool, str]:
        target = self._skill_bundle_root(kind) / slug
        if target.exists():
            return False, f"恢复失败：当前已存在同名 skill：{slug}"
        deleted_root = self._deleted_skill_bundle_root(kind)
        if not deleted_root.exists():
            return False, f"没有找到已删除 skill：{slug}"
        candidates = [
            item
            for item in deleted_root.iterdir()
            if item.is_dir() and self._parse_deleted_skill_dir(kind, item)[0] == slug
        ]
        if not candidates:
            return False, f"没有找到已删除 skill：{slug}"
        deleted_path = sorted(candidates)[-1]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(deleted_path), str(target))
        return True, f"✅ 已恢复 skill：{slug}\n- 目录：{target}"

    def _deleted_skill_bundle_root(self, kind: str) -> Path:
        return self._skill_bundle_root(kind).parent / ".deleted"

    def _parse_deleted_skill_dir(self, kind: str, path: Path) -> tuple[str, str]:
        prefix = f"{kind}_"
        raw = path.name[len(prefix) :] if path.name.startswith(prefix) else path.name
        if "_" not in raw:
            return raw, "unknown"
        slug, deleted_at = raw.rsplit("_", 1)
        return slug, deleted_at

    def _skill_bundle_root(self, kind: str) -> Path:
        base_dir = Path(
            getattr(self, "skill_bundle_base_dir", Path(__file__).resolve().parents[3])
        )
        return base_dir / ("bosses" if kind == "boss" else "colleagues")

    def _render_boss_judgment(self, name: str, distillation: dict) -> str:
        return "\n".join(
            [
                f"# {name} Judgment",
                "",
                "## 判断标准",
                *self._markdown_items(distillation, "boss_success_criteria"),
                "",
                "## 约束与否决因素",
                *self._markdown_items(distillation, "department_constraints"),
                "",
                "## 需要深挖的信号",
                *self._markdown_items(distillation, "dissatisfaction_signals"),
                "",
                "## 使用规则",
                "- 回答前先判断：这件事是否能让老板快速做决策。",
                "- 优先输出结论、判断依据、风险和下一步动作。",
                "- 没有事实来源时必须标注待确认。",
            ]
        )

    def _render_boss_management(self, name: str, distillation: dict) -> str:
        return "\n".join(
            [
                f"# Managing {name}",
                "",
                "## 向上管理要点",
                *self._markdown_items(distillation, "boss_success_criteria"),
                "",
                "## 汇报结构",
                "- 先给结论。",
                "- 再给老板需要拍板的选项。",
                "- 标清预算、周期、负责人、风险和兜底方案。",
                "",
                "## 不要这样做",
                "- 不要用大量背景铺垫替代结论。",
                "- 不要把未确认事实包装成已发生。",
                "- 不要把私聊内容直接转成公开结论，除非得到确认。",
            ]
        )

    def _render_boss_persona(self, name: str, distillation: dict) -> str:
        return "\n".join(
            [
                f"# {name} Persona",
                "",
                "## 表达与沟通偏好",
                *self._markdown_items(distillation, "bot_role_hints"),
                "",
                "## 私聊边界",
                *self._markdown_items(distillation, "private_concerns"),
                "",
                "## 运行规则",
                "- 始终保持尊重、简洁、稳重。",
                "- 对老板相关内容结论先行，并明确判断标准。",
                "- 私聊诉求只作为内部理解依据，不自动公开传播。",
            ]
        )

    def _render_colleague_work(self, name: str, distillation: dict) -> str:
        return "\n".join(
            [
                f"# {name} Work Skill",
                "",
                "## 工作上下文与约束",
                *self._markdown_items(distillation, "department_constraints"),
                "",
                "## 执行阻力与常见问题",
                *self._markdown_items(distillation, "employee_questions"),
                "",
                "## 可沉淀知识",
                *self._markdown_items(distillation, "knowledge_candidates"),
                "",
                "## 执行规则",
                "- 输出要落到步骤、负责人、时间点和验收口径。",
                "- 遇到缺资料时列出补充清单。",
                "- 不把个人偏好当作公司标准。",
            ]
        )

    def _render_colleague_persona(self, name: str, distillation: dict) -> str:
        return "\n".join(
            [
                f"# {name} Persona",
                "",
                "## 沟通偏好",
                *self._markdown_items(distillation, "private_concerns"),
                "",
                "## 角色修正",
                *self._markdown_items(distillation, "bot_role_hints"),
                "",
                "## 运行规则",
                "- 先判断对方是在求助、补充约束还是表达阻力。",
                "- 对员工表达多给鼓励和减压。",
                "- 涉及私聊内容时默认不外传。",
            ]
        )

    def _render_boss_skill(
        self,
        name: str,
        slug: str,
        judgment: str,
        management: str,
        persona: str,
    ) -> str:
        return "\n".join(
            [
                "---",
                f"name: boss-{slug}",
                f"description: {name} 的老板判断、向上管理和沟通画像。",
                "user-invocable: true",
                "---",
                f"# {name}",
                "",
                "## PART A: Judgment",
                judgment,
                "",
                "## PART B: Management",
                management,
                "",
                "## PART C: Persona",
                persona,
                "",
                "## 运行规则",
                "1. 先用 Judgment 判断方案是否能过。",
                "2. 再用 Management 组织汇报和推进方式。",
                "3. 最后用 Persona 控制语气和边界。",
            ]
        )

    def _render_colleague_skill(
        self,
        name: str,
        slug: str,
        work: str,
        persona: str,
    ) -> str:
        return "\n".join(
            [
                "---",
                f"name: colleague-{slug}",
                f"description: {name} 的工作能力与协作画像。",
                "user-invocable: true",
                "---",
                f"# {name}",
                "",
                "## PART A: 工作能力",
                work,
                "",
                "## PART B: 人物性格",
                persona,
                "",
                "## 运行规则",
                "1. 先由 PART B 判断：用什么态度接这个任务。",
                "2. 再由 PART A 执行：用工作能力完成任务。",
                "3. 输出时始终保持 PART B 的表达风格。",
                "4. 私聊边界和真实性护栏优先级最高。",
            ]
        )

    def _render_section_skill(self, name: str, section: str, content: str) -> str:
        return "\n".join(
            [
                "---",
                f"name: {self._slugify_skill_name(name)}-{section}",
                f"description: {name} {section} section",
                "user-invocable: true",
                "---",
                content,
            ]
        )

    def _render_discussion_excerpt(self, discussion: list[dict]) -> str:
        lines = ["# Session Excerpt", ""]
        for item in discussion[-self.topic_discussion_limit :]:
            sender = item.get("sender_name") or item.get("sender_id") or "unknown"
            message = str(item.get("message") or "").strip()
            if message:
                lines.append(f"- **{sender}**: {message}")
        return "\n".join(lines)

    def _markdown_items(self, distillation: dict, key: str) -> list[str]:
        values = distillation.get(key) or []
        if not values:
            return ["- 暂无稳定证据。"]
        return [f"- {value}" for value in values[:6]]

    def _slugify_skill_name(self, name: str) -> str:
        cleaned = _re_card.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", name.strip())
        cleaned = cleaned.strip("-_").lower()
        return cleaned or "unknown"

    async def _topic_close(
        self,
        event: AstrMessageEvent,
        user_id: str,
        summary: str,
    ) -> None:
        engine = self.context.harness_engine
        if engine is None:
            await event.send(MessageChain([Plain("Harness 引擎未初始化。")]))
            return
        task = await self._get_active_topic_task(event)
        if task is None:
            await event.send(MessageChain([Plain("当前群没有进行中的灰度话题。")]))
            return
        discussion = await self._topic_discussion_snapshot(task.task_id)
        distillation = self._distill_grey_topic(
            task,
            discussion,
            final_summary=summary.strip(),
        )
        result = {
            "summary": summary.strip() or "灰度话题已由管理员关闭。",
            "source": "grey_topic_review",
            "closed_by": user_id,
            "discussion_count": len(discussion),
            "discussion": discussion,
            "distillation": distillation,
        }
        await self._append_topic_distillation(task.task_id, distillation)
        await engine.complete_task(task.task_id, result=result)
        self._topic_discussion_cache.pop(task.task_id, None)
        await event.send(
            MessageChain(
                [
                    Plain(
                        f"灰度话题已关闭：{task.task_id[:8]}\n"
                        "讨论、结果和蒸馏摘要已写入 Harness 任务记录。\n\n"
                        f"{self._format_distillation(distillation)}"
                    )
                ]
            )
        )

    async def _record_topic_discussion(
        self,
        event: AstrMessageEvent,
        user_id: str,
        message_text: str,
    ) -> None:
        task = await self._get_active_topic_task(event)
        if task is None:
            return

        item = {
            "sender_id": user_id,
            "sender_name": event.get_sender_name() or user_id,
            "message": message_text.strip()[:1200],
            "platform_id": event.get_platform_id(),
            "session_id": event.unified_msg_origin,
        }
        self._topic_discussion_cache[task.task_id].append(item)
        try:
            await self.context.harness_engine.append_trace(
                task.task_id,
                "topic_discussion_message",
                item,
            )
        except Exception as exc:
            logger.debug("[HermesBridge] 记录灰度话题讨论失败：%s", exc)

    async def _record_conversation_discussion(
        self,
        event: AstrMessageEvent,
        user_id: str,
        message_text: str,
    ) -> None:
        text = message_text.strip()
        if not text or text.startswith(
            ("/topic", "/话题", "/chat", "/私聊", "/deep", "/深挖")
        ):
            return
        item = {
            "sender_id": user_id,
            "sender_name": event.get_sender_name() or user_id,
            "message": text[:1200],
            "platform_id": event.get_platform_id(),
            "session_id": event.unified_msg_origin,
            "conversation_type": "group" if event.is_group() else "private",
        }
        self._conversation_discussion_cache[self._conversation_cache_key(event)].append(
            item
        )

    async def _get_active_topic_task(self, event: AstrMessageEvent):
        store = getattr(self.context, "harness_store", None)
        if store is None:
            engine = getattr(self.context, "harness_engine", None)
            store = getattr(engine, "store", None)
        if store is None:
            return None

        statuses = tuple(
            status
            for status in (
                "pending",
                "in_progress",
                "blocked",
                "review_required",
            )
            if status not in HARNESS_TERMINAL_STATUSES
        )
        tasks = await store.list_tasks_for_session(
            event.unified_msg_origin,
            limit=10,
            statuses=statuses,
        )
        for task in tasks:
            if task.payload.get("source") == "grey_topic":
                return task
        return None

    async def _topic_discussion_snapshot(self, task_id: str) -> list[dict]:
        cached = list(self._topic_discussion_cache.get(task_id, []))
        if cached:
            return cached[-self.topic_discussion_limit :]

        store = getattr(self.context, "harness_store", None)
        if store is None:
            engine = getattr(self.context, "harness_engine", None)
            store = getattr(engine, "store", None)
        if store is None:
            return []
        try:
            events = await store.list_events(task_id)
        except Exception:
            return []
        discussion = [
            event.payload
            for event in events
            if event.event_type == "topic_discussion_message"
        ]
        return discussion[-self.topic_discussion_limit :]

    def _conversation_cache_key(self, event: AstrMessageEvent) -> str:
        return str(event.unified_msg_origin or "")

    def _conversation_discussion_snapshot(
        self,
        event: AstrMessageEvent,
    ) -> list[dict]:
        cached = list(
            self._conversation_discussion_cache.get(
                self._conversation_cache_key(event),
                [],
            )
        )
        return cached[-self.topic_discussion_limit :]

    async def _append_topic_distillation(
        self,
        task_id: str,
        distillation: dict,
    ) -> None:
        if not self.topic_distill_enabled:
            return
        engine = getattr(self.context, "harness_engine", None)
        if engine is None:
            return
        try:
            await engine.append_trace(
                task_id,
                "topic_distillation_snapshot",
                distillation,
            )
        except Exception as exc:
            logger.debug("[HermesBridge] 记录灰度蒸馏摘要失败：%s", exc)

    async def _append_topic_event(
        self,
        task_id: str,
        event_type: str,
        payload: dict,
    ) -> None:
        engine = getattr(self.context, "harness_engine", None)
        if engine is None:
            return
        try:
            await engine.append_trace(task_id, event_type, payload)
        except Exception as exc:
            logger.debug("[HermesBridge] 记录灰度事件失败 %s：%s", event_type, exc)

    def _distill_grey_topic(
        self,
        task,
        discussion: list[dict],
        *,
        trigger_reason: str = "",
        final_summary: str = "",
        source_scope: str = "grey_topic",
        conversation_type: str = "",
    ) -> dict:
        if not self.topic_distill_enabled:
            return {}

        buckets = {
            "boss_success_criteria": [],
            "department_constraints": [],
            "employee_questions": [],
            "private_concerns": [],
            "dissatisfaction_signals": [],
            "knowledge_candidates": [],
            "bot_role_hints": [],
        }
        role_counts: dict[str, int] = {}

        for item in discussion:
            message = str(item.get("message") or "").strip()
            if not message:
                continue
            sender_name = str(
                item.get("sender_name") or item.get("sender_id") or "unknown"
            )
            item_conversation_type = str(
                item.get("conversation_type") or conversation_type or ""
            )
            role_counts[sender_name] = role_counts.get(sender_name, 0) + 1
            compact = " ".join(message.split())[:220]
            lowered = compact.lower()

            if self._contains_any(
                compact,
                (
                    "目标",
                    "满意",
                    "不满意",
                    "标准",
                    "结果",
                    "老板",
                    "老总",
                    "判断",
                    "要的是",
                ),
            ):
                buckets["boss_success_criteria"].append(f"{sender_name}: {compact}")
            if self._contains_any(
                compact,
                (
                    "预算",
                    "资源",
                    "人手",
                    "时间",
                    "周期",
                    "风险",
                    "合规",
                    "审批",
                    "部门",
                    "负责人",
                    "限制",
                ),
            ):
                buckets["department_constraints"].append(f"{sender_name}: {compact}")
            if (
                "?" in compact
                or "？" in compact
                or self._contains_any(
                    compact,
                    (
                        "为什么",
                        "怎么",
                        "如何",
                        "是否",
                        "能不能",
                        "是不是",
                        "哪里",
                        "谁来",
                        "多久",
                    ),
                )
            ):
                buckets["employee_questions"].append(f"{sender_name}: {compact}")
            if item_conversation_type == "private" or self._contains_any(
                compact,
                (
                    "私聊",
                    "单独",
                    "个人",
                    "我这边",
                    "我担心",
                    "我希望",
                    "不方便",
                    "先别发群里",
                    "只跟你说",
                    "保密",
                ),
            ):
                buckets["private_concerns"].append(f"{sender_name}: {compact}")
            if self._contains_any(
                compact,
                (
                    "不满意",
                    "不够",
                    "不行",
                    "不对",
                    "不落地",
                    "太空",
                    "看不懂",
                    "没解决",
                    "继续深挖",
                    "再查",
                ),
            ):
                buckets["dissatisfaction_signals"].append(f"{sender_name}: {compact}")
            if self._contains_any(
                compact,
                (
                    "流程",
                    "规则",
                    "标准",
                    "资料",
                    "文档",
                    "话术",
                    "案例",
                    "客户",
                    "产品",
                    "知识库",
                ),
            ):
                buckets["knowledge_candidates"].append(f"{sender_name}: {compact}")
            if self._contains_any(
                compact,
                (
                    "机器人",
                    "bot",
                    "回复",
                    "语气",
                    "格式",
                    "别",
                    "不要",
                    "需要先",
                    "应该",
                ),
            ):
                buckets["bot_role_hints"].append(f"{sender_name}: {compact}")

            if "老板" in compact or "老总" in compact:
                buckets["bot_role_hints"].append(
                    f"{sender_name}: 老板相关内容需要结论先行，并明确判断标准。"
                )
            if "员工" in compact:
                buckets["bot_role_hints"].append(
                    f"{sender_name}: 员工相关内容需要给出可执行步骤，减少抽象表述。"
                )
            if "hermes" in lowered or "深挖" in compact:
                buckets["bot_role_hints"].append(
                    f"{sender_name}: 深挖请求应带上话题、讨论分歧和不满意原因。"
                )

        for key, values in buckets.items():
            buckets[key] = self._dedupe_keep_order(values, limit=6)

        return {
            "topic_id": task.task_id,
            "title": getattr(task, "title", ""),
            "trigger_reason": trigger_reason,
            "final_summary": final_summary,
            "source_scope": source_scope,
            "conversation_type": conversation_type,
            "discussion_count": len(discussion),
            "speaker_count": len(role_counts),
            "speaker_activity": dict(
                sorted(role_counts.items(), key=lambda item: item[1], reverse=True)[:8]
            ),
            **buckets,
            "knowledge_policy": (
                "仅将已确认的业务事实、流程、标准话术和产品资料沉淀进正式知识库；"
                "正常群聊和私聊中的偏好、疑问和不满意信号先作为会话画像与路由规则使用。"
            ),
        }

    def _format_distillation(self, distillation: dict) -> str:
        if not distillation:
            return "【会话蒸馏摘要】当前未启用对话蒸馏。"

        def render_items(title: str, key: str) -> list[str]:
            values = distillation.get(key) or []
            if not values:
                return [f"{title}：暂无明确样本"]
            return [f"{title}："] + [f"- {value}" for value in values[:4]]

        heading = (
            "【灰度话题蒸馏摘要】"
            if distillation.get("source_scope", "grey_topic") == "grey_topic"
            else "【会话蒸馏摘要】"
        )
        lines = [
            heading,
            f"话题：{distillation.get('title') or distillation.get('topic_id', '')}",
            f"讨论消息：{distillation.get('discussion_count', 0)} 条；参与者：{distillation.get('speaker_count', 0)} 人",
            "",
            *render_items("老板/发起人判断标准", "boss_success_criteria"),
            "",
            *render_items("部门负责人约束", "department_constraints"),
            "",
            *render_items("员工疑问/阻力", "employee_questions"),
            "",
            *render_items("私聊关注/个人诉求", "private_concerns"),
            "",
            *render_items("不满意/需深挖信号", "dissatisfaction_signals"),
            "",
            *render_items("知识库候选", "knowledge_candidates"),
            "",
            *render_items("推广 1 号角色修正", "bot_role_hints"),
            "",
            f"知识库策略：{distillation.get('knowledge_policy', '')}",
        ]
        return "\n".join(lines).strip()

    def _contains_any(self, text: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword in text for keyword in keywords)

    def _dedupe_keep_order(self, values: list[str], *, limit: int) -> list[str]:
        seen = set()
        result = []
        for value in values:
            normalized = value.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
            if len(result) >= limit:
                break
        return result

    async def _get_or_create_current_conversation_id(
        self,
        event: AstrMessageEvent,
    ) -> str:
        conv_mgr = self.context.conversation_manager
        umo = event.unified_msg_origin
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if cid:
            return cid
        return await conv_mgr.new_conversation(umo, event.get_platform_id())

    def _get_or_create_session(self, user_id: str, platform: PlatformType) -> str:
        pu = PlatformUser(platform=platform, user_id=user_id)
        return self.session_router.get_or_create_session(pu)

    async def shutdown(self):
        logger.info("[HermesBridge] 插件已关闭")
