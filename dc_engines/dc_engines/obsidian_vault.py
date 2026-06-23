"""Controlled read-only Obsidian vault automation boundary."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


class VaultAccessError(PermissionError):
    """Raised when a vault operation violates the security policy."""


@dataclass(frozen=True)
class VaultSecurityPolicy:
    """Security policy for a single configured Obsidian vault root."""

    vault_root: Path
    audit_log_path: Path | None = None
    read_only: bool = True
    allowed_write_paths: tuple[str, ...] = ()
    actor: str = "dc-agent"

    def __post_init__(self) -> None:
        root = self.vault_root.expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"vault_root must be an existing directory: {root}")
        object.__setattr__(self, "vault_root", root)
        if self.audit_log_path is not None:
            object.__setattr__(
                self,
                "audit_log_path",
                self.audit_log_path.expanduser().resolve(),
            )


@dataclass(frozen=True)
class VaultEntry:
    path: str
    name: str
    kind: str
    size: int


@dataclass(frozen=True)
class VaultNote:
    path: str
    content: str
    frontmatter: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VaultSearchHit:
    path: str
    line_number: int
    line: str


@dataclass(frozen=True)
class VaultWritePlan:
    operation: str
    target_path: str
    dry_run: bool
    allowed: bool
    audit_id: str


class ObsidianVault:
    """Safe wrapper for Phase 1 vault reads and audited dry-run writes."""

    def __init__(self, policy: VaultSecurityPolicy) -> None:
        self.policy = policy

    def list_directory(self, relative_path: str | Path = ".") -> list[VaultEntry]:
        directory = self._resolve_vault_path(relative_path)
        if not directory.is_dir():
            raise VaultAccessError(f"not a directory: {relative_path}")

        entries: list[VaultEntry] = []
        for child in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
            if not self._is_inside_vault(child.resolve()):
                continue
            stat = child.stat()
            entries.append(
                VaultEntry(
                    path=self._relative_to_vault(child),
                    name=child.name,
                    kind="directory" if child.is_dir() else "file",
                    size=stat.st_size,
                )
            )
        return entries

    def read_note(self, relative_path: str | Path) -> VaultNote:
        note_path = self._resolve_vault_path(relative_path)
        if not note_path.is_file() or note_path.suffix.lower() != ".md":
            raise VaultAccessError(f"not a markdown note: {relative_path}")
        content = note_path.read_text(encoding="utf-8")
        frontmatter, _body = parse_frontmatter(content)
        return VaultNote(
            path=self._relative_to_vault(note_path),
            content=content,
            frontmatter=frontmatter,
        )

    def search_notes(
        self,
        query: str,
        *,
        relative_path: str | Path = ".",
        case_sensitive: bool = False,
        limit: int = 50,
    ) -> list[VaultSearchHit]:
        if not query:
            return []

        root = self._resolve_vault_path(relative_path)
        if root.is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = sorted(root.rglob("*.md"))
        else:
            raise VaultAccessError(f"not a searchable path: {relative_path}")

        needle = query if case_sensitive else query.casefold()
        hits: list[VaultSearchHit] = []
        for note_path in candidates:
            resolved_note = note_path.resolve()
            if not self._is_inside_vault(resolved_note):
                continue
            text = resolved_note.read_text(encoding="utf-8")
            for line_number, line in enumerate(text.splitlines(), start=1):
                haystack = line if case_sensitive else line.casefold()
                if needle in haystack:
                    hits.append(
                        VaultSearchHit(
                            path=self._relative_to_vault(resolved_note),
                            line_number=line_number,
                            line=line,
                        )
                    )
                    if len(hits) >= limit:
                        return hits
        return hits

    def read_backlinks(self, target_note: str | Path) -> list[VaultSearchHit]:
        target_path = self._resolve_vault_path(target_note)
        if not target_path.is_file() or target_path.suffix.lower() != ".md":
            raise VaultAccessError(f"not a markdown note: {target_note}")

        target_keys = {
            target_path.stem.casefold(),
            target_path.with_suffix("")
            .relative_to(self.policy.vault_root)
            .as_posix()
            .casefold(),
        }
        hits: list[VaultSearchHit] = []
        wikilink_pattern = re.compile(r"\[\[([^\]|#]+)")
        for note_path in sorted(self.policy.vault_root.rglob("*.md")):
            resolved_note = note_path.resolve()
            if not self._is_inside_vault(resolved_note) or resolved_note == target_path:
                continue
            text = resolved_note.read_text(encoding="utf-8")
            for line_number, line in enumerate(text.splitlines(), start=1):
                link_targets = {
                    match.group(1).strip().removesuffix(".md").casefold()
                    for match in wikilink_pattern.finditer(line)
                }
                if target_keys & link_targets:
                    hits.append(
                        VaultSearchHit(
                            path=self._relative_to_vault(resolved_note),
                            line_number=line_number,
                            line=line,
                        )
                    )
        return hits

    def create_note(
        self,
        relative_path: str | Path,
        content: str,
        *,
        actor: str | None = None,
        dry_run: bool = True,
    ) -> VaultWritePlan:
        del content
        target_path = self._resolve_vault_path(relative_path, must_exist=False)
        allowed = (
            target_path.suffix.lower() == ".md"
            and self._is_write_allowlisted(target_path)
            and not self.policy.read_only
        )
        result = "dry_run" if dry_run else "denied"
        audit_id = self._audit(
            actor=actor or self.policy.actor,
            action="create_note",
            target_path=self._relative_to_vault(target_path),
            dry_run=dry_run,
            allowed=allowed,
            result=result,
        )
        if dry_run:
            return VaultWritePlan(
                operation="create_note",
                target_path=self._relative_to_vault(target_path),
                dry_run=True,
                allowed=allowed,
                audit_id=audit_id,
            )
        raise VaultAccessError("Phase 1 vault wrapper does not execute writes")

    def _resolve_vault_path(
        self,
        relative_path: str | Path,
        *,
        must_exist: bool = True,
    ) -> Path:
        path = Path(relative_path)
        if path.is_absolute():
            raise VaultAccessError("vault operations require vault-relative paths")
        candidate = self.policy.vault_root / path
        try:
            resolved = candidate.resolve(strict=must_exist)
        except FileNotFoundError as exc:
            raise VaultAccessError(
                f"vault path does not exist: {relative_path}"
            ) from exc
        if not self._is_inside_vault(resolved):
            raise VaultAccessError(
                f"vault path escapes configured root: {relative_path}"
            )
        return resolved

    def _is_inside_vault(self, path: Path) -> bool:
        try:
            path.relative_to(self.policy.vault_root)
        except ValueError:
            return False
        return True

    def _relative_to_vault(self, path: Path) -> str:
        return path.resolve(strict=False).relative_to(self.policy.vault_root).as_posix()

    def _is_write_allowlisted(self, target_path: Path) -> bool:
        target_relative = self._relative_to_vault(target_path)
        for prefix in self.policy.allowed_write_paths:
            normalized = str(PureVaultPath(prefix))
            if target_relative == normalized or target_relative.startswith(
                f"{normalized}/"
            ):
                return True
        return False

    def _audit(
        self,
        *,
        actor: str,
        action: str,
        target_path: str,
        dry_run: bool,
        allowed: bool,
        result: str,
    ) -> str:
        if self.policy.audit_log_path is None:
            raise VaultAccessError("write planning requires an audit_log_path")
        timestamp = datetime.now(UTC).isoformat()
        audit_id = f"obsidian-vault:{timestamp}:{action}:{target_path}"
        event = {
            "audit_id": audit_id,
            "timestamp": timestamp,
            "actor": actor,
            "action": action,
            "target_path": target_path,
            "dry_run": dry_run,
            "allowed": allowed,
            "result": result,
        }
        self.policy.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.policy.audit_log_path.open("a", encoding="utf-8") as audit_log:
            audit_log.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            audit_log.write("\n")
        return audit_id


class PureVaultPath:
    """Normalize allowlist prefixes as relative POSIX vault paths."""

    def __init__(self, path: str) -> None:
        pure = Path(path)
        if pure.is_absolute() or ".." in pure.parts:
            raise VaultAccessError(
                "write allowlist entries must be relative vault paths"
            )
        self.path = pure.as_posix().strip("/")

    def __str__(self) -> str:
        return self.path


def parse_frontmatter(markdown: str) -> tuple[dict[str, Any], str]:
    """Return YAML frontmatter and body from an Obsidian Markdown note."""

    if not markdown.startswith("---\n"):
        return {}, markdown
    try:
        _prefix, yaml_text, body = markdown.split("---", 2)
    except ValueError as exc:
        raise ValueError("frontmatter block is not closed") from exc
    parsed = yaml.safe_load(yaml_text) or {}
    if not isinstance(parsed, dict):
        raise ValueError("frontmatter must be a mapping")
    return parsed, body.lstrip()
