"""Ingest the PCR SKU list and the TBR BUILDING ALLOWABLE MATRIX.

    python scripts/ingest_sku_matrix.py

Two gaps closed at once.

1. GT <-> SKU, from the plant rather than inferred
   ALL PCR CTP SKUS.xlsx  sheet 'sku and gt code mapping' gives
   SKU / GT / description / curing recipe id directly. The BOM route resolved
   55% of GTs and ~0% of TBR; this is authoritative.

2. TBR MACHINE CERTIFICATION -- the real J_g
   TBR BUILDING ALLOWABLE MATRIX.xlsx is a SKU x TBM01..TBM09 YES/NO grid. This
   is what a machine MAY run, which is a different object from what it HAS run.
   Our mined matrix showed a median of 2 machines per GT; if certification is
   wider, we have been planning inside an artificially narrow feasible set --
   and that is exactly the constraint that pinned 47 lots onto two machines and
   pushed the build span 9 days past month end.

The comparison printed at the end is the point: CERTIFIED vs USED. History can
only ever be a subset of certification, so the difference is capacity we own and
have not been planning with.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import polars as pl

from planner.config import CONFIG
from planner.data.warehouse import duck, set_cutoff
from planner.runs.logger import log

PCR_SKU = Path("C:/Users/91810/Downloads/send/ALL PCR CTP SKUS.xlsx")
TBR_MTX = Path("C:/Users/91810/Downloads/send/TBR BUILDING ALLOWABLE MATRIX.xlsx")


def _c(x) -> str:
    return "".join(ch for ch in str(x) if ord(ch) < 128).strip() if x is not None else ""


def _rows(p: Path, sheet: str | None = None, header_row: int = 0) -> list[dict]:
    import openpyxl
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    raw = [[_c(c) for c in r] for r in ws.iter_rows(values_only=True)]
    wb.close()
    hdr = raw[header_row]
    return [dict(zip(hdr, r)) for r in raw[header_row + 1:] if any(r)]


def main() -> int:
    set_cutoff(None)
    out = CONFIG.paths.warehouse / "derived"
    out.mkdir(parents=True, exist_ok=True)

    # ---------------- 1. PCR GT <-> SKU --------------------------------
    pcr = _rows(PCR_SKU, "sku and gt code mapping", header_row=2)
    pm = pl.DataFrame([{
        "plant": "PCR",
        "sku_code": d.get("SKU / Product Code", ""),
        "gt_code": d.get("GT Code", ""),
        "description": d.get("Description", ""),
        "curing_recipe": d.get("Curing Recipe id", ""),
        "recipe_id": d.get("Recipeid", ""),
    } for d in pcr if d.get("GT Code")])
    print(f"PCR SKU mapping: {pm.height} rows, "
          f"{pm['gt_code'].n_unique()} GTs, {pm['sku_code'].n_unique()} SKUs")

    # ---------------- 2. TBR certification matrix ----------------------
    # TBR MES `itemCode` comes in TWO forms and neither joins to the matrix's
    # `GT CODE` directly:
    #     'GT 5075 - 295/90R20 JUH XF'   GT-led  -> take the 'GT NNNN' prefix
    #     '8.25R20JUH5'                  size-led-> prefix-match the matrix
    #                                              DESCRIPTION ('8.25R20_JUH5_...')
    # Matching on GT CODE alone gives 0 of 83; on description alone 44 of 83.
    # Both routes together are needed. Same family as the PCR SAP-code/name
    # split and the wcID/iD crosswalk.
    tbr = _rows(TBR_MTX, "SKU-MACHINE-CONSTRUCTION")
    tbm_cols = [c for c in tbr[0] if re.match(r"^TBM\s*\d+$", c or "")]
    cert_rows, sku_rows = [], []
    for d in tbr:
        gt = d.get("GT CODE", "")
        sku = d.get("SKU CODE", "")
        if not gt:
            continue
        sku_rows.append({"plant": "TBR", "sku_code": sku, "gt_code": gt,
                         "description": d.get("DESCRIPTION", ""),
                         "curing_recipe": "", "recipe_id": ""})
        for c in tbm_cols:
            if str(d.get(c, "")).strip().upper() == "YES":
                n = re.sub(r"\D", "", c)
                cert_rows.append({"plant": "TBR", "gt_code": gt, "sku_code": sku,
                                  "machine_no": int(n),
                                  "machine": f"TBMTBR{int(n)}Stage2"})
    # map each matrix row to the MES itemCode(s) it actually represents
    def _norm(s: str) -> str:
        s = _c(s).upper().replace("_", " ").replace("-", " ")
        return re.sub(r"[^A-Z0-9.]", "", re.sub(r"\s+", " ", s))

    mes_tbr = [r[0] for r in duck().execute(
        "SELECT DISTINCT itemCode FROM v_build WHERE stage=2 AND plant='TBR' "
        "AND itemCode IS NOT NULL").fetchall()]
    desc_of = {}
    for d in tbr:
        if d.get("GT CODE"):
            desc_of.setdefault(d["GT CODE"], []).append(d.get("DESCRIPTION", ""))
    mes_to_gt: dict[str, str] = {}
    for code in mes_tbr:
        g = re.match(r"^(GT\s*\d+)", _c(code), re.I)
        if g:                                    # GT-led form
            key = re.sub(r"\s+", " ", g.group(1).upper())
            for gt in desc_of:
                if re.sub(r"\s+", " ", gt.upper()) == key:
                    mes_to_gt[code] = gt
                    break
            if code in mes_to_gt:
                continue
        n = _norm(code)                          # size-led form
        for gt, descs in desc_of.items():
            if any(_norm(x).startswith(n) for x in descs if x):
                mes_to_gt[code] = gt
                break
    print(f"TBR MES itemCode -> matrix GT CODE: "
          f"{len(mes_to_gt)}/{len(mes_tbr)} matched "
          f"({100*len(mes_to_gt)/max(len(mes_tbr),1):.1f}%)")

    cert = pl.DataFrame(cert_rows).unique()
    # add the MES-facing key so the engine can actually use this
    alias = pl.DataFrame([{"gt_code": v, "mes_gt": k} for k, v in mes_to_gt.items()])
    if alias.height:
        cert = cert.join(alias, on="gt_code", how="left")
        cert = cert.with_columns(
            pl.col("mes_gt").fill_null(pl.col("gt_code")).alias("mes_gt"))
    cert = cert.sort(["gt_code", "machine_no"])
    tm = pl.DataFrame(sku_rows).unique(subset=["sku_code", "gt_code"])
    print(f"TBR certification: {cert.height} certified (GT, machine) pairs over "
          f"{cert['gt_code'].n_unique()} GTs and {cert['machine'].n_unique()} machines")
    print(f"TBR SKU mapping  : {tm.height} rows, {tm['gt_code'].n_unique()} GTs")

    sku = pl.concat([pm, tm]).sort(["plant", "gt_code", "sku_code"])
    sku.write_parquet(out / "gt_sku_master.parquet", compression="zstd")
    cert.write_parquet(out / "tbr_machine_certified.parquet", compression="zstd")

    # ---------------- 3. CERTIFIED vs USED -----------------------------
    print("\n" + "=" * 76)
    print("CERTIFIED (may run) vs USED (has run) -- TBR building machines")
    print("=" * 76)
    used = duck().execute("""
        SELECT DISTINCT itemCode AS mes_gt, machineCode AS machine
        FROM v_build WHERE stage=2 AND plant='TBR' AND itemCode IS NOT NULL
    """).pl()
    per_cert = (cert.filter(pl.col("mes_gt").is_not_null())
                .group_by("mes_gt").agg(pl.col("machine").n_unique().alias("certified")))
    per_used = used.group_by("mes_gt").agg(pl.col("machine").n_unique().alias("used"))
    j = per_cert.join(per_used, on="mes_gt", how="full", coalesce=True).fill_null(0)
    both = j.filter((pl.col("certified") > 0) & (pl.col("used") > 0))
    if both.height:
        print(f"  GTs in both: {both.height}")
        print(f"    machines CERTIFIED per GT : p50 {both['certified'].median():.0f}  "
              f"max {both['certified'].max()}")
        print(f"    machines USED      per GT : p50 {both['used'].median():.0f}  "
              f"max {both['used'].max()}")
        print(f"    => certification is {both['certified'].median()/max(both['used'].median(),1):.1f}x "
              f"wider than history")
    # did the plant ever use a machine it is NOT certified for?
    viol = used.join(cert.select(["mes_gt", "machine"]).unique(),
                     on=["mes_gt", "machine"], how="anti")
    inter = used.join(per_cert.select("mes_gt"), on="mes_gt", how="semi")
    print(f"  GT-machine pairs USED but NOT certified: {viol.join(per_cert.select('mes_gt'), on='mes_gt', how='semi').height}"
          f" of {inter.height} (on GTs the matrix covers)")
    print(f"  MES TBR GT codes covered by the matrix: "
          f"{used.join(per_cert.select('mes_gt'), on='mes_gt', how='semi')['mes_gt'].n_unique()}"
          f"/{used['mes_gt'].n_unique()}")

    # ---------------- 4. coverage of MES -------------------------------
    mes = {r[0] for r in duck().execute(
        "SELECT DISTINCT itemCode FROM v_build WHERE stage=2 AND itemCode IS NOT NULL"
    ).fetchall()}
    have = set(sku["gt_code"].to_list())
    print(f"\nGT->SKU coverage vs MES: {len(mes & have)}/{len(mes)} "
          f"({100*len(mes & have)/len(mes):.1f}%)")
    print(f"WROTE {out/'gt_sku_master.parquet'}  ({sku.height} rows)")
    print(f"WROTE {out/'tbr_machine_certified.parquet'}  ({cert.height} rows)")
    log.info("sku_matrix.done", sku=sku.height, cert=cert.height)
    return 0


if __name__ == "__main__":
    sys.exit(main())
