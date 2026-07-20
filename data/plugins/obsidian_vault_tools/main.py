"""Expose the governed company Obsidian vault to the main Agent as read-only tools."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from dc_engines.obsidian_vault import (
    ObsidianVault,
    VaultAccessError,
    VaultSecurityPolicy,
)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "obsidian_vault_tools",
    "dc_agent",
    "Read-only company Obsidian vault tools for the main Agent",
    "1.0.0",
)
class ObsidianVaultToolsPlugin(Star):
    """Provide bounded, admin-only access to the configured company vault."""

    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context, config)
        self._config = config or {}

    def _is_authorized(self, event: AstrMessageEvent) -> bool:
        """Return whether the current sender may access company vault content.

        Args:
            event: Current AstrBot message event.

        Returns:
            True when the sender is an AstrBot administrator or explicitly
            allowlisted by this plugin.
        """

        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            return False
        try:
            root_config = self.context.get_config() or {}
        except Exception:  # noqa: BLE001
            root_config = {}
        admins = (
            root_config.get("admins_id", []) if isinstance(root_config, dict) else []
        )
        allowed = self._config.get("allowed_user_ids", [])
        authorized_ids = {
            str(item).strip()
            for item in [
                *(admins if isinstance(admins, list) else []),
                *(allowed if isinstance(allowed, list) else []),
            ]
            if str(item).strip()
        }
        return sender_id in authorized_ids

    def _vault(self) -> ObsidianVault:
        """Build the read-only wrapper for the configured vault root.

        Returns:
            A security-policy-enforced Obsidian vault wrapper.

        Raises:
            ValueError: The configured vault root is missing or not a directory.
        """

        configured_path = str(
            self._config.get("obsidian_vault_path")
            or os.environ.get("DC_OBSIDIAN_VAULT_PATH")
            or ""
        ).strip()
        vault_root = (
            Path(configured_path)
            if configured_path
            else Path(__file__).resolve().parents[3] / "ObsidianVault"
        )
        return ObsidianVault(
            VaultSecurityPolicy(
                vault_root=vault_root,
                read_only=True,
                actor="dc-main-agent",
            )
        )

    @filter.llm_tool(name="search_obsidian_vault")
    async def search_obsidian_vault(
        self,
        event: AstrMessageEvent,
        query: str,
        scope: str = ".",
        limit: int = 12,
    ) -> str:
        """Search the real company Obsidian vault before answering internal-knowledge questions.

        Use this when an administrator asks about Obsidian, company knowledge,
        projects, clients, SOPs, reports, or historical materials. Results are
        literal matches with vault-relative source paths and line numbers.

        Args:
            query(string): Literal keyword or phrase to search for.
            scope(string): Vault-relative directory or Markdown note path; default is the whole vault.
            limit(number): Maximum result count, from 1 to 30.

        Returns:
            JSON containing matched source paths, line numbers, and excerpts.
        """

        if not self._is_authorized(event):
            return json.dumps(
                {"status": "forbidden", "reason": "admin_only"},
                ensure_ascii=False,
            )
        bounded_limit = max(1, min(int(limit or 12), 30))
        try:
            vault = self._vault()
            hits = await asyncio.to_thread(
                vault.search_notes,
                str(query or "").strip(),
                relative_path=str(scope or ".").strip() or ".",
                limit=bounded_limit,
            )
        except (ValueError, VaultAccessError, OSError) as exc:
            logger.warning("[obsidian_vault_tools] search failed: %s", exc)
            return json.dumps(
                {"status": "error", "reason": str(exc)},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "status": "ok",
                "query": query,
                "scope": scope,
                "hits": [
                    {
                        "path": hit.path,
                        "source": f"ObsidianVault/{hit.path}",
                        "line_number": hit.line_number,
                        "line": hit.line,
                    }
                    for hit in hits
                ],
            },
            ensure_ascii=False,
        )

    @filter.llm_tool(name="read_obsidian_note")
    async def read_obsidian_note(
        self,
        event: AstrMessageEvent,
        path: str,
        max_chars: int = 8_000,
    ) -> str:
        """Read one real Markdown note from the company Obsidian vault by source path.

        Call this after searching or listing the vault. Only vault-relative
        Markdown paths are accepted; traversal, absolute paths, and escaping
        symlinks are rejected by the controlled wrapper.

        Args:
            path(string): Vault-relative Markdown path returned by search or list.
            max_chars(number): Maximum note characters returned, from 500 to 12000.

        Returns:
            JSON containing the note content, frontmatter, and stable source path.
        """

        if not self._is_authorized(event):
            return json.dumps(
                {"status": "forbidden", "reason": "admin_only"},
                ensure_ascii=False,
            )
        bounded_max_chars = max(500, min(int(max_chars or 8_000), 12_000))
        try:
            note = await asyncio.to_thread(self._vault().read_note, str(path or ""))
        except (ValueError, VaultAccessError, OSError) as exc:
            logger.warning("[obsidian_vault_tools] read failed: %s", exc)
            return json.dumps(
                {"status": "error", "reason": str(exc)},
                ensure_ascii=False,
            )
        content = note.content[:bounded_max_chars]
        return json.dumps(
            {
                "status": "ok",
                "path": note.path,
                "source": f"ObsidianVault/{note.path}",
                "frontmatter": note.frontmatter,
                "content": content,
                "truncated": len(note.content) > len(content),
                "total_chars": len(note.content),
            },
            ensure_ascii=False,
            default=str,
        )

    @filter.llm_tool(name="list_obsidian_vault")
    async def list_obsidian_vault(
        self,
        event: AstrMessageEvent,
        path: str = ".",
        limit: int = 50,
    ) -> str:
        """List one directory in the real company Obsidian vault.

        Use this to discover the governed vault structure before selecting a
        note to search or read. Hidden Obsidian application metadata is omitted.

        Args:
            path(string): Vault-relative directory path; default is the vault root.
            limit(number): Maximum entry count, from 1 to 100.

        Returns:
            JSON containing bounded directory entries and source paths.
        """

        if not self._is_authorized(event):
            return json.dumps(
                {"status": "forbidden", "reason": "admin_only"},
                ensure_ascii=False,
            )
        bounded_limit = max(1, min(int(limit or 50), 100))
        try:
            entries = await asyncio.to_thread(
                self._vault().list_directory,
                str(path or ".").strip() or ".",
            )
        except (ValueError, VaultAccessError, OSError) as exc:
            logger.warning("[obsidian_vault_tools] list failed: %s", exc)
            return json.dumps(
                {"status": "error", "reason": str(exc)},
                ensure_ascii=False,
            )
        visible_entries = [
            entry
            for entry in entries
            if not any(part.startswith(".") for part in Path(entry.path).parts)
        ][:bounded_limit]
        return json.dumps(
            {
                "status": "ok",
                "path": path,
                "entries": [
                    {
                        "path": entry.path,
                        "source": f"ObsidianVault/{entry.path}",
                        "name": entry.name,
                        "kind": entry.kind,
                        "size": entry.size,
                    }
                    for entry in visible_entries
                ],
                "truncated": len(entries) > len(visible_entries),
            },
            ensure_ascii=False,
        )
