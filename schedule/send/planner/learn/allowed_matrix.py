"""Derive the allowable-resource matrices: which machine may build / which press
may cure each GT.

This is the master the plant would normally supply (`masters/allowed_machine_matrix`).
It was defined as a schema stub in `data/masters.py`, never derived and never
read; `plan/building.py` and `plan/curing.py` each substituted "resources that
handled this exact GT in the visible history".

That substitution is wrong, and measurably so. In Jan-2026 the plant used:

  * 40-45 % machine-GT pairs that did not exist in December (37 % of TBR volume)
  * 43-47 % press-GT pairs that did not exist in December (30-32 % of volume)

So last month's assignment log is a *preference*, not a feasibility set. Hard
gating on it forces all demand through roughly half the resource pairs the plant
really uses, which overloads the eligible few while capacity idles elsewhere.

Feasibility here is physical capability:
  a resource may handle GT g if it has handled g, OR any GT of the same SIZE
  (the mould/size class is what actually constrains it).

`allowed` distinguishes the two so callers can still prefer direct evidence:
  'direct' - has handled this exact GT
  'size'   - has handled the same size class
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from planner.config import CONFIG
from planner.data.warehouse import duck
from planner.runs.logger import log


def _gt_sizes(timing) -> dict[str, str]:
    con = duck()
    out: dict[str, str] = {}
    for sql in ("SELECT DISTINCT itemCode FROM v_build WHERE stage = 2 AND itemCode IS NOT NULL",):
        for (gt,) in con.execute(sql).fetchall():
            s = timing._size_for_gt(gt)
            if s:
                out[gt] = s
    return out


def build_matrices(timing) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Return (machine_matrix, press_matrix) honouring the active as-of cutoff."""
    con = duck()
    size_of = _gt_sizes(timing)

    def widen(pairs: list[tuple[str, str, str]], label: str) -> pl.DataFrame:
        """pairs = (plant, gt, resource) observed directly."""
        direct = {(p, g, r) for p, g, r in pairs}
        # resource -> sizes it has handled
        res_sizes: dict[tuple[str, str], set[str]] = {}
        for p, g, r in pairs:
            s = size_of.get(g)
            if s:
                res_sizes.setdefault((p, r), set()).add(s)
        gts = {(p, g) for p, g, _ in pairs}
        rows = []
        for (p, g) in gts:
            s = size_of.get(g)
            for (pp, r), sizes in res_sizes.items():
                if pp != p:
                    continue
                if (p, g, r) in direct:
                    rows.append({"plant": p, "gt_code": g, "resource": r, "basis": "direct"})
                elif s and s in sizes:
                    rows.append({"plant": p, "gt_code": g, "resource": r, "basis": "size"})
        df = pl.DataFrame(rows) if rows else pl.DataFrame(
            schema={"plant": pl.Utf8, "gt_code": pl.Utf8, "resource": pl.Utf8, "basis": pl.Utf8})
        if df.height:
            n_direct = df.filter(pl.col("basis") == "direct").height
            log.info(f"allowed_matrix.{label}", pairs=df.height, direct=n_direct,
                     widened=df.height - n_direct)
        return df

    mach = con.execute("""
        SELECT DISTINCT plant, itemCode, machineCode FROM v_build
        WHERE stage = 2 AND QualityStatus = '1'
          AND itemCode IS NOT NULL AND machineCode IS NOT NULL
    """).fetchall()
    press = con.execute("""
        SELECT DISTINCT b.plant, b.itemCode, c.wcID::VARCHAR
        FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
        WHERE b.stage = 2 AND c.statuscritical = 'Normal' AND b.itemCode IS NOT NULL
    """).fetchall()
    return widen(mach, "machine"), widen(press, "press")


def run(timing, out_dir: Path | None = None) -> dict[str, int]:
    m, p = build_matrices(timing)
    d = out_dir or (CONFIG.paths.warehouse / "derived")
    d.mkdir(parents=True, exist_ok=True)
    if m.height:
        m.write_parquet(d / "allowed_machine_matrix.parquet", compression="zstd")
    if p.height:
        p.write_parquet(d / "allowed_press_matrix.parquet", compression="zstd")
    return {"machine_pairs": m.height, "press_pairs": p.height}
