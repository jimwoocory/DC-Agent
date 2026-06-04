from __future__ import annotations

from typing import Any

from .contracts import DepartmentWorkflow, OutputSpec, RequiredInput, Scenario
from .registry import DepartmentWorkflowRegistry, get_default_registry


def workflow_catalog(
    registry: DepartmentWorkflowRegistry | None = None,
) -> list[dict[str, Any]]:
    """Return a JSON-serializable department workflow catalog."""
    active_registry = registry or get_default_registry()
    return [
        _workflow_to_dict(workflow) for workflow in active_registry.list_workflows()
    ]


def _workflow_to_dict(workflow: DepartmentWorkflow) -> dict[str, Any]:
    return {
        "department_id": workflow.department_id,
        "department_name": workflow.department_name,
        "aliases": list(workflow.aliases),
        "functional_focus": list(workflow.functional_focus),
        "training_focus": list(workflow.training_focus),
        "scenarios": [_scenario_to_dict(scenario) for scenario in workflow.scenarios],
    }


def _scenario_to_dict(scenario: Scenario) -> dict[str, Any]:
    return {
        "scenario_id": scenario.scenario_id,
        "name": scenario.name,
        "description": scenario.description,
        "keywords": list(scenario.keywords),
        "required_inputs": [
            _required_input_to_dict(item) for item in scenario.required_inputs
        ],
        "expected_outputs": [
            _output_spec_to_dict(item) for item in scenario.expected_outputs
        ],
        "truth_requirements": list(scenario.truth_requirements),
        "material_requirements": list(scenario.material_requirements),
        "test_task_directions": list(scenario.test_task_directions),
    }


def _required_input_to_dict(item: RequiredInput) -> dict[str, Any]:
    return {
        "key": item.key,
        "label": item.label,
        "description": item.description,
        "required": item.required,
        "examples": list(item.examples),
    }


def _output_spec_to_dict(item: OutputSpec) -> dict[str, Any]:
    return {
        "key": item.key,
        "label": item.label,
        "description": item.description,
        "format_hint": item.format_hint,
    }
