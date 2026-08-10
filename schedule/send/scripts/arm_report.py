"""The pre-declared metric set for an A/B arm, PER PLANT. Read-only.

    python scripts/arm_report.py base fix1 fix2 ...

Every metric is reported for every arm, including the ones expected to get
WORSE. DO-NOT #14: never judge a change on plant-TOTAL fulfilment -- a total
that moved 1.85 pt once hid an 8.67 pt TBR regression. DO-NOT #9: inventory is
time-weighted, never event-weighted.

Refuses to report any arm whose scorecard does not describe the plan beside it.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from planner.cmbc.l11_validate_plan import arm_is_stale  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
SZ = pl.read_parquet(ROOT.parent.parent / "INPUT" / "derived" / "gt_size.parquet")
FLOOR = {"PCR": 150.0, "TBR": 70.0}
RAIL = {"PCR": 4800, "TBR": 1400}
BAND_LO = {"PCR": 4500, "TBR": 1200}


def _co_min(mach: str) -> tuple[float, float]:
    """Plant master rates. PCR 1-5 (BJ) 28/60, PCR 6-11 (CONTI) 22/42, TBR 10/24."""
    m = re.search(r"PCR(\d+)", mach)
    if m:
        return (28.0, 60.0) if int(m.group(1)) <= 5 else (22.0, 42.0)
    return (10.0, 24.0)


def _rim(plant: str) -> dict:
    s = SZ.filter(pl.col("plant") == plant)
    return {r["gt_code"]: str(r["rim"]) for r in s.iter_rows(named=True)
            if r.get("gt_code") and r.get("rim")}


def _tw(ivt, n_h: int = 744):
    ts0 = ivt["ts"].min()
    ts = np.array([(x - ts0).total_seconds() / 3600.0 for x in ivt["ts"]], float)
    bal = np.array(ivt["bal"], float)
    idx = np.searchsorted(ts, np.arange(n_h) + 0.5, side="right") - 1
    g = np.where(idx >= 0, bal[np.clip(idx, 0, len(bal) - 1)], 0.0)
    return g.mean(), g[: (n_h // 24) * 24].reshape(n_h // 24, 24).mean(axis=1)


def measure(run: Path, month: str = "2026-07") -> dict:
    why = arm_is_stale(run)
    if why and not why.startswith("no l11_provenance"):
        raise SystemExit(f"!! {run.name} STALE: {why}")
    bs = pl.read_parquet(run / "build_schedule.parquet")
    st = pl.read_parquet(run / "build_starved.parquet")
    up = pl.read_parquet(run / "cure_unplaced.parquet")
    inv = pl.read_parquet(run / "l11_invariants.parquet")
    rec = pl.read_parquet(run / "cure_campaigns_reconciled.parquet")
    req = pl.read_parquet(ROOT / "warehouse" / "derived" /
                          f"net_requirement_{month}.parquet")
    out: dict = {"run": run.name,
                 "l11_pass": inv.filter(pl.col("status") == "PASS").height,
                 "l11_n": inv.height,
                 "l11": {r["invariant"]: (r["actual"], r["status"])
                         for r in inv.iter_rows(named=True)}}
    for p in ("PCR", "TBR"):
        rm = _rim(p)
        b = bs.filter((pl.col("plant") == p) & (pl.col("machine") != "OPENING_STOCK"))
        d: dict = {}
        need = float(req.filter((pl.col("plant") == p) & (~pl.col("residual")))
                     ["gross_build"].sum())
        got = float(rec.filter(pl.col("plant") == p)["qty_fed"].sum())
        d["need"], d["fed"] = need, got
        d["ful"] = 100.0 * got / need if need else 0.0
        sp = st.filter(pl.col("plant") == p)
        d["starved"] = float(sp["qty"].sum())
        d["starved_grp"] = sp.height
        d["by_rim"] = {}
        for r in sp.iter_rows(named=True):
            d["by_rim"][rm.get(r["gt_code"], "?")] = \
                d["by_rim"].get(rm.get(r["gt_code"], "?"), 0.0) + float(r["qty"])
        u = up.filter(pl.col("plant") == p)
        d["l5_unplaced"] = float(u["qty"].sum()) if u.height else 0.0
        # --- true setup blocks: split a run_id at any gap > 1 h -------------
        co = same = 0
        setup = 0.0
        qtys: list[float] = []
        for m in sorted(b["machine"].unique()):
            rows = b.filter(pl.col("machine") == m).sort("start_ts").to_dicts()
            if not rows:
                continue
            s_min, d_min = _co_min(m)
            cur = [rows[0]]
            for a2, b2 in zip(rows, rows[1:]):
                gap = (b2["start_ts"] - a2["end_ts"]).total_seconds() / 3600.0
                if b2["gt_code"] == a2["gt_code"] and gap <= 1.0:
                    cur.append(b2)
                    continue
                qtys.append(sum(x["qty"] for x in cur))
                co += 1
                if rm.get(a2["gt_code"]) == rm.get(b2["gt_code"]):
                    same += 1
                    setup += s_min
                else:
                    setup += d_min
                cur = [b2]
            qtys.append(sum(x["qty"] for x in cur))
        q = np.array(qtys) if qtys else np.array([0.0])
        nmach = b["machine"].n_unique() or 1
        d["setups"] = len(q)
        d["same_pct"] = 100.0 * same / max(co, 1)
        d["setup_h"] = setup / 60.0
        d["co_per_md"] = co / (nmach * 31.0)
        d["lot_p50"] = float(np.median(q))
        d["subfloor"] = 100.0 * float((q < FLOOR[p]).mean())
        # --- inventory, TIME-weighted (DO-NOT #9) ---------------------------
        bp = bs.filter(pl.col("plant") == p)
        if bp.height:
            ivt = pl.concat([
                bp.select([pl.col("end_ts").alias("ts"), pl.col("qty").alias("d")]),
                bp.select([pl.col("cure_ts").alias("ts"), (-pl.col("qty")).alias("d")]),
            ]).sort("ts").with_columns(pl.col("d").cum_sum().alias("bal"))
            mean, dm = _tw(ivt)
            d["inv_mean"], d["inv_daymax"], d["inv_lastday"] = mean, dm.max(), dm[-1]
        d["r5_max"] = float(b["wait_h"].max()) if b.height else 0.0
        # --- idle-gap structure (diagnostic) --------------------------------
        runs_ = b.group_by(["machine", "run_id"]).agg(
            pl.col("start_ts").min().alias("s"), pl.col("end_ts").max().alias("e"))
        gaps: list[float] = []
        t0 = bs["start_ts"].min()
        for m in sorted(runs_["machine"].unique()):
            iv = sorted(zip(runs_.filter(pl.col("machine") == m)["s"],
                            runs_.filter(pl.col("machine") == m)["e"]))
            prev = t0
            for s2, e2 in iv:
                g = (s2 - prev).total_seconds() / 3600.0
                if g > 1e-6:
                    gaps.append(g)
                prev = max(prev, e2)
        d["gap_n"] = len(gaps)
        d["gap_p50"] = float(np.median(gaps)) if gaps else 0.0
        d["idle_h"] = float(sum(gaps))
        out[p] = d
    return out


def main() -> int:
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    month = "2026-07"
    if "--month" in sys.argv:
        month = sys.argv[sys.argv.index("--month") + 1]
        argv = [a for a in argv if a != month]
    res = [measure(RUNS / n, month) for n in argv]
    for p in ("PCR", "TBR"):
        print("=" * (26 + 15 * len(res)))
        print(f"{p}")
        print("=" * (26 + 15 * len(res)))
        rows = [
            ("fulfilment %", "ful", "{:.2f}"), ("  fed", "fed", "{:,.0f}"),
            ("starved tyres", "starved", "{:,.0f}"),
            ("starved groups", "starved_grp", "{:,.0f}"),
            ("L5 unplaced", "l5_unplaced", "{:,.0f}"),
            ("lot p50", "lot_p50", "{:,.0f}"),
            ("true setup blocks", "setups", "{:,.0f}"),
            ("same-size %", "same_pct", "{:.1f}"),
            ("weighted setup h", "setup_h", "{:,.0f}"),
            ("CO / machine-day", "co_per_md", "{:.2f}"),
            ("sub-floor %", "subfloor", "{:.1f}"),
            ("GT inv tw-mean", "inv_mean", "{:,.0f}"),
            ("GT inv daily max", "inv_daymax", "{:,.0f}"),
            ("GT inv last day", "inv_lastday", "{:,.0f}"),
            ("R5 max h", "r5_max", "{:.1f}"),
            ("idle h", "idle_h", "{:,.0f}"),
            ("idle gaps n", "gap_n", "{:,.0f}"),
            ("idle gap p50 h", "gap_p50", "{:.2f}"),
        ]
        print(f"{'metric':<22}" + "".join(f"{r['run']:>15}" for r in res))
        for label, key, fmt in rows:
            cells = "".join(f"{fmt.format(r[p][key]):>15}" if key in r[p]
                            else f"{'-':>15}" for r in res)
            print(f"{label:<22}{cells}")
        print(f"{'  rail / band floor':<22}"
              + "".join(f"{str(RAIL[p]) + '/' + str(BAND_LO[p]):>15}" for _ in res))
    print()
    print(f"{'L11 PASS':<22}" + "".join(f"{str(r['l11_pass']) + '/' + str(r['l11_n']):>15}"
                                        for r in res))
    if len(res) > 1:
        base = res[0]
        for r in res[1:]:
            flips = [(k, base["l11"].get(k, ("-", "-"))[1], v[1], v[0])
                     for k, v in r["l11"].items()
                     if base["l11"].get(k, ("-", "-"))[1] != v[1]]
            print(f"\n  {r['run']} vs {base['run']}: {len(flips)} invariant flip(s)")
            for k, o, n, act in flips:
                arrow = "PASS->FAIL" if n == "FAIL" else "FAIL->PASS"
                print(f"     {arrow}  {k}  (now {act})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
