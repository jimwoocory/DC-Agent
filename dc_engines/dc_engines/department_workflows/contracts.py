from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MaterialStatus = Literal["needs_materials", "partial", "ready"]


@dataclass(frozen=True, slots=True)
class RequiredInput:
    key: str
    label: str
    description: str = ""
    required: bool = True
    examples: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OutputSpec:
    key: str
    label: str
    description: str = ""
    format_hint: str = ""


@dataclass(frozen=True, slots=True)
class Scenario:
    scenario_id: str
    name: str
    description: str
    keywords: tuple[str, ...]
    required_inputs: tuple[RequiredInput, ...]
    expected_outputs: tuple[OutputSpec, ...]
    truth_requirements: tuple[str, ...] = ()
    material_requirements: tuple[str, ...] = ()
    test_task_directions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DepartmentWorkflow:
    department_id: str
    department_name: str
    aliases: tuple[str, ...]
    functional_focus: tuple[str, ...]
    training_focus: tuple[str, ...]
    scenarios: tuple[Scenario, ...]


@dataclass(frozen=True, slots=True)
class DepartmentWorkflowMatch:
    workflow: DepartmentWorkflow
    scenario: Scenario
    score: int
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def department_id(self) -> str:
        return self.workflow.department_id

    @property
    def scenario_id(self) -> str:
        return self.scenario.scenario_id


@dataclass(frozen=True, slots=True)
class MaterialIntakeAssessment:
    status: MaterialStatus
    provided_signals: tuple[str, ...] = ()
    missing_required_inputs: tuple[RequiredInput, ...] = ()

    @property
    def needs_followup(self) -> bool:
        return self.status != "ready"
