#!/usr/bin/env python3
"""Run P0 live-answer smoke tests through local AstrBot OpenAPI.

This script validates the last mile after retrieval/injection:
1. Build the same memory-injected prompt used by llm_router.
2. Send it to local AstrBot `/api/v1/chat` with a temporary OpenAPI key.
3. Grade whether the final answer cites sources and keeps key facts.
4. Delete the temporary key and write an Obsidian report.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import secrets
import sqlite3
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

DC_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = DC_ROOT / "data" / "plugins" / "llm_router"
if str(DC_ROOT) not in sys.path:
    sys.path.insert(0, str(DC_ROOT))
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from dc_memory_context import (  # noqa: E402
    BUSINESS_PLATFORM_ID,
    inject_memory_context_into_event,
)

DATA_DB = DC_ROOT / "data" / "data_v4.db"
REPORT_PATH = DC_ROOT / "ObsidianVault" / "30_Reports" / "P0真实回答Smoke报告.md"
CONTRACT_PATH = (
    DC_ROOT / "harness" / "contracts" / "local_knowledge_base_p0_live_answer.json"
)
WORKBENCH_PATH = DC_ROOT / "ObsidianVault" / "10_Index" / "复核工作台.md"
ASTRBOT_CHAT_URL = "http://127.0.0.1:6185/api/v1/chat"
ASTRBOT_SESSIONS_URL = "http://127.0.0.1:6185/api/v1/chat/sessions"
PROVIDER_ID = "aihubmix/gemini-3.5-flash"
HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass(slots=True)
class FakeMessageObj:
    message_str: str


class FakeEvent:
    def __init__(self, text: str, platform_id: str = BUSINESS_PLATFORM_ID) -> None:
        self.message_str = text
        self.message_obj = FakeMessageObj(text)
        self._platform_id = platform_id
        self._extra: dict[str, Any] = {}

    def get_platform_id(self) -> str:
        return self._platform_id

    def set_extra(self, key: str, value: Any) -> None:
        self._extra[key] = value

    def get_extra(self, key: str) -> Any:
        return self._extra.get(key)


def load_p0_cases() -> tuple[Any, ...]:
    module_path = DC_ROOT / "scripts-company" / "validate_p0_qa.py"
    spec = importlib.util.spec_from_file_location("validate_p0_qa", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return tuple(module.TEST_CASES)


def hash_key(raw_key: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        raw_key.encode("utf-8"),
        b"astrbot_api_key",
        100_000,
    ).hex()


def create_temp_api_key() -> tuple[str, str]:
    raw_key = f"abk_{secrets.token_urlsafe(32)}"
    key_id = str(uuid4())
    now = datetime.now(UTC).replace(tzinfo=None)
    expires_at = now + timedelta(hours=1)
    now_sql = now.strftime("%Y-%m-%d %H:%M:%S.%f")
    expires_sql = expires_at.strftime("%Y-%m-%d %H:%M:%S.%f")
    with sqlite3.connect(DATA_DB) as conn:
        conn.execute(
            """
            INSERT INTO api_keys (
                created_at, updated_at, key_id, name, key_hash, key_prefix,
                scopes, created_by, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_sql,
                now_sql,
                key_id,
                "p0-live-answer-smoke",
                hash_key(raw_key),
                raw_key[:12],
                json.dumps(["chat"], ensure_ascii=False),
                "codex-p0-smoke",
                expires_sql,
            ),
        )
    return raw_key, key_id


def delete_temp_api_key(key_id: str) -> None:
    with sqlite3.connect(DATA_DB) as conn:
        conn.execute("DELETE FROM api_keys WHERE key_id = ?", (key_id,))


def cleanup_smoke_sessions() -> None:
    with sqlite3.connect(DATA_DB) as conn:
        session_ids = [
            row[0]
            for row in conn.execute(
                """
                SELECT session_id
                FROM platform_sessions
                WHERE platform_id = 'webchat'
                  AND creator = 'p0_live_smoke'
                  AND (session_id LIKE 'p0-live-%' OR session_id LIKE 'p0-debug-%')
                """
            ).fetchall()
        ]
        if not session_ids:
            return

        placeholders = ",".join("?" for _ in session_ids)
        conv_origins = [
            f"webchat:FriendMessage:webchat!p0_live_smoke!{session_id}"
            for session_id in session_ids
        ]
        conv_placeholders = ",".join("?" for _ in conv_origins)
        conn.execute(
            f"DELETE FROM platform_message_history WHERE platform_id = 'webchat' AND user_id IN ({placeholders})",
            session_ids,
        )
        conn.execute(
            f"DELETE FROM webchat_threads WHERE parent_session_id IN ({placeholders})",
            session_ids,
        )
        conn.execute(
            f"DELETE FROM conversations WHERE platform_id = 'webchat' AND user_id IN ({conv_placeholders})",
            conv_origins,
        )
        conn.execute(
            f"DELETE FROM platform_sessions WHERE platform_id = 'webchat' AND session_id IN ({placeholders})",
            session_ids,
        )


def build_injected_prompt(question: str) -> tuple[str, dict[str, Any]]:
    # Keep retrieval input clean. Generic words like "公司知识库" skew the lightweight
    # fallback search toward Method C/docs-system files instead of the business file.
    event = FakeEvent(question)
    injected = inject_memory_context_into_event(event)
    if not injected:
        raise RuntimeError(f"memory context not injected for question: {question}")
    context = event.get_extra("dc_agent_memory_context") or {}
    prompt = (
        f"{event.message_str}\n\n"
        "请基于上面的公司资料回答，并在最后列出来源。"
        "如果资料没有覆盖，请明确说不确定。"
        "只使用上面的 <dc_agent_memory_context>，不要调用工具或读取本地文件。"
    )
    return prompt, context


def parse_sse(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        data_lines = []
        for line in block.splitlines():
            if line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").strip())
        if not data_lines:
            continue
        payload = "\n".join(data_lines)
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def open_astrbot(req: urllib.request.Request, timeout: int = 30) -> str:
    try:
        with HTTP_OPENER.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"AstrBot HTTP {exc.code}: {body}") from exc


def preflight_api_key(api_key: str) -> None:
    req = urllib.request.Request(
        f"{ASTRBOT_SESSIONS_URL}?username=p0_live_smoke&page=1&page_size=1",
        headers={"X-API-Key": api_key, "Connection": "close"},
        method="GET",
    )
    raw = open_astrbot(req, timeout=15)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"AstrBot preflight returned non-JSON: {raw[:300]}") from exc
    if payload.get("status") != "ok":
        raise RuntimeError(f"AstrBot preflight failed: {payload}")


def collect_answer(events: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    final_complete = ""
    for event in events:
        if event.get("type") == "plain":
            data = str(event.get("data") or "")
            if event.get("streaming"):
                chunks.append(data)
            else:
                chunks = [data]
        elif event.get("type") == "complete":
            final_complete = str(event.get("data") or final_complete)
    answer = final_complete or "".join(chunks)
    return answer.strip()


def call_astrbot_chat(
    api_key: str, prompt: str, session_id: str
) -> tuple[str, list[dict[str, Any]], str]:
    payload = {
        "username": "p0_live_smoke",
        "session_id": session_id,
        "message": prompt,
        "selected_provider": PROVIDER_ID,
        "enable_streaming": True,
        "_skip_user_history": True,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        ASTRBOT_CHAT_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "Connection": "close",
        },
        method="POST",
    )
    raw = open_astrbot(req, timeout=180)
    events = parse_sse(raw)
    return collect_answer(events), events, raw


def grade_answer(
    answer: str, expected_terms: tuple[str, ...], source_terms: tuple[str, ...]
) -> dict[str, Any]:
    checks: list[str] = []
    failures: list[str] = []

    missing_expected = [term for term in expected_terms if term not in answer]
    if missing_expected:
        failures.append("缺少关键事实：" + ", ".join(missing_expected))
    else:
        checks.append("关键事实齐全")

    if not any(term in answer for term in source_terms):
        failures.append("未看到来源文档/来源路径")
    else:
        checks.append("包含来源")

    if "不确定" in answer or "需人工确认" in answer:
        checks.append("包含不确定/复核提示")

    return {
        "status": "fail" if failures else "pass",
        "checks": checks,
        "failures": failures,
    }


def md_escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def write_report(results: list[dict[str, Any]], generated_at: str) -> None:
    pass_count = sum(1 for item in results if item["status"] == "pass")
    fail_count = sum(1 for item in results if item["status"] == "fail")
    lines: list[str] = [
        "# P0真实回答Smoke报告",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "## 结论",
        "",
        f"- 测试问题：{len(results)} 个",
        f"- 通过：{pass_count}",
        f"- 失败：{fail_count}",
        "- 调用入口：AstrBot `/api/v1/chat`",
        f"- 模型：`{PROVIDER_ID}`",
        "",
        "本报告验证的是最终模型拿到公司记忆上下文后，是否能生成带来源、关键事实不跑偏的回答。测试使用一次性 OpenAPI key，脚本结束后已删除。",
        "",
        "## 验证表",
        "",
        "| 状态 | 问题 | 说明 |",
        "|---|---|---|",
    ]
    for item in results:
        notes = item["failures"] or item["checks"]
        lines.append(
            "| "
            + " | ".join(
                [
                    md_escape("通过" if item["status"] == "pass" else "失败"),
                    md_escape(item["question"]),
                    md_escape("; ".join(notes)),
                ]
            )
            + " |"
        )

    lines.extend(["", "## 回答摘录", ""])
    for item in results:
        lines.extend(
            [
                f"### {item['question']}",
                "",
                item["answer"][:1800].strip() or "（无回答）",
                "",
            ]
        )

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_contract(results: list[dict[str, Any]], generated_at: str) -> None:
    payload = {
        "name": "local_knowledge_base_p0_live_answer",
        "generated_at": generated_at,
        "entrypoint": ASTRBOT_CHAT_URL,
        "provider": PROVIDER_ID,
        "summary": {
            "total": len(results),
            "pass": sum(1 for item in results if item["status"] == "pass"),
            "fail": sum(1 for item in results if item["status"] == "fail"),
        },
        "cases": results,
    }
    CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTRACT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def ensure_workbench_link() -> None:
    if not WORKBENCH_PATH.exists():
        return
    line = "P0 真实回答 Smoke：[[P0真实回答Smoke报告]]"
    text = WORKBENCH_PATH.read_text(encoding="utf-8")
    if line in text:
        return
    marker = "P0 端到端注入验证：[[P0端到端问答注入验证报告]]"
    if marker in text:
        text = text.replace(marker, marker + "\n\n" + line, 1)
    else:
        text = text.rstrip() + "\n\n" + line + "\n"
    WORKBENCH_PATH.write_text(text, encoding="utf-8")


def main() -> int:
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    case_map = {case.case_id: case for case in load_p0_cases()}
    live_cases = [
        {
            "case": case_map["org_public_private_sentiment_owner"],
            "expected_terms": ("谭媛尹", "中台", "客户部", "策划"),
            "source_terms": (
                "公私域舆情监控二阶段结算汇报",
                "KOW项目-公、私域社群舆情管控执行SOP",
                "来源",
            ),
        },
        {
            "case": case_map["kow_sop_flow"],
            "expected_terms": ("社群", "群舆论", "素材库"),
            "source_terms": ("KOW项目-公、私域社群舆情管控执行SOP", "来源"),
        },
    ]

    api_key, key_id = create_temp_api_key()
    results: list[dict[str, Any]] = []
    try:
        preflight_api_key(api_key)
        for item in live_cases:
            test_case = item["case"]
            prompt, context = build_injected_prompt(test_case.question)
            answer = ""
            events: list[dict[str, Any]] = []
            attempts = 0
            for attempts in range(1, 3):
                answer, events, _raw = call_astrbot_chat(
                    api_key,
                    prompt,
                    f"p0-live-{test_case.case_id}-{uuid4().hex[:8]}",
                )
                if answer.strip():
                    break
            grade = grade_answer(answer, item["expected_terms"], item["source_terms"])
            results.append(
                {
                    "id": test_case.case_id,
                    "question": test_case.question,
                    "status": grade["status"],
                    "checks": grade["checks"],
                    "failures": grade["failures"],
                    "answer": answer,
                    "attempts": attempts,
                    "events_seen": len(events),
                    "context_documents": [
                        doc.get("title", "")
                        for doc in (context.get("documents") or [])[:5]
                    ],
                }
            )
    finally:
        delete_temp_api_key(key_id)
        cleanup_smoke_sessions()

    write_report(results, generated_at)
    write_contract(results, generated_at)
    ensure_workbench_link()
    summary = {
        "report": str(REPORT_PATH),
        "contract": str(CONTRACT_PATH),
        "summary": {
            "total": len(results),
            "pass": sum(1 for item in results if item["status"] == "pass"),
            "fail": sum(1 for item in results if item["status"] == "fail"),
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["summary"]["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
