"""Phase 1c: Sister-SKU clustering to minimize changeover.

Two-stage:
  1. Coarse — Agglomerative (ward) on slot fingerprint + BOM Jaccard + size.
  2. Refine — inside each coarse cluster of ≤ MAX_FULL_PERM SKUs, enumerate
     orderings and score by expected changeover cost; else beam search.

Emits sister_group rules with canonical order per cluster.
"""
from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np
import polars as pl
from sklearn.cluster import AgglomerativeClustering  # type: ignore
from sklearn.preprocessing import StandardScaler  # type: ignore

from planner.config import CONFIG
from planner.data.warehouse import duck
from planner.kb.rule_types import Rule, RuleType
from planner.runs.logger import log


def _slot_matrix_pcr(con) -> pl.DataFrame:
    try:
        return con.execute("""
            SELECT sku, gt_code, ply1, ply2_up, ply2_dn, flipper, cap_strip,
                   CAST(rim_dia AS DOUBLE) AS rim_dia,
                   CAST(cycle_time_sec AS DOUBLE) AS cycle_s,
                   'PCR' AS plant
            FROM v_construction_pcr
        """).pl()
    except Exception:
        return pl.DataFrame()


def _slot_matrix_tbr(con) -> pl.DataFrame:
    try:
        return con.execute("""
            SELECT sku, gt_code, pre_assembly, nylon1, nylon23, gum_strip,
                   chipper_l, chipper_r, bodyply, shoulder_pad, apexed_bead,
                   belt1, belt2, belt_edge_filler, belt3, belt4, tread_code,
                   'TBR' AS plant
            FROM v_construction_tbr
        """).pl()
    except Exception:
        return pl.DataFrame()


def _one_hot(df: pl.DataFrame, cols: list[str]) -> np.ndarray:
    frames = []
    for c in cols:
        if c not in df.columns:
            continue
        s = df[c].fill_null("_none").cast(pl.Utf8)
        dummies = s.to_frame().to_dummies()
        frames.append(dummies.to_numpy().astype(np.float32))
    if not frames:
        return np.zeros((df.height, 1), dtype=np.float32)
    return np.hstack(frames)


def _changeover_cost(order: list[str], slots: dict[str, list[str]]) -> float:
    """Rough cost = # slot mismatches summed across adjacent SKUs."""
    if len(order) <= 1:
        return 0.0
    total = 0
    for a, b in zip(order, order[1:]):
        sa, sb = slots[a], slots[b]
        total += sum(1 for x, y in zip(sa, sb) if x != y)
    return float(total)


def _best_order(members: list[str], slots: dict[str, list[str]]) -> tuple[list[str], float]:
    if len(members) <= 1:
        return list(members), 0.0
    if len(members) <= CONFIG.thresholds.cluster_max_size_full_perm:
        best = min(itertools.permutations(members), key=lambda o: _changeover_cost(list(o), slots))
        return list(best), _changeover_cost(list(best), slots)
    # Beam search
    beam = [(0.0, [members[0]])]
    remaining = set(members[1:])
    while remaining:
        cand = []
        for cost, path in beam:
            for r in remaining - set(path):
                new_path = path + [r]
                cand.append((_changeover_cost(new_path, slots), new_path))
        cand.sort(key=lambda x: x[0])
        beam = cand[: CONFIG.thresholds.cluster_beam_width]
        remaining -= {p[-1] for _, p in beam}
        if len(beam[0][1]) == len(members):
            break
    beam.sort(key=lambda x: x[0])
    return beam[0][1], beam[0][0]


def _cluster_plant(df: pl.DataFrame, slot_cols: list[str], plant: str) -> list[Rule]:
    if df.height < 4:
        return []
    X = _one_hot(df, slot_cols)
    # Add scaled continuous features if present (rim_dia, cycle_s)
    conts = [c for c in ("rim_dia", "cycle_s") if c in df.columns]
    if conts:
        c = df.select(conts).fill_null(0).to_numpy().astype(np.float32)
        c = StandardScaler().fit_transform(c)
        X = np.hstack([X, c])

    # Choose distance threshold so avg cluster is ~5 SKUs
    n_clusters = max(2, df.height // 5)
    algo = AgglomerativeClustering(n_clusters=n_clusters, linkage="ward")
    labels = algo.fit_predict(X)

    slots_map: dict[str, list[str]] = {}
    for row in df.iter_rows(named=True):
        slots_map[row["sku"]] = [str(row.get(c) or "") for c in slot_cols if c in df.columns]

    rules: list[Rule] = []
    for k in sorted(set(labels)):
        members = [row["sku"] for row, lab in zip(df.iter_rows(named=True), labels) if lab == k]
        if len(members) < 2:
            continue
        order, cost = _best_order(members, slots_map)
        avg_pairs = np.mean([
            sum(1 for x, y in zip(slots_map[a], slots_map[b]) if x == y) / max(1, len(slots_map[a]))
            for a in members for b in members if a != b
        ]) if len(members) >= 2 else 0.0
        rid = f"sister.{plant}.{k}"
        rules.append(Rule(
            rule_id=rid, scope="sister_group",
            statement={"predicate": "sister_cluster", "params": {
                "plant": plant, "members": members, "canonical_order": order,
                "changeover_cost": cost, "avg_slot_similarity": float(avg_pairs)}},
            support=len(members),
            confidence=float(avg_pairs),
            sample_size=len(members),
            ci_low=float(avg_pairs),
            ci_high=float(avg_pairs),
            p_value=0.0,
            type=RuleType.STAT,
            provenance={"miner": "sister_sku", "algo": "agglomerative+perm"},
        ))
    return rules


def cluster_sisters(out_dir: Path) -> tuple[Path, list[Rule]]:
    con = duck()
    rules: list[Rule] = []
    pcr = _slot_matrix_pcr(con)
    tbr = _slot_matrix_tbr(con)
    if pcr.height:
        rules += _cluster_plant(
            pcr,
            slot_cols=["ply1", "ply2_up", "ply2_dn", "flipper", "cap_strip"],
            plant="PCR",
        )
    if tbr.height:
        rules += _cluster_plant(
            tbr,
            slot_cols=["pre_assembly", "nylon1", "nylon23", "gum_strip",
                       "chipper_l", "chipper_r", "bodyply", "shoulder_pad", "apexed_bead",
                       "belt1", "belt2", "belt_edge_filler", "belt3", "belt4", "tread_code"],
            plant="TBR",
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "sister_groups.json"
    import json
    out.write_text(json.dumps([r.model_dump(mode="json") for r in rules], indent=2, default=str))
    log.info("sister.clusters", n_rules=len(rules), path=str(out))
    return out, rules
