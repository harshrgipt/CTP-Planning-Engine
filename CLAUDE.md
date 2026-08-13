# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## READ THIS FIRST — there are three engines in `planner/`, and only one is live

| tree | status | driven by |
|---|---|---|
| **`planner/cmbc/`** | **THE LIVE ENGINE.** | **`main.py`** (the orchestrator), `scripts/run_arm.py` for A/B arms |
| `planner/engine/` | superseded curing-first prototype (P0–P9), logged in [ENGINE_LOG.md](schedule/send/ENGINE_LOG.md) | `scripts/plan.py` |
| `planner/plan/`, `learn/`, `kb/`, `optimize/`, `replay/`, `simulate/` + `planner/cli.py` + `Makefile` | **RETIRED generation-1.** Kept only because `planner/data/` (ingest + warehouse) and `planner/config.py` are shared. | `make *`, `python -m planner.cli *` |

The previous version of this file documented generation-1 as *the* architecture and never mentioned `planner/cmbc/`. Per [README.md](README.md) §9.4, **three separate sessions reasoned about the wrong codebase as a result.** If you are about to edit `plan/building.py`, `plan/curing.py`, `plan/sync.py`, `learn/`, or `kb/promoter.py` for a scheduling change, you are in the retired tree — the equivalent live code is in `planner/cmbc/`.

[README.md](README.md) (wrapper root, ~560 lines) is the current, authoritative onboarding document. Read it before anything non-trivial; this file is the operating summary, not a replacement.

## Repository layout

```
ctp-planner/                  <- WRAPPER ROOT (git root). Docs only.
├── README.md  VERSION  CLAUDE.md
├── INPUT/                    <- EVERY input file lives here. One folder.
│   ├── raw/                  18 plant workbooks / csv / pdf, verbatim
│   ├── derived/              29 frozen mined masters (parquet)
│   ├── cycletime/            plant build + cure cycle-time workbooks
│   ├── opening_gt_manual/    manual GT floor counts
│   ├── btpformat/            target workbook shape, reference only
│   ├── demand/  opening_gt/  archived by month
│   └── MANIFEST.csv
└── schedule/send/            <- ENGINE ROOT. Every command runs from here.
    ├── planner/{cmbc,paths.py,config.py,data,engine,plan,learn,kb,validate,...}
    ├── scripts/  masters/  warehouse/  output/  runs/  tests/
    └── MEMORY.md  PARTITION_AND_CHANGEOVER.md  BUSINESS_RULES.md  EXPERT_AUDIT.md
```

**All input lookups go through [planner/paths.py](schedule/send/planner/paths.py).** Do not hand-write `ROOT.parent.parent / ...` again — that idiom is what made the nesting load-bearing in a way no new reader expects. `PLANNER_DATA_ROOT` relocates the whole `INPUT/` tree (for frontend deployments that mount data elsewhere); unset, it is the wrapper root.

⚠️ **`input_derived()` and `wh_derived()` are separate functions with no cross-fallback, deliberately.** 21 filenames exist in both `INPUT/derived/` and `warehouse/derived/`; twenty are byte-identical and **`press_mould_change.parquet` is not**. L5 and L10 read the warehouse copy. Merging the two resolvers would silently change the cure schedule. Locked by [tests/unit/test_paths.py](schedule/send/tests/unit/test_paths.py).

## Environment (Windows)

`Makefile` (`SHELL := /bin/bash`) and `scripts/bootstrap.sh` are POSIX-only, `make` is usually unavailable, and the Makefile only drives the retired engine anyway. `.venv/` is not present in a fresh checkout.

```powershell
cd schedule\send
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest tests\ -q
```

Interpreter is `.venv\Scripts\python.exe` (Git Bash: `./.venv/Scripts/python.exe`; Linux/macOS: `./.venv/bin/python`). Set `PYTHONPATH=.` from `schedule/send` before invoking modules.

## Commands

### Plan a month — one command

[main.py](schedule/send/main.py) is the orchestrator: inputs → checks → schedule → BTP pack. ~30 s for August 2026.

```bash
cd schedule/send
./.venv/Scripts/python.exe main.py plan --month 2026-08 \
      --run aug_ship --out output/AUG2026_pack \
      --opening-gt opening_gt_manual_2026-08.parquet
```

Steps: `00_preflight · 01_l4 · 02_l45 · 03_partition · 04_l5 · 05_l7 · 06_l10 · 07_l11 · 08_share · 09_btp`. Each step's stdout is written to `runs/<name>/log_<step>.txt` — the partition staleness line and rim-spill report are printed, not persisted, so that log is the only evidence of which partition the run used.

Other subcommands: `status` (what's on disk for a month), `check` (preflight only), `optimize` (plan with L9 wired in), `export` / `verify` (re-export or verify an existing run), `masters` (ingest the month's demand + opening GT), `rebuild` (the offline miners — needs raw MES), `test`.

Trailing `KEY=VALUE` pairs on any subcommand become `PLANNER_*` overrides, so an A/B arm is `main.py plan --run mr0 PLANNER_L7_MAKEROOM=0`. They are applied to `os.environ` as well as the child env, because `status` and the partition-stamp check run in-process.

**Set the opening-GT file once, on the driver.** Both the planner and the exporters read `PLANNER_OPENING_GT`; exporting with a different value than the arm was built with silently changes only the `GT_Inventory` column of the BTP fulfilment sheets while every other figure stays identical — a diff that reads like a scheduling change and is not.

Month inputs (run once, when the plant sends new files):

```bash
$PY -m scripts.ingest_orderbook_demand --xlsx "../../INPUT/raw/August_Demand_PCR_TBR_Classification.xlsx" \
      --sheet "August Demand Classified" --month 2026-08
$PY -m scripts.ingest_manual_opening_gt --month 2026-08 --age-h 24 \
      --pcr "../../INPUT/opening_gt_manual/gt_inventory_manual_pcr_20260801.xlsx" \
      --tbr "../../INPUT/opening_gt_manual/gt_inventory_manual_tbr_20260801.xlsx"
```

The full shift pack (13 sheets) and the independent verifier are separate — the BTP pack does not produce the CSVs `verify_export.py` reads:

```bash
$PY scripts/export_shift_schedule.py aug_ship 2026-08 output/AUG2026_pack
$PY scripts/verify_export.py output/AUG2026_pack 2026-08
```

CLI-arg quirk: L5 names its destination `--out`; every later layer calls it `--run`. Both drivers handle this.

### Tests

```bash
./.venv/Scripts/python.exe -m pytest tests/                            # all, ~2 s
./.venv/Scripts/python.exe -m pytest tests/unit/test_ledger.py         # one file
./.venv/Scripts/python.exe -m pytest tests/unit/test_ledger.py::test_ledger_balance   # one test
```

`pyproject.toml`: `testpaths = ["tests"]`, `addopts = "-q --strict-markers"`. 15 tests. Four of the five files exercise generation-1 (`planner.plan.ledger`, `kb.promoter`, optimizer neighbourhood, fuzz verifier); `test_paths.py` is the only one covering the live path. They are not a regression suite for `planner/cmbc/` — the real gates are `scripts/verify_export.py` (independent, reads only exported CSVs) and L11's 40 invariants.

Lint: `ruff`, line-length 110, target py311 (not wired into a make target).

### What a clone can re-run

Raw MES CSVs (~4.4 GB) are gitignored; everything mined from them is committed, so **a prepared month replans end-to-end out of the box** — verified 2026-08-11: a fresh `.venv` reproduces the August BTP pack on all 34 sheets.

⚠️ **The partition rebuild is the exception, and README §3 overstates this.** `scripts/build_gt_machine_partition.py` mines `v_build`, so it needs the raw MES like L0/L1/L2 do. `main.py` therefore rebuilds it **only when the file on disk is not already stamped for the target month**, and preflight raises a blocking ERROR if the stamp is wrong — so skipping the rebuild can never mean silently using a stale partition. Planning a month whose partition was never built still requires the MES drop at `curing/`, `o_production/`, `io_production_consumption/`.

## Architecture — the L0→L12 flow

Phase A (L0–L4.5) decides *what and how much*; Phase B (L5–L12) decides *where and when*. **Curing proposes, building disposes** — the plant is curing-first, so a cure campaign is placed first and building is released backwards from it at `t_cure − τ* − build_duration`. Green tyres are perishable (72 h, rule R5), so GT inventory is a **synchronization buffer, not a production target**.

`planner/cmbc/` holds only what runs. Everything else moved into two clearly-named subpackages.

| layer | does | key output |
|---|---|---|
| **preflight** | MES-free input gate: allowable machine, PCR inch, TT/TL, press, mould, cycle time, opening GT, partition stamp | `preflight_<M>.parquet`; exit 1 on ERROR |
| **L4** | net cure requirement | `net_requirement_<M>` |
| **L4.5** | lot sizing — the **cure** lot; build slices have no minimum | `l45_lots_<M>` |
| **partition** | static GT→machine map | `INPUT/derived/gt_machine_partition.parquet` (**month-stamped**) |
| **L5** | cure campaign master — **the plan is born here** | `cure_campaigns.parquet` |
| **L7** | **pull release of building** (~4,000 lines — the real work) | `build_schedule.parquet`, `gt_events.parquet`, `carry_forward_gt.parquet` |
| **L10** | time discretisation & sequencing | shift-grid rows, mould changes |
| **L11** | plan validation — 40 invariants | `l11_invariants.parquet`, `l11_provenance.json` |

Kept in `planner/cmbc/` as modules but **not pipeline steps**: `l25_cie.py` (L4.5 imports `mould_sets` from it — a code dependency even though its artefacts are read by nobody), `plant_ct.py`, `l5_ng.py`.

`planner/cmbc/_offline/` — **rebuild-only, needs the raw MES drop** (`main.py rebuild`). These mine the frozen inputs; their outputs are committed, so the pipeline runs without them.

| L0 `l0_learn` | τ\*, yields, availability → `warehouse/params/` |
| L2 `l2_capability` | `cap_machine/_press/_mould/_ttl_groups_<M>` |
| L3 `l3_ceiling` | `l3_cavities.parquet` (read by L5) + the RCCP report |

`planner/cmbc/_retired/` — **nothing reads their output.** Verified: dropping them leaves every plan artefact bit-identical and the BTP pack matching on all 34 sheets.

| L6 `l6_build_gate` | L7 never read `l6_infeasible`/`l6_build_load`; it runs its own B16 gate |
| L8 `l8_prep_explosion` | zero readers except `scripts/export_cmbc_xlsx.py`, not the BTP path |
| **L9 `l9_optimise`** | **removed after measurement — see below** |
| L1 `l1_validate` | its three outputs had one consumer between them (L9 read `cost_table`); with L9 gone, none. Also needs raw MES. Superseded by `l1_preflight` |
| L12 `l12_explain` | never wired into an arm |
| `_diag_*` (5) | diagnostics |

### Why L9 was removed — measured, not assumed

Before retiring it, L9 was wired **correctly** (L5 → **L9** → L7 → L10 → L11 — it rewrites `cure_campaigns.parquet` in place, so every campaign consumer must run *after* it; the old `STAGES` list would have run it last, leaving a directory whose schedule and campaigns describe different plans) and run on 2026-08 with a 300 s budget:

> `searched 1,428 candidates, accepted 0 moves in 300 s`
> **NO IMPROVEMENT FOUND. Plan left unchanged.**

All nine cost tiers ended exactly where they started. `cure_campaigns.parquet` came out byte-identical to L5's own output, **0 of 40 L11 invariants moved**, and built volume was unchanged on both plants (PCR 406,294 / TBR 96,885). The search is **lexicographic** — a move that worsens a higher tier is rejected however much it gains below — and tier 1 (`demand short` = 7,409) blocks everything beneath it.

The measurement is recorded in `planner/cmbc/_retired/__init__.py`. **Re-measure there if the baseline moves** — ledger lesson 5 is that two experiments rejected on the 98.9 %-era engine were both worth points once re-run. Do not re-enable it on the assumption that an optimiser must help.

Read [l7_pull_release.py](schedule/send/planner/cmbc/l7_pull_release.py)'s module docstring before touching it — it names the three campaign levels (cure campaign → build run → build slice), and where the lot floor applies to each. Narrative walkthrough: [ENGINE_FLOW.md](schedule/send/ENGINE_FLOW.md).

**Every layer communicates only through files on disk**, never in-process state. That is what makes a single layer re-runnable and every output number traceable to a parquet.

### Two flag mechanisms — they behave differently

- **`planner/config.py`** is pydantic-settings: `PLANNER_` (paths, seed, log_level), `PLANNER_TH_` (thresholds), `PLANNER_W_` (objective weights). `RunContext` hashes this config into the run_id.
- **The cmbc shape flags** (`PLANNER_HORIZON_MODE`, `PLANNER_L7_MAKEROOM`, `PLANNER_SLIVER_*`, `PLANNER_L5_TAKT*`, …) are raw `os.environ.get` reads inside the layer modules. **`RunContext` does not hash them.** This is exactly why A/B-ing against an existing run directory is forbidden (below). Full table with per-flag measurements: [README.md](README.md) §5.

**Caps are not flags.** Every enforced limit lives in one marked block in [config.py](schedule/send/planner/config.py) — *"add a cap in `config.py` or nowhere."* L7 and L11 both read it; neither keeps a copy (a duplicated rail value was a real bug: L7 enforced 1,400 while L11 graded against 1,500). Build lot floor PCR 150 / TBR 70 (B12); GT WIP rail PCR 4,800 / TBR 1,400 (G8); `GT_SHELF_LIFE_H = 72.0` is **not env-overridable** — import it rather than re-declaring `72.0`.

## Process rules that exist because of measured defects

- **Rebuild the partition every month.** It is sized against one month's demand and calendar hours. L7 **refuses to plan** on a partition stamped with a different month. A stale one cost July 0.58 pt of fulfilment and 10.3 pt of same-size while every gate passed. `PLANNER_ALLOW_STALE_PARTITION=1` downgrades the gate to a warning — measured worse, do not.
- **Never seed an arm with `cp -r`, never A/B against an existing run directory.** Use `scripts/run_arm.py` (deletes and rebuilds from L5) and gate with `scripts/check_arm_fresh.py`. 15 run directories once carried another arm's scorecard, and a flag worth 8,085 tyres read as free.
- **Always report PCR and TBR separately.** A plant-total that moved 1.85 pt once hid an 8.67 pt TBR regression. Order: fulfilment · GT inventory (time-weighted mean *and* daily-mean max vs rail) · weighted changeover hours · same-size share · sub-floor run share vs the plant's · lot p50 · R5 max. If a change trades one KPI for another, give both numbers in the same sentence.
- **A mined statistic is not a constraint.** `tau*` and `min_lot` were both plant *medians* wired in as hard floors; together they cost 13.4 points of fulfilment. The tell is a flat quantile band — identical p01/p05/p10 means you built a wall, not a distribution.
- **A passing check is not a correct check.** Four separate denominator defects have been found; one gate passed for the entire project while dividing plant *event* rows by our *campaign* rows.
- **Mean over events ≠ mean over time.** Inventory is a stock held over time; event-weighting biased TBR upward 5.7 % and made a rail look breached on days it was not.
- **The verifier must not import `planner/`.** `scripts/verify_export.py` and `planner/validate/violations.py` re-derive every check from the exported files. A verifier that calls planner internals only proves the planner agrees with itself.

## Data lineage (memorize before writing any query)

- Build **stage 2** `itemCode` is the **GT code** (e.g. `"GT 1402 XPC TATA"`), not the finished SKU. SKU→GT comes from the curing-recipe chain (`Recipemaster.SAPMaterialCode → v_curing.recipeID → v_build.itemCode`).
- **The GT namespace trap.** The plant writes GT codes in at least four shapes; the engine plans in exactly one (`v_build.itemCode`). The TBR BOM keys on `GT 5001` while TBR MES `itemCode` is size-led — **the two namespaces have zero string overlap**, so a workbook column named "Matched GT Code" is not a planning key. `scripts/gt_namespace.py` is the single bridge; ambiguity returns `None` and nothing is guessed. This has cost the project real debugging twice.
- Build stage 2 `productionID` is a per-tyre barcode (VARCHAR, leading zeros) and equals curing's `gtbarCode`. **Join `v_build.productionID = v_curing.gtbarCode`** — 99.6 % hit rate. Joining on `b.gt_code IS NOT NULL` is cartesian (a real bug).
- Curing `cycleStart` is **press-open, i.e. the cycle END**; `event_ts` is press-close. `duration = cycleStart − event_ts` ≈ 1955 s median. The source is named backwards — do not "fix" it.
- Curing `wcID` is the press id (int in source, cast to VARCHAR everywhere).
- **Moulds are per `(plant, gt_code, press)`**, labelled `<mould>@<press>`. Forcing one primary mould per GT produced 416 K phantom double-book violations.
- The plant day runs **07:00 → 07:00**, shifts A (07–15) / B (15–23) / C (23–07). `date` in exports is the *plant-day* date, not the wall-clock date — labelling C shift by wall clock once mislabelled 28.7 % of build rows.
- The TBR construction-mapping xlsx has physical sheet names differing from logical ones: real data is at logical `Sheet4`, `Sheet6`, `Sheet1`, and `Before`/`After`/`Sheet3`.
- **The three numbers people confuse:** *built* (produced this month) · *fed* (delivered into presses, includes opening stock) · *cured in-month* (the fulfilment numerator: `built + opening − closing`). Sheet `9a_kpi_summary` prints the A + B − C = D reconciliation.

## Performance constraints — do not regress

- **Never write a recursive DuckDB CTE over the BOM.** It OOMs at ~29.5 GB tempdir. Walk the graph once in Python with a visited-set and memoize per-GT leaf multipliers.
- **Never insert row-by-row into DuckDB for >1 K rows.** Build a Polars frame, `con.register(name, df.to_arrow())`, one bulk `INSERT … SELECT`, then `unregister`. 400 K per-row Pydantic inserts previously OOM'd.
- **Filter on the Hive partition column `date`, not `event_ts`,** for partition pruning (`WHERE date < ?::DATE`). Filtering `event_ts` scans everything and hangs on the 14.9 M-row PCR stage-2 consumption file.
- No pandas in the hot path — Polars `scan_csv` or DuckDB `read_csv_auto`.
- Treat `curing/`, `o_production/`, `io_production_consumption/`, `bom_pcr_tbr/`, `Sku construction mapping/` as **read-only**.

## The defect ledger — read before changing anything

This project writes its mistakes down with the measurement that found each one. The code assumes you know them.

| document | what it is |
|---|---|
| [PARTITION_AND_CHANGEOVER.md](schedule/send/PARTITION_AND_CHANGEOVER.md) | **the defect ledger — read first, every time.** §1 measurement errors · §2 the partition · §4b–4t measured-and-rejected experiments · §6 a 36-item do-not-repeat list · §8 single source of truth for every cap |
| [EXPERT_AUDIT.md](schedule/send/EXPERT_AUDIT.md) | independent audit; corrects four documented-but-wrong claims and names the four failure modes that produced them — **read before MEMORY.md** |
| [MEMORY.md](schedule/send/MEMORY.md) | engineering log, data lineage, §10d measurement ledger, §12 do-not list |
| [SESSION_LOG_2026-08-12.md](schedule/send/SESSION_LOG_2026-08-12.md) | **everything tested 2026-08-11/12 with the number that decided it** — 8 of 9 scheduler changes measured negative, every win was a data fix; read before proposing a scheduling change |
| [BUSINESS_RULES.md](schedule/send/BUSINESS_RULES.md) | the 46 numbered plant rules (B/P/C/S/G/E) with per-rule implementation status — check before changing behaviour, update the status column when you do |

## Deliberate non-goals — do not "fix" these

Locked-in technical constraints, several chosen after the alternative was tried and measured: classical statistics + pattern mining (no ML/RL/LLM) · heuristic greedy + SA/Tabu/LNS (no CP-SAT/MILP) · Polars + DuckDB + Parquet · Python 3.11, project-local `.venv` only.

Also deliberate, per [README.md](README.md) §9.4: daily build quota (B7) **rejected** — interior CV is already better than the plant's; GT inventory sitting below the G8 band is **intentional**; the backwards `cycleStart` naming; per-press moulds; rolling-horizon lookahead is built but degrades to a clean no-op because `masters/demand/` ends where MES ends.

Open questions that need a **plant ruling**, not a unilateral fix: the ~4 h carry-in question (§9.1, worth +1.0–1.8 pt/plant), `gt_size` rim coverage (28 August GTs have no rim — the single highest-value master-data fix), and the unresolved BTP building-machine codes (§9.3 — a guessed crosswalk would put a building machine's output on an inspection station).
