"""Heuristic Curing planner â€” VECTORIZED with capacity-aware press spill.

Design:
- Expand `gt_events (source in {build,opening})` into per-tyre lots (one row per GT).
- Weighted-CDF pick of preferred press per (plant, gt_code) from history.
- Capacity balancer: cap each press at historical p95 tyres/day; overflow spills
  to the next-preferred press in the CDF, matching aggregate plant capacity.
- Time-place using cumulative per-press seconds (vectorized within a plant).
- Mould conflicts checked post-hoc via the sync loop.

No per-tyre Python loop. Runs in seconds on a full month.
"""
from __future__ import annotations

import bisect
from datetime import datetime
from pathlib import Path

import polars as pl

from planner.config import CONFIG
from planner.data.warehouse import duck
from planner.plan.decision_trace import DecisionTrace
from planner.plan.ledger import GreenTireLedger
from planner.plan.timing_lookup import TimingLookup
from planner.runs.logger import log


def _historical_press_map() -> pl.DataFrame:
    """Return DataFrame [plant, gt_code, press, n] sorted by (plant, gt_code, -n)."""
    con = duck()
    return con.execute("""
        SELECT b.plant, b.itemCode AS gt_code, c.wcID::VARCHAR AS press, count(*)::BIGINT AS n
        FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
        WHERE b.stage = 2 AND c.statuscritical = 'Normal'
        GROUP BY 1,2,3
    """).pl().sort(["plant", "gt_code", "n"], descending=[False, False, True])


def _press_capacity_per_day() -> dict[tuple[str, str], float]:
    """Historical p95 tyres/day per (plant, press). Feeds capacity balancer."""
    con = duck()
    df = con.execute("""
        WITH d AS (
            SELECT plant, wcID::VARCHAR AS press, date, count(*) AS n
            FROM v_curing WHERE statuscritical = 'Normal'
            GROUP BY 1,2,3
        )
        SELECT plant, press, quantile_cont(n, 0.95) AS cap_p95
        FROM d GROUP BY 1,2
    """).pl()
    return {(r["plant"], r["press"]): float(r["cap_p95"]) for r in df.iter_rows(named=True)}


def _historical_mould_map() -> pl.DataFrame:
    """Return DataFrame [plant, gt_code, press, mould, n] â€” the physical mould
    copy (or copies) historically used on THAT specific press for that GT.
    Each press has its own mould instance in the real plant, so mould-conflict
    is only true when the SAME (plant, gt, press) row appears twice, which is
    prevented by press-time exclusion already."""
    con = duck()
    return con.execute("""
        WITH m AS (
            SELECT b.plant, b.itemCode AS gt_code, c.wcID::VARCHAR AS press,
                   c.MouldCodeLH AS mould
            FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
            WHERE b.stage = 2 AND c.statuscritical = 'Normal' AND c.MouldCodeLH IS NOT NULL
            UNION
            SELECT b.plant, b.itemCode AS gt_code, c.wcID::VARCHAR AS press,
                   c.MouldCodeRH AS mould
            FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
            WHERE b.stage = 2 AND c.statuscritical = 'Normal' AND c.MouldCodeRH IS NOT NULL
        )
        SELECT plant, gt_code, press, mould, count(*) AS n
        FROM m GROUP BY 1,2,3,4
    """).pl().sort(["plant", "gt_code", "press", "n"], descending=[False, False, False, True])


def _size_compatible_presses(timing: TimingLookup) -> dict[tuple[str, str], list[str]]:
    """(plant, size) -> presses that have cured ANY GT of that size.

    The plant's real feasibility set. Measured: 47% of the press-GT pairs it used
    in Jan-2026 did not exist in Dec, carrying 32% of the month's tyres, and 26
    of 52 PCR GTs needed more presses than the previous month showed. Restricting
    a GT to presses that cured *that exact GT* last month therefore forces all
    demand through about half the pairs the plant really uses -- the packer then
    runs out of capacity and overloads the few eligible presses, which is what
    stretched the cure span past the month.

    A press can run a GT if it can hold that size. That is the constraint.
    """
    con = duck()
    out: dict[tuple[str, str], list[str]] = {}
    seen: dict[tuple[str, str], set[str]] = {}
    try:
        rows = con.execute("""
            SELECT DISTINCT b.plant, c.wcID::VARCHAR AS press, b.itemCode AS gt
            FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
            WHERE b.stage = 2 AND c.statuscritical = 'Normal'
        """).fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("curing.size_map_unavailable", err=str(e))
        return out
    for plant, press, gt in rows:
        s = timing._size_for_gt(gt) if gt else None
        if s:
            seen.setdefault((plant, s), set()).add(press)
    for k, v in seen.items():
        out[k] = sorted(v)
    log.info("curing.size_compatible", size_groups=len(out),
             avg_presses=round(sum(len(v) for v in out.values()) / max(len(out), 1), 1))
    return out


def _rate_anchored(demand: list[tuple[str, float]],
                   presses: list[tuple[str, float]],
                   allowed: dict[str, list[str]],
                   horizon_s: float) -> tuple[dict[tuple[str, str], float], dict, float]:
    """Rate-anchored allocation: dedicate whole presses, pack only the residuals.

    Makespan is a CONSTRAINT (H = the month), not an objective. For each SKU the
    average number of presses it needs over the whole horizon is

        n_g = N_g * tau_g / H

    Take D_g = min(floor(n_g), |P_g|) presses and dedicate them to g for the
    ENTIRE month: one mould mount at t=0, zero changeovers, 100% utilisation,
    and a perfectly constant cure rate that building can match. Only the
    fractional residual f_g = n_g - D_g needs packing onto the leftover presses.

    Changeovers then have a closed form:  sum_i k_i - |P|  (first mount is free).

    This reproduces the plant's structure, which is bimodal rather than uniform:
    ~100 presses dedicated at 1 campaign each, ~66 shared at ~4.6 campaigns =
    406 campaigns - 166 presses = 240 changeovers, matching its actual 245.
    Treating all presses as homogeneous and rotating all of them is what gave us
    777 changeovers at 8-day windows.

    Crucially, dedication also makes interleaving free: a press holding one GT
    has zero changeovers however its tyres are ordered, so timing and changeover
    count stop competing.
    """
    out: dict[tuple[str, str], float] = {}
    if not demand or not presses:
        return out, {}, 0.0
    cad = {p: c for p, c in presses}
    free = set(cad)
    ded: dict[str, list[str]] = {}

    # Dedicate, largest-n_g first so high-runners take whole presses.
    need_n = []
    for g, q in demand:
        elig = [p for p in allowed.get(g, []) if p in cad]
        if not elig:
            continue
        tau = sum(cad[p] for p in elig) / len(elig)
        need_n.append((g, q, q * tau / horizon_s, elig))
    need_n.sort(key=lambda t_: -t_[2])

    residual: list[tuple[str, float]] = []
    for g, q, n_g, elig in need_n:
        take = min(int(n_g), len([p for p in elig if p in free]))
        chosen = [p for p in elig if p in free][:take]
        for p in chosen:
            free.discard(p)
            out[(g, p)] = horizon_s / cad[p]          # this press runs g all month
        if chosen:
            ded[g] = chosen
        placed = sum(out.get((g, p), 0.0) for p in chosen)
        if q - placed > 1:
            residual.append((g, q - placed))

    stats = {"dedicated_presses": sum(len(v) for v in ded.values()),
             "dedicated_gts": len(ded), "shared_presses": len(free),
             "residual_tyres": round(sum(q for _g, q in residual))}
    return out, stats, 0.0


def _mcnaughton(demand: list[tuple[str, float]],
                presses: list[tuple[str, float]],
                mu: dict[str, int]) -> tuple[dict[tuple[str, str], float], float, float]:
    """Exact minimum-span assignment of splittable SKU work to presses.

    Press quantities are splittable, so this is P|split|Cmax -- solvable exactly
    in O(n) by McNaughton's wrap-around rule, not an NP-hard bin-packing. LPT /
    best-fit / first-fit are approximations to a problem that has a closed form:

        T* = max( sum_i W_i / sum_p (1/c_p) ,  max_i W_i / mu_i )
              |___ total-work bound ______|    |__ mould bound __|

    (the work bound is the uniform-machine form, since press cadences differ
    4x here; with identical presses it collapses to sum W / (M*k).)

    Lay all SKU work end to end on one timeline, cut at each press's capacity,
    and every press finishes at <= T*. A SKU spans at most two presses and never
    runs on both at once, which is McNaughton's guarantee.

    `demand`  [(gt, tyres)] ordered so size-alike GTs are adjacent -- wrap-around
              then lands same-size work on a press, keeping changeovers low.
    `presses` [(press, seconds_per_tyre)]
    `mu`      gt -> max concurrent presses (mould sets available)

    Returns ({(gt, press): tyres}, T*_seconds, mould_bound_seconds).
    """
    total_tyres = sum(q for _g, q in demand)
    if total_tyres <= 0 or not presses:
        return {}, 0.0, 0.0
    rate_sum = sum(1.0 / c for _p, c in presses if c > 0)
    work_bound = total_tyres / rate_sum if rate_sum else 0.0
    mould_bound = 0.0
    for g, q in demand:
        m = max(1, mu.get(g, 1))
        c = presses[0][1]
        mould_bound = max(mould_bound, q * c / m)
    tstar = max(work_bound, mould_bound)

    # Pre-split any SKU that exceeds its mould cap into <= mu_i strands so no
    # single press is asked to carry more of it than the moulds allow.
    strands: list[tuple[str, float]] = []
    for g, q in demand:
        m = max(1, mu.get(g, 1))
        per = q / m
        if m > 1 and per * presses[0][1] > tstar:
            for _ in range(m):
                strands.append((g, per))
        else:
            strands.append((g, q))

    out: dict[tuple[str, str], float] = {}
    idx = 0
    remaining = strands[idx][1] if strands else 0.0
    for press, cad in presses:
        cap = tstar / cad if cad > 0 else 0.0     # tyres this press can do in T*
        while cap > 1e-9 and idx < len(strands):
            g = strands[idx][0]
            take = min(cap, remaining)
            if take > 0:
                out[(g, press)] = out.get((g, press), 0.0) + take
                cap -= take
                remaining -= take
            if remaining <= 1e-9:
                idx += 1
                remaining = strands[idx][1] if idx < len(strands) else 0.0
            if take <= 0:
                break
    return out, tstar, mould_bound


def _simulate_presses(tyres: pl.DataFrame, timing: TimingLookup,
                      start: datetime) -> tuple[pl.DataFrame, dict]:
    """Event-driven press simulation. Presses NEVER own tyres.

    A press owns exactly two things, neither of which is a tyre: a mould mount
    and a quota (a scalar). Stock is a per-GT FIFO of build-completion times.
    The press asks "is there ANY tyre of g" -- never "where is MY tyre".

    That single change removes a whole bug class. Binding tyre identity to a
    press produced the same defect at two different layers:
      * at ALLOCATION time -- contiguous blocks gave press 144 the GT's last
        1,117 tyres, so it idled 607h while the GT was available from h=0;
      * at SEQUENCING time -- a due-date queue let a tyre built late sit at the
        head and block already-available stock behind it (press 53: 676h of
        work spread over 1,155h).
    Any fix that keeps per-press tyre lists and only reorders them yields a
    third instance, because every static order has a pathological input.

    With popleft() work-conservation is structural rather than maintained, and
    aging is correct for free: FIFO means the age measured is the age that
    exists. Quota is a SOFT ceiling -- if one press drains a GT faster than
    planned it keeps going and the shortfall comes off another press, since a
    hard quota would reintroduce identity binding through the back door.
    """
    import heapq
    from collections import defaultdict, deque

    # NB: epoch arithmetic must match Polars' from_epoch, which treats naive
    # datetimes as UTC. datetime.timestamp() treats them as LOCAL time, so on an
    # IST machine every cure landed 5.5h early -- before the month even started
    # -- and the verifier rightly flagged 53,431 negative-GT violations.
    EPOCH = datetime(1970, 1, 1)
    t0 = (start - EPOCH).total_seconds()
    stock: dict[tuple[str, str], deque] = {}
    for (p_, g_), grp in tyres.sort("avail_ts").group_by(["plant", "gt_code"]):
        stock[(p_, g_)] = deque(
            (x - EPOCH).total_seconds() for x in grp["avail_ts"].to_list())

    # quota = how many of each GT this press was allocated (soft ceiling)
    quota: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    for r in (tyres.group_by(["plant", "press", "gt_code"]).len()).iter_rows(named=True):
        quota[(r["plant"], r["press"])][r["gt_code"]] = int(r["len"])

    presses = sorted(quota)
    mount: dict[tuple[str, str], str] = {}
    for key in presses:
        mount[key] = max(quota[key], key=lambda g: quota[key][g])

    # MOULD CLEANING -- previously charged nothing, so press capacity was
    # optimistic. Measured from MouldCountLH in our own history: the counter
    # tops out at 1,344 cycles (PCR) / 1,769 (TBR), NOT the 3,000 BTP uses, so
    # cleans here are about twice as frequent as that parameter implies.
    # A changeover remounts the mould and resets the counter.
    clean_every = {"PCR": float(CONFIG.thresholds.mould_clean_cycles_pcr),
                   "TBR": float(CONFIG.thresholds.mould_clean_cycles_tbr)}
    clean_s = CONFIG.thresholds.mould_clean_min * 60.0
    cycles: dict[tuple[str, str], float] = defaultdict(float)

    heap = [(t0, key) for key in presses]
    heapq.heapify(heap)
    out_p, out_g, out_s, out_e, out_c = [], [], [], [], []
    idle = defaultdict(float)
    n_co = 0
    n_clean = 0

    while heap:
        t, key = heapq.heappop(heap)
        plant, press = key
        q = quota[key]
        g = mount.get(key)
        cad = max(timing.cure_cadence_s(plant, press), 1.0)

        # 1) cure from mounted GT's stock if any exists and we still owe it
        # mould clean falls due -- 480 min, mid-run, counter resets
        if cycles[key] >= clean_every.get(plant, 1500.0):
            cycles[key] = 0.0
            n_clean += 1
            heapq.heappush(heap, (t + clean_s, key))
            continue

        if g and q.get(g, 0) > 0 and stock.get((plant, g)) and stock[(plant, g)][0] <= t:
            stock[(plant, g)].popleft()
            q[g] -= 1
            cycles[key] += 1.0
            out_p.append(press); out_g.append(g)
            out_s.append(t); out_e.append(t + cad); out_c.append(cad)
            heapq.heappush(heap, (t + cad, key))
            continue

        # 2) changeover to another GT we owe that HAS stock ready now
        cand_now = [g2 for g2, n in q.items()
                    if n > 0 and stock.get((plant, g2)) and stock[(plant, g2)][0] <= t]
        if cand_now:
            g2 = max(cand_now, key=lambda x: q[x])
            if g2 != g:
                s = timing.mould_change_s(plant, press)
                n_co += 1
                cycles[key] = 0.0          # remount resets the mould counter
                mount[key] = g2
                heapq.heappush(heap, (t + s, key))
            else:
                heapq.heappush(heap, (t + 1.0, key))
            continue

        # 3) nothing ready: jump to the next arrival we owe (true starvation)
        nxt = [stock[(plant, g2)][0] for g2, n in q.items()
               if n > 0 and stock.get((plant, g2))]
        if nxt:
            t2 = min(nxt)
            idle[plant] += (t2 - t)
            heapq.heappush(heap, (t2, key))

    df = pl.DataFrame({
        "press": out_p, "gt_code": out_g, "cycle_s": out_c,
        "_s": out_s, "_e": out_e,
    })
    stats = {"changeovers": n_co, "mould_cleans": n_clean,
             "idle_h": {k: round(v / 3600) for k, v in idle.items()}}
    return df, stats


def _emit_cure(sim: pl.DataFrame, sim_stats: dict, ledger: GreenTireLedger,
               out_dir: Path) -> pl.DataFrame:
    """Turn simulated (plant, press, gt, start, end) rows into the cure schedule.

    Shared by the shift grid and the legacy event-driven sim so both produce
    byte-identical downstream artefacts.
    """
    cure_df = sim.with_columns(
        pl.from_epoch(pl.col("_s").cast(pl.Int64), time_unit="s").alias("start_ts"),
        pl.from_epoch(pl.col("_e").cast(pl.Int64), time_unit="s").alias("end_ts"),
    ).drop(["_s", "_e"])

    # FIFO INVARIANT GUARD. The verifier ranks cures and supplies independently
    # per (plant, gt_code) and requires start_ts[k] >= supply_ts[k]. The grid
    # honours that per shift -- it only ever pools tyres built before the shift
    # opened -- but the ranks are re-derived downstream after `sync` removes
    # unmatched cures, and any deletion shifts every later rank left. Enforce it
    # here against the ledger's own supply stream, which at this point holds
    # build and opening events only. Cheap, and it makes the property structural
    # rather than incidental.
    _sup = ledger.con.execute("""
        SELECT ts, plant, gt_code, qty_delta::BIGINT AS q FROM gt_events
        WHERE source IN ('build','opening') AND qty_delta > 0
    """).pl()
    if _sup.height and cure_df.height:
        _sup = (_sup.with_columns(pl.int_ranges(pl.col("q")).alias("_i"))
                    .explode("_i").sort(["plant", "gt_code", "ts"]))
        _sup = _sup.with_columns(
            pl.int_range(pl.len()).over(["plant", "gt_code"]).alias("_rk")
        ).select(["plant", "gt_code", "_rk", pl.col("ts").alias("_supply")])
        # TOTAL order. Sorting on (plant, gt, start_ts) alone leaves ties -- many
        # presses start a shift at the same instant -- so row order among them
        # varied run to run and the parquet was not byte-reproducible even
        # though every KPI matched. Determinism is a contract, so the sort key
        # must be total.
        cure_df = cure_df.sort(["plant", "gt_code", "start_ts", "press", "cycle_s"],
                               maintain_order=True).with_columns(
            pl.int_range(pl.len()).over(["plant", "gt_code"]).alias("_rk"))
        cure_df = cure_df.join(_sup, on=["plant", "gt_code", "_rk"], how="left")
        _n_push = int(cure_df.filter(
            pl.col("_supply").is_not_null()
            & (pl.col("start_ts") < pl.col("_supply"))).height)
        cure_df = cure_df.with_columns(
            pl.max_horizontal(
                pl.col("start_ts"),
                pl.col("_supply").fill_null(pl.col("start_ts")) + pl.duration(seconds=1)
            ).alias("start_ts"))
        cure_df = cure_df.with_columns(
            (pl.col("start_ts") + pl.duration(seconds=pl.col("cycle_s"))).alias("end_ts")
        ).drop(["_rk", "_supply"])
        if _n_push:
            log.info("curing.fifo_guard", pushed=_n_push, of=cure_df.height)
    # Total, stable order before ids are minted, so cure_id -- and therefore the
    # whole parquet -- is byte-reproducible across runs.
    cure_df = cure_df.sort(["plant", "press", "gt_code", "start_ts"],
                           maintain_order=True)
    cure_df = cure_df.with_row_index(name="cure_id").with_columns(
        (pl.lit("cure-") + pl.col("cure_id").cast(pl.Utf8) + pl.lit("-")
         + pl.col("gt_code")).alias("lot_id"),
        pl.lit("").alias("source_build_lot"),
    )
    mould_map = _historical_mould_map()
    if mould_map.height:
        # `group_by().first()` takes a NON-DETERMINISTIC group representative --
        # the same (plant, GT, press) resolved to HMI18 on one run and HMI05 on
        # the next, which was the last source of a non-reproducible parquet.
        # Pre-sort on a total key (most-used mould, then name) and keep order.
        primary = (mould_map
                   .sort(["plant", "gt_code", "press", "n", "mould"],
                         descending=[False, False, False, True, False])
                   .group_by(["plant", "gt_code", "press"], maintain_order=True)
                   .first()
                   .select(["plant", "gt_code", "press", "mould"]))
        cure_df = cure_df.join(primary, on=["plant", "gt_code", "press"], how="left")
        cure_df = cure_df.with_columns(
            (pl.col("mould").fill_null(pl.lit("MOULD_NA"))
             + pl.lit("@") + pl.col("press")).alias("mould"))
    else:
        cure_df = cure_df.with_columns(pl.lit(None, dtype=pl.Utf8).alias("mould"))
    trace_default = DecisionTrace()
    trace_default.add_reason("press.window_campaign", "stat", "window_plan", 1.0)
    cure_df = cure_df.with_columns(
        pl.lit(trace_default.model_dump_flat()).alias("decision_trace"))
    cure_ledger = cure_df.select([
        pl.col("start_ts").alias("ts"), pl.col("plant"), pl.col("gt_code"),
        pl.lit(-1.0).alias("qty_delta"), pl.lit("cure").alias("source"),
        pl.col("lot_id")])
    ledger.con.register("_cure_evt", cure_ledger.to_arrow())
    ledger.con.execute("INSERT INTO gt_events SELECT * FROM _cure_evt")
    ledger.con.unregister("_cure_evt")
    # FINAL total sort. `join` re-orders rows, so the mould lookup above undid
    # the ordering applied before cure_id was minted. Determinism is a contract:
    # pin the row order at the last possible moment, immediately before write.
    cure_df = cure_df.sort(["plant", "press", "start_ts", "gt_code", "cure_id"],
                           maintain_order=True)
    cure_df.write_parquet(out_dir / "cure_schedule.parquet", compression="zstd")
    log.info("curing.planned", n=cure_df.height, **{
        k: v for k, v in sim_stats.items() if not isinstance(v, dict)})
    return cure_df


def plan_curing(
    ledger: GreenTireLedger,
    timing: TimingLookup,
    start: datetime,
    out_dir: Path,
    build_df: pl.DataFrame | None = None,
    campaigns: dict | None = None,
    quota: dict | None = None,
) -> pl.DataFrame:
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) Per-tyre expansion of build events (one row per unit).
    #    NB: named `gt_supply`, not `build_df` -- the parameter of that name
    #    carries the build SCHEDULE (with due_date) and must not be shadowed.
    gt_supply = ledger.con.execute("""
        SELECT ts AS built_ts, plant, gt_code, qty_delta::BIGINT AS qty, lot_id
        FROM gt_events WHERE source IN ('build','opening') AND qty_delta > 0
        ORDER BY plant, gt_code, ts
    """).pl()
    if gt_supply.height == 0:
        log.warning("curing.no_build_events")
        return pl.DataFrame()

    # Explode into per-tyre rows.
    gt_supply = gt_supply.with_columns(pl.col("qty").cast(pl.Int64))
    tyres = gt_supply.with_columns(
        pl.int_ranges(pl.col("qty")).alias("_idx")
    ).explode("_idx")
    log.info("curing.tyres_expanded", n=tyres.height)

    # 2-PRE) CURING-FIRST PATH. If `window_plan` committed press campaigns, the
    # allocation question is already answered -- press p holds GT g over days
    # [a_g, a_g+D_g) -- and everything below (historical CDF pick, McNaughton,
    # bin-packing, capacity spill) is dead. Run the fixed shift grid instead, so
    # the horizon is a loop bound and the span cannot exceed the month.
    if campaigns:
        import calendar as _cal

        from planner.plan.shift_grid import simulate_shift_grid
        # Opening WIP keeps its TRUE build timestamp -- do NOT clamp it to the
        # horizon start. The grid only pools tyres built strictly before a shift
        # opens, so clamping opening stock to `start + 1s` made it ineligible in
        # the very first shift, and combined with the day-0 campaign rule the
        # whole of day 1 went uncured. The clamp existed for the legacy path,
        # where a press timeline could otherwise reach back into last month; the
        # grid cannot, because its loop starts at `start`.
        tyres = tyres.with_columns(pl.col("built_ts").alias("avail_ts"))
        sim, sim_stats = simulate_shift_grid(
            tyres, campaigns, timing, start,
            _cal.monthrange(start.year, start.month)[1], quota=quota)
        if sim.height:
            df = _emit_cure(sim, sim_stats, ledger, out_dir)
            # hand the planner the POTENTIAL cure profile for its fixed point
            df._potential = sim_stats.get("_potential")  # noqa: SLF001
            return df
        log.warning("curing.shift_grid_empty")
        return pl.DataFrame()

    # 2) Press map â€” assign each tyre to a press using the *historical usage
    # distribution* for that (plant, gt_code). This spreads tyres across the
    # 100+ presses the plant actually runs in parallel, matching observed load.
    press_map = _historical_press_map()
    if press_map.height == 0:
        log.warning("curing.no_press_history")
        return pl.DataFrame()

    # Normalize to fractions per (plant, gt_code) and build a cumulative CDF.
    press_map = (
        press_map
        .with_columns(pl.col("n").sum().over(["plant", "gt_code"]).alias("total"))
        .with_columns((pl.col("n").cast(pl.Float64) / pl.col("total")).alias("frac"))
        .with_columns(pl.col("frac").cum_sum().over(["plant", "gt_code"]).alias("cdf"))
    )

    tyres = tyres.with_columns(pl.lit(0.0).alias("u"))
    # For deterministic assignment: sort press_map by (plant, gt_code, cdf ASC)
    # then for each tyre find first press with cdf >= u via asof-like join.
    press_map = press_map.sort(["plant", "gt_code", "cdf"])
    tyres_sorted = tyres.sort(["plant", "gt_code", "u"])
    tyres = tyres_sorted.join_asof(
        press_map.select(["plant", "gt_code", "cdf", "press"]),
        left_on="u", right_on="cdf",
        by=["plant", "gt_code"],
        strategy="forward",
    )
    # Fallback: if no press matched (edge u=1.0), pick modal press.
    modal = press_map.group_by(["plant", "gt_code"]).last().select(
        ["plant", "gt_code", pl.col("press").alias("press_fallback")]
    )
    tyres = tyres.join(modal, on=["plant", "gt_code"], how="left")
    tyres = tyres.with_columns(
        pl.col("press").fill_null(pl.col("press_fallback"))
    ).drop("press_fallback").filter(pl.col("press").is_not_null())

    # 2b) PRESS CAMPAIGN ALLOCATION (rules C3/C4/P8/P9 + E1/S4).
    #
    # Previously each tyre picked a press independently, so a press hopped
    # between GTs ~1.8x/day. At 3.5-7h per mould change that burned 44% of all
    # press capacity and wrecked both changeovers and GT aging.
    #
    # Instead, dedicate presses to GTs up front by bin-packing: walk GTs
    # largest-first and fill whole presses from that GT's historically-valid
    # candidates. A press then runs one GT for as long as its share lasts, so
    # changeovers collapse to roughly one per press per campaign, and only as
    # many presses are opened as demand actually needs (P8/P9) -- without
    # starving any GT of throughput, because a GT that needs several presses
    # gets them.
    cand: dict[tuple[str, str], list[str]] = {}
    for key, grp in press_map.group_by(["plant", "gt_code"]):
        p_, g_ = key if isinstance(key, tuple) else (key, None)
        cand[(p_, g_)] = grp.sort("n", descending=True)["press"].to_list()

    # Widen to the derived capability master. `press_map` above is the visible
    # window only and covers just 47 % of the press-GT pairs the plant used in
    # Jan-2026; the full-history matrix covers 100 %. Historical order is kept
    # first so preference/stickiness is unchanged -- this only adds the presses
    # the plant would also have been free to use.
    _apath = CONFIG.paths.warehouse / "derived" / "allowed_press_matrix.parquet"
    if _apath.exists():
        try:
            am = pl.read_parquet(_apath)
            added = 0
            for r in am.sort("basis").iter_rows(named=True):
                k = (r["plant"], r["gt_code"])
                lst = cand.setdefault(k, [])
                if r["press"] not in lst:
                    lst.append(r["press"])
                    added += 1
            log.info("curing.allowed_matrix", widened_pairs=added, gts=len(cand))
        except Exception as e:  # noqa: BLE001
            log.warning("curing.allowed_matrix_failed", err=str(e))

    # DAILY allocation with carry-over stickiness -- this is how the plant runs.
    # Measured envelope (RULEBOOK.md): a press cures ONE SKU per day (p50 1,
    # max 3) in campaigns of ~1,166 units, and only ~4-5 presses per plant change
    # over per day (~146/month PCR, ~99/month TBR). Month-long dedication gives
    # far too few changeovers AND strands tyres for weeks; per-tyre assignment
    # gives far too many. Allocating per day, and keeping yesterday's press on
    # the same GT whenever it still has work, reproduces both at once and bounds
    # GT age to about a day.
    days = max(1, (tyres["built_ts"].max() - tyres["built_ts"].min()).days + 1)
    # PLANT RULE: the same demand must be cured on the same day the plant would.
    # Key each tyre by its lot's DUE DATE, not by when building happened to
    # emit it. Demand is per (GT, day) so due dates are evenly spread by
    # construction, whereas build arrival is lumpy -- keying on arrival is what
    # made the earlier daily allocation churn presses. This also stops curing
    # running past month end, because every tyre is anchored to a day inside it.
    if build_df is not None and build_df.height and "due_date" in build_df.columns:
        due = (build_df.select(["lot_id", "due_date"]).unique(subset=["lot_id"])
                       .with_columns(pl.col("due_date").cast(pl.Date)))
        tyres = tyres.join(due, on="lot_id", how="left").with_columns(
            pl.col("due_date")
              .fill_null((pl.col("built_ts") - pl.duration(hours=7)).dt.date())
              .alias("_day"))
    else:
        tyres = tyres.with_columns(
            (pl.col("built_ts") - pl.duration(hours=7)).dt.date().alias("_day"))
    daily = (tyres.group_by(["plant", "_day", "gt_code"]).len()
                  .sort(["_day", "len"], descending=[False, True]))

    cap_day = _press_capacity_per_day()          # historical p95 tyres/press/day
    prev_gt: dict[tuple[str, str], str] = {}     # (plant, press) -> GT run yesterday
    day_alloc: dict[tuple[str, object, str], list[str]] = {}
    day_alloc_cum: dict[tuple[str, object, str], list[float]] = {}
    import bisect as _bisect
    USE_DAILY = False  # month-level. Daily reallocation churns presses (28,058 COs).
    def _cap(p_: str, pr: str) -> float:
        return max(1.0, min(cap_day.get((p_, pr), 1e9),
                            86400.0 / max(timing.cure_cadence_s(p_, pr), 1.0)))

    for day in (daily["_day"].unique().sort().to_list() if USE_DAILY else []):
        rem: dict[tuple[str, str], float] = {
            (r["plant"], r["gt_code"]): float(r["len"])
            for r in daily.filter(pl.col("_day") == day).iter_rows(named=True)}
        taken: set[tuple[str, str]] = set()
        today: dict[tuple[str, str], str] = {}
        chosen_by: dict[tuple[str, str], list[str]] = {}

        # PASS 1 -- carry over. A press keeps yesterday's GT while that GT still
        # has work. This is what holds curing at ~4-5 changeovers/plant/day and
        # 99.96% stickiness; reallocating from scratch each day churns every
        # press and multiplies mould changes.
        for (p_, pr), g_ in list(prev_gt.items()):
            if rem.get((p_, g_), 0.0) > 0:
                taken.add((p_, pr))
                today[(p_, pr)] = g_
                chosen_by.setdefault((p_, g_), []).append(pr)
                rem[(p_, g_)] -= _cap(p_, pr)

        # PASS 2 -- place whatever demand is still uncovered on free presses.
        for (p_, g_), qty in sorted(rem.items(), key=lambda kv: -kv[1]):
            if qty <= 0:
                continue
            for pr in cand.get((p_, g_), []):
                if qty <= 0:
                    break
                if (p_, pr) in taken:
                    continue
                taken.add((p_, pr))
                today[(p_, pr)] = g_
                chosen_by.setdefault((p_, g_), []).append(pr)
                qty -= _cap(p_, pr)
            if not chosen_by.get((p_, g_)):
                fall = [x for x in cand.get((p_, g_), []) if (p_, x) not in taken]
                if fall:
                    taken.add((p_, fall[0]))
                    today[(p_, fall[0])] = g_
                    chosen_by[(p_, g_)] = [fall[0]]

        for (p_, g_), prs in chosen_by.items():
            day_alloc[(p_, day, g_)] = prs
            # Weight each press by its OWN daily capacity. Splitting a day's
            # tyres evenly hands a slow press (up to 972s/tyre vs 223s) the same
            # share as a fast one, so its queue runs past month end: 35 presses
            # ended up with more work than the month contains -- TBR press 73
            # got 286 tyres = 891h at its ~3.1h/tyre cadence.
            caps = [_cap(p_, pr) for pr in prs]
            tot_c = sum(caps) or 1.0
            acc, cum = 0.0, []
            for cp in caps:
                acc += cp / tot_c
                cum.append(acc)
            day_alloc_cum[(p_, day, g_)] = cum
        # MEASURED: carrying an idle press's assignment forward
        # (prev_gt.update) is WORSE than dropping it -- aging 185h -> 228h for
        # no changeover saving (1,925 -> 1,939). A press held on a GT that has
        # gone quiet blocks capacity that another GT needs today. Keep the
        # replace semantics.
        prev_gt = today

    if not USE_DAILY:
        # McNAUGHTON EXACT ASSIGNMENT (replaces the bin-packing heuristics).
        # Measured T* for Jan-2026: PCR 636.7h, TBR 697.6h -- both inside the
        # 744h month, and the mould bound (262h / 283h) is nowhere near binding.
        # The bin-packer was producing 1,180-1,201h, i.e. ~46% pure assignment
        # loss on a problem that has an exact O(n) solution.
        # WINDOWED: run the exact assignment per campaign-length window rather
        # than once for the month. Assigning (GT, press) for the whole month
        # fixes WHAT each press does but not WHEN -- a contiguous split hands
        # press i+1 the later tyres, so it idles until they exist and the span
        # slides right (1,295h against T* of 682h). Interleaving fixes timing
        # but shreds campaigns (35,361 changeovers).
        #
        # A GT is a batch of independent tyres, not one indivisible job, so
        # McNaughton's no-overlap guarantee does not apply -- we replace it with
        # a time dimension. One window = one campaign length (plant runs ~1,166
        # units PCR / ~468 TBR, both ~8 days), so a press holds one GT per
        # window: changeovers ~= (windows-1) per press, and every tyre is cured
        # in the window it arrives in.
        camp_days = max(1, int(CONFIG.thresholds.cure_campaign_days))
        day_list = sorted(d for d in tyres["_day"].unique().to_list() if d is not None)
        win_of = {d: i // camp_days for i, d in enumerate(day_list)}
        tyres = tyres.with_columns(
            pl.col("_day").replace_strict(win_of, default=0).alias("_win"))
        gt_dem = (tyres.group_by(["plant", "_win", "gt_code"]).len()
                       .sort(["plant", "_win", "len"], descending=[False, False, True]))
        # mould sets per GT = presses that can hold it (capability matrix)
        mu_all: dict[tuple[str, str], int] = {}
        for k_, v_ in cand.items():
            mu_all[k_] = len(v_)
        alloc_mc: dict[tuple[str, int, str], list[str]] = {}
        dedicated: set = set()
        alloc_cum: dict[tuple[str, int, str], list[float]] = {}
        quota_mc: dict[tuple[str, int, str, str], float] = {}
        n_win = int(tyres["_win"].max() or 0) + 1
        for plant_ in gt_dem["plant"].unique().to_list():
          for win_ in range(n_win):
            sub = gt_dem.filter((pl.col("plant") == plant_) & (pl.col("_win") == win_))
            if sub.height == 0:
                continue
            # Stable ordering (size class, then volume) across windows means a
            # GT lands on roughly the same presses each window -- stickiness for
            # free, without a separate carry-over pass.
            order = sorted(
                [(r["gt_code"], float(r["len"])) for r in sub.iter_rows(named=True)],
                key=lambda t_: (timing._size_for_gt(t_[0]) or "~", -t_[1]))
            # Intersect with the presses that actually exist for this plant --
            # the widened capability matrix was leaking a few cross-plant press
            # ids, giving PCR 114 presses against a real 95 and so computing T*
            # against phantom capacity.
            real = {r[0] for r in duck().execute(
                "SELECT DISTINCT wcID::VARCHAR FROM v_curing WHERE plant = ?",
                [plant_]).fetchall()}
            plist = sorted({pr for (p2, _g), v in cand.items() if p2 == plant_
                            for pr in v} & real)
            pr_c = [(pr, max(timing.cure_cadence_s(plant_, pr), 1.0)) for pr in plist]
            mu_p = {g: mu_all.get((plant_, g), 1) for g, _q in order}
            # 1) Dedicate whole presses to high-runners for the whole horizon.
            H_s = CONFIG.thresholds.cure_horizon_days * 86400.0
            allowed_g = {g: [p for p in cand.get((plant_, g), []) if p in set(plist)]
                         for g, _q in order}
            # Dedication DROPPED: the plant's 38% single-GT presses are a packing
            # artefact of fitting {n_g} into 91 presses, not a design input --
            # its dedicated presses carry merely average volume. Dedicating was
            # solving a problem created by constant lot size, which T_0 sizing
            # now fixes at source; meanwhile it drove the cure span 883h -> 1,350h
            # by maximising single-GT starvation exposure.
            ded_map, ded_stats, _ = (
                _rate_anchored(order, pr_c, allowed_g, H_s)
                if CONFIG.thresholds.dedicate_presses else ({}, {}, 0.0))
            # 2) Only the residual needs packing.
            placed = {}
            for (g, p), q in ded_map.items():
                placed[g] = placed.get(g, 0.0) + q
            resid = [(g, q - placed.get(g, 0.0)) for g, q in order
                     if q - placed.get(g, 0.0) > 1]
            used = {p for (_g, p) in ded_map}
            pr_left = [(p, c) for p, c in pr_c if p not in used]
            assign_map, tstar, mbound = _mcnaughton(resid, pr_left, mu_p)
            for (g_d, p_d) in ded_map:
                dedicated.add((plant_, g_d, p_d))
            assign_map.update(ded_map)
            if win_ == 0:
                log.info("curing.rate_anchored", plant=plant_, **ded_stats,
                         residual_gts=len(resid))
            if win_ == 0:
                log.info("curing.mcnaughton", plant=plant_, presses=len(pr_c),
                         windows=n_win, camp_days=camp_days,
                         tstar_h=round(tstar / 3600, 1),
                         mould_bound_h=round(mbound / 3600, 1))
            for (g, pr), q in assign_map.items():
                if q <= 0:
                    continue
                alloc_mc.setdefault((plant_, win_, g), []).append(pr)
                quota_mc[(plant_, win_, g, pr)] = q
        # cumulative proportions for the per-tyre assignment below
        for k_, prs in alloc_mc.items():
            tot = sum(quota_mc[(k_[0], k_[1], k_[2], pr)] for pr in prs) or 1.0
            acc, cum = 0.0, []
            for pr in prs:
                acc += quota_mc[(k_[0], k_[1], k_[2], pr)] / tot
                cum.append(acc)
            alloc_cum[k_] = cum
        alloc = alloc_mc
        tyres = tyres.with_columns(
            pl.int_range(pl.len()).over(["plant", "_win", "gt_code"]).alias("_seq"),
            pl.len().over(["plant", "_win", "gt_code"]).alias("_cnt"))
        assign = []
        for p_, w_, g_, seq, cur_press, cnt in zip(
                tyres["plant"].to_list(), tyres["_win"].to_list(),
                tyres["gt_code"].to_list(),
                tyres["_seq"].to_list(), tyres["press"].to_list(), tyres["_cnt"].to_list()):
            ps = alloc.get((p_, w_, g_))
            if not ps:
                assign.append(cur_press)
                continue
            # Interleave on DEDICATED presses, run campaigns on SHARED ones.
            #
            # A dedicated press holds one GT all month, so interleaving costs it
            # nothing and gives perfect timing -- it consumes tyres as they are
            # built. A shared press holds several GTs, so interleaving makes it
            # hop between them (594 changeovers across only 44 shared presses =
            # 13.5 each). Those must run as contiguous campaigns instead.
            # ALWAYS INTERLEAVE. A GT is a batch of INDEPENDENT tyres, not one
            # indivisible job, so McNaughton's no-overlap guarantee does not
            # apply -- several presses may cure the same GT concurrently.
            #
            # Contiguous blocks caused the entire span overrun: a GT needing ~12
            # presses had its tyres cut into 12 blocks in build order, so the
            # LAST press could not start until the tail was built. TBR press 144
            # held 702h of work but began at h=607 and ended at h=1,309 --
            # exactly 607 + 702. Its GT was available from h=0 all month.
            cum = alloc_cum.get((p_, w_, g_)) or [1.0]
            per = min(997, max(int(cnt), 1))
            u = (seq % per + 0.5) / per
            assign.append(ps[min(bisect.bisect_left(cum, u), len(ps) - 1)])
        tyres = tyres.with_columns(pl.Series("press", assign)).drop(["_seq", "_cnt", "_win"])
        log.info("curing.press_allocated", gt_windows=len(alloc),
                 presses=len({(p, x) for (p, _w, _g), v in alloc.items() for x in v}))
    elif False:
        # Month-level bin-pack: walk GTs largest-first and fill whole presses
        # from that GT's historically-valid candidates, sized by each press's
        # own cadence. Daily reallocation was tried and is much worse (2,228
        # changeovers, aging 428h) because a press's GT churns every day; the
        # plant instead holds one SKU per press for ~1,166 units (7-8 days).
        gt_demand = (tyres.group_by(["plant", "gt_code"]).len()
                          .sort("len", descending=True))
        press_free: dict[tuple[str, str], float] = {}
        press_gt_count: dict[tuple[str, str], int] = {}
        alloc: dict[tuple[str, str], list[str]] = {}
        extended = 0
        unplaced: dict[str, float] = {}
        size_presses = _size_compatible_presses(timing)
        all_presses: dict[str, list[str]] = {}
        for r in press_map.select(["plant", "press"]).unique().iter_rows(named=True):
            all_presses.setdefault(r["plant"], []).append(r["press"])
        # Presses may work the whole horizon, not just while building runs.
        # Give them the calendar month plus the shelf-life tail.
        # Actual length of the month being planned -- 28/29/30/31. Hardcoding 31
        # over-budgets every press by up to 11% in a 30-day month and 10% in
        # February. Shift grid is 3 x 8h = 24h/day, day boundary 07:00.
        import calendar as _cal
        horizon_days = _cal.monthrange(start.year, start.month)[1]
        log.info("curing.horizon", month=f"{start.year}-{start.month:02d}",
                 days=horizon_days, shifts=horizon_days * 3, hours=horizon_days * 24)
        alloc_cum: dict[tuple[str, str], list[float]] = {}
        quota: dict[tuple[str, str], list[int]] = {}
        gt_total: dict[tuple[str, str], float] = {
            (r["plant"], r["gt_code"]): float(r["len"])
            for r in gt_demand.iter_rows(named=True)}
        for row in gt_demand.iter_rows(named=True):
            p_, g_, need = row["plant"], row["gt_code"], float(row["len"])
            chosen: list[str] = []
            takes: list[float] = []
            # BEST-FIT, not first-fit. Candidates arrive in historical-preference
            # order, so every GT grabs the same popular presses first and they
            # end up over budget while others sit idle -- 102 of 165 presses
            # exceeded the 684h budget by a total of 7,104 press-h, against
            # roughly the same amount of slack elsewhere. Taking the emptiest
            # eligible press first spreads the load; preference order is kept as
            # the tie-break so C1 and stickiness still hold.
            _budget0 = horizon_days * 86400.0 * CONFIG.thresholds.press_budget_frac
            _cands = sorted(
                cand.get((p_, g_), []),
                key=lambda pr: -press_free.get((p_, pr), _budget0))
            for pr in _cands:
                if need <= 0:
                    break
                if press_gt_count.get((p_, pr), 0) >= CONFIG.thresholds.max_gts_per_press:
                    continue
                # FLOW BALANCE (the plant's actual rule):
                #     n_g = ceil( build_rate_g / press_rate )
                # A GT gets enough presses that its cure rate matches its build
                # rate, so inventory mean-reverts instead of accumulating.
                #
                # Budget in SECONDS and charge the mould change, i.e.
                #     sum_g q_gp*c_p + (k_p - 1)*m_p  <=  H*rho
                # Packing to 92% of pure curing capacity and only then adding
                # ~6h per mould change on the timeline put presses at 101% --
                # which is what pushed the cure span past month end. The setup
                # term has to be reserved at allocation time, not discovered
                # afterwards.
                cad = max(timing.cure_cadence_s(p_, pr), 1.0)
                # Plant runs 3 shifts, 24/7 -- the whole month is available, and
                # mould-change time is already charged as its own term below, so
                # the press budget must NOT also be discounted by the demand
                # load target. Budgeting at 0.92 made total budget (112,860
                # press-h) SMALLER than the work (117,044), so the packer ran
                # out, under-reserved, and the shortfall was forced onto presses
                # anyway -- TBR press 201 ended up with 1,242h of work in a 744h
                # month. `curing_load_target` stays for demand trimming only.
                budget_s = horizon_days * 86400.0 * CONFIG.thresholds.press_budget_frac
                free_s = press_free.get((p_, pr), budget_s)
                setup_s = (timing.mould_change_s(p_, pr)
                           if press_gt_count.get((p_, pr), 0) > 0 else 0.0)
                if free_s - setup_s <= cad:
                    continue
                take = min((free_s - setup_s) / cad, need)
                press_free[(p_, pr)] = free_s - setup_s - take * cad
                need -= take
                chosen.append(pr)
                takes.append(take)
                press_gt_count[(p_, pr)] = press_gt_count.get((p_, pr), 0) + 1
            # Extending beyond a GT's historical presses was tried and is worse:
            # it breaks C1 (31 unseen plant/GT/press combos) and pushes curing
            # changeovers 46 -> 1,302, while aging barely moves (666 -> 656h).
            # The aging bottleneck is not press availability. Off by default.
            if need > 0 and CONFIG.thresholds.allow_press_extension:
                # Size-compatible presses only -- the plant's actual feasibility
                # set. Extending to ANY press in the plant was tried and is far
                # worse (span 1,720-2,634h, changeovers up to 2,162) because it
                # scatters GTs across unrelated presses.
                _sz = timing._size_for_gt(g_)
                _pool = size_presses.get((p_, _sz), []) if _sz else all_presses.get(p_, [])
                spare = sorted(
                    (pr for pr in _pool if pr not in chosen),
                    key=lambda pr: -press_free.get((p_, pr), 1e18))
                for pr in spare:
                    if need <= 0:
                        break
                    if press_gt_count.get((p_, pr), 0) >= CONFIG.thresholds.max_gts_per_press:
                        continue
                    per_press = (86400.0 / max(timing.cure_cadence_s(p_, pr), 1.0)
                                 * days * CONFIG.thresholds.curing_load_target)
                    free = press_free.get((p_, pr), per_press)
                    if free <= 1:
                        continue
                    take = min(free, need)
                    press_free[(p_, pr)] = free - take
                    need -= take
                    chosen.append(pr)
                    press_gt_count[(p_, pr)] = press_gt_count.get((p_, pr), 0) + 1
                    extended += 1
            if need > 0:
                # SECOND PASS: the GT is still short. Relax the per-press GT cap
                # (budget still enforced) before giving up. Leaving `need`
                # unplaced is what silently overloaded presses: the assignment
                # step below distributes the GT's FULL tyre count across
                # `chosen` in proportion to `takes`, so any shortfall is scaled
                # back onto those same presses -- press 297 ended up with 6,285
                # tyres against a 4,494 budget (1.4x), and 32 of 88 PCR presses
                # exceeded a month of work while 9 sat idle 454h.
                for pr in _cands:
                    if need <= 0:
                        break
                    cad = max(timing.cure_cadence_s(p_, pr), 1.0)
                    free_s = press_free.get((p_, pr), _budget0)
                    setup_s = (timing.mould_change_s(p_, pr)
                               if press_gt_count.get((p_, pr), 0) > 0 else 0.0)
                    if free_s - setup_s <= cad:
                        continue
                    take = min((free_s - setup_s) / cad, need)
                    press_free[(p_, pr)] = free_s - setup_s - take * cad
                    need -= take
                    if pr in chosen:
                        takes[chosen.index(pr)] += take
                    else:
                        chosen.append(pr)
                        takes.append(take)
                        press_gt_count[(p_, pr)] = press_gt_count.get((p_, pr), 0) + 1
                if need > 0:
                    unplaced[p_] = unplaced.get(p_, 0.0) + need
            if not chosen:
                fall = cand.get((p_, g_), [])
                if fall:
                    chosen, takes = [fall[0]], [1.0]
            if chosen:
                # MODEL #2 -- weighted fair share, not even round-robin.
                # Presses differ 223-972 s/tyre, so splitting a GT's tyres
                # evenly hands a slow press a share it cannot clear and its
                # queue runs past month end. Share in proportion to the
                # capacity actually reserved on each press.
                tot = sum(takes) or 1.0
                cum, acc = [], 0.0
                for t_ in takes:
                    acc += t_ / tot
                    cum.append(acc)
                alloc[(p_, g_)] = chosen
                alloc_cum[(p_, g_)] = cum
                # Integer tyre quota per press == what was actually reserved.
                quota[(p_, g_)] = [max(1, int(round(t_))) for t_ in takes]
        tyres = tyres.with_columns(
            pl.int_range(pl.len()).over(["plant", "gt_code"]).alias("_seq"),
            pl.len().over(["plant", "gt_code"]).alias("_cnt"))
        assign = []
        for p_, g_, seq, cur_press, cnt in zip(
                tyres["plant"].to_list(), tyres["gt_code"].to_list(),
                tyres["_seq"].to_list(), tyres["press"].to_list(), tyres["_cnt"].to_list()):
            ps = alloc.get((p_, g_))
            if not ps:
                assign.append(cur_press)
                continue
            # HARD QUOTA, not a proportion. The packer reserves `take_i` tyres on
            # press i under a 744h budget; distributing the GT's tyres by
            # proportion silently exceeded it -- press 297 held 6,285 tyres
            # against a 4,494 reservation (1.4x), 32 of 88 PCR presses ran over a
            # month of work while 9 idled 454h, and that imbalance is what
            # stretched the cure span to 1,180h against the plant's 744h.
            #
            # Interleave within the quota (round-robin over a repeating pattern
            # scaled to the quotas) so presses stay busy concurrently rather than
            # running as contiguous blocks.
            # MEASURED: enforcing the reservation as an integer quota is WORSE
            # (span 1,180h -> 1,316h, aging 315h -> 480h). Rounding each take to
            # a whole tyre distorts the split for GTs spread over many presses,
            # and the residual lands on one press. Keep the proportional form.
            cum = alloc_cum.get((p_, w_, g_)) or [1.0]
            per = min(997, max(int(cnt), 1))
            u = (seq % per + 0.5) / per
            assign.append(ps[min(bisect.bisect_left(cum, u), len(ps) - 1)])
        # Keep `_day` -- the press timeline sorts campaigns by it below.
        tyres = tyres.with_columns(pl.Series("press", assign)).drop(["_seq", "_cnt", "_win"])
        log.info("curing.press_allocated", gt_windows=len(alloc), extended=extended, unplaced=unplaced,
                 presses=len({(p, x) for (p, _g), v in alloc.items() for x in v}))
    else:
        tyres = tyres.with_columns(
            pl.int_range(pl.len()).over(["plant", "_day", "gt_code"]).alias("_seq"),
            pl.len().over(["plant", "_day", "gt_code"]).alias("_cnt"))
        assign = []
        for p_, d_, g_, seq, cur_press, cnt in zip(
                tyres["plant"].to_list(), tyres["_day"].to_list(), tyres["gt_code"].to_list(),
                tyres["_seq"].to_list(), tyres["press"].to_list(), tyres["_cnt"].to_list()):
            ps = day_alloc.get((p_, d_, g_))
            if not ps:
                assign.append(cur_press)
                continue
            cum = day_alloc_cum.get((p_, d_, g_)) or [1.0]
            # Divide by the GT-day's OWN tyre count. A fixed cycling period
            # (e.g. seq % 499) overshoots when a GT-day has fewer tyres than the
            # period: u never reaches the last press's band, so it is starved and
            # the fast presses are overloaded -- cure span went 1,210h -> 2,083h.
            # Contiguous blocks are harmless here because the whole group is one
            # day's work.
            n_ = max(int(cnt), 1)
            u = (seq + 0.5) / n_
            assign.append(ps[min(_bisect.bisect_left(cum, u), len(ps) - 1)])
        # Keep `_day` -- the press timeline sorts campaigns by it below.
        tyres = tyres.with_columns(pl.Series("press", assign)).drop(["_seq", "_cnt"])
        log.info("curing.press_allocated_daily", day_slots=len(day_alloc),
                 presses=len({(p, x) for (p, _d, _g), v in day_alloc.items() for x in v}))

    cap = None
    if cap:
        tyres = tyres.with_columns(pl.col("built_ts").cast(pl.Date).alias("built_date"))
        # Order candidates per (plant, gt_code) descending by n so pop() spills to next.
        alt_lookup: dict[tuple[str, str], list[str]] = {}
        for (plant, gt), g in press_map.group_by(["plant", "gt_code"]):
            alt_lookup[(plant, gt)] = g.sort("cdf")["press"].to_list()
        # The historical p95 cap admits more tyres per press-day than the press
        # can physically clear at its cadence (PCR p95 ~200/day vs 86400/590s =
        # 146/day), so a backlog compounds daily. Bound the cap by the cadence.
        # NB: doing this ALONE made aging worse (293h -> 520h) because when every
        # candidate was full the loop fell through to `chosen or press` and
        # dumped the overflow back onto the single preferred press. The two must
        # be fixed together -- overflow now goes to the least-loaded candidate.
        cad_cap: dict[tuple[str, str], float] = {}
        for key, p95 in cap.items():
            cad_cap[key] = min(p95, 86400.0 / max(timing.cure_cadence_s(*key), 1.0))

        press_day_load: dict[tuple[str, str, object], int] = {}
        reassigned: list[str] = []
        # Establish the iteration order ONCE and keep it. This previously walked
        # `tyres.sort("built_ts")` but assigned the result back positionally to
        # the *unsorted* frame, so presses landed on the wrong rows -- mixing
        # them across plants and inflating curing changeovers.
        tyres = tyres.sort("built_ts")
        for row in tyres.iter_rows(named=True):
            p, gt, d = row["plant"], row["gt_code"], row["built_date"]
            press = row["press"]
            alts = alt_lookup.get((p, gt), [press])
            # Try preferred first, then walk alternates.
            cands = [press] + [x for x in alts if x != press]
            chosen = None
            for c_press in cands:
                cur = press_day_load.get((p, c_press, d), 0)
                cap_val = cad_cap.get((p, c_press), cap.get((p, c_press), 200.0))
                if cur < cap_val:
                    chosen = c_press
                    press_day_load[(p, c_press, d)] = cur + 1
                    break
            if chosen is None:
                # Every candidate is full today. Spread the overflow onto the
                # least-loaded one rather than piling it on the preferred press.
                chosen = min(cands, key=lambda cp: press_day_load.get((p, cp, d), 0))
                press_day_load[(p, chosen, d)] = press_day_load.get((p, chosen, d), 0) + 1
            reassigned.append(chosen)
        tyres = tyres.with_columns(pl.Series("press", reassigned)).drop("built_date")
        log.info("curing.capacity_balanced", n=len(reassigned))

    # 2c) Opening WIP carries its true build timestamp (needed to age it
    #     correctly), but it was built *before* this plan starts -- so its
    #     earliest cure is the window open, not its build time. Without this the
    #     press timeline reaches back into the previous month and a cure can
    #     sort ahead of its own supply, which the ledger then reports as
    #     starvation. `start` was previously accepted and never used.
    # +1s margin: build credits land on fractional seconds (setup_end +
    # cycle_s*i with float cycle), so a cure computed to the same instant lands
    # a fraction BEFORE its own supply and the verifier rightly calls it
    # negative GT. One second is immaterial against ~285s cures.
    tyres = tyres.with_columns(
        (pl.max_horizontal(pl.col("built_ts"), pl.lit(start))
         + pl.duration(seconds=1)).alias("avail_ts")
    )

    # 3) Press occupancy per tyre, per press, from observed throughput. Using
    #    the in-press dwell time here understates capacity ~3x and makes cure
    #    wait diverge -- see TimingLookup._load_cure_cadence.
    keys = tyres.select(["plant", "press"]).unique()
    cadence = pl.DataFrame({
        "plant": keys["plant"],
        "press": keys["press"],
        "cycle_s": [timing.cure_cadence_s(p, m)
                    for p, m in zip(keys["plant"].to_list(), keys["press"].to_list())],
    })
    tyres = tyres.join(cadence, on=["plant", "press"], how="left").with_columns(
        pl.col("cycle_s").fill_null(1800.0).cast(pl.Float64)
    )

    # 4) Vectorized press scheduling.
    #    Per (plant, press) ordered by built_ts, cumulative cycle_s determines
    #    start_ts = max(built_ts, prev_end_on_press).
    # A mould change costs 210-430 min (PCR) / 361 min (TBR) per the CTP master.
    # It was previously reported in the audit but never charged on the timeline,
    # so the planner saw press changeovers as free. Charge it whenever the GT on
    # a press changes -- this is what makes C3/C4/P8/P9 a real trade-off rather
    # than a number to minimise for free.
    keys2 = tyres.select(["plant", "press"]).unique()
    chg_tbl = pl.DataFrame({
        "plant": keys2["plant"],
        "press": keys2["press"],
        "mould_change_s": [timing.mould_change_s(p, m) for p, m
                           in zip(keys2["plant"].to_list(), keys2["press"].to_list())],
    })
    tyres = tyres.join(chg_tbl, on=["plant", "press"], how="left").with_columns(
        pl.col("mould_change_s").fill_null(0.0))

    # Run each press as CAMPAIGNS, not interleaved. Ordering a shared press
    # purely by arrival time makes it hop between GTs continuously, and every
    # hop is a 3.5-7h mould change -- that is what put us at ~1,032 changeovers
    # against the reference implementation's ~71 for PCR. Group each press's
    # tyres by GT and run those blocks in order of first arrival, so a press
    # changes over once per GT it serves (rules C3/C4).
    tyres = tyres.with_columns(
        pl.col("avail_ts").min().over(["plant", "press", "gt_code"]).alias("_camp_start"))
    # MEASURED: a purely work-conserving order (sort by avail_ts alone) is far
    # WORSE -- span 1,155h -> 4,287h. The press stops idling but hops between
    # GTs constantly: 5,196 changeovers x ~6h = 31,000 press-hours of mould
    # changes, which costs far more than the idle it removed.
    #
    # This is the crux: with a 6h mould change a press CANNOT opportunistically
    # take whatever is available. It must commit to a GT for a long run, and
    # committing means waiting for that GT. Idle is the cheaper of the two.
    tyres = tyres.sort(["plant", "press", "_day", "gt_code", "avail_ts"])
    tyres = tyres.with_columns(
        pl.col("gt_code").shift(1).over(["plant", "press"]).alias("_prev_gt"))
    tyres = tyres.with_columns(
        pl.when(pl.col("_prev_gt").is_not_null() & (pl.col("_prev_gt") != pl.col("gt_code")))
          .then(pl.col("mould_change_s")).otherwise(0.0).alias("chg_s"))
    tyres = tyres.with_columns(
        (pl.col("cycle_s") + pl.col("chg_s"))
          .cum_sum().over(["plant", "press"])
          .alias("cum_cycle_s"),
        # earliest possible if press were always free from start:
        pl.col("avail_ts").min().over(["plant", "press"]).alias("press_base_ts"),
    )
    # This gives naive end_ts = press_base_ts + cum_cycle_s. But avail_ts may
    # push a specific tyre later â€” so we take max(naive_end, avail_ts + cycle_s).
    # Sequential press simulation:  end[i] = max(end[i-1], avail[i]) + work[i]
    # -- a press takes the next tyre in campaign order as soon as it is both
    # free and the tyre exists. The old max(naive_end, avail+cycle) let a
    # late-arriving tyre leapfrog the rest of its campaign, which is why two GTs
    # sharing a press alternated for hundreds of changeovers.
    #
    # The recurrence unrolls to a cumulative max, so it stays vectorised:
    #     end[i] = C[i] + max_{j<=i}( avail[j] - C[j] + work[j] )
    # with C the running total of work (= cum_cycle_s, which already includes
    # the mould-change time charged above).
    # STOCK-PULL SIMULATION replaces the per-press tyre list entirely.
    sim, sim_stats = _simulate_presses(tyres, timing, start)
    if sim.height:
        plant_of = dict(tyres.select(["press", "plant"]).unique().iter_rows())
        cure_df = sim.with_columns(
            pl.col("press").replace_strict(plant_of, default="PCR").alias("plant"),
            pl.from_epoch(pl.col("_s").cast(pl.Int64), time_unit="s").alias("start_ts"),
            pl.from_epoch(pl.col("_e").cast(pl.Int64), time_unit="s").alias("end_ts"),
        ).drop(["_s", "_e"])
        cure_df = cure_df.with_row_index(name="cure_id").with_columns(
            (pl.lit("cure-") + pl.col("cure_id").cast(pl.Utf8) + pl.lit("-")
             + pl.col("gt_code")).alias("lot_id"),
            pl.lit("").alias("source_build_lot"),
        )
        log.info("curing.stock_pull", tyres=cure_df.height, **sim_stats)
        mould_map = _historical_mould_map()
        if mould_map.height:
            primary = mould_map.group_by(["plant", "gt_code", "press"]).first().select(
                ["plant", "gt_code", "press", "mould"])
            cure_df = cure_df.join(primary, on=["plant", "gt_code", "press"], how="left")
            cure_df = cure_df.with_columns(
                (pl.col("mould").fill_null(pl.lit("MOULD_NA"))
                 + pl.lit("@") + pl.col("press")).alias("mould"))
        else:
            cure_df = cure_df.with_columns(pl.lit(None, dtype=pl.Utf8).alias("mould"))
        trace_default = DecisionTrace()
        trace_default.add_reason("press.stock_pull", "stat", "press_history", 1.0)
        cure_df = cure_df.with_columns(
            pl.lit(trace_default.model_dump_flat()).alias("decision_trace"))
        cure_ledger = cure_df.select([
            pl.col("start_ts").alias("ts"), pl.col("plant"), pl.col("gt_code"),
            pl.lit(-1.0).alias("qty_delta"), pl.lit("cure").alias("source"),
            pl.col("lot_id")])
        ledger.con.register("_cure_evt", cure_ledger.to_arrow())
        ledger.con.execute("INSERT INTO gt_events SELECT * FROM _cure_evt")
        ledger.con.unregister("_cure_evt")
        cure_df.write_parquet(out_dir / "cure_schedule.parquet", compression="zstd")
        log.info("curing.planned", n=cure_df.height)
        return cure_df

    tyres = tyres.with_columns(
        (pl.col("cycle_s") + pl.col("chg_s")).alias("_work"),
        pl.col("avail_ts").dt.epoch("s").cast(pl.Float64).alias("_avail_s"),
    )
    tyres = tyres.with_columns(
        (pl.col("_avail_s") - pl.col("cum_cycle_s") + pl.col("_work")).alias("_K"))
    tyres = tyres.with_columns(
        (pl.col("cum_cycle_s") + pl.col("_K").cum_max().over(["plant", "press"]))
        .alias("_end_s"))

    # PRESS PACING -- spread each press's work across the whole horizon.
    #
    #   start_i = max( avail_i , end_{i-1} , t0 + (i/N_p)*H )
    #
    # Measured: the plant's presses are active 28.3 of 31 days at CV 0.28 and
    # ~84% utilisation. They are NOT run flat out; they are paced to fill the
    # month. Our sequential simulation consumed tyres as fast as they arrived,
    # so presses finished at wildly different times and the last one set a
    # 1,350h span. Pacing makes every press finish at H, so span = H by
    # construction, and a constant per-press cure rate is exactly what building's
    # constant build rate can feed -- which is what collapses aging.
    #
    # Same principle as the building fix that took machine utilisation 93.5% ->
    # 82%: subordinate the rate to the horizon. We had applied it to building
    # and never to presses.
    _t0 = float(pl.Series([start]).dt.epoch("s")[0])
    _H = CONFIG.thresholds.cure_horizon_days * 86400.0
    tyres = tyres.with_columns(
        pl.int_range(pl.len()).over(["plant", "press"]).alias("_pi"),
        pl.len().over(["plant", "press"]).alias("_pn"))
    tyres = tyres.with_columns(
        (_t0 + (pl.col("_pi") + 1).cast(pl.Float64)
         / pl.col("_pn").cast(pl.Float64) * _H).alias("_pace_s"))
    tyres = tyres.with_columns(
        pl.max_horizontal("_end_s", "_pace_s").alias("_end_s")).drop(["_pi", "_pn"])
    # Millisecond epoch, not seconds: build credits land on half-seconds, so
    # truncating to whole seconds put a cure 0.5s before its own supply and the
    # verifier (correctly) called it negative GT.
    tyres = tyres.with_columns(
        pl.from_epoch((pl.col("_end_s") * 1000).ceil().cast(pl.Int64),
                      time_unit="ms").alias("end_ts"))
    # Derive start in the same millisecond arithmetic. pl.duration(seconds=...)
    # on a float truncates, which shaved a fraction off start_ts and tripped the
    # negative-GT check against half-second build credits.
    tyres = tyres.with_columns(
        pl.from_epoch(((pl.col("_end_s") - pl.col("cycle_s")) * 1000).ceil().cast(pl.Int64),
                      time_unit="ms").alias("start_ts"))

    # 5) Mould assignment per (plant, gt_code, press) â€” each press has its own
    #    physical mould copy in the real plant, so different presses running
    #    the same GT do not double-book a single mould. We label the mould
    #    with the press suffix so the independent verifier treats them as
    #    distinct physical instances.
    mould_map = _historical_mould_map()
    if mould_map.height:
        primary_mould = mould_map.group_by(["plant", "gt_code", "press"]).first().select(
            ["plant", "gt_code", "press", "mould"]
        )
        tyres = tyres.join(primary_mould, on=["plant", "gt_code", "press"], how="left")
        tyres = tyres.with_columns(
            (pl.col("mould").fill_null(pl.lit("MOULD_NA"))
             + pl.lit("@") + pl.col("press")).alias("mould")
        )
    else:
        tyres = tyres.with_columns(pl.lit(None, dtype=pl.Utf8).alias("mould"))

    # 6) Emit cure_ids + decision_trace (constant, cheap).
    tyres = tyres.with_row_index(name="cure_id")
    trace_default = DecisionTrace()
    trace_default.add_reason("press.historical.primary", "stat", "press_history", 1.0)
    trace_default.add_satisfied("mould_no_double_book")
    trace_json = trace_default.model_dump_flat()

    cure_df = tyres.select([
        pl.col("cure_id"),
        (pl.lit("cure-") + pl.col("cure_id").cast(pl.Utf8) + pl.lit("-") + pl.col("gt_code"))
            .alias("lot_id"),
        pl.col("plant"),
        pl.col("gt_code"),
        pl.col("press").cast(pl.Utf8),
        pl.col("mould"),
        pl.col("start_ts"),
        pl.col("end_ts"),
        pl.col("cycle_s"),
        pl.col("lot_id").alias("source_build_lot"),
    ]).with_columns(pl.lit(trace_json).alias("decision_trace"))

    # 7) Ledger cure debits (bulk-insert via register â€” no 400K python objects).
    cure_ledger = cure_df.select([
        pl.col("start_ts").alias("ts"),
        pl.col("plant"),
        pl.col("gt_code"),
        pl.lit(-1.0).alias("qty_delta"),
        pl.lit("cure").alias("source"),
        pl.col("lot_id"),
    ])
    ledger.con.register("_cure_evt", cure_ledger.to_arrow())
    ledger.con.execute("INSERT INTO gt_events SELECT * FROM _cure_evt")
    ledger.con.unregister("_cure_evt")

    cure_df.write_parquet(out_dir / "cure_schedule.parquet", compression="zstd")
    log.info("curing.planned", n=cure_df.height)
    return cure_df


















