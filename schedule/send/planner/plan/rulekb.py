"""Rulebase loader — reads a rules.duckdb produced by `make learn` and exposes
typed indices used by the planner (MPM, sister-groups, sequences, timing)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from planner.runs.logger import log


@dataclass
class MPMEntry:
    plant: str
    stage: int
    sku: str
    machine: str
    confidence: float
    support: int
    type: str
    weight: float
    rule_id: str


@dataclass
class SisterGroup:
    plant: str
    members: list[str]
    canonical_order: list[str]
    changeover_cost: float
    similarity: float
    rule_id: str


@dataclass
class SequenceRule:
    plant: str
    stage: int
    pattern: list[str]
    confidence: float
    support: int
    rule_id: str


@dataclass
class CycleTimeStat:
    plant: str
    recipe: int
    press: str
    mean_s: float
    p50_s: float
    p95_s: float


@dataclass
class AgingStat:
    plant: str
    gt_code: str
    min_wait_s: float
    p50_wait_s: float
    p95_wait_s: float


@dataclass
class QualityDemotion:
    plant: str
    press: str
    gt_code: str
    defect_rate: float
    rule_id: str


@dataclass
class TestFreqRule:
    sku: str
    frequency_days: int
    category: str
    test_type: str


@dataclass
class RuleBase:
    mpm: dict[tuple[str, int, str], list[MPMEntry]] = field(default_factory=dict)
    sisters: dict[str, list[SisterGroup]] = field(default_factory=dict)  # plant -> groups
    sequences: dict[tuple[str, int], list[SequenceRule]] = field(default_factory=dict)
    cycles: dict[tuple[str, int, str], CycleTimeStat] = field(default_factory=dict)  # (plant,recipe,press)
    aging: dict[tuple[str, str], AgingStat] = field(default_factory=dict)  # (plant, gt_code)
    quality_demotions: dict[tuple[str, str, str], QualityDemotion] = field(default_factory=dict)
    test_freq: dict[str, TestFreqRule] = field(default_factory=dict)
    # Membership: sku -> sister-group rule_id
    sku_to_sister: dict[tuple[str, str], SisterGroup] = field(default_factory=dict)

    def machines_for(self, plant: str, stage: int, sku: str) -> list[MPMEntry]:
        return sorted(
            self.mpm.get((plant, stage, sku), []),
            key=lambda e: (-e.confidence, -e.support),
        )

    def sister_of(self, plant: str, sku: str) -> SisterGroup | None:
        return self.sku_to_sister.get((plant, sku))


def load_rules(kb_path: Path) -> RuleBase:
    con = duckdb.connect(str(kb_path), read_only=True)
    rb = RuleBase()

    # MPM entries
    for row in con.execute("""
        SELECT rule_id, scope, statement, confidence, support, type, weight
        FROM rules WHERE scope = 'sku_machine'
    """).fetchall():
        rid, scope, stmt_json, conf, supp, rtype, weight = row
        stmt = json.loads(stmt_json) if isinstance(stmt_json, str) else stmt_json
        p = stmt.get("params", {})
        e = MPMEntry(
            plant=p["plant"], stage=int(p["stage"]), sku=p["sku"], machine=p["machine"],
            confidence=float(conf), support=int(supp), type=rtype, weight=float(weight),
            rule_id=rid,
        )
        rb.mpm.setdefault((e.plant, e.stage, e.sku), []).append(e)

    # Sister groups
    for row in con.execute("""
        SELECT rule_id, statement, confidence FROM rules WHERE scope = 'sister_group'
    """).fetchall():
        rid, stmt_json, conf = row
        stmt = json.loads(stmt_json) if isinstance(stmt_json, str) else stmt_json
        p = stmt.get("params", {})
        sg = SisterGroup(
            plant=p["plant"], members=list(p["members"]),
            canonical_order=list(p["canonical_order"]),
            changeover_cost=float(p.get("changeover_cost", 0)),
            similarity=float(p.get("avg_slot_similarity", conf)),
            rule_id=rid,
        )
        rb.sisters.setdefault(sg.plant, []).append(sg)
        for m in sg.members:
            rb.sku_to_sister[(sg.plant, m)] = sg

    # Sequence patterns
    for row in con.execute("""
        SELECT rule_id, statement, confidence, support FROM rules WHERE scope = 'sequence'
    """).fetchall():
        rid, stmt_json, conf, supp = row
        stmt = json.loads(stmt_json) if isinstance(stmt_json, str) else stmt_json
        p = stmt.get("params", {})
        sr = SequenceRule(
            plant=p["plant"], stage=int(p["stage"]),
            pattern=list(p["pattern"]),
            confidence=float(conf), support=int(supp),
            rule_id=rid,
        )
        rb.sequences.setdefault((sr.plant, sr.stage), []).append(sr)

    # Cycle-time stat rules
    for row in con.execute("""
        SELECT rule_id, statement FROM rules WHERE scope = 'cycle_time'
    """).fetchall():
        rid, stmt_json = row
        stmt = json.loads(stmt_json) if isinstance(stmt_json, str) else stmt_json
        p = stmt.get("params", {})
        try:
            cs = CycleTimeStat(
                plant=p["plant"], recipe=int(p["recipe"]),
                press=str(p["press"]),
                mean_s=float(p.get("mean_s") or 0),
                p50_s=float(p.get("p50_s") or 0),
                p95_s=float(p.get("p95_s") or 0),
            )
            rb.cycles[(cs.plant, cs.recipe, cs.press)] = cs
        except (TypeError, KeyError):
            continue

    # Aging stat rules
    for row in con.execute("""
        SELECT rule_id, statement FROM rules WHERE scope = 'aging'
    """).fetchall():
        rid, stmt_json = row
        stmt = json.loads(stmt_json) if isinstance(stmt_json, str) else stmt_json
        p = stmt.get("params", {})
        try:
            a = AgingStat(
                plant=p["plant"], gt_code=p["gt_code"],
                min_wait_s=float(p["min_wait_s"]),
                p50_wait_s=float(p["p50_wait_s"]),
                p95_wait_s=float(p["p95_wait_s"]),
            )
            rb.aging[(a.plant, a.gt_code)] = a
        except (TypeError, KeyError):
            continue

    # Quality demotions from balance signal
    for row in con.execute("""
        SELECT rule_id, statement FROM rules WHERE scope = 'sku_machine_quality'
    """).fetchall():
        rid, stmt_json = row
        stmt = json.loads(stmt_json) if isinstance(stmt_json, str) else stmt_json
        p = stmt.get("params", {})
        try:
            q = QualityDemotion(
                plant=p["plant"], press=str(p["press"]),
                gt_code=p["gt_code"], defect_rate=float(p["defect_rate"]),
                rule_id=rid,
            )
            rb.quality_demotions[(q.plant, q.press, q.gt_code)] = q
        except (TypeError, KeyError):
            continue

    # Test SKU frequency
    for row in con.execute("""
        SELECT rule_id, statement FROM rules WHERE scope = 'test_sku'
    """).fetchall():
        rid, stmt_json = row
        stmt = json.loads(stmt_json) if isinstance(stmt_json, str) else stmt_json
        p = stmt.get("params", {})
        try:
            t = TestFreqRule(
                sku=p["sku"], frequency_days=int(p["frequency_days"]),
                category=str(p.get("category", "")),
                test_type=str(p.get("test_type", "")),
            )
            rb.test_freq[t.sku] = t
        except (TypeError, KeyError):
            continue

    # Apply balance demotions to MPM: halve the confidence weight of any
    # (plant, machine=press, gt_code) hit by a quality demotion.
    if rb.quality_demotions:
        for entries in rb.mpm.values():
            for e in entries:
                if (e.plant, e.machine, e.sku) in rb.quality_demotions:
                    e.weight *= 0.5

    log.info("kb.loaded",
             mpm=sum(len(v) for v in rb.mpm.values()),
             sisters=sum(len(v) for v in rb.sisters.values()),
             sequences=sum(len(v) for v in rb.sequences.values()),
             cycles=len(rb.cycles),
             aging=len(rb.aging),
             quality_demotions=len(rb.quality_demotions),
             test_freq=len(rb.test_freq))
    return rb
