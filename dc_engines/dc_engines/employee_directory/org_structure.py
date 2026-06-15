"""Company organization helpers for employee directory enrichment."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True, frozen=True)
class DepartmentOrgInfo:
    name: str
    parent: str = ""
    path: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


@dataclass(slots=True)
class DepartmentOrgIndex:
    by_name: dict[str, DepartmentOrgInfo] = field(default_factory=dict)
    by_alias: dict[str, DepartmentOrgInfo] = field(default_factory=dict)

    def lookup(self, department: str) -> DepartmentOrgInfo | None:
        key = _normalize(department)
        if not key:
            return None
        return self.by_name.get(key) or self.by_alias.get(key)


def default_org_structure_path() -> Path:
    repo_root = Path(os.environ.get("DC_AGENT_ROOT") or Path(__file__).parents[3])
    return repo_root / "data" / "config" / "company_org_structure.json"


def load_department_org_index(path: str | Path | None = None) -> DepartmentOrgIndex:
    org_path = Path(path) if path is not None else default_org_structure_path()
    if not org_path.exists():
        return DepartmentOrgIndex()
    try:
        raw = json.loads(org_path.read_text(encoding="utf-8"))
    except Exception:
        return DepartmentOrgIndex()
    roots = raw.get("roots") if isinstance(raw, dict) else None
    if not isinstance(roots, list):
        return DepartmentOrgIndex()
    return build_department_org_index(roots)


def build_department_org_index(roots: list[dict[str, Any]]) -> DepartmentOrgIndex:
    index = DepartmentOrgIndex()

    def visit(node: dict[str, Any], path: tuple[str, ...]) -> None:
        name = str(node.get("name") or "").strip()
        if not name:
            return
        aliases = tuple(
            str(alias).strip()
            for alias in (node.get("aliases") or [])
            if str(alias).strip()
        )
        next_path = (*path, name)
        parent = path[-1] if path else ""
        info = DepartmentOrgInfo(
            name=name,
            parent=parent,
            path=next_path,
            aliases=aliases,
        )
        index.by_name[_normalize(name)] = info
        for alias in aliases:
            index.by_alias[_normalize(alias)] = info
        for child in node.get("children") or []:
            if isinstance(child, dict):
                visit(child, next_path)

    for root in roots:
        if isinstance(root, dict):
            visit(root, ())
    return index


def _normalize(value: str) -> str:
    return "".join(str(value or "").lower().split())
