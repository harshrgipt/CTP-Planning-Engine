# Tyre-Plant Production Planning Engine

Python engine that learns from **8 months of Smart MES history** and produces synchronized **Building + Curing schedules** for **PCR + TBR**. Every learned pattern is statistically validated. Every scheduled lot is explainable via a `decision_trace` referencing which rules it obeyed.

---

## 1. What this project does

**Input**
- 8 months of MES CSVs (~32 M rows): curing events, building events (stage 1 + stage 2), component consumption.
- BOM xlsx (PCR + TBR): hierarchical parent→child structure.
- SKU construction mapping xlsx: SKU→GT→component-slot map, cycle time, tyres-per-shift @100%.
- TBR uniformity/balance quality data.

**Output**
- **Building schedule** (per plant, per machine, per lot).
- **Curing schedule** (per press, per mould, per tyre).
- **KPI panel** per month with all primary + efficiency + hard-constraint metrics.
- **Rule base** (`rules.duckdb`) with hard/soft/statistical rules.
- **Explainability**: every lot carries a `decision_trace` naming the rules that chose it.

**Design principles**
- Learn plant behaviour from history — never hard-code assumptions when the data supports them.
- **Classical stats + pattern mining**, no ML.
- **Heuristic greedy + iterative local search (SA + Tabu + LNS)**, no CP-SAT.
- **Polars + DuckDB + Parquet warehouse**. No pandas in the hot path.
- Project-local `.venv` only — never install into system Python.

---

## 2. Folder structure

```
schedule/
├── curing/                              # raw MES CSVs (read-only)
│   ├── curingpcr.csv                    # 2.99 M rows
│   └── curingtbr.csv                    # 753 K rows
├── o_production/                        # raw MES CSVs
│   ├── tbmpcrstage1.csv                 # 2.99 M
│   ├── tbmpcrstage2.csv                 # 2.99 M
│   ├── tbmtbrstage1.csv                 # 754 K
│   └── tbmtbrstage2.csv                 # 753 K
├── io_production_consumption/           # raw MES CSVs
│   ├── tbmpcrstage1.csv                 # 517 K
│   ├── tbmpcrstage2.csv                 # 14.9 M  ← largest file
│   ├── tbmtbrstage1.csv                 # 6.36 M
│   └── tbmtbrstage2.csv                 # 102 K
├── bom_pcr_tbr/                         # xlsx BOMs
│   ├── jkt_bom_pcr 23.xlsx              # 46 K rows
│   └── jkt_bom_tbr 5.xlsx               # 83 K rows
├── Sku construction mapping/            # xlsx SKU→GT→slot maps
│   ├── SKU wise construction mapping PCR.xlsx
│   └── SKU wise construction mapping TBR.xlsx
├── masters/                             # (optional) plant-provided masters
│   ├── demand.csv|.parquet              # sku, due_date, qty, priority
│   ├── routing.csv|.parquet
│   ├── machine_master.csv|.parquet
│   ├── allowed_machine_matrix.csv|.parquet
│   ├── cycle_time.csv|.parquet
│   ├── setup_time.csv|.parquet
│   ├── calendar.csv|.parquet
│   ├── opening_inventory.csv|.parquet
│   ├── opening_wip.csv|.parquet
│   ├── moq_mpq.csv|.parquet
│   └── aging_rules.csv|.parquet
├── warehouse/                           # Hive-partitioned Parquet (created by `make ingest`)
│   ├── curing/plant={PCR,TBR}/date=YYYY-MM-DD/*.parquet
│   ├── building_output/plant=/stage={1,2}/date=/*.parquet
│   ├── consumption/plant=/stage=/date=/*.parquet
│   ├── bom/{bom_edges,bom_gt_map}.parquet
│   ├── construction/{construction_pcr,construction_tbr,construction_gt_template_tbr,test_skus_tbr}.parquet
│   └── balance/plant=TBR/balance_tbr.parquet
├── planner/                             # source
│   ├── config.py                        # pydantic-settings; thresholds, weights, paths
│   ├── data/
│   │   ├── ingest.py                    # CSV → Parquet warehouse (DuckDB stream)
│   │   ├── warehouse.py                 # DuckDB singleton + views (v_curing, v_build, …)
│   │   ├── bom.py                       # BOM xlsx → edges + GT map
│   │   ├── construction.py              # SKU→GT→slot xlsx → parquet
│   │   ├── balance.py                   # TBR balance sheets → parquet
│   │   ├── masters.py                   # pluggable loaders for masters/*.csv|.parquet
│   │   └── schema.py                    # canonical schemas
│   ├── learn/
│   │   ├── descriptive.py               # per-SKU / per-machine baselines
│   │   ├── machine_pref.py              # MPM + Wilson CI + chi²
│   │   ├── sister_sku.py                # Ward clustering + permutation search
│   │   ├── sequence_mining.py           # PrefixSpan on SKU chains
│   │   ├── timing.py                    # cycle + setup distributions
│   │   ├── aging.py                     # per-GT cure-lag p05/p50/p95
│   │   ├── balance_signal.py            # TBR quality rank → MPM demotion
│   │   ├── test_freq.py                 # test-SKU cadence
│   │   ├── calendar_infer.py            # idle-gap → shift boundaries
│   │   ├── violation_scan.py            # mould double-book, cure critical, quality flags
│   │   └── rule_extract.py              # orchestrator; runs all miners
│   ├── kb/
│   │   ├── rule_types.py                # Rule / RuleType pydantic
│   │   ├── rule_store.py                # DuckDB-backed rule table + JSON export
│   │   └── promoter.py                  # Wilson CI, chi², hard/soft/stat classifier
│   ├── plan/
│   │   ├── ledger.py                    # GreenTireLedger (DuckDB in-memory)
│   │   ├── component_ledger.py          # per-raw-component ledger + BOM leaf explosion
│   │   ├── demand.py                    # DemandMode {proxy_prev28, actual_month, master}
│   │   ├── lots.py                      # demand → lots (MOQ / historical mode)
│   │   ├── timing_lookup.py             # KB stat rules → cycle/setup lookups
│   │   ├── calendar.py                  # MachineTimer cursor
│   │   ├── decision_trace.py            # trace schema + writer
│   │   ├── rulekb.py                    # loads rules.duckdb into typed indices
│   │   ├── building.py                  # cluster-first greedy building planner
│   │   ├── inv_sim.py                   # GT + component ledger snapshots
│   │   ├── curing.py                    # weighted-CDF press + capacity balancer
│   │   └── sync.py                      # FIFO pairing build↔cure
│   ├── simulate/
│   │   ├── dist.py                      # truncated-lognormal fits
│   │   └── discrete_event.py            # SimPy, N=200 replications, multiprocess
│   ├── optimize/
│   │   ├── state.py                     # mutable Schedule + SortedList timeline
│   │   ├── objective.py                 # weighted score (Demand-first)
│   │   ├── neighbourhood.py             # SwapTwoLots, ShiftLotTime, ReassignMachine
│   │   ├── tabu.py                      # ring buffer
│   │   ├── lns.py                       # destroy 10% + greedy repair
│   │   ├── sa.py                        # simulated-annealing driver
│   │   └── driver.py                    # CLI-facing entry
│   ├── validate/
│   │   ├── violations.py                # independent hard-rule verifier
│   │   └── fuzz.py                      # adversarial-schedule generators
│   ├── replay/
│   │   ├── harness.py                   # walk-forward monthly replay
│   │   ├── kpi.py                       # per-run planner KPIs
│   │   ├── compare.py                   # planner vs historical actual
│   │   └── full_kpi.py                  # extended KPI panel (primary+efficiency+constraints)
│   ├── runs/
│   │   ├── run_context.py               # deterministic run_id + seed
│   │   └── logger.py                    # structlog JSON
│   └── cli.py                           # typer entry (ingest / learn / replay / plan / …)
├── runs/                                # every run's outputs
│   └── <run_id>/                        # e.g. 20260805T151900-learn-b8638a7c/
│       ├── config.json                  # snapshot of config
│       ├── rules.duckdb                 # (learn runs only) 2 500+ rules
│       ├── rules.json                   # human-readable export
│       ├── learn/{baselines,mpm,timing,sister,sequences,aging,violations,calendar}/*.parquet
│       └── month=YYYY-MM/               # (replay + plan runs) per-month outputs
│           ├── build_schedule.parquet   # scheduled lots with decision_trace
│           ├── cure_schedule.parquet
│           ├── gt_events.parquet        # full ledger stream (supply + cure)
│           ├── gt_daily_delta.parquet
│           ├── gt_final_balances.parquet
│           ├── component_events.parquet # BOM-exploded per-lot consumption
│           ├── component_final_balances.parquet
│           ├── component_starvation.parquet
│           ├── demand_shortfall.parquet
│           ├── kpi.json                 # primary KPIs
│           ├── compare.json             # planner vs actual month
│           └── violations.json          # hard-rule verifier output
├── tests/
│   ├── unit/                            # pytest — promoter, ledger, neighbourhood, fuzz
│   ├── fixtures/                        # (optional) 1-day CSV slices for smoke
│   └── replay/                          # golden KPI ranges
├── scripts/
│   └── bootstrap.sh                     # create .venv + pip install
├── Makefile
├── requirements.txt
├── pyproject.toml
├── .python-version
└── README.md
```

---

## 3. How to run from scratch

### 3.1 Prerequisites
- macOS or Linux
- Python 3.11 (see `.python-version`)
- ~10 GB free disk for the Parquet warehouse
- ~5 GB RAM

### 3.2 Full pipeline

```bash
# Step 1: create project-local .venv + install deps
make venv

# Step 2: ingest all raw CSVs and xlsx into warehouse/*.parquet (~15 s)
make ingest bom construction balance

# Step 3: learn rules from 8-month history (~17 min)
#   → produces runs/<run_id>-learn/rules.duckdb (2 500+ rules)
make learn

# Step 4: walk-forward replay for 3 months, comparing to actual (~7 min)
#   → produces runs/<run_id>-replay/month=YYYY-MM/{build,cure,kpi,compare}.json
make replay

# Step 5: (optional) run the optimizer on the latest plan (~10 min)
make optimize

# Step 6: (optional) SimPy stochastic evaluation, N=200 replications (~5 min)
make sim

# Step 7: (optional) independent verifier + fuzz suite
make validate

# One-shot: run the whole pipeline end-to-end
make all
```

### 3.3 Plan one month forward (production use)

```bash
# Uses the latest learn run + optional plant demand from masters/demand.parquet.
./.venv/bin/python -m planner.cli plan --month 2026-08
```

### 3.4 Unit tests

```bash
make test              # 11 unit tests, ~2 s
```

---

## 4. Result we got (3-month replay, Feb-Apr 2026)

### 4.1 Rule base learned

- **2 515 total rules**: **912 hard**, **616 soft**, **987 stat**
- Cycle-time distributions: 1 211 rules
- Machine Preference Matrix candidates: 322
- Sequence patterns (PrefixSpan): 705
- Sister-SKU clusters (Ward + permutation search): 60 groups
- Aging distributions: 189 rules
- TBR quality demotions: 1
- Test-SKU cadences: 27

### 4.2 KPI Panel per month

#### Primary objectives

| KPI | Feb | Mar | Apr | Actual |
|---|---:|---:|---:|---:|
| Demand qty | 450,939 | 487,480 | 436,693 | same |
| Demand fulfillment ↑ | **100 %** | **100 %** | **100 %** | 100 % |
| Building–Curing sync ↑ | 97.1 % | 96.9 % | 95.7 % | — |
| Makespan (h) ↓ | 762 | 900 | 753 | — |
| Machine util ↑ | 47.2 % | 42.9 % | 46.1 % | 95 % |
| Press util ↑ | 36.1 % | 29.3 % | 27.5 % | — |
| On-time production ↑ | 98.4 % | 98.3 % | 98.5 % | — |

#### Efficiency objectives

| KPI | Feb | Mar | Apr | Actual |
|---|---:|---:|---:|---:|
| Building changeovers ↓ | **1,464** | **1,524** | 1,534 | 1,487 |
| Curing changeovers ↓ | 254 K | 251 K | 228 K | — |
| GT aging p95 (hours) ↓ | 828 | 935 | 457 | 28 |
| Avg WIP ↓ | 1,036 | 1,064 | 726 | — |
| Machine idle (h) ↓ | **0** | **0** | **0** | — |
| Press idle (h) ↓ | 131 K | 207 K | 246 K | — |
| Daily production CV ↓ | 0.82 | 0.86 | 0.85 | — |

#### Hard constraints & business rules

| KPI | Feb | Mar | Apr |
|---|---:|---:|---:|
| **Hard violations = 0** | ✅ 0 | ✅ 0 | ✅ 0 |
| Machine–SKU stickiness | 59.8 % | 59.0 % | 56.1 % |
| Size lock | **100 %** | **100 %** | **100 %** |
| Avg SKUs / (machine × day) | 3.53 | 3.31 | 3.49 |
| Starvation events | 118 | 133 | 146 |
| Demand shortfall | 13,141 | 15,344 | 18,718 |
| **Wins vs history** (of 8 KPIs) | **3** | **3** | **2** |

### 4.3 Where the planner beats history

- **Building changeovers on Feb & Mar** — 1 464 / 1 524 vs actual 1 487 / 1 563 (better by 1.5–2.5 %)
- **Zero hard-rule violations** (mould double-book, machine overlap, negative GT)
- **Size lock 100 %** — sister-cluster ordering never mixes different sizes on the same run
- **Explainability** — every lot has a `decision_trace` with the rule IDs that chose it

### 4.4 Where the planner still loses (and why)

- **Machine util 45 % vs actual 95 %** — greedy leaves multi-hour gaps between lots. Real plant packs 24/7 across multiple shifts. Fix: continuous packing + shift calendar.
- **GT aging 828 h vs actual 28 h** — planner assumes near-zero opening WIP (only prior-month unmatched builds ≈ 13 K). Real plant has thousands of green tyres staged when a shift starts. Fix: plant-provided `masters/opening_inventory.parquet`.
- **Curing changeovers 250 K** — my curing planner interleaves tyres by (plant, GT) across presses. Real plant runs long campaigns per press. Fix: campaign-length rule inside curing planner.

### 4.5 Timings

| Step | Wall time |
|---|---|
| `make ingest` (32 M rows into Parquet) | 15 s |
| `make learn` (all miners) | 17 min |
| `make replay --limit 3` (3 months) | 7 min |
| `make plan --month YYYY-MM` (1 month) | 2–3 min |
| `make test` | 2 s |

---

## 5. When plant masters arrive

Drop any of these into `masters/`. The loader picks them up automatically and emits `runs/<id>/derived_masters/matched_vs_derived.json` diff so any deviation from the data-derived version is transparent:

```
masters/
├── demand.parquet
├── routing.parquet
├── machine_master.parquet
├── allowed_machine_matrix.parquet
├── cycle_time.parquet
├── setup_time.parquet
├── calendar.parquet
├── opening_inventory.parquet
├── opening_wip.parquet
├── moq_mpq.parquet
└── aging_rules.parquet
```

---

## 6. Make target reference

```
make venv            # create .venv + pip install requirements
make ingest          # CSV → warehouse
make bom             # xlsx BOM → parquet
make construction    # xlsx SKU-mapping → parquet
make balance         # xlsx balance sheets → parquet
make wip AS_OF=YYYY-MM-DD    # derive WIP snapshot from MES
make learn           # warehouse → rules.duckdb
make replay          # walk-forward across N months (default 3)
make plan            # produce a schedule (needs env MONTH=YYYY-MM)
make optimize        # SA + Tabu + LNS on latest plan
make sim             # SimPy N=200 stochastic
make validate        # independent verifier + fuzz
make smoke           # 1-day fixture end-to-end
make test            # pytest
make all             # venv + ingest + bom + construction + balance + learn + smoke + test
make clean           # remove tmp files (keeps warehouse + runs)
```

---

## 7. Config knobs (see `planner/config.py`)

```python
# Rule promotion thresholds
hard_confidence      = 0.995
hard_support_min     = 500
hard_exception_max   = 0.005
hard_p_value_max     = 0.001
soft_confidence      = 0.80
soft_support_min     = 100
soft_ci_low          = 0.70

# MPM candidate emission
mpm_preferred_p       = 0.60
mpm_preferred_ci_low  = 0.40

# Sequence mining
seq_min_support_frac = 0.01
seq_max_pattern_len  = 4

# Sister-SKU clustering
cluster_max_size_full_perm = 8
cluster_beam_width         = 200

# Optimizer weights — Demand-first
w_missed_demand   = 1000.0
w_changeover_min  = 1.0
w_wip_area        = 0.5
w_curing_wait     = 2.0
w_soft_penalty    = 5.0
w_stability_edit  = 0.5

# Global
seed        = 42
log_level   = "INFO"
```

Override any of these via env vars with prefix `PLANNER_TH_` / `PLANNER_W_`, e.g.

```bash
export PLANNER_W_MISSED_DEMAND=500
export PLANNER_W_CHANGEOVER_MIN=20
make plan
```

---

## 8. Architecture (data flow)

```
raw MES CSVs + xlsx BOM + xlsx construction + xlsx balance
        │
        ▼
   ingest (DuckDB streaming, Hive partitioned Parquet)
        │
        ▼
   warehouse/*.parquet  (32 M rows, day + plant + stage partitions)
        │
        ▼
   learn (Polars + scipy + prefixspan + sklearn)
   ├─ descriptive         ─┐
   ├─ machine_pref (MPM)   │
   ├─ sister_sku           │──► candidate rules
   ├─ sequence_mining      │
   ├─ timing               │
   ├─ aging                │
   ├─ balance_signal       │
   ├─ test_freq            │
   ├─ calendar_infer       │
   └─ violation_scan      ─┘        │
                                    ▼
                              kb/promoter (Wilson CI + chi²)
                                    │
                                    ▼
                              rules.duckdb (hard | soft | stat)
                                    │
                                    ▼
  replay / plan
  ├─ demand loader (proxy_prev28 | actual_month | master)
  ├─ lot batching
  ├─ building planner (cluster-first + sequence + MPM)
  ├─ inv_sim (GT ledger + BOM-explosion component ledger)
  ├─ curing planner (weighted-CDF press + capacity balancer + per-press mould)
  └─ sync (FIFO pairing build↔cure)
        │
        ▼
  optimize (SA + Tabu + LNS, delta-eval)
        │
        ▼
  simulate (SimPy, N=200 replications, KPI CIs)
        │
        ▼
  validate (independent hard-rule verifier + fuzz)
        │
        ▼
  runs/<run_id>/month=YYYY-MM/{schedules, kpi, compare, violations}.{parquet,json}
```
