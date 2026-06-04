"""巅池-技术 日报阶段 B：用 agy 完成分析、学习和巡检 → report.md。"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date as _date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from agy_runner import run_agy  # noqa: E402

DC_ROOT = Path("/Users/dianchi/DC-Agent")
PLUGIN_DIR = DC_ROOT / "data/plugins/dianchi_tech"
DATA_ROOT = DC_ROOT / "data/dianchi_tech"
LEARNING_LOG = DATA_ROOT / "learning_log.json"
PROMPT_PATH = PLUGIN_DIR / "prompts/agy_analyze.md"

LOG_RE = re.compile(r"LEARNING_LOG_JSON\s*:\s*(\{.*?\})\s*(?:$|\n)", re.DOTALL)


def _strip_markdown_fence(text: str) -> str:
    clean = text.strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        clean = "\n".join(lines).strip()
    return clean


def _clean_report_text(text: str) -> str:
    text = _strip_markdown_fence(text)
    text = LOG_RE.sub("", text)
    text = re.sub(r"(?im)^\s*DONE:\s*report\.md saved\s*$", "", text)
    return text.strip() + "\n"


def _append_learning_log(text: str, date_str: str) -> dict:
    match = LOG_RE.search(text)
    if not match:
        return {"updated": False, "reason": "missing LEARNING_LOG_JSON"}
    try:
        entry = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        return {"updated": False, "reason": f"bad json: {exc}"}
    if not isinstance(entry, dict):
        return {"updated": False, "reason": "entry is not object"}
    entry.setdefault("date", date_str)

    try:
        data = json.loads(LEARNING_LOG.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            data = []
    except FileNotFoundError:
        data = []
    except Exception:
        data = []

    data = [
        old
        for old in data
        if not (
            isinstance(old, dict)
            and old.get("date") == entry.get("date")
            and old.get("topic") == entry.get("topic")
        )
    ]
    data.append(entry)
    LEARNING_LOG.parent.mkdir(parents=True, exist_ok=True)
    LEARNING_LOG.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"updated": True, "entry": entry}


def build_prompt(date_str: str, day_dir: Path) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    raw_path = day_dir / "raw_news.md"
    raw_news = (
        raw_path.read_text(encoding="utf-8", errors="replace")
        if raw_path.exists()
        else f"# 硅谷 AI 资讯 {date_str}\n\n⚠️ raw_news.md 缺失。"
    )
    return (
        template.replace("{DATE}", date_str)
        .replace("{DAY_DIR}", str(day_dir))
        .replace("{RAW_NEWS}", raw_news)
    )


def run(date_str: str, day_dir: Path, timeout: int) -> int:
    day_dir.mkdir(parents=True, exist_ok=True)
    prompt = build_prompt(date_str, day_dir)
    started = time.monotonic()
    result = run_agy(prompt, timeout=timeout)
    meta = {
        "channel": "agy",
        "kind": result.get("kind"),
        "elapsed_sec": result.get("elapsed_sec"),
        "error": result.get("error", ""),
        "ok": bool(result.get("ok")),
    }
    (day_dir / "analysis_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not result.get("ok") or not result.get("text", "").strip():
        print(json.dumps(meta, ensure_ascii=False))
        return 1

    raw_text = str(result["text"])
    report_text = _clean_report_text(raw_text)
    (day_dir / "report.md").write_text(report_text, encoding="utf-8")
    learning = _append_learning_log(raw_text, date_str)
    meta["report_bytes"] = (day_dir / "report.md").stat().st_size
    meta["learning_log"] = learning
    meta["wall_elapsed_sec"] = round(time.monotonic() - started, 2)
    (day_dir / "analysis_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=_date.today().isoformat())
    parser.add_argument("--day-dir", default="")
    parser.add_argument("--timeout", type=int, default=3300)
    args = parser.parse_args()
    day_dir = Path(args.day_dir) if args.day_dir else DATA_ROOT / args.date
    return run(args.date, day_dir, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
