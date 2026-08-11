"""THE GT NAMESPACE BRIDGE -- one place, because it has misled this project twice.

The plant writes green-tyre codes in at least four different shapes and the
engine plans in exactly one: `v_build.itemCode` (MES stage-2), which is what
every capability, partition, cycle-time and cost table is keyed on.

    engine (MES itemCode)          plant workbooks
    ---------------------------    -----------------------------------------
    GT 1402 XPC TATA               1402 XPC TATA        (no "GT ")
    GT  T1457 STAR    (2 spaces)   T1457 STAR
    GT1564 NEO        (no space)   1564 NEO
    GT 2568 HT2                    2568 RAN HT2         (extra brand token)
    10.00 R 20 JDE                 10.00 R 20 JDE       (TBR, size-led)
    GT 5055 - 295/80R22.5 JUC XM   GT 5055              (TBR, BOM short code)

THE TBR TRAP, stated once so nobody rediscovers it a third time
  `warehouse/derived/gt_sku_master.parquet` (the BOM) keys TBR on "GT 5001" while
  TBR MES itemCode is size-led ("10.00 R 20 JDC3"). The two namespaces have ZERO
  string overlap. `scripts/make_demand.py` documents the same trap; it cost TBR
  100 % of its cycle-time coverage once already. A workbook column called
  "Matched GT Code" that reads "GT 5001" is therefore NOT a planning key.

WHAT THIS MODULE PROVIDES, in the order it should be tried

  1. `sku_to_gt()` -- THE AUTHORITATIVE ROUTE. Resolves a finished SKU to the GT
     it was actually built as, over the whole MES history, via the curing-recipe
     chain `Recipemaster.SAPMaterialCode -> v_curing.recipeID -> v_build.itemCode`
     (the chain `build_gt_sku_share.py` documents at 100 % of cured volume on both
     plants). This is measurement, not string matching, and it is the only route
     that crosses the TBR namespace gap.

  2. `resolve_gt_label()` -- STRING FALLBACK for a GT the chain cannot reach
     (a SKU never yet produced). Three tiers, each uniqueness-gated:
       a. normalised exact  -- strip a leading "GT", drop non-alphanumerics
       b. numeric head      -- "GT 5055" -> "GT 5055 - 295/80R22.5 JUC XM"
       c. token subset      -- the MES code's tokens are a subset of the label's
                               AND the numeric head matches AND exactly one
                               candidate qualifies ("2568 RAN HT2" -> "GT 2568 HT2",
                               against GT 2568 RAN AT / GT 2568 RAN HTP which do
                               not qualify). Ambiguity returns None; it is never
                               guessed.

  Every caller must report what tier 1/2 could not resolve. A GT absent from MES
  history has no capability row either, so it is genuinely unplannable and saying
  so is the correct answer -- not inventing a mapping for it.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import polars as pl
from planner import paths

ROOT = Path(__file__).resolve().parent.parent


def norm(s: str | None) -> str:
    """Collapse a GT label to its comparable core: no leading GT, alnum only."""
    s = (s or "").upper().strip()
    s = re.sub(r"^GT\s*", "", s)
    s = re.sub(r"\s*-\s*", " ", s)
    return re.sub(r"[^A-Z0-9]", "", s)


def _tokens(s: str | None) -> set[str]:
    s = (s or "").upper().strip()
    s = re.sub(r"^GT\s+", "", s)
    return {t for t in re.split(r"[^A-Z0-9.]+", s) if t}


def _head(s: str | None) -> str:
    m = re.match(r"^\s*(?:GT\s*)?(\d+)", (s or "").upper())
    return m.group(1) if m else ""


@lru_cache(maxsize=4)
def mes_namespace() -> dict[str, list[str]]:
    """Every gt_code the engine has ever seen, per plant, from MES stage-2."""
    import sys
    sys.path.insert(0, str(ROOT))
    from planner.data.warehouse import duck, set_cutoff
    set_cutoff(None)
    d = duck().execute("""SELECT DISTINCT plant, itemCode AS gt_code FROM v_build
                          WHERE stage = 2 AND itemCode IS NOT NULL""").pl()
    out: dict[str, list[str]] = {}
    for p, g in zip(d["plant"], d["gt_code"]):
        out.setdefault(p, []).append(g)
    return {k: sorted(v) for k, v in out.items()}


def resolve_gt_label(label: str, plant: str,
                     ns: dict[str, list[str]] | None = None) -> tuple[str | None, str]:
    """A plant-written GT label -> engine gt_code. Returns (gt_code|None, tier)."""
    ns = ns or mes_namespace()
    cands = ns.get(plant, [])
    n = norm(label)
    if not n:
        return None, "empty"
    exact = {norm(g): g for g in cands}
    if n in exact:
        return exact[n], "exact"
    h = _head(label)
    if h:
        # "GT 5055" -> "GT 5055 - 295/80R22.5 JUC XM"
        heads = [g for g in cands if re.match(rf"^GT\s*{h}\s*-", g.upper())]
        if len(heads) == 1:
            return heads[0], "numeric-head"
        lt = _tokens(label)
        sub = [g for g in cands if _head(g) == h and _tokens(g) <= lt]
        if len(sub) == 1:
            return sub[0], "token-subset"
        if len(sub) > 1:
            return None, f"ambiguous({len(sub)})"
    return None, "unknown"


@lru_cache(maxsize=4)
def sku_to_gt() -> dict[str, tuple[str, str, int]]:
    """SKU -> (plant, engine gt_code, tyres observed), modal over all history.

    The curing-recipe chain. `Recipemaster 1.xlsx`.`SAPMaterialCode` IS the
    finished SKU; `v_curing.recipeID` carries it on every cured tyre; the
    per-tyre barcode join `v_build.productionID = v_curing.gtbarCode` (MEMORY §3,
    99.6 % hit rate) gives the GT it was built as.
    """
    import sys
    sys.path.insert(0, str(ROOT))
    import openpyxl
    from planner.data.warehouse import duck, set_cutoff
    set_cutoff(None)
    con = duck()
    rm_path = paths.raw("Recipemaster 1.xlsx")
    wb = openpyxl.load_workbook(rm_path, read_only=True, data_only=True)
    raw = [[("".join(ch for ch in str(c) if ord(ch) < 128).strip()
             if c is not None else "") for c in r]
           for r in wb.worksheets[0].iter_rows(values_only=True)]
    wb.close()
    hdr = raw[0]
    recs = [dict(zip(hdr, r)) for r in raw[1:] if any(r)]
    con.register("recipe", pl.DataFrame(
        [{"recipe_id": d["iD"], "sku_code": d.get("SAPMaterialCode", "")}
         for d in recs]).to_arrow())
    chain = con.execute("""
        SELECT b.plant, r.sku_code, b.itemCode AS gt_code, count(*) AS n
        FROM v_curing c
        JOIN v_build b ON b.productionID = c.gtbarCode AND b.stage = 2
        JOIN recipe r  ON c.recipeID::VARCHAR = r.recipe_id
        WHERE c.statuscritical = 'Normal' AND b.itemCode IS NOT NULL
          AND r.sku_code <> ''
        GROUP BY 1, 2, 3""").pl()
    con.unregister("recipe")
    best: dict[str, tuple[str, str, int]] = {}
    for p, s, g, n in zip(chain["plant"], chain["sku_code"],
                          chain["gt_code"], chain["n"]):
        if s not in best or best[s][2] < n:
            best[s] = (p, g, int(n))
    return best
