from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES_ROOT = ROOT / "drafts" / "aihubmix_standalone" / "pages"
WORKSPACE_PATHS = (
    PAGES_ROOT / "gallery" / "index.html",
    PAGES_ROOT / "video" / "index.html",
    PAGES_ROOT / "writing" / "index.html",
)


def test_workspace_headers_use_single_toolbox_brand():
    for path in WORKSPACE_PATHS:
        html = path.read_text(encoding="utf-8")

        assert 'class="product-title" href="/"' in html
        assert "巅池 Agent 工具箱" in html
        assert 'class="company-logo"' not in html
        assert "Local Studio" not in html


def test_workspace_mode_navigation_is_centered_and_equal_width():
    for path in WORKSPACE_PATHS:
        html = path.read_text(encoding="utf-8")

        assert (
            "grid-template-columns: minmax(230px, 1fr) auto minmax(230px, 1fr);" in html
        )
        assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in html
        assert "justify-self: center;" in html
        assert "line-height: 1;" in html


def test_image_parameter_labels_align_with_controls():
    html = WORKSPACE_PATHS[0].read_text(encoding="utf-8")

    assert "grid-template-rows: 16px 34px;" in html
    assert "padding: 0 10px;" in html
    assert "align-items: center;" in html
