"""Decision trace helpers — attach explainability to each scheduled lot."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TraceReason(BaseModel):
    rule_id: str
    type: str            # 'hard' | 'soft' | 'stat'
    role: str            # 'machine_preference' | 'sister_batch' | 'sequence' | ...
    weight: float = 1.0


class TraceRelaxation(BaseModel):
    rule_id: str
    penalty: float


class DecisionTrace(BaseModel):
    chosen_because: list[TraceReason] = Field(default_factory=list)
    constraints_satisfied: list[str] = Field(default_factory=list)
    constraints_relaxed: list[TraceRelaxation] = Field(default_factory=list)
    alternatives_considered: int = 0
    objective_delta: float = 0.0

    def add_reason(self, rule_id: str, type: str, role: str, weight: float = 1.0) -> None:
        self.chosen_because.append(TraceReason(rule_id=rule_id, type=type, role=role, weight=weight))

    def add_satisfied(self, rule_id: str) -> None:
        self.constraints_satisfied.append(rule_id)

    def relax(self, rule_id: str, penalty: float) -> None:
        self.constraints_relaxed.append(TraceRelaxation(rule_id=rule_id, penalty=penalty))

    def model_dump_flat(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
