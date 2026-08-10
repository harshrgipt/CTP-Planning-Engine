# INPUT — every file the engine needs

Assembled by `schedule/send/scripts/build_input.py` (re-runnable, idempotent).
Coverage measured against **July demand**: PCR 48 GTs / 48 SKUs · TBR 56 GTs / 56 SKUs.

```
INPUT/
├── raw/        10 source files, copied verbatim — never edited
├── derived/    17 parquets — 4 newly derived, 13 from warehouse
├── demand/     8 months
└── MANIFEST.csv
```

---

## NEWLY DERIVED — these did not exist before

| File | Rule | Coverage (by SKU) | Note |
|---|---|---|---|
| `tt_tl.parquet` | **R8** | **88 % PCR · 100 % TBR** | 902 TT / 1,423 TL |
| `gt_size.parquet` | **R6, R7** | **88 % PCR · 100 % TBR** | 10 rim classes; was 71 %/64 % by GT |
| `aging_limits.parquet` | **R5, R17** | **100 % · 100 %** | per-SKU MaxAging / Minaging / interlock |
| `mould_cavity.parquet` | R3, R14 | 69 % · 45 % | `curingCapacity` per SKU |

### R8 was never impossible — it was a regex bug

`\bTT\b` cannot match `10.00R20_JDC3_16PR_K_TT`, because `_` is a word character so
there is no word boundary before `TT`. That single detail made R8 look unimplementable
and produced the earlier "15 of 1,724 rows" reading.

With an explicit delimiter class `(?:^|[\s_/\-])(TT|TL)(?:$|[\s_/\-])`:
- TBR allowable matrix DESCRIPTION → **93.0 %** tagged
- `Master_Mapping_Mould_SKU` description → **95.7 %** tagged

**GAP-10 is closed. R8 moves from INERT to implementable.**

### Per-SKU aging limits were never used

`Recipemaster 1.xlsx` carries `MaxAging`, `Minaging`, `AgingInterlock` for all
1,282 SKUs. We have been using a **global 72 h** for R5 and a **guessed 2.0 h**
for τ_min.

| column | distribution | meaning |
|---|---|---|
| `MaxAging` | 0 h ×711 · **48 h ×493** · 72 h ×74 | shelf life — **48 h, not 72 h, wherever it is set** |
| `Minaging` | 0 ×721 · **15 ×542** | minimum age before cure |
| `AgingInterlock` | on for **136** of 1,282 | is the limit actually enforced |

⚠️ **On the July planning universe specifically**, MaxAging is unset (0) for 114 of
126 SKU rows and 48 h for 12. So the 48 h finding is real but narrow *for this month* —
R5 should be `min(72 h, MaxAging where MaxAging > 0)`, not a blanket change.

⚠️ **Units are unconfirmed and inconsistent.** `MaxAging` 48/72 reads as hours
(2 and 3 days, matching R5's "three days"). `Minaging` 15 reads as *minutes* — and
15 min = 0.25 h, which is **exactly the τ_min measured independently from 8 months of
MES**. That agreement is strong evidence, but two units in one table must be
confirmed by the plant before either is enforced. → plant letter.

---

## KNOWN LIMITATIONS OF THIS BUILD

1. **`tt_tl` and `gt_size` are SKU-keyed, not GT-keyed** (GT coverage reads 0 %).
   The TBR matrix uses its own GT codes; the MES uses different ones. The bridge
   exists in `tbr_machine_certified.mes_gt` and must be applied before these
   files can be joined on `gt_code`. **Do this before R6/R7/R8 are wired.**
2. **`aging_limits` has duplicate SKU rows** — 126 join rows for 104 planned SKUs.
   Needs a dedup rule (latest `lastUpdatedDate`) before use.
3. **`mould_cavity` at 69 %/45 %** does not close GAP-3. Cavity count is present for
   about half the planned SKUs; physical press compatibility and maintenance status
   are still absent.

---

## FULL INVENTORY

### `raw/` — source of truth, never edited

| File | Feeds |
|---|---|
| `Building Business Rules.docx` | R1–R12, the 14-step order |
| `TBR BUILDING ALLOWABLE MATRIX.xlsx` | R2 eligibility, R7 construction, R8 TT/TL |
| `Master_Building_ChangeoverTime_{pcr,tbr}.csv` | **R7 cost model, R13 crew** |
| `Master_Mapping_Mould_SKU.csv` | R3, R14, R8 |
| `Recipemaster 1.xlsx` | **R5/R17 aging, R3 cavity, R6 size** |
| `ALL PCR CTP SKUS.xlsx` | PCR GT↔SKU, recipe link |
| `CTP Set up building ,curing and inspection (1) 2.xlsx` | platen dia, mould-change time — **not yet mined** |
| `recipelookup 1.xlsx`, `wcmaster 1.xlsx` | recipe ↔ WC bridges — **not yet mined** |

### `derived/` — engine-ready

| File | Rule | Class |
|---|---|---|
| `allowed_press_matrix` (2,392) | R3, R4 | 🟩 HARD |
| `capacity_press_day` (172) · `capacity_machine_day` (20) | R10 | 🟩 HARD |
| `calendar_shifts` (3) | R10 | 🟩 HARD |
| `cycle_time_curing` (172) · `cycle_time_building` (20) | R3, R10 | 🟩 HARD |
| `aging_limits` (1,282) | R5, R17 | 🟩 HARD *(pending unit confirmation)* |
| `tt_tl` (2,325) | R8 | 🟨 SOFT-high |
| `gt_size` (599) | R6, R7 | 🟨 SOFT-high |
| `allowed_machine_matrix` (420) | R2 | 🟨 SOFT-high — mined, not plant-owned |
| `tbr_machine_certified` (670) · `sku_construction` (230) | R2, R7 | 🟨 TBR only |
| `gt_sku_master` (475) | R1 | 🟨 PCR only |
| `lot_size` (191) | R9 | 🟨 SOFT band |
| `mould_cavity` (510) · `press_platen_master` (184) | R3, R14 | 🟨 SOFT |
| `opening_gt_inventory` (90) | R1, R5 | 🟧 65 %/64 % — weakens a HARD rule |

---

## STILL NEEDED FROM THE PLANT

| ID | Need | Blocks |
|---|---|---|
| **GAP-2** | PCR SKU→machine **eligibility** (vs observed dedication) | R2. Decides whether PCR's HHI 1.00 is physical or habitual — the largest throughput question on site. |
| **GAP-3** | Mould cavity count (full), maintenance status, physical press compatibility | R3, exact cure ceiling |
| **GAP-4** | Mould-change crew roster *(durations partly found — 2/3 men, 665/manday)* | R13 |
| **GAP-8** | Semi-finished shelf life (tread tack, ply, belt) | R16 |
| **GAP-9** | Prep-shop routing and capacity | L8 |
| **NEW** | **Confirm aging units** — is `Minaging` minutes and `MaxAging` hours? | R5, R17 |
| **NEW** | **Confirm `MaxAging = 0`** means "no limit" or "not set" | R5 |

**GAP-1 (PCR construction matrix) is largely dissolved** — the plant's own changeover
model is binary same-size / different-size and we hold it for both plants.

**GAP-10 (TT/TL) is closed** — it was a parsing defect, not missing data.

**GAP-11 (size coverage) is closed to 88 %/100 % by SKU** — pending the GT-code bridge.

**GAP-12 (opening GT stock 65 %/64 %) remains open and is ours to fix.**
