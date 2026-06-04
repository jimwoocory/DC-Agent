from __future__ import annotations

from collections.abc import Iterable, Sequence

from .contracts import DepartmentWorkflow, DepartmentWorkflowMatch, Scenario
from .defaults import DEFAULT_DEPARTMENT_WORKFLOWS

LEGACY_DEPARTMENT_IDS = {
    "marketing": "client_dept",
    "strategy": "planning",
    "branding": "brand_publicity",
    "exec_office": "executive_office",
    "operations": "execution_ops",
    "film": "brand_publicity",
}


class DepartmentWorkflowRegistry:
    def __init__(
        self,
        workflows: Sequence[DepartmentWorkflow] = DEFAULT_DEPARTMENT_WORKFLOWS,
    ) -> None:
        self._workflows = tuple(workflows)
        self._by_id = {workflow.department_id: workflow for workflow in self._workflows}
        for legacy_id, current_id in LEGACY_DEPARTMENT_IDS.items():
            workflow = self._by_id.get(current_id)
            if workflow is not None:
                self._by_id[legacy_id] = workflow

    def list_workflows(self) -> tuple[DepartmentWorkflow, ...]:
        return self._workflows

    def get(self, department_id: str) -> DepartmentWorkflow | None:
        return self._by_id.get(department_id)

    def find_by_department(self, department: str) -> DepartmentWorkflow | None:
        normalized = _normalize(department)
        if not normalized:
            return None
        for workflow in self._workflows:
            if normalized == _normalize(workflow.department_id):
                return workflow
            if _contains_any(normalized, workflow.aliases):
                return workflow
        return None

    def match(
        self,
        *,
        employee_department: str = "",
        text: str = "",
        employee_role: str = "",
        relation_type: str = "",
        min_score: int = 10,
        allow_department_only: bool = False,
    ) -> DepartmentWorkflowMatch | None:
        normalized_text = _normalize(text)
        normalized_department = _normalize(employee_department)
        normalized_role = _normalize(employee_role)
        normalized_relation = _normalize(relation_type)
        best: DepartmentWorkflowMatch | None = None

        for workflow in self._workflows:
            department_score, department_reasons = _score_department(
                workflow,
                normalized_department,
                normalized_role,
                normalized_relation,
            )
            for scenario in workflow.scenarios:
                scenario_score, scenario_reasons = _score_scenario(
                    workflow,
                    scenario,
                    normalized_text,
                )
                if scenario_score <= 0 and not allow_department_only:
                    continue
                score = department_score + scenario_score
                if score < min_score:
                    continue
                reasons = (*department_reasons, *scenario_reasons)
                candidate = DepartmentWorkflowMatch(
                    workflow=workflow,
                    scenario=scenario,
                    score=score,
                    reasons=reasons,
                )
                if best is None or candidate.score > best.score:
                    best = candidate

        return best


DEFAULT_REGISTRY = DepartmentWorkflowRegistry()


def get_default_registry() -> DepartmentWorkflowRegistry:
    return DEFAULT_REGISTRY


def match_department_workflow(
    *,
    employee_department: str = "",
    text: str = "",
    employee_role: str = "",
    relation_type: str = "",
    min_score: int = 10,
    allow_department_only: bool = False,
    registry: DepartmentWorkflowRegistry | None = None,
) -> DepartmentWorkflowMatch | None:
    return (registry or DEFAULT_REGISTRY).match(
        employee_department=employee_department,
        text=text,
        employee_role=employee_role,
        relation_type=relation_type,
        min_score=min_score,
        allow_department_only=allow_department_only,
    )


def _score_department(
    workflow: DepartmentWorkflow,
    department: str,
    role: str,
    relation_type: str,
) -> tuple[int, tuple[str, ...]]:
    score = 0
    reasons: list[str] = []
    if department and _contains_any(department, workflow.aliases):
        score += 40
        reasons.append("department_alias")
    if role and _contains_any(role, workflow.aliases):
        score += 12
        reasons.append("role_alias")
    return score, tuple(reasons)


def _score_scenario(
    workflow: DepartmentWorkflow,
    scenario: Scenario,
    text: str,
) -> tuple[int, tuple[str, ...]]:
    if not text:
        return 0, ()

    score = 0
    reasons: list[str] = []
    keyword_hits = _count_hits(text, scenario.keywords)
    if keyword_hits:
        score += keyword_hits * 12
        reasons.append("scenario_keyword")

    focus_hits = _count_hits(text, workflow.functional_focus)
    if focus_hits:
        score += focus_hits * 4
        reasons.append("functional_focus")

    training_hits = _count_hits(text, workflow.training_focus)
    if training_hits:
        score += training_hits * 5
        reasons.append("training_focus")

    if _contains_any(text, workflow.aliases):
        score += 8
        reasons.append("text_department_alias")

    return score, tuple(reasons)


def _normalize(value: str) -> str:
    return "".join(str(value or "").lower().split())


def _contains_any(text: str, needles: Iterable[str]) -> bool:
    return any(_normalize(needle) in text for needle in needles if _normalize(needle))


def _count_hits(text: str, needles: Iterable[str]) -> int:
    return sum(1 for needle in needles if _normalize(needle) in text)
