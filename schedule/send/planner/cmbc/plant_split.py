"""PLANT BIFURCATION -- one demand file in, PCR and TBR out, never guessed.

    from planner.cmbc.plant_split import resolve_plant, load_rim_map
    plant, basis, why = resolve_plant(sku, gt_code, classification, rim_map)

WHY THIS EXISTS
  The frontend should take ONE demand file. Splitting it between the two plants
  is our job, not the user's -- and it is the one decision in the whole pipeline
  where a wrong answer is silently unrecoverable: a TBR tyre routed to PCR gets
  planned against PCR machines, PCR presses, PCR moulds and a PCR lot floor, and
  every downstream number for BOTH plants is then wrong. Nothing downstream can
  detect it, because every layer trusts `plant`.

  So this module REFUSES rather than guesses. A SKU it cannot place with an
  agreeing pair of independent sources is returned as `None` with a reason, and
  the caller routes it to UNRESOLVED. Losing a row loudly is recoverable; putting
  it on the wrong plant is not.

THE PHYSICAL FACT THIS RESTS ON
  PCR and TBR do not share a rim size. Measured over `gt_size.parquet`, 583 rows:

      PCR   R12 R13 R14 R15 R16 R17 R18      83 GTs
      TBR   R20 R22.5                       132 GTs
      overlap: NONE. No GT appears in both plants.

  There is a clean gap at 19 inch, and it is not a coincidence of this dataset --
  a passenger-car radial and a truck-bus radial are different products on
  different machines. Checked against the plant's own `Classification` column on
  both prepared months: 104 GTs (July) and 110 GTs (August), **zero mismatches,
  zero GTs with no rim**.

THE THREE SOURCES, IN ORDER OF AUTHORITY
  1. RIM        `gt_size.parquet` -> rim -> >= 20 in is TBR, <= 18 in is PCR.
                Physical, and the only one that cannot be mis-keyed by a person.
  2. RECIPE     `gt_namespace.sku_to_gt()` carries the plant it mined the GT from.
  3. WORKBOOK   the `Classification` column, if the sheet has one.

  A SKU is placed only when at least two of these AGREE and none DISAGREES.
  One source alone is enough ONLY when it is the rim and no other source spoke --
  because the rim is physical and the other two are records that can be wrong.

WHAT THIS DELIBERATELY DOES NOT DO
  It does not parse the plant out of the SKU material code. The codes are
  positional and the format differs between plants and WITHIN TBR:

      1123111020016KJC30   index 8-9 = "20"   (10.00 R 20)
      115223852C020KUL40   index 8-9 = "2C"   (385/65R22.5)

  A fixed offset works for one family and silently mis-reads the other. The GT
  step is not skippable.
"""
from __future__ import annotations

import warnings

import polars as pl

from planner import paths

# The gap is at 19 inch: PCR tops out at R18, TBR starts at R20. Anything that
# ever lands between them is a data error, not a new product -- it is returned
# unresolved rather than rounded to a side.
PCR_MAX_IN = 18.0
TBR_MIN_IN = 20.0


def _inch(rim) -> float | None:
    """'R22.5' -> 22.5. Returns None rather than raising -- an unparseable rim
    must reach the caller as 'unknown', never as a number that happens to sort."""
    if rim is None:
        return None
    try:
        return float(str(rim).strip().upper().lstrip("R"))
    except ValueError:
        return None


def load_rim_map() -> dict[str, str]:
    """gt_code -> rim, from the committed `gt_size.parquet`.

    Keyed on gt_code alone: verified 0 GTs appear under both plants, so the key
    is unique and a plant-qualified key would only hide a future collision. Rows
    with a blank `plant` ARE kept -- 40 of 215 GTs carry no plant label, and the
    RIM is what places them, so dropping them would lose usable evidence."""
    sz = pl.read_parquet(paths.input_derived("gt_size.parquet"))
    return {r["gt_code"]: str(r["rim"]) for r in sz.iter_rows(named=True)
            if r.get("gt_code") and r.get("rim")}


def load_sku_map() -> dict[str, tuple[str, str, str]]:
    """sku -> (plant, engine gt_code, rim), for a demand file with NO plant column.

    This is what lets the frontend send bare (SKU, qty) rows: no MES, no recipe
    chain, no 4.4 GB drop -- `gt_namespace.sku_to_gt()` reads `v_curing` and so
    does not exist on a clone or on the frontend machine.

    READS `sku_gt_crosswalk.parquet` (`scripts/build_sku_gt_crosswalk.py`), NOT
    `gt_size.parquet` directly. Both carry `sku`, but gt_size is MIXED-NAMESPACE
    for TBR -- 121 of its 182 TBR rows hold the BOM short code ("GT 5123"), which
    is not a planning key and matches no gt_code the layers ever see. The
    crosswalk resolves those through a measured bridge and drops the 14 it cannot
    resolve. It is also strictly wider: 430 SKUs vs 202.

    Falls back to gt_size if the crosswalk has not been built, so an old checkout
    still runs -- but then TBR gt_codes may be short codes, which is why the
    fallback warns.

    Verified on the built file: 430 SKUs, ZERO in both plants, and of the 253
    rows carrying a rim, ZERO contradict their plant."""
    p = paths.input_derived("sku_gt_crosswalk.parquet")
    if p.exists():
        d = pl.read_parquet(p)
        return {r["sku"]: (r["plant"], r["gt_code"], str(r["rim"] or ""))
                for r in d.iter_rows(named=True)}
    warnings.warn(
        "sku_gt_crosswalk.parquet missing -- falling back to gt_size.parquet, "
        "whose TBR gt_codes are BOM short codes. Run "
        "`python -m scripts.build_sku_gt_crosswalk`.", stacklevel=2)
    # BLANK-PLANT ROWS ARE EXCLUDED HERE, unlike in `load_rim_map`. Keeping them
    # made 75 of 445 SKUs look like they belonged to both plants; every one was a
    # real row plus a blank-plant twin, not a genuine collision.
    sz = pl.read_parquet(paths.input_derived("gt_size.parquet")).filter(
        pl.col("plant").is_in(["PCR", "TBR"])
        & pl.col("sku").is_not_null() & (pl.col("sku") != ""))
    return {r["sku"]: (r["plant"], r["gt_code"], str(r["rim"]))
            for r in sz.iter_rows(named=True)}


def plant_from_rim(rim) -> str | None:
    """The physical rule. None means the rim is missing or sits in the 19-inch
    gap -- both are 'do not place', not 'pick the nearer side'."""
    i = _inch(rim)
    if i is None:
        return None
    if i >= TBR_MIN_IN:
        return "TBR"
    if i <= PCR_MAX_IN:
        return "PCR"
    return None


def resolve_plant(sku: str,
                  gt_code: str | None = None,
                  classification: str | None = None,
                  recipe_plant: str | None = None,
                  rim_map: dict[str, str] | None = None,
                  sku_map: dict[str, tuple[str, str, str]] | None = None,
                  strict: bool = False
                  ) -> tuple[str | None, str, str]:
    """(plant, basis, why). `plant is None` means REFUSE -- route to unresolved.

    `basis` names every source that spoke, so a row can always be traced back to
    the evidence that placed it. `why` is empty on success and carries the
    conflict on refusal.
    """
    votes: dict[str, list[str]] = {}

    # SKU -> (plant, gt, rim) first: it supplies the gt_code when the caller has
    # none, which is the whole point of a bare (SKU, qty) upload.
    hit = (sku_map or {}).get(sku)
    if hit and not gt_code:
        gt_code = hit[1]

    rim = (rim_map or {}).get(gt_code or "", None)
    if rim is None and hit:
        rim = hit[2]
    p_rim = plant_from_rim(rim)
    if p_rim:
        votes.setdefault(p_rim, []).append(f"rim={rim}")

    if hit and hit[0] in ("PCR", "TBR"):
        votes.setdefault(hit[0], []).append("sku_master")

    if recipe_plant in ("PCR", "TBR"):
        votes.setdefault(recipe_plant, []).append("recipe")

    cls = (classification or "").strip().upper()
    if cls in ("PCR", "TBR"):
        votes.setdefault(cls, []).append("workbook")

    if not votes:
        return None, "", "no source could place this SKU"

    if len(votes) > 1:
        # TWO SOURCES DISAGREE. This is exactly the case the module exists for:
        # picking a winner here is how a TBR tyre ends up on a PCR press. The
        # physical rim is the one I would trust, but a disagreement means one of
        # the masters is wrong and that is worth a human look, not an override.
        detail = " vs ".join(f"{p}({'+'.join(v)})" for p, v in sorted(votes.items()))
        return None, "", f"sources disagree: {detail}"

    plant = next(iter(votes))
    srcs = votes[plant]
    if len(srcs) == 1 and not srcs[0].startswith("rim"):
        # A LONE RECORD WITH NO PHYSICAL CONFIRMATION -- in practice this is a
        # SKU the plant has ordered but never built, so it has no master row and
        # therefore no rim. On August that is 42 SKUs / 47,079 tyres.
        #
        # `strict=True` refuses it. The DEFAULT accepts it, on purpose: the only
        # source is the plant's own classification of its own new product, and
        # the plant is the authority there. Dropping 47,079 tyres to avoid
        # trusting the plant about its own order book is the worse error.
        #
        # But it is never accepted SILENTLY -- the basis is returned marked, so
        # the ingest log and the UNCONFIRMED report both name every such row. A
        # wrong plant here is unrecoverable downstream, so it must stay visible.
        if strict:
            return None, "", (f"only {srcs[0]} says {plant}; no rim to confirm "
                              f"(gt_code={gt_code or 'unresolved'})")
        return plant, f"{srcs[0]}-UNCONFIRMED", (
            f"placed on {srcs[0]} alone -- no master row, so no rim to confirm")
    return plant, "+".join(srcs), ""


def split_report(rows: list[dict]) -> str:
    """One-line-per-plant summary for the ingest log. Callers print it; this
    module never prints, so it stays importable from a layer."""
    out = []
    for p in ("PCR", "TBR"):
        r = [x for x in rows if x.get("plant") == p]
        q = sum(float(x.get("qty") or 0) for x in r)
        out.append(f"{p} {q:,.0f} tyres / {len({x['gt_code'] for x in r})} GTs")
    return " · ".join(out)
