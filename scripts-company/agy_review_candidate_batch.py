#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "nas_memory.db"
OUT_DIR = ROOT / "data" / "agy_review_candidates"


def loads_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def compact_metadata(metadata_json: str | None) -> dict[str, Any]:
    metadata = loads_json(metadata_json, {})
    return {
        "graph_evidence": metadata.get("graph_evidence") or [],
        "rel_path": metadata.get("rel_path") or "",
        "source": metadata.get("source") or "",
        "warning": metadata.get("warning") or "",
        "text_chars": metadata.get("text_chars") or 0,
    }


def fetch_batch(limit: int, min_confidence: float, offset: int) -> list[dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select
              doc_key,
              title,
              rel_path,
              project_name,
              doc_type,
              initiator,
              owner,
              departments_json,
              participants_json,
              confidence,
              summary,
              metadata_json
            from documents
            where review_status = 'need_review'
              and coalesce(confidence, 0) >= ?
            order by confidence desc, indexed_at desc, title
            limit ? offset ?
            """,
            (min_confidence, limit, offset),
        ).fetchall()
    finally:
        conn.close()

    docs: list[dict[str, Any]] = []
    for row in rows:
        docs.append(
            {
                "doc_key": row["doc_key"],
                "title": row["title"] or "",
                "rel_path": row["rel_path"] or "",
                "project_name": row["project_name"] or "",
                "doc_type": row["doc_type"] or "",
                "initiator": row["initiator"] or "",
                "current_owner": row["owner"] or "",
                "current_departments": loads_json(row["departments_json"], []),
                "participants": loads_json(row["participants_json"], []),
                "confidence": row["confidence"] or 0,
                "summary": (row["summary"] or "")[:1800],
                "metadata": compact_metadata(row["metadata_json"]),
            }
        )
    return docs


def build_prompt(batch_path: Path) -> str:
    return f"""
你是 DC-Agent 公司知识库的业务关系复核助手。请读取 JSON 文件：
{batch_path}

任务：只生成“候选关系”，不要修改任何文件或数据库。

请逐条判断：
1. 文档里的人员分别更像 owner、participant、client_contact、subject、executor、unknown 中哪一种。
2. 能否建议 owner 和 departments。只有证据明确才给 suggested_owner。
3. 是否应该进入 rule_confirmed。保守判断；如果只是人名首次出现、拍摄对象、客户、专家、素材作者，不要确认 owner。
4. 给出 evidence 和 risk_reason。

输出必须是纯 JSON，不要 Markdown，不要解释性正文。格式：
{{
  "batch_id": "...",
  "model_note": "agy candidate extraction",
  "items": [
    {{
      "doc_key": "...",
      "title": "...",
      "suggested_owner": "",
      "suggested_departments": [],
      "people_roles": [{{"name": "...", "role": "unknown", "reason": "..."}}],
      "candidate_status": "no_change|candidate_rule|needs_human",
      "confidence": 0.0,
      "evidence": [],
      "risk_reason": ""
    }}
  ]
}}
""".strip()


def write_batch(
    docs: list[dict[str, Any]], limit: int, min_confidence: float, offset: int
) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    batch_id = (
        f"need_review_{generated_at}_limit{limit}_min{min_confidence:g}_offset{offset}"
    )
    batch_path = OUT_DIR / f"{batch_id}.input.json"
    prompt_path = OUT_DIR / f"{batch_id}.prompt.txt"
    payload = {
        "batch_id": batch_id,
        "generated_at": generated_at,
        "source": str(DB_PATH),
        "policy": {
            "mode": "candidate_only",
            "must_not_update_db": True,
            "do_not_confirm_owner_from_first_mention_only": True,
            "do_not_confirm_subject_or_client_as_owner": True,
        },
        "documents": docs,
    }
    batch_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    prompt_path.write_text(build_prompt(batch_path), encoding="utf-8")
    return batch_path, prompt_path


def run_agy(prompt_path: Path, timeout_seconds: int) -> Path:
    output_path = prompt_path.with_suffix(".agy-output.json")
    result = subprocess.run(
        [
            "agy",
            "--prompt",
            prompt_path.read_text(encoding="utf-8"),
            "--print-timeout",
            f"{timeout_seconds}s",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout_seconds + 30,
        check=False,
    )
    output_path.write_text(result.stdout.strip() + "\n", encoding="utf-8")
    if result.returncode != 0:
        err_path = prompt_path.with_suffix(".agy-stderr.txt")
        err_path.write_text(result.stderr, encoding="utf-8")
        raise SystemExit(
            f"agy failed with code {result.returncode}; stderr saved to {err_path}"
        )
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--min-confidence", type=float, default=0.88)
    parser.add_argument("--run-agy", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args()

    docs = fetch_batch(args.limit, args.min_confidence, args.offset)
    batch_path, prompt_path = write_batch(
        docs, args.limit, args.min_confidence, args.offset
    )
    output: dict[str, Any] = {
        "documents": len(docs),
        "batch_path": str(batch_path),
        "prompt_path": str(prompt_path),
    }
    if args.run_agy:
        output["agy_output_path"] = str(run_agy(prompt_path, args.timeout_seconds))
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
