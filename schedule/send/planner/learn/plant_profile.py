"""Mine the plant's operating envelope from the full MES history.

Produces the *input files* and the *rule values* the planning engine is held to:
how the plant actually runs, per plant (PCR/TBR), for building and curing.

Everything here is descriptive of observed behaviour -- no targets are invented.
A rule value is only emitted if the data supports it, and each carries the
sample size it was measured from.

Shifts follow the plant convention: A 07:00-15:00, B 15:00-23:00, C 23:00-07:00,
so a "shift day" starts at 07:00 (a tyre built 02:00 belongs to the previous
day's C shift).

Output: warehouse/derived/plant_profile.json  (+ per-topic parquet)
"""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from planner.config import CONFIG
from planner.data.warehouse import duck
from planner.runs.logger import log

# Shift-day expression: hours before 07:00 belong to the previous shift-day.
SHIFT_DAY = "CAST(event_ts - INTERVAL 7 HOUR AS DATE)"
SHIFT_ID = ("CASE WHEN hour(event_ts) >= 7 AND hour(event_ts) < 15 THEN 'A' "
            "WHEN hour(event_ts) >= 15 AND hour(event_ts) < 23 THEN 'B' "
            "ELSE 'C' END")

BUILD_SRC = ("SELECT plant, itemCode AS sku, machineCode AS res, event_ts, "
             f"{SHIFT_DAY} AS d, {SHIFT_ID} AS shift "
             "FROM v_build WHERE stage = 2 AND QualityStatus = '1' "
             "AND itemCode IS NOT NULL AND machineCode IS NOT NULL")

CURE_SRC = ("SELECT c.plant, b.itemCode AS sku, c.wcID::VARCHAR AS res, c.event_ts, "
            f"{SHIFT_DAY.replace('event_ts', 'c.event_ts')} AS d, "
            f"{SHIFT_ID.replace('event_ts', 'c.event_ts')} AS shift "
            "FROM v_curing c JOIN v_build b ON c.gtbarCode = b.productionID "
            "WHERE b.stage = 2 AND c.statuscritical = 'Normal' AND b.itemCode IS NOT NULL")


def _q(con, sql: str) -> pl.DataFrame:
    return con.execute(sql).pl()


def _stats(df: pl.DataFrame, col: str) -> dict:
    if df.height == 0:
        return {}
    s = df[col].drop_nulls()
    if s.len() == 0:
        return {}
    return {
        "n": int(s.len()),
        "mean": round(float(s.mean()), 2),
        "p50": round(float(s.median()), 2),
        "p90": round(float(s.quantile(0.90)), 2),
        "p95": round(float(s.quantile(0.95)), 2),
        "min": round(float(s.min()), 2),
        "max": round(float(s.max()), 2),
    }


def _stage_profile(con, src: str, stage: str) -> dict:
    """All per-stage metrics, split by plant."""
    out: dict[str, dict] = {}
    base = f"WITH e AS ({src})"

    # --- resource counts and volume -------------------------------------
    vol = _q(con, f"""{base}
        SELECT plant, count(*) AS tyres, count(DISTINCT res) AS resources,
               count(DISTINCT sku) AS skus, count(DISTINCT d) AS days
        FROM e GROUP BY 1""")

    # --- run length (campaign / lot size) via gaps-and-islands ----------
    runs = _q(con, f"""{base},
        r AS (SELECT plant, res, sku, d, shift,
                     row_number() OVER (PARTITION BY plant, res ORDER BY event_ts)
                   - row_number() OVER (PARTITION BY plant, res, sku ORDER BY event_ts) AS grp
              FROM e)
        SELECT plant, res, sku, min(d) AS d, count(*) AS run_len
        FROM r GROUP BY plant, res, sku, grp""")

    # --- changeovers per resource per day and per shift ------------------
    co = _q(con, f"""{base},
        s AS (SELECT plant, res, d, shift, sku,
                     lag(sku) OVER (PARTITION BY plant, res ORDER BY event_ts) AS prev
              FROM e)
        SELECT plant, res, d, shift,
               sum(CASE WHEN prev IS NOT NULL AND prev <> sku THEN 1 ELSE 0 END) AS cos
        FROM s GROUP BY 1,2,3,4""")

    # --- stickiness: share of consecutive tyres that keep the same sku ---
    stick = _q(con, f"""{base},
        s AS (SELECT plant, sku,
                     lag(sku) OVER (PARTITION BY plant, res ORDER BY event_ts) AS prev
              FROM e)
        SELECT plant,
               100.0 * sum(CASE WHEN prev = sku THEN 1 ELSE 0 END)
                     / nullif(sum(CASE WHEN prev IS NOT NULL THEN 1 ELSE 0 END), 0) AS stickiness_pct
        FROM s GROUP BY 1""")

    # --- variety: skus per resource-day, resources per sku, skus per day -
    spd = _q(con, f"""{base}
        SELECT plant, res, d, count(DISTINCT sku) AS n FROM e GROUP BY 1,2,3""")
    rps = _q(con, f"""{base}
        SELECT plant, sku, count(DISTINCT res) AS n FROM e GROUP BY 1,2""")
    upd = _q(con, f"""{base}
        SELECT plant, d, count(DISTINCT sku) AS n FROM e GROUP BY 1,2""")

    # --- daily output per plant and per resource ------------------------
    dq = _q(con, f"""{base}
        SELECT plant, d, count(*) AS n FROM e GROUP BY 1,2""")
    rq = _q(con, f"""{base}
        SELECT plant, res, d, count(*) AS n FROM e GROUP BY 1,2,3""")

    # --- cadence: seconds between consecutive tyres on a resource -------
    cad = _q(con, f"""{base},
        g AS (SELECT plant, res, date_diff('second',
                  lag(event_ts) OVER (PARTITION BY plant, res ORDER BY event_ts),
                  event_ts) AS gap FROM e)
        SELECT plant, res, median(gap) AS s_per_unit
        FROM g WHERE gap BETWEEN 1 AND 7200 GROUP BY 1,2""")

    for plant in sorted(vol["plant"].to_list()):
        f = lambda df: df.filter(pl.col("plant") == plant)  # noqa: E731
        v = f(vol).to_dicts()[0]
        co_day = (f(co).group_by(["res", "d"]).agg(pl.col("cos").sum().alias("cos")))
        co_plant_day = (f(co).group_by("d").agg(pl.col("cos").sum().alias("cos")))
        daily = f(dq)
        cv = (float(daily["n"].std() / daily["n"].mean())
              if daily.height > 1 and daily["n"].mean() else 0.0)
        out[plant] = {
            "tyres": int(v["tyres"]), "resources": int(v["resources"]),
            "skus": int(v["skus"]), "days": int(v["days"]),
            "run_length_units": _stats(f(runs), "run_len"),
            "changeovers_per_resource_day": _stats(co_day, "cos"),
            "changeovers_per_resource_shift": _stats(f(co), "cos"),
            "changeovers_per_plant_day": _stats(co_plant_day, "cos"),
            "stickiness_pct": round(float(f(stick)["stickiness_pct"][0] or 0), 2),
            "skus_per_resource_day": _stats(f(spd), "n"),
            "resources_per_sku": _stats(f(rps), "n"),
            "unique_skus_per_day": _stats(f(upd), "n"),
            "daily_output_plant": _stats(daily, "n"),
            "daily_output_per_resource": _stats(f(rq), "n"),
            "daily_output_cv": round(cv, 3),
            "seconds_per_unit": _stats(f(cad), "s_per_unit"),
        }
    return out


def _size_lock(con) -> dict:
    """Share of consecutive builds on a machine that keep the same tyre size.

    Size comes from the construction mapping (PCR, via gt_code_updated) or from
    the GT code itself (TBR codes are size-led).
    """
    from planner.data.plant_masters import _size_of
    gt_size: dict[str, str] = {}
    for sql in ("SELECT gt_code_updated, size FROM v_construction_pcr "
                "WHERE gt_code_updated IS NOT NULL AND size IS NOT NULL",
                "SELECT gt_code, description FROM v_construction_tbr "
                "WHERE gt_code IS NOT NULL AND description IS NOT NULL"):
        try:
            for gt, raw in con.execute(sql).fetchall():
                s = _size_of(raw)
                if gt and s:
                    gt_size.setdefault(str(gt).strip(), s)
        except Exception:  # noqa: BLE001
            continue
    df = _q(con, f"""WITH e AS ({BUILD_SRC}),
        s AS (SELECT plant, sku, lag(sku) OVER (PARTITION BY plant, res ORDER BY event_ts) AS prev
              FROM e)
        SELECT plant, sku, prev, count(*) AS n FROM s WHERE prev IS NOT NULL GROUP BY 1,2,3""")

    def size_of(gt: str) -> str | None:
        s = gt_size.get(gt)
        if s:
            return s
        return _size_of(gt) if gt and gt[:1].isdigit() else None

    out: dict[str, dict] = {}
    for plant in sorted(df["plant"].unique().to_list()):
        sub = df.filter(pl.col("plant") == plant)
        same = tot = known = 0
        for r in sub.iter_rows(named=True):
            a, b = size_of(r["prev"]), size_of(r["sku"])
            tot += r["n"]
            if a and b:
                known += r["n"]
                if a == b:
                    same += r["n"]
        out[plant] = {
            "size_lock_pct": round(100.0 * same / known, 2) if known else None,
            "transitions": tot, "size_known_pct": round(100.0 * known / tot, 2) if tot else 0,
        }
    return out


def _gt_inventory(con) -> dict:
    """Daily GT balance and the build->cure lag the plant actually achieves."""
    lag = _q(con, """
        SELECT b.plant,
               date_diff('second', b.event_ts, c.event_ts) / 3600.0 AS lag_h
        FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
        WHERE b.stage = 2 AND b.QualityStatus = '1'
          AND c.statuscritical = 'Normal' AND c.event_ts >= b.event_ts""")
    daily = _q(con, f"""
        WITH b AS (SELECT plant, {SHIFT_DAY} AS d, count(*) AS built
                   FROM v_build WHERE stage = 2 AND QualityStatus = '1' GROUP BY 1,2),
             c AS (SELECT plant, {SHIFT_DAY} AS d, count(*) AS cured
                   FROM v_curing WHERE statuscritical = 'Normal' GROUP BY 1,2)
        SELECT coalesce(b.plant, c.plant) AS plant, coalesce(b.d, c.d) AS d,
               coalesce(built, 0) AS built, coalesce(cured, 0) AS cured
        FROM b FULL OUTER JOIN c ON b.plant = c.plant AND b.d = c.d ORDER BY 1,2""")
    out: dict[str, dict] = {}
    for plant in sorted(daily["plant"].drop_nulls().unique().to_list()):
        d = daily.filter(pl.col("plant") == plant).sort("d").with_columns(
            (pl.col("built") - pl.col("cured")).cum_sum().alias("bal"))
        out[plant] = {
            "build_to_cure_lag_h": _stats(lag.filter(pl.col("plant") == plant), "lag_h"),
            "daily_built": _stats(d, "built"),
            "daily_cured": _stats(d, "cured"),
            "gt_balance_running": _stats(d, "bal"),
        }
    return out


def run(out_dir: Path | None = None) -> dict:
    con = duck()
    log.info("plant_profile.start")
    profile = {
        "building": _stage_profile(con, BUILD_SRC, "building"),
        "curing": _stage_profile(con, CURE_SRC, "curing"),
        "size_lock": _size_lock(con),
        "gt_inventory": _gt_inventory(con),
    }
    span = con.execute(
        "SELECT min(date), max(date), count(*) FROM v_build WHERE stage = 2").fetchone()
    profile["_meta"] = {"from": str(span[0]), "to": str(span[1]), "build_rows": int(span[2])}

    d = out_dir or (CONFIG.paths.warehouse / "derived")
    d.mkdir(parents=True, exist_ok=True)
    (d / "plant_profile.json").write_text(json.dumps(profile, indent=2, default=str),
                                          encoding="utf-8")
    log.info("plant_profile.done", path=str(d / "plant_profile.json"))
    return profile


if __name__ == "__main__":
    p = run()
    print(json.dumps(p, indent=2, default=str))
