from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

ASSET_NAME = "dc-dashboard-quick-entries.js"
SCRIPT_MARKER = "data-dc-dashboard-quick-entries"


@dataclass(frozen=True)
class DashboardQuickEntriesResult:
    ok: bool
    changed: bool
    messages: tuple[str, ...]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_dist_path() -> Path:
    return _repo_root() / "data" / "dist"


def _default_source_path() -> Path:
    return _repo_root() / "data" / "plugins" / "system_entries" / ASSET_NAME


def _asset_version(source_bytes: bytes) -> str:
    return hashlib.sha256(source_bytes).hexdigest()[:12]


def _script_tag(asset_version: str) -> str:
    return (
        f'<script src="/assets/{ASSET_NAME}?v={asset_version}" '
        f"defer {SCRIPT_MARKER}></script>"
    )


def _script_tag_present(index_html: str, asset_version: str) -> bool:
    return _script_tag(asset_version) in index_html


def _patch_index_html(index_html: str, asset_version: str) -> tuple[str, bool]:
    tag = _script_tag(asset_version)
    if _script_tag_present(index_html, asset_version):
        return index_html, False

    cleaned_html = re.sub(
        r"\s*<script\b[^>]*\bdata-dc-dashboard-quick-entries\b[^>]*>\s*</script>\s*",
        "\n",
        index_html,
        flags=re.IGNORECASE,
    )

    if "</head>" not in cleaned_html:
        return cleaned_html.rstrip() + f"\n{tag}\n", True

    return cleaned_html.replace("</head>", f"    {tag}\n  </head>", 1), True


def install_dashboard_quick_entries(
    dist_path: str | Path | None = None,
    *,
    source_path: str | Path | None = None,
    check_only: bool = False,
) -> DashboardQuickEntriesResult:
    dist = Path(dist_path) if dist_path is not None else _default_dist_path()
    source = Path(source_path) if source_path is not None else _default_source_path()
    index_path = dist / "index.html"
    asset_path = dist / "assets" / ASSET_NAME

    messages: list[str] = []
    changed = False

    if not source.is_file():
        return DashboardQuickEntriesResult(
            ok=False,
            changed=False,
            messages=(f"source script not found: {source}",),
        )
    if not index_path.is_file():
        return DashboardQuickEntriesResult(
            ok=False,
            changed=False,
            messages=(f"dashboard index not found: {index_path}",),
        )

    source_bytes = source.read_bytes()
    asset_version = _asset_version(source_bytes)
    asset_ok = asset_path.is_file() and asset_path.read_bytes() == source_bytes
    index_html = index_path.read_text(encoding="utf-8")
    index_ok = _script_tag_present(index_html, asset_version)

    if check_only:
        if asset_ok and index_ok:
            return DashboardQuickEntriesResult(
                ok=True,
                changed=False,
                messages=("dashboard quick entries are installed",),
            )
        if not asset_ok:
            messages.append(f"asset missing or stale: {asset_path}")
        if not index_ok:
            messages.append(f"script tag missing: {index_path}")
        return DashboardQuickEntriesResult(
            ok=False,
            changed=False,
            messages=tuple(messages),
        )

    asset_path.parent.mkdir(parents=True, exist_ok=True)
    if not asset_ok:
        shutil.copyfile(source, asset_path)
        changed = True
        messages.append(f"installed asset: {asset_path}")

    patched_html, index_changed = _patch_index_html(index_html, asset_version)
    if index_changed:
        index_path.write_text(patched_html, encoding="utf-8")
        changed = True
        messages.append(f"patched dashboard index: {index_path}")

    if not messages:
        messages.append("dashboard quick entries already installed")

    return DashboardQuickEntriesResult(
        ok=True,
        changed=changed,
        messages=tuple(messages),
    )
