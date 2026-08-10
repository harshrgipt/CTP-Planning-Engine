# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project root

The workspace root is a wrapper. **The actual project root is [schedule/send/](schedule/send/)** — `Makefile`, `pyproject.toml`, `planner/`, and all data directories live there. Every command below assumes you are in `schedule/send/`. `planner/config.py` derives `CONFIG.paths.root` from `Path(__file__).parent.parent`, so all data/warehouse/runs paths resolve relative to that directory regardless of cwd.

[schedule/send/BUSINESS_RULES.md](schedule/send/BUSINESS_RULES.md) is the plant's rulebook (46 numbered rules, B/P/C/S/G/E prefixes) with per-rule implementation status — check it before changing planner behaviour, and update the status column when you do.

Read [schedule/send/MEMORY.md](schedule/send/MEMORY.md) before doing anything non-trivial — it is a dense engineering log (bug ledger, do-not list, data lineage) that the code assumes you know.

[schedule/send/EXPERT_AUDIT.md](schedule/send/EXPERT_AUDIT.md) is an independent expert audit of the whole engine — **read it before the ledger**; it corrects four documented-but-wrong claims and records the four failure modes that produced them (aggregate metrics hiding per-segment regressions, one-bug-N-files, always-failing guards, unverified explanations).

[schedule/send/PARTITION_AND_CHANGEOVER.md](schedule/send/PARTITION_AND_CHANGEOVER.md) is the defect ledger for the build partition and changeover costs. **Read it before hardcoding any mined constant, before changing changeover/cadence/month-length arithmetic, and before running a month other than the one the partition was built for** (the partition must be rebuilt per month; a staleness guard refuses to use it otherwise). It records the recurring bug class in this codebase: a mined *median* wired in as a hard constraint, which cost 13.4 points of fulfilment across two separate instances before being found.

## What this is

A Python engine that learns plant behaviour from ~8 months / 32 M rows of tyre-plant MES history (Dec 2025 – Jul 2026) and emits synchronized **Building + Curing** schedules for two plants (PCR, TBR). Locked-in technical constraints, decided deliberately — do not substitute alternatives:

- **Classical statistics + pattern mining only.** No ML, no RL, no LLM.
- **Heuristic greedy + SA/Tabu/LNS.** No CP-SAT or MILP solver.
- **Polars + DuckDB + Parquet.** No pandas in the hot path.
- Python 3.11, project-local `.venv` only. Never `pip install` into system Python.

## Environment (Windows caveat)

`Makefile` (`SHELL := /bin/bash`, `PY := $(ROOT)/.venv/bin/python`) and `scripts/bootstrap.sh` are POSIX-only. On this Windows machine the venv interpreter is `.venv\Scripts\python.exe` and `make` may be unavailable — run the CLI module directly instead:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m planner.cli <command>
```

`.venv/` is not present in a fresh checkout; the raw MES CSVs (~4.4 GB) and two pre-shipped runs under `runs/` are.

## Commands

Pipeline order matters — each stage consumes the previous stage's artifacts.

```bash
make venv                                # bootstrap .venv
make ingest bom construction balance     # raw CSV/xlsx -> warehouse/*.parquet   (~15 s)
make learn                               # warehouse -> runs/<id>-learn/rules.duckdb (~17 min)
make replay                              # walk-forward 3 months vs actual        (~7 min)
make test                                # pytest, ~2 s
make all                                 # venv + ingest + bom + construction + balance + learn + smoke + test
make smoke                               # 5k-row-per-CSV end-to-end sanity check
```

**Make targets accept no CLI flags** (`make replay --limit 3` passes `--limit` to make, not to the CLI). For anything parameterized, and for `plan` / `wip` (which have *required* options that the make targets do not supply, so `make plan` and `make wip` always fail), invoke the module:

```bash
./.venv/bin/python -m planner.cli plan --month 2026-08 --demand-mode proxy_prev28
./.venv/bin/python -m planner.cli replay --months 2026-02,2026-03 --limit 2
./.venv/bin/python -m planner.cli optimize --budget 600
./.venv/bin/python -m planner.cli sim --n-reps 200
./.venv/bin/python -m planner.cli validate --fuzz
./.venv/bin/python -m planner.cli wip --as-of 2026-08-01
```

Tests (`pytest`, `testpaths = ["tests"]`, `-q --strict-markers`):

```bash
./.venv/bin/python -m pytest tests/                                # all
./.venv/bin/python -m pytest tests/unit/test_ledger.py             # one file
./.venv/bin/python -m pytest tests/unit/test_ledger.py::test_ledger_balance   # one test
```

Lint config is `ruff`, line-length 110, target py311 (not currently wired into a make target).

## Architecture

Four layers, each communicating only through **files on disk**, never in-process state:

```
raw CSV/xlsx → warehouse/*.parquet → runs/<id>-learn/rules.duckdb → runs/<id>-{replay,plan}/month=YYYY-MM/*
```

**1. `planner/data/` — ingest.** `ingest.py` streams each MES CSV through a single DuckDB `COPY … PARTITION_BY (date, stage, plant)` — no frame is ever materialized in Python. Identifier columns are force-cast to VARCHAR at read time (`read_csv_auto(..., types={...})`) to prevent schema drift across daily partitions and preserve leading zeros in `productionID`. `warehouse.py` is a process-wide DuckDB singleton that registers `v_curing`, `v_build`, `v_consume`, `v_bom`, `v_bom_gt`, `v_construction_*`, `v_balance` over the Parquet globs; a view is silently skipped if nothing has been ingested. `masters.py` is a stub-first loader: every plant master is optional and returns an empty *typed* frame with a warning when absent, so downstream code needs no refactor when real files land in `masters/`.

**2. `planner/learn/` + `planner/kb/` — rule mining.** `rule_extract.run_learn()` is the orchestrator: it runs every miner (`descriptive`, `machine_pref`/MPM, `sister_sku`, `sequence_mining`, `timing`, `aging`, `balance_signal`, `test_freq`, `calendar_infer`, `violation_scan`), concatenates the candidate `Rule` objects, and passes them through `kb/promoter.promote()`. The promoter is the single classification chokepoint — Wilson CI + chi² against thresholds in `config.Thresholds` decides `hard` / `soft` / `stat` and assigns `weight` (hard=1.0, soft scales with confidence, stat=0.0). Everything is persisted to `rules.duckdb` (table `rules`) plus a human-readable `rules.json`.

**3. `planner/plan/` — scheduling.** `plan/rulekb.load_rules()` reads `rules.duckdb` back into typed in-memory indices (`RuleBase.mpm`, `.sisters`, `.sequences`, `.cycles`, `.aging`, …), dispatching on the `scope` column, and applies quality demotions by halving MPM weights. Then:
- `building.plan_building()` — cluster lots by sister-SKU (canonical order from the KB), reorder across clusters using mined `sequence` rules, pick a machine by `argmax(MPM confidence × weight × 1000 − hours-until-free)`, and place in time with `MachineTimer`.
- `curing.plan_curing()` — fully vectorized Polars: expand GT events to one row per tyre, pick a press via a weighted-CDF `join_asof` over historical usage, then spill overflow to the next-preferred press when a press-day exceeds its historical p95 capacity.
- `sync.sync()` — vectorized FIFO pairing of build↔cure by rank within `(plant, gt_code)`; a cure is pushed to `max(cure_ts, supply_ts)`, and cures with no matching supply are deleted from the ledger and written to `demand_shortfall.parquet`. One pass suffices for correctness (`max_iters` is API compat only).
- `inv_sim.simulate()` — persists the GT ledger stream and the BOM-exploded component ledger.

**4. `planner/optimize/`, `simulate/`, `validate/`, `replay/`.** SA + Tabu + LNS over `Schedule` (`optimize/state.py`) scored by `optimize/objective.py`; SimPy replications; and — importantly — `validate/violations.py` is an **independent verifier that must not call planner internals**. It re-derives per-tyre supply from the schedule parquets and checks machine overlap, mould double-booking, and negative GT from scratch. Keep it that way.

### Cross-cutting concepts

- **The run directory is the unit of output.** `runs/run_context.RunContext.new(tag=...)` mints `<YYYYmmddTHHMMSS>-<tag>-<config-sha1[:8]>`, creates the directory, snapshots `config.json`, and seeds `random`/`numpy` from `CONFIG.seed` (42). Downstream commands find inputs by **substring-matching the run_id and taking the lexically last one** — `_latest_learn_run()` matches `"learn"`, while `optimize`/`sim`/`validate` match `"plan-"`. Consequence: those three commands will **not** pick up a `replay` run; pass `--run-id` explicitly, or run `cli plan` first. Changing the tag naming scheme breaks this discovery.
- **The ledger is the source of truth for inventory**, not the schedule frames. `plan/ledger.GreenTireLedger` is an in-memory DuckDB table `gt_events(ts, plant, gt_code, qty_delta, source, lot_id)`: building writes `+1` per tyre at `setup_end + i × cycle_s` (not one credit per lot), curing writes `-1`, and starvation is a window-function scan for a negative running balance. KPIs and the verifier both read the persisted `gt_events.parquet` rather than recomputing from lot end timestamps.
- **Explainability is mandatory.** Every scheduled lot carries a `decision_trace` (`plan/decision_trace.py`) naming the rule IDs that chose it, their type, and their role. New scheduling decisions must add a reason.
- **Demand mode is the replay/production distinction.** Replay uses `DemandMode.ACTUAL_MONTH` (fair backtest against that month's real output); forward `cli plan` defaults to `PROXY_PREV28` so no future information leaks in.

### Configuration

`planner/config.py` is pydantic-settings. Override anything via env var with the matching prefix — `PLANNER_` (paths, `seed`, `log_level`), `PLANNER_TH_` (rule-promotion thresholds), `PLANNER_W_` (objective weights):

```bash
PLANNER_W_MISSED_DEMAND=500 PLANNER_W_CHANGEOVER_MIN=20 make plan
```

Because `RunContext` hashes the full config into the run_id, changing any knob produces a distinct run directory automatically.

Logging is structlog JSON to stdout via `planner/runs/logger.py`; use `log.info("dotted.event.name", key=value)` — event names are dotted `module.action` strings and are grepped in practice.

## Data lineage (memorize before touching queries)

- Build **stage 2** `itemCode` is the **GT code** (green-tyre spec, e.g. `"GT 1402 XPC TATA"`), *not* the finished SKU. Finished SKU → GT comes from `v_bom_gt`.
- Build stage 2 `productionID` is a **per-tyre barcode** (VARCHAR with leading zeros) and equals curing's `gtbarCode`. **Always join `v_build.productionID = v_curing.gtbarCode`** — 99.6 % hit rate. Never join on `b.gt_code IS NOT NULL` (cartesian; this was a real bug).
- Curing `cycleStart` is **press-open, i.e. the cycle END**; `event_ts` is press-close. Duration = `cycleStart − event_ts` ≈ 1955 s median. The source data is named backwards — do not "fix" it.
- Curing `wcID` is the press id (int in source, cast to VARCHAR everywhere).
- The TBR construction-mapping xlsx has physical sheet names that differ from logical ones: real data is at logical `Sheet4` (Material→GT→slots), `Sheet6` (GT template), `Sheet1` (test SKUs), and `Before`/`After`/`Sheet3` (balance).

## Hard-won constraints — do not regress

These are fixes for bugs that already cost real debugging time (full ledger in `MEMORY.md` §8/§12):

- **Never write a recursive DuckDB CTE over the BOM.** It OOMs at ~29.5 GB tempdir. `plan/component_ledger._precompute_gt_leaves()` walks the graph once in Python with a visited-set and memoizes per-GT leaf multipliers; per-lot explosion is then a dict lookup.
- **Never insert row-by-row into DuckDB for >1 K rows.** Build a Polars frame, `con.register(name, df.to_arrow())`, then one bulk `INSERT … SELECT`, then `unregister`. This pattern is used identically in `building.py`, `curing.py`, `sync.py`, and `component_ledger.py`; 400 K per-row Pydantic inserts previously OOM'd.
- **Filter on the Hive partition column `date`, not `event_ts`,** when you want partition pruning (`WHERE date < ?::DATE`). Filtering `event_ts` scans everything and hangs on the 14.9 M-row PCR stage-2 consumption file.
- **Moulds are per `(plant, gt_code, press)`**, labelled `<mould>@<press>`. Each press holds its own physical mould copy; forcing one primary mould per GT produced 416 K phantom double-book violations.
- Do not read the big CSVs with pandas — use Polars `scan_csv` or DuckDB `read_csv_auto`.
- Treat everything in `curing/`, `o_production/`, `io_production_consumption/`, `bom_pcr_tbr/`, and `Sku construction mapping/` as **read-only**. `warehouse/` is fully regenerable via `make ingest` and should not be committed.

## Verifying a change end-to-end

After `make learn` + a replay/plan run, the expected invariants are: ≥ 2 500 rules with ≥ 900 hard, demand fulfillment 100 %, `violations.json` → `n_hard == 0`, size lock 100 %. Extended KPIs come from `planner/replay/full_kpi.compute_run(Path("runs")/<run_id>)`.

Known open gaps (deliberate, documented in `MEMORY.md` §11 — do not treat as bugs to be silently "fixed"): machine utilization ~47 % vs 95 % actual (greedy leaves inter-lot gaps; needs continuous packing), GT aging p95 far above actual (needs `masters/opening_inventory.parquet`), and high curing changeovers (needs a campaign-length rule in `plan/curing.py`).
