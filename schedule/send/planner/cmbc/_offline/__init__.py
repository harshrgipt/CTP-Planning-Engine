"""Rebuild-only layers. They MINE the frozen inputs from the raw MES drop and
are NOT part of the shipped pipeline. Run them only when new MES history lands:

    l0_learn      -> warehouse/params/params_<as_of>.json      (tau*, yields)
    l2_capability -> warehouse/derived/cap_*_<month>.parquet   (what runs where)
    l3_ceiling    -> warehouse/derived/l3_cavities.parquet     (read by L5)

Each needs `v_curing`/`v_build`, i.e. the 4.4 GB CSV drop and `planner.cli
ingest`. On a clone without it they raise CatalogException -- that is why the
shipped gate is `planner/cmbc/l1_preflight.py`, which checks the same contracts
against the frozen masters instead.

    python main.py rebuild --month YYYY-MM
"""
