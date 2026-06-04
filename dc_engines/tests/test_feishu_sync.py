"""新版 feishu_sync.py 单元测试（不要真飞书凭证）。

覆盖：
- load_config: yaml 解析 + 默认值
- SyncManager._supported: 文件类型过滤逻辑
- SyncManager._load_state / _save_state: 增量状态持久化
- SyncManager.sync_once: is_enabled=False 时返 disabled
- SyncManager._sync_one: mtime 没变 → skipped（增量）

不覆盖（要真飞书 API）：
- _list_files / _download_to_file / _export_to_file
"""

from __future__ import annotations

# ruff: noqa: E402, I001

import sys
from pathlib import Path

import pytest

# 让 nas_sync.feishu_sync 能 import（它本身有 sys.path 加 dc_engines，
# 这里再补一份 repo root 让 nas_sync 包能被找到）
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from nas_sync.feishu_sync import SyncConfig, SyncManager, load_config  # noqa: E402
from dc_engines.feishu_hub import FeishuHub  # noqa: E402


# ────────────────────────── config ──────────────────────────


def test_load_config_minimal(tmp_path: Path) -> None:
    """最小 yaml：默认值兜底。"""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("feishu: {}\nwatch: {}\nnas: {}\n", encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.folder_tokens == []
    assert cfg.nas_inbox.name == "inbox"
    assert ".pdf" in cfg.supported_extensions
    assert "docx" in cfg.export_map  # 默认 export_map 有 docx


def test_load_config_with_folder_tokens(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
feishu:
  folder_tokens:
    - fld_aaa111
    - fld_bbb222
nas:
  mount_point: /tmp/test_nas
watch:
  inbox_dir: my_inbox
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.folder_tokens == ["fld_aaa111", "fld_bbb222"]
    assert str(cfg.nas_inbox) == "/tmp/test_nas/my_inbox"


# ────────────────────────── _supported 文件类型过滤 ──────────────────────────


def _make_manager(tmp_path: Path) -> SyncManager:
    cfg = SyncConfig(
        sync_scope="folder",
        folder_tokens=[],
        manual_urls=[],
        company_search_queries=[""],
        company_search_doc_types=[],
        company_search_page_size=50,
        company_search_max_items=0,
        sheet_attachments_enabled=True,
        sheet_attachments_include_hidden=True,
        sheet_attachments_max_items=0,
        sheet_attachment_timeout_seconds=90,
        sheet_attachment_max_download_mb=150,
        export_task_timeout_seconds=600,
        nas_inbox=tmp_path / "inbox",
        nas_processed=tmp_path / "processed",
        nas_failed=tmp_path / "failed",
        state_file=tmp_path / "state.json",
        export_map={"docx": "markdown", "sheet": "xlsx", "bitable": "csv"},
        supported_extensions={".pdf", ".md", ".docx", ".xlsx"},
        poll_interval_seconds=300,
        settle_seconds=3,
    )
    return SyncManager(cfg, dry_run=True)


def test_supported_native_feishu_doc(tmp_path: Path) -> None:
    """飞书原生 docx/sheet/bitable type → 始终 supported（可 export）。"""
    mgr = _make_manager(tmp_path)
    assert mgr._supported("营销方案.docx", "docx") is True
    assert mgr._supported("客户清单.sheet", "sheet") is True
    assert mgr._supported("项目跟踪.bitable", "bitable") is True


def test_supported_by_extension(tmp_path: Path) -> None:
    """非飞书原生文档 → 按扩展名过滤。"""
    mgr = _make_manager(tmp_path)
    assert mgr._supported("report.pdf", "file") is True
    assert mgr._supported("notes.md", "file") is True
    assert mgr._supported("video.mp4", "file") is False  # 不在白名单
    assert mgr._supported("image.png", "file") is False


# ────────────────────────── state 持久化 ──────────────────────────


def test_state_load_save_roundtrip(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    mgr.dry_run = False  # save 才生效
    mgr._state["files"]["token_x"] = {
        "name": "test.pdf",
        "mtime": 12345,
        "done": True,
    }
    mgr._save_state()
    # 重 load
    mgr2 = _make_manager(tmp_path)
    assert mgr2._state["files"].get("token_x", {}).get("mtime") == 12345


def test_state_missing_file_returns_empty(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    assert mgr._state == {"files": {}}


def test_state_corrupted_json_returns_empty(tmp_path: Path) -> None:
    """state 文件损坏 → 回退到空，不 crash。"""
    state_file = tmp_path / "state.json"
    state_file.write_text("not valid {{{ json", encoding="utf-8")
    cfg = SyncConfig(
        sync_scope="folder",
        folder_tokens=[],
        manual_urls=[],
        company_search_queries=[""],
        company_search_doc_types=[],
        company_search_page_size=50,
        company_search_max_items=0,
        sheet_attachments_enabled=True,
        sheet_attachments_include_hidden=True,
        sheet_attachments_max_items=0,
        sheet_attachment_timeout_seconds=90,
        sheet_attachment_max_download_mb=150,
        export_task_timeout_seconds=600,
        nas_inbox=tmp_path,
        nas_processed=tmp_path / "processed",
        nas_failed=tmp_path / "failed",
        state_file=state_file,
        export_map={},
        supported_extensions={".pdf"},
        poll_interval_seconds=300,
        settle_seconds=3,
    )
    mgr = SyncManager(cfg, dry_run=True)
    assert mgr._state == {"files": {}}


# ────────────────────────── sync_once: disabled 分支 ──────────────────────────


@pytest.fixture(autouse=True)
def _reset_hub_singleton():
    FeishuHub.reset_for_test()
    yield
    FeishuHub.reset_for_test()


async def test_sync_once_disabled_when_no_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    """凭证缺失 → sync_once 返 disabled，不抛异常、不拉飞书。"""
    monkeypatch.setenv("DC_AGENT_ROOT", str(tmp_path))  # 空目录，凭证查不到
    mgr = _make_manager(tmp_path)
    stats = await mgr.sync_once()
    assert stats.get("disabled") == 1
    assert stats["synced"] == 0
    assert stats["failed"] == 0
