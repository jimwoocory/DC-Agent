"""Controlled read-only automation boundary for Obsidian vaults."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from difflib import unified_diff
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

_TEXT_SUFFIXES = {
    ".md",
    ".markdown",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
}


class VaultAccessError(PermissionError):
    """Raised when a vault operation violates the configured boundary."""


@dataclass(frozen=True)
class VaultAuditRecord:
    timestamp: str
    actor: str
    operation: str
    path: str
    allowed: bool
    dry_run: bool
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VaultEntry:
    path: str
    kind: str
    size_bytes: int
    modified_at: str


@dataclass(frozen=True)
class SearchHit:
    path: str
    line_number: int
    line: str


@dataclass(frozen=True)
class VaultAutomationConfig:
    vault_roots: tuple[Path | str, ...]
    audit_log_path: Path | str | None = None
    actor: str = "obsidian_vault_wrapper"
    max_read_bytes: int = 256 * 1024
    max_plan_bytes: int = 256 * 1024
    plan_ttl_seconds: int = 3600

    def resolved_roots(self) -> tuple[Path, ...]:
        roots = tuple(
            Path(root).expanduser().resolve(strict=True) for root in self.vault_roots
        )
        if not roots:
            raise VaultAccessError("At least one vault root must be configured.")
        return roots


class ObsidianVaultAutomation:
    """Read-only operations over allowlisted Obsidian vault roots."""

    def __init__(self, config: VaultAutomationConfig) -> None:
        self._config = config
        self._vault_roots = config.resolved_roots()
        self.audit_records: list[VaultAuditRecord] = []
        self._audit_log_path = (
            Path(config.audit_log_path).expanduser()
            if config.audit_log_path is not None
            else None
        )

    def list(
        self,
        path: Path | str = "",
        *,
        recursive: bool = False,
        actor: str | None = None,
    ) -> list[VaultEntry]:
        resolved = self._resolve_path(path, operation="list", actor=actor)
        if not resolved.is_dir():
            self._audit(
                "list",
                path,
                allowed=False,
                dry_run=False,
                actor=actor,
                reason="path is not a directory",
            )
            raise VaultAccessError("Vault list path must be a directory.")

        iterator = resolved.rglob("*") if recursive else resolved.iterdir()
        entries: list[VaultEntry] = []
        for item in sorted(iterator, key=lambda item: item.as_posix()):
            if item.name.startswith("."):
                continue
            resolved_item = item.resolve(strict=True)
            self._ensure_within_roots(
                resolved_item,
                operation="list",
                raw_path=item,
                actor=actor,
            )
            entries.append(self._entry_for(item))
        self._audit(
            "list",
            path,
            allowed=True,
            dry_run=False,
            actor=actor,
            reason="listed vault path",
            payload={"count": len(entries), "recursive": recursive},
        )
        return entries

    def read(
        self,
        path: Path | str,
        *,
        actor: str | None = None,
        max_bytes: int | None = None,
    ) -> str:
        resolved = self._resolve_path(path, operation="read", actor=actor)
        self._ensure_readable_text_file(resolved, path, "read", actor)
        byte_limit = max_bytes or self._config.max_read_bytes
        size_bytes = resolved.stat().st_size
        if size_bytes > byte_limit:
            self._audit(
                "read",
                path,
                allowed=False,
                dry_run=False,
                actor=actor,
                reason="file exceeds read byte limit",
                payload={"size_bytes": size_bytes, "max_bytes": byte_limit},
            )
            raise VaultAccessError("Vault file exceeds read byte limit.")

        content = resolved.read_text(encoding="utf-8")
        self._audit(
            "read",
            path,
            allowed=True,
            dry_run=False,
            actor=actor,
            reason="read vault text file",
            payload={"size_bytes": size_bytes},
        )
        return content

    def search(
        self,
        query: str,
        path: Path | str = "",
        *,
        actor: str | None = None,
        max_results: int = 50,
        case_sensitive: bool = False,
    ) -> list[SearchHit]:
        if not query:
            raise ValueError("Search query must not be empty.")
        resolved = self._resolve_path(path, operation="search", actor=actor)
        if resolved.is_file():
            candidates = [resolved]
        else:
            candidates = [
                item
                for item in resolved.rglob("*")
                if item.is_file() and item.suffix.lower() in _TEXT_SUFFIXES
            ]

        needle = query if case_sensitive else query.lower()
        hits: list[SearchHit] = []
        for candidate in sorted(candidates, key=lambda item: item.as_posix()):
            resolved_candidate = candidate.resolve(strict=True)
            self._ensure_within_roots(
                resolved_candidate,
                operation="search",
                raw_path=candidate,
                actor=actor,
            )
            for line_number, line in enumerate(
                resolved_candidate.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                haystack = line if case_sensitive else line.lower()
                if needle in haystack:
                    hits.append(
                        SearchHit(
                            path=self._relative_path(resolved_candidate),
                            line_number=line_number,
                            line=line,
                        )
                    )
                    if len(hits) >= max_results:
                        self._audit(
                            "search",
                            path,
                            allowed=True,
                            dry_run=False,
                            actor=actor,
                            reason="searched vault text files",
                            payload={
                                "query": query,
                                "hit_count": len(hits),
                                "truncated": True,
                            },
                        )
                        return hits

        self._audit(
            "search",
            path,
            allowed=True,
            dry_run=False,
            actor=actor,
            reason="searched vault text files",
            payload={"query": query, "hit_count": len(hits), "truncated": False},
        )
        return hits

    def metadata(self, path: Path | str, *, actor: str | None = None) -> dict[str, Any]:
        resolved = self._resolve_path(path, operation="metadata", actor=actor)
        stat = resolved.stat()
        result = {
            "path": self._relative_path(resolved),
            "kind": "directory" if resolved.is_dir() else "file",
            "size_bytes": stat.st_size,
            "modified_at": _format_timestamp(stat.st_mtime),
        }
        if resolved.is_file():
            result["sha256"] = _sha256(resolved)
            result["suffix"] = resolved.suffix.lower()

        self._audit(
            "metadata",
            path,
            allowed=True,
            dry_run=False,
            actor=actor,
            reason="read vault metadata",
            payload={"kind": result["kind"]},
        )
        return result

    def frontmatter(
        self, path: Path | str, *, actor: str | None = None
    ) -> dict[str, Any]:
        markdown = self.read(path, actor=actor)
        frontmatter = _parse_frontmatter(markdown)
        self._audit(
            "frontmatter",
            path,
            allowed=True,
            dry_run=False,
            actor=actor,
            reason="parsed vault note frontmatter",
            payload={"keys": sorted(frontmatter)},
        )
        return frontmatter

    def plan_write(
        self,
        path: Path | str,
        content: str,
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        resolved = self._resolve_candidate_path(
            path, operation="plan_write", actor=actor
        )
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > self._config.max_plan_bytes:
            self._audit(
                "plan_write",
                path,
                allowed=False,
                dry_run=True,
                actor=actor,
                reason="planned content exceeds byte limit",
                payload={
                    "bytes": len(content_bytes),
                    "max_plan_bytes": self._config.max_plan_bytes,
                },
            )
            raise VaultAccessError("Planned vault content exceeds byte limit.")

        existing_content = ""
        existing_sha256 = None
        action = "create"
        if resolved.exists():
            self._ensure_readable_text_file(resolved, path, "plan_write", actor)
            existing_content = resolved.read_text(encoding="utf-8")
            existing_sha256 = hashlib.sha256(
                existing_content.encode("utf-8")
            ).hexdigest()
            action = "update"

        created_at = datetime.now(UTC)
        expires_at = created_at + timedelta(seconds=self._config.plan_ttl_seconds)
        relative_path = self._relative_path(resolved)
        payload = {
            "plan_id": f"ova-plan-{uuid4().hex}",
            "path": relative_path,
            "action": action,
            "risk": _plan_risk(action, len(content_bytes)),
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "content_sha256": hashlib.sha256(content_bytes).hexdigest(),
            "existing_sha256": existing_sha256,
            "bytes": len(content_bytes),
            "diff": _unified_content_diff(
                existing_content,
                content,
                fromfile=f"a/{relative_path}",
                tofile=f"b/{relative_path}",
            ),
        }
        self._audit(
            "plan_write",
            path,
            allowed=True,
            dry_run=True,
            actor=actor,
            reason="write plan recorded without modifying vault",
            payload=payload,
        )
        return {"dry_run": True, **payload}

    def write(
        self,
        path: Path | str,
        content: str,
        *,
        actor: str | None = None,
    ) -> None:
        _ = content
        self._deny_operation("write", path, actor=actor)

    def delete(self, path: Path | str, *, actor: str | None = None) -> None:
        self._deny_operation("delete", path, actor=actor)

    def run_shell(self, command: str, *, actor: str | None = None) -> None:
        self._audit(
            "shell",
            "<shell>",
            allowed=False,
            dry_run=False,
            actor=actor,
            reason="arbitrary shell execution is forbidden",
            payload={"command": command},
        )
        raise VaultAccessError("Arbitrary shell execution is forbidden.")

    def _deny_operation(
        self,
        operation: str,
        path: Path | str,
        *,
        actor: str | None,
    ) -> None:
        self._audit(
            operation,
            path,
            allowed=False,
            dry_run=False,
            actor=actor,
            reason="vault write execution is not enabled",
        )
        raise VaultAccessError(f"Vault {operation} is not enabled.")

    def _resolve_path(
        self,
        path: Path | str,
        *,
        operation: str,
        actor: str | None,
    ) -> Path:
        candidate = self._candidate_path(path)
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            self._audit(
                operation,
                path,
                allowed=False,
                dry_run=False,
                actor=actor,
                reason="path does not exist",
            )
            raise VaultAccessError("Vault path does not exist.") from exc
        return self._ensure_within_roots(
            resolved,
            operation=operation,
            raw_path=path,
            actor=actor,
        )

    def _resolve_candidate_path(
        self,
        path: Path | str,
        *,
        operation: str,
        actor: str | None,
    ) -> Path:
        candidate = self._candidate_path(path).resolve(strict=False)
        return self._ensure_within_roots(
            candidate,
            operation=operation,
            raw_path=path,
            actor=actor,
        )

    def _candidate_path(self, path: Path | str) -> Path:
        candidate = Path(path).expanduser()
        if candidate.is_absolute():
            return candidate
        return self._vault_roots[0] / candidate

    def _ensure_within_roots(
        self,
        resolved: Path,
        *,
        operation: str,
        raw_path: Path | str,
        actor: str | None,
    ) -> Path:
        if any(_is_relative_to(resolved, root) for root in self._vault_roots):
            return resolved
        self._audit(
            operation,
            raw_path,
            allowed=False,
            dry_run=False,
            actor=actor,
            reason="path is outside configured vault roots",
            payload={"resolved_path": str(resolved)},
        )
        raise VaultAccessError("Path is outside configured vault roots.")

    def _ensure_readable_text_file(
        self,
        resolved: Path,
        raw_path: Path | str,
        operation: str,
        actor: str | None,
    ) -> None:
        if not resolved.is_file():
            self._audit(
                operation,
                raw_path,
                allowed=False,
                dry_run=False,
                actor=actor,
                reason="path is not a file",
            )
            raise VaultAccessError("Vault path must be a file.")
        if resolved.suffix.lower() not in _TEXT_SUFFIXES:
            self._audit(
                operation,
                raw_path,
                allowed=False,
                dry_run=False,
                actor=actor,
                reason="file suffix is not enabled for text reads",
                payload={"suffix": resolved.suffix.lower()},
            )
            raise VaultAccessError("Vault file suffix is not enabled for text reads.")

    def _entry_for(self, path: Path) -> VaultEntry:
        stat = path.stat()
        return VaultEntry(
            path=self._relative_path(path),
            kind="directory" if path.is_dir() else "file",
            size_bytes=stat.st_size,
            modified_at=_format_timestamp(stat.st_mtime),
        )

    def _relative_path(self, path: Path) -> str:
        for root in self._vault_roots:
            if _is_relative_to(path, root):
                return path.relative_to(root).as_posix()
        return str(path)

    def _audit(
        self,
        operation: str,
        path: Path | str,
        *,
        allowed: bool,
        dry_run: bool,
        actor: str | None,
        reason: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        record = VaultAuditRecord(
            timestamp=datetime.now(UTC).isoformat(),
            actor=actor or self._config.actor,
            operation=operation,
            path=str(path),
            allowed=allowed,
            dry_run=dry_run,
            reason=reason,
            payload=payload or {},
        )
        self.audit_records.append(record)
        if self._audit_log_path is not None:
            self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_log_path.open("a", encoding="utf-8") as audit_file:
                audit_file.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def _parse_frontmatter(markdown: str) -> dict[str, Any]:
    if not markdown.startswith("---\n"):
        return {}
    parts = markdown.split("---", 2)
    if len(parts) < 3:
        return {}
    parsed = yaml.safe_load(parts[1]) or {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _unified_content_diff(
    before: str,
    after: str,
    *,
    fromfile: str,
    tofile: str,
) -> str:
    return "".join(
        unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
    )


def _plan_risk(action: str, byte_count: int) -> str:
    if action == "create" and byte_count <= 64 * 1024:
        return "low"
    if byte_count <= 256 * 1024:
        return "medium"
    return "high"


def _format_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
