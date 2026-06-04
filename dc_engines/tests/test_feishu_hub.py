"""feishu_hub 单元测试（不需要真飞书凭证）。

覆盖：
- credentials.py 4 种场景：主 yaml 有 / fallback yaml 有 / 都没有 / yaml malformed
- client.py：单例、reset、record_call 计数、disabled 时 is_enabled=False
- 不测真飞书 API 调用（那是 e2e，等凭证齐了再做）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dc_engines.feishu_hub import (
    FeishuHub,
    HubStats,
    get_credentials,
    get_hub,
    is_enabled,
)
from dc_engines.feishu_hub.credentials import load_credentials


# ────────────────────────── credentials 加载 ──────────────────────────


def _write_yaml(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_credentials_main_yaml_only(tmp_path: Path) -> None:
    """主 yaml 有凭证 → 加载成功。"""
    _write_yaml(
        tmp_path / "data/feishu_whitelist.yaml",
        "feishu:\n  app_id: cli_main\n  app_secret: secret_main\n  enable: true\n",
    )
    creds = load_credentials(repo_root=tmp_path)
    assert creds is not None
    assert creds.app_id == "cli_main"
    assert creds.app_secret == "secret_main"
    assert creds.enable is True
    assert "feishu_whitelist.yaml" in creds.source


def test_credentials_fallback_to_nas_sync(tmp_path: Path) -> None:
    """主 yaml 没凭证，nas_sync/config.yaml 有 → 回退到 nas_sync。"""
    _write_yaml(tmp_path / "data/feishu_whitelist.yaml", "documents: []\n")  # 无 feishu 段
    _write_yaml(
        tmp_path / "nas_sync/config.yaml",
        "feishu:\n  app_id: cli_nas\n  app_secret: secret_nas\n",
    )
    creds = load_credentials(repo_root=tmp_path)
    assert creds is not None
    assert creds.app_id == "cli_nas"
    assert "nas_sync/config.yaml" in creds.source


def test_credentials_none_when_all_missing(tmp_path: Path) -> None:
    """两个 yaml 都没有 → None（不抛异常）。"""
    creds = load_credentials(repo_root=tmp_path)
    assert creds is None


def test_credentials_malformed_yaml_returns_none(tmp_path: Path) -> None:
    """yaml 解析失败 → None，不抛。"""
    _write_yaml(tmp_path / "data/feishu_whitelist.yaml", "this is not: valid: yaml: !!")
    creds = load_credentials(repo_root=tmp_path)
    # 无效 yaml 不 crash，返 None 让上层走 disabled
    assert creds is None


def test_credentials_disable_flag(tmp_path: Path) -> None:
    """enable: false → 凭证字段对但 enable=False。"""
    _write_yaml(
        tmp_path / "data/feishu_whitelist.yaml",
        "feishu:\n  app_id: cli_x\n  app_secret: secret_x\n  enable: false\n",
    )
    creds = load_credentials(repo_root=tmp_path)
    assert creds is not None
    assert creds.enable is False


def test_credentials_explicit_paths_override(tmp_path: Path) -> None:
    """显式 explicit_paths 覆盖默认 lookup 顺序。"""
    custom = tmp_path / "custom_feishu.yaml"
    _write_yaml(custom, "feishu:\n  app_id: cli_custom\n  app_secret: secret_c\n")
    creds = load_credentials(
        repo_root=tmp_path,
        explicit_paths=[custom],
    )
    assert creds is not None
    assert creds.app_id == "cli_custom"


# ────────────────────────── FeishuHub 单例 ──────────────────────────


@pytest.fixture(autouse=True)
def _reset_hub_singleton():
    """每个 test 前后清理单例，保证测试隔离。"""
    FeishuHub.reset_for_test()
    yield
    FeishuHub.reset_for_test()


def test_hub_is_singleton() -> None:
    """两次 get_hub() 返同一个对象。"""
    h1 = get_hub()
    h2 = get_hub()
    assert h1 is h2


def test_hub_disabled_when_no_credentials(monkeypatch, tmp_path: Path) -> None:
    """凭证缺失场景：is_enabled=False，client/credentials=None，但 get_hub 仍能拿到。"""
    monkeypatch.setenv("DC_AGENT_ROOT", str(tmp_path))  # 指向空目录
    hub = get_hub()
    assert hub is not None
    assert is_enabled() is False
    assert hub.client is None
    assert get_credentials() is None


def test_hub_stats_record_success(monkeypatch, tmp_path: Path) -> None:
    """record_call 成功路径计数。"""
    monkeypatch.setenv("DC_AGENT_ROOT", str(tmp_path))
    hub = get_hub()
    hub.record_call("docx.document.get")
    hub.record_call("docx.document.get")
    hub.record_call("contact.user.list")
    snap = hub.stats.snapshot()
    assert snap["total_calls"] == 3
    assert snap["total_errors"] == 0
    assert snap["error_rate"] == 0.0
    assert snap["calls_by_method"]["docx.document.get"] == 2
    assert snap["calls_by_method"]["contact.user.list"] == 1


def test_hub_stats_record_failure(monkeypatch, tmp_path: Path) -> None:
    """record_call 失败路径计数 + last_error 信息。"""
    monkeypatch.setenv("DC_AGENT_ROOT", str(tmp_path))
    hub = get_hub()
    hub.record_call("drive.file.list")
    hub.record_call("drive.file.list", error=TimeoutError("飞书超时"))
    snap = hub.stats.snapshot()
    assert snap["total_calls"] == 2
    assert snap["total_errors"] == 1
    assert snap["error_rate"] == 0.5
    assert "TimeoutError" in snap["last_error"]
    assert snap["errors_by_method"]["drive.file.list"] == 1


def test_hub_stats_snapshot_shape() -> None:
    """snapshot() 返的 dict 字段齐全（看门狗 / dashboard 消费这个结构）。"""
    stats = HubStats()
    snap = stats.snapshot()
    required_keys = {
        "started_at",
        "uptime_seconds",
        "total_calls",
        "total_errors",
        "error_rate",
        "calls_by_method",
        "errors_by_method",
        "last_error",
        "last_error_at",
    }
    assert required_keys.issubset(snap.keys())
