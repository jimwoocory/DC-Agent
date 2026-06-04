#!/usr/bin/env python3
"""飞书云空间 → NAS inbox 同步（v2，用 lark-oapi + feishu_hub）。

老版 ``feishu_sync.py.legacy``：466 行手写 raw HTTP + 自管 token
新版（本文件）：~230 行，凭证/限流/重试托给 feishu_hub，业务逻辑保持

用法：
    python feishu_sync.py            # 跑一次
    python feishu_sync.py --watch    # 持续模式（默认 5 分钟轮询一次）
    python feishu_sync.py --dry-run  # 只 list 不下载

迁移路径：
- 老版 ``FeishuClient`` (raw HTTP) 删了
- 老版 ``SyncManager`` 内部改用 ``dc_engines.feishu_hub.get_client()``
- 凭证来源：先看 ``data/feishu_whitelist.yaml``，回退 ``nas_sync/config.yaml``
- token 刷新、retry、调用统计：全 hub 接管
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import hashlib
import html
import json
import logging
import re
import sqlite3
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx
import yaml

# 确保 dc_engines 在 PYTHONPATH（兼容 venv 装 + 直接 python feishu_sync.py）
_DC_ROOT = Path(__file__).resolve().parent.parent
if str(_DC_ROOT / "dc_engines") not in sys.path:
    sys.path.insert(0, str(_DC_ROOT / "dc_engines"))

from dc_engines.feishu_hub import call, get_client, get_hub, is_enabled  # noqa: E402
from lark_oapi import AccessTokenType  # noqa: E402
from lark_oapi.api.drive.v1 import (  # noqa: E402
    CreateExportTaskRequest,
    DownloadExportTaskRequest,
    DownloadFileRequest,
    DownloadMediaRequest,
    ExportTask,
    GetExportTaskRequest,
    ListFileRequest,
)
from lark_oapi.api.sheets.v3 import QuerySpreadsheetSheetRequest  # noqa: E402
from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest  # noqa: E402
from lark_oapi.core.enum import HttpMethod  # noqa: E402
from lark_oapi.core.http.transport import _build_header, _build_url  # noqa: E402
from lark_oapi.core.model.base_request import BaseRequest  # noqa: E402
from lark_oapi.core.model.request_option import RequestOption  # noqa: E402
from lark_oapi.core.token.auth import verify  # noqa: E402

logger = logging.getLogger("feishu_sync")
DOWNLOAD_STAGING_DIR = _DC_ROOT / "data" / "staging" / "feishu_sync"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ────────────────────────── 配置 ──────────────────────────


@dataclass(slots=True)
class SyncConfig:
    # folder: 只同步配置的 folder_token；company_cloud: 发现公司云文档后同步
    sync_scope: str
    # 飞书：folder 模式下哪些 folder_token 要同步、本地保存到哪
    folder_tokens: list[str]
    # 手工指定的小范围文档 URL，适合 wiki/docx/sheet 灰度同步
    manual_urls: list[str]
    # company_cloud 模式下通过搜索接口发现企业云文档
    company_search_queries: list[str]
    company_search_doc_types: list[str]
    company_search_page_size: int
    company_search_max_items: int
    sheet_attachments_enabled: bool
    sheet_attachments_include_hidden: bool
    sheet_attachments_max_items: int
    sheet_attachment_timeout_seconds: int
    sheet_attachment_max_download_mb: int
    export_task_timeout_seconds: int
    nas_inbox: Path
    nas_processed: Path
    nas_failed: Path
    state_file: Path
    # 文件类型 → 导出格式（飞书 docx/sheet/bitable 必须 export，不能直接 download）
    export_map: dict[str, str]
    supported_extensions: set[str]
    poll_interval_seconds: int
    settle_seconds: int


def load_config(path: Path | str) -> SyncConfig:
    """从 nas_sync/config.yaml 加载同步配置（不含飞书凭证——凭证由 hub 管）。"""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    feishu_cfg = raw.get("feishu", {}) or {}
    company_cfg = feishu_cfg.get("company_cloud", {}) or {}
    sheet_attachment_cfg = feishu_cfg.get("sheet_attachments", {}) or {}
    sync_cfg = raw.get("sync", {}) or raw.get("watch", {}) or {}
    nas_cfg = raw.get("nas", {}) or {}

    mount = Path(nas_cfg.get("mount_point", "/Users/dianchi/nas_kb"))
    inbox = mount / sync_cfg.get("inbox_dir", "inbox")
    return SyncConfig(
        sync_scope=str(feishu_cfg.get("sync_scope", "folder")).strip() or "folder",
        folder_tokens=feishu_cfg.get("folder_tokens", []) or [],
        manual_urls=feishu_cfg.get("manual_urls", []) or [],
        company_search_queries=company_cfg.get("queries", [""]) or [""],
        company_search_doc_types=company_cfg.get("doc_types", []) or [],
        company_search_page_size=min(int(company_cfg.get("page_size", 20)), 20),
        company_search_max_items=int(company_cfg.get("max_items_per_run", 0)),
        sheet_attachments_enabled=bool(sheet_attachment_cfg.get("enabled", True)),
        sheet_attachments_include_hidden=bool(
            sheet_attachment_cfg.get("include_hidden_sheets", True)
        ),
        sheet_attachments_max_items=int(
            sheet_attachment_cfg.get("max_items_per_run", 0)
        ),
        sheet_attachment_timeout_seconds=int(
            sheet_attachment_cfg.get("timeout_seconds", 90)
        ),
        sheet_attachment_max_download_mb=int(
            sheet_attachment_cfg.get("max_download_mb", 150)
        ),
        export_task_timeout_seconds=int(
            sync_cfg.get("export_task_timeout_seconds", 600)
        ),
        nas_inbox=inbox,
        nas_processed=mount / sync_cfg.get("processed_dir", "processed"),
        nas_failed=mount / sync_cfg.get("failed_dir", "failed"),
        state_file=Path(__file__).parent / "logs" / "sync_state.json",
        export_map={
            "doc": "docx",
            "docx": "docx",
            "sheet": "xlsx",
            "bitable": "xlsx",
            "slides": "pptx",
        },
        supported_extensions={
            ext.lower()
            for ext in sync_cfg.get(
                "supported_extensions",
                [".pdf", ".md", ".txt", ".docx", ".xlsx", ".pptx", ".csv"],
            )
        },
        poll_interval_seconds=int(sync_cfg.get("poll_interval", 300)),
        settle_seconds=int(sync_cfg.get("settle_seconds", 3)),
    )


# ────────────────────────── 同步逻辑 ──────────────────────────


class SyncManager:
    """飞书 → NAS 同步主控。"""

    def __init__(self, cfg: SyncConfig, *, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self._state: dict[str, Any] = self._load_state()

    # ── 状态持久化（防重复下载）──────────────────────────────

    def _load_state(self) -> dict[str, Any]:
        if not self.cfg.state_file.exists():
            return {"files": {}}
        try:
            return json.loads(self.cfg.state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"files": {}}

    def _save_state(self) -> None:
        if self.dry_run:
            return
        self.cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_file.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _state_files(self) -> dict[str, Any]:
        files = self._state.setdefault("files", {})
        if not isinstance(files, dict):
            self._state["files"] = {}
            return self._state["files"]
        return files

    def _failed_path_for(self, path: Path) -> Path | None:
        if not str(path):
            return None
        try:
            rel = path.relative_to(self.cfg.nas_inbox)
        except ValueError:
            rel = Path(path.name)
        return self.cfg.nas_failed / rel

    def _path_is_quarantined(self, path: Path) -> bool:
        failed_path = self._failed_path_for(path)
        if failed_path is None:
            return False
        return (
            failed_path.is_file()
            or failed_path.with_name(f"{failed_path.name}.error.txt").is_file()
        )

    def _path_exists_or_indexed(self, path: Path) -> bool:
        if path.is_file():
            return True
        db_path = _DC_ROOT / "data" / "nas_memory.db"
        if not str(path) or not db_path.is_file():
            return False
        try:
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT archive_path
                    FROM documents
                    WHERE source_path = ?
                    ORDER BY indexed_at DESC
                    LIMIT 1
                    """,
                    (str(path),),
                ).fetchone()
        except sqlite3.Error:
            return False
        if not row:
            return False
        return Path(str(row[0] or "")).is_file()

    # ── 主入口 ──────────────────────────────────────────

    async def sync_once(self) -> dict[str, int]:
        """跑一遍同步。返 stats dict。"""
        if not is_enabled():
            logger.warning("飞书凭证未配置 → 跳过同步")
            return {"synced": 0, "skipped": 0, "failed": 0, "disabled": 1}

        client = get_client()
        assert client is not None  # is_enabled 已保护

        stats: dict[str, int] = {"synced": 0, "skipped": 0, "failed": 0}
        self.cfg.nas_inbox.mkdir(parents=True, exist_ok=True)

        if self.cfg.sync_scope == "company_cloud":
            try:
                files = await self._discover_company_cloud_files(client)
            except Exception as exc:  # noqa: BLE001
                logger.warning("公司云文档发现失败: %s", exc)
                stats["failed"] += 1
                return stats

            for f in files:
                outcome = await self._sync_one(client, f)
                stats[outcome] = stats.get(outcome, 0) + 1
                self._merge_stats(
                    stats, await self._sync_related_files(client, f, outcome)
                )

            self._save_state()
            return stats

        if self.cfg.sync_scope != "folder":
            logger.warning(
                "未知 sync_scope=%s（支持: folder/company_cloud）", self.cfg.sync_scope
            )
            stats["failed"] += 1
            return stats

        if not self.cfg.folder_tokens:
            logger.warning("folder 模式未配置 folder_tokens，未执行同步")

        for folder_token in self.cfg.folder_tokens:
            try:
                files = await self._list_files(client, folder_token)
            except Exception as exc:  # noqa: BLE001
                logger.warning("list_files folder=%s 失败: %s", folder_token, exc)
                stats["failed"] += 1
                continue

            for f in files:
                outcome = await self._sync_one(client, f)
                stats[outcome] = stats.get(outcome, 0) + 1
                self._merge_stats(
                    stats, await self._sync_related_files(client, f, outcome)
                )

        self._merge_stats(stats, await self._sync_manual_urls_with_client(client))
        self._save_state()
        return stats

    async def preflight(self) -> dict[str, Any]:
        """只读预检：确认当前配置和飞书权限是否足够跑同步。"""
        if not is_enabled():
            return {"ok": False, "reason": "飞书凭证未配置"}
        client = get_client()
        assert client is not None

        if self.cfg.sync_scope == "company_cloud":
            files = await self._discover_company_cloud_files(client, limit=1)
            return {
                "ok": True,
                "scope": "company_cloud",
                "sample_count": len(files),
                "permission": "search:docs:read",
            }

        if self.cfg.sync_scope == "folder":
            if not self.cfg.folder_tokens and not self.cfg.manual_urls:
                return {
                    "ok": False,
                    "reason": "folder 模式未配置 folder_tokens/manual_urls",
                }
            files = []
            if self.cfg.folder_tokens:
                files = await self._list_files(client, self.cfg.folder_tokens[0])
            return {
                "ok": True,
                "scope": "folder",
                "folder_tokens": self.cfg.folder_tokens,
                "manual_urls": self.cfg.manual_urls,
                "sample_count": len(files),
            }

        return {"ok": False, "reason": f"未知 sync_scope={self.cfg.sync_scope}"}

    async def sync_urls(self, urls: list[str]) -> dict[str, int]:
        """同步手工指定的飞书云文档 URL，绕过 company_cloud/folder 发现逻辑。"""
        if not is_enabled():
            logger.warning("飞书凭证未配置 → 跳过同步")
            return {"synced": 0, "skipped": 0, "failed": 0, "disabled": 1}

        client = get_client()
        assert client is not None

        stats: dict[str, int] = {"synced": 0, "skipped": 0, "failed": 0}
        self.cfg.nas_inbox.mkdir(parents=True, exist_ok=True)
        self._merge_stats(stats, await self._sync_manual_urls_with_client(client, urls))
        self._save_state()
        return stats

    async def sync_file_infos(self, file_infos: list[dict[str, Any]]) -> dict[str, int]:
        """同步已知 token/type/name 的后台云文档条目。"""
        if not is_enabled():
            logger.warning("飞书凭证未配置 → 跳过同步")
            return {"synced": 0, "skipped": 0, "failed": 0, "disabled": 1}

        client = get_client()
        assert client is not None

        stats: dict[str, int] = {"synced": 0, "skipped": 0, "failed": 0}
        self.cfg.nas_inbox.mkdir(parents=True, exist_ok=True)
        for file_info in file_infos:
            outcome = await self._sync_one(client, file_info)
            stats[outcome] = stats.get(outcome, 0) + 1
            self._merge_stats(
                stats, await self._sync_related_files(client, file_info, outcome)
            )
        self._save_state()
        return stats

    async def _sync_manual_urls_with_client(
        self,
        client,
        urls: list[str] | None = None,
    ) -> dict[str, int]:
        stats: dict[str, int] = {}
        for url in urls if urls is not None else self.cfg.manual_urls:
            try:
                file_info = await self._file_info_from_url(client, url)
                outcome = await self._sync_one(client, file_info)
                related = await self._sync_related_files(client, file_info, outcome)
            except Exception as exc:  # noqa: BLE001
                logger.warning("同步 URL %s 失败: %s", url, exc)
                outcome = "failed"
                related = {}
            stats[outcome] = stats.get(outcome, 0) + 1
            self._merge_stats(stats, related)
        return stats

    @staticmethod
    def _merge_stats(stats: dict[str, int], extra: dict[str, int]) -> None:
        for key, value in extra.items():
            stats[key] = stats.get(key, 0) + value

    async def watch(self) -> None:
        """持续同步模式（每 poll_interval 秒跑一次）。"""
        logger.info(
            "进入持续模式，每 %ds 同步一次（Ctrl+C 退出）",
            self.cfg.poll_interval_seconds,
        )
        while True:
            try:
                stats = await self.sync_once()
                logger.info("一轮同步完成: %s", stats)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("同步异常（继续下一轮）")
            await asyncio.sleep(self.cfg.poll_interval_seconds)

    # ── 单文件同步 ─────────────────────────────────────

    async def _normalize_file_info(self, client, file_info: dict) -> dict:
        if file_info.get("type") == "wiki" or (
            file_info.get("wiki_token") and not file_info.get("token")
        ):
            return await self._resolve_wiki_file_info(
                client,
                str(file_info.get("wiki_token") or file_info.get("token") or ""),
                url=str(file_info.get("url") or ""),
                fallback_name=str(file_info.get("name") or ""),
            )
        return file_info

    async def _sync_related_files(
        self,
        client,
        file_info: dict,
        parent_outcome: str,
    ) -> dict[str, int]:
        if parent_outcome == "failed" or not self.cfg.sheet_attachments_enabled:
            return {}

        try:
            normalized = await self._normalize_file_info(client, file_info)
        except Exception as exc:  # noqa: BLE001
            logger.warning("解析关联文件失败: %s", exc)
            return {"attachment_failed": 1}

        if normalized.get("type") != "sheet":
            return {}

        try:
            return await self._sync_spreadsheet_attachments(client, normalized)
        except Exception as exc:  # noqa: BLE001
            logger.warning("同步表格附件失败: %s", exc)
            return {"attachment_failed": 1}

    async def _file_info_from_url(self, client, url: str) -> dict[str, Any]:
        parsed = self._parse_feishu_url(url)
        if not parsed:
            raise ValueError(f"不支持的飞书云文档 URL: {url}")

        ftype, token = parsed
        if ftype == "wiki":
            return await self._resolve_wiki_file_info(client, token, url=url)

        return {
            "token": token,
            "name": token,
            "type": ftype,
            "modified_time": 0,
            "source": "manual_url",
            "url": url,
        }

    @staticmethod
    def _parse_feishu_url(url: str) -> tuple[str, str] | None:
        path = urlparse(url).path
        for prefix, ftype in (
            ("/wiki/", "wiki"),
            ("/docx/", "docx"),
            ("/docs/", "doc"),
            ("/doc/", "doc"),
            ("/sheets/", "sheet"),
            ("/sheet/", "sheet"),
            ("/base/", "bitable"),
            ("/file/", "file"),
        ):
            if prefix not in path:
                continue
            token = path.split(prefix, 1)[1].split("/", 1)[0]
            return (ftype, token) if token else None
        return None

    async def _resolve_wiki_file_info(
        self,
        client,
        wiki_token: str,
        *,
        url: str = "",
        fallback_name: str = "",
    ) -> dict[str, Any]:
        req = GetNodeSpaceRequest.builder().token(wiki_token).build()
        resp = await call("wiki.space.get_node", client.wiki.v2.space.aget_node(req))
        if not resp.success() or not resp.data or not resp.data.node:
            raise RuntimeError(
                f"resolve wiki node failed code={getattr(resp, 'code', '?')} "
                f"msg={getattr(resp, 'msg', '?')}"
            )

        node = resp.data.node
        name = self._clean_title(str(node.title or fallback_name or node.obj_token))
        return {
            "token": str(node.obj_token or ""),
            "name": name,
            "type": str(node.obj_type or ""),
            "modified_time": int(node.obj_edit_time or node.node_create_time or 0),
            "source": "wiki",
            "url": url,
            "wiki_token": str(node.node_token or wiki_token),
        }

    async def _sync_spreadsheet_attachments(
        self,
        client,
        spreadsheet_info: dict,
    ) -> dict[str, int]:
        attachments = self._discover_spreadsheet_attachments(client, spreadsheet_info)
        state_files = self._state_files()
        pending_attachments = [
            attachment
            for attachment in attachments
            if not (
                (state := state_files.get(str(attachment.get("token") or ""), {}))
                and state.get("done")
                and (
                    self._path_exists_or_indexed(
                        Path(str(state.get("local_path") or ""))
                    )
                    or self._path_is_quarantined(
                        Path(str(state.get("local_path") or ""))
                    )
                    or state.get("failed")
                )
            )
        ]
        sync_targets = pending_attachments
        if self.cfg.sheet_attachments_max_items > 0:
            sync_targets = pending_attachments[: self.cfg.sheet_attachments_max_items]

        stats: dict[str, int] = {
            "attachment_synced": 0,
            "attachment_skipped": 0,
            "attachment_failed": 0,
        }
        outcomes: dict[str, str] = {}
        for attachment in sync_targets:
            token = str(attachment.get("token") or "")
            size_mb = int(attachment.get("size") or 0) / 1024 / 1024
            if (
                self.cfg.sheet_attachment_max_download_mb > 0
                and size_mb > self.cfg.sheet_attachment_max_download_mb
            ):
                outcome = "failed"
                self._mark_attachment_failed(attachment, "attachment_too_large")
                logger.warning(
                    "同步附件超过大小阈值 %.2fMB > %dMB，已标记失败并跳过: %s",
                    size_mb,
                    self.cfg.sheet_attachment_max_download_mb,
                    attachment.get("name") or token,
                )
            else:
                try:
                    outcome = await asyncio.wait_for(
                        self._sync_one(client, attachment),
                        timeout=max(1, self.cfg.sheet_attachment_timeout_seconds),
                    )
                except TimeoutError:
                    outcome = "failed"
                    self._mark_attachment_failed(attachment, "attachment_sync_timeout")
                    logger.warning(
                        "同步附件超时，已标记失败并跳过后续重试: %s",
                        attachment.get("name") or token,
                    )
                if outcome == "failed":
                    self._mark_attachment_failed(attachment, "attachment_sync_failed")
            outcomes[token] = outcome
            stats[f"attachment_{outcome}"] = stats.get(f"attachment_{outcome}", 0) + 1

        self._write_spreadsheet_attachment_index(
            spreadsheet_info,
            attachments,
            outcomes,
        )
        self._write_local_spreadsheet_links(spreadsheet_info, attachments)
        if attachments:
            logger.info(
                "表格附件同步完成: %s，共 %d 个发现附件，本轮处理 %d 个",
                stats,
                len(attachments),
                len(sync_targets),
            )
        return stats

    def _mark_attachment_failed(self, attachment: dict[str, Any], reason: str) -> None:
        token = str(attachment.get("token") or "")
        if not token:
            return
        target_dir = Path(str(attachment.get("target_dir") or self.cfg.nas_inbox))
        local_path = target_dir / str(attachment.get("name") or token)
        self._state_files()[token] = {
            "name": attachment.get("name") or token,
            "type": attachment.get("type") or "attachment",
            "mtime": int(attachment.get("modified_time") or 0),
            "local_path": str(local_path),
            "done": True,
            "failed": True,
            "failed_reason": reason,
            "failed_at": int(time.time()),
            "source": attachment.get("source") or "sheet_attachment",
            "spreadsheet_token": attachment.get("spreadsheet_token") or "",
            "spreadsheet_name": attachment.get("spreadsheet_name") or "",
            "sheet_title": attachment.get("sheet_title") or "",
            "cell": attachment.get("cell") or "",
        }

    def _discover_spreadsheet_attachments(
        self,
        client,
        spreadsheet_info: dict,
    ) -> list[dict]:
        spreadsheet_token = str(spreadsheet_info.get("token") or "")
        spreadsheet_name = self._safe_path_part(
            str(spreadsheet_info.get("name") or "sheet")
        )
        req = (
            QuerySpreadsheetSheetRequest.builder()
            .spreadsheet_token(spreadsheet_token)
            .build()
        )
        resp = client.sheets.v3.spreadsheet_sheet.query(req)
        if not resp.success() or not resp.data:
            raise RuntimeError(
                f"query spreadsheet sheets failed code={getattr(resp, 'code', '?')} "
                f"msg={getattr(resp, 'msg', '?')}"
            )

        seen: set[str] = set()
        attachments: list[dict] = []
        base_dir = self.cfg.nas_inbox / f"{spreadsheet_name}__attachments"

        for sheet in resp.data.sheets or []:
            if sheet.hidden and not self.cfg.sheet_attachments_include_hidden:
                continue

            sheet_id = str(sheet.sheet_id or "")
            sheet_title = self._safe_path_part(str(sheet.title or sheet_id or "sheet"))
            row_count = int(getattr(sheet.grid_properties, "row_count", 0) or 0)
            column_count = int(getattr(sheet.grid_properties, "column_count", 0) or 0)
            if not sheet_id or row_count <= 0 or column_count <= 0:
                continue

            headers = self._read_spreadsheet_header(
                client, spreadsheet_token, sheet_id, column_count
            )
            for start_row in range(1, row_count + 1, 500):
                end_row = min(start_row + 499, row_count)
                values = self._read_spreadsheet_values(
                    client,
                    spreadsheet_token,
                    f"{sheet_id}!A{start_row}:{self._column_name(column_count)}{end_row}",
                )
                for row_offset, row in enumerate(values):
                    row_index = start_row + row_offset
                    row_summary = self._row_summary(row)
                    for col_index, cell in enumerate(row, start=1):
                        for attachment in self._extract_cell_attachments(cell):
                            token = str(
                                attachment.get("fileToken")
                                or attachment.get("file_token")
                                or ""
                            )
                            if not token or token in seen:
                                continue
                            seen.add(token)

                            header = self._safe_path_part(
                                headers.get(col_index) or self._column_name(col_index)
                            )
                            name = self._attachment_name(attachment, token)
                            target_dir = base_dir / sheet_title / header
                            attachments.append(
                                {
                                    "token": token,
                                    "name": f"{row_index:04d}_{name}",
                                    "type": "attachment",
                                    "modified_time": int(attachment.get("size") or 0),
                                    "size": int(attachment.get("size") or 0),
                                    "mime_type": str(attachment.get("mimeType") or ""),
                                    "source": "sheet_attachment",
                                    "spreadsheet_token": spreadsheet_token,
                                    "spreadsheet_name": spreadsheet_name,
                                    "sheet_id": sheet_id,
                                    "sheet_title": sheet_title,
                                    "cell": f"{self._column_name(col_index)}{row_index}",
                                    "row_index": row_index,
                                    "column_title": header,
                                    "row_summary": row_summary,
                                    "target_dir": str(target_dir),
                                }
                            )

        logger.info("发现表格附件: %d 个", len(attachments))
        return attachments

    def _write_spreadsheet_attachment_index(
        self,
        spreadsheet_info: dict,
        attachments: list[dict],
        outcomes: dict[str, str],
    ) -> None:
        if self.dry_run or not attachments:
            return

        spreadsheet_name = self._safe_path_part(
            str(spreadsheet_info.get("name") or "sheet")
        )
        base_dir = self.cfg.nas_inbox / f"{spreadsheet_name}__attachments"
        base_dir.mkdir(parents=True, exist_ok=True)
        sync_dir = base_dir / ".sync"
        sync_dir.mkdir(parents=True, exist_ok=True)
        index_path = sync_dir / "attachments_manifest.csv"

        fieldnames = [
            "status",
            "spreadsheet_name",
            "sheet_title",
            "row_index",
            "cell",
            "column_title",
            "attachment_name",
            "mime_type",
            "size_bytes",
            "size_mb",
            "local_path",
            "file_token",
            "row_summary",
        ]
        state_files = self._state_files()
        with index_path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for attachment in attachments:
                token = str(attachment.get("token") or "")
                state = state_files.get(token, {}) if token else {}
                predicted_path = str(
                    Path(str(attachment.get("target_dir") or self.cfg.nas_inbox))
                    / str(attachment.get("name") or token)
                )
                local_path = str(state.get("local_path") or predicted_path)
                if token in outcomes:
                    status = outcomes[token]
                elif state.get("failed"):
                    status = "failed"
                elif state.get("done") and Path(local_path).exists():
                    status = "synced"
                elif state.get("done"):
                    status = "recorded_missing"
                else:
                    status = "pending"

                size = int(attachment.get("size") or 0)
                writer.writerow(
                    {
                        "status": status,
                        "spreadsheet_name": attachment.get("spreadsheet_name")
                        or spreadsheet_name,
                        "sheet_title": attachment.get("sheet_title") or "",
                        "row_index": attachment.get("row_index") or "",
                        "cell": attachment.get("cell") or "",
                        "column_title": attachment.get("column_title") or "",
                        "attachment_name": attachment.get("name") or "",
                        "mime_type": attachment.get("mime_type") or "",
                        "size_bytes": size,
                        "size_mb": f"{size / 1024 / 1024:.2f}" if size else "",
                        "local_path": local_path,
                        "file_token": token,
                        "row_summary": attachment.get("row_summary") or "",
                    }
                )
        logger.info("附件索引已写入: %s", index_path)

    def _write_local_spreadsheet_links(
        self,
        spreadsheet_info: dict,
        attachments: list[dict],
    ) -> None:
        if self.dry_run or not attachments:
            return

        spreadsheet_token = str(spreadsheet_info.get("token") or "")
        state = self._state_files().get(spreadsheet_token, {})
        workbook_path = Path(str(state.get("local_path") or ""))
        if not workbook_path.exists() or workbook_path.suffix.lower() != ".xlsx":
            return

        cell_links: dict[tuple[str, str], str] = {}
        workbook_dir = workbook_path.parent
        state_files = self._state_files()
        for attachment in attachments:
            token = str(attachment.get("token") or "")
            attachment_state = state_files.get(token, {}) if token else {}
            if not attachment_state.get("done"):
                continue
            local_path = Path(str(attachment_state.get("local_path") or ""))
            if not local_path.is_file():
                continue
            sheet_title = str(attachment.get("sheet_title") or "")
            cell = str(attachment.get("cell") or "")
            if not sheet_title or not cell:
                continue
            cell_links[(sheet_title, cell)] = Path(
                self._relative_posix(workbook_dir, local_path)
            ).as_posix()

        if not cell_links:
            return

        self._patch_xlsx_hyperlinks(workbook_path, cell_links)
        logger.info(
            "主表本地附件链接已更新: %s（%d 个链接）", workbook_path, len(cell_links)
        )

    @staticmethod
    def _relative_posix(base_dir: Path, target: Path) -> str:
        try:
            rel = target.relative_to(base_dir)
        except ValueError:
            rel = Path("../") / target
        return rel.as_posix()

    @classmethod
    def _patch_xlsx_hyperlinks(
        cls,
        workbook_path: Path,
        cell_links: dict[tuple[str, str], str],
    ) -> None:
        spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        relationships_ns = (
            "http://schemas.openxmlformats.org/package/2006/relationships"
        )
        office_rel_ns = (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        )
        hyperlink_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
        ET.register_namespace("", spreadsheet_ns)
        ET.register_namespace("r", office_rel_ns)

        tmp_path = workbook_path.with_name(f"{workbook_path.name}.tmp-links")
        tmp_path.unlink(missing_ok=True)
        try:
            with zipfile.ZipFile(workbook_path, "r") as src:
                workbook_root = ET.fromstring(src.read("xl/workbook.xml"))
                rels_root = ET.fromstring(src.read("xl/_rels/workbook.xml.rels"))
                workbook_rels = {
                    rel.attrib["Id"]: rel.attrib["Target"]
                    for rel in rels_root.findall(f"{{{relationships_ns}}}Relationship")
                }

                sheet_paths: dict[str, str] = {}
                for sheet in workbook_root.findall(f".//{{{spreadsheet_ns}}}sheet"):
                    title = sheet.attrib.get("name", "")
                    rid = sheet.attrib.get(f"{{{office_rel_ns}}}id", "")
                    target = workbook_rels.get(rid)
                    if not target:
                        continue
                    sheet_paths[title] = (
                        f"xl/{target.lstrip('/')}"
                        if not target.startswith("xl/")
                        else target
                    )

                updates: dict[str, dict[str, str]] = {}
                for (sheet_title, cell), target in cell_links.items():
                    sheet_path = sheet_paths.get(sheet_title)
                    if not sheet_path:
                        continue
                    updates.setdefault(sheet_path, {})[cell] = target
                rel_paths_to_update = {
                    cls._sheet_rels_path(sheet_path) for sheet_path in updates
                }

                with zipfile.ZipFile(
                    tmp_path, "w", compression=zipfile.ZIP_DEFLATED
                ) as dst:
                    written: set[str] = set()
                    for info in src.infolist():
                        if (
                            info.filename in rel_paths_to_update
                            and info.filename not in written
                        ):
                            continue
                        if info.filename in updates:
                            data = cls._patched_sheet_xml(
                                src.read(info.filename),
                                updates[info.filename],
                                info.filename,
                                src,
                                spreadsheet_ns,
                                relationships_ns,
                                office_rel_ns,
                                hyperlink_type,
                            )
                            dst.writestr(copy.copy(info), data)
                            written.add(info.filename)
                            rel_path = cls._sheet_rels_path(info.filename)
                            if rel_path:
                                dst.writestr(
                                    rel_path,
                                    cls._patched_sheet_rels_xml(
                                        src.read(rel_path)
                                        if rel_path in src.namelist()
                                        else b"",
                                        updates[info.filename],
                                        relationships_ns,
                                        hyperlink_type,
                                    ),
                                )
                                written.add(rel_path)
                            continue
                        if info.filename in written:
                            continue
                        dst.writestr(copy.copy(info), src.read(info.filename))
            tmp_path.replace(workbook_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    @classmethod
    def _patched_sheet_xml(
        cls,
        xml_bytes: bytes,
        links: dict[str, str],
        sheet_path: str,
        src: zipfile.ZipFile,
        spreadsheet_ns: str,
        relationships_ns: str,
        office_rel_ns: str,
        hyperlink_type: str,
    ) -> bytes:
        root = ET.fromstring(xml_bytes)
        rel_path = cls._sheet_rels_path(sheet_path)
        rel_bytes = (
            src.read(rel_path) if rel_path and rel_path in src.namelist() else b""
        )
        rel_ids = cls._hyperlink_rel_ids(
            rel_bytes, links, relationships_ns, hyperlink_type
        )

        hyperlinks = root.find(f"{{{spreadsheet_ns}}}hyperlinks")
        if hyperlinks is None:
            hyperlinks = ET.Element(f"{{{spreadsheet_ns}}}hyperlinks")
            root.append(hyperlinks)

        for child in list(hyperlinks):
            if child.attrib.get("ref") in links:
                hyperlinks.remove(child)

        for cell in sorted(links, key=cls._cell_sort_key):
            display = Path(links[cell]).name
            cls._set_inline_cell_text(root, cell, display, spreadsheet_ns)
            ET.SubElement(
                hyperlinks,
                f"{{{spreadsheet_ns}}}hyperlink",
                {
                    "ref": cell,
                    "display": display,
                    f"{{{office_rel_ns}}}id": rel_ids[cell],
                },
            )
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    @classmethod
    def _set_inline_cell_text(
        cls,
        sheet_root: ET.Element,
        cell_ref: str,
        text: str,
        spreadsheet_ns: str,
    ) -> None:
        column_name, row_number = cls._split_cell_ref(cell_ref)
        if row_number <= 0 or not column_name:
            return

        sheet_data = sheet_root.find(f"{{{spreadsheet_ns}}}sheetData")
        if sheet_data is None:
            sheet_data = ET.SubElement(sheet_root, f"{{{spreadsheet_ns}}}sheetData")

        row = None
        for candidate in sheet_data.findall(f"{{{spreadsheet_ns}}}row"):
            if candidate.attrib.get("r") == str(row_number):
                row = candidate
                break
        if row is None:
            row = ET.Element(f"{{{spreadsheet_ns}}}row", {"r": str(row_number)})
            sheet_data.append(row)

        cell = None
        for candidate in row.findall(f"{{{spreadsheet_ns}}}c"):
            if candidate.attrib.get("r") == cell_ref:
                cell = candidate
                break
        if cell is None:
            cell = ET.Element(f"{{{spreadsheet_ns}}}c", {"r": cell_ref})
            row.append(cell)

        cell.attrib["t"] = "inlineStr"
        for child in list(cell):
            if child.tag in {
                f"{{{spreadsheet_ns}}}v",
                f"{{{spreadsheet_ns}}}is",
                f"{{{spreadsheet_ns}}}f",
            }:
                cell.remove(child)
        inline = ET.SubElement(cell, f"{{{spreadsheet_ns}}}is")
        ET.SubElement(inline, f"{{{spreadsheet_ns}}}t").text = text
        cls._sort_sheet_rows_and_cells(sheet_data)

    @classmethod
    def _sort_sheet_rows_and_cells(cls, sheet_data: ET.Element) -> None:
        rows = list(sheet_data)
        rows.sort(key=lambda row: int(row.attrib.get("r", "0") or 0))
        for row in rows:
            cells = list(row)
            cells.sort(key=lambda cell: cls._cell_sort_key(cell.attrib.get("r", "")))
            row[:] = cells
        sheet_data[:] = rows

    @staticmethod
    def _split_cell_ref(cell_ref: str) -> tuple[str, int]:
        match = re.match(r"([A-Z]+)(\d+)", cell_ref)
        if not match:
            return "", 0
        column_name, row_number = match.groups()
        return column_name, int(row_number)

    @staticmethod
    def _sheet_rels_path(sheet_path: str) -> str:
        path = Path(sheet_path)
        return str(path.parent / "_rels" / f"{path.name}.rels")

    @classmethod
    def _patched_sheet_rels_xml(
        cls,
        rel_bytes: bytes,
        links: dict[str, str],
        relationships_ns: str,
        hyperlink_type: str,
    ) -> bytes:
        if rel_bytes:
            root = ET.fromstring(rel_bytes)
        else:
            root = ET.Element(f"{{{relationships_ns}}}Relationships")

        by_target = {
            rel.attrib.get("Target"): rel
            for rel in root.findall(f"{{{relationships_ns}}}Relationship")
            if rel.attrib.get("Type") == hyperlink_type
        }
        used_ids = {
            rel.attrib.get("Id", "")
            for rel in root.findall(f"{{{relationships_ns}}}Relationship")
        }

        for target in links.values():
            if target in by_target:
                continue
            rid = cls._next_rid(used_ids)
            used_ids.add(rid)
            ET.SubElement(
                root,
                f"{{{relationships_ns}}}Relationship",
                {
                    "Id": rid,
                    "Type": hyperlink_type,
                    "Target": target,
                    "TargetMode": "External",
                },
            )
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    @classmethod
    def _hyperlink_rel_ids(
        cls,
        rel_bytes: bytes,
        links: dict[str, str],
        relationships_ns: str,
        hyperlink_type: str,
    ) -> dict[str, str]:
        if rel_bytes:
            root = ET.fromstring(rel_bytes)
        else:
            root = ET.Element(f"{{{relationships_ns}}}Relationships")

        by_target = {
            rel.attrib.get("Target"): rel.attrib.get("Id", "")
            for rel in root.findall(f"{{{relationships_ns}}}Relationship")
            if rel.attrib.get("Type") == hyperlink_type
        }
        used_ids = {
            rel.attrib.get("Id", "")
            for rel in root.findall(f"{{{relationships_ns}}}Relationship")
        }
        result: dict[str, str] = {}
        for cell, target in links.items():
            if target in by_target and by_target[target]:
                result[cell] = by_target[target]
                continue
            rid = cls._next_rid(used_ids)
            used_ids.add(rid)
            by_target[target] = rid
            result[cell] = rid
        return result

    @staticmethod
    def _next_rid(used_ids: set[str]) -> str:
        index = 1
        while f"rId{index}" in used_ids:
            index += 1
        return f"rId{index}"

    @staticmethod
    def _cell_sort_key(cell: str) -> tuple[int, int]:
        match = re.match(r"([A-Z]+)(\d+)", cell)
        if not match:
            return (0, 0)
        col, row = match.groups()
        col_num = 0
        for char in col:
            col_num = col_num * 26 + ord(char) - 64
        return (int(row), col_num)

    def _read_spreadsheet_header(
        self,
        client,
        spreadsheet_token: str,
        sheet_id: str,
        column_count: int,
    ) -> dict[int, str]:
        values = self._read_spreadsheet_values(
            client,
            spreadsheet_token,
            f"{sheet_id}!A1:{self._column_name(column_count)}1",
        )
        if not values:
            return {}
        return {
            index: self._safe_path_part(self._plain_cell_text(value))
            for index, value in enumerate(values[0], start=1)
            if self._plain_cell_text(value)
        }

    def _read_spreadsheet_values(
        self,
        client,
        spreadsheet_token: str,
        range_ref: str,
    ) -> list[list[Any]]:
        req = (
            BaseRequest.builder()
            .http_method(HttpMethod.GET)
            .uri(
                f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{range_ref}"
            )
            .token_types({AccessTokenType.TENANT})
            .build()
        )
        resp = client.request(req)
        code = getattr(resp, "code", None)
        if code != 0:
            raise RuntimeError(
                f"read spreadsheet values failed range={range_ref} "
                f"code={code} msg={getattr(resp, 'msg', '')}"
            )
        data = self._response_data(resp)
        value_range = data.get("valueRange") or {}
        return value_range.get("values") or []

    @classmethod
    def _extract_cell_attachments(cls, value: Any) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if isinstance(value, dict):
            if value.get("type") == "attachment" and (
                value.get("fileToken") or value.get("file_token")
            ):
                found.append(value)
            for child in value.values():
                found.extend(cls._extract_cell_attachments(child))
        elif isinstance(value, list):
            for child in value:
                found.extend(cls._extract_cell_attachments(child))
        return found

    @classmethod
    def _attachment_name(cls, attachment: dict[str, Any], fallback: str) -> str:
        name = cls._safe_filename(
            str(attachment.get("text") or attachment.get("name") or fallback)
        )
        if Path(name).suffix:
            return name
        suffix = cls._extension_from_mime(str(attachment.get("mimeType") or ""))
        return f"{name}{suffix}" if suffix else name

    @staticmethod
    def _plain_cell_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)):
            return str(value)
        if isinstance(value, dict):
            return str(value.get("text") or value.get("name") or "")
        if isinstance(value, list):
            return " ".join(
                text for item in value if (text := SyncManager._plain_cell_text(item))
            )
        return ""

    @classmethod
    def _row_summary(cls, row: list[Any]) -> str:
        parts: list[str] = []
        for cell in row[:8]:
            text = cls._plain_cell_text(cell).strip()
            if not text or text == "/":
                continue
            parts.append(text)
            if len(parts) >= 4:
                break
        return " | ".join(parts)[:500]

    @staticmethod
    def _column_name(index: int) -> str:
        name = ""
        while index > 0:
            index, remainder = divmod(index - 1, 26)
            name = chr(65 + remainder) + name
        return name or "A"

    @staticmethod
    def _extension_from_mime(mime_type: str) -> str:
        return {
            "application/pdf": ".pdf",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
            "application/vnd.ms-powerpoint": ".ppt",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        }.get(mime_type.lower(), "")

    @staticmethod
    def _safe_path_part(value: str) -> str:
        value = SyncManager._safe_filename(value)
        return value[:120] or "未命名"

    @staticmethod
    def _safe_filename(value: str) -> str:
        value = re.sub(r'[\\/:*?"<>|]+', "_", value)
        value = re.sub(r"\s+", " ", value).strip()
        return value[:180] or "未命名"

    async def _list_files(self, client, folder_token: str) -> list[dict]:
        """列一个 folder 下的所有文件（分页）。"""
        all_files: list[dict] = []
        page_token: str | None = None
        while True:
            builder = ListFileRequest.builder().folder_token(folder_token).page_size(50)
            if page_token:
                builder = builder.page_token(page_token)
            req = builder.build()

            resp = await call(
                "drive.file.list",
                client.drive.v1.file.alist(req),
            )
            if not resp.success():
                logger.warning(
                    "list_files folder=%s code=%s msg=%s",
                    folder_token,
                    getattr(resp, "code", "?"),
                    getattr(resp, "msg", "?"),
                )
                break

            items = (resp.data and resp.data.files) or []
            for it in items:
                all_files.append(
                    {
                        "token": getattr(it, "token", ""),
                        "name": getattr(it, "name", ""),
                        "type": getattr(it, "type", ""),
                        "modified_time": int(getattr(it, "modified_time", 0) or 0),
                    }
                )

            if not (resp.data and resp.data.has_more):
                break
            page_token = resp.data.next_page_token
            if not page_token:
                break

        return all_files

    async def _discover_company_cloud_files(
        self,
        client,
        *,
        limit: int | None = None,
    ) -> list[dict]:
        """通过飞书搜索接口发现企业云文档。

        注意：这要求应用开通 ``search:docs:read``。这个发现范围由飞书
        搜索接口和应用权限决定，不能再用单个测试 folder_token 代替公司云文档。
        """
        seen: set[str] = set()
        files: list[dict] = []
        hard_limit = limit or self.cfg.company_search_max_items or 0

        for query in self.cfg.company_search_queries:
            page_token = ""
            while True:
                batch = self._search_company_cloud_page(
                    client,
                    query=query,
                    page_token=page_token,
                )
                for item in batch["items"]:
                    token = item.get("token", "")
                    if not token or token in seen:
                        continue
                    seen.add(token)
                    files.append(item)
                    if hard_limit and len(files) >= hard_limit:
                        logger.info(
                            "达到 company_cloud max_items=%d，停止发现", hard_limit
                        )
                        return files

                if not batch["has_more"] or not batch["page_token"]:
                    break
                page_token = batch["page_token"]

        logger.info("公司云文档发现完成: %d 个候选文件", len(files))
        return files

    def _search_company_cloud_page(
        self,
        client,
        *,
        query: str,
        page_token: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "query": query,
            "page_size": self.cfg.company_search_page_size,
        }
        if page_token:
            body["page_token"] = page_token
        if self.cfg.company_search_doc_types:
            body["doc_filter"] = {"doc_types": self.cfg.company_search_doc_types}

        req = (
            BaseRequest.builder()
            .http_method(HttpMethod.POST)
            .uri("/open-apis/search/v2/doc_wiki/search")
            .token_types({AccessTokenType.TENANT})
            .headers({"Content-Type": "application/json; charset=utf-8"})
            .body(body)
            .build()
        )
        resp = client.request(req)
        code = getattr(resp, "code", None)
        msg = getattr(resp, "msg", "")
        data = self._response_data(resp)
        if code != 0:
            self._raise_company_search_error(code, msg, data)

        items = [
            self._search_result_to_file_info(item)
            for item in data.get("res_units") or []
        ]
        return {
            "items": [item for item in items if item],
            "has_more": bool(data.get("has_more")),
            "page_token": data.get("page_token") or "",
        }

    @staticmethod
    def _response_data(resp) -> dict[str, Any]:
        raw = getattr(resp, "raw", None)
        if raw and hasattr(raw, "content"):
            try:
                payload = json.loads(raw.content)
                return payload.get("data") or {}
            except (TypeError, json.JSONDecodeError):
                return {}
        return getattr(resp, "data", None) or {}

    @staticmethod
    def _raise_company_search_error(code: int, msg: str, data: dict[str, Any]) -> None:
        text = msg or json.dumps(data, ensure_ascii=False)
        if "search:docs:read" in text:
            raise RuntimeError(
                "公司云文档全量发现需要开通飞书应用权限 search:docs:read。"
                "授权页: https://open.feishu.cn/app/cli_aa8cc8481e7a9bea/auth"
                "?q=search:docs:read&op_from=openapi&token_type=tenant"
            )
        if code == 99992402:
            raise RuntimeError(
                "飞书搜索参数校验失败，请检查 feishu.company_cloud.queries "
                f"和 doc_types 配置。原始错误: {msg}"
            )
        raise RuntimeError(f"search_doc_wiki failed code={code} msg={msg}")

    def _search_result_to_file_info(self, item: dict[str, Any]) -> dict[str, Any]:
        meta = item.get("result_meta") or {}
        token = meta.get("token") or ""
        ftype = self._normalize_search_type(
            str(meta.get("doc_types") or item.get("entity_type") or ""),
            str(meta.get("url") or ""),
        )
        title = self._clean_title(str(item.get("title_highlighted") or ""))
        if not title:
            title = Path(str(meta.get("url") or "")).name or token or "feishu-doc"
        return {
            "token": token,
            "name": title,
            "type": ftype,
            "modified_time": int(meta.get("update_time") or 0),
            "source": "company_cloud",
            "url": meta.get("url") or "",
        }

    @staticmethod
    def _clean_title(value: str) -> str:
        value = re.sub(r"<[^>]+>", "", value)
        value = html.unescape(value).strip()
        return value.replace("/", "_").replace(":", "_")[:180]

    @staticmethod
    def _normalize_search_type(raw_type: str, url: str) -> str:
        value = raw_type.lower().strip()
        if value in {"doc", "docx", "sheet", "bitable", "file"}:
            return value
        if "/docx/" in url:
            return "docx"
        if "/wiki/" in url:
            return "wiki"
        if "/docs/" in url or "/doc/" in url:
            return "doc"
        if "/sheets/" in url or "/sheet/" in url:
            return "sheet"
        if "/base/" in url:
            return "bitable"
        return value or "file"

    async def _sync_one(self, client, file_info: dict) -> str:
        """同步一个文件。返 'synced' / 'skipped' / 'failed'。"""
        original_info = copy.deepcopy(file_info)
        try:
            file_info = await self._normalize_file_info(client, file_info)
        except Exception as exc:  # noqa: BLE001
            logger.warning("解析文档信息失败: %s", exc)
            return "failed"

        token = file_info["token"]
        name = file_info["name"]
        ftype = file_info["type"]
        mtime = file_info["modified_time"]

        # 类型过滤
        if not self._supported(name, ftype):
            return "skipped"

        # mtime 增量过滤
        state_files = self._state_files()
        prev = state_files.get(token, {})
        if prev.get("mtime") == mtime and prev.get("done"):
            prev_path = Path(str(prev.get("local_path") or ""))
            if self._path_exists_or_indexed(prev_path):
                return "skipped"

        # dry-run 不动磁盘
        if self.dry_run:
            logger.info("[dry-run] would sync: %s (token=%s)", name, token[:8])
            return "synced"

        # 飞书 docx/sheet/bitable 需要 export，普通文件直接 download
        target_dir = (
            Path(str(file_info["target_dir"])) if file_info.get("target_dir") else None
        )
        target = self._target_for(token, name, ftype, prev, target_dir=target_dir)
        existing_token_target = self._find_existing_token_target(target, token)
        if existing_token_target is not None:
            state_files[token] = self._state_entry_for(file_info, existing_token_target)
            logger.info("跳过已存在附件: %s → %s", name, existing_token_target.name)
            return "skipped"
        if self._path_is_quarantined(target):
            state_files[token] = {
                "name": name,
                "type": ftype,
                "mtime": mtime,
                "local_path": str(target),
                "done": True,
                "quarantined": True,
                "skipped_at": int(time.time()),
            }
            logger.info("跳过已隔离附件: %s", name)
            return "skipped"

        tmp_target = self._temporary_download_target(target, token)
        try:
            if ftype in self.cfg.export_map:
                ext = self.cfg.export_map[ftype]
                target = target.with_suffix(f".{ext}")
                target = self._target_for(
                    token, target.name, ftype, prev, target_dir=target_dir
                )
                tmp_target = self._temporary_download_target(target, token)
                await self._export_to_file(client, token, ftype, ext, tmp_target)
            elif ftype == "attachment":
                await self._download_media_to_file(client, token, tmp_target)
            else:
                await self._download_to_file(client, token, tmp_target)
        except Exception as exc:  # noqa: BLE001
            tmp_target.unlink(missing_ok=True)
            if (
                not original_info.get("_wiki_fallback_tried")
                and str(original_info.get("type") or "") != "wiki"
            ):
                try:
                    fallback_info = await self._resolve_wiki_file_info(
                        client,
                        str(
                            original_info.get("wiki_token")
                            or original_info.get("token")
                            or ""
                        ),
                        url=str(original_info.get("url") or ""),
                        fallback_name=str(original_info.get("name") or name),
                    )
                    fallback_info["source"] = str(
                        original_info.get("source") or "wiki_fallback"
                    )
                    fallback_info["admin_token"] = str(original_info.get("token") or "")
                    fallback_info["_wiki_fallback_tried"] = True
                    logger.info(
                        "直连 token 同步失败，改用 wiki 解析 token 重试: %s → %s",
                        str(original_info.get("token") or "")[:8],
                        str(fallback_info.get("token") or "")[:8],
                    )
                    return await self._sync_one(client, fallback_info)
                except Exception as fallback_exc:  # noqa: BLE001
                    logger.warning("wiki fallback 解析失败: %s", fallback_exc)
            logger.warning("同步 %s 失败: %s", name, exc)
            return "failed"

        try:
            existing = self._find_existing_same_content(tmp_target, target)
            if existing is not None:
                tmp_target.unlink(missing_ok=True)
                target = existing
                outcome = "skipped"
            else:
                self._install_downloaded_file(tmp_target, target, token)
                self._validate_downloaded_target(target, ftype)
                outcome = "synced"
        except Exception as exc:  # noqa: BLE001
            tmp_target.unlink(missing_ok=True)
            logger.warning("保存 %s 失败: %s", name, exc)
            return "failed"

        # 更新 state
        state_files[token] = self._state_entry_for(file_info, target)
        logger.info("✓ %s → %s", name, target.name)
        return outcome

    def _state_entry_for(
        self, file_info: dict[str, Any], target: Path
    ) -> dict[str, Any]:
        state_entry = {
            "name": file_info["name"],
            "type": file_info["type"],
            "mtime": file_info["modified_time"],
            "local_path": str(target),
            "done": True,
            "synced_at": int(time.time()),
        }
        for key in (
            "source",
            "size",
            "mime_type",
            "spreadsheet_token",
            "spreadsheet_name",
            "sheet_id",
            "sheet_title",
            "cell",
            "row_index",
            "column_title",
            "row_summary",
            "admin_token",
        ):
            if key in file_info:
                state_entry[key] = file_info[key]
        return state_entry

    def _target_for(
        self,
        token: str,
        name: str,
        ftype: str,
        prev: dict[str, Any],
        *,
        target_dir: Path | None = None,
    ) -> Path:
        target = (target_dir or self.cfg.nas_inbox) / name
        if ftype in self.cfg.export_map:
            target = target.with_suffix(f".{self.cfg.export_map[ftype]}")

        prev_local = str(prev.get("local_path") or "")
        state_files = self._state_files()
        claimed_paths = {
            str(item.get("local_path"))
            for other_token, item in state_files.items()
            if other_token != token and isinstance(item, dict)
        }

        if (
            str(target) == prev_local
            or str(target) not in claimed_paths
            and not target.exists()
        ):
            return target

        stem = target.stem
        suffix = target.suffix
        token_hint = token[:8] or "feishu"
        candidate = target.with_name(f"{stem}__feishu_{token_hint}{suffix}")
        index = 2
        while str(candidate) in claimed_paths or (
            candidate.exists() and str(candidate) != prev_local
        ):
            candidate = target.with_name(f"{stem}__feishu_{token_hint}_{index}{suffix}")
            index += 1
        return candidate

    def _find_existing_same_content(
        self, tmp_target: Path, target: Path
    ) -> Path | None:
        candidates = [
            target,
            self.cfg.nas_processed / target.name,
        ]
        candidates.extend(self._token_variant_candidates(target))
        tmp_hash = sha256_file(tmp_target)
        for candidate in candidates:
            if not candidate.exists() or not candidate.is_file():
                continue
            try:
                if candidate.stat().st_size == tmp_target.stat().st_size and (
                    sha256_file(candidate) == tmp_hash
                ):
                    return candidate
            except OSError:
                continue
        return None

    def _find_existing_token_target(self, target: Path, token: str) -> Path | None:
        candidates = [target, *self._token_variant_candidates(target, token)]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def _token_variant_candidates(
        self, target: Path, token: str | None = None
    ) -> list[Path]:
        token_hint = (token or "").strip()[:8]
        if not token_hint:
            match = re.search(r"__feishu_([^_./]+)", target.stem)
            token_hint = match.group(1)[:8] if match else ""
        if not token_hint:
            return []

        marker = f"__feishu_{token_hint}"
        base_stem = (
            target.stem.split(marker, 1)[0] if marker in target.stem else target.stem
        )
        pattern = f"{base_stem}__feishu_{token_hint}*{target.suffix}"
        try:
            return sorted(
                path for path in target.parent.glob(pattern) if path.is_file()
            )
        except OSError:
            return []

    def _temporary_download_target(self, target: Path, token: str) -> Path:
        DOWNLOAD_STAGING_DIR.mkdir(parents=True, exist_ok=True)
        token_hint = token[:8] or "feishu"
        safe_stem = self._safe_path_part(target.stem or "feishu-download")
        return (
            DOWNLOAD_STAGING_DIR / f"{safe_stem}__{token_hint}{target.suffix}.download"
        )

    def _install_downloaded_file(
        self, tmp_target: Path, target: Path, token: str
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        token_hint = token[:8] or "feishu"
        install_target = target.with_name(f".{target.name}.install-{token_hint}")
        source_size = tmp_target.stat().st_size
        attempts = 0
        while install_target.exists() and install_target.stat().st_size > source_size:
            install_target.unlink(missing_ok=True)
        while (
            not install_target.exists() or install_target.stat().st_size < source_size
        ):
            attempts += 1
            if attempts > 12:
                raise OSError(f"install copy failed after retries: {target}")
            offset = install_target.stat().st_size if install_target.exists() else 0
            try:
                with tmp_target.open("rb") as source, install_target.open("ab") as dest:
                    source.seek(offset)
                    for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                        dest.write(chunk)
            except OSError as exc:
                logger.warning(
                    "安装到 NAS 重试: target=%s offset=%s reason=%s",
                    target,
                    offset,
                    exc,
                )
                time.sleep(min(15, 2 * attempts))
                continue
        install_target.replace(target)
        if not target.exists():
            raise OSError(f"install replace did not create target: {target}")
        target_size = target.stat().st_size
        if target_size != source_size:
            raise OSError(
                f"install size mismatch: {target} target={target_size} source={source_size}"
            )
        tmp_target.unlink(missing_ok=True)

    def _validate_downloaded_target(self, target: Path, ftype: str) -> None:
        if not target.exists():
            raise OSError(f"download target missing after install: {target}")
        if ftype in self.cfg.export_map and target.suffix.lower() in {
            ".docx",
            ".xlsx",
            ".pptx",
        }:
            if not zipfile.is_zipfile(target):
                raise OSError(
                    f"exported office file is not a valid zip container: {target}"
                )

    def _supported(self, name: str, ftype: str) -> bool:
        # 飞书原生文档（docx/sheet/bitable）可以 export，永远支持
        if ftype in self.cfg.export_map:
            return True
        # 其他文件按扩展名过滤
        ext = Path(name).suffix.lower()
        return ext in self.cfg.supported_extensions

    async def _download_to_file(self, client, token: str, target: Path) -> None:
        req = DownloadFileRequest.builder().file_token(token).build()
        await self._stream_download_request_to_file(
            client, req, target, "drive.file.download"
        )

    async def _download_media_to_file(self, client, token: str, target: Path) -> None:
        req = DownloadMediaRequest.builder().file_token(token).build()
        await self._stream_download_request_to_file(
            client, req, target, "drive.media.download"
        )

    async def _stream_download_request_to_file(
        self,
        client,
        req: BaseRequest,
        target: Path,
        method: str,
    ) -> None:
        config = client.config
        if config is None:
            raise RuntimeError("lark client config unavailable")
        target.parent.mkdir(parents=True, exist_ok=True)
        timeout = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)
        attempts = 0
        expected_total: int | None = None
        last_error: Exception | None = None

        while attempts < 20:
            attempts += 1
            offset = target.stat().st_size if target.exists() else 0
            req.headers.pop("Range", None)
            option = RequestOption()
            if offset:
                option.headers["Range"] = f"bytes={offset}-"
            verify(config, req, option)
            headers = _build_header(req, option, config).copy()
            url = _build_url(config.domain, req.uri, req.paths)
            try:
                async with httpx.AsyncClient(timeout=timeout) as http_client:
                    async with http_client.stream(
                        str(req.http_method.name),
                        url,
                        headers=headers,
                        params=req.queries,
                    ) as response:
                        if response.status_code in {200, 206}:
                            if offset and response.status_code == 200:
                                logger.warning(
                                    "飞书下载不支持续传，重新下载: target=%s",
                                    target.name,
                                )
                                offset = 0
                            content_range = response.headers.get("Content-Range") or ""
                            if "bytes" in content_range and "/" in content_range:
                                total_text = content_range.rsplit("/", 1)[-1]
                                if total_text.isdigit():
                                    expected_total = int(total_text)
                            elif response.headers.get("Content-Length", "").isdigit():
                                content_length = int(response.headers["Content-Length"])
                                expected_total = offset + content_length
                            mode = (
                                "ab" if offset and response.status_code == 206 else "wb"
                            )
                            with target.open(mode) as file:
                                async for chunk in response.aiter_bytes(
                                    8 * 1024 * 1024
                                ):
                                    if chunk:
                                        file.write(chunk)
                            if (
                                expected_total is None
                                or target.stat().st_size >= expected_total
                            ):
                                get_hub().record_call(method, error=None)
                                return
                            logger.warning(
                                "飞书下载未完整，准备续传: target=%s got=%s expected=%s",
                                target.name,
                                target.stat().st_size,
                                expected_total,
                            )
                            continue
                        if response.headers.get("Content-Type", "").startswith(
                            "application/json"
                        ):
                            body = await response.aread()
                            raise RuntimeError(
                                f"download failed status={response.status_code} body={body[:500]!r}"
                            )
                        raise RuntimeError(
                            f"download failed status={response.status_code}"
                        )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                get_hub().record_call(method, error=exc)
                logger.warning(
                    "飞书分块下载重试: target=%s attempt=%s partial=%s reason=%s",
                    target.name,
                    attempts,
                    target.stat().st_size if target.exists() else 0,
                    exc,
                )
                await asyncio.sleep(min(20, attempts * 2))

        raise RuntimeError(f"download failed after retries: {last_error}")

    async def _download_exported_to_file(
        self,
        client,
        file_token: str,
        target: Path,
    ) -> None:
        req = DownloadExportTaskRequest.builder().file_token(file_token).build()
        await self._stream_download_request_to_file(
            client,
            req,
            target,
            "drive.export_task.download",
        )

    async def _export_to_file(
        self,
        client,
        token: str,
        source_type: str,
        target_ext: str,
        target: Path,
    ) -> None:
        # 1. 提 export task（这个版本的 lark-oapi 把 body 类型复用 ExportTask 模型）
        body = (
            ExportTask.builder()
            .file_extension(target_ext)
            .token(token)
            .type(source_type)
            .build()
        )
        req = CreateExportTaskRequest.builder().request_body(body).build()
        resp = await call(
            "drive.export_task.create",
            client.drive.v1.export_task.acreate(req),
        )
        if not resp.success() or not resp.data:
            raise RuntimeError(
                f"create_export_task failed code={getattr(resp, 'code', '?')} "
                f"msg={getattr(resp, 'msg', '?')}"
            )
        ticket = resp.data.ticket

        # 2. 轮询 task 状态。大表格 / 多维表格导出经常超过 30s。
        export_file_token: str | None = None
        timeout_seconds = max(30, int(self.cfg.export_task_timeout_seconds or 600))
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            req2 = GetExportTaskRequest.builder().ticket(ticket).token(token).build()
            resp2 = await call(
                "drive.export_task.get",
                client.drive.v1.export_task.aget(req2),
            )
            if not resp2.success() or not resp2.data or not resp2.data.result:
                continue
            status = resp2.data.result.job_status
            if status == 0:  # success
                export_file_token = resp2.data.result.file_token
                break
            if status not in (1, 2):  # 1/2 = pending/in_progress
                error_msg = str(getattr(resp2.data.result, "job_error_msg", "") or "")
                file_size = str(getattr(resp2.data.result, "file_size", "") or "")
                raise RuntimeError(
                    f"export_task job_status={status}"
                    f"{f' error={error_msg}' if error_msg else ''}"
                    f"{f' file_size={file_size}' if file_size else ''}"
                )
        if not export_file_token:
            raise RuntimeError(f"export_task 超时（{timeout_seconds}s 内未完成）")

        # 3. download 导出后的文件
        await self._download_exported_to_file(client, export_file_token, target)


# ────────────────────────── CLI ──────────────────────────


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def _main_async(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.skip_sheet_attachments:
        cfg.sheet_attachments_enabled = False
    if args.attachment_limit is not None:
        cfg.sheet_attachments_max_items = args.attachment_limit
    if args.attachment_timeout is not None:
        cfg.sheet_attachment_timeout_seconds = args.attachment_timeout
    if args.attachment_max_download_mb is not None:
        cfg.sheet_attachment_max_download_mb = args.attachment_max_download_mb
    mgr = SyncManager(cfg, dry_run=args.dry_run)

    if args.preflight:
        try:
            result = await mgr.preflight()
        except Exception as exc:  # noqa: BLE001
            logger.error("预检失败: %s", exc)
            return 1
        logger.info("预检结果: %s", result)
        return 0 if result.get("ok") else 1

    if args.url:
        stats = await mgr.sync_urls(args.url)
        logger.info("同步统计: %s", stats)
        logger.info("hub 调用统计: %s", get_hub().stats.snapshot())
        return 0 if stats.get("failed", 0) == 0 else 1

    if args.token:
        if not args.type or not args.name:
            logger.error("--token 同步需要同时提供 --type 和 --name")
            return 2
        stats = await mgr.sync_file_infos(
            [
                {
                    "token": args.token,
                    "name": args.name,
                    "type": args.type,
                    "modified_time": args.modified_time or 0,
                    "source": "admin_cloud_space",
                }
            ]
        )
        logger.info("同步统计: %s", stats)
        logger.info("hub 调用统计: %s", get_hub().stats.snapshot())
        return 0 if stats.get("failed", 0) == 0 else 1

    if args.watch:
        await mgr.watch()
        return 0

    stats = await mgr.sync_once()
    logger.info("同步统计: %s", stats)
    logger.info("hub 调用统计: %s", get_hub().stats.snapshot())
    return 0 if stats.get("failed", 0) == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="飞书云空间 → NAS inbox 同步 (v2)")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "config.yaml"),
        help="配置 yaml 路径",
    )
    parser.add_argument("--watch", action="store_true", help="持续模式")
    parser.add_argument("--dry-run", action="store_true", help="只看不下载")
    parser.add_argument("--preflight", action="store_true", help="只读预检配置和权限")
    parser.add_argument(
        "--url",
        action="append",
        default=[],
        help="同步指定的飞书云文档 URL，可重复传多个",
    )
    parser.add_argument("--token", help="同步后台云文档 token（需配合 --type/--name）")
    parser.add_argument("--type", help="后台云文档类型：docx/sheet/bitable/file 等")
    parser.add_argument("--name", help="后台云文档标题或文件名")
    parser.add_argument(
        "--modified-time",
        type=int,
        default=0,
        help="后台云文档修改时间戳；未知时为 0",
    )
    parser.add_argument(
        "--skip-sheet-attachments",
        action="store_true",
        help="同步表格时不递归下载单元格附件",
    )
    parser.add_argument(
        "--attachment-limit",
        type=int,
        default=None,
        help="本次最多同步多少个表格附件；0 表示不限制",
    )
    parser.add_argument(
        "--attachment-timeout",
        type=int,
        default=None,
        help="单个表格附件同步超时时间秒数；默认读取配置，未配置为 90",
    )
    parser.add_argument(
        "--attachment-max-download-mb",
        type=int,
        default=None,
        help="单个表格附件最大下载 MB；超过则标记 failed 并跳过，0 表示不限制",
    )
    args = parser.parse_args()
    _setup_logging()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
