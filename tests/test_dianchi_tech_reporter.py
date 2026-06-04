from __future__ import annotations

import json
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "data/plugins/dianchi_tech"
sys.path.insert(0, str(PLUGIN_DIR))

from reporter import load_report  # noqa: E402


def test_load_report_fallback_includes_retry_attempts(tmp_path: Path) -> None:
    run_meta = {
        "agy": {"exit": 0},
        "analysis": {
            "exit": 124,
            "duration_seconds": 4810,
            "max_attempts": 2,
            "hard_timeout_seconds": 2400,
            "attempts": [
                {
                    "attempt": 1,
                    "exit": 124,
                    "kind": "timeout",
                    "duration_seconds": 2405,
                },
                {
                    "attempt": 2,
                    "exit": 1,
                    "kind": "failed",
                    "duration_seconds": 199,
                },
            ],
        },
    }
    (tmp_path / "run.json").write_text(
        json.dumps(run_meta, ensure_ascii=False),
        encoding="utf-8",
    )

    text, ok = load_report(tmp_path, "2026-05-29")

    assert ok is False
    assert "夜间任务退出码：agy=0, analysis=124" in text
    assert "硬超时：2400s" in text
    assert "最多尝试：2 次" in text
    assert "第1次=timeout/exit124/2405s" in text
    assert "第2次=failed/exit1/199s" in text
