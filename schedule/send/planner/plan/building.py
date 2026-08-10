"""Heuristic Building planner.

1. Cluster lots by sister-SKU (from KB); order sisters back-to-back on same
   machine using the KB's canonical_order.
2. For each lot, pick machine argmax(MPM confidence Ã— free-time bonus)
   respecting hard rules.
3. Time-place using cycle+setup Î¼; write decision_trace.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import polars as pl

from planner.config import CONFIG
from planner.plan.calendar import MachineTimer
from planner.plan.decision_trace import DecisionTrace
from planner.plan.ledger import GreenTireLedger
from planner.plan.lots import Lot
from planner.plan.rulekb import RuleBase
from planner.plan.shift_grid import SHIFT_S
from planner.plan.timing_lookup import TimingLookup
from planner.runs.logger import log


STAGE_BUILD = 2  # planner schedules the finishing stage (stage 2 = green tyre)


def _cure_rate_per_h(plant: str) -> float:
    """Tyres/hour the presses can absorb â€” the pace building must not exceed.

    Measured as the plant's own median daily cured volume. Building faster than
    this does not build inventory, it builds scrap: a green tyre uncured within
    `gt_shelf_life_h` is waste.
    """
    from planner.data.warehouse import duck
    try:
        r = duck().execute(
            """
            WITH d AS (
                SELECT CAST(event_ts - INTERVAL 7 HOUR AS DATE) AS day, count(*) AS n
                FROM v_curing WHERE plant = ? AND statuscritical = 'Normal'
                GROUP BY 1 HAVING count(*) > 100
            )
            SELECT median(n) FROM d
            """, [plant]).fetchone()
        if r and r[0]:
            return float(r[0]) / 24.0
    except Exception as e:  # noqa: BLE001
        log.warning("building.cure_rate_unavailable", plant=plant, err=str(e))
    return 0.0


def _gt_machine_map(plant: str) -> dict[str, list[str]]:
    """Feasible machines per GT, busiest first.

    Prefers the derived master `warehouse/derived/allowed_machine_matrix.parquet`
    (see data/derive_masters.py). That matrix is capability, mined over the FULL
    history: it covers 99 % of the machine-GT pairs the plant actually used in
    Jan-2026, where the visible-window fallback below covers only 57 %. Planning
    off the narrow set withheld half the plant's routing options and forced
    demand onto a minority of machines.

    MPM still supplies the preference ORDER; this is only the feasibility set.
    """
    from planner.config import CONFIG as _C
    mpath = _C.paths.warehouse / "derived" / "allowed_machine_matrix.parquet"
    if mpath.exists():
        try:
            import polars as _pl
            m = _pl.read_parquet(mpath).filter(_pl.col("plant") == plant)
            if m.height:
                out: dict[str, list[str]] = {}
                # direct evidence first, size-widened after
                for r in m.sort("basis").iter_rows(named=True):
                    out.setdefault(r["gt_code"], []).append(r["machine"])
                log.info("building.allowed_matrix", plant=plant, gts=len(out))
                return out
        except Exception as e:  # noqa: BLE001
            log.warning("building.allowed_matrix_failed", err=str(e))

    from planner.data.warehouse import duck
    rows = duck().execute(
        "SELECT itemCode, machineCode, count(*) AS n FROM v_build "
        "WHERE plant = ? AND stage = ? AND QualityStatus = '1' "
        "GROUP BY 1, 2 ORDER BY 1, n DESC",
        [plant, STAGE_BUILD],
    ).fetchall()
    out: dict[str, list[str]] = {}
    for gt, machine, _n in rows:
        if gt and machine:
            out.setdefault(gt, []).append(machine)
    return out


def _machine_size_lock(plant: str, timing) -> dict[str, str]:
    """machine -> the rim size it is locked to. Measured 99.89% PCR / 99.75% TBR.

    A building machine essentially never changes rim size, so this belongs in the
    CANDIDATE SET, not in the score. It has been a soft term in `score` through
    several attempts and never reached the assignment layer, because the score is
    dominated by MPM confidence and queue depth. As a prefilter it is absolute.

    This is the one place history is a hard gate, and it is legitimate because it
    is a property of the MACHINE -- not a (machine, GT) feasibility claim, which
    would be the Retraction-2 error (40-47% of pairs are new every month).
    """
    from planner.data.warehouse import duck
    out: dict[str, str] = {}
    try:
        rows = duck().execute(
            "SELECT machineCode, itemCode, count(*) FROM v_build "
            "WHERE plant = ? AND stage = ? AND itemCode IS NOT NULL GROUP BY 1,2",
            [plant, STAGE_BUILD]).fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("building.size_lock_unavailable", plant=plant, err=str(e))
        return out
    tally: dict[str, dict[str, int]] = {}
    for machine, gt, n in rows:
        s = timing._size_for_gt(gt) if gt else None
        if s:
            tally.setdefault(machine, {})[s] = tally.setdefault(machine, {}).get(s, 0) + int(n)
    for machine, sizes in tally.items():
        out[machine] = max(sizes, key=sizes.get)
    log.info("building.size_lock", plant=plant, machines=len(out))
    return out


def _fallback_machines(plant: str) -> list[str]:
    # If a lot has zero MPM rows (novel SKU), enumerate machines seen for the plant
    # from historical build events. Cached at call sites in practice.
    from planner.data.warehouse import duck
    con = duck()
    rows = con.execute(
        "SELECT DISTINCT machineCode FROM v_build WHERE plant = ? AND stage = ?",
        [plant, STAGE_BUILD],
    ).fetchall()
    return [r[0] for r in rows]


def _sequence_order(rb: RuleBase, plant: str, stage: int, lots: list[Lot]) -> list[Lot]:
    """Reorder lots by sister-cluster canonical order, then across clusters
    apply mined `sequence.follow` rules (highest-confidence first)."""
    if not lots:
        return lots

    def sister_key(lot: Lot):
        # DUE DATE FIRST, then sister grouping within the day.
        #
        # Sorting by sister group first built a GT's entire month back-to-back
        # (days 1-3), after which those tyres waited weeks for their press --
        # the dominant cause of GT aging, and unfixable downstream because the
        # tyres were already built. The plant builds ~22 SKUs *every day* and
        # cycles 2-3 per machine per day (RULEBOOK.md), which is what keeps its
        # median build->cure lag at 4.3h.
        #
        # Grouping sisters *within* the day still gets the setup saving: a
        # machine runs its same-size cluster consecutively, just scoped to a day
        # rather than the whole month.
        sg = rb.sister_of(plant, lot.gt_code)
        if sg is None:
            return (lot.due_date, "__nosister__", 0, lot.gt_code)
        try:
            idx = sg.canonical_order.index(lot.gt_code)
        except ValueError:
            idx = 999
        return (lot.due_date, sg.rule_id, idx, lot.gt_code)

    ordered = sorted(lots, key=sister_key)

    seq_rules = rb.sequences.get((plant, stage), [])
    if not seq_rules:
        return ordered

    # Build a follow-preference map: (gt_a, gt_b) -> confidence when the
    # sequence rule says A â†’ B (pattern of length 2). Higher = prefer B right
    # after A.
    follow: dict[tuple[str, str], float] = {}
    for r in seq_rules:
        if len(r.pattern) == 2 and r.confidence >= 0.6:
            a, b = r.pattern[0], r.pattern[1]
            follow[(a, b)] = max(follow.get((a, b), 0.0), r.confidence)
    if not follow:
        return ordered

    # Greedy chain re-order: for adjacent lots in `ordered`, if a preferred
    # successor for lot_i exists further ahead, swap it to position i+1.
    for i in range(len(ordered) - 1):
        a = ordered[i].gt_code
        best_j = None
        best_c = 0.0
        for j in range(i + 1, min(i + 20, len(ordered))):
            c = follow.get((a, ordered[j].gt_code), 0.0)
            if c > best_c:
                best_c = c
                best_j = j
        if best_j is not None and best_j != i + 1:
            ordered[i + 1], ordered[best_j] = ordered[best_j], ordered[i + 1]
    return ordered


def plan_building(
    lots: list[Lot],
    rb: RuleBase,
    timing: TimingLookup,
    ledger: GreenTireLedger,
    start: datetime,
    assigned: dict | None = None,
    horizon_end: datetime | None = None,
    unplaced: list | None = None,
) -> pl.DataFrame:
    if not lots:
        log.warning("building.no_lots")
        return pl.DataFrame()

    # Group lots by plant so we sequence per plant.
    plants = sorted({l.plant for l in lots})
    machine_cache: dict[str, list[str]] = {}
    timers: dict[str, MachineTimer] = {p: MachineTimer(start) for p in plants}
    # Track last SKU on each machine to compute setup properly.
    last_on: dict[str, str | None] = {}

    scheduled: list[dict] = []
    # Collected per-lot GT-credit rows for a single bulk-insert at the end.
    gt_credit_rows: list[dict] = []

    for plant in plants:
        plant_lots = [l for l in lots if l.plant == plant]
        plant_lots = _sequence_order(rb, plant, STAGE_BUILD, plant_lots)
        timer = timers[plant]
        cycle_s = timing.build_cycle_s(plant)
        gt_machines = _gt_machine_map(plant)
        # gt_code -> machine currently running that GT, for run continuation.
        gt_last_machine: dict[str, str] = {}
        cure_rate_h = _cure_rate_per_h(plant)
        build_ahead_h = CONFIG.thresholds.gt_build_ahead_h
        # Per-GT cure rate: split the plant's press throughput by each GT's share
        # of demand. lambda_g is what sizes that GT's WIP cap.
        _tot = sum(float(l.qty) for l in plant_lots) or 1.0
        _by_gt: dict[str, float] = {}
        for l in plant_lots:
            _by_gt[l.gt_code] = _by_gt.get(l.gt_code, 0.0) + float(l.qty)
        gt_rate_h = {g: cure_rate_h * q / _tot for g, q in _by_gt.items()}
        built_g: dict[str, float] = {}
        built_so_far = 0.0
        size_lock = _machine_size_lock(plant, timing)
        size_lock_misses = 0
        widened = 0
        log.info("building.conwip", plant=plant, cure_rate_per_h=round(cure_rate_h, 1),
                 w_target_h=build_ahead_h, gts=len(gt_rate_h))

        for lot in plant_lots:
            trace = DecisionTrace()

            # 1) Candidate machines: MPM preferences first (their index lines up
            #    with mpm_rows for scoring), then any other machine that has
            #    historically built this GT, so load can spill when needed.
            mpm_rows = rb.machines_for(plant, STAGE_BUILD, lot.gt_code)
            preferred = [e.machine for e in mpm_rows]
            feasible = gt_machines.get(lot.gt_code, [])
            candidates = preferred + [m for m in feasible if m not in preferred]
            # Score by MACHINE, not by position. The loop below used to read
            # mpm_rows[i] off the candidate index, which is only correct while
            # `candidates` starts with `preferred` in order -- any filter applied
            # here silently mis-attributes every preference.
            mpm_by_machine = {e.machine: e for e in mpm_rows}
            # ASSIGNED MACHINE FIRST (engine/assign.py). Measured: a GT's top
            # machine carries 94.2% of its month and a (machine, GT) run is
            # active 10 of 13 days. Our stickiness was 41% because nothing told
            # building to COMMIT a GT to a machine -- it re-chose per lot, so a
            # GT's daily quota scattered across the fleet.
            _asg = assigned.get((plant, lot.gt_code)) if assigned else None
            if _asg:
                _keep = [m for m in _asg if m in set(candidates)] or _asg
                candidates = _keep + [m for m in candidates if m not in set(_keep)]
            # SIZE LOCK (hard). Keep only machines locked to this GT's rim size;
            # fall through if that would empty the set, so a novel size can still
            # be placed rather than silently dropped.
            _sz = timing._size_for_gt(lot.gt_code)
            if _sz:
                _keep = [m for m in candidates if size_lock.get(m, _sz) == _sz]
                if _keep:
                    candidates = _keep
                else:
                    size_lock_misses += 1
            # DAY CAPACITY GATE. Prefer machines that can still start this lot
            # inside its own day. Without it a machine drifts permanently behind:
            # the MPM bonus (24h) plus the run-continuation bonus (36h) outweigh
            # 60h of extra queue, so the same machine keeps winning lots it has
            # no room for, and the tail slid to 947h in a 744h month while no
            # machine held more than 718h of work.
            if lot.due_date is not None:
                # Deadline is the END OF THE LOT'S OWN DAY -- a calendar
                # boundary, not a tuned slack. A lot the press wants on day d
                # must be built during day d.
                _dl = (datetime.combine(lot.due_date, datetime.min.time())
                       + timedelta(seconds=3 * SHIFT_S))
                _ready = [m for m in candidates if timer.next_free(m) <= _dl]
                if not _ready:
                    # Every historically-seen machine for this GT is already
                    # booked past the deadline. Widen to the whole plant, minus
                    # the size lock, rather than queueing behind them: 47 lots
                    # pinned this way to two PCR machines carried the build span
                    # 9 days past month end while one of them idled 210h.
                    # Per Retraction 2 the (machine, GT) history is a PREFERENCE,
                    # never a feasibility set -- 40-47% of pairs are new monthly.
                    if plant not in machine_cache:
                        machine_cache[plant] = _fallback_machines(plant)
                    _wide = machine_cache[plant]
                    if _sz:
                        _wide = [m for m in _wide if size_lock.get(m, _sz) == _sz] or _wide
                    _ready = [m for m in _wide if timer.next_free(m) <= _dl]
                    if _ready:
                        widened += 1
                if _ready:
                    candidates = _ready
            if not candidates:
                # NB: dict.setdefault evaluates its default eagerly, so the
                # obvious one-liner re-ran this full-table scan for every lot.
                if plant not in machine_cache:
                    machine_cache[plant] = _fallback_machines(plant)
                candidates = machine_cache[plant]
                if not candidates:
                    log.warning("building.no_machines", plant=plant, gt=lot.gt_code)
                    continue

            # 2) Score machines: prefer high-conf MPM, but bound how much queue
            #    delay that preference may buy. The old weight of 1000 was in
            #    hours, so a fully-preferred machine outranked an idle one until
            #    it was ~1000h more loaded -- i.e. preference always won and the
            #    top machine absorbed the plant. Tolerance is now an explicit
            #    "how many hours of extra wait is preference worth" knob.
            tol_h = CONFIG.thresholds.mpm_delay_tolerance_h
            cont_h = CONFIG.thresholds.gt_continuation_bonus_h
            # Lots of one GT are adjacent in the ordering, so "machine that last
            # took this GT" is the run currently open for it. Keeping it there
            # extends the run instead of opening a second one elsewhere.
            open_run_machine = gt_last_machine.get(lot.gt_code)
            scored = []
            for m in candidates:
                mpm_score = 0.0
                mpm_rid = None
                mpm_type = "stat"
                _e = mpm_by_machine.get(m)
                if _e is not None:
                    mpm_score = _e.confidence * _e.weight
                    mpm_rid = _e.rule_id
                    mpm_type = _e.type
                free_at = timer.next_free(m)
                # Earlier-free is better; combine linearly.
                free_pen = (free_at - start).total_seconds() / 3600.0
                cont_bonus = cont_h if m == open_run_machine else 0.0
                score = mpm_score * tol_h + cont_bonus - free_pen
                scored.append((score, m, mpm_rid, mpm_type))
            scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
            _, machine, rid, rtype = scored[0]
            if open_run_machine == machine:
                trace.add_satisfied("gt_run_continuation")
            gt_last_machine[lot.gt_code] = machine

            # 3) Setup + cycle -- cadence is per machine, not per plant.
            prev = last_on.get(machine)
            setup_s = timing.setup_s(plant, machine, prev, lot.gt_code)
            cycle_s = timing.build_cycle_s(plant, machine)
            duration_s = setup_s + cycle_s * lot.qty

            # 3b) CONWIP release control, PER GT (Little's Law: W = L/lambda).
            #
            # Plant and planner have identical cure rate lambda, yet the plant's
            # lag is 4.5h and ours was 40h -- so we simply carried ~9x the WIP.
            # Capping WIP per GT at B_g = lambda_g * W_target therefore *bounds*
            # the wait at W_target; it is arithmetic, not a tuned heuristic.
            #
            # Per GT matters: a plant-level cumulative cap does nothing for a GT
            # whose own presses are behind, which is why the earlier version
            # barely moved aging.
            # MEASURED (Jan 2026): this is OFF by default because open-loop it
            # does not work. True CONWIP needs feedback -- release when a cure
            # actually completes -- but curing is planned *after* building here,
            # so there is nothing to observe. It degenerates into a fixed-rate
            # pacer: W_target 24h and 8h give identical output (481.9 vs 482.2h
            # aging) because the two differ only by a constant b/lambda offset.
            # Result: aging 666->482h but makespan 705->1057h (past the month)
            # and machine util 81%->54%. Enabling it requires scheduling curing
            # first so the release signal exists.
            # RATE PACING -- subordinate building to the curing constraint.
            #
            # Measured: the plant runs its building machines at 77.9% PCR /
            # 76.9% TBR and holds a build/cure ratio of 1.007. It keeps machines
            # idle ~22% ON PURPOSE, because curing is the bottleneck and a
            # non-bottleneck running faster makes only WIP and scrap. The target
            # utilisation is not a free choice:
            #     util = cure_capacity / machine_capacity = 11,671/15,086 = 77.4%
            # which is what the plant achieves.
            #
            # Unpaced we build 14,367/day against 7,600/day of curing (ratio
            # 1.8), WIP peaks at 88,851 vs the plant's +/-624, and the cure span
            # runs to 48 days. Releasing the nth tyre no earlier than
            # n/cure_rate paces building to exactly the cure rate.
            # SUPERSEDED BY THE DAILY IDENTITY (v2 rule 9). With curing planned
            # first, every lot already carries a due date derived from the press
            # plan -- built(g,d) = cured(g,d+1) -- so pacing on a PLANT-WIDE
            # cumulative counter is both redundant and wrong. It is a single
            # scalar, so a GT whose presses are idle today is throttled by the
            # total built for every other GT; that pushed the build span to 835h
            # against curing's 736h at only 65% machine utilisation.
            #
            # Release on the lot's own day instead. A lot may not start before
            # the day the press plan wants it, and nothing holds it after.
            # The due date is a TARGET with a look-ahead, not a hard floor. No
            # machine carries more than 718h of work in a 744h month, so a
            # machine forced to sit idle until a day-30 lot's exact date has no
            # room left to absorb it and the tail slides past month end (span
            # 971h at 56% utilisation, with the work itself fitting). Allowing
            # release up to gt_build_ahead_h early is the same slack the shelf
            # life already permits: a tyre may wait build_ahead_h before curing.
            # The lead is ONE SHIFT, and it is not a free parameter: shift_grid
            # only pools tyres built before the shift opened, so a tyre needs
            # exactly one shift of lead to be curable on its own day, and every
            # further hour of lead is pure age. Deriving it from the grid's shift
            # length keeps plan and executor consistent across any month; a
            # hand-set look-ahead is fitted to whichever month it was tuned on.
            built_so_far += float(lot.qty)
            if lot.due_date is not None:
                release = (datetime.combine(lot.due_date, datetime.min.time())
                           - timedelta(seconds=SHIFT_S))
                if release > timer.next_free(machine):
                    timer.occupy(machine, release)

            # 4) Commit -- HORIZON IS A HARD PRECONDITION, checked BEFORE the
            #    commit, not reported after it. A lot that cannot finish inside
            #    the month is not "placed 7.55h late", it is UNPLACED, and it
            #    goes to `unplaced` with the machine and the overshoot so the
            #    rejection is explainable. Placing it anyway and reporting the
            #    breach afterwards is the failure mode the traceability rule
            #    exists to prevent -- and T_0=12 removing the symptom without
            #    fixing the path left the guard absent and the signal gone.
            if horizon_end is not None:
                peek_s = timer.next_free(machine)
                peek_e = peek_s + timedelta(seconds=duration_s)
                if peek_e > horizon_end:
                    if unplaced is not None:
                        unplaced.append({
                            "plant": lot.plant, "gt_code": lot.gt_code,
                            "lot_id": lot.lot_id, "qty": float(lot.qty),
                            "machine": machine,
                            "would_start": peek_s, "would_end": peek_e,
                            "over_h": round((peek_e - horizon_end)
                                            .total_seconds() / 3600.0, 2),
                            "reason": "no slot inside horizon on assigned machine"})
                    continue
            s_ts, e_ts = timer.commit(machine, duration_s)
            last_on[machine] = lot.gt_code
            lot.machine = machine
            lot.start_ts = s_ts
            lot.end_ts = e_ts

            # 5) Trace
            if rid:
                trace.add_reason(rid, rtype, "machine_preference",
                                 weight=mpm_rows[0].weight if mpm_rows else 0.0)
            sg = rb.sister_of(plant, lot.gt_code)
            if sg is not None:
                trace.add_reason(sg.rule_id, "stat", "sister_batch",
                                 weight=sg.similarity)
            if setup_s > 0:
                trace.add_satisfied("setup_time_stat")
            lot.trace = trace.model_dump_flat()

            # 6) Ledger credits â€” one per tyre at (setup_end + i * cycle_s), so
            #    curing can begin on each tyre as it comes off the TBM rather
            #    than waiting for the whole lot to finish. Collect here, bulk
            #    insert once after the greedy loop finishes (Arrow register).
            gt_credit_rows.append({
                "plant": plant,
                "gt_code": lot.gt_code,
                "lot_id": lot.lot_id,
                "setup_end": s_ts + timedelta(seconds=setup_s),
                "cycle_s": float(cycle_s),
                "qty": int(lot.qty),
            })

            scheduled.append({
                "lot_id": lot.lot_id, "plant": plant, "gt_code": lot.gt_code,
                "stage": "build_s2", "machine": machine,
                "start_ts": s_ts, "end_ts": e_ts, "qty": lot.qty,
                "due_date": lot.due_date, "setup_s": setup_s, "cycle_s": duration_s - setup_s,
                "decision_trace": lot.trace,
            })

    # Bulk-insert per-tyre GT credits via Arrow â€” one shot instead of ~400K
    # per-row inserts. Expand qty via pl.int_ranges then compute ts.
    if gt_credit_rows:
        credits = pl.DataFrame(gt_credit_rows).with_columns(
            pl.col("qty").cast(pl.Int64)
        )
        tyres = credits.with_columns(
            pl.int_ranges(pl.col("qty")).alias("_i")
        ).explode("_i")
        tyres = tyres.with_columns(
            (pl.col("setup_end")
             + pl.duration(seconds=pl.col("cycle_s") * (pl.col("_i").cast(pl.Float64) + 1.0))
            ).alias("ts")
        )
        ledger_df = tyres.select([
            pl.col("ts"),
            pl.col("plant"),
            pl.col("gt_code"),
            pl.lit(1.0).alias("qty_delta"),
            pl.lit("build").alias("source"),
            pl.col("lot_id"),
        ])
        ledger.con.register("_build_evt", ledger_df.to_arrow())
        ledger.con.execute("INSERT INTO gt_events SELECT * FROM _build_evt")
        ledger.con.unregister("_build_evt")
        log.info("building.gt_credits_bulk", n=ledger_df.height)

    df = pl.DataFrame(scheduled)
    log.info("building.planned", n_lots=df.height,
             n_plants=len(plants),
             span_hours=None if df.height == 0 else
             (df["end_ts"].max() - df["start_ts"].min()).total_seconds() / 3600.0)
    return df

