"""RIGHT-SHIFT POST-PASS. Remove free slack; change nothing else.

Push every build lot as late as its consuming press allows. Same lots, same
sequence, same machine assignment -- so fulfilment and changeover count are
unchanged BY CONSTRUCTION. Any movement in either is a bug, not a trade-off.

THE COMPOSITION IS min-GOVERNED, NOT ADDITIVE. Right-shift does not subtract
from W; it drives W DOWN TO the floor. The pacemaker does not subtract either;
it LOWERS the floor:

    W_final = tau_min + margin + T_c(1-delta)/2

At delta = 0.135 the floor is 9.6h; at 0.45 it is 6.1h. So right-shift never
needs to reach its own ceiling -- it needs to reach the floor.

FOUR LIMITS PER LOT, backward along the machine chain:

    press_limit  min over the lot's tyres of (their cure start) - tau_min
    succ_limit   start of the NEXT lot on that machine - its setup
    horizon      H

`succ_limit` is what resolves delta*_lot (17.54h, which breaches the 9.6h floor
and is therefore not simultaneously achievable) against delta*_GT (3.32h, which
assumes every lot moves together). Per-lot minima hold every OTHER lot fixed;
the backward pass propagates the constraint down the chain so each lot takes
only the slack its successor left. The gap between delta*_lot and achieved IS
the capacity binding, measured rather than bounded.

TAGGING the binding term turns this into the pacemaker's sizing input:
    succ_limit  binds -> machine capacity: exactly where the pacemaker pays
    press_limit binds -> already just-in-time, leave alone
    margin      binds -> at the wall; z is doing real work here

ORDERING NOTE: in the final engine this runs LAST, after the pacemaker. Wired
before it, it compacts the machine calendar toward the end of each cycle and
leaves the pacemaker fighting for the room it needs to stretch a 3h block to
11h. Total idle is conserved, so nothing is destroyed -- but the pacemaker must
then be a re-plan, not a perturbation of right-shifted output.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import polars as pl

from planner.engine.contract import ordered
from planner.plan.shift_grid import TAU_MIN_H
from planner.runs.logger import log

MARGIN_Z = 1.0           # stopping margin: z * sigma_g * sqrt(tau) / D_g
REPAIR_ITERS = 8         # FIFO re-pairing fixups; converged in 2 when measured


def right_shift(build_df: pl.DataFrame, cure_df: pl.DataFrame,
                ledger_events: pl.DataFrame, origin: datetime,
                horizon_end: datetime) -> tuple[pl.DataFrame, dict]:
    """Return (shifted build_df, stats). Lots keep identity, order and machine."""
    if build_df.height == 0 or cure_df.height == 0:
        return build_df, {"note": "empty"}

    # ---- FIFO pairing: k-th cured tyre IS the k-th built tyre ------------
    b = (ledger_events
         .filter(pl.col("source").is_in(["build", "opening"]) & (pl.col("qty_delta") > 0))
         .with_columns(pl.col("qty_delta").cast(pl.Int64))
         .with_columns(pl.int_ranges(pl.col("qty_delta")).alias("_i")).explode("_i")
         .select(["plant", "gt_code", "ts", "lot_id"])
         .sort(["plant", "gt_code", "ts", "lot_id"]))
    c = (cure_df.select(["plant", "gt_code", "start_ts"])
         .sort(["plant", "gt_code", "start_ts"]))
    bk = b.with_columns(pl.int_range(pl.len()).over(["plant", "gt_code"]).alias("k"))
    ck = c.with_columns(pl.int_range(pl.len()).over(["plant", "gt_code"]).alias("k"))
    pair = ck.join(bk, on=["plant", "gt_code", "k"], how="inner")

    # PER-TYRE, NOT PER-LOT-END. A lot's tyres feed cures progressively: tyre i
    # is built at setup_end + i*cycle and only has to precede cure i. Taking
    # "lot END before FIRST cure" instead reports 75% of volume already-tight
    # against a delta*_lot of 17.5h -- the criterion, not the chain, is what
    # binds. The correct budget is a SHIFT, min over the lot's own tyres:
    #     press_shift = min_i (cure_i - build_i) - tau_min   == delta*_lot
    # GATE LIMIT REMOVED with the gate itself. The grid now tests eligibility
    # per slot against that slot's own clock, so there is no shift boundary to
    # clear -- `press_h - tau` IS the constraint. Keeping a gate term here would
    # enforce a rule the simulator no longer has and silently cap the shift.
    sec = (pl.col("start_ts") - pl.col("ts")).dt.total_seconds() / 3600.0
    lot_lim = (pair.with_columns(sec.alias("_lag")).group_by("lot_id")
               .agg(pl.col("_lag").min().alias("press_h"),
                    pl.col("start_ts").min().alias("due"))
               .sort("lot_id"))
    lim = {r["lot_id"]: float(r["press_h"]) for r in lot_lim.iter_rows(named=True)}
    due = {r["lot_id"]: r["due"] for r in lot_lim.iter_rows(named=True)}

    # ---- demand variability, for the stopping margin ---------------------
    daily = (build_df.with_columns(pl.col("start_ts").dt.date().alias("_d"))
             .group_by(["plant", "gt_code", "_d"]).agg(pl.col("qty").sum().alias("q"))
             .group_by(["plant", "gt_code"])
             .agg(pl.col("q").mean().alias("mu"), pl.col("q").std().alias("sd"))
             .sort(["plant", "gt_code"]))
    cv = {(r["plant"], r["gt_code"]):
          (float(r["sd"] or 0.0) / float(r["mu"]) if r["mu"] else 0.0)
          for r in daily.iter_rows(named=True)}

    # ABSOLUTE latest-end per lot, captured BEFORE anything moves. `press_h` is
    # a budget RELATIVE to the lot's current position, so the moment EDD
    # re-places the chain it is stale -- using it after a re-sequence let lots
    # move to positions their own presses could not feed, and inventory rose
    # 7,327 -> 8,050. An absolute deadline survives re-sequencing by definition.
    # Original start per lot, so the recorded displacement covers EDD too. The
    # pipeline shifts the LEDGER by `rs_shift_h`; if that records only the
    # backward-pass delta while EDD has also moved the lot, schedule and ledger
    # desync and the verifier reports phantom machine overlap (52 of them).
    orig_start: dict[str, datetime] = dict(
        zip(build_df["lot_id"].to_list(), build_df["start_ts"].to_list()))
    latest_end: dict[str, datetime] = {}
    for r in build_df.select(["lot_id", "end_ts"]).iter_rows(named=True):
        lm = lim.get(r["lot_id"])
        if lm is not None:
            latest_end[r["lot_id"]] = r["end_ts"] + timedelta(hours=lm - TAU_MIN_H)

    # ---- backward pass, per machine -------------------------------------
    rows = build_df.sort(["machine", "start_ts", "lot_id"]).to_dicts()
    by_machine: dict[str, list[dict]] = {}
    for r in rows:
        by_machine.setdefault(r["machine"], []).append(r)

    tags: dict[str, int] = {}
    moved: list[float] = []
    clamped: list[dict] = []
    over: list[dict] = []
    out: list[dict] = []

    # ---- EDD RE-SEQUENCE, before the backward pass -----------------------
    # W decomposes exactly as head + drain - bspread. Measured against the
    # plant, drain and bspread are already at parity (spread gap 6.17 vs 7.48
    # PCR, 6.67 vs 6.73 TBR) -- ALL the excess is HEAD: 8.10h against the
    # plant's 2.02h. Head is precisely what the press limit targets, since lag
    # grows across a lot's tyres, so ALAP should drive it to tau.
    #
    # It stalls at 8.10h because 40.25% of adjacent pairs are INVERTED: a lot
    # whose deadline is early sitting BEHIND one whose deadline is late pins the
    # entire prefix, because the backward pass bounds lot i by lot i+1's start.
    # ALAP is optimal for a given sequence; on a wrong sequence it leaves
    # q_i(d_i - d_j + p_j) on the table for every inverted pair.
    #
    # Sorting by deadline is Jackson's rule and is optimal for maximum lateness,
    # so re-placing the chain in EDD order and re-running ALAP is monotone.
    # Setups change, because setup is a property of the GT transition -- so this
    # does NOT preserve changeover count and its acceptance test is fulfilment
    # and volume, not bit-identical changeovers.
    # EDD RE-SEQUENCE: REMOVED, measured worse three ways. Deadlines come from
    # the FIFO pairing, which is by build time -- so reordering lots changes the
    # pairing, which changes the deadlines the reorder was computed from. It is
    # circular, and every variant regressed: reorder + relative shift 8,050;
    # reorder + absolute ALAP 10,028 with 9 hard violations. The 40% inversion
    # rate is real but is a SYMPTOM of pairing order, not an independent defect.

    for m in ordered(by_machine):
        chain = sorted(by_machine[m], key=lambda r: (r["start_ts"], r["lot_id"]))
        succ_start: datetime | None = None
        succ_setup = 0.0
        for r in reversed(chain):
            le = latest_end.get(r["lot_id"])
            cand: list[tuple[float, str]] = []
            if le is not None:
                mg = MARGIN_Z * cv.get((r["plant"], r["gt_code"]), 0.0) * (TAU_MIN_H ** 0.5)
                cand.append(((le - r["end_ts"]).total_seconds() / 3600.0 - mg,
                             "press"))
            if succ_start is not None:
                cand.append(((succ_start - timedelta(seconds=succ_setup)
                              - r["end_ts"]).total_seconds() / 3600.0, "succ"))
            cand.append(((horizon_end - r["end_ts"]).total_seconds() / 3600.0,
                         "horizon"))
            delta, why = min(cand, key=lambda t: (t[0], t[1]))
            delta = int(delta * 3600.0) / 3600.0
            tags[why] = tags.get(why, 0) + 1
            if delta > 0:
                r = {**r, "start_ts": r["start_ts"] + timedelta(hours=delta),
                     "end_ts": r["end_ts"] + timedelta(hours=delta),
                     "rs_shift_h": delta, "rs_binding": why}
                moved.append(delta)
            else:
                r = {**r, "rs_shift_h": 0.0, "rs_binding": why}
            if r["end_ts"] > horizon_end:
                over.append({"lot": r["lot_id"], "plant": r["plant"],
                             "gt": r["gt_code"], "machine": r["machine"],
                             "qty": float(r["qty"]),
                             "over_h": round((r["end_ts"] - horizon_end)
                                             .total_seconds() / 3600.0, 2)})
            out.append(r)
            succ_start = r["start_ts"]
            succ_setup = float(r.get("setup_s") or 0.0)

    shifted = pl.DataFrame(out).sort(["plant", "machine", "start_ts", "lot_id"])

    # ---- REPAIR: the budget was computed against the OLD pairing ---------
    # Shifting right can change FIFO rank within a GT -- a lot that moves past
    # its neighbour is paired with a different cure than the budget assumed, so
    # a few lots land marginally short (measured: 4 lots, 0.06h). Re-derive the
    # pairing on the shifted timeline and SHRINK the offenders by their deficit.
    # Shrinking only, so this is monotone decreasing and terminates; it can
    # never introduce a new breach in a lot that was already clean.
    shrink: dict[str, float] = {}
    for _ in range(REPAIR_ITERS):
        cur = shifted.with_columns(
            pl.col("lot_id").replace_strict(shrink, default=0.0)
            .cast(pl.Float64).alias("_sh"))
        adj = dict(zip(cur["lot_id"].to_list(),
                       (cur["rs_shift_h"] - cur["_sh"]).to_list()))
        b2 = (b.with_columns(pl.col("lot_id").replace_strict(adj, default=0.0)
                             .cast(pl.Float64).alias("_a"))
              .with_columns((pl.col("ts") + pl.duration(seconds=pl.col("_a") * 3600))
                            .alias("ts"))
              .sort(["plant", "gt_code", "ts", "lot_id"])
              .with_columns(pl.int_range(pl.len()).over(["plant", "gt_code"]).alias("k")))
        bad = (ck.rename({"start_ts": "c_ts"})
               .join(b2, on=["plant", "gt_code", "k"], how="inner")
               .with_columns(((pl.col("c_ts") - pl.col("ts")).dt.total_seconds() / 3600
                              ).alias("lag"))
               .group_by("lot_id").agg(pl.col("lag").min().alias("m"))
               .filter(pl.col("m") < TAU_MIN_H - 1e-9).sort("lot_id"))
        if bad.height == 0:
            break
        # CAP THE SHRINK AT THE LOT'S OWN SHIFT. Uncapped, a lot with zero
        # shift gets pushed LEFT of where it started and collides with its
        # predecessor (measured: 5 overlaps, sequence broken). shift >= 0 is
        # exactly the condition under which the original feasible calendar
        # still holds. A lot still short at shift == 0 was ALREADY short in the
        # input plan -- pre-existing, not introduced here, and it is the
        # verifier's job to say so rather than this pass's to hide it.
        cap = {k: max(0.0, v) for k, v in zip(shifted["lot_id"].to_list(),
                                              shifted["rs_shift_h"].to_list())}
        progressed = False
        for r in bad.iter_rows(named=True):
            want = shrink.get(r["lot_id"], 0.0) + (TAU_MIN_H - r["m"])
            new = min(want, float(cap.get(r["lot_id"], 0.0)))
            if new > shrink.get(r["lot_id"], 0.0) + 1e-9:
                shrink[r["lot_id"]] = new
                progressed = True
        if not progressed:
            break
    if shrink:
        stats_repair = {"lots": len(shrink), "total_h": round(sum(shrink.values()), 2)}
        shifted = (shifted
                   .with_columns(pl.col("lot_id").replace_strict(shrink, default=0.0)
                                 .cast(pl.Float64).alias("_sh"))
                   .with_columns(
                       (pl.col("start_ts") - pl.duration(seconds=pl.col("_sh") * 3600)),
                       (pl.col("end_ts") - pl.duration(seconds=pl.col("_sh") * 3600)),
                       (pl.col("rs_shift_h") - pl.col("_sh")).alias("rs_shift_h"))
                   .drop("_sh"))
        moved = [v for v in shifted["rs_shift_h"].to_list() if v > 0]
    else:
        stats_repair = {}

    stats = {
        "repair": stats_repair,
        "lots": len(rows), "moved": len(moved),
        "mean_shift_h": round(sum(moved) / len(moved), 2) if moved else 0.0,
        "binding": dict(sorted(tags.items())),
        "clamped_past_horizon": len(clamped),
        "ends_past_horizon": len(over),
        "ends_past_horizon_detail": over[:5],
    }
    if clamped:
        stats["clamped_sample"] = clamped[:5]
    log.info("engine.right_shift", **{k: v for k, v in stats.items()
                                      if k != "clamped_sample"})
    return shifted, stats
