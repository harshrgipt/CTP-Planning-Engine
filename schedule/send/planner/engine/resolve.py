"""PHASE 1 -- master resolution, and PHASE 2 -- the feasibility gate.

Everything here is DERIVED from measured data. A constant read off one month's
plan cannot be detected as wrong from that month's own KPIs, which is why two
earlier attempts (rho = 0.863/0.945, f_book = 1.095) both looked fine on July
and were wrong on every other month.

The one non-negotiable coupling: `rate` must come from the SAME capacity model
the shift grid executes with, or the planner and its executor disagree about
what a press-day is worth.

    eff_CT      = (raw_dwell + 2.3) / 0.94
    tyres/shift = floor(480 / eff_CT) x slots
    rate        = 3 x tyres/shift

Validated: reproduces 156.0 (PCR) / 48.0 (TBR) exactly, bit-identical across
Jan, Mar, May and Jul.
"""
from __future__ import annotations

import json
import os

from dataclasses import dataclass, field
from datetime import date

import polars as pl

from planner.config import CONFIG
from planner.data.warehouse import duck
from planner.engine.contract import PlanRequest, ordered
from planner.runs.logger import log

DEFAULT_RATE = {"PCR": 156.0, "TBR": 48.0}
# Strip packing degrades sharply as fill approaches 1: at 93% it needs a handful
# of fragment campaigns, at 99% it is effectively impossible (a stagger peak of
# 131 against 92 presses). Leave the packer room.
BOX_FILL_MAX = float(os.environ.get("PLANNER_BOX_FILL_MAX", "0.97"))
SETUP_DAYS = 1.0 / 3.0        # one 480-min mould change = one shift


@dataclass
class Masters:
    presses: dict[str, list[str]] = field(default_factory=dict)
    machines: dict[str, list[str]] = field(default_factory=dict)
    rate: dict[str, float] = field(default_factory=dict)
    press_of: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    scrap: dict[str, float] = field(default_factory=dict)
    zero_area: dict[str, float] = field(default_factory=dict)
    cadence_s: dict[tuple[str, str], float] = field(default_factory=dict)
    cert_machines: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    gt_rate: dict[tuple[str, str], float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "presses": {k: len(v) for k, v in self.presses.items()},
            "machines": {k: len(v) for k, v in self.machines.items()},
            "rate": self.rate,
            "scrap_pct": {k: round(100 * v, 3) for k, v in self.scrap.items()},
            "zero_hold_area_pd": {k: round(v, 2) for k, v in self.zero_area.items()},
            "eligible_pairs": len(self.press_of),
            "certified_machine_pairs": len(self.cert_machines),
            "per_gt_rates": len(self.gt_rate),
        }


PRESS_LOOKBACK_DAYS = 28


def _real_presses(asof=None) -> dict[str, list[str]]:
    """Presses AVAILABLE for the plan month -- active in the recent past.

    `DISTINCT wcID` over all history counts a press that has been dead for five
    months as available. Measured on July 2026: it gave PCR 95 presses, of which
    the plant ran 86, and the surplus included presses 17/18/19 last used in
    mid-JANUARY. Those phantom presses get mounted, cannot be fed, and show up
    as `ineligible` press-shifts (5.3% of the grid) -- capacity invented by a
    query, then measured as inefficiency.

    A 28-day lookback is the same convention as PROXY_PREV28 demand and uses no
    information from the plan month. It reproduces TBR exactly (80) and takes
    PCR 95 -> 89. The residual three (21, 22, 109) ran until 25-30 June and were
    stopped by a plant decision in July -- not inferable from history, which is
    what `press_override` is for.
    """
    ov = os.environ.get("PLANNER_PRESS_LIST")
    if ov:
        try:
            spec = json.loads(ov)
            return {k: ordered(set(map(str, v))) for k, v in spec.items()}
        except Exception as e:                                  # noqa: BLE001
            log.warning("resolve.press_override_bad", err=str(e))
    lb = int(os.environ.get("PLANNER_PRESS_LOOKBACK_DAYS",
                            PRESS_LOOKBACK_DAYS))
    out: dict[str, set[str]] = {}
    rows = duck().execute(
        "SELECT DISTINCT plant, wcID::VARCHAR FROM v_curing "
        "WHERE ? IS NULL OR event_ts >= ?::DATE - INTERVAL 1 DAY * ?",
        [asof, asof, lb]).fetchall() if asof else duck().execute(
        "SELECT DISTINCT plant, wcID::VARCHAR FROM v_curing").fetchall()
    for plant, press in rows:
        out.setdefault(plant, set()).add(press)
    log.info("resolve.presses", lookback_days=lb if asof else None,
             counts={k: len(v) for k, v in out.items()})
    return {k: ordered(v) for k, v in out.items()}


def _real_machines() -> dict[str, list[str]]:
    out: dict[str, set[str]] = {}
    for plant, m in duck().execute(
            "SELECT DISTINCT plant, machineCode FROM v_build WHERE stage = 2"
    ).fetchall():
        if m:
            out.setdefault(plant, set()).add(m)
    return {k: ordered(v) for k, v in out.items()}


def _rate(plant: str, presses: list[str], timing) -> float:
    """Tyres per press-DAY, from the plant's own ACTIVE-DAY median output.

    The shift model -- eff_CT = (raw_dwell+2.3)/0.94, rate = 3*floor(480/eff_CT)
    * slots -- is right in structure but needs a slot count we do not have a
    master for, and the answer is very sensitive to it. Measured across all 8
    months:

        PCR  dwell 29.4-31.5 min -> 13 cyc/shift -> 117 (3 slots) / 156 (4)
             ACTIVE-DAY actual p50 144-158, p95 192-202     => 4 slots, 156 OK
        TBR  dwell 76.7-85.8 min ->  5 cyc/shift ->  45 (3 slots) /  60 (4)
             ACTIVE-DAY actual p50  38-44,  p95  48-52      => neither fits

    So 156 happens to sit at PCR's active-day p50, while the 48 we were using
    for TBR sits near its p95 -- a BEST-DAY figure. Planning TBR at its best day
    over-states press capacity ~14% and under-books its press-days, which is
    consistent with TBR being the class that under-delivers.

    Take the active-day p50 for BOTH plants: one basis, measured, and it
    reproduces PCR's 156 anyway. A press idle half the month drags the monthly
    mean and says nothing about its rate, so ACTIVE days only.
    """
    try:
        r = duck().execute("""
            WITH pd AS (
                SELECT wcID::VARCHAR p, CAST(event_ts AS DATE) d, count(*) n
                FROM v_curing WHERE plant = ? AND statuscritical = 'Normal'
                GROUP BY 1,2)
            SELECT quantile_cont(n, 0.5) FROM pd
        """, [plant]).fetchone()
        if r and r[0] and float(r[0]) > 0:
            return float(round(float(r[0])))
    except Exception as e:  # noqa: BLE001
        log.warning("engine.rate_measure_failed", plant=plant, err=str(e))
    caps = ordered(max(1, int(28800.0 // max(timing.cure_cadence_s(plant, p), 1.0)))
                   for p in presses)
    return 3.0 * float(caps[len(caps) // 2]) if caps else DEFAULT_RATE.get(plant, 100.0)


MIN_CLEAN_DAYS = 10          # evidence bar for trusting a per-GT rate
RATE_CLAMP = (0.25, 2.0)     # sanity bound on ratio to the plant median


def _gt_rate(rate: dict[str, float]) -> dict[tuple[str, str], float]:
    """Tyres per press-DAY, PER GT. Cure speed is a property of the GT.

    We booked every PCR GT at the plant median (151/press-day). Measured, they
    run 41 to 200 -- wrong by >20% on a THIRD of PCR GTs, mean booking error
    14.8%. Booking a slow GT at 151 sizes its campaign ~3x short so it cannot
    finish (unfilled tyres), while a fast GT is under-booked and its press idles.
    That is the starved-4.4% / idle-1.3% coexistence.

    Verified three ways before use:
      stable across months   CV p50 0.086 (PCR) / 0.084 (TBR)
      not a press artefact   same GT across >=3 presses, CV p50 0.089 --
                             GT 1844 XPC TML holds 115/day over 15 presses at CV 0.06
      physically consistent  r(dwell, rate) = -0.772 (PCR) / -0.600 (TBR):
                             longer cure cycle => fewer tyres per day

    Measured on SINGLE-GT press-days only, so a press sharing its day between
    GTs cannot dilute the figure. GTs without enough clean evidence keep the
    plant median -- this is a per-GT rate WHERE THE DATA SUPPORTS ONE, not an
    invitation to fit noise.

    CAPACITY, NOT ACHIEVED THROUGHPUT. Deriving the rate from what a GT actually
    got per press-day was tried and is WRONG: achieved/capacity is 0.95 (PCR) /
    0.93 (TBR), so booking on it charges the plant's own idle a SECOND time --
    11% (PCR) / 7% (TBR) more press-days than the work needs. That inflated total
    area, tightened the packing and starved GTs: fulfilment 98.89% -> 96.08%.
    It is also circular -- a GT that was starved shows a low rate, so we book it
    more press-time, which is the symptom feeding the cause.

    Derive from the CURE CYCLE instead, which is a physical property:

        eff_CT_g    = (dwell_g + 2.3) / 0.94
        tyres/shift = floor(480 / eff_CT_g) x slots
        rate_g      = 3 x tyres/shift
    """
    # WITHDRAWN -- measured worse in BOTH forms, and they fail in opposite
    # directions, which is the whole lesson:
    #     flat active-day median (151/42)   98.89%   <- best
    #     per-GT ACHIEVED      (p50 149)    96.08%   books ~11% too much
    #                                                press-time, packing tightens
    #                                                until GTs cannot be placed
    #     per-GT CAPACITY      (p50 156)    94.66%   books too little, GTs
    #                                                cannot finish
    # The per-GT signal is REAL -- stable across months (CV 0.086), holds across
    # 15 presses (CV 0.06), r(dwell, rate) = -0.77 -- but a more accurate
    # per-unit rate does not help, because the binding constraint is the
    # build/cure COUPLING, not the rate. Fourth time this class of "more correct
    # constant" has failed (rho, f_book, booking margin, now per-GT rate).
    # Re-test only after shift-level release, and measure, do not assume.
    return {}
    out: dict[tuple[str, str], float] = {}
    slots = {"PCR": 4, "TBR": 3}
    try:
        rows = duck().execute("""
            WITH dw AS (
                SELECT b.plant, b.itemCode AS gt, count(*) AS n,
                       quantile_cont(
                           date_diff('second', c.event_ts, c.cycleStart)/60.0,
                           0.5) AS dwell
                FROM v_curing c JOIN v_build b ON b.productionID = c.gtbarCode
                WHERE b.stage = 2 AND c.statuscritical = 'Normal'
                  AND c.cycleStart > c.event_ts AND b.itemCode IS NOT NULL
                GROUP BY 1,2)
            SELECT plant, gt, n, dwell FROM dw WHERE n >= ? AND dwell > 0
        """, [MIN_CLEAN_DAYS * 20]).fetchall()
        for plant, gt, _n, dwell in rows:
            eff = (float(dwell) + 2.3) / 0.94
            if eff <= 0:
                continue
            v = 3.0 * float(int(480.0 / eff)) * slots.get(plant, 4)
            med = rate.get(plant, 100.0)
            if v > 0 and med > 0 and RATE_CLAMP[0] <= v / med <= RATE_CLAMP[1]:
                out[(plant, gt)] = v
    except Exception as e:  # noqa: BLE001
        log.warning("engine.gt_rate_failed", err=str(e))
    return out


def _scrap() -> dict[str, float]:
    """Green tyres built and never cured. build/cure - 1 is NOT drift when
    inventory is trend-flat: the excess has to leave the system, and this is
    where it goes. Measured PCR ~0.47% (stationary), TBR ~2.0% (TRENDING:
    1.09% Jan -> 2.87% May), so it must be re-derived each run."""
    out: dict[str, float] = {}
    try:
        for plant, f in duck().execute("""
            WITH b AS (SELECT plant, productionID pid, event_ts FROM v_build
                       WHERE stage=2 AND QualityStatus='1' AND productionID IS NOT NULL),
                 mx AS (SELECT max(event_ts) - INTERVAL 7 DAY cut FROM b),
                 c AS (SELECT DISTINCT gtbarCode pid FROM v_curing
                       WHERE statuscritical='Normal')
            SELECT b.plant,
                   count(*) FILTER (WHERE c.pid IS NULL)::DOUBLE / count(*)
            FROM b CROSS JOIN mx LEFT JOIN c ON b.pid=c.pid
            WHERE b.event_ts < mx.cut GROUP BY 1
        """).fetchall():
            if f is not None and 0.0 <= float(f) < 0.2:
                out[plant] = float(f)
    except Exception as e:  # noqa: BLE001
        log.warning("engine.scrap_failed", err=str(e))
    return out


def _zero_hold_area(rate: dict[str, float]) -> dict[str, float]:
    """Area (press-days) below which the plant carries NO stock for a GT.

    19% of PCR GT-months and 10% of TBR hold zero; those GTs cure ~480 (PCR) /
    221 (TBR) a month -- about 3 press-days, i.e. one short campaign, cured as
    built. NB the literal rule "I* = 0 if N_g <= Q_g" is degenerate: with
    T_0 = 24h it reduces to active_days <= 1, which is ~0% of GTs.
    """
    out: dict[str, float] = {}
    try:
        rows = duck().execute("""
            WITH p AS (SELECT b.plant, b.itemCode gt,
                              date_trunc('month', c.event_ts) mo
                       FROM v_build b JOIN v_curing c ON b.productionID=c.gtbarCode
                       WHERE b.stage=2 AND c.statuscritical='Normal'
                         AND b.itemCode IS NOT NULL)
            SELECT plant, mo, gt, count(*) n FROM p GROUP BY 1,2,3
        """).fetchall()
        by: dict[str, list[float]] = {}
        for plant, _mo, _gt, n in rows:
            by.setdefault(plant, []).append(float(n))
        for plant, v in by.items():
            v.sort()
            out[plant] = v[int(0.19 * len(v))] / rate.get(plant, 100.0)
    except Exception as e:  # noqa: BLE001
        log.warning("engine.zero_area_failed", err=str(e))
    return out


def _gt_rim(timing) -> dict[str, int | None]:
    """GT -> rim diameter in inches, parsed from its size string.

    Rim is the number after R/ R- in a tyre size (215/60R17 -> 17), or the last
    number in a TBR size-led code (10.00 R 20 JDC3 -> 20).
    """
    import re
    out: dict[str, int | None] = {}
    try:
        gts = [r[0] for r in duck().execute(
            "SELECT DISTINCT itemCode FROM v_build WHERE stage=2 "
            "AND itemCode IS NOT NULL").fetchall()]
    except Exception:  # noqa: BLE001
        return out
    for g in ordered(gts):
        s = timing._size_for_gt(g) or g
        m = re.search(r"R\s*[-]?\s*(\d{2}(?:\.\d)?)", str(s), re.I)
        if not m:
            m = re.search(r"\b(\d{2})\b(?!.*\b\d{2}\b)", str(s))
        try:
            out[g] = int(float(m.group(1))) if m else None
        except Exception:  # noqa: BLE001
            out[g] = None
    return out


def resolve_masters(req: PlanRequest, timing) -> Masters:
    """PHASE 1."""
    presses = _real_presses(asof=req.plan_start)
    machines = _real_machines()
    rate = {p: _rate(p, presses.get(p, []), timing) for p in ordered(presses)}

    # ELIGIBILITY IS CAPABILITY, NOT HISTORY. 40-47% of press-GT pairs are new
    # every month, carrying 30-37% of volume. History RANKS candidates; it must
    # never GATE them -- gating left 542 press-days unserved while 25.6% of
    # press-shifts held no mould at all.
    press_of: dict[tuple[str, str], list[str]] = {}
    # PLATEN GATE REMOVED -- the master contradicts production.
    # It classified PCR presses by platen size (36" -> rim 12-14, 48" -> rim
    # 14-20) and gated eligibility on that. Checked against July 2026: the plant
    # cured GT 1402 XPC TATA (rim 12) on presses 23, 24, 34, 41, 44, 45, and the
    # master recognises only 23 and 24 as rim-12 capable. Presses 34/41/44/45
    # ran rim-12 all month while the master says they cannot.
    #
    # The damage was hidden by over-mounting: with 95 presses the surplus
    # covered the gap, and only when the press list was cut to the plant's real
    # 86 did it surface -- GT 1402 and GT 1503 dropped to 2 eligible presses
    # each and PCR was declared INFEASIBLE for a month the plant actually ran.
    # It also mis-attributed an earlier experiment: capping to 86 presses cost
    # 2 pp of fulfilment and that was read as physical inefficiency; it was this.
    #
    # Eligibility now comes from the allowed-press matrix below, which is built
    # from observed production. Restore a platen gate only with a master that
    # has been reconciled against what the presses actually ran.
    n_platen = 0
    # History still RANKS: put the presses a GT has actually run on first, so
    # preference is honoured inside the physically-feasible set.
    ap = CONFIG.paths.warehouse / "derived" / "allowed_press_matrix.parquet"
    if ap.exists():
        m = pl.read_parquet(ap).sort(["plant", "gt_code", "basis", "press"])
        seen: dict[tuple[str, str], list[str]] = {}
        for r in m.iter_rows(named=True):
            if r["press"] in set(presses.get(r["plant"], [])):
                seen.setdefault((r["plant"], r["gt_code"]), []).append(r["press"])
        for k, hist in seen.items():
            phys = press_of.get(k)
            if phys:
                pref = [p for p in hist if p in set(phys)]
                press_of[k] = pref + [p for p in phys if p not in set(pref)]
            else:
                press_of[k] = hist
    log.info("engine.eligibility", platen_gated_gts=n_platen,
             total_pairs=len(press_of))

    # MACHINE CERTIFICATION -- what a machine MAY run, from the plant's own
    # TBR BUILDING ALLOWABLE MATRIX. This is a different object from what it HAS
    # run: certification is 1.5x wider (p50 3 machines per GT vs 2 mined), and
    # the narrow mined set is exactly what pinned 47 lots onto two machines and
    # pushed the build span 9 days past month end.
    # Union with observed usage: 15 of 171 used pairs are NOT in the matrix, so
    # either it is stale or the floor deviates. Dropping those would refuse
    # routings the plant demonstrably uses.
    cert: dict[tuple[str, str], list[str]] = {}
    cp = CONFIG.paths.warehouse / "derived" / "tbr_machine_certified.parquet"
    if cp.exists():
        try:
            cm = pl.read_parquet(cp)
            if "mes_gt" in cm.columns:
                for r in cm.sort(["mes_gt", "machine"]).iter_rows(named=True):
                    if r.get("mes_gt") and r["machine"] in set(
                            machines.get(r["plant"], [])):
                        cert.setdefault((r["plant"], r["mes_gt"]), []).append(
                            r["machine"])
            for k in cert:
                cert[k] = ordered(set(cert[k]))
            log.info("engine.certification", pairs=len(cert),
                     machines_per_gt_p50=(
                         sorted(len(v) for v in cert.values())[len(cert) // 2]
                         if cert else 0))
        except Exception as e:  # noqa: BLE001
            log.warning("engine.cert_failed", err=str(e))

    ms = Masters(presses=presses, machines=machines, rate=rate,
                 press_of=press_of, scrap=_scrap(),
                 zero_area=_zero_hold_area(rate), cert_machines=cert,
                 gt_rate=_gt_rate(rate))
    log.info("engine.masters", **ms.to_dict())
    return ms


# ---------------------------------------------------------------------------
# PHASE 2 -- feasibility gate. Run BEFORE any planning.
# ---------------------------------------------------------------------------

def feasibility(req: PlanRequest, ms: Masters) -> dict:
    """C1-C4. C1 failing means no scheduler can help -- stop and report."""
    H = req.horizon_days
    rep: dict = {"go": True, "plants": {}}
    tot = (req.demand.group_by(["plant", "gt_code"])
           .agg(pl.col("qty").sum().alias("N"))
           .sort(["plant", "gt_code"]))
    for plant in ordered(tot["plant"].unique().to_list()):
        sub = tot.filter(pl.col("plant") == plant)
        P = ms.presses.get(plant, [])
        rate = ms.rate.get(plant, DEFAULT_RATE.get(plant, 100.0))
        if not P or rate <= 0:
            rep["go"] = False
            rep["plants"][plant] = {"C0": "no presses or rate for plant"}
            continue
        area = {r["gt_code"]: float(r["N"]) / rate for r in sub.iter_rows(named=True)}
        cap = {g: max(1, len(ms.press_of.get((plant, g), P))) for g in area}

        c1 = [{"gt": g, "area_pd": round(a, 1), "limit": cap[g] * H}
              for g, a in ordered(area.items()) if a > cap[g] * H]
        tot_area = sum(area.values())
        c2_lim = len(P) * H * BOX_FILL_MAX
        c3 = sum(max(1, -(-a // H)) for a in area.values())
        machine_h = len(ms.machines.get(plant, [])) * H * 24.0

        ok = (not c1) and tot_area <= c2_lim
        rep["plants"][plant] = {
            "presses": len(P), "machines": len(ms.machines.get(plant, [])),
            "rate_per_press_day": rate, "gts": len(area),
            "C1_infeasible_gts": c1,
            "C2_area_press_days": round(tot_area), "C2_limit": round(c2_lim),
            "C2_fill_pct": round(100 * tot_area / (len(P) * H), 1),
            "C3_changeover_floor": int(c3 - len(P)),
            "C4_machine_hours_available": round(machine_h),
            "verdict": "GO" if ok else "NO-GO",
        }
        if not ok:
            rep["go"] = False
    log.info("engine.feasibility", go=rep["go"],
             **{f"{k}_{kk}": vv for k, v in rep["plants"].items()
                for kk, vv in v.items() if not isinstance(vv, list)})
    return rep
