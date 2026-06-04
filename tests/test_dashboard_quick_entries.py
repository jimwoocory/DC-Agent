from pathlib import Path

from astrbot.dashboard.quick_entries import ASSET_NAME, install_dashboard_quick_entries


def _make_dist(tmp_path: Path) -> tuple[Path, Path]:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><head><title>x</title></head><body></body></html>",
        encoding="utf-8",
    )
    source = tmp_path / ASSET_NAME
    source.write_text("console.log('quick entries');\n", encoding="utf-8")
    return dist, source


def test_install_dashboard_quick_entries_patches_index_and_asset(tmp_path):
    dist, source = _make_dist(tmp_path)

    result = install_dashboard_quick_entries(dist, source_path=source)

    assert result.ok is True
    assert result.changed is True
    assert (dist / "assets" / ASSET_NAME).read_text(
        encoding="utf-8"
    ) == source.read_text(encoding="utf-8")
    index_html = (dist / "index.html").read_text(encoding="utf-8")
    assert "data-dc-dashboard-quick-entries" in index_html
    assert f"/assets/{ASSET_NAME}" in index_html


def test_install_dashboard_quick_entries_check_detects_stale_asset(tmp_path):
    dist, source = _make_dist(tmp_path)
    install_dashboard_quick_entries(dist, source_path=source)

    ok_result = install_dashboard_quick_entries(
        dist,
        source_path=source,
        check_only=True,
    )
    assert ok_result.ok is True

    (dist / "assets" / ASSET_NAME).write_text("stale", encoding="utf-8")
    stale_result = install_dashboard_quick_entries(
        dist,
        source_path=source,
        check_only=True,
    )

    assert stale_result.ok is False
    assert "asset missing or stale" in stale_result.messages[0]


def test_install_dashboard_quick_entries_refreshes_script_version(tmp_path):
    dist, source = _make_dist(tmp_path)
    install_dashboard_quick_entries(dist, source_path=source)
    first_index_html = (dist / "index.html").read_text(encoding="utf-8")

    source.write_text("console.log('quick entries v2');\n", encoding="utf-8")
    result = install_dashboard_quick_entries(dist, source_path=source)
    second_index_html = (dist / "index.html").read_text(encoding="utf-8")

    assert result.changed is True
    assert "?v=" in second_index_html
    assert first_index_html != second_index_html
    assert second_index_html.count("data-dc-dashboard-quick-entries") == 1
