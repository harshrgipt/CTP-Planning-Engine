"""Generate the COMPLETE set of planner input masters as files.

Everything the planner needs is written to `warehouse/derived/` so that planning
reads declared inputs instead of re-deriving them ad-hoc mid-run. Each file is
either taken from a plant-supplied master (preferred) or derived from MES, and
every derived file records how it was obtained.

Why this exists: several inputs were never materialised at all -- most damagingly
`allowed_machine_matrix`, which `data/masters.py` declared but nothing ever
loaded, so `plan/building.py` silently substituted "machines that built this GT
last month". That covers only 57 % of the machine-GT pairs the plant actually
uses (47 % for presses), which is what starves the planner of routing options.

**Cutoff policy — the distinction that matters:**

  * CAPABILITY / TOPOLOGY (allowed matrices, cycle times, daily capacity, lot
    size, gt->size, calendar) is derived from the FULL 8-month history. These
    describe what the plant physically *can* do; a plant-supplied master would
    state them in full regardless of which month is being planned, so using all
    history is not leakage -- it is the master file we are standing in for. It
    also matters practically: one month of history covers only 57 % of the
    machine-GT pairs the plant actually uses.
  * DEMAND and OPENING INVENTORY are strictly pre-cutoff, since those are
    forward-looking and would leak the answer.

    python -m planner.data.derive_masters [YYYY-MM-DD]

`cutoff` therefore applies only to the opening-inventory file; everything else
is mined over the whole warehouse.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import polars as pl

from planner.config import CONFIG
from planner.data.warehouse import duck
from planner.runs.logger import log

OUT = CONFIG.paths.warehouse / "derived"


def _w(df: pl.DataFrame, name: str, how: str, manifest: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if df.height == 0:
        log.warning("derive_masters.empty", file=name)
        manifest[name] = {"rows": 0, "source": how}
        return
    df.write_parquet(OUT / f"{name}.parquet", compression="zstd")
    manifest[name] = {"rows": df.height, "source": how}
    log.info("derive_masters.written", file=name, rows=df.height)


def derive(cutoff: date | None = None) -> dict:
    from planner.data.warehouse import set_cutoff
    # Capability masters: FULL history (see module docstring). Only the opening
    # inventory below re-applies the cutoff.
    set_cutoff(None)
    con = duck()
    manifest: dict = {"capability_from": "full 8-month history",
                      "demand_cutoff": str(cutoff)}

    # ---- 1. cycle time: building, per machine -------------------------------
    _w(con.execute("""
        WITH g AS (SELECT plant, machineCode AS machine,
                          date_diff('second', lag(event_ts) OVER
                              (PARTITION BY plant, machineCode ORDER BY event_ts),
                              event_ts) AS gap
                   FROM v_build WHERE stage = 2 AND QualityStatus = '1')
        SELECT plant, machine, median(gap) AS s_per_tyre, count(*) AS samples
        FROM g WHERE gap BETWEEN 1 AND 3600 GROUP BY 1, 2
    """).pl(), "cycle_time_building", "MES median inter-event gap", manifest)

    # ---- 2. cycle time: curing, per press (sustained throughput) ------------
    _w(con.execute("""
        SELECT plant, wcID::VARCHAR AS press,
               date_diff('second', min(event_ts), max(event_ts))::DOUBLE
                   / NULLIF(count(*) - 1, 0) AS s_per_tyre,
               count(*) AS samples
        FROM v_curing WHERE statuscritical = 'Normal'
        GROUP BY 1, 2 HAVING count(*) > 10
    """).pl(), "cycle_time_curing", "MES span/tyres (matches plant-stated 13.5k/day)",
       manifest)

    # ---- 3. daily capacity per resource ------------------------------------
    _w(con.execute("""
        WITH d AS (SELECT plant, machineCode AS machine,
                          CAST(event_ts - INTERVAL 7 HOUR AS DATE) AS day, count(*) AS n
                   FROM v_build WHERE stage = 2 AND QualityStatus = '1' GROUP BY 1,2,3)
        SELECT plant, machine, median(n) AS p50, quantile_cont(n,0.95) AS p95, max(n) AS mx
        FROM d GROUP BY 1,2
    """).pl(), "capacity_machine_day", "MES daily output distribution", manifest)

    _w(con.execute("""
        WITH d AS (SELECT plant, wcID::VARCHAR AS press,
                          CAST(event_ts - INTERVAL 7 HOUR AS DATE) AS day, count(*) AS n
                   FROM v_curing WHERE statuscritical = 'Normal' GROUP BY 1,2,3)
        SELECT plant, press, median(n) AS p50, quantile_cont(n,0.95) AS p95, max(n) AS mx
        FROM d GROUP BY 1,2
    """).pl(), "capacity_press_day", "MES daily output distribution", manifest)

    # ---- 4. lot size = the plant's own uninterrupted run --------------------
    _w(con.execute("""
        WITH ev AS (SELECT plant, itemCode AS gt_code, machineCode AS machine, event_ts,
                           row_number() OVER (PARTITION BY plant, machineCode ORDER BY event_ts)
                         - row_number() OVER (PARTITION BY plant, machineCode, itemCode
                                              ORDER BY event_ts) AS grp
                    FROM v_build WHERE stage = 2 AND QualityStatus = '1'),
             runs AS (SELECT plant, gt_code, grp, count(*) AS n
                      FROM ev GROUP BY 1,2,3)
        SELECT plant, gt_code, median(n) AS lot_p50,
               quantile_cont(n,0.9) AS lot_p90, max(n) AS lot_max
        FROM runs GROUP BY 1,2
    """).pl(), "lot_size", "MES gaps-and-islands campaign length", manifest)

    # ---- 5. allowed machine / press matrices -------------------------------
    from planner.learn.allowed_matrix import build_matrices
    from planner.plan.timing_lookup import TimingLookup
    learn_dirs = sorted(p for p in CONFIG.paths.runs.iterdir()
                        if p.is_dir() and (p / "learn").exists())
    timing = TimingLookup(learn_dirs[-1] / "learn") if learn_dirs else None
    if timing is not None:
        mach, press = build_matrices(timing)
        _w(mach.rename({"resource": "machine"}), "allowed_machine_matrix",
           "MES direct + size-class widening (NEEDS plant master: covers 62%)", manifest)
        _w(press.rename({"resource": "press"}), "allowed_press_matrix",
           "MES direct + size-class widening (NEEDS plant master: covers 50%)", manifest)

        # ---- 6. gt -> size ------------------------------------------------
        gts = [r[0] for r in con.execute(
            "SELECT DISTINCT itemCode FROM v_build WHERE stage = 2 AND itemCode IS NOT NULL"
        ).fetchall()]
        rows = [{"gt_code": g, "size": timing._size_for_gt(g)} for g in gts]
        _w(pl.DataFrame([r for r in rows if r["size"]]), "gt_size",
           "construction mapping + GT-code parse (66% resolvable)", manifest)

    # ---- 7. calendar: 3 shifts, 24/7 (plant-confirmed) ---------------------
    _w(pl.DataFrame({
        "shift": ["A", "B", "C"],
        "start_hour": [7, 15, 23],
        "duration_min": [480, 480, 480],
    }), "calendar_shifts", "plant-confirmed 3 shifts 24/7", manifest)

    # ---- 8. opening GT inventory (CUTOFF APPLIES -- forward-looking) -------
    if cutoff is not None:
        set_cutoff(cutoff)
        _w(con.execute("""
            WITH built AS (SELECT plant, itemCode AS gt_code, productionID AS bar, event_ts
                           FROM v_build WHERE stage = 2 AND QualityStatus = '1'),
                 cured AS (SELECT gtbarCode AS bar FROM v_curing
                           WHERE statuscritical = 'Normal')
            SELECT b.plant, b.gt_code, count(*) AS qty, max(b.event_ts) AS latest_built
            FROM built b LEFT JOIN cured c ON b.bar = c.bar
            WHERE c.bar IS NULL GROUP BY 1,2
        """).pl(), "opening_gt_inventory", "MES built-not-cured before cutoff", manifest)

    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str),
                                       encoding="utf-8")
    set_cutoff(None)
    return manifest


if __name__ == "__main__":
    cut = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else None
    m = derive(cut)
    print(json.dumps(m, indent=2, default=str))
