from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

RiskSeverity = Literal["high", "medium", "low", "info"]


@dataclass(slots=True)
class CognitionRisk:
    severity: RiskSeverity
    area: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    recommendation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "area": self.area,
            "message": self.message,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
        }


@dataclass(slots=True)
class CompanyCognitionReport:
    generated_at: str
    verdict: str
    components: dict[str, Any]
    coverage: dict[str, float | None]
    risks: list[CognitionRisk] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "verdict": self.verdict,
            "components": self.components,
            "coverage": self.coverage,
            "risks": [risk.to_dict() for risk in self.risks],
            "recommendations": self.recommendations,
        }
