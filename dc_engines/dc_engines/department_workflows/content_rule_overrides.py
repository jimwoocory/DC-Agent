from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DC_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RULE_OVERRIDES_PATH = (
    DC_ROOT / "data" / "config" / "content_sop_rule_overrides.json"
)
DEFAULT_RULE_BACKUP_DIR = (
    DC_ROOT / "data" / "temp" / "content_sop_rule_overrides_backups"
)
SUPPORTED_APPROVED_STATUSES = {"approved_for_runtime"}


class ContentSopRuleOverrideError(RuntimeError):
    """Raised when runtime SOP rule override config cannot be trusted."""


@dataclass(frozen=True, slots=True)
class ContentSopRuleApplyResult:
    applied: bool
    rule_id: str
    version: int
    dry_run: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "rule_id": self.rule_id,
            "version": self.version,
            "dry_run": self.dry_run,
            "reason": self.reason,
        }


def load_content_sop_rule_overrides(
    path: Path | str | None = None,
) -> dict[str, Any]:
    config_path = Path(path) if path is not None else DEFAULT_RULE_OVERRIDES_PATH
    if not config_path.exists():
        return _empty_config()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ContentSopRuleOverrideError(
            f"cannot read content SOP rule override config: {config_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ContentSopRuleOverrideError(
            f"malformed content SOP rule override config: {config_path}"
        ) from exc
    if not isinstance(data, dict):
        raise ContentSopRuleOverrideError(
            f"invalid content SOP rule override config root: {config_path}"
        )
    data.setdefault("version", 1)
    data.setdefault("rules", [])
    data.setdefault("audit", [])
    if not isinstance(data["version"], int):
        raise ContentSopRuleOverrideError(
            f"invalid content SOP rule override version: {config_path}"
        )
    if not isinstance(data["rules"], list):
        raise ContentSopRuleOverrideError(
            f"invalid content SOP rule override rules: {config_path}"
        )
    for index, rule in enumerate(data["rules"]):
        _validate_rule_entry(rule, config_path=config_path, index=index)
    if not isinstance(data["audit"], list):
        raise ContentSopRuleOverrideError(
            f"invalid content SOP rule override audit: {config_path}"
        )
    return data


def apply_rule_proposal_to_overrides(
    proposal: dict[str, Any],
    *,
    path: Path | str | None = None,
    actor: str,
    now: str,
    dry_run: bool = False,
) -> ContentSopRuleApplyResult:
    """Apply an approved stable proposal to the versioned runtime override file."""

    proposal_id = str(proposal.get("proposal_id") or "").strip()
    status = str(proposal.get("status") or "").strip()
    rule_id = _rule_id(proposal)
    config = load_content_sop_rule_overrides(path)
    version = int(config.get("version") or 1)

    if not proposal_id:
        return ContentSopRuleApplyResult(
            applied=False,
            rule_id=rule_id,
            version=version,
            dry_run=dry_run,
            reason="missing proposal_id",
        )
    if status not in SUPPORTED_APPROVED_STATUSES:
        return ContentSopRuleApplyResult(
            applied=False,
            rule_id=rule_id,
            version=version,
            dry_run=dry_run,
            reason="proposal is not approved for runtime",
        )

    existing = _find_rule(config, proposal_id)
    if existing is not None:
        if existing.get("enabled") is False:
            existing["enabled"] = True
            existing["updated_at"] = now
            existing["updated_by"] = actor
            existing["rollback_of"] = ""
            version += 1
        else:
            return ContentSopRuleApplyResult(
                applied=False,
                rule_id=str(existing.get("rule_id") or rule_id),
                version=version,
                dry_run=dry_run,
                reason="proposal already applied",
            )
    else:
        config["rules"].append(_rule_from_proposal(proposal, actor=actor, now=now))
        version += 1

    if dry_run:
        return ContentSopRuleApplyResult(
            applied=True,
            rule_id=rule_id,
            version=version,
            dry_run=True,
            reason="dry_run",
        )

    config["version"] = version
    config["audit"].append(
        {
            "action": "apply_rule_proposal",
            "actor": actor,
            "proposal_id": proposal_id,
            "rule_id": rule_id,
            "created_at": now,
        }
    )
    _save_config(config, path)
    return ContentSopRuleApplyResult(applied=True, rule_id=rule_id, version=version)


def rollback_rule_override(
    proposal_id: str,
    *,
    path: Path | str | None = None,
    actor: str,
    now: str,
) -> ContentSopRuleApplyResult:
    config = load_content_sop_rule_overrides(path)
    version = int(config.get("version") or 1)
    existing = _find_rule(config, proposal_id)
    if existing is None:
        return ContentSopRuleApplyResult(
            applied=False,
            rule_id="",
            version=version,
            reason="proposal rule not found",
        )
    if existing.get("enabled") is False:
        return ContentSopRuleApplyResult(
            applied=False,
            rule_id=str(existing.get("rule_id") or ""),
            version=version,
            reason="rule already disabled",
        )

    existing["enabled"] = False
    existing["updated_at"] = now
    existing["updated_by"] = actor
    existing["rollback_of"] = proposal_id
    version += 1
    config["version"] = version
    config["audit"].append(
        {
            "action": "rollback_rule_override",
            "actor": actor,
            "proposal_id": proposal_id,
            "rule_id": str(existing.get("rule_id") or ""),
            "created_at": now,
        }
    )
    _save_config(config, path)
    return ContentSopRuleApplyResult(
        applied=True,
        rule_id=str(existing.get("rule_id") or ""),
        version=version,
    )


def matching_content_sop_rules(
    payload: dict[str, Any],
    *,
    path: Path | str | None = None,
) -> list[dict[str, Any]]:
    config = load_content_sop_rule_overrides(path)
    department_id = str(payload.get("department_id") or "")
    scenario_id = str(payload.get("scenario_id") or "")
    rules: list[dict[str, Any]] = []
    for rule in config.get("rules") or []:
        if not isinstance(rule, dict) or rule.get("enabled") is False:
            continue
        if str(rule.get("department_id") or "") != department_id:
            continue
        rule_scenario = str(rule.get("scenario_id") or "")
        if rule_scenario and rule_scenario != scenario_id:
            continue
        rules.append(
            {
                "rule_id": str(rule.get("rule_id") or ""),
                "proposal_id": str(rule.get("proposal_id") or ""),
                "rule_type": str(rule.get("rule_type") or "process"),
                "rule_text": str(rule.get("rule_text") or ""),
                "source_candidate_ids": list(rule.get("source_candidate_ids") or []),
                "applied_at": str(rule.get("applied_at") or ""),
            }
        )
    return rules


def attach_content_sop_rule_overrides(
    payload: dict[str, Any],
    *,
    path: Path | str | None = None,
) -> dict[str, Any]:
    rules = matching_content_sop_rules(payload, path=path)
    if not rules:
        return payload
    payload["runtime_sop_rule_overrides"] = rules
    truth_requirements = list(payload.get("truth_requirements") or [])
    for rule in rules:
        rule_text = str(rule.get("rule_text") or "").strip()
        if rule_text and rule_text not in truth_requirements:
            truth_requirements.append(rule_text)
    payload["truth_requirements"] = truth_requirements
    return payload


def _rule_from_proposal(
    proposal: dict[str, Any],
    *,
    actor: str,
    now: str,
) -> dict[str, Any]:
    return {
        "rule_id": _rule_id(proposal),
        "proposal_id": str(proposal.get("proposal_id") or ""),
        "department_id": str(proposal.get("department_id") or ""),
        "scenario_id": str(proposal.get("scenario_id") or ""),
        "rule_type": str(proposal.get("rule_type") or "process"),
        "rule_text": str(proposal.get("rule_text") or ""),
        "source_candidate_ids": list(proposal.get("evidence_candidate_ids") or []),
        "support_count": int(proposal.get("support_count") or 0),
        "enabled": True,
        "applied_at": now,
        "applied_by": actor,
        "updated_at": now,
        "updated_by": actor,
        "rollback_of": "",
    }


def _rule_id(proposal: dict[str, Any]) -> str:
    proposal_id = str(proposal.get("proposal_id") or "").strip()
    if proposal_id:
        return f"content_sop_rule_{proposal_id}"
    return "content_sop_rule_unknown"


def _find_rule(config: dict[str, Any], proposal_id: str) -> dict[str, Any] | None:
    for rule in config.get("rules") or []:
        if isinstance(rule, dict) and str(rule.get("proposal_id") or "") == proposal_id:
            return rule
    return None


def _empty_config() -> dict[str, Any]:
    return {"version": 1, "rules": [], "audit": []}


def _validate_rule_entry(rule: Any, *, config_path: Path, index: int) -> None:
    if not isinstance(rule, dict):
        raise ContentSopRuleOverrideError(
            f"invalid content SOP rule entry at index {index}: {config_path}"
        )
    required_strings = ("rule_id", "proposal_id", "department_id", "rule_text")
    for key in required_strings:
        if not isinstance(rule.get(key), str) or not rule.get(key, "").strip():
            raise ContentSopRuleOverrideError(
                f"invalid content SOP rule entry {key} at index {index}: {config_path}"
            )
    if "scenario_id" in rule and not isinstance(rule.get("scenario_id"), str):
        raise ContentSopRuleOverrideError(
            f"invalid content SOP rule entry scenario_id at index {index}: {config_path}"
        )
    if not isinstance(rule.get("enabled"), bool):
        raise ContentSopRuleOverrideError(
            f"invalid content SOP rule entry enabled at index {index}: {config_path}"
        )
    if "source_candidate_ids" in rule and not isinstance(
        rule.get("source_candidate_ids"), list
    ):
        raise ContentSopRuleOverrideError(
            f"invalid content SOP rule entry source_candidate_ids at index {index}: {config_path}"
        )


def _save_config(config: dict[str, Any], path: Path | str | None) -> None:
    config_path = Path(path) if path is not None else DEFAULT_RULE_OVERRIDES_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        previous_text = config_path.read_text(encoding="utf-8")
        previous_version = _previous_version(previous_text)
        backup_dir = _backup_dir(config_path)
        backup_dir.mkdir(parents=True, exist_ok=True)
        rollback = backup_dir / f"{config_path.name}.rollback-from-v{previous_version}"
        rollback.write_text(previous_text, encoding="utf-8")
    tmp_dir = _backup_dir(config_path)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{config_path.name}.tmp"
    tmp.write_text(
        json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(config_path)


def _previous_version(text: str) -> int:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return 0
    if not isinstance(data, dict):
        return 0
    try:
        return int(data.get("version") or 0)
    except (TypeError, ValueError):
        return 0


def _backup_dir(config_path: Path) -> Path:
    if config_path == DEFAULT_RULE_OVERRIDES_PATH:
        return DEFAULT_RULE_BACKUP_DIR
    return config_path.parent / "content_sop_rule_overrides_backups"
