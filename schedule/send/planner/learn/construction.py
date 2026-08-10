"""CONSTRUCTION-AWARE SEQUENCING -- learn what a changeover costs, then group.

The engine has always optimised "fewer changeovers". That treats a tread-code
swap and a full carcass rebuild as the same event. Measured on 6,422 MES TBR
changeovers, they are not:

    components changed    1      2      3      7     10     13
    setup p50 (min)     5.4    8.3   10.8   15.6   17.3   20.8

Monotone, r = +0.404, a 3.9x spread the sequencer cannot see.

PIPELINE
  1. learn w_k, minutes per component, by NNLS on MES gaps
  2. distance d(i,j) = sum_k w_k * delta_k   -- MINUTES, not a count, so the
     clustering objective and the scheduling objective are the same object
  3. complete-linkage clustering -> families (bounds the WORST intra-family
     pair; single linkage would chain and destroy the guarantee)
  4. sequence

WHY MES GAPS AND NOT OUR OWN setup_s: `setup_s` in build_schedule comes from the
changeover-time master, so regressing on it would recover the master rather than
learn anything. The MES inter-tyre gap at a GT change is an independent
observation: same-GT gap p50 205 s (the build cadence) against 580 s at a GT
change.

CEILING, stated honestly: r = 0.404 means construction explains ~16% of setup
variance. The rest is operator, shift, material and mould logistics. These
weights are good enough to RANK changeovers, not to predict any single one.
"""
from __future__ import annotations

import numpy as np
import polars as pl

from planner.runs.logger import log

COMPONENTS = ["PRE ASSEMBLY", "NYLON-1", "NYLON2&3", "GUM STRIP",
              "STEEL CHIPPER LEFT", "STEEL CHIPPER RIGHT", "BODYPLY",
              "SHOULDER PAD", "APEXED BEAD", "BELT-1", "BELT-2",
              "BELT EDGE FILLER", "BELT-3", "BELT-4", "Tread Code"]
MIN_OBS_PER_COMPONENT = 30


def signatures(path) -> dict[str, tuple]:
    d = pl.read_parquet(path)
    return {r["gt_code"]: tuple((r[c] or "") for c in COMPONENTS)
            for r in d.iter_rows(named=True)}


def learn_weights(sig: dict[str, tuple], plant: str = "TBR") -> dict:
    """NNLS fit of setup minutes onto component-change indicators.

    w_k >= 0 is physical -- no component change can shorten a setup. NNLS also
    damps the multicollinearity from components that always co-change
    (belt/tread, ply/carcass).
    """
    from planner.data.warehouse import duck
    q = duck().execute(
        "WITH s AS (SELECT machineCode m, itemCode g, event_ts, "
        "  lag(itemCode) OVER (PARTITION BY machineCode ORDER BY event_ts) pg, "
        "  date_diff('second', lag(event_ts) OVER "
        "    (PARTITION BY machineCode ORDER BY event_ts), event_ts) gap "
        "  FROM v_build WHERE stage=2 AND plant=? AND itemCode IS NOT NULL "
        "  AND machineCode IS NOT NULL) "
        "SELECT pg, g, gap FROM s "
        "WHERE pg IS NOT NULL AND g <> pg AND gap BETWEEN 60 AND 14400",
        [plant]).pl()
    X, y = [], []
    for r in q.iter_rows(named=True):
        a, b = sig.get(r["pg"]), sig.get(r["g"])
        if a is None or b is None:
            continue
        X.append([1.0] + [1.0 if x != z else 0.0 for x, z in zip(a, b)])
        y.append(float(r["gap"]) / 60.0)          # minutes
    if len(X) < 200:
        log.warning("construction.too_few_obs", n=len(X))
        return {}
    A = np.asarray(X)
    b = np.asarray(y)
    from scipy.optimize import nnls
    w, _res = nnls(A, b)
    pred = A @ w
    ss_res = float(((b - pred) ** 2).sum())
    ss_tot = float(((b - b.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot else 0.0
    varies = A[:, 1:].sum(axis=0)
    out = {"intercept_min": float(w[0]), "r2": round(r2, 3), "n": len(X),
           "weights_min": {c: round(float(v), 2)
                           for c, v in zip(COMPONENTS, w[1:])},
           "obs_varying": {c: int(v) for c, v in zip(COMPONENTS, varies)}}
    # a component that never varies cannot be identified -- say so rather than
    # letting a fitted zero pretend to be knowledge
    out["unidentifiable"] = [c for c, v in zip(COMPONENTS, varies)
                             if v < MIN_OBS_PER_COMPONENT]
    log.info("construction.weights", r2=out["r2"], n=out["n"],
             intercept_min=round(out["intercept_min"], 2),
             unidentifiable=len(out["unidentifiable"]))
    return out


def distance_fn(sig: dict[str, tuple], weights: dict):
    w = [weights["weights_min"].get(c, 1.0) for c in COMPONENTS]

    def d(a: str, b: str) -> float | None:
        sa, sb = sig.get(a), sig.get(b)
        if sa is None or sb is None:
            return None
        return sum(wk for wk, x, y in zip(w, sa, sb) if x != y)
    return d


def families(gts: list[str], d, theta: float) -> dict[str, int]:
    """Complete-linkage agglomerative clustering, cut at theta MINUTES.

    Complete linkage bounds the WORST pair inside a family:
        D(A,B) = max over i in A, j in B of d(i,j)
    so every intra-family changeover is guaranteed under theta. Single linkage
    would chain -- A near B, B near C, A far from C all in one family -- and an
    intra-family changeover would then cost as much as a between-family one,
    which is the entire property we are buying.
    """
    gts = sorted(gts)
    n = len(gts)
    if n <= 1:
        return {g: i for i, g in enumerate(gts)}
    cl = {i: [g] for i, g in enumerate(gts)}
    D: dict[tuple[int, int], float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            v = d(gts[i], gts[j])
            D[(i, j)] = float("inf") if v is None else v
    while len(cl) > 1:
        best = None
        keys = sorted(cl)
        for ii in range(len(keys)):
            for jj in range(ii + 1, len(keys)):
                a, b = keys[ii], keys[jj]
                link = max(D[(min(x, y), max(x, y))]
                           for x in [gts.index(g) for g in cl[a]]
                           for y in [gts.index(g) for g in cl[b]])
                if best is None or link < best[0]:
                    best = (link, a, b)
        if best is None or best[0] > theta:
            break
        _v, a, b = best
        cl[a] = cl[a] + cl[b]
        del cl[b]
    out: dict[str, int] = {}
    for fid, (_k, members) in enumerate(sorted(cl.items())):
        for g in members:
            out[g] = fid
    return out


def order_atsp(items: list[str], d, start: str | None = None) -> list[str]:
    """Nearest-neighbour + 2-opt on the asymmetric distance. Deterministic.

    Exact would be affordable at these sizes, but NN+2-opt is within a few
    percent and keeps the tie-breaks trivially reproducible.
    """
    if len(items) <= 2:
        return sorted(items)
    todo = sorted(items)
    cur = start if start in todo else todo[0]
    seq = [cur]
    todo.remove(cur)
    while todo:
        nxt = min(todo, key=lambda x: ((d(seq[-1], x) if d(seq[-1], x)
                                        is not None else 1e9), x))
        seq.append(nxt)
        todo.remove(nxt)
    improved = True
    while improved:
        improved = False
        for i in range(1, len(seq) - 1):
            for j in range(i + 1, len(seq)):
                a, b = seq[i - 1], seq[i]
                c = seq[j]
                e = seq[j + 1] if j + 1 < len(seq) else None
                cur_c = (d(a, b) or 0) + ((d(c, e) or 0) if e else 0)
                new_c = (d(a, c) or 0) + ((d(b, e) or 0) if e else 0)
                if new_c < cur_c - 1e-9:
                    seq[i:j + 1] = reversed(seq[i:j + 1])
                    improved = True
    return seq
