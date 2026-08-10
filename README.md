# CTP Planner — synchronized Building + Curing scheduler for a two-plant tyre works

A production planning engine that reads a month's demand, the plant's masters and
~8 months of MES production history, and emits an hour-by-hour **building** and
**curing** schedule for two plants — **PCR** (passenger car radial, 11 building
machines / 86 presses) and **TBR** (truck & bus radial, 9 machines / 79 presses).

It is not a demand forecaster and not an ERP. It answers one question:

> Given this month's orders, which green tyre gets built on which machine at which
> hour, so that the press that needs it is neither starved nor over-fed, and no
> tyre sits past its 72-hour shelf life?

Everything below assumes you have never seen this project.

---

## 1. What the engine is, and the constraints it is locked into

Green tyres (**GT**) are built on building machines and cured in presses. A green
tyre is perishable — 72 hours from build to cure (rule **R5**) — so building and
curing cannot be planned independently. The engine treats GT inventory as a
**synchronization buffer**, not a production target, and releases building by
**pull**: a cure campaign is placed first, and building is released backwards from
it at `t_cure − τ* − build_duration`.

Five technical constraints were decided deliberately. Do not substitute
alternatives; several were chosen after the alternative was tried and measured.

| locked in | not used |
|---|---|
| classical statistics + pattern mining | ML, RL, LLMs |
| heuristic greedy + SA / Tabu / LNS | CP-SAT, MILP solvers |
| Polars + DuckDB + Parquet | pandas in the hot path |
| Python 3.11, project-local `.venv` | system-Python installs |
| files on disk between layers | in-process state between layers |

Every layer communicates **only through files**. That is what makes a single
layer re-runnable, and it is why every number in an output pack can be traced
back to a parquet on disk.

### The three numbers people confuse

| number | what it is |
|---|---|
| **built** | green tyres produced by building machines this month |
| **fed** | tyres delivered into presses — includes opening stock, excludes closing stock |
| **cured in-month** | the fulfilment numerator: `built + opening stock − closing stock` |

The KPI sheet (`9a_kpi_summary`) prints an explicit A + B − C = D reconciliation
so these can never be quoted interchangeably. A fourth number, **campaign
nameplate**, is press capacity seated and is never a tyre count.

---

## 2. Repository layout — and why it is nested

```
ctp-planner/                       <- WRAPPER ROOT. Plant workbooks live here.
├── README.md   VERSION   .gitignore
├── INPUT/
│   ├── derived/                   mined master parquets the scheduler reads
│   ├── demand/  opening_gt/  raw/ archived inputs by month
│   └── MANIFEST.csv               provenance of every derived file
├── cycletime/                     plant cycle-time workbooks (build + cure)
├── gtinvaug/                      manual GT floor counts, 2026-08-01
├── btpformat/                     the plant's own BTP workbooks (format reference)
├── August_Demand_PCR_TBR_Classification.xlsx
├── Recipemaster 1.xlsx   wcmaster 1.xlsx   ALL PCR CTP SKUS.xlsx   ...
└── schedule/send/                 <- ENGINE ROOT. Every command runs from here.
    ├── planner/
    │   ├── cmbc/                  ** THE LIVE ENGINE, layers L0–L12 **
    │   ├── config.py              single source of truth for every enforced cap
    │   ├── data/                  DuckDB warehouse singleton + ingest
    │   ├── plan/ learn/ kb/ …     RETIRED generation-1 engine (see §9)
    │   └── validate/              independent verifier
    ├── scripts/                   drivers, ingest, exporters, diagnostics
    ├── masters/                   demand, opening GT, press rosters, changeover
    ├── warehouse/                 derived/ params/ masters/ bom/ … (small, committed)
    ├── output/                    AUG2026_pack, JUL2026_pack
    ├── runs/                      aug_ship, hz_ext72 (reference runs)
    └── MEMORY.md  PARTITION_AND_CHANGEOVER.md  BUSINESS_RULES.md  EXPERT_AUDIT.md
```

**Why the two levels.** The engine resolves the plant workbooks and
`INPUT/derived/` as `ROOT.parent.parent`, i.e. two directories above `planner/`.
About thirty read sites depend on it. Flattening the tree breaks all of them, so
the nesting is preserved exactly as the code expects. `schedule/send/` is the
project root for every command; the wrapper above it is the data root.

---

## 3. Install and run (Windows)

`Makefile` and `scripts/bootstrap.sh` are POSIX-only and `make` is usually
unavailable on Windows. Invoke the modules directly.

```powershell
cd schedule\send
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest tests\ -q          # 11 tests, ~2 s
```

Under Git Bash the interpreter is `./.venv/Scripts/python.exe`; on Linux/macOS it
is `./.venv/bin/python` and the `make` targets work.

### Plan a month — the actual sequence

There is **no orchestrator** that chains L0→L12. `scripts/run_arm.py` builds
L5→L11; the layers before it are month-level and are run once.

```bash
cd schedule/send
export PYTHONPATH=.
PY=./.venv/Scripts/python.exe

# ---- 0. inputs for the month ------------------------------------------------
$PY -m scripts.ingest_orderbook_demand \
      --xlsx "../../August_Demand_PCR_TBR_Classification.xlsx" \
      --sheet "August Demand Classified" --month 2026-08
$PY -m scripts.ingest_manual_opening_gt --month 2026-08 --age-h 24 \
      --pcr "../../gtinvaug/gt_inventory_manual_pcr_20260801.xlsx" \
      --tbr "../../gtinvaug/gt_inventory_manual_tbr_20260801.xlsx"
export PLANNER_OPENING_GT=opening_gt_manual_2026-08.parquet

# ---- 1. month-level layers --------------------------------------------------
$PY -m planner.cmbc.l1_validate        --month 2026-08
$PY -m planner.cmbc.l2_capability      --month 2026-08     # needs raw MES
$PY -m planner.cmbc.l25_cie            --month 2026-08
$PY -m planner.cmbc.l3_ceiling         --month 2026-08
$PY -m planner.cmbc.l4_net_requirement --month 2026-08
$PY -m planner.cmbc.l45_lotsize        --month 2026-08

# ---- 2. REBUILD THE PARTITION. MANDATORY, PER MONTH. ------------------------
$PY scripts/build_gt_machine_partition.py 2026-08

# ---- 3. the schedule --------------------------------------------------------
$PY scripts/run_arm.py aug_ship --month 2026-08 \
      PLANNER_OPENING_GT=opening_gt_manual_2026-08.parquet

# ---- 4. exports + independent verification ----------------------------------
$PY scripts/export_shift_schedule.py aug_ship 2026-08 output/AUG2026_pack
$PY -m scripts.build_gt_sku_share --month 2026-08 --from-demand
$PY -m scripts.export_btp_format --run aug_ship --month 2026-08 \
      --out output/AUG2026_pack/btp
$PY scripts/verify_export.py output/AUG2026_pack 2026-08
```

**The partition rebuild is not optional.** The GT→machine partition is sized
against one month's demand and that month's calendar hours. L7 **refuses to plan**
if the file on disk carries a different month stamp — a stale partition once cost
July 0.58 pt of fulfilment and 10.3 pt of same-size while every gate passed.

`run_arm.py` deletes and rebuilds the run directory from L5. Never seed an arm
with `cp -r`: 15 run directories once carried another arm's scorecard, and a flag
worth 8,085 tyres read as free.

### What a clone can and cannot re-run

The raw MES CSVs (~4.4 GB) are **not** in this repository. Everything mined from
them is committed, so:

- **L3 → L12 run out of the box** — verified: a clean copy of this package
  reproduces the August plan to the tyre (477,350 of 523,335 = 91.2 %).
- **L0 (learning), L1's mould checks, L2 (capability) and `make ingest` need the
  raw MES.** Point the plant's CSV drops at `curing/`, `o_production/`,
  `io_production_consumption/` and run `python -m planner.cli ingest`.

---

## 4. The layer flow, L0 → L12

Phase A (L0–L4.5) decides *what and how much*. Phase B (L5–L12) decides *where and
when*. Curing proposes; building disposes.

| layer | name | reads | writes |
|---|---|---|---|
| **L0** | continuous learning — parameters only, never policy | MES history (8 months) | `warehouse/params/params_<as_of>.json` — τ\*, cure yield, availability, campaign-length distributions |
| **L1** | validation & constraint compilation | demand, opening GT, all masters | `l1_findings_<M>`, `rule_table_<M>`, `cost_table` — every input checked, every failure named; refuses to proceed on ERROR |
| **L2** | capability model — what *can* run where | MES history, TBR allowable matrix, PCR inch spec, mould inventory | `cap_machine_<M>`, `cap_press_<M>`, `cap_mould_<M>`, `cap_ttl_groups_<M>`, `cap_changeover` |
| **L2.5** | campaign intelligence — advisor, not generator | L2 + history | `cie_proposals_<M>`, `cie_mould_sets_<M>` (candidates with confidence; L3/L6 dispose) |
| **L3** | throughput ceiling (RCCP) | capacity, cavities, press roster | `l3_ceiling_<M>` — MAX_FEASIBLE/week per plant and which stage binds |
| **L4** | net cure requirement | demand, opening GT | `net_requirement_<M>` — `net_cure = demand − FG`, `gross_build = net_cure/yield − usable stock` |
| **L4.5** | lot sizing & demand consolidation | L4, mould sets, L0 campaign hours | `l45_lots_<M>` — the **cure** lot (build slices have no minimum) |
| **partition** | static GT→machine map, the way the plant builds | `l45_lots_<M>`, 8-month machine×size matrix | `INPUT/derived/gt_machine_partition.parquet` (**month-stamped**) |
| **L5** | cure campaign master — *the plan is born here* | L4.5 lots, presses, moulds, opening GT | `cure_campaigns.parquet` — mould-set × press × `[t_start, t_end]` |
| **L6** | building feasibility gate | L5 campaigns, L2 eligibility, B16 TT/TL split | `build_gate.parquet` — which campaigns building can actually feed |
| **L7** | **pull release of building** | L5/L6, partition, opening GT, cycle times | `build_schedule.parquet`, `gt_events.parquet`, `cure_campaigns_reconciled.parquet`, `carry_forward_gt.parquet` |
| **L8** | prep / mixing back-explosion | build schedule, BOM, ageing spec | component and compound requirements with their ageing windows |
| **L9** | coupled optimisation (SA/Tabu/LNS over cure campaigns) | L5 output | *skipped in the shipped arm* — it overwrites L5's own output |
| **L10** | time discretisation & sequencing | L7 output | shift-grid build and cure rows, mould changes |
| **L11** | plan validation — 40 invariants | everything above | `l11_invariants.parquet` + `l11_provenance.json` (freshness fingerprint) |
| **L12** | explainability, KPIs, gap-to-ceiling | the run directory | per-decision rationale |

**L7 is where the real work happens** (~4,000 lines). It orders GTs by *scarcity*
(fewest eligible machines first), assigns one machine per GT for the whole
horizon, releases slices backwards from each cure seat, enforces the B12 lot floor
at the point the run is *created* rather than only where it is divided, honours
the GT WIP rail and R5 per tyre, and runs a compact-and-insert "make-room" repair
pass when a release will not fit.

---

## 5. Shipped flags

Every knob is an environment variable. **All of these ship at the value shown**,
which is the measured-best setting — running with no flags at all reproduces the
packs in `output/`, except that a run using a non-default opening-GT file must set
`PLANNER_OPENING_GT`.

### The ones you will actually set

| flag | default | meaning |
|---|---|---|
| `PLANNER_OPENING_GT` | *(unset)* | opening-stock file; bare name resolves inside `masters/opening_gt/`, absolute path taken as given. Unset = the MES-derived `opening_gt_<M>.parquet` |
| `PLANNER_HORIZON_MODE` | `extend` | `extend` plans month + tail and reports the month; `truncate` is the closed-box month |
| `PLANNER_HORIZON_TAIL_H` | `72` | length of that tail in hours |
| `PLANNER_PLANT_CT` | `1` | use the plant's own cycle-time workbooks (per-machine build, per-GT cure) instead of mined times |
| `PLANNER_STRICT_LOT_FLOOR` | `1` | B12 lot floor is hard: `_place` refuses a run below the floor on every machine |
| `PLANNER_ALLOW_STALE_PARTITION` | `0` | `1` downgrades the partition month gate to a warning. Measured worse — do not |

### Shape knobs (algorithm parameters, each carries its measurement in `l7_pull_release.py`)

| flag | default | meaning |
|---|---|---|
| `PLANNER_L5_TAKT` | `flat` | L5 campaign-start governor; `off` disables |
| `PLANNER_L5_ALPHA` | `1.0` | governor strength |
| `PLANNER_L5_TAKT_PLANTS` | `TBR` | plants the governor applies to. Measured −2.79/−2.17 on PCR; rejected there |
| `PLANNER_L5_TAKT_PART` | `1` | sub-partition the takt by TT/TL (TBR) and rim lock (PCR) |
| `PLANNER_L5_FLOOR_BASIS` | `star` | campaign floor from τ\* rather than a mined median |
| `PLANNER_EARLY_STOCK` | `1` | let L5 draw opening stock for an early cure |
| `PLANNER_SLIVER_PCR` | `1.0` | anti-sliver packing: never leave a hole no legal run could fill. +0.14 pt on PCR |
| `PLANNER_SLIVER_TBR` | `0` | **off on TBR** — anti-synergistic with make-room; on costs TBR 0.54/0.37 pt |
| `PLANNER_L7_MAKEROOM` | `1` | compact-and-insert repair when a release does not fit. Dominant lever on both plants |
| `PLANNER_L7_MR_POINTS` | `1` | insertion points make-room tries |
| `PLANNER_CLUSTER_SEQ` | `1` | order releases by construction cluster inside a bucket |
| `PLANNER_CLUSTER_SEQ_H` | `4` | that bucket, in hours |
| `PLANNER_CLUSTER_SEQ_KEY` | `rc` | `(rim, cluster)`. Adding machine to the key empties the buckets and the term cannot fire |
| `PLANNER_HARD_LOCK` | `1` | PCR rim confinement — machines run their own rim |
| `PLANNER_HARD_PIN` | `1` | one machine per GT for the horizon |
| `PLANNER_PARTITION_PLANTS` | `PCR` | TBR is deliberately not partitioned |
| `PLANNER_RIM_SPILL` | `1` | targeted overflow to the plant's designated flex machine (TBMPCR2) |
| `PLANNER_SPILL_MULT` | `1.0` | size of that spill budget |
| `PLANNER_LOT_INTERVAL_H` | `16` | release grid T. Does **not** move run size — p50 stays ~288 at T = 16/20/24 |
| `PLANNER_SLICE_MULT_PCR` / `_TBR` | `0` / `3.0` | slice granularity per plant |
| `PLANNER_MACH_UTIL_CAP` | `0.95` | share of horizon hours a machine may be booked to |
| `PLANNER_R5_SAFETY_H` | `6.0` | margin held back inside the 72 h shelf life |
| `PLANNER_TAU_RELEASE` | `min` | which τ the release offset uses |
| `PLANNER_HARD_FLOOR` | `budget` | forced on by `STRICT_LOT_FLOOR=1` |
| `PLANNER_SUBFLOOR_PCR` / `_TBR` | `180` / `400` | sub-floor budget, unreachable while `STRICT_LOT_FLOOR=1` |
| `PLANNER_CARRY_OUT` | `1` | campaigns may finish past month end and are reported separately |
| `PLANNER_LOOKAHEAD_DAYS` | `0` | next-month demand lookahead — **not yet usable**, see §9 |

### Caps — these are **not** flags

Every enforced limit lives in one marked block in `planner/config.py`
(*"add a cap in `config.py` or nowhere"*). `l7` and `l11` both read it; neither
keeps a copy.

| cap | value | rule |
|---|---|---|
| build lot floor | PCR 150 / TBR 70 | B12 |
| minimum demand to plan | PCR 300 / TBR 150 | B12 residual |
| GT WIP rail (enforced) | PCR 4,800 / TBR 1,400 | G8 |
| rail margin | 0.94 | reconciliation headroom |
| GT shelf life | **72 h, not env-overridable** | R5 |
| changeover minutes | from `v_changeover_build`, never hardcoded | G4 |
| per-machine cadence | from `cycle_time_building.parquet` | B8 |

---

## 6. Input files, and where each one comes from

| file | source | used for |
|---|---|---|
| `August_Demand_PCR_TBR_Classification.xlsx` | plant order book, sheet `August Demand Classified` | the month's demand. 291 rows, 533,641 tyres |
| `gtinvaug/gt_inventory_manual_*_20260801.xlsx` | **manual plant floor count**, 07:00 on 2026-08-01 | opening GT stock. `ItemCode` + `TotalQuantity` only — no ages |
| `cycletime/{PCR,TBR} BUILDING/CURING CYCLE TIME.xlsx` | plant industrial engineering | per-machine build cadence, per-GT cure time (`PLANNER_PLANT_CT=1`) |
| `SKU_Construction_Clusters_{PCR,TBR} (1).xlsx` | plant | construction clusters → `INPUT/derived/sku_con_cluster.parquet` |
| `Recipemaster 1.xlsx` | plant MES master | `SAPMaterialCode` = finished SKU, `iD` = curing `recipeID`. **The SKU↔GT bridge** |
| `wcmaster 1.xlsx` | plant work-centre master | press number lookup (`iD` = MES `wcID`, 175/175) |
| `ALL PCR CTP SKUS.xlsx`, `TBR BUILDING ALLOWABLE MATRIX.xlsx` | plant | SKU rosters and TBR machine eligibility |
| `CTP Set up building ,curing and inspection (1) 2.xlsx` | plant | PCR inch capability, mould-change minutes |
| `Master_Building_ChangeoverTime_{pcr,tbr}.csv` | plant | changeover minutes per machine type |
| `curing_item_mould_mapping 2.csv`, `mould_inv_ctp_17072026.csv` | plant | mould→GT→press mapping and physical mould inventory |
| `Ageing spec-20.01.2024 (2).pdf` | plant quality | semi-finished component ageing windows (L8) |
| `masters/press_list_<M>.json` | plant | the presses actually available that month |
| `btpformat/optimizer_*.xlsx` | plant's own optimizer | **format reference only** — the target workbook shape |

### The GT namespace trap — read before touching any mapping

The plant writes green-tyre codes in at least four shapes and the engine plans in
exactly one (`v_build.itemCode`, the MES stage-2 item code):

```
engine (MES itemCode)           plant workbooks
GT 1402 XPC TATA                1402 XPC TATA          (no "GT ")
GT  T1457 STAR   (two spaces)   T1457 STAR
GT1564 NEO       (no space)     1564 NEO
GT 2568 HT2                     2568 RAN HT2           (extra brand token)
10.00 R 20 JDE                  10.00 R 20 JDE         (TBR, size-led)
GT 5055 - 295/80R22.5 JUC XM    GT 5055                (TBR, BOM short code)
```

**The TBR BOM keys on `GT 5001` while TBR MES `itemCode` is size-led. The two
namespaces have zero string overlap.** A workbook column called "Matched GT Code"
reading `GT 5001` is therefore not a planning key. This has cost the project real
debugging twice.

`scripts/gt_namespace.py` is the single bridge. It resolves **SKU → GT through the
curing-recipe chain** (`Recipemaster.SAPMaterialCode → v_curing.recipeID →
v_build.itemCode`, documented at 100 % of cured volume on both plants) and falls
back to a three-tier, uniqueness-gated string match only for SKUs never yet
produced. Ambiguity returns `None`; nothing is ever guessed.

---

## 7. Output packs — what each sheet is

Two packs per month, both in `schedule/send/output/<MONTH>_pack/`.

### A. The verification pack — `schedule_<month>.xlsx` (+ the same sheets as CSVs)

Built by `scripts/export_shift_schedule.py`. The plant day runs **07:00 → 07:00**
and is split into shifts A (07–15), B (15–23), C (23–07). `date` is the *plant-day*
date, not the wall-clock date of the timestamp — labelling the C shift by wall
clock once mislabelled 28.7 % of build rows and made the night shift look empty.

| sheet | contents |
|---|---|
| `0_settings` | every setting that shaped this run, what it acts on, which file it lives in, and what it does |
| `1_build_schedule_shift` | the building schedule: machine × plant-day × shift × GT × qty |
| `1b_build_runs` | maximal consecutive same-GT blocks per machine — the object the lot floor applies to |
| `2_cure_schedule_shift` | the curing schedule: press × plant-day × shift × GT × qty |
| `2b_cure_campaigns` | one row per cure campaign with its mould set, press, window and nameplate |
| `3_mould_changes` | every mould change, press and duration |
| `4_crew_load` | manning implied by the plan |
| `5_machine_summary` | per building machine: hours, tyres, changeovers, occupancy |
| `6_press_summary` | per press: hours, tyres, mould changes, occupancy |
| `7_daily_summary` | per plant-day: built, cured, GT inventory |
| `8_demand_vs_plan` | per GT: demanded, gross build, fed, shortfall, **and the named reason** |
| `9a_kpi_summary` | headline KPIs with the A + B − C = D reconciliation spelled out |
| `9b_l11_invariants` | all 40 invariants, actual vs target, pass/fail |
| `11_changeover_by_machine` | changeover count and weighted minutes per machine |
| `12_lot_size_violations` | sub-floor runs — emitted with an explicit zero line when there are none |

Quantities are never smoothed or rounded to look tidy. If a run is 11 tyres it is
exported as 11 tyres.

### B. The BTP pack — `btp/optimizer_{building,curing}_schedule_full_<date>_<PLANT>.xlsx`

Built by `scripts/export_btp_format.py`, matching the plant's own optimizer
workbooks sheet for sheet and column for column.

Building workbook: `Shift Schedule` · `Changeover Plan` · `SKU Classification` ·
`Daily GT & Carcass` · `Demand Fulfillment (B2C)` · `Machine Utilization`.
Curing workbook: the equivalent press-side sheets.

Two things it does deliberately:

- **GT → SKU split.** The engine plans GTs; the BTP format reports SKUs. A press
  holds one mould set and a machine holds one drum setup, so a machine-shift is
  *one* SKU: the share decides how many **rows** each SKU gets, never how a row is
  divided. Splitting inside rows produced 43 % zero-quantity PCR rows. For a
  forward month the split comes from the order book itself
  (`build_gt_sku_share.py --from-demand`); for a historical month from the cured
  recipe chain.
- **Machine codes are ours, not the reference pack's.** See §9.

### C. Independent verification

`scripts/verify_export.py` reads **only the exported CSVs** and re-derives every
check from scratch — it imports nothing from `planner/`, because a verifier that
calls planner internals only proves the planner agrees with itself. Severities are
HARD (physically impossible), SOFT (breaks a business rule but is executable) and
EXPORT (the file misrepresents the plan).

**Both shipped packs verify at 0 HARD / 0 SOFT / 0 EXPORT.**

---

## 8. Current KPIs

Both months, both plants, shipped defaults, partitions rebuilt per month, arms
built fresh from L5 by `run_arm.py`.

| | **July 2026** PCR | **July 2026** TBR | **August 2026** PCR | **August 2026** TBR |
|---|---|---|---|---|
| plannable demand (tyres) | 393,639 | 97,991 | 423,796 | 99,539 |
| **fulfilment (cured in-month)** | **96.6 %** | **94.5 %** | **90.6 %** | **93.7 %** |
| … including carry-out tail | 98.3 % | 96.4 % | 95.9 % | 97.3 % |
| tyres cured in-month | 380,163 | 92,585 | 384,123 | 93,227 |
| carry-out tail (cured next month) | 6,762 | 1,918 | 22,171 | 3,658 |
| opening stock consumed | 4,352 | 1,010 | 4,080 | 1,015 |
| **sub-floor runs** (plant: 12.7 % / 30.8 %) | **0.0 %** | **0.0 %** | **0.0 %** | **0.0 %** |
| lot p50 (tyres/run) | 317 | 93 | 308 | 103 |
| **R5 GT wait max** (limit 72 h) | 68.8 h | 71.8 h | 56.5 h | 67.2 h |
| GT wait p95 | 24.6 h | 31.7 h | 24.4 h | 32.8 h |
| **GT inventory, time-weighted mean** | 4,096 | 1,236 | 4,223 | 1,184 |
| **GT inventory, daily max vs rail** | 4,631 / 4,800 | 1,340 / 1,400 | 4,623 / 4,800 | 1,340 / 1,400 |
| weighted setup hours | 371.3 h | 134.8 h | 498.4 h | 102.0 h |
| changeovers / machine-day (plant 2.66 / 3.56) | 2.40 | 2.84 | 2.62 | 1.90 |
| same-size share, as L11 reports it | 91.4 % | 100.0 % | 64.7 % | 91.9 % |
| same-size among known-rim pairs | 89.0 % | 98.5 % | **85.5 %** | **97.9 %** |
| machine occupancy | 77.7 % | 77.0 % | 79.5 % | 78.3 % |
| realised n_g | 3.01 | 2.45 | 2.72 | 3.20 |
| L11 invariants passed | 27 / 40 | | 23 / 40 | |
| verifier | 0 HARD / 0 SOFT / 0 EXPORT | | 0 HARD / 0 SOFT / 0 EXPORT | |

Read the two same-size rows together. **August PCR's 64.7 % is a denominator
artefact, not a scheduling regression.** 28 August GTs have no rim in `gt_size`,
so 29 % of PCR build transitions involve a GT whose rim is unknown; among the
71 % where both sides have a known rim the share is 85.5 %. July has 100 % rim
coverage, which is the whole difference. Fix the master, not the scheduler.

### Why August is lower than July

August demand is 6.4 % larger than July's and, crucially, it is a real order book
rather than a production-derived proxy. **L3 puts August PCR at 98.5 % of the
build ceiling** — the month is nearly full before any sequencing decision is made.

The PCR gap of 39,673 tyres decomposes as:

| cause | tyres | note |
|---|---|---|
| carry-out tail | 22,171 | campaigns that start in August and finish in the first days of September. Real output, past the month boundary |
| no feasible release | 15,436 | the genuine capacity/timing residue |
| would breach min_lot | 8,854 | volume too small for an economic run at the times it is wanted |

(The three overlap by construction — a GT can be both late and small.) TBR is a
much easier month at 73.8 % of ceiling and lands at 93.7 %.

---

## 9. Known gaps and open decisions

State these to the plant rather than working around them.

### 9.1 The ~4 h carry-in question — **needs a plant ruling**

Two plant rulings conflict and the conflict is unresolved:

1. **Closed-box month** — only demand cured inside the plant month counts.
2. **Everything available at t0** — no ramp, full rate from hour 0.

46–52 % of remaining unfed volume is *cold*: cure campaigns sitting at t0 that
need GT which could only have been built **before** the month. The deficit is
small — 2.3–3.3 h median, 4.2 h worst case. Ruling 2 implies a few hours of
pre-month build; ruling 1 forbids it. Closing it is worth roughly **+1.0–1.8 pt
per plant**. Do not resolve it unilaterally; it is a policy call. Either the
closed box flexes by ~4 h at the start, or this volume is permanently unreachable.

### 9.2 Master-data gaps

- **`gt_size` rim coverage.** 28 of August's GTs have no rim. They cannot be
  rim-locked or partitioned, they fall back to L7's dynamic assignment, they
  scatter across machines, and they are the direct cause of August PCR's higher
  weighted setup (498 h vs July's 371 h) and its contaminated same-size figure.
  **This is the single highest-value master-data fix available.**
- **Unplannable demand.** 5,476 tyres of the August order book (1.0 %) never reach
  the plan: 4,304 tyres across 14 PCR GTs and 28 tyres across 2 TBR GTs that have
  no MES production history — and therefore no capability row, no cycle time and
  no mould — plus 1,172 tyres of SKUs in neither plant master. Listed in
  `masters/demand/UNMAPPED_2026-08.csv`.
- **B12 residual.** 19 GTs / 2,734 tyres fall below `min_demand_units`
  (PCR 300 / TBR 150) and are routed to the residual policy rather than planned.
  They are flagged, never silently dropped.
- **Opening-GT ages are not supplied.** The manual count gives quantities only, so
  `--age-h` writes one assumed age (default 24 h, leaving 48 h of R5). Check
  whether it binds by re-running at `--age-h 48` before arguing about the value.

### 9.3 Unresolved BTP machine codes

The reference BTP pack uses building codes 6001–6004 / 6802 / 7001–7004 /
8501–8502 and curing codes 14801–148xx / 9503 / 9701–9704 / 24824 / 4406 / 4923.
Checked against this plant's own `wcmaster 1.xlsx` (294 rows):

- 10 of the 38 BTP **building** codes exist in wcmaster — and every one is a
  *different kind of equipment*. 6001–6004 are "PCR INSPECTION STATION 1–4";
  7001–7004 are "INSPECTION STATION 1–4". In the BTP pack 6001 is a Unistage
  building machine. That is a direct contradiction, not a missing mapping.
- **0** of the BTP **curing** codes appear in wcmaster at all.
- Only 12 of the reference pack's 91 SKUs match ours on a 16-character prefix, and
  its month totals 689,563 units against our 398,405. It is a different plant or a
  different master generation.

So the `Machine` column carries **our** identifiers. Curing presses *are* resolved
(86/86 PCR, 79/79 TBR, via `wcmaster.iD` = MES `wcID`); the 11 PCR and 9 TBR
building machines are not, because wcmaster has no `TBM<plant><n>Stage2` row.
`btp/machine_code_map_UNRESOLVED.csv` lists both code spaces side by side. When
the plant supplies the real bridge, only one dict in the exporter changes. A
guessed crosswalk would put a building machine's output on an inspection station.

### 9.4 Deliberate non-goals — do not "fix" these

- **Daily build quota (B7): rejected.** Interior CV is already 0.046 (PCR) and
  0.059 (TBR — better than the plant's 0.097). The headline variability is days
  1/30/31 only.
- **GT inventory below the G8 band: intentional.** We sit under the plant.
- **Curing `cycleStart` is press-open, i.e. the cycle END.** The source data is
  named backwards. `duration = cycleStart − event_ts`. Do not "correct" it.
- **Moulds are per `(plant, gt_code, press)`.** Forcing one primary mould per GT
  produced 416 K phantom double-booking violations.
- **Rolling horizon / next-month lookahead is built but unusable.**
  `--lookahead-days` degrades to a clean no-op because `masters/demand/` ends
  where MES ends. It is worth ~0.64 pt plus relief of day-30/31 contention and is
  the most valuable unbuilt lever.
- **`planner/plan/`, `planner/cli.py` and the `Makefile` are the RETIRED
  generation-1 engine.** They are kept because the ingest and warehouse layers are
  shared. `CLAUDE.md` documents *that* engine as the architecture and never
  mentions `planner/cmbc/`; three separate sessions have reasoned about the wrong
  codebase as a result. **The live engine is `planner/cmbc/`.**

---

## 10. The defect ledger — read this before changing anything

This project keeps its mistakes written down, with the measurement that found each
one. The documents are not optional reading; the code assumes you know them.

| document | what it is |
|---|---|
| **`schedule/send/PARTITION_AND_CHANGEOVER.md`** | the defect ledger. §1 measurement errors · §2 the machine partition · §4b–4t measured-and-rejected experiments · §6 a 36-item do-not-repeat list · §8 the single source of truth for every cap. **Read this first, every time.** |
| **`schedule/send/MEMORY.md`** | engineering log, data lineage, §10d measurement ledger |
| **`schedule/send/BUSINESS_RULES.md`** | the 46 numbered plant rules (B/P/C/S/G/E) with per-rule implementation status |
| **`schedule/send/EXPERT_AUDIT.md`** | independent audit; corrects four documented-but-wrong claims and names the four failure modes that produced them |
| **`schedule/send/ENGINE_FLOW.md`** | narrative walkthrough of L0–L12 |

### The five lessons that cost the most

1. **A mined statistic is not a constraint.** `tau*` and `min_lot` were both plant
   *medians* wired in as hard floors; together they cost 13.4 points of fulfilment.
   The tell is a flat quantile band — identical p01/p05/p10 means you built a wall,
   not a distribution.
2. **A passing check is not a correct check.** The cure-changeover gate passed at
   0.38 against a 1.43 ceiling for the whole project while dividing plant *event*
   rows by our *campaign* rows — a 10× denominator error hiding a metric where we
   actually beat the plant 2:1. Four separate denominator defects have been found.
3. **Mean over events is not mean over time.** Inventory is a stock held over time.
   Event-weighting biases it upward by 5.7 % on TBR and made a rail look breached
   on days it was not.
4. **Never A/B against an existing run directory.** `RunContext` hashes config but
   not `PLANNER_*` env flags. Use `scripts/run_arm.py`, which builds fresh from L5,
   and gate on `scripts/check_arm_fresh.py`.
5. **Re-measure a rejected experiment when its baseline has moved.** Two
   experiments rejected on the 98.9 %-era engine were both worth points once
   re-run.

### Reporting discipline

Always report **PCR and TBR separately** — a total that moved 1.85 pt once hid an
8.67 pt TBR regression. Always in this order: fulfilment · GT inventory
(time-weighted mean *and* daily-mean max vs rail) · weighted changeover hours ·
same-size share · sub-floor run share against the plant's · lot p50 · R5 max. If a
change trades one KPI for another, give both numbers in the same sentence. Never
report a gain without its price.
