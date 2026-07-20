from __future__ import annotations

from pathlib import Path


def test_feishu_doc_fetcher_config_does_not_depend_on_mac_home() -> None:
    source = Path("data/plugins/feishu_doc_fetcher/main.py").read_text(encoding="utf-8")

    assert "/Users/dianchi" not in source
    assert "get_astrbot_data_path" in source
