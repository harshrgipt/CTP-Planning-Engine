import sys
from pathlib import Path

ROOT = Path(r"C:\Users\91810\Downloads\send\schedule\send")
sys.path.insert(0, str(ROOT))

from planner.data.warehouse import duck

c = duck()
print("views", c.sql("show tables").fetchall())
print("cure schema", c.sql("describe v_curing").fetchall())
print("build schema", c.sql("describe v_build").fetchall())
print("cure range/count", c.sql("select min(date), max(date), count(*) from v_curing").fetchall())
print("build range/count", c.sql("select min(date), max(date), count(*) from v_build").fetchall())
print("cure by month", c.sql("select date_trunc('month', date) m, count(*) n from v_curing group by 1 order by 1").fetchall())
print("build by month", c.sql("select date_trunc('month', date) m, count(*) n from v_build group by 1 order by 1").fetchall())

import polars as pl
run = ROOT / "runs" / "plant_2026-07"
inv = pl.read_parquet(run / "l11_invariants.parquet")
print("invariant schema", inv.schema)
for row in inv.iter_rows(named=True):
    print("INV", row["status"], "|", row["invariant"], "|", row["actual"], "|", row["target"], "|", row["basis"])

q = """
WITH b AS (
 SELECT productionID, plant, itemCode, event_ts, QualityStatus,
        count(*) over(partition by productionID) ndupe
 FROM v_build WHERE stage=2 AND productionID IS NOT NULL
), j AS (
 SELECT c.iD cure_id,c.plant,c.event_ts cure_ts,b.event_ts build_ts,b.ndupe,b.QualityStatus,
        epoch(c.event_ts-b.event_ts)/3600 wait_h
 FROM v_curing c JOIN b ON b.productionID=c.gtbarCode
 WHERE c.plant='PCR' AND c.statuscritical='Normal'
   AND c.date>=DATE '2026-07-01' AND c.date<DATE '2026-08-01'
)
SELECT count(*) join_rows, count(distinct cure_id) cures,
       count(*) filter(where ndupe>1) rows_dupe_build,
       count(*) filter(where wait_h<0) negative_wait,
       count(*) filter(where wait_h>72) over72,
       count(*) filter(where wait_h>168) over168,
       quantile_cont(wait_h,.5),quantile_cont(wait_h,.95),max(wait_h)
FROM j
"""
print("JOIN_AUDIT", c.sql(q).fetchall())
print("JULY_CURE", c.sql("select plant,statuscritical,count(*) from v_curing where date>=date '2026-07-01' and date<date '2026-08-01' group by all order by 1,2").fetchall())

for name in ["rule_table_2026-07.parquet", "l45_lots_2026-07.parquet", "plant_machine_make.parquet", "plant_ct_build.parquet", "plant_ct_cure_gt.parquet"]:
    p = ROOT / "warehouse" / "derived" / name
    if p.exists():
        d = pl.read_parquet(p)
        print("DERIVED", name, d.shape, d.schema)
        print(d.head(3))
