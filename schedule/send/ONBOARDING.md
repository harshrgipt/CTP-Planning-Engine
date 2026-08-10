# Onboarding — how to run this project

## 1. Drop your raw MES CSVs into these empty folders

```
curing/                      → curingpcr.csv, curingtbr.csv
o_production/                → tbmpcrstage{1,2}.csv, tbmtbrstage{1,2}.csv
io_production_consumption/   → tbmpcrstage{1,2}.csv, tbmtbrstage{1,2}.csv
```

The xlsx BOMs and SKU construction mapping are already included in `bom_pcr_tbr/`
and `Sku construction mapping/`.

## 2. Bootstrap Python

```bash
make venv                  # creates ./.venv and pip-installs requirements.txt
```

## 3. Build the warehouse

```bash
make ingest bom construction balance   # ~15 s on the full 8mo dataset
```

## 4. Learn the rulebase

```bash
make learn                 # ~17 min — mould self-join dominates
```

## 5. Replay past months to compare vs actual

```bash
make replay                # 3 months by default, ~7 min
```

Outputs land at `runs/<run_id>/month=YYYY-MM/`:
- `build_schedule.parquet`
- `cure_schedule.parquet`
- `kpi.json`, `compare.json`, `violations.json`
- `gt_events.parquet`, `component_events.parquet`, `demand_shortfall.parquet`

## 6. Plan a new month forward

```bash
./.venv/bin/python -m planner.cli plan --month 2026-08
```

## 7. What is already included in `runs/`

Two representative runs are pre-shipped so you can inspect KPI outputs before
running anything yourself:
- `20260805T151900-learn-…/` — full 8mo learn run, 2 515 rules
- `20260805T171516-replay-…/` — 3-month replay (Feb-Apr 2026), 0 hard-rule
  violations, 100% demand fulfillment, 3 wins/8 vs history on Feb & Mar

## 8. Read next

- `README.md` — full architecture, KPI panel, folder guide
- `MEMORY.md` — dense engineering memory: bug ledger, do-not lists, lineage
  bridges, key facts

## What NOT to do

- Do not `pip install` outside `.venv`.
- Do not modify the raw CSVs — treat them as read-only.
- Do not recurse in DuckDB — will OOM at ~30 GB tempdir (see MEMORY §12).
- Do not commit the `warehouse/` directory (regenerable via `make ingest`).
