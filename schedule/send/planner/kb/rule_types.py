"""Rule schema shared across learn/kb/plan layers."""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RuleType(str, Enum):
    HARD = "hard"
    SOFT = "soft"
    STAT = "stat"


class Rule(BaseModel):
    rule_id: str
    scope: str                       # 'sku','sku_pair','machine','sku_machine','sequence','sister_group','global'
    statement: dict[str, Any]        # {predicate: str, params: {...}}
    support: int
    confidence: float
    exception_rate: float = 0.0
    sample_size: int = 0
    ci_low: float = 0.0
    ci_high: float = 1.0
    p_value: float = 1.0
    type: RuleType = RuleType.STAT
    weight: float = 1.0
    provenance: dict[str, Any] = Field(default_factory=dict)
    first_seen: date | None = None
    last_seen: date | None = None
    active: bool = True
