"""Locks the PCR/TBR bifurcation.

This is the one decision in the pipeline that nothing downstream can detect if
it is wrong: every layer trusts `plant`, so a TBR tyre routed to PCR is planned
against PCR machines, presses, moulds and lot floor, and every number for BOTH
plants is then wrong -- silently. So it gets a test, not just a docstring.

Sibling of `test_paths.py`, which locks the other decision of this kind (the
deliberate `input_derived` / `wh_derived` split).
"""
from __future__ import annotations

import polars as pl
import pytest

from planner import paths
from planner.cmbc.plant_split import (
    PCR_MAX_IN, TBR_MIN_IN, load_rim_map, load_sku_map, plant_from_rim,
    resolve_plant)


def _inch(rim: str) -> float | None:
    try:
        return float(str(rim).strip().upper().lstrip("R"))
    except ValueError:
        return None


def test_no_sku_belongs_to_both_plants():
    """The map is keyed on SKU alone. If one SKU could be both, the key is a lie
    and whichever row loaded last would win at random."""
    sk = load_sku_map()
    assert sk, "sku map is empty -- run scripts/build_sku_gt_crosswalk.py"
    assert all(v[0] in ("PCR", "TBR") for v in sk.values())


def test_no_rim_contradicts_its_plant():
    """The physical claim the whole module rests on: PCR tops out at R18, TBR
    starts at R20, and the 19-inch gap is empty. If this ever fails, the rim is
    no longer evidence and `resolve_plant` must stop treating it as decisive."""
    sk = load_sku_map()
    bad = []
    for sku, (plant, _gt, rim) in sk.items():
        if not rim:
            continue
        i = _inch(rim)
        if i is None:
            continue
        if plant == "PCR" and i >= TBR_MIN_IN:
            bad.append((sku, plant, rim))
        if plant == "TBR" and i <= PCR_MAX_IN:
            bad.append((sku, plant, rim))
    assert not bad, f"rim contradicts plant on {len(bad)} SKUs: {bad[:5]}"


def test_the_19_inch_gap_places_nothing():
    """A rim between the two families is a data error, not a new product. It must
    come back unplaced rather than rounded to the nearer side."""
    assert plant_from_rim("R19") is None
    assert plant_from_rim("R19.5") is None
    assert plant_from_rim(None) is None
    assert plant_from_rim("not a rim") is None
    assert plant_from_rim("R18") == "PCR"
    assert plant_from_rim("R20") == "TBR"
    assert plant_from_rim("R22.5") == "TBR"


@pytest.mark.parametrize("month", ["2026-07", "2026-08"])
def test_resolver_reproduces_the_prepared_months(month):
    """Replay every row of a committed demand file through the resolver using
    ONLY the SKU -- no Classification column, no GT, no MES. It must land every
    row on the plant the MES recipe chain put it on, with zero refusals.

    July is the harder case: its source workbook had no Classification column at
    all, so the split there was never human-supplied to begin with.
    """
    p = paths.ROOT / "masters" / "demand" / f"demand_{month}.parquet"
    if not p.exists():
        pytest.skip(f"demand_{month}.parquet not present")
    dm = pl.read_parquet(p)
    rim, sk = load_rim_map(), load_sku_map()
    mismatch, refused = [], []
    for r in dm.iter_rows(named=True):
        plant, _basis, why = resolve_plant(r["sku"], None, None, None, rim, sk)
        if plant is None:
            refused.append((r["sku"], why))
        elif plant != r["plant"]:
            mismatch.append((r["sku"], r["plant"], plant))
    assert not mismatch, f"{len(mismatch)} rows placed on the WRONG plant: {mismatch[:5]}"
    assert not refused, f"{len(refused)} rows unplaceable: {refused[:5]}"


def test_disagreement_refuses_rather_than_picking_a_winner():
    """The behaviour the module exists for. A workbook saying PCR about a SKU the
    rim says is TBR must yield None -- picking either is how a TBR tyre reaches a
    PCR press, and no downstream layer would notice."""
    rim = {"GT TEST TBR": "R22.5"}
    sk = {"SKU-X": ("TBR", "GT TEST TBR", "R22.5")}
    plant, _basis, why = resolve_plant("SKU-X", None, "PCR", None, rim, sk)
    assert plant is None
    assert "disagree" in why


def test_lone_workbook_claim_is_accepted_but_marked():
    """A SKU with no master row has no rim, so nothing physical confirms it. The
    plant's own classification of its own new product is accepted -- dropping it
    would be the worse error -- but the basis must say so, because that string is
    what puts the row in the UNCONFIRMED report."""
    plant, basis, _why = resolve_plant("SKU-NEW", None, "TBR", None, {}, {})
    assert plant == "TBR"
    assert "UNCONFIRMED" in basis
    # ...and strict mode refuses it outright
    plant2, _b, why2 = resolve_plant("SKU-NEW", None, "TBR", None, {}, {}, strict=True)
    assert plant2 is None and "no rim" in why2


def test_no_source_means_no_placement():
    plant, _basis, why = resolve_plant("SKU-UNKNOWN", None, "", None, {}, {})
    assert plant is None
    assert why


def test_crosswalk_holds_no_bare_bom_short_codes():
    """`gt_size` and `gt_sku_master` both file TBR under the BOM short code
    ("GT 5001"), which matches no gt_code any layer ever sees. The crosswalk
    exists to bridge those; if a bare one survives into it, the bridge regressed
    and those SKUs would resolve to an unplannable GT."""
    import re
    p = paths.input_derived("sku_gt_crosswalk.parquet")
    if not p.exists():
        pytest.skip("crosswalk not built")
    d = pl.read_parquet(p)
    bare = re.compile(r"^GT\s*\d+$", re.I)
    hits = [g for g in d["gt_code"].to_list() if g and bare.match(g.strip())]
    assert not hits, f"bare BOM short codes leaked into the crosswalk: {hits[:5]}"
