"""Re-derive the B16 TT/TL machine groups WITHOUT the raw MES.

    python scripts/rescore_ttl_groups.py 2026-07 2026-08          # report only
    python scripts/rescore_ttl_groups.py 2026-08 --write          # rewrite the parquet
    python scripts/rescore_ttl_groups.py 2026-08 --basis raw      # reproduce today's file

WHY THIS SCRIPT EXISTS -- a measured defect, found 2026-08-14.

`_offline/l2_capability.py:205` builds the eligibility set used to CHOOSE the
TT/TL split from the RAW `cap_machine` frame:

    for r in cm.filter(pl.col("plant") == "TBR").iter_rows(named=True):

but L7 plans against `allowable.restrict(cm)` (`l7_pull_release.py:774-777`),
which cuts August's TBR rows 142 -> 127 and the whole frame 813 -> 364. The
partition search therefore scores candidates against an eligibility set the
planner does not have. Under the raw frame TL={6,9}, TL={5,6} and TL={4,6} all
tie at key (0, 0.0, 0, 0.0) and the winner is decided by the final `combo`
tiebreak -- lowest machine number. August 2026 shipped TL={6,9} for that reason,
which left TBMTBR6 as the sole eligible machine for 994 build-hours in a 744-hour
month (133.7 % load) while TBMTBR9 sat at 30.8 %.

    August 2026, fresh arms:  TBR BUILT 91,509 -> 97,023   starved 6,816 -> 1,302
                              PCR bit-identical, sub-floor held at 0.00 %

THE POINT OF THIS SCRIPT, though, is provenance, not the tyres.

`l2_capability` needs the MES drop to re-mine `cap_machine`, so the temptation is
to hand-edit `cap_ttl_groups_<M>.parquet` and move on. That produces a master
with no reproducible derivation -- the exact failure mode PARTITION section 4o
records for the build partition, and `l1_preflight.py:225-228` will not catch it
(it emits an INFO row-count and no month stamp).

It is also unnecessary. The search reads four inputs and only `cm` is
MES-derived -- and `cm` has ALREADY been mined to
`warehouse/derived/cap_machine_<M>.parquet`, which is on disk and tracked. So the
choice is a pure function of committed artefacts and replays offline. This script
is that replay, byte-for-byte the same scoring block as
`l2_capability.py:188-304`.

TIES ARE THE OUTPUT, NOT A FOOTNOTE. Restricted scoring gives:

    2026-07  n_tt=6  -> TL=[4,5,6]            1-way, UNIQUE   == shipped file
    2026-08  n_tt=7  -> TL=[5,6] or TL=[4,6]  2-way TIE

so the fix reproduces July exactly (which is the regression guard) but on August
it still decides by machine number. Until `l2_capability` carries the B16 step-7
capacity term, this script prints the full tie set so nobody reads a coin-flip as
a derivation.
"""
from __future__ import annotations

import argparse
from itertools import combinations

import polars as pl

from planner import paths
from planner.cmbc import allowable

# Mirrors l2_capability.TBR_MAKE. Nine machines, SAV 1-3 + MESNAC 4-9.
TBR_MAKE = {1: "SAV", 2: "SAV", 3: "SAV", 4: "MESNAC", 5: "MESNAC",
            6: "MESNAC", 7: "MESNAC", 8: "MESNAC", 9: "MESNAC"}


def _restrict(cm: pl.DataFrame) -> pl.DataFrame:
    """The same chain L7 applies at l7_pull_release.py:774-777."""
    cm = allowable.restrict(cm)
    for fn in ("restrict_rimlock", "restrict_rimset"):
        f = getattr(allowable, fn, None)
        if f is None:
            continue
        try:
            cm = f(cm)
        except TypeError:                                    # takes a month
            cm = f(cm, None)
    return cm


def score_all(month: str, basis: str = "restricted", crit: str = "coverage"):
    """Return (n_tt, ranked rows). Each row is (key, tl_list, tt_combo)."""
    dem = pl.read_parquet(paths.demand(month))
    tt = pl.read_parquet(paths.input_derived("tt_tl.parquet")).filter(pl.col("sku") != "")
    tbr = (dem.filter(pl.col("plant") == "TBR")
           .join(tt.select(["sku", "tt_tl"]).unique(subset=["sku"]), on="sku", how="left"))

    q = tbr.group_by("tt_tl").agg(pl.col("qty").sum().alias("q"))
    tot = float(q["q"].sum())
    ttq = float(q.filter(pl.col("tt_tl") == "TT")["q"].sum() or 0)
    # NOTE: BUSINESS_RULES.md:139 specifies the split on HOURS; l2_capability
    # uses tyre-qty share. Reproduced verbatim here so this script agrees with
    # the engine. Aug 2026: 76.5 % vs 76.3 %, so it does not bite this month.
    n_tt = int(round(9 * ttq / tot)) if tot else 0

    cm = pl.read_parquet(paths.wh_derived(f"cap_machine_{month}.parquet"))
    n_raw = cm.filter(pl.col("plant") == "TBR").height
    if basis == "restricted":
        cm = _restrict(cm)
    n_use = cm.filter(pl.col("plant") == "TBR").height

    elig: dict[str, set[str]] = {}
    for r in cm.filter(pl.col("plant") == "TBR").iter_rows(named=True):
        elig.setdefault(r["gt_code"], set()).add(r["machine"])
    tag = {r["gt_code"]: r["tt_tl"] for r in tbr.iter_rows(named=True) if r["tt_tl"]}
    qty = {r["gt_code"]: float(r["q"]) for r in
           tbr.group_by("gt_code").agg(pl.col("qty").sum().alias("q")).iter_rows(named=True)}

    def uncovered(ts: set[int]) -> tuple[int, float]:
        ttm = {f"TBMTBR{n}Stage2" for n in ts}
        tlm = {f"TBMTBR{n}Stage2" for n in TBR_MAKE if n not in ts}
        n, v = 0, 0.0
        for g, t in tag.items():
            if not (elig.get(g, set()) & (ttm if t == "TT" else tlm)):
                n += 1
                v += qty.get(g, 0.0)
        return n, v

    def stranded(ts: set[int]) -> tuple[int, float]:
        go = {n: ("TT" if n in ts else "TL") for n in TBR_MAKE}
        reach = {n: sum(qty.get(g, 0.0) for g, t in tag.items()
                        if t == go[n] and f"TBMTBR{n}Stage2" in elig.get(g, set()))
                 for n in TBR_MAKE}
        dead = sum(1 for n in TBR_MAKE if reach[n] <= 0.0)
        deficit = 0.0
        for t in ("TT", "TL"):
            ms = [n for n in TBR_MAKE if go[n] == t]
            if not ms:
                continue
            share = sum(qty.get(g, 0.0) for g, x in tag.items() if x == t) / len(ms)
            deficit += sum(max(0.0, share - reach[n]) for n in ms)
        return dead, deficit

    rows = []
    for combo in combinations(sorted(TBR_MAKE), n_tt):
        cnt, vol_bad = uncovered(set(combo))
        dead, deficit = stranded(set(combo))
        makes = len({TBR_MAKE[n] for n in combo})
        if crit == "gt":
            key = (cnt, vol_bad, makes, combo)
        elif crit == "machine":
            key = (cnt, vol_bad, dead, makes, combo)
        else:
            key = (cnt, vol_bad, dead, deficit, makes, combo)
        rows.append((key, [n for n in TBR_MAKE if n not in combo], combo))
    rows.sort(key=lambda r: r[0])
    return n_tt, rows, n_raw, n_use


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("months", nargs="+")
    ap.add_argument("--basis", choices=["restricted", "raw"], default="restricted",
                    help="'raw' reproduces today's l2_capability behaviour")
    ap.add_argument("--crit", default="coverage", choices=["gt", "machine", "coverage"])
    ap.add_argument("--write", action="store_true",
                    help="rewrite cap_ttl_groups_<M>.parquet with the winner")
    a = ap.parse_args()

    rc = 0
    for month in a.months:
        n_tt, rows, n_raw, n_use = score_all(month, a.basis, a.crit)
        win_key = rows[0][0][:-2]                     # drop makes/combo tiebreaks
        ties = [r for r in rows if r[0][:-2] == win_key]
        tl = rows[0][1]

        f = paths.wh_derived(f"cap_ttl_groups_{month}.parquet")
        on_disk = None
        if f.exists():
            d = pl.read_parquet(f)
            on_disk = sorted(int(r["machine"][6:-6]) for r in d.iter_rows(named=True)
                             if r["group"] == "TL")

        print(f"\n=== {month} ===  basis={a.basis}  n_tt={n_tt}  "
              f"TBR eligibility rows {n_raw} -> {n_use}")
        print(f"  winning key {tuple(round(x, 1) if isinstance(x, float) else x for x in win_key)}"
              f"   ->  TL={tl}")
        if len(ties) > 1:
            print(f"  ** {len(ties)}-WAY TIE — the winner is decided by machine number, "
                  f"not by evidence: " + "  ".join(f"TL={t[1]}" for t in ties))
            print("     B16 step 7 (capacity) is what should break this. Not implemented.")
        else:
            print("  unique winner (no tiebreak used) — safe to regenerate")
        if on_disk is not None:
            mark = "MATCHES" if on_disk == tl else "** DIFFERS FROM **"
            print(f"  file on disk TL={on_disk}  {mark} the rule")
            if on_disk != tl and not a.write:
                rc = 1

        if a.write:
            tt_m = sorted(rows[0][2])
            out = pl.DataFrame([
                {"plant": "TBR", "machine": f"TBMTBR{n}Stage2", "make": TBR_MAKE[n],
                 "group": ("TT" if n in tt_m else "TL")} for n in TBR_MAKE])
            out.write_parquet(f)
            print(f"  -> wrote {f}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
