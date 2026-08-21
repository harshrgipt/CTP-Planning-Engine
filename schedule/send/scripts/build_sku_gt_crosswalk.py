"""Freeze SKU -> (plant, engine gt_code) into a committed master.

    python -m scripts.build_sku_gt_crosswalk

WHY THIS EXISTS
  `gt_namespace.sku_to_gt()` is the authoritative SKU->GT route, but it reads
  `v_curing` and `v_build` -- i.e. the raw MES drop, which is gitignored and is
  NOT present on a clone or on the frontend machine. So the order-book ingest,
  the one step a frontend user actually triggers, could only ever run on a
  machine holding 4.4 GB of MES. That defeats the point of a single-file upload.

  This is the same pattern as the other 29 masters in `INPUT/derived/`: mine it
  once where MES exists, commit the result, run from the committed file. The
  ingest then uses MES only as a CROSS-CHECK when it happens to be present.

THE NAMESPACE TRAP THIS EXISTS TO NAVIGATE  (CLAUDE.md, "GT namespace trap")
  `gt_sku_master.parquet` looks like the crosswalk and is half of one. Measured
  against every SKU the MES chain has actually resolved:

      PCR   108 of 108 gt_codes AGREE     -- it IS the engine namespace
      TBR     0 of 111 gt_codes agree     -- it is the BOM SHORT CODE namespace
                                             ("GT 5001" vs "10.00 R 20 JDC3")

  Its `plant` column, by contrast, is right on 1,825 of 1,825 rows. So this
  builder takes PCR gt_codes from it directly, and for TBR learns a short-code
  -> engine-code bridge from the SKUs that appear in both namespaces: 79 short
  codes, ZERO ambiguous. That bridge then places 93 further TBR SKUs that have
  never appeared in a demand file at all.

REFUSES RATHER THAN GUESSES
  Where two sources give different gt_codes for one SKU, the row is written with
  `conflict=true` and the higher-authority value; the conflicts are printed and
  must be looked at. Nothing is silently averaged or picked by string distance.
"""
from __future__ import annotations

import glob
import hashlib
import re
import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from planner import paths

ROOT = Path(__file__).resolve().parents[1]

# Source order = authority order. Earlier wins; a later source may only ADD a
# SKU, never overwrite one, and any overwrite it wanted is recorded as a
# conflict. `demand` is first because those rows are what the MES recipe chain
# actually resolved, in the exact namespace the layers plan on.
SRC_ORDER = ["demand", "gt_sku_master.pcr", "gt_sku_master.tbr.bridged",
             "gt_size.bridged", "gt_size"]

# "GT 5001" with nothing after it is the BOM short code, never a planning key.
# "GT 5001 - 10.00R20 JDC3" IS one. The hyphen is the whole difference.
_BARE_SHORT = re.compile(r"^GT\s*\d+$", re.I)


def _sha(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()[:12] if p.exists() else "ABSENT"


def _demand_files() -> list[str]:
    return sorted(
        f for f in glob.glob(str(ROOT / "masters" / "demand" / "demand_2*.parquet"))
        if "book2" not in f
    )


def build() -> tuple[pl.DataFrame, dict]:
    dm = pl.concat([pl.read_parquet(f).select("plant", "gt_code", "sku")
                    for f in _demand_files()]).unique()
    m = pl.read_parquet(paths.input_derived("gt_sku_master.parquet"))
    sz = pl.read_parquet(paths.input_derived("gt_size.parquet"))

    out: dict[str, dict] = {}
    conflicts: list[dict] = []
    unbridged_short: set[str] = set()

    def add(sku, plant, gt, src, rim=""):
        if not sku or not gt or plant not in ("PCR", "TBR"):
            return
        prev = out.get(sku)
        if prev is None:
            out[sku] = {"sku": sku, "plant": plant, "gt_code": gt, "rim": rim,
                        "src": src, "conflict": False}
            return
        if prev["gt_code"] != gt or prev["plant"] != plant:
            # A LATER SOURCE DISAGREES. Keep the higher-authority value, but say
            # so -- a silent pick here is how a TBR tyre reaches a PCR press.
            conflicts.append({"sku": sku,
                              "kept": prev["plant"] + "/" + prev["gt_code"],
                              "kept_src": prev["src"],
                              "rejected": plant + "/" + gt,
                              "rejected_src": src})
            prev["conflict"] = True
        if not prev["rim"] and rim:
            prev["rim"] = rim

    for r in dm.iter_rows(named=True):
        add(r["sku"], r["plant"], r["gt_code"], "demand")

    # PCR only -- verified engine namespace. TBR gt_codes from this file are the
    # BOM short code and would poison the map.
    for r in m.iter_rows(named=True):
        if r["plant"] == "PCR":
            add(r["sku_code"], "PCR", r["gt_code"], "gt_sku_master.pcr")

    # TBR: learn short -> engine from SKUs known in both namespaces, then apply.
    short = {r["sku_code"]: r["gt_code"] for r in m.iter_rows(named=True)
             if r["plant"] == "TBR" and r["sku_code"] and r["gt_code"]}
    eng_tbr = {r["sku"]: r["gt_code"] for r in dm.iter_rows(named=True)
               if r["plant"] == "TBR"}
    bridge: dict[str, str] = {}
    ambiguous: set[str] = set()
    for s, sc in short.items():
        e = eng_tbr.get(s)
        if not e:
            continue
        if sc in bridge and bridge[sc] != e:
            ambiguous.add(sc)
        bridge[sc] = e
    for sc in ambiguous:                    # an ambiguous short code places nothing
        bridge.pop(sc, None)
    # `gt_size` carries the SAME trap: 121 of its 182 TBR rows hold the bare
    # short code, 0 of its 83 PCR rows do. So a second bridge tier -- the engine
    # often writes TBR as "GT 5123 - 385/65R22.5 JUL4", whose numeric head IS
    # the short code. Accept a head match only when exactly ONE engine GT has
    # that head; two candidates means we do not know, so nothing is placed.
    engine_gts = set(dm["gt_code"].to_list()) | {
        r["gt_code"] for r in sz.iter_rows(named=True)
        if r.get("gt_code") and not _BARE_SHORT.match(r["gt_code"].strip())}
    for sc in sorted({g.strip() for g in sz["gt_code"].to_list()
                      if g and _BARE_SHORT.match(g.strip())} | set(short.values())):
        if sc in bridge:
            continue
        head = sc.upper().replace(" ", "")
        cands = [g for g in engine_gts
                 if g.upper().replace(" ", "").startswith(head + "-")]
        if len(cands) == 1:
            bridge[sc] = cands[0]

    for s, sc in short.items():
        if sc in bridge:
            add(s, "TBR", bridge[sc], "gt_sku_master.tbr.bridged")

    for r in sz.iter_rows(named=True):
        if not r.get("sku") or r.get("plant") not in ("PCR", "TBR"):
            continue
        gt, rim = r["gt_code"], str(r.get("rim") or "")
        if gt and _BARE_SHORT.match(gt.strip()):
            # A bare short code is NOT a planning key. Bridge it or drop it --
            # writing it through would put an unplannable gt_code in the map.
            gt = bridge.get(gt.strip(), "")
            if not gt:
                unbridged_short.add(r["gt_code"].strip())
                continue
            add(r["sku"], r["plant"], gt, "gt_size.bridged", rim)
            continue
        add(r["sku"], r["plant"], gt, "gt_size", rim)

    # Rim backfill, keyed on gt_code -- verified unique across the two plants, so
    # the key cannot pull a rim from the wrong plant. `gt_size` files most TBR
    # rims under the SHORT code, so index each rim under the bridged engine code
    # as well; without this 184 rows carry no rim and lose their one physical
    # confirmation of which plant they belong to.
    rim_by_gt: dict[str, str] = {}
    for r in sz.iter_rows(named=True):
        g, rim = r.get("gt_code"), str(r.get("rim") or "")
        if not g or not rim:
            continue
        rim_by_gt.setdefault(g, rim)
        if _BARE_SHORT.match(g.strip()) and g.strip() in bridge:
            rim_by_gt.setdefault(bridge[g.strip()], rim)
    for v in out.values():
        if not v["rim"]:
            v["rim"] = rim_by_gt.get(v["gt_code"], "")

    df = pl.DataFrame(sorted(out.values(), key=lambda z: (z["plant"], z["sku"])))
    by_src = {r["src"]: int(r["len"]) for r in
              df.group_by("src").len().sort("src").iter_rows(named=True)}
    prov = {
        "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": df.height,
        "by_src": by_src,
        "by_plant": {p: int(df.filter(pl.col("plant") == p).height)
                     for p in ("PCR", "TBR")},
        "tbr_bridge_size": len(bridge),
        "tbr_bridge_ambiguous_dropped": sorted(ambiguous),
        "unbridged_short_codes": sorted(unbridged_short),
        "conflicts": conflicts,
        "no_rim": int(df.filter(pl.col("rim") == "").height),
        "inputs": {
            "gt_sku_master.parquet": _sha(paths.input_derived("gt_sku_master.parquet")),
            "gt_size.parquet": _sha(paths.input_derived("gt_size.parquet")),
            "demand_files": [Path(f).name for f in _demand_files()],
        },
    }
    return df, prov


def main() -> int:
    df, prov = build()
    out = paths.input_derived("sku_gt_crosswalk.parquet")
    df.write_parquet(out, compression="zstd")
    Path(str(out).replace(".parquet", ".provenance.json")).write_text(
        json.dumps(prov, indent=2), encoding="utf-8")

    print("=" * 78)
    print("SKU -> (plant, engine gt_code) CROSSWALK")
    print("=" * 78)
    print("  rows             " + str(df.height))
    print("  by plant         " + str(prov["by_plant"]))
    print("  by source        " + str(prov["by_src"]))
    print("  TBR bridge       " + str(prov["tbr_bridge_size"]) + " short codes, "
          + str(len(prov["tbr_bridge_ambiguous_dropped"])) + " ambiguous dropped")
    if prov["unbridged_short_codes"]:
        print("  UNBRIDGED short  " + str(len(prov["unbridged_short_codes"]))
              + " TBR short codes have no engine GT -- their SKUs are dropped, "
                "not guessed")
        print("                   " + ", ".join(prov["unbridged_short_codes"][:12]))
    print("  rows with no rim " + str(prov["no_rim"])
          + "   (rim is what physically confirms the plant)")
    if prov["conflicts"]:
        print("")
        print("  !! " + str(len(prov["conflicts"])) + " CONFLICTS -- a lower source disagreed:")
        for c in prov["conflicts"][:15]:
            print("     " + c["sku"].ljust(20) + " kept " + c["kept"].ljust(26)
                  + " (" + c["kept_src"] + ")")
            print("     " + "".ljust(20) + " over " + c["rejected"].ljust(26)
                  + " (" + c["rejected_src"] + ")")
    else:
        print("")
        print("  no conflicts between sources")
    print("")
    print("  -> " + str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
