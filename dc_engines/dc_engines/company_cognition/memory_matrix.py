from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

MEMORY_STORAGE_WHITELIST: dict[str, str] = {
    "user": "用户画像、技术水平、协作习惯",
    "feedback": "用户纠正与正向确认",
    "project": "无法从代码库直接推导的宏观目标、交付日期",
    "reference": "外部系统关联指针",
}

MEMORY_STORAGE_BLACKLIST: tuple[str, ...] = (
    "具体的代码实现细节",
    "当前的架构拓扑分析",
    "具体文件的绝对路径",
    "Git 历史提交日志",
    "临时的调试编译输出",
)

CREDENTIAL_PATTERN = re.compile(
    r"(ghp_[a-zA-Z0-9]{36}|AIzaSy[a-zA-Z0-9-_]{35}|AWS_ACCESS_KEY_ID)"
)
ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?<![\w./-])(?:/(?:Users|private|var|tmp|etc|home|opt)/[^\s`'\"，。；、]+|[A-Za-z]:\\[^\s`'\"，。；、]+)"
)
RELATIVE_TIME_PATTERN = re.compile(
    r"(今天|明天|后天|昨天|前天|本周|下周|上周|本月|下月|上月|月底|周[一二三四五六日天])"
)

RULE_FILE_NAMES = ("AGENTS.md", "CLAUDE.md")
SKIP_SCAN_DIRS = {
    ".git",
    ".claude",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}


class MemoryGuardrailViolation(RuntimeError):
    """Raised when a memory write candidate violates persistence guardrails."""


@dataclass(slots=True)
class MemoryGuardrailResult:
    accepted: bool
    violations: list[str] = field(default_factory=list)
    category: str = ""
    target_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "violations": self.violations,
            "category": self.category,
            "target_path": self.target_path,
        }


@dataclass(slots=True)
class MemoryWriteCandidate:
    category: str
    content: str
    target_path: str | Path | None = None
    absolute_dates: list[str] = field(default_factory=list)


class MemorySafetyGuardrails:
    """Shared L4 write filter and path boundary validator."""

    def __init__(self, memory_root: str | Path) -> None:
        self.memory_root = Path(memory_root).expanduser()

    def evaluate(self, candidate: MemoryWriteCandidate) -> MemoryGuardrailResult:
        violations: list[str] = []
        category = candidate.category.strip().lower()
        if category not in MEMORY_STORAGE_WHITELIST:
            violations.append("category_not_whitelisted")
        if category in MEMORY_STORAGE_BLACKLIST:
            violations.append("category_blacklisted")

        content = candidate.content or ""
        if CREDENTIAL_PATTERN.search(content):
            violations.append("credential_leak")
        if ABSOLUTE_PATH_PATTERN.search(content):
            violations.append("absolute_path_detail")
        if RELATIVE_TIME_PATTERN.search(content) and not candidate.absolute_dates:
            violations.append("relative_time_without_absolute_date")

        target_path = ""
        if candidate.target_path is not None:
            path_result = self.validate_write_path(candidate.target_path)
            target_path = path_result.target_path
            violations.extend(path_result.violations)

        return MemoryGuardrailResult(
            accepted=not violations,
            violations=sorted(set(violations)),
            category=category,
            target_path=target_path,
        )

    def assert_safe(self, candidate: MemoryWriteCandidate) -> None:
        result = self.evaluate(candidate)
        if not result.accepted:
            raise MemoryGuardrailViolation(", ".join(result.violations))

    def validate_write_path(self, target_path: str | Path) -> MemoryGuardrailResult:
        target = Path(target_path).expanduser()
        if not target.is_absolute():
            target = self.memory_root / target

        violations: list[str] = []
        root_resolved = self.memory_root.resolve(strict=False)
        target_resolved = target.resolve(strict=False)
        root_real = Path(os.path.realpath(root_resolved))
        target_real = Path(os.path.realpath(target_resolved))

        if not self._is_relative_to(target_resolved, root_resolved):
            violations.append("path_escape_resolved")
        if not self._is_relative_to(target_real, root_real):
            violations.append("path_escape_realpath")
        if self._has_dangling_symlink(target):
            violations.append("dangling_symlink")

        return MemoryGuardrailResult(
            accepted=not violations,
            violations=sorted(set(violations)),
            target_path=str(target_resolved),
        )

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    @staticmethod
    def _has_dangling_symlink(path: Path) -> bool:
        for item in (path, *path.parents):
            if item.is_symlink():
                try:
                    item.resolve(strict=True)
                except OSError:
                    return True
        return False


class MemoryMatrixHealthCheck:
    """Read-only inspection for the six-layer memory matrix."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root)
        self.data_dir = self.project_root / "data"
        self.guardrails = MemorySafetyGuardrails(self.data_dir / "memory")

    async def build_component(self) -> dict[str, Any]:
        layers = {
            "L1": await self._instruction_memory(),
            "L2": await self._short_term_memory(),
            "L3": await self._working_memory(),
            "L4": await self._long_term_memory(),
            "L5": await self._summary_memory(),
            "L6": await self._offline_remodeling(),
        }
        return {
            "available": True,
            "verdict": self._verdict(layers),
            "layers": layers,
            "guardrails": self._guardrail_summary(),
            "spec": {
                "name": "Codex Memory Matrix",
                "layers": ["L1", "L2", "L3", "L4", "L5", "L6"],
                "whitelist": MEMORY_STORAGE_WHITELIST,
                "blacklist": list(MEMORY_STORAGE_BLACKLIST),
            },
        }

    async def _instruction_memory(self) -> dict[str, Any]:
        files = self._find_rule_files()
        system_reminder_hits = self._count_text_hits(
            self.project_root / "astrbot" / "core",
            "system_reminder",
            max_files=20,
        )
        dual_track_signals = {
            "rule_file_count": len(files),
            "rule_files": files[:20],
            "system_reminder_hits": system_reminder_hits,
        }
        gaps: list[str] = []
        if not files:
            gaps.append("缺少 AGENTS.md/CLAUDE.md 指令文件。")
        if system_reminder_hits <= 0:
            gaps.append("未检测到 system_reminder 动态注入信号。")
        return self._layer(
            "L1",
            "Instruction Memory",
            "instruction_memory",
            "disk_markdown",
            status="healthy" if files and system_reminder_hits else "partial",
            signals=dual_track_signals,
            gaps=gaps,
        )

    async def _short_term_memory(self) -> dict[str, Any]:
        path = self.data_dir / "data_v4.db"
        if not path.exists():
            return self._layer(
                "L2",
                "Short-term Memory",
                "conversation",
                "ram_and_sqlite_history",
                status="missing",
                signals={"path": str(path), "available": False},
                gaps=["缺少 data_v4.db，无法审计会话历史。"],
            )
        counts = await self._sqlite_counts(
            path,
            {
                "messages": "platform_message_history",
                "conversations": "conversations",
                "platform_sessions": "platform_sessions",
            },
        )
        message_count = counts.get("messages", 0)
        gaps = []
        if message_count <= 0:
            gaps.append("没有可回放的原始消息记录。")
        return self._layer(
            "L2",
            "Short-term Memory",
            "conversation",
            "ram_and_sqlite_history",
            status="healthy" if message_count > 0 else "partial",
            signals={"path": str(path), **counts},
            gaps=gaps,
        )

    async def _working_memory(self) -> dict[str, Any]:
        harness = await self._sqlite_counts(
            self.data_dir / "harness.db",
            {"harness_tasks": "harness_tasks"},
        )
        cases = await self._sqlite_counts(
            self.data_dir / "cases.db", {"cases": "cases"}
        )
        inbox = await self._sqlite_counts(
            self.data_dir / "ai_inbox.db",
            {"inbox_items": "inbox_items"},
        )
        signals = {
            "harness_tasks": harness.get("harness_tasks", 0),
            "cases": cases.get("cases", 0),
            "inbox_items": inbox.get("inbox_items", 0),
        }
        total = sum(signals.values())
        return self._layer(
            "L3",
            "Working Memory",
            "task_state",
            "ram_and_sqlite_sidecar",
            status="healthy" if total > 0 else "missing",
            signals=signals,
            gaps=[] if total > 0 else ["没有任务级状态载体。"],
        )

    async def _long_term_memory(self) -> dict[str, Any]:
        employee = await self._sqlite_counts(
            self.data_dir / "employees.db",
            {"employee_memories": "employee_memories", "employees": "employees"},
        )
        harness = await self._sqlite_counts(
            self.data_dir / "harness_memory.db",
            {"harness_memories": "harness_memories"},
        )
        kb = await self._kb_counts()
        memory_sources = (
            employee.get("employee_memories", 0)
            + harness.get("harness_memories", 0)
            + kb.get("chunks", 0)
        )
        gaps = [
            "L4 写入过滤器已有合同，但还没有强制接入所有历史写入入口。",
        ]
        if memory_sources <= 0:
            gaps.append("没有可审计的长期记忆内容。")
        return self._layer(
            "L4",
            "Long-term Memory",
            "knowledge_base",
            "sqlite_and_markdown",
            status="partial" if memory_sources > 0 else "missing",
            signals={
                **employee,
                **harness,
                **kb,
                "write_filter_available": True,
                "globally_enforced": False,
            },
            gaps=gaps,
        )

    async def _summary_memory(self) -> dict[str, Any]:
        path = self.data_dir / "lossless_context.db"
        sessionmemory_files = self._find_named_files("sessionmemory.md")
        if not path.exists():
            return self._layer(
                "L5",
                "Summary Memory",
                "session_memory",
                "markdown_or_sqlite",
                status="missing",
                signals={
                    "path": str(path),
                    "sessionmemory_file_count": len(sessionmemory_files),
                },
                gaps=["缺少 lossless_context.db/sessionmemory.md 摘要载体。"],
            )
        counts = await self._sqlite_counts(
            path,
            {
                "lossless_items": "lossless_items",
                "lossless_heads": "lossless_heads",
                "lossless_jobs": "lossless_jobs",
            },
        )
        item_types = await self._group_counts(path, "lossless_items", "item_type")
        tool_boundary_guard = (
            self.project_root / "astrbot/core/agent/context/truncator.py"
        ).exists()
        gaps = []
        if not sessionmemory_files:
            gaps.append("尚未检测到固定 schema 的 sessionmemory.md 滚动笔记。")
        if not tool_boundary_guard:
            gaps.append("未检测到工具调用边界修复器。")
        status = "healthy"
        if not sessionmemory_files:
            status = "partial"
        return self._layer(
            "L5",
            "Summary Memory",
            "session_memory",
            "sqlite_lossless_and_markdown",
            status=status,
            signals={
                "path": str(path),
                **counts,
                "item_types": item_types,
                "sessionmemory_file_count": len(sessionmemory_files),
                "tool_boundary_guard": tool_boundary_guard,
            },
            gaps=gaps,
        )

    async def _offline_remodeling(self) -> dict[str, Any]:
        state_path = self.data_dir / "watchdog" / "knowledge_cycle_state.json"
        log_path = self.data_dir / "watchdog" / "knowledge_cycle.log"
        lock_path = self.data_dir / "watchdog" / "autodream.lock"
        state = self._read_json(state_path)
        steps = state.get("steps") if isinstance(state.get("steps"), dict) else {}
        ok_steps = [
            name
            for name, value in steps.items()
            if isinstance(value, dict) and value.get("status") == "ok"
        ]
        available = state_path.exists() or log_path.exists()
        gaps = []
        if not lock_path.exists():
            gaps.append("未检测到 AutoDream 专用 CAS 锁。")
        gaps.append(
            "当前 knowledge_cycle 是周期知识维护，不等同于多会话记忆熔炼与索引修剪。"
        )
        return self._layer(
            "L6",
            "Offline Remodeling",
            "autodream",
            "watchdog_files",
            status="partial" if available else "missing",
            signals={
                "state_path": str(state_path),
                "log_path": str(log_path),
                "state_available": state_path.exists(),
                "log_available": log_path.exists(),
                "log_size": log_path.stat().st_size if log_path.exists() else 0,
                "last_tick_at": state.get("last_tick_at", ""),
                "ok_steps": ok_steps,
                "autodream_lock": lock_path.exists(),
            },
            gaps=gaps if available else ["缺少离线重塑状态文件。"],
        )

    def _guardrail_summary(self) -> dict[str, Any]:
        return {
            "memory_root": str(self.guardrails.memory_root.resolve(strict=False)),
            "path_escape_guard": True,
            "dangling_symlink_guard": True,
            "credential_sanitizer": CREDENTIAL_PATTERN.pattern,
            "absolute_path_guard": True,
            "relative_time_guard": True,
            "whitelist_categories": list(MEMORY_STORAGE_WHITELIST),
        }

    def _find_rule_files(self) -> list[str]:
        found: list[str] = []
        for name in RULE_FILE_NAMES:
            found.extend(self._find_named_files(name))
        return sorted(set(found))

    def _find_named_files(self, file_name: str) -> list[str]:
        found: list[str] = []
        for path in self.project_root.rglob(file_name):
            if self._should_skip(path):
                continue
            found.append(self._relative(path))
        return sorted(found)

    def _count_text_hits(self, root: Path, pattern: str, *, max_files: int) -> int:
        if not root.exists():
            return 0
        hits = 0
        scanned = 0
        for path in root.rglob("*.py"):
            if scanned >= max_files:
                break
            if self._should_skip(path):
                continue
            scanned += 1
            try:
                if pattern in path.read_text(encoding="utf-8", errors="ignore"):
                    hits += 1
            except OSError:
                continue
        return hits

    async def _kb_counts(self) -> dict[str, int]:
        path = self.data_dir / "knowledge_base" / "kb.db"
        if not path.exists():
            return {"knowledge_bases": 0, "kb_documents": 0, "chunks": 0}
        try:
            async with aiosqlite.connect(path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA busy_timeout=1000")
                knowledge_bases = await self._count(db, "knowledge_bases")
                documents = await self._count(db, "kb_documents")
                if await self._table_exists(db, "kb_documents"):
                    cursor = await db.execute(
                        "SELECT coalesce(sum(chunk_count), 0) FROM kb_documents"
                    )
                    row = await cursor.fetchone()
                    chunks = int(row[0] or 0)
                else:
                    chunks = 0
        except Exception:  # noqa: BLE001
            return {"knowledge_bases": 0, "kb_documents": 0, "chunks": 0}
        return {
            "knowledge_bases": knowledge_bases,
            "kb_documents": documents,
            "chunks": chunks,
        }

    async def _sqlite_counts(
        self,
        path: Path,
        table_by_key: dict[str, str],
    ) -> dict[str, int]:
        counts = dict.fromkeys(table_by_key, 0)
        if not path.exists():
            return counts
        try:
            async with aiosqlite.connect(path) as db:
                await db.execute("PRAGMA busy_timeout=1000")
                for key, table in table_by_key.items():
                    if await self._table_exists(db, table):
                        counts[key] = await self._count(db, table)
        except Exception:  # noqa: BLE001
            return counts
        return counts

    async def _group_counts(
        self, path: Path, table: str, column: str
    ) -> dict[str, int]:
        if not path.exists():
            return {}
        try:
            async with aiosqlite.connect(path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA busy_timeout=1000")
                if not await self._table_exists(db, table):
                    return {}
                cursor = await db.execute(
                    f"""
                    SELECT coalesce(nullif(trim({column}), ''), '(blank)') AS label,
                           count(*) AS count
                    FROM {table}
                    GROUP BY label
                    ORDER BY count DESC
                    """
                )
                rows = await cursor.fetchall()
        except Exception:  # noqa: BLE001
            return {}
        return {str(row["label"]): int(row["count"] or 0) for row in rows}

    async def _table_exists(self, db: aiosqlite.Connection, table: str) -> bool:
        cursor = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        )
        return await cursor.fetchone() is not None

    async def _count(self, db: aiosqlite.Connection, table: str) -> int:
        cursor = await db.execute(f"SELECT count(*) FROM {table}")
        row = await cursor.fetchone()
        return int(row[0] or 0)

    def _layer(
        self,
        layer_id: str,
        name: str,
        module: str,
        storage: str,
        *,
        status: str,
        signals: dict[str, Any],
        gaps: list[str],
    ) -> dict[str, Any]:
        return {
            "id": layer_id,
            "name": name,
            "module": module,
            "storage": storage,
            "status": status,
            "available": status != "missing",
            "signals": signals,
            "gaps": gaps,
        }

    def _verdict(self, layers: dict[str, dict[str, Any]]) -> str:
        statuses = {layer["status"] for layer in layers.values()}
        if "missing" in statuses:
            return "action_required"
        if "partial" in statuses:
            return "needs_attention"
        return "healthy"

    def _should_skip(self, path: Path) -> bool:
        try:
            relative_parts = path.relative_to(self.project_root).parts
        except ValueError:
            return True
        return any(part in SKIP_SCAN_DIRS for part in relative_parts)

    def _relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.project_root))
        except ValueError:
            return str(path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            raw = path.read_text(encoding="utf-8")
            value = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}
