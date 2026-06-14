from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path


def _load_dianchi_tech_module():
    module_path = Path("data/plugins/dianchi_tech/main.py")
    spec = importlib.util.spec_from_file_location("dianchi_tech_main", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _plugin(module, data_root: Path):
    plugin = module.DianchiTechPlugin.__new__(module.DianchiTechPlugin)
    plugin.data_root = data_root
    plugin.recent_limit = 14
    plugin.max_report_age_days = 2
    plugin.cai_ting_open_id = ""
    plugin.wiki_space_name = "DC-Agent 运维"
    return plugin


def test_dianchi_tech_health_missing_data_is_not_healthy(tmp_path: Path) -> None:
    module = _load_dianchi_tech_module()
    plugin = _plugin(module, tmp_path / "missing")

    payload = asyncio.run(plugin._api_health())

    assert payload["status"] == "warning"
    assert payload["message"] == "no data yet"
    assert payload["data"]["healthy"] is False
    assert payload["data"]["reason"] == "no_data"


def test_dianchi_tech_health_empty_data_is_not_healthy(tmp_path: Path) -> None:
    module = _load_dianchi_tech_module()
    data_root = tmp_path / "dianchi_tech"
    data_root.mkdir()
    plugin = _plugin(module, data_root)

    payload = asyncio.run(plugin._api_health())

    assert payload["status"] == "warning"
    assert payload["data"]["healthy"] is False
    assert payload["data"]["reason"] == "no_report"


def test_dianchi_tech_health_recent_report_is_healthy(tmp_path: Path) -> None:
    module = _load_dianchi_tech_module()
    data_root = tmp_path / "dianchi_tech"
    today = date.today().isoformat()
    day_dir = data_root / today
    day_dir.mkdir(parents=True)
    (day_dir / "report.md").write_text("今日技术日报", encoding="utf-8")
    plugin = _plugin(module, data_root)

    payload = asyncio.run(plugin._api_health())

    assert payload["status"] == "ok"
    assert payload["message"] is None
    assert payload["data"]["healthy"] is True
    assert payload["data"]["latest_report_date"] == today


def test_dianchi_tech_health_stale_report_is_not_healthy(tmp_path: Path) -> None:
    module = _load_dianchi_tech_module()
    data_root = tmp_path / "dianchi_tech"
    stale_date = (date.today() - timedelta(days=5)).isoformat()
    day_dir = data_root / stale_date
    day_dir.mkdir(parents=True)
    (day_dir / "report.md").write_text("旧技术日报", encoding="utf-8")
    plugin = _plugin(module, data_root)

    payload = asyncio.run(plugin._api_health())

    assert payload["status"] == "warning"
    assert payload["data"]["healthy"] is False
    assert payload["data"]["reason"] == "stale_report"
    assert payload["data"]["latest_report_date"] == stale_date
