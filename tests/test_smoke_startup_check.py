from pathlib import Path

from scripts.smoke_startup_check import dashboard_static_assets_ready


def test_smoke_ready_requires_static_index(tmp_path: Path) -> None:
    webui_dir = tmp_path / "webui"
    webui_dir.mkdir()

    assert dashboard_static_assets_ready(webui_dir) is False

    (webui_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")

    assert dashboard_static_assets_ready(webui_dir) is True
