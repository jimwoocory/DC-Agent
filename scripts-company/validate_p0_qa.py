#!/usr/bin/env python3
"""Validate P0 knowledge-base Q&A retrieval seeds.

The script does not call an LLM. It checks whether each business question can
retrieve the expected source document and required metadata from the local NAS
memory index, then writes a grounded Obsidian report for human review.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

DC_ROOT = Path(__file__).resolve().parents[1]
if str(DC_ROOT) not in sys.path:
    sys.path.insert(0, str(DC_ROOT))

from nas_sync.dc_memory_indexer import (  # noqa: E402
    NAS_MEMORY_DB,
    dedupe_query_rows,
    fetch_fts_rows,
    fetch_like_rows,
    query_terms,
)

OBSIDIAN_ROOT = DC_ROOT / "ObsidianVault"
REPORT_PATH = OBSIDIAN_ROOT / "30_Reports" / "P0问答验证报告.md"
CONTRACT_PATH = DC_ROOT / "harness" / "contracts" / "local_knowledge_base_p0_qa.json"
WORKBENCH_PATH = OBSIDIAN_ROOT / "10_Index" / "复核工作台.md"
SEED_PATH = OBSIDIAN_ROOT / "20_Bridges" / "Review" / "P0-问答测试种子.md"


@dataclass(frozen=True, slots=True)
class QATestCase:
    case_id: str
    question: str
    expected_titles: tuple[str, ...]
    answer_points: tuple[str, ...]
    max_rank: int = 3
    expected_owner: str = ""
    expected_departments: tuple[str, ...] = ()
    note: str = ""


TEST_CASES: tuple[QATestCase, ...] = (
    QATestCase(
        case_id="org_public_private_sentiment_owner",
        question="公私域舆情谁负责，属于哪个部门？",
        expected_titles=(
            "公私域舆情监控二阶段结算汇报",
            "KOW项目-公、私域社群舆情管控执行SOP",
        ),
        expected_owner="谭媛尹",
        expected_departments=("中台", "客户部", "策划"),
        answer_points=(
            "负责人按项目归属落到谭媛尹。",
            "组织口径为中台统筹，客户部与策划协同。",
            "韦欢、潘彩月、黄丽清等在 SOP 中体现为具体更新或执行角色。",
        ),
        note="校验用户刚纠正过的组织归属，不再把执行人直接当项目负责人。",
    ),
    QATestCase(
        case_id="kow_sop_flow",
        question="KOW 公私域舆情管控 SOP 的主要流程是什么？",
        expected_titles=("KOW项目-公、私域社群舆情管控执行SOP",),
        expected_owner="谭媛尹",
        expected_departments=("中台", "客户部", "策划"),
        answer_points=(
            "核心流程包括社群收集、社群仪表盘、群舆论收集、舆论收集仪表盘。",
            "素材库、金句、玩梗和真实话术转化每日更新。",
            "项目运行统筹、群引导排期、KOS 账号、建联和周报分别有更新负责人。",
        ),
    ),
    QATestCase(
        case_id="method_c_flow",
        question="Method C 的核心流程是什么？",
        expected_titles=(
            "Method_C_5分钟指南",
            "Method_C_文件索引",
            "Method_C_执行清单",
            "Method_C_完整技术文档",
            "Method_C_系统综述",
            "Method_C_快速开始",
        ),
        answer_points=(
            "员工在飞书群组分享文档。",
            "feishu_sync_method_c.py 监听消息并提取文档链接。",
            "文件下载到 NAS 后由监听/索引链路进入知识库，机器人再基于索引回答。",
        ),
    ),
    QATestCase(
        case_id="feishu_to_nas_components",
        question="飞书文档同步到 NAS 后，哪些组件负责下载、监听和索引？",
        expected_titles=(
            "飞书文档自动同步系统 - 执行方案",
            "Method_C_完整技术文档",
            "Method_C_系统综述",
        ),
        answer_points=(
            "下载侧是飞书同步脚本，尤其是 feishu_sync_method_c.py 这条 Method C 链路。",
            "存储侧落到 NAS 知识目录。",
            "监听和索引侧由 watcher.py、dc_memory_indexer.py 等组件把文档纳入本地知识库。",
        ),
    ),
    QATestCase(
        case_id="feishu_ai_phases",
        question="飞书 AI 机器人规划分几个阶段？",
        expected_titles=(
            "飞书AI机器人功能规划方案",
            "飞书AI机器人对接技术方案",
        ),
        answer_points=(
            "规划按三个阶段逐步上线。",
            "先从基础 AI 助手能力起步，再扩展到业务场景和复杂自动化。",
            "原则包括零额外采购、数据内网流转、分阶段上线和效果驱动迭代。",
        ),
    ),
    QATestCase(
        case_id="openclaw_permissions",
        question="openclaw 目前已有权限能支持哪些机器人能力？",
        expected_titles=("openclaw应用API权限申请方案",),
        answer_points=(
            "openclaw 已配置 63 项应用身份权限。",
            "权限主要集中在消息与群组领域。",
            "可支撑消息收发、群组管理、卡片交互等飞书机器人核心能力。",
        ),
    ),
    QATestCase(
        case_id="liuyi_campaign_idea",
        question="六一五菱联盟鉴宝活动的核心创意和执行动作是什么？",
        expected_titles=("六一五菱联盟鉴宝执行策划案",),
        answer_points=(
            "借六一儿童节节点，把活动包装成宝宝巴士探索之旅。",
            "邀约亲子博主和带娃用户参观五菱制造工厂。",
            "通过工厂参观、博士讲解和话题传播激发亲子内容扩散。",
        ),
    ),
    QATestCase(
        case_id="script_upgrade_feasibility",
        question="脚本升级和机器人模型可行性评估的结论是什么？",
        expected_titles=("脚本升级和机器人模型可行性评估",),
        answer_points=(
            "评估对象是 Method C 脚本升级和机器人模型配置。",
            "升级重点包括识别群组附件、文件格式、下载附件、按部门分类、自动命名和去重。",
            "结论用于判断飞书 IM/Drive 能力、权限与机器人知识库链路是否足够支撑落地。",
        ),
    ),
)


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
    rows: list[dict[str, Any]], test_case: QATestCase
) -> tuple[int | None, dict[str, Any] | None]:
    for index, row in enumerate(rows, start=1):
        title = str(row.get("title") or "")
        if any(expected in title for expected in test_case.expected_titles):
            return index, row
    return None, None


def search_case(
    conn: sqlite3.Connection, test_case: QATestCase
) -> list[dict[str, Any]]:
    terms = query_terms(test_case.question)
    fetch_limit = 1000
    rows = fetch_fts_rows(conn, test_case.question, fetch_limit)
    rows.extend(fetch_like_rows(conn, terms, fetch_limit))
    return dedupe_query_rows(rows, terms, test_case.question, 8)


def evaluate_case(conn: sqlite3.Connection, test_case: QATestCase) -> dict[str, Any]:
    rows = search_case(conn, test_case)
    hit_rank, hit = find_match(rows, test_case)

    checks: list[str] = []
    failures: list[str] = []
    warnings: list[str] = []

    if hit is None or hit_rank is None:
        failures.append("未命中预期来源")
    elif hit_rank > test_case.max_rank:
        failures.append(
            f"预期来源排名第 {hit_rank}，超过验收上限 Top {test_case.max_rank}"
        )
    else:
        checks.append(f"预期来源进入 Top {test_case.max_rank}")

    owner = str(hit.get("owner") or "") if hit else ""
    if test_case.expected_owner:
        if owner == test_case.expected_owner:
            checks.append(f"负责人={test_case.expected_owner}")
        else:
            failures.append(
                f"负责人应为 {test_case.expected_owner}，实际为 {owner or '空'}"
            )

    departments = parse_json_list(hit.get("departments_json")) if hit else []
    if test_case.expected_departments:
        missing = [
            dept for dept in test_case.expected_departments if dept not in departments
        ]
        if missing:
            failures.append(f"缺少部门归属：{', '.join(missing)}")
        else:
            checks.append(f"部门包含 {', '.join(test_case.expected_departments)}")

    if hit and not str(hit.get("source_path") or ""):
        warnings.append("命中文档缺少 source_path")

    status = "fail" if failures else ("warn" if warnings else "pass")
    return {
        "id": test_case.case_id,
        "question": test_case.question,
        "status": status,
        "hit_rank": hit_rank,
        "hit": hit,
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "answer_points": list(test_case.answer_points),
        "note": test_case.note,
        "top_hits": rows,
    }


def db_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    docs = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
    chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    pending = conn.execute(
        "SELECT count(*) FROM documents WHERE review_status = 'need_review'"
    ).fetchone()[0]
    confirmed = conn.execute(
        "SELECT count(*) FROM documents WHERE review_status = 'confirmed'"
    ).fetchone()[0]
    parser_rows = conn.execute(
        """
        SELECT parser, count(*) AS count
        FROM documents
        GROUP BY parser
        ORDER BY count DESC, parser
        """
    ).fetchall()
    doc_type_rows = conn.execute(
        """
        SELECT coalesce(nullif(doc_type, ''), '未分类') AS doc_type, count(*) AS count
        FROM documents
        GROUP BY coalesce(nullif(doc_type, ''), '未分类')
        ORDER BY count DESC, doc_type
        """
    ).fetchall()
    return {
        "documents": docs,
        "chunks": chunks,
        "need_review": pending,
        "confirmed": confirmed,
        "parsers": {row[0]: row[1] for row in parser_rows},
        "doc_types": {row[0]: row[1] for row in doc_type_rows},
    }


def status_label(status: str) -> str:
    return {"pass": "通过", "warn": "警告", "fail": "失败"}.get(status, status)


def write_report(
    results: list[dict[str, Any]], stats: dict[str, Any], generated_at: str
) -> None:
    pass_count = sum(1 for item in results if item["status"] == "pass")
    warn_count = sum(1 for item in results if item["status"] == "warn")
    fail_count = sum(1 for item in results if item["status"] == "fail")
    parser_text = ", ".join(
        f"{parser} {count}" for parser, count in stats["parsers"].items()
    )

    lines: list[str] = [
        "# P0问答验证报告",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "## 结论",
        "",
        f"- 测试问题：{len(results)} 个",
        f"- 通过：{pass_count}",
        f"- 警告：{warn_count}",
        f"- 失败：{fail_count}",
        f"- 当前索引：{stats['documents']} 个文档 / {stats['chunks']} 个 chunks",
        f"- 复核状态：confirmed {stats['confirmed']}，need_review {stats['need_review']}",
        f"- 解析器分布：{parser_text}",
        "",
        "本次验证只检查本地 NAS 记忆索引的检索命中、来源路径和关键元数据，不调用大模型生成长回答。通过后才适合进入 AstrBot 端到端问答。",
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

    lines.extend(["", "## 模拟回答要点", ""])
    for item in results:
        hit = item.get("hit") or {}
        lines.extend(
            [
                f"### {item['question']}",
                "",
                f"- 状态：{status_label(item['status'])}",
                f"- 来源：{wiki_link(str(hit.get('title') or '未命中'))}",
                f"- 源路径：`{hit.get('source_path') or ''}`",
            ]
        )
        if item.get("note"):
            lines.append(f"- 备注：{item['note']}")
        for point in item["answer_points"]:
            lines.append(f"- {point}")
        top_titles = [str(row.get("title") or "") for row in item["top_hits"][:3]]
        if top_titles:
            lines.append(f"- Top3：{', '.join(top_titles)}")
        lines.append("")

    lines.extend(
        [
            "## 后续口径",
            "",
            "- P0 通过代表“检索层能找到正确来源”，不代表最终机器人回答已经上线。",
            "- 下一步需要在 AstrBot 或 DC-Agent 问答入口做端到端回答测试，检查是否带来源、是否拒答不确定问题。",
            "- 对 PDF/PPTX 的业务素材，仍建议先做人工复核和去重，再扩大导入。",
        ]
    )

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_contract(
    results: list[dict[str, Any]], stats: dict[str, Any], generated_at: str
) -> None:
    payload = {
        "name": "local_knowledge_base_p0_qa",
        "generated_at": generated_at,
        "db": str(NAS_MEMORY_DB),
        "stats": stats,
        "summary": {
            "total": len(results),
            "pass": sum(1 for item in results if item["status"] == "pass"),
            "warn": sum(1 for item in results if item["status"] == "warn"),
            "fail": sum(1 for item in results if item["status"] == "fail"),
        },
        "cases": [
            {
                "id": item["id"],
                "question": item["question"],
                "status": item["status"],
                "hit_rank": item["hit_rank"],
                "hit": {
                    "title": (item.get("hit") or {}).get("title", ""),
                    "source_path": (item.get("hit") or {}).get("source_path", ""),
                    "owner": (item.get("hit") or {}).get("owner", ""),
                    "departments": parse_json_list(
                        (item.get("hit") or {}).get("departments_json")
                    ),
                    "parser": (item.get("hit") or {}).get("parser", ""),
                    "review_status": (item.get("hit") or {}).get("review_status", ""),
                    "score": (item.get("hit") or {}).get("score", ""),
                },
                "checks": item["checks"],
                "warnings": item["warnings"],
                "failures": item["failures"],
                "answer_points": item["answer_points"],
                "top_hits": [
                    {
                        "rank": index,
                        "title": row.get("title", ""),
                        "source_path": row.get("source_path", ""),
                        "score": row.get("score", ""),
                    }
                    for index, row in enumerate(item["top_hits"][:5], start=1)
                ],
            }
            for item in results
        ],
    }
    CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTRACT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def ensure_link(path: Path, link_line: str, after: str) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    if link_line in text:
        return
    marker = after
    if marker in text:
        text = text.replace(marker, marker + "\n\n" + link_line, 1)
    else:
        text = text.rstrip() + "\n\n" + link_line + "\n"
    path.write_text(text, encoding="utf-8")


def update_workbench_stats(stats: dict[str, Any], generated_at: str) -> None:
    if not WORKBENCH_PATH.exists():
        return
    text = WORKBENCH_PATH.read_text(encoding="utf-8")
    parser_text = ", ".join(
        f"{parser} {count}" for parser, count in stats["parsers"].items()
    )
    doc_type_text = ", ".join(
        f"{doc_type} {count}" for doc_type, count in stats["doc_types"].items()
    )
    block = "\n".join(
        [
            "## 当前状态",
            "",
            f"- 已索引文档：{stats['documents']}",
            f"- 待复核：{stats['need_review']}",
            f"- 已确认：{stats['confirmed']}",
            f"- chunks：{stats['chunks']}",
            f"- 解析器分布：{parser_text}",
            f"- 类型分布：{doc_type_text}",
        ]
    )
    text = text.replace(
        text.split("## 当前状态", 1)[0],
        f"# 复核工作台\n\n生成时间：`{generated_at}`\n\n",
        1,
    )
    start = text.find("## 当前状态")
    end = text.find("## 推荐顺序")
    if start != -1 and end != -1 and start < end:
        text = text[:start] + block + "\n\n" + text[end:]
    WORKBENCH_PATH.write_text(text, encoding="utf-8")


def main() -> int:
    if not NAS_MEMORY_DB.exists():
        print(f"NAS memory db not found: {NAS_MEMORY_DB}", file=sys.stderr)
        return 1

    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    with sqlite3.connect(NAS_MEMORY_DB) as conn:
        conn.row_factory = sqlite3.Row
        stats = db_stats(conn)
        results = [evaluate_case(conn, test_case) for test_case in TEST_CASES]

    write_report(results, stats, generated_at)
    write_contract(results, stats, generated_at)
    update_workbench_stats(stats, generated_at)
    ensure_link(
        WORKBENCH_PATH,
        "P0 问答验证：[[P0问答验证报告]]",
        "第一轮问答测试种子：[[P0-问答测试种子]]",
    )
    ensure_link(
        SEED_PATH,
        "验证报告：[[P0问答验证报告]]",
        "下一步只把这些种子用于问答效果验证。",
    )

    summary = {
        "report": str(REPORT_PATH),
        "contract": str(CONTRACT_PATH),
        "summary": {
            "total": len(results),
            "pass": sum(1 for item in results if item["status"] == "pass"),
            "warn": sum(1 for item in results if item["status"] == "warn"),
            "fail": sum(1 for item in results if item["status"] == "fail"),
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["summary"]["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
