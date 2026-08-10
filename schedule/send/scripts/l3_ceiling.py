"""L3 -- TRUE UTILISATION AND THROUGHPUT CEILING.  Closes GAP-5, sizes GAP-3.

    python scripts/l3_ceiling.py

Ships before anything else is built (Corrected_Planning_Architecture_v2 §11 step 1).
It gives every subsequent plan a denominator and settles §2.1 empirically.

WHY RESOURCE-DAY OCCUPANCY IS NOT ENOUGH
  Phase 0 reported build occupancy 100.0% / 99.9% and concluded building is the
  bottleneck.  That statistic only says every machine did SOMETHING every day.
  True utilisation is productive_h / available_h, and it is a different number.

MEASUREMENT
  * cure: one press cycle produces MULTIPLE tyres (multi-cavity).  Busy time must
    be summed over DISTINCT (press, cycle) pairs, never per tyre -- doing it per
    tyre overstates by the cavity count (a real 3.3x error made earlier).
    Cycle duration = cycleStart - event_ts  (source names are backwards:
    event_ts is press-CLOSE = cure start, cycleStart is press-OPEN = cure end).
  * build: productive time = tyres x per-tyre cadence, where cadence is the
    within-run median inter-event gap.  Gaps beyond IDLE_CUT are idle, not slow.
  * available time is calendar (24 h x active days).  Any PM/breakdown calendar
    would only LOWER available and RAISE utilisation, so these figures are a
    lower bound on true utilisation -- stated, not assumed.
"""
from __future__ import annotations

import numpy as np
import polars as pl

from planner.data.warehouse import duck, set_cutoff

PLANTS = ["PCR", "TBR"]
SEP = "=" * 94
IDLE_CUT_S = 1800.0        # a build gap beyond 30 min is idle, not a slow tyre


def hdr(t: str) -> None:
    print("\n" + SEP)
    print(t)
    print(SEP)


# ---------------------------------------------------------------------------
def build_util() -> dict:
    """Productive build hours vs calendar, per machine."""
    q = duck().execute(f"""
        WITH s AS (
          SELECT plant, machineCode AS m, event_ts, date,
                 date_diff('second',
                   lag(event_ts) OVER (PARTITION BY plant, machineCode
                                       ORDER BY event_ts), event_ts) AS gap
          FROM v_build
          WHERE stage = 2 AND machineCode IS NOT NULL AND itemCode IS NOT NULL)
        SELECT plant, m,
               count(*)                                         AS tyres,
               count(DISTINCT date)                             AS days,
               median(gap) FILTER (WHERE gap BETWEEN 1 AND {IDLE_CUT_S}) AS cadence_s,
               sum(gap)    FILTER (WHERE gap BETWEEN 1 AND {IDLE_CUT_S}) AS busy_s,
               sum(gap)    FILTER (WHERE gap > {IDLE_CUT_S})     AS idle_s,
               count(*)    FILTER (WHERE gap > {IDLE_CUT_S})     AS idle_events
        FROM s GROUP BY 1, 2 ORDER BY 1, 2""").pl()
    out = {}
    hdr("BUILD -- true time utilisation (GAP-5)")
    print(f"  {'plant':<5}{'machine':<10}{'tyres':>10}{'days':>6}{'cadence':>9}"
          f"{'prod h':>9}{'non-prod':>9}{'avail h':>9}{'UTIL':>8}{'idle gaps':>11}")
    for p in PLANTS:
        s = q.filter(pl.col("plant") == p)
        tb = ti = ta = 0.0
        for r in s.iter_rows(named=True):
            # productive = tyres x cadence.  Summing every gap <= IDLE_CUT
            # books changeover and slow-running as work and overstates util.
            busy = r["tyres"] * float(r["cadence_s"] or 0) / 3600.0
            idle = (float(r["busy_s"] or 0) + float(r["idle_s"] or 0)) / 3600.0 - busy
            avail = r["days"] * 24.0
            tb += busy; ti += idle; ta += avail
            print(f"  {p:<5}{r['m'].replace('TBMTBR','TBM').replace('TBMPCR','M').replace('Stage2',''):<10}"
                  f"{r['tyres']:>10,}{r['days']:>6}{float(r['cadence_s'] or 0):>8.0f}s"
                  f"{busy:>9,.0f}{idle:>9,.0f}{avail:>9,.0f}"
                  f"{100*busy/max(avail,1):>7.1f}%{r['idle_events']:>11,}")
        out[p] = {"busy_h": tb, "idle_h": ti, "avail_h": ta,
                  "util": tb / max(ta, 1), "machines": s.height}
        print(f"  {p:<5}{'TOTAL':<10}{'':>10}{'':>6}{'':>9}{tb:>9,.0f}{ti:>9,.0f}"
              f"{ta:>9,.0f}{100*tb/max(ta,1):>7.1f}%")
    return out


# ---------------------------------------------------------------------------
def cure_util() -> dict:
    """Press busy time by INTERVAL UNION per press.

    Co-cured tyres do NOT share an event_ts, so grouping on it counts press
    occupancy once per tyre and yields >100% utilisation.  Merging overlapping
    [event_ts, cycleStart] intervals collapses them correctly and needs no
    assumption about the cavity count -- the count falls out as
    tyres / merged-cycles.
    """
    q = duck().execute("""
        SELECT plant, CAST(wcID AS VARCHAR) AS press, event_ts AS t0,
               cycleStart AS t1, date
        FROM v_curing
        WHERE wcID IS NOT NULL AND cycleStart IS NOT NULL
          AND date_diff('second', event_ts, cycleStart) BETWEEN 60 AND 14400
        """).pl().sort(["plant", "press", "t0"])
    # merge overlapping intervals: a new block starts when t0 > running max end
    q = q.with_columns(
        pl.col("t1").cum_max().shift(1).over(["plant", "press"]).alias("prev_end"))
    q = q.with_columns(
        ((pl.col("prev_end").is_null()) |
         (pl.col("t0") > pl.col("prev_end"))).cum_sum()
        .over(["plant", "press"]).alias("blk"))
    blocks = (q.group_by(["plant", "press", "blk"])
              .agg(pl.col("t0").min().alias("s"), pl.col("t1").max().alias("e"),
                   pl.len().alias("tyres"), pl.col("date").min().alias("d"))
              .with_columns(((pl.col("e") - pl.col("s")).dt.total_seconds())
                            .alias("dur_s")))
    out = {}
    hdr("CURE -- true time utilisation (GAP-5), by interval union per press")
    print(f"  {'plant':<6}{'presses':>9}{'tyres':>12}{'press cycles':>14}"
          f"{'tyres/cycle':>13}{'cycle s':>9}{'busy h':>11}{'avail h':>11}{'UTIL':>8}")
    for p in PLANTS:
        s2 = blocks.filter(pl.col("plant") == p)
        if not s2.height:
            continue
        days = (q.filter(pl.col("plant") == p)
                .group_by("press").agg(pl.col("date").n_unique().alias("dd")))
        busy = float(s2["dur_s"].sum()) / 3600.0
        avail = float(days["dd"].sum()) * 24.0
        npress = int(days.height)
        tyres = int(s2["tyres"].sum())
        cav = tyres / max(s2.height, 1)
        out[p] = {"busy_h": busy, "avail_h": avail, "util": busy / max(avail, 1),
                  "presses": npress, "cycle_s": float(s2["dur_s"].median()),
                  "cav": cav, "tyres": tyres}
        print(f"  {p:<6}{npress:>9}{tyres:>12,}{s2.height:>14,}{cav:>13.2f}"
              f"{float(s2['dur_s'].median()):>9.0f}{busy:>11,.0f}{avail:>11,.0f}"
              f"{100*busy/max(avail,1):>7.1f}%")
    return out


# ---------------------------------------------------------------------------
def ceiling(b: dict, c: dict) -> None:
    """MAX_FEASIBLE per line per week."""
    hdr("THROUGHPUT CEILING -- MAX_FEASIBLE per week")
    print(f"  {'plant':<6}{'cure ceiling':>15}{'build ceiling':>16}"
          f"{'MAX_FEASIBLE':>15}{'actual':>12}{'gap to ceiling':>16}")
    for p in PLANTS:
        if p not in b or p not in c:
            continue
        weeks = b[p]["avail_h"] / b[p]["machines"] / 168.0
        # cure rate from TOTALS, never a ratio of medians -- median block
        # duration divided into median block tyres is not a rate.
        rate_press = c[p]["tyres"] / max(c[p]["busy_h"], 1e-9)   # tyres/press-busy-h
        cure_cap = c[p]["presses"] * 168.0 * rate_press
        # build: machines x 168 h / cadence
        cad = duck().execute(f"""
            WITH s AS (SELECT date_diff('second',
                 lag(event_ts) OVER (PARTITION BY machineCode ORDER BY event_ts),
                 event_ts) g FROM v_build WHERE stage=2 AND plant='{p}'
                 AND machineCode IS NOT NULL)
            SELECT median(g) FROM s WHERE g BETWEEN 1 AND {IDLE_CUT_S}""").fetchone()[0]
        build_cap = b[p]["machines"] * 168.0 * 3600.0 / float(cad)
        mx = min(cure_cap, build_cap)
        actual = c[p]["tyres"] / max(weeks, 1)
        binding = "CURE" if cure_cap < build_cap else "BUILD"
        print(f"  {p:<6}{cure_cap:>15,.0f}{build_cap:>16,.0f}{mx:>15,.0f}"
              f"{actual:>12,.0f}{100*actual/max(mx,1):>15.1f}%")
        print(f"        binding stage: {binding}   "
              f"(cure {c[p]['presses']} presses x {rate_press:.2f} tyres/press-h  |  "
              f"{c[p]['cycle_s']:.0f}s  |  build {b[p]['machines']} machines / "
              f"{float(cad):.0f}s per tyre)")
    print("\n  NOTE: available time is CALENDAR. A PM/breakdown calendar would lower")
    print("        available and raise utilisation, so these are LOWER BOUNDS.")


def main() -> None:
    set_cutoff(None)
    print(SEP)
    print("L3 -- TRUE UTILISATION & THROUGHPUT CEILING   (8 months MES)")
    print(SEP)
    b = build_util()
    c = cure_util()
    ceiling(b, c)
    hdr("VERDICT on the bottleneck (Architecture v2 §2.1)")
    for p in PLANTS:
        if p not in b or p not in c:
            continue
        bu, cu = b[p]["util"], c[p]["util"]
        who = ("BUILDING" if bu > cu + 0.05 else
               "CURING" if cu > bu + 0.05 else "NEITHER -- balanced")
        print(f"  {p}:  build true util {100*bu:.1f}%   cure true util {100*cu:.1f}%"
              f"   ->  constraint: {who}")
    print("\n  Compare with Phase 0 resource-day occupancy (build 100.0/99.9%,")
    print("  cure 90.7/97.4%).  If the ordering differs, the Phase 0 bottleneck")
    print("  finding was an artefact of the weaker statistic and §2 must be revised.")


if __name__ == "__main__":
    main()
