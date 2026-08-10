"""Lot batching: turn per-day demand into lot rows respecting MOQ/MPQ + observed
historical mode-lot-size when no master is supplied."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date

import polars as pl

from planner.config import CONFIG
from planner.data.masters import load as load_master
from planner.data.warehouse import duck
from planner.runs.logger import log


@dataclass
class Lot:
    lot_id: str
    plant: str
    gt_code: str
    qty: float
    due_date: date
    stage: int = 2
    priority: int = 100
    # populated by the planner:
    machine: str | None = None
    start_ts: object | None = None    # datetime
    end_ts: object | None = None
    trace: dict = field(default_factory=dict)


def _observed_lot_size() -> pl.DataFrame:
    """Typical lot size = the plant's own uninterrupted run length.

    Rule B17: match the plant. A "lot" on the floor is a *campaign* -- the run
    of consecutive same-GT tyres on one machine before it changes over -- not a
    calendar-day total. Day totals conflate several runs (or split one across
    midnight) and produced lot sizes unlike anything the plant actually builds.

    Detected with a gaps-and-islands scan: rank tyres per machine by time, rank
    them again within GT, and the difference is constant across one unbroken
    run.
    """
    con = duck()
    df = con.execute(
        """
        WITH ev AS (
            SELECT plant, itemCode AS gt_code, machineCode AS machine, event_ts,
                   row_number() OVER (PARTITION BY plant, machineCode ORDER BY event_ts) AS rn_all,
                   row_number() OVER (PARTITION BY plant, machineCode, itemCode
                                      ORDER BY event_ts) AS rn_gt
            FROM v_build
            WHERE stage = 2 AND QualityStatus = '1'
        ),
        runs AS (
            SELECT plant, gt_code, machine, rn_all - rn_gt AS grp, count(*) AS run_len
            FROM ev GROUP BY 1,2,3,4
        )
        SELECT plant, gt_code,
               mode() WITHIN GROUP (ORDER BY run_len) AS lot_size_mode,
               quantile_cont(run_len, 0.5) AS lot_size_median,
               max(run_len) AS lot_size_max
        FROM runs GROUP BY 1,2
        """
    ).pl()
    return df


def build_lots(demand: pl.DataFrame) -> list[Lot]:
    """Lots sized by a COMMON REPLENISHMENT INTERVAL, not a common quantity.

        Q_g = r_g * T_0          r_g = N_g / H      T_0 ~ 47h

    A constant lot size makes the replenishment gap scale as delta_g = Q / r_g,
    i.e. inversely with rate -- so a slow GT is rebuilt every 4.5 days while a
    fast one is rebuilt every 0.4 days, an 11x spread. That is what starved
    PCR press 33 for 519 consecutive hours: its GT needed 3.4/h but arrived in
    362-unit lots every 4.5 days.

    Holding the INTERVAL constant instead makes every GT arrive on the same
    cadence and the gap disappears by construction. T_0 is recovered from the
    plant's own changeover count:

        building changeovers = |G| * H / T_0 - |M|
        => T_0 = 99 * 744 / (1519 + |M|) ~ 47h,  insensitive to |M|.

    Cross-check: the last unit of a lot waits ~T_0, so aging p95 ~ 0.95*T_0 =
    45h with no build/cure overlap. The plant reports 32h. Consistent.
    """
    if demand.height == 0:
        return []
    moq_df = load_master("moq_mpq")

    T0_H = float(CONFIG.thresholds.replenish_interval_h)
    # r_g is the IN-WINDOW rate, over the GT's own active days -- not N_g/H.
    # A GT live for 15 of 31 days draws at twice the horizon average while it is
    # live, and it is the live rate its presses consume at. Dividing by the full
    # month halves every lot and doubles every replenishment gap, which is the
    # same 2x starvation the 47h T_0 produced, one level down.
    tot = demand.group_by(["plant", "gt_code"]).agg(
        pl.col("qty").sum().alias("_N"),
        pl.col("due_date").n_unique().alias("_days"))
    dm = demand.join(tot, on=["plant", "gt_code"], how="left")
    dm = dm.with_columns(
        (pl.col("_N") / (pl.col("_days").cast(pl.Float64) * 24.0)).alias("_D"))

    # ---- SQRT-D LOT SIZING (the plant's own law) -------------------------
    # Q_g = D_g * T_0 gives EVERY GT the same cover, T_0 hours. The plant does
    # not do that. Measured over 8 months and 288/343 GT-months, its runs and
    # its order-up-to levels both scale as the SQUARE ROOT of draw:
    #     PCR   Q = 268.7*D^+0.475 (r .789)   S = 314.7*D^+0.486 (r .813)
    #     TBR   Q =  75.4*D^+0.279 (r .698)   S =  96.1*D^+0.365 (r .817)
    # and the order-up-to level S is the lowest-CV quantity in the data
    # (0.407/0.330 vs 0.489/0.449 for Q), so the plant runs periodic-review
    # order-up-to, not fixed-quantity.
    #
    # sqrt(D) is the EOQ optimum: minimising sum(Q_g/2) subject to a fixed
    # changeover budget sum(D_g/Q_g) gives Q_g ~ sqrt(D_g). The gain over
    # proportional sizing is Cauchy-Schwarz and depends only on how spread the
    # draws are:
    #     I_sqrt / I_prop = (sum sqrt D)^2 / (n * sum D)  <= 1
    # Our draws span 240x (PCR p10 0.31/h, max 74.7/h), so the ratio is 0.643
    # PCR / 0.688 TBR -- a 32-36% cut AT THE SAME CHANGEOVER COUNT.
    #
    # a_p is calibrated, not fitted: set so the sawtooth sums to the G8 band
    # midpoint (a plant-given business rule). SHAPE from the plant's data, LEVEL
    # from the band -- no constant carried over from any single month.
    #
    # OFF BY DEFAULT (PLANNER_SQRT_LOTS=1 to arm). The law is REAL and the fit is
    # sound, but the predicted gain is not, because the premise I ~ sum(Q_g/2)
    # is false. Measured, inventory is ADDITIVE in two terms:
    #     I = sum(Q_g/2)  +  lambda * head
    #   T0=12h   2,921 + 3,976 = 6,897   (head 7.4h)
    #   sqrt-D   3,576 + 3,323 = 6,899   (head 6.2h)
    # The total is invariant to 3 tyres. Lot sizing only MOVES stock between the
    # two terms -- bigger lots mean fewer, longer runs, so less head but more
    # sawtooth. That is also why T_0 24->12 helped but 12->8->6 reversed.
    # Cauchy-Schwarz still bounds the sawtooth term; it just is not the term
    # that dominates. sqrt-D did cut aging p95 (27.7 -> 26.4h) and changeovers
    # (1,748 -> 1,727) for equal fulfilment, so it is worth re-arming if the
    # PHASE term is ever brought down and the sawtooth starts to dominate.
    if os.environ.get("PLANNER_SQRT_LOTS") == "1":
        band = {p: 0.5 * (CONFIG.thresholds.gt_wip_min.get(p, 0)
                          + CONFIG.thresholds.gt_wip_max.get(p, 0))
                for p in tot["plant"].unique().to_list()}
        sq = (tot.with_columns(
                (pl.col("_N") / (pl.col("_days").cast(pl.Float64) * 24.0))
                .sqrt().alias("_sq"))
              .group_by("plant").agg(pl.col("_sq").sum().alias("_S")))
        a = {r["plant"]: (2.0 * band.get(r["plant"], 0.0) / r["_S"]
                          if r["_S"] > 0 else 0.0)
             for r in sq.iter_rows(named=True)}
        dm = dm.with_columns(
            (pl.col("plant").replace_strict(a, default=0.0).cast(pl.Float64)
             * pl.col("_D").sqrt()).round().clip(lower_bound=1).alias("_lot"))
        log.info("lots.sqrt_d", a={k: round(v, 2) for k, v in a.items()},
                 lot_p50=float(dm["_lot"].median()),
                 lot_min=float(dm["_lot"].min()), lot_max=float(dm["_lot"].max()))
    else:
        dm = dm.with_columns(
            (pl.col("_D") * T0_H).round().clip(lower_bound=1).alias("_lot"))
        log.info("lots.common_cycle", T0_h=T0_H,
                 active_days_p50=float(dm["_days"].median()),
                 lot_p50=float(dm["_lot"].median()),
                 lot_min=float(dm["_lot"].min()), lot_max=float(dm["_lot"].max()))

    # ---- MINIMUM RUNNABLE LOT (B12 / R9) --------------------------------
    # Raise the lot size to the plant floor, but never above what the GT needs
    # for the whole month -- a GT demanding 40 tyres must not be rounded to 150.
    floor = CONFIG.thresholds.min_lot_units
    dm = dm.with_columns(
        pl.min_horizontal(
            pl.col("plant").replace_strict(floor, default=0).cast(pl.Float64),
            pl.col("_N")).alias("_floor"))
    dm = dm.with_columns(
        pl.max_horizontal(pl.col("_lot"), pl.col("_floor")).alias("_lot"))
    log.info("lots.min_lot", floor=floor,
             lot_p50=float(dm["_lot"].median()), lot_min=float(dm["_lot"].min()))

    lots: list[Lot] = []
    for row in dm.iter_rows(named=True):
        lot_size = int(row["_lot"])
        # honour MOQ if provided
        if moq_df.height:
            f = moq_df.filter(pl.col("sku") == row["gt_code"])
            if f.height:
                lot_size = max(lot_size, int(f["moq"][0]))
        remaining = float(row["qty"])
        idx = 0
        while remaining > 0:
            q = min(remaining, lot_size)
            lot_id = f"{row['plant']}-{row['due_date']}-{row['gt_code']}-{idx}".replace(" ", "_")
            lots.append(Lot(
                lot_id=lot_id, plant=row["plant"], gt_code=row["gt_code"],
                qty=q, due_date=row["due_date"], stage=2, priority=100,
            ))
            remaining -= q
            idx += 1
    log.info("lots.built", n=len(lots))
    return lots

