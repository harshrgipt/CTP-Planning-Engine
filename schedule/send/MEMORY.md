# MEMORY — Tyre-Plant Production Planning Engine

Dense session-state snapshot for anyone (human or agent) re-entering this project cold. Read once; then read the code.

> ## 2026-08-10 — AUGUST ON THE REAL ORDER BOOK + MANUAL GT COUNT
>
> **Reference runs: `runs/hz_ext72` (July) and `runs/aug_ship` (August).** Both
> built fresh by `scripts/run_arm.py` on shipped defaults, partition rebuilt per
> month. Packs at `output/JUL2026_pack` and `output/AUG2026_pack`, both
> 0 HARD / 0 SOFT / 0 EXPORT.
>
> | | Jul PCR | Jul TBR | Aug PCR | Aug TBR |
> |---|---|---|---|---|
> | fulfilment (cured in-month) | 96.6 % | 94.5 % | **90.6 %** | **93.7 %** |
> | incl. carry-out tail | 98.3 % | 96.4 % | 95.9 % | 97.3 % |
> | sub-floor runs | 0.0 % | 0.0 % | 0.0 % | 0.0 % |
> | R5 max (72 h) | 68.8 | 71.8 | 56.5 | 67.2 |
> | GT inv mean / daymax / rail | 4,096 / 4,631 / 4,800 | 1,236 / 1,340 / 1,400 | 4,223 / 4,623 / 4,800 | 1,184 / 1,340 / 1,400 |
> | weighted setup h | 371.3 | 134.8 | 498.4 | 102.0 |
> | L11 pass | 27/40 | | 23/40 | |
>
> **Five findings worth carrying forward.**
>
> 1. **August is a capacity month, not a scheduling failure.** L3 puts August PCR
>    at **98.5 % of the build ceiling** (July was comfortably below). Of the
>    39,673-tyre PCR gap, **22,171 is the carry-out tail** — campaigns that start in
>    August and finish in early September. Do not read 90.6 % as a regression
>    against July's 96.6 % without stating the load.
> 2. **August PCR same-size 64.7 % is the denominator defect AGAIN** (fifth
>    instance, after §1e, §4d, §4p.1, §4q.7). 28 August GTs have no `gt_size` rim,
>    so 29 % of PCR build transitions have an unknown rim on one side; **among
>    known-rim pairs it is 85.5 %** (TBR 97.9 %). L11's own formula makes it worse
>    both ways: `rim_of.get(a) == rim_of.get(b)` scores `None == None` as SAME and
>    `None != R13` as DIFFERENT. Fix the master, not the scheduler.
> 3. **The manual GT count is NOT far smaller than the MES master** — the brief
>    expected it to be. Manual PCR 4,847 / TBR 1,275 = 6,122 against MES 5,132 /
>    1,266 = 6,398: **−4.3 % overall**, −5.6 % PCR, +0.7 % TBR, and 27 vs 26 / 25 vs
>    25 GTs. The two measure the same thing and agree. Opening stock covers only
>    1.0 % of the August build either way, so this was never going to move the month.
> 4. **The workbook's own `Matched GT Code` is corroborated, not merely trusted.**
>    Resolving every demand SKU BOTH ways — recipe chain and string bridge — gave
>    **148 rows where both resolve, 0 disagreements, 0 plant mismatches**. The chain
>    also rescued `1325214617107MSXT0` (**11,220 tyres, 2.1 % of demand**), which the
>    workbook filed as "Not Existing" with a blank GT while classifying it PCR.
>    Always run both routes and print the agreement count.
> 5. **`SLIVER_TBR` default changed 1.0 → 0** (`l7_pull_release.py`). It was already
>    the measured-best value and every shipped run passed it on the command line;
>    the default now IS the shipped setting, verified by re-running August with only
>    `PLANNER_OPENING_GT` set and getting the identical 477,350 / 523,335.
>
> **New in `scripts/`:** `gt_namespace.py` (the single SKU/GT bridge, with the TBR
> `GT 5001` vs size-led trap documented in one place), `ingest_orderbook_demand.py`
> (forward order book → `masters/demand/`), `ingest_manual_opening_gt.py` (manual
> floor count → an opening-GT master, with the age assumption stated and a
> "check whether it binds" instruction). `build_gt_sku_share.py --from-demand`
> derives the BTP SKU split from the order book, which is the only correct source
> for a month with no cured volume.
>
> **Two bugs fixed while packaging:** `l1_validate` and both exporters read
> `opening_gt_<month>.parquet` directly and so validated/reported a different
> opening stock from the one the plan used whenever `PLANNER_OPENING_GT` was set;
> and the BTP split reconciliation compared against `round(Σ float)` instead of
> `Σ round(per-GT)`, printing `!! MISMATCH` for a 1-tyre-in-95,082 rounding residue
> that is arithmetically unavoidable.

> **Read [EXPERT_AUDIT.md](EXPERT_AUDIT.md) first** — an independent expert pass
> found four real defects in already-reviewed work, including an 8.67-point TBR
> regression hidden by a plant-total metric. Current reference run: **`runs/f_solo`
> — PCR 97.13 % / TBR 95.89 %** (was v32); see MEMORY §10d and PARTITION §4j.
> **Superseded 2026-08-09: `runs/st_jul` / `runs/st_aug` — PCR 93.84 / 89.84,
> TBR 87.12 / 92.02, with ZERO runs below the B12 floor** (plant instruction;
> MEMORY §10f, PARTITION §4m). The permissive arms `runs/s4_*` — PCR 95.40 /
> 91.80, TBR 96.59 / 98.55 at 7.9–31.6 % sub-floor — are §10e / §4l.
>
> **Then [PARTITION_AND_CHANGEOVER.md](PARTITION_AND_CHANGEOVER.md) before touching
> any mined constant, the build partition, or changeover costs, and before running
> a month other than the one the partition was built for.** It is the defect
> ledger for four errors that each cost real debugging time — a mined *median*
> used as a hard floor (twice, 13.4 pt of fulfilment between them), flat
> changeover minutes charged to both plants (every setup figure wrong, and wrong
> by a different factor per plant), and three bugs that made the partition
> silently month-specific. Each entry records the wrong value, the right value,
> and the guard that now prevents recurrence. Current reference run: `runs/v24`.

---

## 1. What this is
Python engine that learns from 8 months of Smart MES history (Dec 2025 → Jul 2026) and produces synchronized Building + Curing schedules for PCR + TBR. Every rule is statistically vetted. Every scheduled lot carries a `decision_trace` naming the rule IDs that chose it.

**Locked-in user decisions**
- All 10 phases end-to-end.
- Planning tech: **heuristic greedy + SA + Tabu + LNS**. No CP-SAT.
- Learning tech: **classical stats + pattern mining**. **No ML**, no RL, no LLM.
- Python 3.11, project-local `.venv`. Never system pip.
- Big data via **Polars + DuckDB + Parquet**. **No pandas in hot path**.
- Objective weights = Demand-first (missed_demand × 1000, changeover × 1, cure_wait × 2, WIP × 0.5, soft × 5, stability × 0.5).
- SimPy N=200 replications, `ProcessPoolExecutor(max_workers=cpu-1)`, batches of 50.
- Replay uses `DemandMode.ACTUAL_MONTH`. Forward `cli plan` uses `PROXY_PREV28`.

---

## 2. Data landscape (root `/Users/vinay/Documents/ME/schedule/`)

**Raw MES CSVs** (read-only)
- `curing/curingpcr.csv` 2.99 M rows, `curingtbr.csv` 753 K.
- `o_production/tbm{pcr,tbr}stage{1,2}.csv` — building output.
- `io_production_consumption/tbm{pcr,tbr}stage{1,2}.csv` — component consumption. **PCR stage2 = 14.9 M rows (largest file)**.
- 8-month span: **2025-12-01 → 2026-07-31**.

**xlsx supporting**
- `bom_pcr_tbr/jkt_bom_pcr 23.xlsx` (46 K rows), `jkt_bom_tbr 5.xlsx` (83 K). Cols: `Super_parent, Equipment, grand_parent, Parent, Parent_qty, Parent_unit, child, child_quantity, child_Unit, child_description`.
- `Sku construction mapping/…PCR.xlsx` — sheet **PCR**, header row 4. Includes cycle_time_sec, tyres_per_shift_100.
- `…TBR.xlsx` — **beware**: physical sheet names ≠ logical. Real mappings live at logical sheet names **Sheet4** (200 rows, Material→GT→slots), **Sheet6** (137 rows, GT template), **Sheet1** (28 rows, test SKUs). Balance data at **Before / After / Sheet3** (411 + 411 + 124 rows).

**Not yet supplied by plant** — masters loaded via stubs; drop `.parquet|.csv` into `masters/` when available:
`demand, routing, machine_master, allowed_machine_matrix, cycle_time, setup_time, calendar, opening_inventory, opening_wip, moq_mpq, aging_rules`.

**Data-only principle**: everything derived from MES + BOM + construction until real plant masters arrive. Each derived master gets `provenance.json`.

---

## 3. Lineage bridges (memorize)

- **Build stage 2 `itemCode`** = GT code (green tyre spec, e.g. `"GT 1402 XPC TATA"`). Not the finished SKU.
- **Build stage 2 `productionID`** = per-tyre barcode (leading-zero VARCHAR, e.g. `"0123762147"`).
- **Curing `gtbarCode`** = same barcode. Join `build.productionID = curing.gtbarCode`. 99.6 % hit rate on 3.7 M events.
- **BOM `bom_gt_map`** = finished SKU → GT code (via `child_description == 'Green Tyres'`).
- **Curing `wcID`** = press id (int, cast to VARCHAR at ingest).
- **Curing `cycleStart` timestamp** is **press-open (cycle END)**, not start. `event_ts` = press-close. Duration = `cycleStart − event_ts` ≈ 1955 s median. Named backwards in source data; do not flip.

---

## 4. Directory layout

```
schedule/
├── curing/ o_production/ io_production_consumption/   # raw CSVs
├── bom_pcr_tbr/ "Sku construction mapping"/           # xlsx
├── masters/                                            # plant files land here
├── warehouse/                                          # Hive parquet, produced by `make ingest`
├── planner/
│   ├── data/         ingest, warehouse, masters, bom, construction, balance, schema
│   ├── learn/        descriptive, machine_pref, sister_sku, sequence_mining,
│   │                 timing, aging, balance_signal, test_freq, calendar_infer,
│   │                 violation_scan, rule_extract
│   ├── kb/           rule_types, rule_store, promoter
│   ├── plan/         ledger, component_ledger, demand, lots, timing_lookup,
│   │                 calendar, decision_trace, rulekb, building, inv_sim,
│   │                 curing, sync
│   ├── simulate/     dist, discrete_event
│   ├── optimize/     state, objective, neighbourhood, tabu, lns, sa, driver
│   ├── validate/     violations, fuzz
│   ├── replay/       harness, kpi, compare, full_kpi
│   ├── runs/         run_context, logger
│   └── cli.py        typer entry
├── runs/<run_id>/    outputs (config.json, rules.duckdb, month=YYYY-MM/…)
├── tests/unit/       pytest (11/11 green)
├── scripts/bootstrap.sh
├── Makefile          14 targets incl. `make all`
├── requirements.txt  pinned deps installed only into .venv
├── pyproject.toml
├── .python-version   3.11
├── README.md
└── MEMORY.md         (this file)
```

---

## 5. Rule Store (rules.duckdb)

DuckDB table `rules`. Columns:
`rule_id, scope, statement(JSON), support, confidence, exception_rate, sample_size, ci_low, ci_high, p_value, type∈{hard,soft,stat}, weight, provenance, active`.

**Promotion thresholds** (`planner/kb/promoter.py`):
- **hard**: `conf ≥ 0.995 ∧ support ≥ 500 ∧ excep ≤ 0.005 ∧ p < 0.001`
- **soft**: `conf ≥ 0.80 ∧ support ≥ 100 ∧ ci_low ≥ 0.70`
- **stat**: descriptive priors (μ, σ, p50, p95)

Historical anomalies (from violation_scan) never count toward numerator when promoting to `hard`.

---

## 6. Learn results (2 515 rules)

- **912 hard, 616 soft, 987 stat**
- MPM candidates: 322
- Cycle-time stat: 1 211 (per plant × recipe × press)
- Setup-time stat: 3 792
- Sequence patterns (PrefixSpan, min_support 1 %): 705
- Sister-SKU clusters (Ward + permutation search, ≤ 8! full else beam=200): 60 groups
- Aging rules (per plant × GT): 189
- TBR balance quality demotion: 1 (rate > 5 %)
- Test-SKU cadence rules: 27
- Calendar-inferred machine-days: 9 675
- Violations flagged: 22 189 cure critical, 81 balance defects, 0 mould double-book.

Run time: ~17 min. Mould self-join dominates (partition-pruned by month, not by day — kept for correctness across midnight).

---

## 7. Replay results (Feb-Apr 2026, actual_month demand)

**Full KPI Panel per month**

| KPI | Feb | Mar | Apr | Actual |
|---|---:|---:|---:|---:|
| Demand qty | 450 939 | 487 480 | 436 693 | same |
| Demand fulfillment ↑ | 100 % | 100 % | 100 % | 100 % |
| Sync ↑ | 97.1 % | 96.9 % | 95.7 % | — |
| Makespan (h) ↓ | 762 | 900 | 753 | — |
| Machine util ↑ | 47 % | 43 % | 46 % | **95 %** |
| Press util ↑ | 36 % | 29 % | 27 % | — |
| On-time ↑ | 98.4 % | 98.3 % | 98.5 % | — |
| Building changeovers ↓ | **1 464** | **1 524** | 1 534 | 1 487 |
| Curing changeovers ↓ | 254 K | 251 K | 228 K | — |
| GT aging p95 (h) ↓ | 828 | 935 | 457 | **28** |
| Avg WIP ↓ | 1 036 | 1 064 | 726 | — |
| Machine idle (h) ↓ | 0 | 0 | 0 | — |
| Press idle (h) ↓ | 131 K | 207 K | 246 K | — |
| Daily CV ↓ | 0.82 | 0.86 | 0.85 | — |
| **Hard violations = 0** | ✅ 0 | ✅ 0 | ✅ 0 | — |
| Machine-SKU stickiness | 60 % | 59 % | 56 % | — |
| Size lock | **100 %** | **100 %** | **100 %** | — |
| Avg SKUs/(machine·day) | 3.5 | 3.3 | 3.5 | — |
| Starvations | 118 | 133 | 146 | — |
| Demand shortfall | 13 141 | 15 344 | 18 718 | — |
| **Wins / 8** | **3** | **3** | 2 | — |

**Where planner wins**: changeovers on Feb/Mar (1-2 % better), zero hard-rule violations, 100 % size lock.

**Where planner loses (known root causes)**
- Machine util 45 % vs 95 % → greedy leaves multi-hour gaps between lots; real plant packs 24/7 multi-shift.
- GT aging 828 h vs 28 h → planner assumes near-zero opening WIP (only 13 K prior-month leftovers); real plant stages thousands. **Only fixable with `masters/opening_inventory.parquet`**.
- Curing changeovers 250 K → curing planner interleaves tyres by GT across presses. Real plant runs long campaigns. Fix: campaign-length rule inside `curing.py`.

---

## 8. Bugs hit + how they were fixed (do not re-introduce)

1. **`productionID` schema drift** (INT vs VARCHAR across daily partitions). Fix: force VARCHAR at ingest via `read_csv_auto(..., types={...})`. Also preserves leading zeros.
2. **DuckDB substr on TIMESTAMP** — `substr` needs VARCHAR. Fix: `CAST(TRY_CAST(dtandTime AS TIMESTAMP) AS DATE)`.
3. **`cycleStart` direction reversed** — see §3.
4. **`curing_wait` counted 190 rows only** — `itemCode` is GT code (191 distinct) not SKU. Not a bug; expected.
5. **Timing rules = 0** on first learn — direction bug in `learn/timing.py`. Fixed.
6. **400 K per-tyre Pydantic `GTEvent` inserts → OOM / 90 s builder** — replaced with **Polars → Arrow → DuckDB register + one bulk INSERT**. Pattern reused in curing + sync.
7. **compare.py cartesian JOIN** on `b.gt_code IS NOT NULL` — fixed to `productionID = gtbarCode`.
8. **Recursive BOM CTE OOM** — 29.5 GB tempdir. Fix: **precomputed per-GT leaf multipliers in Python once**, then per-lot join. `_precompute_gt_leaves()` walks BFS with visited-set for cycles. **Never revert to DuckDB recursive walk per lot**.
9. **Opening inventory query hung** — was filtering `WHERE event_ts < ?` on unpartitioned column. Fix: filter on `date < ?::DATE` for Hive partition pruning.
10. **Cure wait 68 M s (822 days)** → CDF press assignment concentrated load on top presses. Fix: **capacity balancer** that spills to next-preferred press when press-day exceeds historical p95. Now cure_wait ≈ 3-5 M s.
11. **Sync FIFO didn't cascade** — old code updated only one cure_ts. Fix: **vectorized FIFO pairing** — kth cure paired with kth supply per (plant, gt_code) via DuckDB rank + join + join-update.
12. **KPI cure_wait used lot-end_ts** (apples-to-oranges). Fix: **per-tyre pairing via ledger** (`gt_events.parquet` persisted by inv_sim).
13. **Historical util hardcoded to 100 %** in compare.py. Fix: real utilization computed via inter-event gaps.
14. **416 K mould double-book violations** — same mould code assigned to same GT across all presses. Fix: **mould per (plant, gt_code, press)** — each press has its own physical mould copy in reality; labelled `<mould>@<press>` so the verifier treats them as distinct. Verifier now shows **0 hard violations**.
15. **verifier check_negative_gt used lot end_ts** for all tyres in lot. Fix: per-tyre supply = `start_ts + setup_s + (i+1) × cycle_s/qty`.

---

## 9. Key facts you'll need again

- **Cure cycle time**: 1 955 s median (~33 min) per plant.
- **PCR distinct GT codes** in production: 191.
- **Presses**: 95 PCR + 80 TBR = **175 total**.
- **Historical throughput cap**: PCR ~200 tyres/press/day, TBR ~50/press/day (from p95).
- **Full ingest run time**: 15 s (32 M rows via DuckDB `COPY … PARTITION_BY`).
- **Full learn run time**: 17 min. Mould self-join dominates.
- **Replay per month**: ~2.5 min.
- **DuckDB tempdir OOM budget**: 29.5 GiB on this machine. Recursive CTE across BOM = certain OOM.

---

## 10. CLI cheat-sheet

```
./.venv/bin/python -m planner.cli ingest
./.venv/bin/python -m planner.cli bom construction balance
./.venv/bin/python -m planner.cli learn
./.venv/bin/python -m planner.cli replay --limit 3
./.venv/bin/python -m planner.cli plan --month 2026-08 --demand-mode proxy_prev28
./.venv/bin/python -m planner.cli optimize --budget 600
./.venv/bin/python -m planner.cli sim --n-reps 200
./.venv/bin/python -m planner.cli validate --fuzz
./.venv/bin/python -m planner.cli smoke
./.venv/bin/python -m pytest tests/
```

Env-var overrides: `PLANNER_W_MISSED_DEMAND=500 make plan` etc. (all `OptimizerWeights` fields).

---

## 10b. Walk-forward rebuild — experiment ledger (Aug 2026)

Full 8-month re-run (Dec 2025 → Jul 2026, 33.2 M rows ingested clean). Purpose:
produce a leak-free result and beat the plant. **Keep this section updated with
every knob tried — it is the record of which permutation won.**

### The leakage that made the old numbers invalid

`replay/harness.py` says it trains on `date < M`. It never did: `run_learn()`
mined the whole warehouse and `replay_month` planned months already inside that
training set. All README §4 numbers are **in-sample**. `plan/curing.py` compounds
it — its press CDF, p95 capacity and mould map are rebuilt from *full* history at
plan time.

**Fix**: `data/warehouse.set_cutoff(date)` applies `WHERE date < cutoff` to
`v_curing`/`v_build`/`v_consume` at the *view* layer, so all ten miners plus the
planner and ledger inherit it with no signature churn. Filter is on the Hive
partition key, so it prunes rather than scans. Static views (BOM, construction,
balance) have no event date and stay unfiltered. `run_learn(cutoff=)` tags its run
`learn-asof<date>`; `_latest_learn_run()` excludes those so forward `cli plan`
still picks the full-history KB. Driver: `scripts/walkforward.py`, one month at a
time via `scripts/run_month.py`, report via `scripts/report.py`.

Protocol: cutoff → learn from scratch → plan with `PROXY_PREV28` → **lift cutoff
only to score**. Dec 2025 has no prior history and is seed-only; Jan–Jul are the
seven plannable months.

### Bugs found by walk-forward (all pre-existing, all fixed)

| # | Bug | Effect |
|---|---|---|
| 1 | `machine_cache.setdefault(plant, _fallback_machines(plant))` — default evaluated **eagerly**, so a full-table scan ran per lot | Jan hung >32 min; now 101 s |
| 2 | `build_cycle_s` hardcoded 45 s/90 s per tyre. Measured: PCR 58 s, **TBR 204 s** | schedule was fiction; now learned per machine |
| 3 | machine score `mpm_score * 1000 - free_pen` — weight is in *hours*, so preference won until ~1000 h more loaded | one PCR machine held 999 h vs 500 h neighbours |
| 4 | MPM lists only *preferred* machines (p ≥ 0.60), often one per GT — overload had nowhere to spill | candidates widened to any machine that historically built that GT |
| 5 | opening GT inventory counted `built AND NOT cured-before-cutoff`, which includes **never-cured scrap** | 9,290 vs true 5,502 at Jan 1 (+69 %) |
| 6 | opening written as **one row per GT carrying qty**, but sync/kpi rank supply **by row** | 9,200 of 9,290 opening tyres silently dropped → *exactly* the phantom `demand_shortfall` 9,200 (Feb: 13,141) |
| 7 | `plan_curing` accepted `start` and **never used it** — opening WIP carrying real build timestamps let cures schedule before the window opened, sorting ahead of their own supply | 1,547 phantom starvations; fixed via `avail_ts = max(built_ts, start)` |
| 8 | cure time modelled as in-press dwell (`cycleStart − event_ts` ≈ 1931 s PCR) | **~3× under-modelled capacity**; queue diverged, aging p95 1,744 h vs 32 h actual |

**Bug 8 detail — do not re-introduce.** A `wcID` starts a new tyre every ~268 s
(PCR) yet dwell is ~1931 s, so **a wcID is not one exclusive press** and dwell is
not its per-tyre occupancy. Measured 1.00 tyres/cycle, so it is not dual-cavity
either. Correct model is sustained throughput, `span/tyres` per press:
**PCR 581 s, TBR 2247 s** — reproduces actual (581 s × 86 presses × 744 h ≈ the
396 k tyres/month the plant really cured). Lives in
`TimingLookup._load_cure_cadence` / `cure_cadence_s`.

### Effect of the inventory + curing fixes (bugs 5-8)

| KPI | Jan before | Jan after | Feb before | Feb after |
|---|---:|---:|---:|---:|
| GT aging p95 (h) | 1,743.6 | **135.8** | 1,156.1 | **95.8** |
| Avg WIP | 1,904.8 | **342.6** | 1,415.2 | **281.9** |
| Press util % | — | 42.18 | 43.36 | **63.31** |
| demand_shortfall | 9,200 | **0** | 13,141 | **0** |
| starvation events | 1,547 | **0** | 3,324 | **0** |

Also: artefacts like `demand_shortfall.parquet` are written *only when
non-empty*, so a stale file from a prior run reads back as this run's result —
`scripts/run_month.py` now wipes the month dir first. This masked the bug-6 fix
entirely on first re-run.

Bugs 1 and 3–4 only bite when the KB is thin, which is why stock replay
(Feb–Apr, full-history KB) never surfaced them.

### Knobs introduced (`config.Thresholds`)

- `mpm_delay_tolerance_h` (24.0) — hours of extra queue wait a fully-preferred
  machine is worth.
- `gt_continuation_bonus_h` (36.0) — hours of extra wait worth paying to keep a
  GT on the machine already running it.

These two are **one mechanism**: their sum bounds how far machine loads may
drift apart, and makespan *is* the most-loaded machine. Util, makespan and
changeovers move together — they cannot be tuned independently.

### Tuning sweep — Jan 2026 (development fold; frozen after)

| `gt_continuation_bonus_h` | Util % | Makespan h | Changeovers | Aging p95 h |
|---:|---:|---:|---:|---:|
| 12 | 91.29 | 672.0 | 1,351 | 1,180 |
| **36 (chosen)** | **88.56** | **691.9** | **1,281** | — |
| 72 | 88.47 | 691.9 | 1,245 | 1,257 |

Util flattens above 36 while changeovers keep falling and aging degrades by 72.
**Config frozen at 36 from Feb onward** — retuning per month would reintroduce
the overfitting being measured.

### Jan 2026 progression (same month, successive fixes)

| Stage | Util % | Makespan h | Changeovers | CV |
|---|---:|---:|---:|---:|
| baseline (bug 1 fixed only) | 42.70 | 917.6 | 1,577 | 0.897 |
| + real cadence + tolerance | 61.35 | 999.2 ↑ | 1,518 | 0.664 |
| + wide candidates + continuation | **88.56** | **691.9** | **1,281** | 0.368 |
| plant actual | 86.91 | 744 avail | 1,229 | — |

Makespan *rose* at step 2 and that was correct: TBR work had been understated
2.3×, so 917 h was fiction and 999 h was the honest cost. Balancing then brought
it to 691.9 h, inside the month.

### Bug 9 — press assignments were scrambled across plants (pre-existing, severe)

`plan/curing.py` capacity balancer iterated `tyres.sort("built_ts")` but wrote
the result back positionally to the **unsorted** frame
(`pl.Series("press", reassigned)`). Row orders differ, so every press landed on
the wrong tyre — **all 166 presses appeared under both PCR and TBR**, and
`GT 1402 XPC TATA` was spread over 159 presses when only 13 ever cured it.
Fix: sort `tyres` once, then iterate that same frame.

Effect on Jan: curing changeovers **319,494 → 9,570 (33x)**, mould-change cost
1,907,517 → 57,401 press-hours, rule C1 4,634 bad combos → **0**, presses
activated PCR 88 / TBR 80 (was 166 each). Aging *rose* 101 → 293 h because the
scrambling had been spreading load across every press and hiding the real queue
— 293 h is the honest figure.

**Every curing KPI produced before this fix is invalid**, including the
README §4 numbers.

### GT shelf life is a HARD 72 h limit (plant, Aug 2026)

`Thresholds.gt_shelf_life_h = 72.0`. A green tyre not cured within 72 h is
scrap, so this is a constraint, not a KPI. Audited per tyre by
`scripts/check_rules.py` (rule S4), FIFO-paired off `gt_events.parquet`.

**Curing is at or beyond 100 % load at proxy demand — this is the root cause.**
`proxy_prev28` projects December (the *peak* month, 397,673 PCR) onto January:

| | Planned | Capacity | Load |
|---|---:|---:|---:|
| PCR | 397,673 | 394,871 | **100.7 %** |
| TBR | 96,627 | 91,512 | **105.6 %** |

Above ρ=1 queue wait diverges — **no sequencing can bound aging**. The plant
never faced this: it actually built 352,548 PCR + 89,531 TBR in Jan (89 % / 98 %
load). Fix: `demand.cap_to_curing_capacity()` trims demand to
`curing_load_target` (0.92) and declares the rest short rather than building
tyres that will age out. Trimmed qty **must stay integral** — a fractional qty
desynchronises the per-unit ledger from cure events (caused 1,113 phantom hard
violations).

**Press-day cap must be bounded by cadence, AND overflow must spread.** The
historical p95 cap admits ~200 tyres/press/day when the cadence clears only
86400/590 = 146, so a backlog compounds daily. Bounding the cap *alone* made
aging worse (293 h → 520 h): when every candidate press was full the loop fell
through to `chosen or press` and dumped overflow back on the single preferred
press. Both must change together — overflow now goes to the least-loaded
candidate.

Jan aging p95 progression: 1,744 h → 293 (press-scramble fix) → 240 (demand cap)
→ **145 h** (cadence cap + spread overflow). Still 2× the 72 h limit; 41.9 % of
tyres breach it.

**Still required to actually meet 72 h** — building is planned first and
independently, then curing consumes it, so a GT whose build rate exceeds its
cure rate accumulates. Curing is the bottleneck and must be scheduled *first*,
with building release paced to it per GT (rule E1). Not yet implemented.

### Press allocation formulation (the correct model)

    min  sum_p (k_p - 1)                                   [changeovers]
    s.t. sum_{g in p} q_gp * c_p  +  (k_p - 1) * m_p  <=  H * rho     for all p
         sum_p q_gp = Q_g                                  [demand met]
         q_gp > 0  =>  p in Hist(g)                        [rule C1]

Two consequences, both of which we had wrong:

1. **Changeovers = sum_p k_p - P**, so minimising them means minimising
   *(press, GT)* PAIRS -> allocate ONCE for the month. A per-day allocator
   creates *(press, GT, day)* TRIPLES, which is why daily keying gave 1,803-1,939
   changeovers against the plant's ~245.
2. **Budget in SECONDS with the setup term reserved at allocation time.** We
   packed to 92 % of pure curing capacity and only then added ~6 h per mould
   change on the timeline -> presses at 101 % -> span spills past month end.

Measured effect of adding the `(k_p - 1) * m_p` term and reverting to
month-level: curing changeovers **1,803 -> 365** (plant ~245), mould-change cost
10,893 -> **2,183 press-hours**. Aging 187 -> 332 h, cure span 1,211 -> 1,356 h.

**So the changeover objective is now essentially solved and the span is not.**
The residual is time distribution, not capacity: total work 114,861 + 2,183 =
117,044 press-h over 167 presses = 701 h each, which fits 744 h, yet the span is
1,356 h. Presses are packed to <=685 h by the budget but finish late because
their queue is back-loaded relative to tyre arrival.

**Standing trade-off (Jan):** few changeovers <-> low aging pull against each
other under the current architecture.

| Config | Curing COs | Aging p95 | Cure span |
|---|---:|---:|---:|
| due-date daily keying | 1,925 | **185 h** | 1,211 h |
| month-level + setup-aware budget | **365** | 332 h | 1,356 h |
| plant | ~245 | 32 h | **744 h** |

### How the plant couples build->cure (measured, Jan 2026) — the target mechanism

| | PCR | TBR |
|---|---:|---:|
| cured **within 8h** (same shift) | **64.2 %** | 66.4 % |
| 8-24h | 26.9 % | 27.0 % |
| >72h (scrap) | **1.11 %** | 0.33 % |
| built/day vs cured/day | 11,752 / 11,671 | 2,888 / 2,863 |
| GT inventory swing | **mean ±624**, min **-1,576**, max +2,427 | ±103 |

The plant's GT inventory **mean-reverts around zero and goes negative** — curing
sometimes runs ahead of building. Little's Law on that buffer gives W = 1.3h,
consistent with the observed 4.5h p50. Zero scrap is NOT the plant's target
either: it strands ~1 % of PCR.

**Ours, for contrast:** cumulative GT balance grows monotonically to **99,338**
(mean 34,676, min 0 — never drains), schedule spans **65 days** not 31, Little's
Law W = 148h. Aging is real, not a measurement artefact.

### The governing formula: per-GT flow balance

    n_g = ceil( build_rate_g / press_rate )

Give each GT enough presses that its cure rate matches its build rate; inventory
then mean-reverts instead of accumulating. **Bug found and fixed:** press
capacity was sized with `days` = the *build span* (~27d) rather than the planning
horizon, so the packer ran out of sized capacity and opened only **61 of 87**
presses. Cure throughput landed at ~45 % of build. Fixed to horizon+shelf-life
tail: presses opened 61 -> **163** (83/87 PCR, 80/80 TBR), aging 594 -> **523h**.

### Models tried against the aging error (Jan 2026)

| Model | Aging p95 | Mach util | Makespan | Verdict |
|---|---:|---:|---:|---|
| baseline | 665.7h | 80.8 % | 704.7h | — |
| **#2 weighted fair share** (press share ∝ 1/cadence) | 593.7h | **88.6 %** | **644.2h** | **kept — wins on all three** |
| #1 CONWIP + #2 | 481.9h | 53.9 % | 1,057h | rejected, see below |
| #1 CONWIP W=8h + #2 | 482.2h | 53.1 % | 1,073h | identical to W=24h |
| #3 flow-balance press sizing | **522.7h** | 88.6 % | 644.2h | kept |

**Why CONWIP failed (important):** W_target 24h and 8h give identical output
(481.9 vs 482.2h) — a real WIP cap would respond strongly to a 3x change. Open
loop it is not a WIP cap at all, just a fixed-rate pacer, because a true CONWIP
release fires when a *cure completes* and curing is planned AFTER building here,
so there is nothing to observe. `conwip_enabled` is off by default. This is
empirical proof that bounding aging requires scheduling curing FIRST.

### Press allocation granularity — three tried, none yet correct (Jan 2026)

Measured plant behaviour (`RULEBOOK.md`): a press holds ONE SKU for a campaign
of ~1,166 units PCR / ~468 TBR — that is **7-8 days** at 152 tyres/press/day —
and only ~4-5 presses per plant change over per day (**~146/mo PCR, ~99/mo TBR**).

| Granularity | Curing COs | Aging p95 | Verdict |
|---|---:|---:|---|
| per tyre (CDF pick) | 319,494 | — | wildly too many |
| **per day** (realloc each day, carry-over stickiness) | **2,228** | 428 h | presses churn daily |
| **per month** (bin-pack, current) | **18** | 265 h | campaigns ~28k units — far too long |
| **plant actual** | **~245** | **~29 h** | target |

Daily reallocation fails because a GT's daily volume varies, so presses churn
even with carry-over. Month-level dedication fails the other way: campaigns run
the whole month and strand tyres. **The right granularity is the campaign
(~1,166 / ~468 units), which is neither.** Not yet implemented.

**Aging cannot be closed in curing alone.** Building runs flat out (685 h for
498 k tyres) while curing drains over the month, so tyres wait no matter how
presses are scheduled. The reference's `GT_BUFFER_SHIFTS = 4` caps build-ahead
at ~32 h — that is the mechanism, and it belongs in *building*, not curing.

### Objective (north star, planning, Aug 2026)

> Produce 100 % demand with the fewest changeovers, longest practical campaigns,
> grouped sister SKUs, balanced machine utilization, synchronized Building &
> Curing, minimum WIP/aging, and zero constraint violations.

Priority when rules conflict: **zero violations → demand → changeovers/campaigns
→ WIP/aging → utilisation balance.** Also in `BUSINESS_RULES.md`.

### Reference implementation in `referance/` — NOT plant actuals

Built by the user's intern: CTP **v6.23**, generated 2026-08-04, June/July/Aug
2026, PCR+TBR, building (20 sheets) and curing (17 sheets). Treat as a **format
and rule reference**, never as a baseline to score against.

Its config, which answers several of our open questions:

| Param | Value | Our rule |
|---|---|---|
| **GT_BUFFER_SHIFTS** | **4** | build-ahead cap = the E1/aging mechanism |
| MAX_ENDOFDAY_GT_INVENTORY | 5,100 | S3 (we had this Blocked) |
| MAX_MACHINES_PER_SKU | 3 | B9 |
| MIN_CURE_CAMPAIGN | 100 | C3 |
| MIN_BUILD_BATCH | 150 PCR / 44 TBR | B12 (distinct from the 300/150 *demand* floor) |
| PACING_FACTOR_BY_LINE | 1.08 | E1 |
| CONSOLIDATION_MIN_SLICE | 400 | campaign merging |
| MONTHEND_STOCK_DAYS / FRAC | 2 / 0.15 | month-boundary carry |

Structural differences from ours: **shift-based** (A/B/C x 480 min, day starts
07:00) not continuous; **SKU-level** with GT as an attribute (we are GT-only, so
we cannot emit a SKU schedule); curing rows carry `Status` RUNNING/CHANGEOVER
with `Used_Mins`/`CO_Mins`/`Mould_Clean_Mins`/`Starved_Qty`; changeovers are
scheduled rows, not derived counts; an `Exceptions` sheet with Hard/Soft +
threshold (same idea as our rule audit). It also has a `Press Tube-Type Lock`
sheet, so **C2 is derivable after all**.

Its thresholds: building stickiness >= 84 % MoM, curing >= 66.8 %, SKUs/press
<= 5, unique SKUs/day PCR 28-33 / TBR 35-40. Its PCR July curing has **71
changeovers** (~6/day) — the bar to beat.

### Curing changeovers: 319,494 -> 18 (Jan 2026)

Four compounding fixes, each of which alone was insufficient:

1. **Press campaign ordering** — order each press's queue by GT campaign (block
   per GT, blocks by first arrival), not by arrival time. Arrival ordering makes
   a shared press hop between GTs continuously.
2. **`max_gts_per_press` (5)** — each extra GT on a press is another 3.5-7 h
   mould change; opening an idle spare press is cheaper.
3. **Sequential press simulation** — `end[i] = max(end[i-1], avail[i]) + work[i]`,
   unrolled to a cumulative max so it stays vectorised:
   `end[i] = C[i] + cummax(avail[j] - C[j] + work[j])`. The old
   `max(naive_end, avail+cycle)` let a late-arriving tyre leapfrog the rest of
   its campaign — with only **1.17 GTs per press** we still had 1,225
   changeovers because the two GTs on 13 shared presses alternated endlessly.
4. **+1 s availability margin** — build credits land on fractional seconds
   (`setup_end + cycle_s*i`, float), so a cure computed to the same instant sat
   0.5 s before its own supply: 2,819 phantom `negative_gt` violations, every
   one exactly 0.5 s.

Jan result: **18 curing changeovers** (105 press-hours) vs the reference's 71,
**0 hard violations, 100 % demand, 100 % size lock, 0 starvation**, machine util
90.3 %, makespan 685 h.

**Open trade-off:** long campaigns push GT aging to 192.7 h (28.3 % over the
72 h limit) versus 102.4 h when we had 1,225 changeovers. Campaign length and
aging pull against each other; `GT_BUFFER_SHIFTS`-style build-ahead capping is
the mechanism that resolves it and is **not yet implemented**.

### New plant rules B16/B17 (Aug 2026)

- **B16 min run size** — drop GTs below **PCR 300 / TBR 150** tyres for the
  horizon (`Thresholds.min_demand_pcr/_tbr`, `demand.drop_below_min_demand`).
  Below that, a 28-60 min building changeover plus a 3.5-7 h mould change costs
  more than the output. Jan: 15 GTs / 1,878 tyres dropped, 82 kept.
- **B17 lot size = plant campaign length** — `lots._observed_lot_size` now
  measures the plant's own uninterrupted run (gaps-and-islands over consecutive
  same-GT tyres per machine), not the calendar-day total. Day totals conflate
  several runs or split one across midnight. Jan default lot 93.

### Curing press campaign allocation (replaces per-tyre CDF pick)

Presses are now **bin-packed to GTs up front**: walk GTs largest-first, fill
whole presses from that GT's historically-valid candidates, then share each GT's
tyres across its presses by **weighted round-robin**. Three traps found while
building it, all of which produce plausible-looking but wrong schedules:

1. Sizing every press by the **plant median** cadence swamps slow presses —
   presses differ several-fold. Size by each press's own cadence.
2. Splitting a GT across its presses in **contiguous blocks** (`seq/total`) puts
   the first 60 % of the month on press A and the last 10 % on press D, so D
   idles for weeks. Use a short cycling period so presses interleave.
3. Reserving capacity but distributing **evenly** overloads a press that only
   had a sliver free. Distribute in proportion to capacity reserved.

Jan effect: curing changeovers **319,494 → 1,032**, mould-change cost
1,907,517 → 5,863 press-hours, presses opened PCR 61/87 + TBR 45/80 (P8/P9 now
satisfied), aging 1,191 → **213 h**, tyres breaching 72 h 41.6 % → **17.8 %**.
Still FAIL on S4 — the tail (max 3,091 h) is unresolved.

### THREE different press times — never conflate them (root of several bugs)

| Number | Value (PCR/TBR) | What it is | Correct use |
|---|---|---|---|
| `cycleStart - event_ts` | 1931 / 3912 s | tyre dwell inside press | **none** — a wcID starts a new tyre every ~268 s, so it is not exclusive occupancy |
| `span / tyres` | 590 / 2200 s | observed *throughput* = service + changeover + idle | **none** — circular (see below) |
| median inter-cure gap | **285 / 344 s** | service time while running | press timeline |

**The circularity that invalidated the capacity test.** Using `span/tyres` as
service time makes modelled capacity equal whatever the plant already produced,
so *any* real demand reports ~100 % load and headroom can never appear. That is
what made Jan's demand look "unbuildable" when the plant had actually built and
cured that exact volume the month before. It also stretched press timelines 2.2x
and manufactured queues. Fixed in `TimingLookup._load_cure_cadence` and in
`demand.cap_to_curing_capacity` (both now use the median gap).

**Mould change is now charged on the press timeline**, not merely reported:
`timing.mould_change_s(plant, press)` from the CTP master via the pressbarCode
crosswalk (PCR 210-430 min, TBR 361 min; plant default ~6 h). Previously the
planner saw press changeovers as free, which is why it happily switched GT on a
press ~1.8x/day.

**What that revealed:** at 6 h per change, 9,243 changes = 55,564 press-hours =
44 % of all press capacity. Jan aging p95 went 145 h -> 1,191 h *not* because the
schedule got worse but because the previous number was fiction — changeovers had
been free. The schedule was always this bad; the model finally says so.

**Therefore the real open problem is curing campaign length.** A press can afford
roughly one change every 2-3 days, not 1.8/day. Target is ~960 changeovers/month
(each GT on a small dedicated press set, run continuously), against 9,243 today.
With corrected service time one TBM needs only ~4.6 presses to keep up (not the
9.5 implied by the throughput figure), and 88/11 = 8 presses per machine are
available — so dedicated per-GT press allocation is feasible. **Not yet
implemented.**

### Plant master files received (Aug 2026) — `masters/`

- `Master_Building_ChangeoverTime_{pcr,tbr}.csv` — **setup time IS changeover
  time**, and it is *size-dependent*: PCR 28 min same-size / 60 min different
  (22/42 on TBMPCR10-11), TBR 10/24. A flat mined median cannot express this.
- `Master_Mapping_Mould_SKU.csv` — 2,208 rows, 1,438 moulds, 405 SKUs, 114
  sizes; `Matl.Description` carries the size.

Loader `data/plant_masters.py` → `warehouse/masters/*.parquet`, views
`v_changeover_build`, `v_mould_sku`. Wired into `TimingLookup.setup_s`, which
now returns the plant's same/different-size minutes whenever both sizes resolve
and falls back to the mined median otherwise.

**Three GT-code join traps (cost a full debug cycle — results were byte-identical
because nothing matched):**
1. PCR construction `gt_code` **drops the "GT " prefix** the MES `itemCode`
   carries → 0 overlap. **Use `gt_code_updated`** → 71 of 191 match.
2. TBR construction keys on `GT 5001`, but TBR MES `itemCode` is **size-led**
   (`10.00 R 20 JDC3`) → 0 overlap. Parse the size straight off the GT code
   (`_size_for_gt`), no master needed.
3. TBR changeover master names machines `SAV-1..9`, MES uses
   `TBMTBR1Stage2..9` — mapped positionally off `wcID`.

Coverage: 291 GT sizes resolved. Effect (Jan/Feb): makespan 691.9→707.6 /
563.4→553.0, util 88.56→87.91 / 89.05→**91.81**, aging 135.8→**101.1** /
95.8→95.1, WIP 342.6→**297.7** / 281.9→280.8.

### Still open

- GT aging p95 ~1,200 h vs 32 h actual — untouched; a curing/opening-WIP
  problem, not a build-scheduling one.
- Curing changeovers ~300 K — needs the campaign rule (§11.1).

---

## 10c. CURING-FIRST REBUILD (Aug 2026) — the permutation that worked

Driven by `curing_planning_engine_v2.txt` (+ v1 `curing_planning_formulation.txt`).
**This is the best permutation so far. Revert here if a later change regresses.**

### Headline (July 2026, out-of-sample, KB mined Dec–Jun only)

| KPI | Before | After | Plant | Limit |
|---|---:|---:|---:|---|
| GT aging p95 | 292.8 h | **73.4 h** | 27.6 h | ≤72 h (marginal) |
| Total span | 991.8 h | **765.7 h** | 744 h | — |
| Cure span | 991.8 h | **712 h** | 744 h | — |
| Avg WIP | 336 | **359.6** | ±624 | — |
| Machine util | 80.6 % | **83.6 %** | 96.9 % | — |
| Press util | 61.4 % | **86.1 %** | — | — |
| Daily production CV | 0.43 | **0.282** | 0.28–0.31 | — |
| Building changeovers | 1,344 | 1,480 | 1,631 | — |
| Curing changeovers | 267 | 220 | 153 | — |
| Press-days unserved | — | **0 / 0** | — | =0 ✓ |
| Hard violations | 0 | **0** | — | =0 ✓ |
| Demand / shortfall | 100 % / 35 | **100 % / 0** | — | — |

Aging fell from 292.8 h to 73.4 h. **NO TUNED CONSTANTS** — see "Nothing fitted
to a month" below. An earlier variant reached 49.2 h using the spec's ρ and a
hand-set 8 h build look-ahead; both were withdrawn as month-fitted, and 73.4 h
is what the model delivers on its own.

### Nothing fitted to a month (the rule for this path)

Every number the planner uses is derived at run time from the month being
planned. A constant read off one month's plan will not transfer, and there is
no way to detect that from that month's own KPIs.

| Was | Now | Derivation |
|---|---|---|
| `RATE = {PCR 156, TBR 48}` | `plant_rate()` | `3 x floor(28800 / cure_cadence_s)`, plant median — the SAME capacity `shift_grid` executes with, so plan and executor cannot disagree. Reproduces 156/48 exactly on July, which validates the formula. |
| `rho = {0.863, 0.945}` | `SETUP_DAYS = 1/3` per campaign | ρ = T\*/H is one month's measurement. A mould change is one 480-min shift — countable, exact, and it self-corrects with the plan's actual fragmentation. |
| `gt_build_ahead_h = 8.0` | `SHIFT_S` | Not a free parameter: the grid makes a tyre eligible the shift after it is built, so exactly one shift of lead is needed and every further hour is pure age. |
| day gate `+24 h` | `3 x SHIFT_S` | The end of the lot's own day — a calendar boundary. |
| `D` from the spec's sweep | measured + packed outcome | Median active cure-days from MES under the as-of cutoff, then chosen on unserved press-days. |
| `PRIME_DAYS = 1.7` | deleted | Superseded by deriving the target from the packed press plan. |

### The architectural change

Curing is planned **first**. `plan/window_plan.py` sizes and staggers per-GT
windows and books press campaigns; `plan/shift_grid.py` executes them on a fixed
31 × 3 × 480 min grid; building is handed a per-(GT, day) target table and keeps
only two freedoms — which machine, and order within the day.

Span stops being an output. The horizon is a loop bound, so a coupling loss can
no longer show up as span — it surfaces as unfilled tyres, which decompose by
GT, press and shift. Nine earlier allocation-side variants each fixed their own
metric and left the span between 883 and 1,356 h; none could work, because ρ is
set by the build schedule and cannot be repaired downstream.

### Bugs found and fixed (each cost a run to find)

1. **`campaigns` computed then discarded.** `plan_windows` returned it,
   `plan_curing` never took it. "Curing-first" was only reshaping the build
   target while curing still ran the old unbounded event sim. Symptom: aging
   improved 142 → 51.8 h but span did not move. v2 assertion A2 could not hold
   because the windows never reached the curing code.
2. **The D sweep is degenerate.** `argmin_D Σceil(W_g/24D) − |P|` decreases
   monotonically in D, and *every* stated rejection test (mould count,
   eligibility, peak draw ≤ |P|) bounds D from **below** — so the argmin is
   always D = H. It returned 31 against the 15.6/17.1 the same document reports
   from MES. Now selected on the **measured packing outcome** (unserved
   press-days), which is the thing that actually costs output.
3. **A single global D is infeasible at every D.** The `max(1,·)` floor pins 45
   PCR GTs at ≥1 press for the whole window, so peak draw ≥ 108 against 92
   presses no matter what D is. D is now **per GT**: `n_g = ceil(W_g/D_target)`,
   `D_g = ceil(W_g/n_g)`. A 3-press-day GT gets one press for three days.
4. **Cross-plant press leak.** The capability matrix gave PCR 114 presses
   against a real 92, so n_g was sized on phantom capacity. Intersect with
   `SELECT DISTINCT wcID FROM v_curing WHERE plant = ?` (`real_presses()`).
5. **All-or-nothing interval packing.** Forcing all n_g presses onto the same
   `[a, a+D_g)` window left 542 press-days unserved while 25.6 % of press-shifts
   held no mould — the presses were free, just not on those exact days. Each
   press now takes its **own** interval; a GT needs W_g press-days, and where
   they come from does not matter. Third tier scans from day 1 for GTs the
   a-ascending order would otherwise strand (9 GTs cured *nothing*).
6. **Fractional tyre quantities, again.** The daily split emitted `qty 0.5`
   lots; `int_ranges(0.5::BIGINT)` is an EMPTY range, so the ledger gained 213
   **null-timestamped** events. Nulls sort ahead of everything and shift every
   FIFO rank the verifier derives → **236 phantom `negative_gt` violations**.
   Fixed with an integer largest-remainder split summing exactly to `round(N_g)`.
   *A tyre is a discrete object — never let a float reach the ledger.*
7. **A full day of aging baked into the target table.** `built(g,d) =
   cured(g,d+1)` guarantees ≥24 h of age before any queueing: p50 was 52.5 h
   with 19 % of tyres past the 72 h shelf life. The plant cures **64 % in the
   same shift** it builds. Same-day identity + `gt_build_ahead_h` 24 → 8 h took
   p95 115.8 → 80.7 h.
8. **Press booking ignored setup.** Booking exactly `N_g/rate` press-days assumes
   a press is productive on every shift it holds a mould, but the first shift of
   every campaign is a 480-min mould change. Curing cleared 462,588 of 496,928
   and the 34,340 surplus sat in the pool — and **under FIFO a persistent
   surplus ages every tyre**, not just the leftovers, because each cure pops the
   oldest. `_pack` now charges `SETUP_DAYS = 1/3` per interval directly.
   *First attempt used the spec's ρ (0.863 PCR / 0.945 TBR) and gave aging 49.2 h
   — **withdrawn as overfitting**: ρ = T\*/H is measured against one specific
   month's demand, so it books the wrong press-time for every other month. It
   also over-booked PCR by 227 press-days. Exact setup charging replaces it and
   is month-independent; the honest number is 73.4 h.*
9. **Build-side eligibility dead-end.** 47 lots pinned to 2 PCR machines carried
   the build span 9 days past month end while one of them idled 210 h. Widen to
   the whole plant (minus the size lock) when every historical machine is booked
   past the deadline. Span 947 → 780 h. Same bug class as (5), one layer up.
10. **`candidates` ↔ `mpm_rows` index coupling.** Scoring read `mpm_rows[i]` off
    the candidate *position*, correct only while `candidates` starts with
    `preferred` in order — so any filter added there silently mis-attributes
    every machine preference. Now a `{machine: rule}` dict.

### Rule changes applied from the v2 spec

| # | Rule | Action | Result |
|---|---|---|---|
| 4 | T₀ 47 h → **24 h in-window** | CHANGE | 47 h is a horizon average over a ~50 % duty cycle; building every 47 h while presses drain every 24 h is a 2× starvation generator on every GT. `lots.py` now divides by the GT's **own active days**, not H. |
| 9 | Aggregate rate pacing | REPLACE | A plant-wide scalar throttled a GT whose presses were idle by the total built for every *other* GT. Replaced by per-lot due-date release. |
| 2, 3 | Min-run filter 300/150, demand cap 0.92 | DELETE | Together they cut ~10–13 % of volume *before* planning, so "100 % fulfilment" was 100 % of a pre-cut demand. Now re-baselined on full demand. |
| 13 | McNaughton wrap-around | DEAD | Bypassed entirely on the curing-first path. |
| 14 | ρ = 0.92 blended | CHANGE | Split 0.863 PCR / 0.945 TBR (`rho_target_pcr/tbr`). |
| 19–21 | D_g windows, daily identity, size lock | ADD | Size lock is a **hard prefilter**, not a score term — it had never reached the assignment layer as a soft term. |

### What did NOT work (do not retry)

- **Sweeping D for minimum changeovers** — degenerate, see bug 2. D=31 means one
  window covering the month, i.e. not windowing at all.
- **Selecting D on predicted peak load** — picked D=15 (peak 93) over D=31
  (peak 94) even though 31 packed strictly better: span 744 vs 802 h, 78.4 %
  vs 69.8 % productive press-shifts. The stagger peak is only an upper bound;
  the packer relocates what it could not fit. Select on the packed outcome.
- **Synthetic prime/steady build curve** over `[a, a+D_g)` — assumes the presses
  are evenly spread across the span. Once each press carries its own interval
  they are not, and building landed days from the presses consuming it: aging
  93.8 → 293.6 h. Derive the target from the **actual** packed press plan.
- **`n_g × rate` as the daily target** without normalising to N_g — `ceil`
  rounding handed a 500-tyre GT 1 × 156 × 29 = 4,524. Planned 665,713 tyres
  against 461,660 of demand (+44 %).
- **Due date as a hard release floor** — no machine carries more than 718 h in a
  744 h month, so a machine idled until an exact day-30 date has no room left
  and the tail slides out (span 971 h at 56 % utilisation, with the work
  fitting). Needs the look-ahead.
- **Day-0 campaigns** — a press mounted on day 0 has nothing to pull; its GT's
  first tyres are built that same day and are only eligible the following shift.
  Guaranteed starved press-day. Windows start at day 1.
- **Building for the last day** (under the old next-day identity) — those tyres
  had no cure day after them and were stranded in WIP forever.

### G8 — the GT inventory band (stated by planning, Aug 2026)

**PCR 4,500–4,800 and TBR 1,200–1,500 green tyres, EVERY day including the
last.** Total 5,700–6,300 — which reconciles with the measured opening GT of
5,948, i.e. the plant runs at steady state month to month. This is the
closed loop the engine never had.

Why it matters: the daily identity `built(g,d) = cured(g,d)` holds WIP flat
only if curing executes perfectly. It does not — presses lose ~11% of shifts to
setup, starve and idle — so the surplus compounds. Measured: PCR WIP ramps to
~24,000 against a 4,800 ceiling and closing stock reaches 5.4x opening. Under
FIFO a monotonically growing queue is *exactly* the aging: each tyre waits
longer than the one before it.

**Implemented but NOT yet binding.** `window_plan` walks the days, projects
per-GT stock forward and trims any day's build that would breach the ceiling.
The projection is optimistic: it credits a campaign day with a full press-day
of curing, but the grid delivers ~94% of that (setup 1.5%, idle 2.2%, starved
1.6% of shifts). So projected closing WIP is ~4,000 while the realised figure
is ~32,000, and the cap never fires. Charging the mould-change shift exactly
(a campaign's first day yields `1 - 1/3` press-days) recovered ~8,000 of the
gap; the rest is per-press timing granularity a daily projection cannot see.

**The fix is a two-pass fixed point, not a yield constant.** The shift grid is
deterministic and runs in ~10 s: plan → build → cure → re-derive the build
target as the grid's OWN realised cure per (GT, day) → re-run. Then
`built = cured` by construction and WIP is flat without estimating anything.
Do NOT introduce a fitted yield factor here.

**Expect the metric to flip.** Honouring the band means building only what can
be cured, so demand fulfilment drops below 100% and the shortfall becomes
visible per GT/press/shift. v3 s19.A predicted this: *"do not treat this as a
regression"* — the inefficiency was always there, previously hidden in WIP.

### 8-month study of the masters (Aug 2026) — three corrections

`scripts/study_months.py` measures every master against all 8 months
independently. Three findings, two of which reversed an earlier belief.

**1. The TBR press rate was WRONG — the single biggest correction so far.**

|      | shift model | ACTIVE-DAY p50 | ACTIVE-DAY p95 | we used |
|------|---:|---:|---:|---:|
| PCR  | 117 (3 slots) / **156** (4) | **144–158** | 192–202 | 156 ✅ |
| TBR  | **45** (3) / 60 (4) | **38–44** | 48–52 | **48 ❌** |

156 happens to sit at PCR's active-day p50, so it was right by luck. The 48 we
used for TBR sits at its **p95** — a best-day figure, over-stating press
capacity ~14% and under-booking TBR's press-days. That is consistent with TBR
being the class that under-delivered.

Fix: derive `rate` from the plant's own **active-day median** for both plants,
one basis, measured (`engine/resolve.py::_rate`). A press idle half the month
drags the monthly mean and says nothing about its rate, so ACTIVE days only.

    July: cure fulfilment 96.53% -> 99.16%,  aging p95 38.2 -> 36.2h,
          over-72h 0.43% -> 0.33%,  span 736.2h

**2. "The allowable matrices are far too narrow" — that claim was WRONG.**
Measured against every month's real usage they cover **96.5–99.1%** (press) and
**98.7–99.5%** (machine). The median of 2 machines per GT is the plant's actual
behaviour, not a data gap. The XLSX caveat has been corrected.

**3. History is a feasibility set for MACHINES, never for PRESSES.**

| | Jan | Mar | May | Jul |
|---|---:|---:|---:|---:|
| NEW press pairs | 45.4% | 40.8% | 40.4% | **35.8%** |
| NEW machine pairs | 41.2% | 15.8% | 8.5% | **9.3%** |

Machine pairs settle after ~4 months; press pairs stay at 36–45% new every month
with no sign of saturating. So machines may be gated on history once enough
history exists; presses must never be.

Also corrected: `allowed_press_matrix` had 56 rows whose press id belongs to the
other plant (PCR listed 114 presses, 95 exist). `cycle_time_curing` now carries
dwell / capacity / actual as three labelled columns — one ambiguous `s_per_tyre`
was the reason two parts of the engine disagreed about a press-day.

Master audit after the fixes: `scripts/validate_masters.py` -> **0 FAIL, 2 WARN**.

### Still open on this path

- PCR is over-booked at ρ = 0.863: 227 press-days short, stagger peak 131 vs 92
  presses, 6 % starved shifts. PCR needs 2,823 press-days against 2,852
  available — genuinely capacity-tight, exactly as v2 §6.2 predicts (it expects
  TBR to be the binding class; on our data it is PCR).
- Span 754 h vs 744 h — 10 h over.
- Curing changeovers 252 vs plant 153; machine utilisation 85.5 % vs 96.9 %.
- **Only July has been run** with the full set. Jan/Feb/Mar/May have cached
  `learn-asof` KBs and were not re-run; Apr/Jun have no cached KB (~17 min each).

---

## 10q. SISTER GROUPING & THE INCH LOCK AS A PRIORITY — two plant requests, measured (2026-08-09)

Full ledger in [PARTITION_AND_CHANGEOVER.md](PARTITION_AND_CHANGEOVER.md) §4q
(inch lock) and §4r (sister grouping). Arms `RP_*_jul` / `RP_*_aug`, all fresh
via `run_arm.py`; `RP_base_jul` reproduces `runs/jul_prod_v1` **metric-for-metric**,
so the gating is provably clean. New harness: `scripts/arm_kpi.py` re-derives
every KPI from the plan parquets in the named directory.

**The four things worth remembering:**

1. **A tie-break below `HARD_PIN` is a no-op.** Both flags were first built where
   §12 says resource coherence belongs — the candidate-machine sort — and
   `RIM_PRIORITY` measured **byte-identical to baseline**. `PIN_RUNS` breaks on
   the first feasible machine and the partition gives most GTs one, so the
   candidate list is usually length 1. What binds is where the resource is
   *created*: the spill assignment (rims) and the deadline heap (sisters).
2. **A building-side rim campaign is a shadow of the cure schedule.** Every PCR
   rim has an active cure campaign on **all 31 days** with 7–28 presses
   concurrently, and R5 chains build to cure within 72 h. The plant's literal
   request — finish 12 inch, then run only 13 — is physically unschedulable from
   the building side. Capping rims per machine halves the switches
   proportionally (66 → 34) but does not campaign them.
3. **The cost master cannot express sisterhood.** `cap_changeover` is keyed on
   (machine x same/different size) only. The plant's realised gap is 5.2 min at
   distance 0, 8.3 at distance 1, 12.1 at distance ≥2 (Cliff's δ −0.768, size
   controlled) — a 2.3x spread the engine charges as a flat 10 min. Sister
   benefit is invisible in `weighted_setup_h` by construction.
4. **August same-size was a master-data hole all along.** 17 PCR GTs / 6.9 % of
   August build slices have no `gt_size` row, so 27.9 % of August changeovers
   involve an unknown rim and are charged as different-size. Reported 65.2 %;
   among known-rim pairs it is **89.9 %**. Every August same-size figure this
   project has quoted is contaminated.

**Data lineage note — `planner/data/construction.py` has three live defects**
(found against the raw workbooks; **not yet fixed**):

| # | defect | consequence |
|---|---|---|
| 1 | `PCR_COLS` (lines 25–39) maps only the merged super-header `"Component Code"`, which at `header_row=4` lands on col S. Cols T–AC get auto-generated names and are dropped by the `keep` filter at line 62 | `construction_pcr.parquet.component_code` holds **Inner Liner alone** — 7 of 8 component columns silently discarded |
| 2 | `rim_dia` cast to `Float64` (lines 67–69) but the raw values are `'R12'`/`'R15'` **strings** | **0 of 233 non-null**, against 218/236 in the sheet. This is the origin of the "rim_dia 100 % null" claim, and `learn/sister_sku.py:30` has been doing `CAST(rim_dia AS DOUBLE)` on an already-null column — PCR sister mining has always run without its rim feature |
| 3 | `derived/sku_construction.parquet` is **TBR-only** (230 TBR rows, 0 PCR) | no PCR branch exists |

Also: the module docstring (lines 7–9) says `Sheet5 → per-SKU slot map` /
`Sheet4 → test SKU list`; the code at lines 153–155 reads **`Sheet4`** for the
SKU map and **`Sheet1`** for test SKUs. **The code is right, the docstring is
wrong.** Sheet selection is by NAME and is correct — the trap is that
name-number ≠ position (`Sheet6` is at 0-based index 5, `Sheet1` at index 3), so
switching to positional indexing would silently read the `After` balance sheet.

⚠ **Never join PCR GT codes on the 4-digit numeric core.** It maps
`GT 1513 XPC1 MSIL` (55,583 tyres, the largest July GT) to `GT1513 NEO`, a
different tread. The digits encode SIZE, not product. The exact SKU bridge gives
72.9 % of GTs with **zero** ambiguity; take the lower coverage.

---

## 10g. THE HARD FLOOR WAS NOT WHAT COST THE VOLUME (2026-08-09)

§10f shipped a true 0 % sub-floor and priced it at −1.56/−9.47/−1.96/−6.53 pt.
**That price was a packing defect, not the rule.** Reference `runs/FIX_jul` /
`runs/FIX_aug`: **96.37 / 95.02 (Jul PCR/TBR), 94.89 / 96.82 (Aug)**, sub-floor
still **0.0 %**, export 0 HARD / 0 SOFT / 0 EXPORT, pytest green. Against a
permissive arm on the SAME engine the floor now costs **−0.59 / −2.34 / +0.03 /
−1.84** pt.

**The tell was in §10f's own numbers and went unread:** strict *freed* machine
time and produced less. Jul TBR occupancy 79.6 → 71.7 %, idle 976 → 1,294 h,
setup 175 → 125 h — and 9,278 fewer tyres. A constraint that consumes capacity
cannot raise idle time. Whenever a rule appears to cost volume while releasing
resource, the rule is not the cause.

**Root cause.** Runs are released just-in-time, so each leaves a hole sized by
DEADLINE SPACING: p50 1.00 h (PCR) / 1.30 h (TBR) against floor-minimal runs of
2.84 h / 5.05 h. A run that may not be cut below the floor needs ONE contiguous
hole, cascades past every sliver to `t0`, and is refused beside 1,294 idle hours
on its own machines. The permissive arm split it into halves that fit the
slivers — which is exactly what made those halves sub-floor.

**Three plausible causes were tested and all three are false** (this is why they
were tested rather than argued): shelf life 72 → 144 h is worth +0.47 pt TBR /
−0.14 PCR; both WIP rails → 99,999 is worth +0.15 / 0.00; a 72 h pre-horizon
warm start +0.05 / +0.14. Capacity is not short either — PCR needs 220 machine-
hours of 2,150 free, TBR 446 of 1,714 — and the refused GTs are locked to
machines at 58-87 %, not saturated ones.

**The obvious fix would have measured as a no-op.** Re-cutting the slice stream
into the best floor-feasible partition (a DP over the delivery stream) cannot
help: every refused PCR group is already between 150 and 300 tyres (p50 156, 0
of 123 above 2× floor) and every refused TBR group is 3 slices (p50 87). They
are already the smallest legal runs. **Before optimising a decision, check
whether it has any freedom left.**

**What shipped** (PARTITION §4n.3): anti-sliver packing — never leave a hole no
legal run could occupy, `PLANNER_SLIVER_*` default 1.0, +1.59/+3.29/+2.98/+2.13
pt — and `_make_room`, a targeted LNS that pulls the runs blocking the latest
legal slot earlier, each inside its own R5 floor, then inserts, with a bundle
rail check and full rollback: a further +0.94/+4.61/+2.07/+2.67 pt.

**The LNS lesson:** the first `_make_room` compacted the whole prefix and failed
699 times on the rail against 6 successes. Minimal displacement — only the
blocking runs, only by the hours needed — took it to 188 rescues. More insertion
points made it worse again. **In a rail-bound schedule, a repair must move as
little stock as possible; breadth of search is not the lever.**

**Honest residual:** Aug PCR 94.89 %, 0.11 pt short. 56 % of its remaining
refusal is month-boundary cold start (days 1-2 = 72 %), which a 72 h warm start
closes exactly. That is a rolling-horizon/carry-in problem, not a packing one.

---

## 10f. STRICT B12 — ZERO SUB-FLOOR RUNS, AND WHY THE OBVIOUS FIX WAS WRONG (2026-08-09)

Plant instruction: *"I strictly want that NO lots below this min lot cap."*
Shipped ON as `PLANNER_STRICT_LOT_FLOOR=1`. Reference `runs/st_jul` /
`runs/st_aug`. Ledger in [PARTITION_AND_CHANGEOVER.md](PARTITION_AND_CHANGEOVER.md) §4m.

**Verified 0 runs below the floor**, both plants, both months, re-derived from
`build_schedule.parquet`: min run 150 exactly (PCR, floor 150), 78 and 82 (TBR,
floor 70).

| | Jul PCR | Jul TBR | Aug PCR | Aug TBR |
|---|---:|---:|---:|---:|
| permissive → strict | 95.40→**93.84** | 96.59→**87.12** | 91.80→**89.84** | 98.55→**92.02** |
| pt / tyres | −1.56 / −6,162 | −9.47 / −9,278 | −1.96 / −7,986 | −6.53 / −5,190 |
| sub-floor | 7.9→**0.0 %** | 31.6→**0.0 %** | 9.6→**0.0 %** | 18.7→**0.0 %** |

**The one lesson worth keeping: the obvious diagnosis was wrong.** `HARD_FLOOR=1`
plateaued at 3.6 %/4.5 %, and the natural conclusion — "L4.5/L5 must be emitting
under-floor lots, fix it upstream" — does not survive decomposition. **99 % of
sub-floor runs were the GROUPING REMAINDER in `l7` phase 2a**, on a machine where
that GT was already well above the floor. `groups` is cut at `acc >= target` and
the trailing leftover becomes a group of any size; `_place` then never compared
`gq` to the floor at all, because the floor was only ever checked on the SPLIT
path. Gating splits harder could not reach zero no matter how hard it gated.

Fix is three gates — grouping repair (merge right-to-left within `span_cap`),
`_place` refusal, `HARD_FLOOR` forced on — plus `ATOMIC_SPLIT` force-disabled,
since that fix works *by* creating sub-floor runs. Where R5 and B12 genuinely
conflict, R5 wins and the volume is shortfall tagged `below min_lot (strict B12)`
(175 tyres Jul, 58 Aug) rather than a sub-floor run.

**It is not all cost.** Best changeover result the project has produced: TBR
CO/machine-day 3.76→2.70 and 2.62→2.10 against a 3.56 plant benchmark, weighted
setup TBR 175→125 h and 128→102 h, PCR same-size 82.3→84.1 %, PCR lot p50
179→303 on August. L11 improves on both months (23→24, 21→22).

---

## 10e. L5 SHAPE — THE TAKT CAP, THE ATOMIC SPLIT, AND A TABLE NOBODY READ (2026-08-09)

Reference `runs/s4_jul` / `runs/s4_aug`. Full ledger in
[PARTITION_AND_CHANGEOVER.md](PARTITION_AND_CHANGEOVER.md) §4l.

| cumulative step | Jul PCR | Jul TBR | Aug PCR | Aug TBR |
|---|---:|---:|---:|---:|
| base | 94.53 | 94.45 | 90.14 | 92.69 |
| +1 atomic split (PCR only) | 95.62 | 94.45 | 91.18 | 92.69 |
| +2 L5 takt cap (TBR only) | 95.62 | 96.59 | 91.18 | 98.55 |
| +3 load tie-break — **REJECTED** | 95.21 | 96.35 | 91.92 | 99.28 |
| **+4 per-press mould change → SHIPPED** | **95.40** | **96.59** | **91.80** | **98.55** |

Three lessons worth more than the points:

1. **L5 had no view of the month.** First-fit-decreasing, as-early-as-possible.
   TBR August ran presses at 98.2 % for 20 days and 34.7 % for the last 11 while
   5,810 tyres starved — against a month whose work content is only 75.6 % of the
   press hours. **The fix is not delay** (campaigns are 248 h at p50, nothing can
   be staggered); it is a cap on how many presses may be SEATED AT ONCE. α = 1.0
   is an interior maximum on both months, so the knob is the takt rate, not a
   tuned constant.
2. **A budget can be un-spendable.** `l7`'s sub-floor budget was 180 and had spent
   ~9 while 27,203 PCR tyres starved, because the split that would spend it
   terminated at `len(grp) == 1`. Before raising a limit, check whether the code
   can reach it.
3. **A table loaded and never read is a silent wrong answer.** `mch_press` sat in
   `l5` unused for the whole project while the plant MEDIAN was reserved; 28
   August events physically over-ran a still-curing press and every gate passed.

Two per-plant splits, both DO-NOT 15 again: the atomic split is **+1.09/+1.04 on
PCR and −2.01/−0.92 on TBR**; the takt cap is **+2.14/+5.86 on TBR and mixed-sign
on PCR**. Neither transfers.

**Watch R5.** July TBR is now at **71.5 h against a hard 72**. The takt cap causes
it. `PLANNER_LOAD_TIEBREAK=1` takes it back to 66.3 h at a cost of 0.41/0.24 pt on
July — that is the lever if the margin ever binds.

---

## 10d. CMBC L4/L7 — LOT SIZING, CONSERVATION, AND THE PLANT GAP (Aug 2026)

Work on `planner/cmbc/`. Baseline `runs/july_cmbc_v3`, result `runs/july_cmbc_v5`.

### Headline

| | v3 baseline | v5 | plant (July MES) |
|---|---:|---:|---:|
| PCR build runs / lot p50 | 4,832 / 48 | **1,246 / 192** | 753 / **363** |
| PCR runs below the 150 floor | **91.1 %** | **2.7 %** | 12.7 % |
| PCR build changeovers / machine-day | 15.96 | 3.64 | 2.18 |
| TBR build runs / lot p50 | 2,145 / 29 | **1,060 / 87** | 898 / **86** |
| TBR runs below the 70 floor | 89.4 % | 16.5 % | 30.8 % |
| TBR changeovers / machine-day | 7.94 | 3.89 | 3.19 |
| true fulfilment vs plannable demand | 98.3 % | **98.5 %** | — |
| phantom tyres in the reconcile | 449 | **0** | — |

**TBR lot size now matches the plant (87 vs 86).** PCR is still half (192 vs 363).

### Defects found and fixed (each cost a run to find)

1. **The build RUN did not exist as an object.** L7 chose a machine per SLICE, so
   consecutive slices of one campaign landed on different machines and no run
   ever reached the B12 floor. `run_id` (maximal consecutive same-GT block on one
   machine) is now an emitted column in `build_schedule.parquet`.
2. **The continuity sort was dead code.** Candidates were sorted by continuity and
   then selected with `key = (-wait, mach)`. Because `mach` sits in that tuple the
   sort could only decide an exact wait tie, which never happens once machines
   have different cadences. Continuity must DECIDE, not tie-break.
3. **Release was computed per CAMPAIGN, not per GT.** A PCR GT cures on ~6.4
   presses at once, so each campaign draws only ~6.4 tyres/h against a machine
   that builds at ~62. No single campaign is ever worth a setup — that is why runs
   came out at 0.84 h. Batched at the GT the same demand is ~41 tyres/h.
4. **449 phantom tyres: a fan-out join.** `fed` is one row per
   `(plant, gt_code, press)` but 75 of 346 keys carry more than one campaign, so a
   left join handed EVERY campaign the full fed quantity and `min(qty, fed_qty)`
   counted it once per campaign. Now allocated FIFO by campaign start. TBR
   reported 95,411 fed against 94,962 that existed for a full release cycle.
5. **The R5 guard checked the wrong endpoint.** It tested `t_last − run_end`, but
   when slices share a cure time the FIRST-BUILT slice waits longest. 10 breaches
   passed it. Check every slice.
6. **Placement was all-or-nothing.** Pinning + batching interact: a run that does
   not fit had nowhere to go and the WHOLE 87-tyre run starved where the old
   per-slice code lost 29. Measured in isolation — pin alone 4,086 starved,
   neither 2,457, both **8,994**. Fix: halve the run and retry down to one slice.
   Starved 305 → 44, unfed 8,994 → 1,230.
7. **L4 netted opening GT off the CURE.** An opening green tyre is upstream of the
   press — it still has to be cured. It reduces what must be BUILT, never what
   must be CURED. L4 removed 6,117 tyres from the cure requirement and L7 read the
   SAME `opening_gt` file and fed 5,256 of them to presses as supply: once as a
   demand reduction, once as a delivery. Now `net_cure = demand − fg_stock` and
   `gross_build = net_cure/yield − from_stock`.

### The lot-sizing model (validated against the ledger)

    I  =  lambda*tau*  +  lambda*T/2          Little + the Q/2 sawtooth
    Q  =  (lambda / n_active) * T
      =>  I  =  lambda*tau*  +  Q*n_active/2
    T* =  2 * ( I_target/sum(r_g)  -  tau* )

**The `tau*` term is not optional.** Solving `T = 2I/sum(r_g)` without it gave
T = 15.06 h and delivered 10,303 tyres against a 4,650 target. With it, T = 6.42 h
and predicted 5,650 vs realised 5,835 — 3 %, the first time this reconciled.

### The B12 floor, not T, is what sets inventory

The demand-derived lot `r_g x T` is p50 **55 on PCR and 17 on TBR**, far below the
150/70 policy floor, so the floor binds on **32/40 PCR GTs (80 %) and 46/46 TBR
(100 %)**. Sweeping T from 3 h to 12 h therefore moves PCR inventory only
5,638 → 6,186. Decomposition at T = 6.42 h:

    PCR 5,851 realised = 2,667 tau* standing + 1,982 pure sawtooth + 1,400 floor cost
    TBR 1,986 realised =   806 tau* standing +   538 pure sawtooth + 1,072 floor cost

PCR's 4,500–4,800 band is unreachable while holding BOTH tau* = 4.32 h and a
150-tyre floor. Only cutting one of the two moves it.

### Plant July 2026, measured on the SAME definitions

`scripts/plant_run_baseline.py`, `scripts/plant_gap_analysis.py`.

| | plant | ours (v5) |
|---|---:|---:|
| PCR cure presses / utilisation | 87 / 96.2 % | 86 / 95.3 % |
| **PCR n_active GTs** | **26.3** | **26.7** |
| PCR distinct presses per GT | 3.28 | 4.45 |
| PCR GT wait p50 / mean | 4.76 / 8.84 h | 7.34 / 11.08 h |
| TBR GT wait p50 / mean | 4.90 / 8.17 h | 11.29 / 14.63 h |

**The plant breaks its own B12 floor far more than we do** — 12.7 % of PCR runs
and 30.8 % of TBR runs are below 150/70, the latter carrying 12.7 % of volume.
Chasing 0 % below floor is chasing something the plant does not do.

**`n_active` is NOT the differentiator** (26.3 vs 26.7). An earlier hypothesis that
the plant runs ~13 GTs concurrently was wrong. What differs is that we touch MORE
distinct presses (4.45 vs 3.28) while running FEWER concurrently (p50 2 against a
mould cap of 4), so a GT stays live 592 h of 744 instead of ~408 h. The stretched
window is what produces the wait gap, and the wait gap is what produces inventory.

### What did NOT work — L5 GT-clustering (do not retry as tested)

Grouping a GT's lots together in L5's queue so its campaigns run concurrently
(`scripts/exp_l5_cluster.py`, patches L5's sort line in memory, changes no code):

- n_active 26.7 → 18.5, presses/GT 4.5 → **6.0**, GT inventory 5,851 → **4,588**,
  head p50 7.34 → 6.32. Inventory target met on both plants.
- **Lot p50 unchanged at 192** — n_active never reached the ~13 the 363-tyre lot
  needs, so the floor still set the lot.
- **True fulfilment 98.5 % → 92.1 %** (31,338 tyres). The loss is at L5, not L7:
  cure placed fell 485,327 → 459,312 and PCR press utilisation 95.3 % → 89.5 %.
  Mould changes rose 98 → 137 and 90 → 112, worth only ~370 of ~3,700 lost
  press-hours; the rest is scheduling holes between waves.

It also moved presses/GT AWAY from the plant's 3.28. Directionally wrong.

### TRIED: rate anchoring + campaign identity as an alternative L5 — MIXED, NOT SHIPPABLE

`scripts/exp_l5_ng.py` (read-only; emits `cure_campaigns.parquet` in L5's schema,
L6/L7/L10/L11 run unmodified). Tests formulation v2 §2.2 + §6.1 together:

    n_g = ceil( W_g / (D_g * 24) ),  D_g ~ 15.6 d PCR / 17.1 d TBR
    ONE campaign per (GT, press), spanning the GT's whole window

**§6.1 rate anchoring VALIDATES.** It reproduces the plant's own numbers:

| | this run | plant July | our L5 |
|---|---:|---:|---:|
| n_g mean | **3.33 / 2.86** | 3.42 / 3.11 | 4.45 / 3.65 distinct, p50 **2** concurrent |
| build changeovers / machine-day | **2.26 / 1.34** | 2.18 / 3.19 | 3.64 / 3.89 |
| machines per GT p50 | **1 / 1** | — | 3 / 1 |
| GT inventory | **3,876 / 1,235** | 4,772 / 1,743 | 5,851 / 1,986 |
| GT head p50 | 7.23 / 10.10 h | 4.76 / 4.90 | 7.34 / 11.29 |
| R5 breaches | 0 | — | 0 |

**§2.2 campaign identity FAILS on packing.** True fulfilment **65.9 %** (324,097 of
491,630) against 98.5 % on the shipped path. 9 GTs never placed, reason
`no common window`, and PCR press utilisation fell to ~66 %.

**Why, and it is structural not a bug.** One campaign per (GT,press) spanning the
window makes each GT a rigid rectangle of `n_g` presses × `D_g` hours, with `D_g`
up to 374 h. A GT needing 3–4 presses simultaneously free for 374 h contiguous
cannot find one once the earlier GTs are placed — fat rectangles do not tile a
95 %-utilised space. `scripts/check_wave_feasibility.py` predicted exactly this
(32–51 of ~127 PCR campaigns unplaceable at every α) and this run confirms it end
to end. Splitting a GT's window into 2–3 campaigns per press — between our current
6.4 campaigns/GT and the rigid 1 — is the untested middle and the only version
worth trying.

**Do not re-run §2.2 as a rigid one-campaign-per-pair allocator.** Keep §6.1.

### TRIED: relaxing the B12 floor (`PLANNER_RUN_MULT` sweep) — a clean trade, not a win

Four arms, all fresh, derived T unchanged:

| floor × | PCR lot p50 | <floor | chg/mach-day | PCR inv | head p50 | fulfilment |
|---:|---:|---:|---:|---:|---:|---:|
| 0 | 143 | 66.4 % | 6.57 | **4,579** | **6.39** | 98.3 % |
| 0.5 | 144 | 59.7 % | 5.45 | 4,904 | 6.58 | 98.4 % |
| **1.0 (shipped)** | 192 | **2.7 %** | 3.64 | 5,851 | 7.34 | **98.5 %** |
| 1.5 | 240 | 11.8 % | 3.23 | 6,122 | 7.90 | 98.4 % |
| *plant* | *363* | *12.7 %* | *2.18* | *4,772* | *4.76* | — |

**Fulfilment is flat across the whole sweep (98.3–98.5 %)** — relaxing the floor
does NOT buy demand, so it is not a fulfilment lever. It is purely a trade:
smaller floor → lower inventory and head, more changeovers and more sub-floor
runs. k = 0 lands PCR inventory 4,579 and TBR 1,303, both INSIDE the plant band,
but at 6.57/6.39 changeovers per machine-day against the plant's 2.18/3.19.

**The important structural read: the plant is not on our frontier, it is inside
it.** It holds bigger lots AND fewer changeovers AND lower inventory AND lower
head simultaneously. No setting of our floor reaches that, so the remaining gap
is not a lot-sizing parameter — it is the head (plant 4.76/4.90 vs our best
6.39/7.41), and via `I = lambda*W` the head is what carries the inventory.

### FOUND: the flat cure rate is wrong — cycle time varies 2.0x across GTs

Measured on July MES, `epoch(cycleStart - event_ts)` (press-close to press-open):

| | per-tyre p05 / p50 / p95 | per-GT median min / max | range |
|---|---|---|---|
| PCR | 1,116 / 1,804 / 2,591 s | 1,368 / 2,738 s | **2.0x** |
| TBR | 3,174 / 4,819 / 6,781 s | 3,233 / 6,534 s | **2.0x** |

L5 uses ONE plant-level median (`cav_p[p] * 3600 / cyc_p[p]`), so a GT with a
2,738 s cycle is planned at the same press rate as one at 1,368 s. Consequences:
campaign durations are wrong by up to 2x per GT, `W_g = N_g * tau_g` is wrong,
and therefore **n_g rate anchoring is computed off a wrong tau_g** — it validated
anyway (§10d above), which suggests it gets better, not worse, with real cycle
times. `cycle_time_curing` is classed HARD at 100 % coverage in MASTER_DATA and
is currently unread. **This is a prerequisite for §6.1, not an alternative to it.**

### SOLVED: PCR lot size — the T sweep never reached the plant's operating point

The earlier sweep stopped at T = 12 h and concluded "T is not a dial". It was
stopped too early. The plant's implied interval is `Q/r_g = 366/6.5 = ~56 h`.
Extending the sweep (all arms fresh, `PLANNER_LOT_INTERVAL_H`):

| T (h) | PCR lot p50 | PCR chg/mach-day | PCR GT inv | fulfilment |
|---:|---:|---:|---:|---:|
| 6.42 (shipped) | 192 | 3.64 | 5,851 | 98.5 % |
| **24** | **403** | **2.04** | 7,669 | **98.5 %** |
| 36 | 480 | 1.56 | 9,679 | 98.4 % |
| 56 | 618 | 1.16 | 11,839 | 98.3 % |
| *plant* | *366* | *2.18* | *4,772* | — |

**T = 24 h hits the plant's lot size AND its changeover rate at zero fulfilment
cost** (98.5 %, unchanged). TBR at T = 24 gives lot 115 / 2.83 per machine-day
against the plant's 87 / 3.19 — slightly over-lotted, so TBR wants a shorter T
than PCR. This is the single cheapest correction available: one parameter.

### NOT SOLVED, and now precisely bounded: GT inventory

Inventory moves the WRONG way with lot size — 5,851 → 7,669 at T = 24. There is
no T that gives both. The decisive comparison:

> **At matched lot (403 vs 366) and matched changeovers (2.04 vs 2.18), the plant
> carries 7,669 − 4,772 = ~2,900 FEWER tyres.**

So inventory is not a lot-sizing problem and cannot be fixed by one. Two further
pieces of evidence bound what it is:

1. **Part of our reported figure is a measurement artifact.** We book cure as a
   POINT event per slice (48 tyres sharing one `cure_ts`); the plant's MES has a
   timestamp per tyre. A point-booked drain holds half a slice of extra stock per
   active GT: **641 tyres PCR (11 %), 426 TBR (21 %)**. Adjusted, PCR is 5,210
   (+9 % vs plant, not +23 %) and **TBR is 1,559 — BELOW the plant's 1,743.**
   TBR has no real inventory gap. Do not chase it.
2. **The plant beats its own sawtooth geometry by 3.2x; we beat ours by 1.25x.**
   With `W = tau* + (Q/2)(1/r − 1/b)`: plant Q 366, b 49.9/h, r 6.5/h predicts
   28.63 h against an actual 8.84 h. Ours predicts 13.88 h against 11.08 h. The
   plant is consuming a run far faster than its own average draw rate allows,
   and our model does not explain how. **That unexplained factor is the whole
   remaining inventory gap and it has not been isolated.**

⚠️ A per-GT "active hours" comparison (plant 65 % of the month at 15/h vs ours
14 % at 48/h) is NOT usable evidence — our cure is quantised to ~25 point events
per campaign, so both figures are artifacts of our own discretisation. Any future
build-vs-cure continuity comparison must first put both sides on the same time
resolution.

### SHIPPED: global cure-deadline placement order (`runs/july_cmbc_v6`)

**WIP decomposes into exactly three terms.** `I = lambda * W` (Little), and W is:

| | PCR | TBR | correctable? |
|---|---:|---:|---|
| tau* coupling buffer | 4.32 h → 2,255 tyres | 4.81 h → 619 | **no** — L0 CV 0.058 over 8 months |
| **earliness** (backward walk) | **2.86 h → 1,495** | **3.23 h → 415** | **yes** |
| residual (drain + ordering) | 3.90 h → 2,036 | 6.59 h → 847 | PCR already beats plant |

Key check: our `tau* + residual = 8.22 h` is already BELOW the plant's ENTIRE
W of 8.84 h. **On PCR the whole gap to the plant was the earliness term.**

**Root cause was the placement ORDER, not the backward walk.** Runs were placed
GT by GT, ordered by eligibility and volume — nothing to do with when a run is
needed — so a run due day 30 could take the slot a run due day 3 needed. Measured
on the old plan: runs due days 0-7 were pushed early 45 % of the time (TBR 60 %)
at mean 3.32 h, falling monotonically to 30 %/32 % and 2.62 h for days 21-31.
The runs needed FIRST were displaced MOST.

Fix: size all runs first (phase 2a), then place from a heap in **global cure
deadline order** (phase 2b), scarcity only as a tiebreak; split halves re-enter
at their own deadline.

| | v5 | **v6** | plant |
|---|---:|---:|---:|
| PCR GT inventory | 5,851 | **5,119** | 4,772 |
| TBR GT inventory | 1,986 | **1,713** | 1,743 |
| PCR W mean / p95 | 11.08 / 32.3 h | **9.73 / 24.3** | 8.84 / 28.3 |
| TBR W mean / p95 | 14.63 / 37.3 h | **12.98 / 33.4** | 8.17 / 25.4 |
| PCR chg / machine-day | 3.64 | 4.10 | 2.18 |
| PCR / TBR runs below floor | 34 / 175 | 57 / 337 | 12.7 % / 30.8 % |
| fulfilment | 98.5 % | 98.1 % | — |
| **L11** | **13/26** | **14/26** | — |

`PCR GT wait p95` — the one gate that regressed when runs were batched — is
RECOVERED (32.3 → 24.3 h, now better than the plant's 28.3). Cost: build
changeovers +0.46/machine-day, sub-floor runs roughly doubled, fulfilment −0.4 pt.

### TRIED AND REVERTED (both inside the same change)

1. **Capping the backward walk** (`PLANNER_EARLY_CAP_H`). Swept under deadline
   ordering: PCR inventory 5,119 (off) / 4,922 (12 h) / 4,961 (6 h) / 5,061 (3 h)
   — noise, and changeovers rose slightly with the cap. **The 2.86 h of earliness
   was a symptom of the ordering, not of the walk.** Knob kept, defaults to `inf`
   (off). Do not expect it to buy anything.
2. **Preferring a machine whose `last_gt` is this GT**, to recover the continuity
   that GT-ordered placement gave for free. Did the opposite: machines/GT 4 → 5,
   changeovers 4.10 → 4.31. Under deadline ordering the GTs interleave, so by the
   time a GT's next run comes up the machine has moved on. **Continuity has to
   come from the ORDER, not from the machine choice** — and the order is now
   spent on deadlines. Comment left in `_place`.

### CHANGEOVERS vs the plant — building is the problem, curing is matched

July 2026, same definition both sides (consecutive same-GT block per resource).

| | ours (v6) | plant | ratio |
|---|---:|---:|---:|
| **PCR building** / month | **1,390** | **742** | **1.9x** |
| PCR building / machine-day | 4.10 | 2.18 | 1.9x |
| **TBR building** / month | **1,142** | **889** | **1.3x** |
| TBR building / machine-day | 4.23 | 3.19 | 1.3x |
| **PCR curing** / month | **170** | **~170** | **1.0x** |
| **TBR curing** / month | **110** | **~80** | 1.4x |

**The plant's cure-changeover count MUST be measured with a minimum campaign
size.** Counting every consecutive-same-recipe break gives PCR 333 / TBR 770,
which is single-tyre recipe blips, not mould changes. Filtering campaigns to
>= 5 tyres drops **0.06 % of PCR and 0.7 % of TBR volume** and the count collapses
to 170 / 80, then stays flat as the filter rises (PCR 170/165/160/157 at 5/10/25/50;
TBR 80/79/74/70). The filtered figure also reconciles with RULEBOOK §3b's
independent ~146 / ~99. **Do not quote the unfiltered number** — it says we do
1/7th the plant's TBR cure changeovers when in truth we do 1.4x.

So the changeover gap is **entirely on the BUILDING side, and mostly PCR**:
648 extra changeovers/month at ~40 min = ~430 machine-hours, ~5 % of the PCR
machine-month. Curing needs no work.

### SIZE-AWARE MACHINE CHOICE — the changeover lever L7 never had

The plant's changeover master is BINARY: same size 22-28 min, different 42-60.
Measured July, consecutive build runs on a machine:

| | changeovers | same-size | setup hours |
|---|---:|---:|---:|
| PCR plant | 742 | **91.8 %** | ~334 |
| PCR ours (v6) | 1,390 | **32.3 %** | **987** |
| TBR plant / ours | 889 / 1,142 | 100 % / 100 % | 148 / 190 |

**2.9x the setup TIME on 1.9x the COUNT** — the extra is the size mix. L7 had no
size term at all in machine selection (L5 has a `same_rim` tiebreak for presses;
the build side never got one). RULEBOOK §4 already said the plant's size lock is
99.8 % and "a machine essentially never mixes sizes"; we were at 32 %.

Fix: prefer a machine whose current GT shares this rim. Unlike the same-GT
continuity preference (reverted, §10d), rim is a broad condition so it fires.

| arm | PCR chgovers | same-size | setup h | lot p50 | GT inv | ful |
|---|---:|---:|---:|---:|---:|---:|
| v6, no rim | 1,390 | 32.3 % | 987 | 192 | 5,119 | 98.1 % |
| **rim, T derived** | 1,413 | **65.7 %** | **798** | 192 | 5,618 | 98.1 % |
| **rim, T=18** | 853 | 69.5 % | **468** | 336 | 7,039 | 98.3 % |
| **rim, T=24** | **706** | 70.3 % | **385** | 384 | 7,886 | 98.1 % |
| *plant* | *742* | *91.8 %* | *334* | *363* | *4,772* | — |

**rim + T=24 BEATS the plant on changeover count (706 vs 742)** and lands setup
time within 15 % (385 vs 334 h) with lot size at 384 vs 363 and fulfilment
unmoved. TBR reaches 136 h against the plant's 148.

⚠️ **It is NOT free, contrary to my prediction before running it.** Rim
preference costs ~500 tyres of inventory at constant T (5,119 -> 5,618): a
rim-matching machine is sometimes only available earlier, so setup time is bought
with wait. Predicting "no inventory change" was wrong — measure, don't reason.

Ceiling: we reach ~70 % same-size, not the plant's 92 %, because this is a
tiebreak among FEASIBLE machines and often none matches. Going higher needs
horizon rim-dedication, which is a stronger policy and untested.

**The frontier is unchanged: changeovers, lot size and fulfilment can all be put
at the plant simultaneously; GT inventory still cannot come along.**

### SHIPPED: v11 — rim lock, rolling horizon, T=16 (Aug 2026)

`runs/v11`. Six changes, each measured alone. Baseline `runs/july_cmbc_v6`.

| PCR | v6 | **v11** | plant |
|---|---:|---:|---:|
| lot p50 | 192 | **335** | 363 |
| build changeovers | 1,390 | **881** | 742 |
| **same-size share** | **32.3 %** | **78.2 %** | 91.5 % |
| **weighted setup h** | **750** | **265** | 171 |
| GT inventory | 5,119 | **6,946** | 4,772 |
| fulfilment | 98.1 % | **98.4 %** | — |
| TBR weighted setup h | 190 | **166** | 148 |
| L11 invariants | 14/22 | **16/34** | — |

**1. The changeover root cause was a DATA-SELECTION defect, not an algorithm one.**
`l2_capability.PCR_INCH` marks a machine eligible whenever `lo <= rim <= hi`, and
the ranges overlap so heavily (1-2: 12-20, 3-5: 12-16, 6-11: 13-18) that rims
13-16 match ALL ELEVEN machines. 381 of 468 PCR pairs are INCH, giving 9.75
machines/GT at **20 % option-set rim purity**. No scheduler can produce 92 %
same-size from a 20 %-pure option set. `INCH_PENALTY` 500 -> **5,000** (not
deleted: 4 PCR GTs have no non-INCH machine and would strand).

**2. The machine->rim lock is IN THE DATA, not something to solve.**
`scripts/build_machine_rim_lock.py` -> `INPUT/derived/machine_rim_lock.parquet`.
Mined over 8 months: R13 -> machines 10,7,5 · R15 -> 3,8 · R18 -> 9,2 · R12 -> 4
· R17 -> 1 · R14 -> 6 · R16 -> 11 = 3+2+2+1+1+1+1 = **11 machines**, matching the
load vector (R13 3.0 · R15 1.4 · R18 1.2 · R12 1.1 · R17 1.0 · R14 0.9 · R16 0.8
= 9.4 machine-equivalents) exactly. Tiers by purity: **hard >= 99.5 %** (6 PCR
machines), **primary 85-99.5 %** (4), **flex < 85 %** (TBMPCR2 at 66.4 %, 4 rims
seen — the data names the overflow machine).
Soft, not a gate: R12 needs 1.1 machines and has 1, so ~4,142 tyres must spill.

**3. Rank the SPILL by the lock, not by the machine's last GT.** Ordering on
`last_gt`'s rim left 24.4 % of PCR volume off-lock; ordering on the machine's
horizon lock cut it to 14.9 % and moved weighted setup 472 -> 454 h.

**4. Rolling horizon: carry-out works, day-1 stock exemption does not.**
Carry-out (a campaign starting before hour 744 may finish after it; the tail goes
to `carry_out.parquet`) is worth **+0.8 pt fulfilment** on its own — 24 campaigns,
2,760 tyres. The day-1 opening-stock exemption is `PLANNER_EARLY_STOCK`,
**DEFAULT OFF**: it does raise day-1 press occupancy (PCR 51 -> 70 %, TBR
58 -> 77 %) and L5 places 450 more, but L7 cannot then feed those early campaigns
once the stock runs out — starvation 3,036 -> 3,901 — and fulfilment nets
98.9 -> 98.7 %. The original docstring warning was right in substance; its stated
bound (stock must cover the CAMPAIGN) was wrong — the correct bound is the GAP,
`(tau* + band) x rate` ~= 72 tyres PCR — and it still does not pay.

**5. `min_demand_units` -> 0.** It discarded 17 GTs / 1,701 tyres that the plant
demonstrably built (demand IS the plant's own output). The lot FLOOR is the real
economic guard; this rule deleted orders before they were considered.

**6. T = 16 h, from the design equation.** `Q = r_g x T`, `r_g = n_g x press_rate`,
`I = lambda(tau* + (Q/2)(1/r - 1/b))`. Pinning the plant's Q=363 and I=4,772 fixes
BOTH: `n_g = 3.33`, `T = 15.9 h`. This is what moved lot 192 -> 335 and
changeovers 1,390 -> 881.

### EARLINESS AND RIM PURITY ARE THE SAME RESOURCE — do not treat them as levers

W decomposes as `tau* + earliness + drain`. On v11 PCR: 4.32 (2,268 tyres,
justified) + 2.00 (1,048, not justified by any cure need) + 6.79 (3,567, the
Q/2 sawtooth).

The earliness looked free — 23 % of machine capacity is idle. It is not.
**Avoiding an early build means moving machine, and that breaks the lock:**

| PCR | inventory | same-size | weighted setup |
|---|---:|---:|---:|
| cap off | 6,946 | **78.2 %** | **265 h** |
| lock-scoped cap 12 h | 6,595 | 73.3 % | 295 h |
| unscoped cap 12 h | **5,825** | 56.2 % | 397 h |

Only ~350 of the 1,048 tyres have a same-rim alternative. `PLANNER_EARLY_CAP_H`
defaults to `inf`. **The remaining inventory lever is the DRAIN term (n_g), not
earliness and not build pacing** — full pacing needs 17,677 machine-hours against
8,184 available (2.2x over), and partial pacing inside the idle is worth only
~470 tyres because `1/r` dominates `1/b` by 2.6x.

### The plan does NOT overbuild — audited

Against "every released tyre must have a justified consumption path":
**0 rows with no `cure_ts`, and built == fed exactly on both plants.** The
inventory is genuinely synchronisation buffer, not destination stock. The defect
is not WHAT or HOW MUCH we build — it is WHEN: build rate 58.1/h against a
consumption rate of 22.1/h, **2.7x**, on machines that are 23 % idle.

### TRIED: n_g additive seating (`planner/cmbc/l5_ng.py`) — 5th confirmation, NOT SHIPPABLE

Built as a drop-in L5 (same `cure_campaigns.parquet` schema). `runs/ng3`.

| PCR | ng3 | v12 shipped | plant |
|---|---:|---:|---:|
| lot p50 | **451** | 335 | 363 |
| build changeovers | **453** | 881 | 742 |
| same-size share | **86.8 %** | 78.2 % | 91.5 % |
| **weighted setup h** | **116** | 265 | 171 |
| GT inventory | **5,466** | 6,946 | 4,772 |
| TBR GT inventory | **1,555** | 1,819 | 1,743 |
| **fulfilment** | **85.1 %** | **98.4 %** | — |

**Beats the plant on changeovers, setup time and TBR inventory. Loses 13 points
of demand.** Fifth formulation in a row with this shape.

Two real bugs found while building it, both worth keeping:

1. **n_g has a HARD FLOOR of `W_g / (H - start_floor)`.** The plant's 3.25 is an
   AVERAGE; a 55,224-tyre GT needs 11.85 presses just to finish inside the month.
   Anchoring every GT to 3.25 stranded **11 PCR GTs / 256,790 tyres**.
2. **The ladder must go UP, not down.** `L = W_g/n_g`, so lowering n_g LENGTHENS
   the window — the opposite of what a congested schedule can absorb. Reversing
   it took PCR placement 137,618 -> 329,942 tyres.

### n_g IS NOT THE INVENTORY LEVER — the model's premise is false here

The design equation assumed `r = n_g x press_rate`. Measured:

| | n_g measured | model r | **r implied by observed W** | |
|---|---:|---:|---:|---:|
| v6 | 3.07 | 21.0/h | 13.6/h | 65 % |
| v12 | 3.04 | 20.8/h | 14.3/h | 69 % |
| **plant** | **3.25** | 22.2/h | **22.3/h** | **100 %** |

**Our n_g is already at plant level.** We get 69 % of the nominal drain rate; the
plant gets 100 %. Raising n_g raises a number already at target. The runs
themselves are dense (effective draw 22.1/h against a 20.8/h nominal) — the loss
is in the TAIL: run cure span p50 15.2 h but p90 25.2 and max 270 h.

### THERE IS ONE DIAL, NOT THREE

Lot size, changeover cost and inventory are all set by **how much cure demand one
build run absorbs**. Every "lever" tried is that same dial renamed:

| config | lot | weighted setup h | GT inventory | fulfilment |
|---|---:|---:|---:|---:|
| v6 baseline | 192 | 750 | 5,119 | 98.1 % |
| rim lock only | 193 | **454** | 5,491 | 98.1 % |
| + T=16 (v12) | 335 | 265 | 6,946 | 98.4 % |
| + span cap 1.0 | 288 | 311 | 6,285 | 98.0 % |
| + span cap 0.75 | 192 | 381 | 5,532 | 97.9 % |
| n_g seating (ng3) | 451 | 116 | 5,466 | **85.1 %** |
| *plant* | *363* | *171* | *4,772* | — |

**The rim lock is the ONLY change that is not a trade** — setup 750 -> 454 h for
+372 tyres of inventory at unchanged fulfilment. Everything else moves along the
curve. The plant sits OFF the curve and we have ruled out why: n_g matches, draw
rate matches, overbuild is zero, build pacing is capacity-infeasible (17,677
machine-hours needed against 8,184).

### Two knobs added, both DEFAULT OFF, both measured

- `PLANNER_SPAN_MULT` (cure-window a run may absorb, x T). At 1.0 inventory
  6,946 -> 6,285 but it caps Q with it (same dial as T) and fragments: PCR runs
  below the B12 floor 4.0 % -> **35.3 %**. Default 99 (off).
- `PLANNER_HARD_FLOOR` (refuse a split that would breach min_lot). Costs 1.1 pt
  of fulfilment and only moves TBR sub-floor 59.1 % -> 51.8 %, because the breach
  comes from group sizing, not from splitting. Default off. NB the plant itself
  runs 12.7 % / 30.8 % of its runs below its own floor — 0 % is stricter than the
  plant and is not a target worth paying for.

### THE TWO FORMULATION ERRORS (found last; worth +10.2 pt together)

Both were the same mistake in different clothes: **a plant STATISTIC was being
enforced as a plant CONSTRAINT.** When a mined number is a median or a mean, it
describes the middle of a distribution the plant sits on both sides of. Using it
as a floor deletes the entire lower half of that distribution. Before wiring any
mined value into a hard gate, plot its distribution and check which side the
plant actually lives on.

**1. `tau*` was a hard release floor. It is the plant's MEDIAN coupling buffer.**

Build was forbidden from releasing a tyre less than `tau*` before its cure. But:

| GT wait, h | p01 | p05 | p10 | p25 | p50 | mean | % below tau* |
|---|---|---|---|---|---|---|---|
| plant PCR | 0.50 | 0.94 | 1.35 | 2.45 | 4.76 | 8.84 | **47 %** |
| ours (pre-fix) | **4.32** | **4.32** | **4.32** | 5.45 | 8.10 | 9.95 | **0 %** |

The flat 4.32 across p01–p10 is the signature: a wall, not a distribution.
**47 % of PCR and 50 % of TBR plant tyres cure sooner than `tau*`.** The physical
floor is `tau_min` = 0.27 h (R17), 16x smaller. Fix: `PLANNER_TAU_RELEASE=min`
(default). **85.7 % -> 92.9 % fulfilment**, W 9.95 -> 8.40 h, and W p50 now lands
on the plant's (4.63–6.13 vs 4.76). Keep R17 as the floor; `tau*` stays a
preference only.

**2. `min_lot_units` was a hard split refusal. The plant has NO hard lot floor.**

Machine x GT x day runs in `v_build` stage 2, full 8 months:

| | runs | p10 | p25 | p50 | below floor | July |
|---|---|---|---|---|---|---|
| PCR (floor 150) | 5,691 | 125 | 234 | 415 | **13.1 %** | 14.0 % |
| TBR (floor 70) | 6,541 | 33 | 62 | 96 | **31.0 %** | 31.0 % |

A sub-floor run is not something a supervisor refuses — it is something this
plant does one time in seven (PCR) or one in three (TBR). As a hard gate it was
**the single binding constraint on fulfilment**: all 30,615 starved tyres were
tagged `would breach min_lot`, more than the WIP cap and the rim lock combined.
Off entirely gives 97.1 % but fragments to 18.7 % sub-floor, *looser* than the
plant. Fix: a **plant-calibrated budget** — spend exactly the plant's own
sub-floor setup count (`PLANNER_SUBFLOOR_PCR=180` / `_TBR=400`), oldest cure
deadline first, then refuse. **92.9 % -> 95.9 %** at 12.9 %/33.5 % sub-floor vs
the plant's 14.0 %/31.0 %. L11's gate was changed from `== 0` to the plant share.

**`I = lambda x W` is Little's Law and needs no replacement** — it is an identity,
and it closes here to 0.2 %. It was never the wrong equation; `W` was being
inflated by a constraint the plant does not have. At W = 8.40 h the WIP cap
permits 4,800/8.40 = 571 tyres/h against the 519/h demanded, so **the cap stopped
being the binding constraint once `tau*` was fixed** — do not tune the cap first.

### Measurement caveats found here

- **Twin moulds are universal**: 99.99 % of `v_curing` rows carry `mouldNo` of the
  form `A#B`, and rows arrive in pairs ~20–60 s apart (the two cavities). A press
  runs one recipe on 94.8 % (PCR) / 98.1 % (TBR) of press-days.
- **Cure-campaign medians are definition-sensitive.** Strict consecutive-same-item
  runs give PCR p50 177 tyres / 32.5 h and a TBR p50 of **1**, because rare
  single-tyre recipe interruptions shatter the run. RULEBOOK §3b's 1,166 (~8 d) /
  468 does NOT reproduce on July under this definition. Collapse short
  interruptions before quoting a campaign median, and treat §3b's figure as
  Jan-2026 and month-level until reconciled.
- `referance/` was re-confirmed as a **format reference, not plant actuals**
  (§10c above already says so — all 12 workbooks share one generation timestamp).

---

### SHIPPED: targeted rim spill + the arm-freshness guard (`runs/f_solo`, Aug 2026)

**PCR 95.66 % -> 97.13 % (+1.47 pt). TBR untouched. 0 invariant flips.**
Full ledger in [PARTITION_AND_CHANGEOVER.md](PARTITION_AND_CHANGEOVER.md) §4j.

Three findings, in the order they mattered:

**1. 15 run directories carried another arm's scorecard.** `hl_* rr_* sm_* fin_*
perplant` — eight byte-identical to v31's `l11_invariants.parquet`, seven to
v29's. Every HARD_LOCK arm read 93.6 % / 96.1 % same-size while
`build_starved.parquet` beside it moved 13,743 -> 5,336. Cause: the `cp -r`
seeding step in PARTITION §7 inherits the previous arm's L11 result and only L7
is re-run. Fixed by `scripts/run_arm.py` (fresh from L5, no copy),
`l11_provenance.json`, and `scripts/check_arm_fresh.py`. *Fulfilment/starvation
columns from those dirs stand — they came from arm-specific artefacts. Every
INVARIANT read from them belongs to a different arm.*

**2. "Three PCR rims over 100 %" was the flat-cadence bug.** `need_h` charged the
plant median (62 s) while machines run 49-78 s. Corrected: R12 113.2 -> **89.5 %**,
R13 100.7 -> **80.6 %**, R17 101.5 -> 103.1 %. Only one rim is genuinely over, by
22 h. And the rim story never explained the volume: **51.8 % of starved PCR tyres
are on rims under 100 %**, and every starved GT has idle hours on its own rim
(R13 starves 1,120 beside 549 idle hours of its own). The mechanism is TEMPORAL:
PCR idle is **1,430 h in 508 gaps, p50 1.72 h**, against a p50 run of 5.27 h, and
`_place` walks backwards only. `CORRECTION_REGISTER` §H4 quantified.

**3. Relaxing HARD_LOCK is measured and REJECTED**: +1.64 pt at same-size
96.5 -> **69.3 %**, below the plant's 91.5 % kill line. What worked instead is a
spill whose criterion is **eligibility, not load** — the 4 PCR rims with exactly
ONE locked machine (R12/R14/R16/R17) get the plant's own flex machine (TBMPCR2,
tagged `tier == flex` in the mined lock master). Costs: same-size -3.8 pt (still
above plant), setup +31 h, R5 +6.3 h.

**Two negative results, kept honestly:**
- **Per-machine cadence alone: 0.00 pt PCR, -0.05 pt TBR.** EXPERT_AUDIT §5
  ranked it +0.3-0.8; falsified. PCR is byte-identical because ~44 of 48 PCR GTs
  bypass the `cap_h` loop via the partition. Kept because the spill is sized
  against it.
- **`SPILL_MULT` saturates immediately** (1x 95.75 / 3x 95.71 / 6x 95.71 / 12x
  95.71). The budget is not the lever. A sweep table was written into the code
  comment *before* being run and the run falsified every row — measure first.

**G8 last-day detector added and it FAILS by design:** PCR 486 / TBR 195 against
band floors of 4,500 / 1,200. Verified it is the SAME defect as the demand
horizon, not a separate one — build AND cure collapse together on day 31 (PCR to
25 % / 32 % of interior, TBR 7 % / 13 %), so WIP = cum(build) - cum(cure) falls to
~0 by construction. **Never fix it with a closing-stock floor** — that means
building tyres with no cure to consume them and destroys `built == fed`.

**The lookahead flag (`l4 --lookahead-days`) is built but is a NO-OP on July
2026**, and this is a data fact: `masters/demand/` ends at 2026-07 because demand
is derived from cured MES (`scripts/make_demand.py`) and MES ends 2026-07-31.
Verified working on June (+109,546 tyres of July appended). **To measure it, the
reference month must move to one with a successor on disk.**

---

### SHIPPED: B16 machine-side feasibility (`runs/b16_*_coverage`, Aug 2026)

**TBR May 73.7 -> 90.4 %, August 75.2 -> 94.0 %, June neutral (control), July
-1.2 %. PCR unmoved on all four months.** Full ledger PARTITION §4k.

The B16 TT/TL partition search scored only GT-side feasibility. TBMTBR7 (May) and
TBMTBR8 (Aug) landed in a group containing NONE of their eligible GTs and built
**zero tyres for the whole month** -- while the plant ran TBMTBR7 at 89.6 % for
11,587 tyres. July is the only month of four with no strand, which is why every
tuning pass missed it.

Recovery is **62 %, not 100 %**: May TBR 68,397 -> 84,133 against a plant 93,674.
TBMTBR7 0 -> 10,263 and TBMTBR5 1,293 -> 9,800, but TBMTBR4 and TBMTBR9 gave back
3,962 between them. **Freeing a machine rebalances the split; it does not purely
add.** Costs: May TBR same-size 92.2 -> 88.6 %, sub-floor 29.1 -> 35.3 %, R5
62 -> 69 h; July TBR -1.2 pt.

Also confirmed NOT defects, so do not spend time on them: the TBR eligibility
matrix is a SUPERSET (allows 159-184 pairs, plant uses 82-102, 1 forbidden pair
<=0.5 % of volume); TBR press/mould is not binding at all (cure-unplaced 0-130
tyres against build-starved 3,802-24,867 -- **TBR is purely building-limited**);
and no partition / rim lock / flex machine on TBR remains correct.

---

## 11. What still to do (in priority order)

> ⚠️ **This list predates the curing-first (§10c) and CMBC (§10d) paths and is
> scored against the ORIGINAL `planner/plan/` engine.** Items 1–3 in particular
> are already addressed there: press utilisation is 95.3 % (PCR) on the CMBC path,
> not 47 %, and opening stock is loaded. Read §10d for the live priorities.

1. **Campaign-length curing rule** (biggest KPI win — cuts curing changeovers ~10×).
2. **Continuous packing in building planner** (raises util from 47 → 80 %+).
3. **Plant `masters/opening_inventory.parquet`** (drops cure wait toward 28 h).
4. **Actually run optimizer** on Feb schedule to observe realistic gains (~5-10 % on changeovers).
5. **SimPy validate** N=200 (never yet exercised at scale).
6. **Golden-KPI regression tests** under `tests/replay/`.

---

## 11b. THREE COUNTING/RESERVATION DEFECTS FOUND AND FIXED (2026-08-09)

### 1. Changeover time never occupied the machine — HARD, fixed

`l7_pull_release.py` `_place` booked exactly `dur = qty x cadence` and tested only
interval OVERLAP, so two different GTs could sit back-to-back with a ZERO gap.
Setup was scored in `weighted setup h` and never happened on the timeline.

| | PCR Jul | TBR Jul | PCR Aug | TBR Aug |
|---|---|---|---|---|
| setup owed | 347.0 h | 169.0 h | 436.7 h | 119.3 h |
| **not reserved** | **155.0 h (45 %)** | **71.0 h (42 %)** | **184.6 h (42 %)** | **47.0 h (39 %)** |
| zero-gap transitions | 350 / 856 | 398 / 1,014 | 398 / 1,057 | 260 / 716 |
| machine-days over 24 h | **55 (16.3 %)** | 35 (12.5 %) | 59 (17.0 %) | — |

**Control that proved it real:** the plant's own July MES never exceeds 24 h on any
of its 341 PCR / 279 TBR machine-days (max 23.17 / 23.56 h under identical
arithmetic). The cadence was right; the plan was over-committed.

**Fix** `busy` now stores `(start, end, gt, rim)` and the backward walk requires
`gap >= setup(prev_gt -> gt)` on BOTH sides (a run may be inserted before an
existing one). It reserves the SHORTFALL, not a fixed block — ~55 % of owed setup
already fitted in a gap and does not move. **Price: PCR 97.1 -> 95.0 % (Jul),
95.0 -> 91.7 % (Aug); TBR 95.9 -> 95.2 % / 93.8 -> 92.7 %.** PCR same-size fell
91.8 -> 78.8 % on July: making room means moving machine, and moving breaks the
rim lock. **Do not recover this by relaxing a cap.**

### 2. Fulfilment counted output produced after month end — fixed

`qty_fed` had no horizon clip, so a campaign starting on day 30 and finishing next
month contributed its WHOLE quantity, while the denominator (`gross_build`) was
strictly this month's requirement. Numerator and denominator covered different
periods.

| | reported | **in-month** | overstated by |
|---|---|---|---|
| Jul PCR | 94.99 % | **94.57 %** | 1,687 tyres (17 campaigns cross) |
| Jul TBR | 95.17 % | **94.22 %** | 929 tyres (7) |
| Aug PCR | 91.67 % | **90.64 %** | 4,219 tyres (39) |
| Aug TBR | 92.72 % | **92.59 %** | 104 tyres (2) |

**Fix** `cure_campaigns_reconciled.parquet` gains `frac_in_month` /
`qty_fed_in_month` / `carry_out`; L11 grades on `qty_fed_in_month`.
**Opening stock is deliberately NOT clipped** — a tyre built last month and cured
this month is genuine output against this month's demand, and it is reported
separately in the KPI sheet so it is never mistaken for production.
**The carry-out ROWS stay in the schedule**: the press really is occupied into
next month, and deleting the row would let the next month double-book it.

### 3. `verify_export.py` checked the wrong property — fixed

* An **overlap check is not a feasibility check.** It passed the physically
  impossible plan above for the whole project. Now HARD-checks (a) every GT
  transition has a gap >= its own setup and (b) every machine-day fits
  production + setup in 24 h, with **hours clipped into the day they fall in** —
  bucketing a straddling run by its start day manufactures false >24 h findings.
* Its **"12 presses exceed 744 h" HARD finding was its own false positive**: it
  summed raw `end - start` without clipping to the horizon, so a carry-out
  campaign contributed its whole duration. A permanent false HARD hides real ones.
* Two more of its own bugs: the calendar month was used where the **plant** month
  (07:00 -> 07:00) was meant, and the headline lookup `metric == "tyres fed"`
  missed the actual label `"tyres fed (incl. opening stock)"` and silently
  compared against 0.

Both packs now verify **0 HARD / 0 SOFT / 0 EXPORT**.

### 4. `target` acts as a CEILING — REAL, measured, lever REVERTED

`l7_pull_release.py` Phase 2a cuts a run at `acc >= target` with
`target = max(min_lot x RUN_MULT, r_g x interval)`, so for a slow-drawing GT
`min_lot` is both floor and ceiling. Fingerprint: 119 TBR runs of exactly 87
(= 70 rounded up to the next 29-tyre slice), max-run-per-GT p10 85 / p25 87.
Binds 31.4 % of TBR volume, 11 % of PCR. **R5 is not the wall** — 0 of 92 GTs are
bound by `span_cap`, which would allow runs 3.0x / 3.1x larger.

`TARGET_CEIL_MULT` was added and swept. **On July, 2.0 looked free** (fulfilment
+0.05 pt, sub-floor 34.6 -> 30.6 %, +3 invariants). **On August the same setting
costs TBR 0.71 pt.** Net −521 tyres across the two months. **Reverted to 1.0.**
The defect is real and still open; this lever is not the fix, and a knob validated
on one month is not validated.

### 5. `min_demand_units` turned ON — {PCR: 300, TBR: 150}

`config.py:265`. Drops any GT whose whole-month demand is below the threshold.
**August: 25 GTs / 3,754 tyres** (PCR 20 / 3,362, TBR 5 / 392). **14 of those GTs
(2,243 tyres) the plant demonstrably built in May-Jul MES** — `GT 2157 ROYL KIA`
ran 21 separate days, `295/75R22.5JTHSD` 27. It is a DEMAND-side deletion, not an
economic lot decision. Volume is held under the residual policy, not lost.

### 6. Open data gap — 19 GTs have no rim

31,124 tyres (7.9 % of August PCR build) have no rim in `gt_size`, so they cannot
be rim-locked or partitioned. This is why August PCR same-size reads 65.9 % against
July's 91.8 %. **Master data we do not have — not an engine regression, and a later
same-size improvement must not be credited to the engine.**

---

## 11c. HORIZON POLICY — closed box (plant ruling, 2026-08-09), **SUPERSEDED BY `extend` (plant ruling, 2026-08-10)**

> ⚠ **STATUS CORRECTED 2026-08-19.** This section described `truncate` as SHIPPED.
> **It has not been the shipped default since 2026-08-10.** The plant ruling of
> that date replaced the closed box with a **split boundary**, and the code has
> read that way since: `l5_cure_master.py` ships
> **`PLANNER_HORIZON_MODE=extend`** with **`PLANNER_HORIZON_TAIL_H=72`** and
> **`PLANNER_CARRY_OUT=1`**. The authoritative write-up is the
> `---- SUPERSEDED BY "extend", PLANT RULING 2026-08-10 ----` block in
> `planner/cmbc/l5_cure_master.py`; this section is kept for the measurement
> below, which is still a valid comparison of the three *pre-`extend`* modes.
>
> **What actually changed.** The REPORTING rule is unchanged and still the
> plant's: a tyre counts for this month only if it is **cured inside the month**
> (`qty_fed_in_month`). What moved is the **PLANNING** horizon — a cure campaign
> may now start inside the month and finish up to 72 h past it. Two boundaries,
> deliberately distinct: `month_end` (reporting, never moves) and `horizon`
> (planning, `month_end + HORIZON_TAIL_H` under `extend`).
>
> **The defect `extend` fixes.** Under `truncate` a campaign is cut at hour 744
> and the press released, so nothing pulls building through the final ~25 h: PCR
> built 5,712 tyres on day 30 and **ZERO on day 31** while the plant runs flat to
> the last hour. Month-end GT collapsed to ~0 and the hand-off to next month was
> fictitious. **72 h is not a tuning knob** — a green tyre may be held exactly
> `GT_SHELF_LIFE_H = 72.0` (R5), so a cure seat further out can never be fed by
> in-month building. It is the largest tail that does any work and the smallest
> that does all of it.
>
> The carry-out tail is **not** counted as fulfilment; it is reported separately
> (see the 2026-08-10 reference-run table at the top of this file, row
> "incl. carry-out tail"). Deleting the tail does not move those tyres into the
> month — it deletes them: `HORIZON_MODE=strict` costs **−30,572 BUILT** on
> August and **−17,036 / −5,189** on July (PARTITION §4x).

> *"Only demand which is filled within the month time is considered fulfilled.
> After that, discard — that's unfulfilment."*

This overrode the rolling-horizon/carry-out design in §10c and
PARTITION §4h. It is a **business rule from the plant**, not an optimisation —
and it was itself amended by the plant the following day, as above. The
*reporting* half of it still stands unaltered.

`PLANNER_HORIZON_MODE` in `l5_cure_master.py`, three modes, all measured fresh:

| mode | Jul PCR | Jul TBR | Aug PCR | Aug TBR | out-of-month rows |
|---|---|---|---|---|---|
| `window` (old carry-out) | 94.6 % | 94.2 % | 90.6 % | 92.6 % | **28 / 14 — non-compliant** |
| `truncate` (**shipped 2026-08-09 → 2026-08-10 ONLY**) | 94.5 % | 94.5 % | 90.1 % | 92.7 % | **0 / 0** |
| `strict` | 93.5 % | 93.7 % | 89.4 % | 92.6 % | 0 / 0 |
| **`extend` + `HORIZON_TAIL_H=72` (SHIPPED, plant ruling 2026-08-10)** | — | — | — | — | tail rows are **legitimate**, not counted |

⚠ The winner of *this* table is `truncate`, and it was the shipped default for
**one day**. The table compares three closed-box modes against each other; it
does not contain the mode that actually ships. `extend` is not a fourth row of
the same experiment — it changes the planning boundary while leaving the
reporting boundary alone, so its fulfilment is measured on the same in-month
basis and its tail is reported separately. Do not read "94.5 % / SHIPPED" off
this table.

**`truncate` beat `strict` among the closed-box modes: identical compliance,
materially cheaper** (it was shipped on that basis on 2026-08-09 and superseded
by `extend` on 2026-08-10 — see the correction at the head of this section).
`strict` ("never start what cannot finish") gives up a further **1.0 pt** on July PCR,
0.8 pt on July TBR and 0.7 pt on August PCR by refusing work that could have been
delivered inside the month, and loses 2 invariants. Delivering the in-month
portion IS "demand filled within the month"; only the cut tail is unfulfilment.
An earlier note here guessed the two modes would converge — **they do not**.

**Cost of the boundary itself is small**: worst case August PCR −0.50 pt against
`window`, and July TBR actually *gained* 0.23 pt.

### Consequences, measured — do not fix these silently

1. **`PLANNER_CARRY_IN` is now unnecessary and the July→August inconsistency is
   gone.** Under `window`, July committed 24 campaigns / **805 press-hours** of
   August press time that August never reserved (0.75 % of its booked press
   time). Under `truncate` no press is booked past the horizon: verified
   0 campaigns ending after month end on both months.
2. **The month now ends EMPTY.** Last-day GT inventory PCR **508 → 0** (July) and
   **472 → 0** (August); TBR 184 → 60 and 27 → 19, against a G8 band floor of
   4,500 / 1,200. This is the closed box by construction: nothing is running at
   the boundary, so nothing is in stock. G8's "every day including the last" and a
   hard month boundary are **mutually exclusive** — the plant must choose.
3. **Tail capacity is NOT materially wasted by the rule** — it was already idle.
   Last 3 plant-days, press hours used: July PCR 74.1 % → 74.0 %, TBR 38.8 % →
   39.5 %; August PCR 82.7 % → 82.7 %, TBR 5.6 % → 5.6 %. Idle tail is
   1,607 h (PCR Jul), 3,440 h (TBR Jul), 1,074 h / 5,368 h (August). The boundary
   moved it by under 1 pt.
4. **New shortfall reason `remainder past horizon`** — the truncated tail, named
   separately from `past horizon`: July PCR 669 / TBR 776, August PCR 2,845.

**Opening stock still counts, and this is consistent with the ruling.** A tyre
built in June and cured in July fills July demand *in July* — the delivery is
in-month, only the build was not. It is reported separately in the KPI sheet
(`of which OPENING STOCK carried in`) so it can never read as this month's
production. Only the *tail* — output delivered after month end — is discarded.

---

## 11d. "EVERYTHING AVAILABLE AT t0" (plant ruling, 2026-08-09) — ALREADY TRUE

Plant: *"Assume everything is available for building from the very start — we
don't have to wait for anything."* Recorded as **B-ASSUME-1** in
`BUSINESS_RULES.md` §6b. Full measurement in `PARTITION_AND_CHANGEOVER.md` §4p.

**The engine already satisfied every clause.** Materials/components/compounds are
never referenced in `l6_build_gate.py` or `l7_pull_release.py` — L8 explodes them
*downstream* of L7, so material has never gated building at all. `busy = {}`
(l7) and `free.get(pr, t0)` (l5) mean machines and presses are free at hour 0.
Build starts at **t0 + 0.00 h**. There was no ramp to remove.

**Three numbers that started this work were all wrong, all the denominator class**
(§4d, §1e — now the third instance):

* `warehouse/derived/opening_gt_inventory.parquet` (PCR 6,960 / TBR 2,330) is a
  **December-31 snapshot**, not a per-month opening balance. The per-month master
  `masters/opening_gt/opening_gt_<M>.parquet` holds **4,820 / 1,297** for July.
* §4i's "opening stock 40 % / 54 % unused" divided July usage by that December
  denominator. Truth: **630 (13 %) / 287 (22 %)** unused — **87 % / 78 % consumed**.
* "Opening stock exists for most of what day 1–2 needs" measured whether the GT
  *holds* stock, not whether it is *spare*. **Spare = 0 on every GT with day-1
  shortfall**, both plants, both months.

**Both faithful implementations measured, both defaulted OFF** (flags
`PLANNER_FULL_AVAILABILITY_T0` / `_RAMP` / `_LADDER`; `off` reproduces the
pre-flag engine with 0 invariant flips):

| | Jul PCR | Jul TBR | Aug PCR | Aug TBR |
|---|---|---|---|---|
| **off (shipped)** | **96.95** | **95.56** | **94.89** | **97.19** |
| ramp (L5 partial-credit seating) | −0.12 | −0.42 | +0.23 | +0.72 |
| ladder (L7 exact per-tyre R5) | −0.09 | 0.00 | −0.13 | 0.00 |

The ladder replaced a MEDIAN-age screen — a textbook §1 defect — and moved the
opening-stock draw by **zero tyres** (PCR 4,190 / TBR 1,010 July, 4,523 / 1,018
August, identical in every arm). **The screen was never binding.** New DO-NOT #30:
verify a gate BINDS before building the exact version of it.

**What actually binds** (`runs/jul_diag`, `PLANNER_L7_DIAG=1`): 46 % of PCR and
52 % of TBR unfed volume is `cold` — the run's ideal start precedes t0 — and the
deficit is only **2.3 h / 3.3 h at p50**, max 4.2 h. That is a **carry-in gap of
a few hours**, not availability. Closing it means building before t0, which the
closed-box month (§11c) forbids and `verify_export.py` fails as HARD. **Two plant
rulings conflict; the plant must resolve it.** A *bounded ~4 h* pre-horizon
window has never been tried — the 72 h warm start that measured +0.05/+0.14 pt was
`PLANNER_DIAG_PRE_H`, diagnostic-only and not a runnable plan.

---

## 10r. CONSTRUCTION-CLUSTER WORKBOOKS — machine map proven, PCR claim corrected, both levers mixed-sign (2026-08-09)

Full ledger in [PARTITION_AND_CHANGEOVER.md](PARTITION_AND_CHANGEOVER.md) §4s.
Inputs: `SKU_Construction_Clusters_{PCR,TBR} (1).xlsx` at the workspace root.
New: `scripts/build_sku_con_cluster.py` -> `INPUT/derived/sku_con_cluster.parquet`;
`cluster_adj_pct` in `scripts/arm_kpi.py`; flags `PLANNER_CLUSTER_BUCKET_H` /
`PLANNER_CLUSTER_PLANTS` (L7) and `PLANNER_PART_SEED=wb`
(`build_gt_machine_partition.py`). Arms `cc_*_jul` / `cc_*_aug`, all fresh.

**Six things worth remembering:**

1. **The workbook machine codes `3401-3411` / `3801-3809` are in NO MES table.**
   They were DERIVED — workbook per-month machine column vs the GT's MES
   dominant machine, 222 (PCR) + 415 (TBR) GT-months — and the derivation is
   asserted at build time so a silent mis-map is impossible. Answer: identity,
   at 95.8-100 % purity, independently confirmed by rim signature.
   **`3407` is the exception: zero workbook rows, so it is assigned by
   ELIMINATION with no direct evidence.** TBMPCR7 carries R13; do not treat
   that one as proven.

2. **A CLUSTER OF N SKUs IS NOT N GREEN TYRES.** The workbook's unit is a SKU,
   the planner's is a GT (PCR 1.23 SKUs/GT, TBR 1.95). PCR's "134 clusters"
   collapses to 4 co-active multi-GT clusters covering 6.9 % of July demand;
   TBR's 32 collapse to 41 GTs / 81.6 %. **Always re-measure a supplied
   grouping in the planner's own key space before believing its size.**

3. **"PCR has no sister structure" (§4r.1) is CORRECTED.** The plant's
   same-cluster adjacency is 14.1 % against a 10.0 % within-machine permutation
   null (z = +7.5), and the realised gap is **2.4 min same-cluster vs 13.9 min
   different-cluster same-rim**. The structure is real. What is true is the
   weaker, operational claim: **it is redundant with the rim lock** — 33 of 37
   genuine PCR clusters are already single-machine single-rim.

4. **Average linkage leaves a dustbin, and a dustbin inverts the signal.** PCR
   cluster 132 holds 15 GTs across **all seven rims and seven machines**.
   Grouping on it would manufacture different-size changeovers. The builder
   drops any cluster spanning >1 rim; state such a rule rather than tuning
   around it.

5. **A supplied assignment that matches history is not a plan.** The workbook's
   `Assigned_Machine` matches the plant's own dominant machine on 88.9 % (PCR) /
   82.9 % (TBR) of GTs against our partition's 40.7 % / 31.4 % — and is
   **capacity-infeasible**, at 140-142 % on TBMTBR4 in both months, with
   TBMPCR7 at zero. It is also 93 %/84 % identical to `gt_home_machine`, i.e.
   the twice-rejected pin. Hence a tier-0 *seed* guarded by the existing
   free-hours test, never the partition itself.

6. **Mixed sign is the answer, and it is the MONTH that flips it.** TBR cluster
   bucketing: July −0.64 to −2.05 pt at every bucket from 2 h to 48 h, August
   +0.23 to +0.52. No bucket size is neutral in July, so this is not a tuning
   problem. Priced at the plant's own 8.25 min/transition, July 24 h buys
   **6.74 h of setup for 1,127 tyres**. And the best August arm (2 h, +0.49 pt)
   moves adjacency **not at all** (49.0 -> 49.0) — that gain is reordering
   luck, not the mechanism. Do not let a favourable headline stand in for the
   mechanism it is supposed to demonstrate.

---
## 12. Do NOT do

- **A supplied grouping must be re-measured in the planner's own key space
  before its size is believed.** A workbook "cluster of 2 SKUs" is very often
  ONE green tyre listed twice: PCR's 134 clusters are 4 usable ones covering
  6.9 % of July demand. Count in GTs that are CO-ACTIVE in the month planned.
- **Never let a heap key change SHAPE between entries.** Scoping a bucketed
  `_hkey` to one plant by returning a bare `datetime` for the others makes
  `heapq` compare a datetime against a tuple and raise. Out-of-scope entries get
  a sentinel INSIDE the tuple, so the key shape is uniform across the heap.
- **An OVERLAP check is not a FEASIBILITY check — reserve the resource, do not
  merely avoid collision.** Two runs of different GTs sitting back-to-back with a
  zero gap never overlap, and are still impossible. Cost: 45 % of all changeover
  time had nowhere to happen and 16 % of PCR machine-days needed >24 h, for the
  whole project, while both the planner gate and the independent verifier passed.
- **A block-size statistic is meaningless without its gap cutoff — sweep it and
  state it beside every number, on BOTH sides of a comparison.** "Plant p50 235,
  p10 1 tyre, 39.9 % sub-floor" was a 1 h cutoff on a within-month cure join. At
  a >=4 h cutoff on the full stream it is p50 363-381, p10 123, 12.7 %. The
  conclusion inverted: the plant runs LARGER blocks than we do.
- **Never let a numerator and denominator cover different periods.** `qty_fed` had
  no horizon clip while `gross_build` was strictly the month's requirement, so
  campaigns finishing next month inflated fulfilment by up to 1.04 pt.
- **A knob validated on ONE month is not validated.** `TARGET_CEIL_MULT=2.0`
  measured free-to-positive on July and cost TBR 0.71 pt on August. Sweep at least
  two months before shipping any shape knob.
- **Clip hours into the day/period they are actually spent.** Bucketing a
  straddling run by its START day, or a crossing campaign by its whole duration,
  manufactures false over-capacity findings. This bug appeared three times in one
  session: press capacity, machine-day feasibility, and setup attribution.
- ⚠ **CORRECTED 2026-08-19 — this entry said "the horizon is a CLOSED BOX... Do
  not reintroduce carry-out". THAT IS NO LONGER THE RULE and has not been since
  the plant ruling of 2026-08-10** (§11c, and the `SUPERSEDED BY "extend"` block
  in `planner/cmbc/l5_cure_master.py`). Carry-out was not "reintroduced" by
  mistake — **the plant asked for it**, and the shipped default is
  `PLANNER_HORIZON_MODE=extend` with `PLANNER_HORIZON_TAIL_H=72` and
  `PLANNER_CARRY_OUT=1`. **The old wording conflated three different boundaries.
  Separate them and both the old rule and the new one are true, of different
  things:**
    - **PLANNING horizon — NO LONGER a closed box.** It is `month_end + 72 h`.
      A cure campaign may start inside the month and finish in the tail. This is
      the half the old entry got wrong.
    - **REPORTING boundary — still the plant month, unchanged.** Nothing cured
      outside it counts toward this month's fulfilment (`qty_fed_in_month`).
      This is the half the plant never amended.
    - **EXPORT — still a closed box, and still HARD-gated.** Verified on
      `output/AUG2026_pack` 2026-08-19: sheets `1_build_schedule_shift` and
      `2_cure_schedule_shift` have max `end_ts` exactly 2026-09-01 07:00 = the
      plant month end, and **zero `carry_out` rows in any sheet that has the
      column** (1, 1b, 2, 2b, 7). `scripts/verify_export.py` HARD-fails any
      exported row starting or ending outside `[t0, t0+ndays)` and that check is
      LIVE — it does not consult `PLANNER_HORIZON_MODE`, so the export cannot
      silently grow a tail. **Do not weaken it.**
  So: carry-out in the PLAN is correct and was asked for; carry-out in the
  EXPORT is still a defect. A tail row inside the run artefacts is the press
  genuinely occupied into next month — deleting it would let next month
  double-book that press. **Anyone acting on the old wording would shorten the
  planning horizon, which does not move tyres into the month — it deletes them**
  (`strict` = −30,572 BUILT on August; PARTITION §4x).
- Don't recurse in DuckDB — will OOM.
- Don't `pandas.read_csv` any of the big files — use Polars `scan_csv` or DuckDB `read_csv_auto`.
- Don't `pip install` outside `.venv`.
- Don't assume `cycleStart` is cycle start — it's cycle END.
- Don't join `b.gt_code IS NOT NULL` (cartesian) — join by `productionID = gtbarCode`.
- Don't insert per-row into DuckDB in a Python loop for > 1 K rows — use Arrow register + bulk INSERT.
- Don't force a "primary mould per GT" — plant has multiple physical copies; use per-(plant, gt, press).
- Don't skip the `.DS_Store` cleanup before zipping.
- Don't let a **fractional quantity** reach the ledger. `int_ranges(0.5::BIGINT)`
  is an empty range, which becomes a NULL-timestamped event, which sorts ahead
  of everything and shifts every FIFO rank the verifier derives. This has now
  produced phantom `negative_gt` violations **twice** (1,113 and 236).
- Don't compute a **span** on the curing-first path. The horizon is a loop bound
  (§10c); measure fulfilment and the shift buckets instead.
- Don't index `mpm_rows[i]` off a candidate's position — score by `{machine: rule}`.
- Don't trust the v2 D-sweep as written; it has no interior minimum (§10c bug 2).
- Don't express a preference as a SORT and then select by a different key — the
  sort becomes dead code. L7 sorted candidates by continuity and picked by
  `(-wait, mach)`; continuity never decided anything (§10d defect 2).
- Don't broadcast a grouped aggregate back onto a non-unique key. `camp` has
  multiple rows per `(plant, gt_code, press)`; a left join from a grouped `fed`
  gave each the full quantity and invented 449 tyres (§10d defect 4).
- Don't net opening GT off the CURE requirement — it is upstream of the press and
  still has to be cured. It nets off the BUILD (§10d defect 7).
- Don't place a run all-or-nothing. Split and retry, or a shape constraint
  silently becomes lost demand (§10d defect 6).
- Don't target 0 % of runs below the B12 floor. The plant itself runs 12.7 % (PCR)
  and 30.8 % (TBR) below it (§10d).
- Don't quote a cure-campaign median without saying how interruptions were
  handled — strict consecutive runs give a TBR p50 of 1 (§10d).
- Don't allocate a GT as a rigid rectangle (`n_g` presses × the whole window).
  At 95 % press utilisation fat rectangles do not tile: 34 % of demand went
  unplaced with reason `no common window` (§10d). Rate anchoring is fine; the
  rigid single campaign per (GT, press) is not.
- Don't A/B a `PLANNER_*` env flag against an existing run directory. `RunContext`
  hashes the config but NOT flags read through `os.environ`, so two arms can be
  indistinguishable on disk. Run both arms fresh.
- Don't seed an arm with `cp -r` and re-run only L7 -- it inherits the previous
  arm's `l11_invariants.parquet` and 15 directories were affected at once. Use
  `scripts/run_arm.py`; gate on `scripts/check_arm_fresh.py`.
- Don't size a capacity exception off a NOMINAL load table when realised
  occupancy is 61-83 %. The binding constraint is temporal fragmentation.
- Don't write a measured-looking table into a code comment before running it.
- Don't gate one side of a bipartite assignment and call it feasibility. B16
  checked every GT had a machine, never that every machine had a GT (§4k).

---

## 13. Verification checklist (single end-to-end run)

```
make venv               # no-op if .venv/ present
make ingest bom construction balance
make learn              # ≥ 2 500 rules; ≥ 900 hard
make replay --limit 3   # 3 months processed
python -c "from planner.replay.full_kpi import compute_run; \
           from pathlib import Path; import os, json; \
           r=sorted(d for d in os.listdir('runs') if 'replay' in d)[-1]; \
           print(json.dumps(compute_run(Path('runs')/r), indent=2, default=str))"
# Expect: fulfillment 100 %, hard_violations 0, size_lock 100 % per month.
make test               # 11 pass
```
