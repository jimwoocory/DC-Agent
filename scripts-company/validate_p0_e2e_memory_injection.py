#!/usr/bin/env python3
"""Validate P0 questions through the AstrBot llm_router memory injection path.

This is intentionally one step closer to the live assistant than
validate_p0_qa.py: it creates a small fake AstrBot event, runs the same
dc_memory_context.inject_memory_context_into_event() function used by
llm_router, and verifies that the routed prompt carries the right sources.

It still does not call an external LLM. That final live-answer test should be
run separately once we decide which production chat entry to exercise.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

DC_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = DC_ROOT / "data" / "plugins" / "llm_router"
if str(DC_ROOT) not in sys.path:
    sys.path.insert(0, str(DC_ROOT))
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from dc_memory_context import (  # noqa: E402
    BUSINESS_PLATFORM_ID,
    MEMORY_MARKER,
    inject_memory_context_into_event,
)

REPORT_PATH = DC_ROOT / "ObsidianVault" / "30_Reports" / "P0端到端问答注入验证报告.md"
CONTRACT_PATH = DC_ROOT / "harness" / "contracts" / "local_knowledge_base_p0_e2e.json"
WORKBENCH_PATH = DC_ROOT / "ObsidianVault" / "10_Index" / "复核工作台.md"
QA_CONTRACT_PATH = DC_ROOT / "harness" / "contracts" / "local_knowledge_base_p0_qa.json"


def load_p0_cases() -> tuple[Any, ...]:
    module_path = DC_ROOT / "scripts-company" / "validate_p0_qa.py"
    spec = importlib.util.spec_from_file_location("validate_p0_qa", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return tuple(module.TEST_CASES)


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


def parse_json_list(value: Any) -> list[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def md_escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def wiki_link(title: str) -> str:
    return f"[[{title}]]" if title else ""


def find_match(
    docs: list[dict[str, Any]], expected_titles: tuple[str, ...]
) -> tuple[int | None, dict[str, Any] | None]:
    for index, doc in enumerate(docs, start=1):
        title = str(doc.get("title") or "")
        if any(expected in title for expected in expected_titles):
            return index, doc
    return None, None


def evaluate_case(test_case: Any) -> dict[str, Any]:
    event = FakeEvent(test_case.question)
    injected = inject_memory_context_into_event(event)
    context = event.get_extra("dc_agent_memory_context") or {}
    docs = list(context.get("documents") or [])
    items = list(context.get("project_items") or [])
    hit_rank, hit = find_match(docs, test_case.expected_titles)

    checks: list[str] = []
    warnings: list[str] = []
    failures: list[str] = []

    if not injected:
        failures.append("真实插件注入函数未注入 memory context")
    else:
        checks.append("memory context 已注入")

    if (
        MEMORY_MARKER not in event.message_str
        or "</dc_agent_memory_context>" not in event.message_str
    ):
        failures.append("注入后的消息缺少 memory marker")
    else:
        checks.append("消息中包含 memory marker")

    if event.message_obj.message_str != event.message_str:
        failures.append("event.message_obj.message_str 未同步")
    else:
        checks.append("message_obj 已同步")

    if hit is None or hit_rank is None:
        failures.append("真实插件上下文未命中预期来源文档")
    elif hit_rank > test_case.max_rank:
        failures.append(f"预期来源排名第 {hit_rank}，超过 Top {test_case.max_rank}")
    else:
        checks.append(f"预期来源进入 Top {test_case.max_rank}")

    owner = str(hit.get("owner") or "") if hit else ""
    if getattr(test_case, "expected_owner", ""):
        if owner == test_case.expected_owner:
            checks.append(f"负责人={test_case.expected_owner}")
        else:
            failures.append(
                f"负责人应为 {test_case.expected_owner}，实际为 {owner or '空'}"
            )

    departments = parse_json_list(hit.get("departments_json")) if hit else []
    expected_departments = tuple(getattr(test_case, "expected_departments", ()) or ())
    if expected_departments:
        missing = [dept for dept in expected_departments if dept not in departments]
        if missing:
            failures.append(f"缺少部门归属：{', '.join(missing)}")
        else:
            checks.append(f"部门包含 {', '.join(expected_departments)}")

    rel_path = str(hit.get("rel_path") or "") if hit else ""
    source_path = str(hit.get("source_path") or "") if hit else ""
    if (
        hit
        and rel_path not in event.message_str
        and source_path not in event.message_str
    ):
        failures.append("注入提示中没有命中文档来源路径")
    elif hit:
        checks.append("注入提示中包含来源路径")

    if "不要编造" not in event.message_str:
        warnings.append("注入提示缺少防编造约束")
    else:
        checks.append("注入提示包含防编造约束")

    status = "fail" if failures else ("warn" if warnings else "pass")
    return {
        "id": test_case.case_id,
        "question": test_case.question,
        "status": status,
        "hit_rank": hit_rank,
        "hit": hit,
        "checks": checks,
        "warnings": warnings,
        "failures": failures,
        "documents": docs,
        "project_items": items,
        "injected_text_excerpt": event.message_str[:1200],
    }


def evaluate_guards() -> list[dict[str, Any]]:
    unsupported = FakeEvent("公私域舆情谁负责？", platform_id="webchat")
    unsupported_injected = inject_memory_context_into_event(unsupported)

    already = FakeEvent(
        f"公私域舆情谁负责？\n\n{MEMORY_MARKER}\n已有上下文\n</dc_agent_memory_context>"
    )
    already_injected = inject_memory_context_into_event(already)

    return [
        {
            "id": "unsupported_platform_no_injection",
            "status": "pass" if not unsupported_injected else "fail",
            "detail": "webchat 平台不在公司记忆注入白名单内",
        },
        {
            "id": "idempotent_no_double_injection",
            "status": "pass" if not already_injected else "fail",
            "detail": "已有 memory marker 时不会重复注入",
        },
    ]


def status_label(status: str) -> str:
    return {"pass": "通过", "warn": "警告", "fail": "失败"}.get(status, status)


def write_contract(
    results: list[dict[str, Any]], guards: list[dict[str, Any]], generated_at: str
) -> None:
    payload = {
        "name": "local_knowledge_base_p0_e2e_memory_injection",
        "generated_at": generated_at,
        "scope": "AstrBot llm_router dc_memory_context injection path",
        "qa_contract": str(QA_CONTRACT_PATH),
        "summary": {
            "total": len(results),
            "pass": sum(1 for item in results if item["status"] == "pass"),
            "warn": sum(1 for item in results if item["status"] == "warn"),
            "fail": sum(1 for item in results if item["status"] == "fail"),
            "guard_fail": sum(1 for item in guards if item["status"] == "fail"),
        },
        "cases": [
            {
                "id": item["id"],
                "question": item["question"],
                "status": item["status"],
                "hit_rank": item["hit_rank"],
                "hit": {
                    "title": (item.get("hit") or {}).get("title", ""),
                    "owner": (item.get("hit") or {}).get("owner", ""),
                    "departments": parse_json_list(
                        (item.get("hit") or {}).get("departments_json")
                    ),
                    "rel_path": (item.get("hit") or {}).get("rel_path", ""),
                    "source_path": (item.get("hit") or {}).get("source_path", ""),
                    "review_status": (item.get("hit") or {}).get("review_status", ""),
                    "score": (item.get("hit") or {}).get("score", ""),
                },
                "checks": item["checks"],
                "warnings": item["warnings"],
                "failures": item["failures"],
                "top_documents": [
                    {
                        "rank": index,
                        "title": doc.get("title", ""),
                        "owner": doc.get("owner", ""),
                        "rel_path": doc.get("rel_path", ""),
                        "score": doc.get("score", ""),
                    }
                    for index, doc in enumerate(item["documents"][:5], start=1)
                ],
            }
            for item in results
        ],
        "guards": guards,
    }
    CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTRACT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_report(
    results: list[dict[str, Any]], guards: list[dict[str, Any]], generated_at: str
) -> None:
    pass_count = sum(1 for item in results if item["status"] == "pass")
    warn_count = sum(1 for item in results if item["status"] == "warn")
    fail_count = sum(1 for item in results if item["status"] == "fail")
    guard_fail_count = sum(1 for item in guards if item["status"] == "fail")

    lines: list[str] = [
        "# P0端到端问答注入验证报告",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "## 结论",
        "",
        f"- P0 问题：{len(results)} 个",
        f"- 通过：{pass_count}",
        f"- 警告：{warn_count}",
        f"- 失败：{fail_count}",
        f"- 守卫项失败：{guard_fail_count}",
        "",
        "本报告验证的是 AstrBot `llm_router` 真实使用的公司记忆注入函数：消息进入「巅池-Agent小助手」后，是否能带上正确 NAS 资料、来源路径、负责人和防编造提示。这里仍不调用外部 LLM。",
        "",
        "## 验证表",
        "",
        "| 状态 | 问题 | 命中来源 | 排名 | 负责人 | 部门 | 说明 |",
        "|---|---|---|---:|---|---|---|",
    ]
    for item in results:
        hit = item.get("hit") or {}
        departments = parse_json_list(hit.get("departments_json"))
        notes = item["failures"] or item["warnings"] or item["checks"]
        lines.append(
            "| "
            + " | ".join(
                [
                    md_escape(status_label(item["status"])),
                    md_escape(item["question"]),
                    md_escape(wiki_link(str(hit.get("title") or ""))),
                    md_escape(item["hit_rank"] or ""),
                    md_escape(hit.get("owner") or ""),
                    md_escape(", ".join(str(dept) for dept in departments)),
                    md_escape("; ".join(notes)),
                ]
            )
            + " |"
        )

    lines.extend(["", "## 守卫项", "", "| 状态 | 项目 | 说明 |", "|---|---|---|"])
    for guard in guards:
        lines.append(
            "| "
            + " | ".join(
                [
                    md_escape(status_label(guard["status"])),
                    md_escape(guard["id"]),
                    md_escape(guard["detail"]),
                ]
            )
            + " |"
        )

    lines.extend(["", "## 注入 Top3", ""])
    for item in results:
        lines.extend([f"### {item['question']}", ""])
        for index, doc in enumerate(item["documents"][:3], start=1):
            departments = ", ".join(
                str(dept) for dept in parse_json_list(doc.get("departments_json"))
            )
            lines.append(
                f"{index}. {wiki_link(str(doc.get('title') or ''))}；"
                f"负责人={doc.get('owner') or '未标注'}；部门={departments or '未标注'}；"
                f"来源=`{doc.get('rel_path') or ''}`"
            )
        lines.append("")

    lines.extend(
        [
            "## 后续口径",
            "",
            "- 这一关通过后，说明 AstrBot 入口能拿到正确上下文。",
            "- 最后一关还需要选择一个真实聊天入口做 LLM 生成测试，验证最终回答是否真的引用来源并拒绝不确定问题。",
            "- 当前不扩大 P1/P2 导入，继续保护 NAS 原始文件不被搬动。",
        ]
    )

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_workbench_link() -> None:
    if not WORKBENCH_PATH.exists():
        return
    line = "P0 端到端注入验证：[[P0端到端问答注入验证报告]]"
    text = WORKBENCH_PATH.read_text(encoding="utf-8")
    if line in text:
        return
    marker = "P0 问答验证：[[P0问答验证报告]]"
    if marker in text:
        text = text.replace(marker, marker + "\n\n" + line, 1)
    else:
        text = text.rstrip() + "\n\n" + line + "\n"
    WORKBENCH_PATH.write_text(text, encoding="utf-8")


def main() -> int:
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    results = [evaluate_case(test_case) for test_case in load_p0_cases()]
    guards = evaluate_guards()
    write_contract(results, guards, generated_at)
    write_report(results, guards, generated_at)
    ensure_workbench_link()

    summary = {
        "report": str(REPORT_PATH),
        "contract": str(CONTRACT_PATH),
        "summary": {
            "total": len(results),
            "pass": sum(1 for item in results if item["status"] == "pass"),
            "warn": sum(1 for item in results if item["status"] == "warn"),
            "fail": sum(1 for item in results if item["status"] == "fail"),
            "guard_fail": sum(1 for item in guards if item["status"] == "fail"),
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["summary"]["fail"] or summary["summary"]["guard_fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
