"""统一飞书凭证加载器。

设计目标：
- **single source of truth**：飞书 app_id / app_secret 只在一个 yaml 里维护
- 主读：``data/feishu_whitelist.yaml``（推荐）
- 兼容：旧 ``nas_sync/config.yaml`` 的 ``feishu.app_id / app_secret`` 字段
  （让老 feishu_sync.py 在迁移过渡期不报"凭证缺失"）
- 凭证缺失不抛异常 → 上层 plugin 退到 v0 / disabled，业务流程不中断

为什么 dc_engines/feishu_reader/whitelist.py 已经有 _credentials，我们还要再写一个？

- ``feishu_reader.whitelist`` 只读 `data/feishu_whitelist.yaml`，不知道
  nas_sync 那边可能也填了凭证。hub 做"两边都看一眼"的合并兜底。
- ``feishu_reader.whitelist`` 是为白名单文档查询设计的，hub 凭证 loader
  更通用，没有"document/table/folder"那些业务字段。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(slots=True, frozen=True)
class FeishuCredentials:
    """飞书 app 凭证 + 启用开关。"""

    app_id: str
    app_secret: str
    enable: bool = True
    source: str = ""  # 调试用：从哪个 yaml 加载的


# 默认 lookup 顺序（前者优先）
_DEFAULT_PATHS = (
    "data/feishu_whitelist.yaml",
    "nas_sync/config.yaml",  # 老 nas_sync 用的
)


def _read_yaml(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _expand_env(val: str) -> str:
    """支持 ``${VAR}`` / ``$VAR`` 展开。未设置的 env var 返空串（让上层判失败）。"""
    expanded = os.path.expandvars(val)
    # 未展开的占位（如 ${FOO} 但 FOO 没设）会原样保留 → 视为空
    if "$" in expanded and ("${" in expanded or expanded.startswith("$")):
        return ""
    return expanded


def _extract(raw: dict) -> tuple[str, str, bool] | None:
    fs = raw.get("feishu") or {}
    if not isinstance(fs, dict):
        return None
    app_id = _expand_env(str(fs.get("app_id") or "").strip())
    app_secret = _expand_env(str(fs.get("app_secret") or "").strip())
    if not app_id or not app_secret:
        return None
    enable = bool(fs.get("enable", True))
    return app_id, app_secret, enable


def load_credentials(
    *,
    repo_root: str | Path | None = None,
    explicit_paths: list[str | Path] | None = None,
) -> FeishuCredentials | None:
    """从 yaml 加载飞书凭证。

    Args:
        repo_root: DC-Agent 根目录，默认从环境变量 ``DC_AGENT_ROOT`` 读，
            没设的话回退到当前工作目录。
        explicit_paths: 显式覆盖 lookup 顺序（测试用）。

    Returns:
        ``FeishuCredentials`` 或 ``None``（所有 yaml 都没有有效凭证）。
        永不抛异常。
    """
    if repo_root is None:
        repo_root = os.environ.get("DC_AGENT_ROOT") or "/Users/dianchi/DC-Agent"
    repo = Path(repo_root)

    paths = explicit_paths or _DEFAULT_PATHS
    for p in paths:
        full = repo / p if not Path(p).is_absolute() else Path(p)
        raw = _read_yaml(full)
        if raw is None:
            continue
        extracted = _extract(raw)
        if extracted is None:
            continue
        app_id, app_secret, enable = extracted
        return FeishuCredentials(
            app_id=app_id,
            app_secret=app_secret,
            enable=enable,
            source=str(full),
        )
    return None
