"""What signal is sitting in the MES that we are NOT using?

    python scripts/audit_unused_columns.py

Profiles every column of v_build / v_curing / v_consume: fill rate, cardinality,
and a value sample. Columns the engine already consumes are marked USED; the
rest are candidates. The point is to find data that would close a KNOWN gap --
calendar/downtime, mould count M_g, operator effects, consumable life -- rather
than to admire the schema.
"""
from __future__ import annotations

import sys

from planner.data.warehouse import duck, set_cutoff
from planner.runs.logger import log

USED = {
    "v_build": {"plant", "stage", "itemCode", "machineCode", "productionID",
                "event_ts", "date", "QualityStatus"},
    "v_curing": {"plant", "wcID", "gtbarCode", "event_ts", "date", "cycleStart",
                 "statuscritical", "recipeID", "MouldCodeLH", "MouldCodeRH",
                 "MouldCountLH", "MouldCountRH"},
    "v_consume": set(),
}


def profile(view: str) -> None:
    con = duck()
    try:
        cols = [r[0] for r in con.execute(f"DESCRIBE {view}").fetchall()]
        n = con.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
    except Exception as e:  # noqa: BLE001
        print(f"  {view}: unavailable ({str(e)[:60]})")
        return
    print("\n" + "=" * 88)
    print(f"{view}   {n:,} rows")
    print("=" * 88)
    print(f"  {'column':<20} {'used':<5} {'fill%':>6} {'distinct':>9}  sample")
    for c in cols:
        try:
            r = con.execute(
                f'SELECT count("{c}") f, count(DISTINCT "{c}") d FROM {view}'
            ).fetchone()
            fill = 100.0 * r[0] / max(n, 1)
            sample = con.execute(
                f'SELECT DISTINCT "{c}" FROM {view} WHERE "{c}" IS NOT NULL LIMIT 3'
            ).fetchall()
            sv = ", ".join(
                "".join(ch for ch in str(x[0]) if ord(ch) < 128)[:18] for x in sample)
        except Exception:  # noqa: BLE001
            continue
        mark = "USED" if c in USED.get(view, set()) else ""
        flag = ""
        if not mark and fill > 50 and 1 < r[1] < 5000:
            flag = "  <-- CANDIDATE"
        print(f"  {c:<20} {mark:<5} {fill:6.1f} {r[1]:>9,}  {sv}{flag}")


def deep(view: str, col: str, title: str) -> None:
    con = duck()
    try:
        d = con.execute(
            f'SELECT "{col}" v, count(*) n FROM {view} GROUP BY 1 '
            f'ORDER BY 2 DESC LIMIT 10').fetchall()
    except Exception:
        return
    print(f"\n  {title}  ({view}.{col})")
    for v, k in d:
        vs = "".join(ch for ch in str(v) if ord(ch) < 128)[:40]
        print(f"    {vs:<42} {k:>10,}")


def main() -> int:
    set_cutoff(None)
    for v in ("v_build", "v_curing", "v_consume"):
        profile(v)

    print("\n" + "=" * 88)
    print("DEEP DIVE on the columns most likely to close a KNOWN gap")
    print("=" * 88)
    # calendar / downtime -- our single biggest missing master
    deep("v_curing", "statusMinor", "statusMinor: stoppage reasons? -> CALENDAR")
    deep("v_curing", "updateStatus", "updateStatus")
    deep("v_curing", "cycleUpdate", "cycleUpdate")
    # M_g -- caps n_g, we only ever had a lower bound
    deep("v_curing", "mouldNo", "mouldNo: direct mould identity -> M_g")
    # operator effects
    deep("v_curing", "manningID", "manningID: operator/crew")
    deep("v_build", "shiftID", "shiftID (if present)")

    con = duck()
    print("\n  bladder life (consumable PM, like mould count):")
    try:
        r = con.execute("""
            SELECT plant, quantile_cont(BladderCountLH,0.5) p50,
                   max(BladderCountLH) mx, count(DISTINCT BladdercodeLH) codes
            FROM v_curing WHERE BladderCountLH IS NOT NULL GROUP BY 1
        """).fetchall()
        for p, a, b, c in r:
            print(f"    {p}: count p50 {a}, max {b}, {c:,} distinct bladder codes")
    except Exception as e:  # noqa: BLE001
        print("    unavailable:", str(e)[:60])

    print("\n  mouldNo vs MouldCodeLH -- which gives M_g?")
    try:
        r = con.execute("""
            SELECT b.plant,
                   count(DISTINCT c.mouldNo) AS by_mouldno,
                   count(DISTINCT c.MouldCodeLH) AS by_code
            FROM v_curing c JOIN v_build b ON b.productionID=c.gtbarCode
            WHERE b.stage=2 GROUP BY 1
        """).fetchall()
        for p, a, b in r:
            print(f"    {p}: mouldNo {a:,} distinct   MouldCodeLH {b:,} distinct")
    except Exception as e:  # noqa: BLE001
        print("    unavailable:", str(e)[:60])
    log.info("audit_unused.done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
