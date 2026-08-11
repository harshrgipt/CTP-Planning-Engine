"""Contracts for `planner.paths` -- the single input resolver.

These lock in the two things that would silently change the plan if someone
"simplified" the module.
"""
import hashlib

import polars as pl
import pytest

from planner import paths


def test_input_and_warehouse_derived_are_separate_namespaces():
    """`press_mould_change.parquet` exists in BOTH INPUT/derived and
    warehouse/derived and THE TWO DIFFER. L5 and L10 read the warehouse copy.
    A resolver that fell back from one to the other would silently change the
    cure schedule, so the two lookups must stay distinct functions."""
    name = "press_mould_change.parquet"
    a, b = paths.input_derived(name), paths.wh_derived(name)
    assert a != b, "input_derived and wh_derived resolved to the same file"
    if a.exists() and b.exists():
        ha = hashlib.sha1(a.read_bytes()).hexdigest()
        hb = hashlib.sha1(b.read_bytes()).hexdigest()
        assert ha != hb, (
            "press_mould_change.parquet is now identical in both trees. That is "
            "fine, but re-check every other shared filename before merging the "
            "two resolvers -- 21 names exist in both.")


def test_opening_gt_honours_env(monkeypatch):
    """Bare name resolves inside masters/opening_gt/, absolute path taken as
    given. The EXPORTERS read this too -- a mismatch between the arm and the
    export silently changes GT_Inventory in the BTP fulfilment sheets."""
    monkeypatch.delenv("PLANNER_OPENING_GT", raising=False)
    assert paths.opening_gt("2026-08").name == "opening_gt_2026-08.parquet"

    monkeypatch.setenv("PLANNER_OPENING_GT", "opening_gt_manual_2026-08.parquet")
    p = paths.opening_gt("2026-08")
    assert p.name == "opening_gt_manual_2026-08.parquet"
    assert p.parent == paths.MASTERS / "opening_gt"

    monkeypatch.setenv("PLANNER_OPENING_GT", str(paths.ROOT / "elsewhere.parquet"))
    assert paths.opening_gt("2026-08") == paths.ROOT / "elsewhere.parquet"


def test_raw_files_live_in_one_folder():
    """Every plant workbook the engine reads is under INPUT/raw/, not loose at
    the wrapper root."""
    for n in ("wcmaster 1.xlsx", "Recipemaster 1.xlsx", "Book6(Sheet5).csv"):
        p = paths.raw(n)
        assert p.exists(), f"{n} not found at {p}"
        assert p.parent == paths.RAW, f"{n} resolved outside INPUT/raw: {p}"


@pytest.mark.skipif(not paths.input_derived("gt_machine_partition.parquet").exists(),
                    reason="partition not built in this checkout")
def test_partition_carries_a_month_stamp():
    """L7 refuses a partition built for another month. The stamp is what makes
    that gate possible -- a stale partition once cost July 0.58 pt of fulfilment
    while every other check passed."""
    p = pl.read_parquet(paths.input_derived("gt_machine_partition.parquet"))
    assert "month" in p.columns
    assert p["month"].n_unique() == 1, "partition mixes months"
