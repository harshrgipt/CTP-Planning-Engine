# APPROACH — the CTP planner, end to end

**What this document is.** The single place that says *what the engine does, what
it reads, what is hardcoded, and why each choice was made*. Written 2026-08-19
from the code, not from the older design documents — several of those describe
layers that no longer run.

**Read this before `MEMORY.md` or `PARTITION_AND_CHANGEOVER.md`.** Those are
ledgers of what went wrong; this is the design they are ledgers of.

---

## 0. THE ONE-LINE IDEA

> **Curing proposes, building disposes.**

A cure campaign is placed on a press first. Building is then released *backwards*
from it:

```
release = t_cure - tau - build_duration
```

Not `cure_ts = max(cure_ts, supply_ts)`. That inversion — building emits when it
can and curing waits — is the defect the whole architecture exists to fix.

**Why curing-first, and not building-first.** Two independent routes agree:

| route | evidence |
|---|---|
| the plant's own rulebook | `Building Business Rules.docx` orders capacity → cure quantity → build quantity (steps 3-5) |
| 8 months of MES | cure campaigns **58.5 h PCR / 210.7 h TBR**, fed by build campaigns of **7.6 h / 5.4 h**, same-day correlation **r = 0.92 / 0.94** |

Presses set the rhythm. A building-first engine would produce green tyres the
presses cannot consume — and green tyres **expire in 72 hours** (R5), so
over-production is not inventory, it is scrap.

**GT inventory is a synchronization buffer, never a production target.**

---

## 1. THREE ENGINES EXIST. ONE RUNS.

| tree | status |
|---|---|
| **`planner/cmbc/`** | **LIVE.** Driven by `main.py`, `scripts/run_arm.py` |
| `planner/engine/` | superseded curing-first prototype (P0–P9) |
| `planner/plan/`, `learn/`, `kb/`, `optimize/`, `replay/`, `simulate/`, `cli.py`, `Makefile` | **RETIRED gen-1.** Kept only because `planner/data/` and `planner/config.py` are shared |
| `planner/cmbc/_retired/` | verified: nothing reads their output |
| `planner/cmbc/_offline/` | L0/L2/L3 — need the raw MES; their outputs are committed |

Three separate sessions reasoned about the wrong tree before this was written
down. If you are editing `plan/building.py`, `plan/curing.py` or `plan/sync.py`
for a scheduling change, you are in the retired tree.

---

## 2. INPUTS — everything the engine reads

All input lookups go through **`planner/paths.py`**. Do not hand-write
`ROOT.parent.parent / ...`. `PLANNER_DATA_ROOT` relocates the whole `INPUT/` tree.

### 2a. The two derived roots are SEPARATE, deliberately

`input_derived()` and `wh_derived()` have **no cross-fallback**. 21 filenames
exist in both `INPUT/derived/` and `warehouse/derived/`; twenty are byte-identical
and **`press_mould_change.parquet` is not**. L5 and L10 read the warehouse copy.
Merging the two resolvers would silently change the cure schedule. Locked by
`tests/unit/test_paths.py`.

### 2b. Required inputs — preflight refuses to plan without these

| input | resolver | what it carries |
|---|---|---|
| `demand_<M>` | `paths.demand` | the month's order book (plant, gt_code, sku, qty, month, due_date, day) |
| `opening_gt_<M>` | `paths.opening_gt` | one row per green tyre on the floor at t0, with `age_h` |
| `params_*.json` | `paths.WH_PARAMS` | tau\*, tau_min, cure_yield, availability — mined by L0 |
| `cap_machine_<M>` | `wh_derived` | GT x building-machine eligibility |
| `cap_press_<M>` | `wh_derived` | GT x press eligibility |
| `cap_mould_<M>` | `wh_derived` | mould counts per GT (the R3 ceiling) |
| `cap_ttl_groups_<M>` | `wh_derived` | the B16 TT/TL machine split |
| `cap_changeover` | `wh_derived` | per-machine same/different-size changeover minutes |
| `l3_cavities` | `wh_derived` | per-press cavities and cycle seconds |
| `press_mould_change` | `wh_derived` | mould-change durations |
| `plant_ct_build` | `wh_derived` | the plant's own per-GT build cycle time |
| `plant_ct_cure_gt` | `wh_derived` | the plant's own per-GT cure cycle time |
| `cycle_time_building` | `wh_derived` | per-machine seconds per tyre |
| `allowed_press_matrix` | `wh_derived` | the plant's hard press allowable list |
| `gt_size` | `input_derived` | GT → rim |
| `tt_tl` | `input_derived` | SKU → tube-type / tubeless |
| `allowed_machine_matrix` | `input_derived` | the plant's hard machine allowable list |
| `pcr_inch_eligibility` | `input_derived` | PCR machine inch windows |
| `gt_machine_partition` | `input_derived` | the static GT → machine map (month-stamped) |
| `press_list_<M>` | `paths.press_list` | the press roster |

**Why these are ERRORs and not warnings:** each one degrades *silently* when
absent. `allowable.restrict()` responds to a missing matrix by printing one line
and returning the frame **unrestricted** — the plan would use roughly twice the
machines the plant permits and still pass every other gate. A plan the floor
cannot run is worse than no plan.

### 2c. Optional inputs — the layer runs without them, but says so

`gt_sister_group` · `sku_con_cluster` · `gt_home_machine` · `l3_ceiling_<M>` ·
`running_moulds_<M>` · `machine_warm_<M>` · `carry_in_<M>`.

Each is read behind an `.exists()` test. Preflight emits a WARN when one is
missing, because *a gate that silently stops applying is a different gate from
the one that was measured*.

`machine_rim_lock` and `machine_gt_share` are required **only** when their STRICT
flag is on — otherwise the restrict functions return before touching the file.

### 2d. What is NOT an input

The raw MES (~4.4 GB: `curing/`, `o_production/`, `io_production_consumption/`,
`bom_pcr_tbr/`, `Sku construction mapping/`) is **read-only and gitignored**.
Everything mined from it is committed, so a prepared month replans end-to-end from
a fresh clone. Only `main.py rebuild` (L0/L2/L3) needs the drop.

---

## 3. THE FLOW

Phase A decides **what and how much** — no time, no machine.
Phase B decides **where and when**.

Every layer communicates **only through files on disk**, never in-process state.
That is what makes a single layer re-runnable and every number traceable to a
parquet.

### PHASE A

| step | module | does | writes |
|---|---|---|---|
| `00_preflight` | `l1_preflight` | the input gate — 20 required entries, 16 ERROR paths, exit 1 on any | `preflight_<M>` |
| `00a_l2_ttl_b16` | `l2_ttl` | B16 TT/TL split of the 9 TBR machines. **All 510 partitions searched**, ranked under a capacity gate | `cap_ttl_groups_<M>`, `b16_machine_reach_<M>` |
| `01_l4` | `l4_net_requirement` | `net_cure = demand - FG - usable GT` · `cure_requirement = ceil(net_cure/yield)` · `gross_build = cure_requirement - from_stock` | `net_requirement_<M>` |
| `02_l45` | `l45_lotsize` | **cure** lot sizing — mould-set aggregation, sister consolidation, cavity multiples, 72 h cap | `l45_lots_<M>` |
| `02b_l4b` | `l4b_capacity_flow` | max-flow feasibility: can building feed this month at all? | `l4b_flow_*` |
| `03_partition` | `scripts/cpsat_partition.py` | static GT → machine map, CP-SAT, proven optimal | `gt_machine_partition` |

**Two quantities, not one, and they are not interchangeable:**

```
cure_requirement   green tyres the PRESSES must consume  = ceil(net_cure / yield)
gross_build        green tyres BUILDING must make        = cure_requirement - from_stock
```

Opening stock changes **who supplies** a press cycle, never **how many cycles
exist**. `cure_requirement` did not exist until 2026-08-18; before that every cure
consumer reached for `gross_build`, and the cure plan was short by exactly
`from_stock` every month on both plants — 4,820 PCR / 1,251 TBR on July.

### PHASE B

| step | module | does | writes |
|---|---|---|---|
| `04_l5` | `l5_cure_master` | **the plan is born here.** Every L4.5 lot becomes a campaign: mould-set x press x [t_start, t_end]. Building enters nowhere | `cure_campaigns` |
| `05_l7` | `l7_pull_release` | pull release of building, ~4,000 lines. Three levels: cure campaign → build run → build slice | `build_schedule`, `gt_events`, `build_starved`, `carry_forward_gt`, `carry_out` |
| `06_l10` | `l10_discretise` | continuous time → shift grid (A 07-15, B 15-23, C 23-07). **Last**, so tau\* is never quantised | shift rows, `mould_changes` |
| `07_l11` | `l11_validate_plan` | **48 invariants** | `l11_invariants` |
| `08/09` | export | GT → SKU share, BTP pack | `output/` |

**Why discretise last.** L5–L7 plan in continuous time because the pull equation
is continuous. Rounding to shifts before the coupling is solved would quantise
tau\* itself and destroy the buffer the architecture is built around.

**The plant day is 07:00 → 07:00.** `date` in exports is the *plant-day*, not the
wall-clock day. Labelling C shift by wall clock once mislabelled 28.7 % of build
rows.

---

## 4. HARDCODED VALUES — every one, and where it lives

> **The rule: add a cap in `config.py` or nowhere.** A duplicated rail value was a
> real bug — L7 enforced 1,400 while L11 graded against 1,500.

### 4a. In `config.py` (the correct home)

| cap | value | read by | why |
|---|---|---|---|
| `min_lot_units` (B12) | PCR 150 / TBR 70 | l7, l11, l45 | plant instruction: a build run below this is not worth a setup |
| `min_demand_units` (B12) | PCR 300 / TBR 150 | l4 | plant instruction, Aug 2026. **Note it is 2x the lot floor** — a GT can be refused at a quantity that would form one legal lot |
| `gt_wip_rail` (G8) | PCR 4,800 / TBR 1,400 | l7 rail, l11 | enforced GT stock ceiling |
| `gt_wip_rail_margin` | 0.94 | l7 `_cap_ok` | |
| `gt_wip_min/max` (G8 band) | 4,500–4,800 / 1,200–1,500 | l11 | reported, wider than the enforced rail. **Sitting below is intentional** |
| `plant_co_per_machine_day` | 2.66 / 3.56 | l11 gate | the plant's own benchmark |
| `plant_weighted_co_min_per_machine_day` | 74.0 / 35.6 | l11 gate | the plant's own benchmark |
| `GT_SHELF_LIFE_H` (R5) | **72.0 — module constant, NOT env-overridable** | l7, l11 | import it, never re-declare `72.0` |
| `PRESS_ROSTER` | PCR 86 / TBR 80 | scorecard, l11 | plant ruling. Four masters say 92/80 — they are stale |

### 4b. Enforced limits that live OUTSIDE `config.py` — a known violation of the rule

| constant | where | value | note |
|---|---|---|---|
| `LOAD_CAP` | `l2_ttl.py` | 0.95 of **calendar** hours | the B16 group load gate |
| `UTIL` | `cpsat_partition.py` | 0.95 of calendar hours | **the second 0.95, on a different basis** — the same August TBR group reads 92.9 % in one and 97.4 % in the other |
| `IMB_CAP_H` | `cpsat_partition.py` | 8 h within a B16 group | **measured binding on 2 of 4 (plant, month) cells** — it shapes the optimum, and the value itself is untested |

### 4c. Mined values used as parameters (not caps)

`tau*` (PCR 4.32 h / TBR 4.81 h) and `tau_min` (0.268 h) come from
`params_*.json`, mined by L0 over 8 months. `cure_yield` is **one number per
plant** — PCR 0.99712, TBR 0.98202.

> **A mined statistic is not a constraint.** `tau*` and `min_lot` were both plant
> *medians* wired in as hard floors; together they cost **13.4 points** of
> fulfilment before it was found. The tell is a flat quantile band — identical
> p01/p05/p10 means you built a wall, not a distribution.
>
> This is why `PLANNER_TAU_RELEASE` defaults to `min`: the release floor is the
> physical minimum (16 minutes), not the plant's median.

---

## 5. FLAGS — 122 live, and what that means

Two mechanisms, and they behave differently:

**`planner/config.py`** is pydantic-settings — `PLANNER_` (paths, seed, log_level),
`PLANNER_TH_` (thresholds), `PLANNER_W_` (weights). `RunContext` hashes this into
the run_id.

**The cmbc shape flags** are raw `os.environ.get` reads inside the layer modules.
**`RunContext` does not hash them.** That is exactly why A/B-ing against an
existing run directory is forbidden.

### The defaults that matter most

| flag | default | why |
|---|---|---|
| `PLANNER_PARTITION_PLANTS` | `PCR` | TBR is already at 100 % same-size; partitioning it fails the two-month gate on BUILT (−168 Jul / +174 Aug) |
| `PLANNER_HORIZON_MODE` | `extend` | plant ruling 2026-08-10. `strict` costs 17,036 PCR BUILT |
| `PLANNER_HORIZON_TAIL_H` | `72` | plan 72 h past the month, report only the month |
| `PLANNER_TAU_RELEASE` | `min` | see 4c |
| `PLANNER_STRICT_ALLOWABLE` | `1` | the plant's machine list is HARD |
| `PLANNER_STRICT_LOT_FLOOR` | `1` | B12 is never breached to buy a placement |
| `PLANNER_CLUSTER_SEQ` | `1` | on by plant instruction; **the PCR cost (−0.6 / −0.7 pt) is accepted, not free** |
| `PLANNER_CARRY_OUT` | `1` | a campaign may finish past the boundary; the tail goes to `carry_out.parquet` |
| `PLANNER_CAD_BASIS` | `machine` | prefer the plant's own per-GT cycle time, then the mined per-machine cadence |
| `PLANNER_L7_PINNED_FIRST` | `0` | PCR-positive on both months, but TBR flips sign — fails the gate |

**122 flags is itself a finding.** Most default off, and most were measured
negative. The full measured table is `PARTITION_AND_CHANGEOVER.md` section 5.

---

## 6. WHY THESE TECHNIQUES AND NOT OTHERS

**Classical statistics and pattern mining. No ML, no RL, no LLM.** The plan must
be explainable to a planner who has to defend it on the floor.

**Heuristic greedy + a CP-SAT partition.** CP-SAT is used for **one** decision —
which machine builds which GT — because that model is small (~41 GTs x 11
machines), closes to proven optimality, and **never touches a clock**. Pointed at
the scheduling problem itself it failed: a from-scratch joint build+cure model
reached only 77.8 % against greedy's 96 %, and could not find a first solution in
420 s. More freedom without guidance made it monotonically worse.

**The dominant empirical rule of this engine:**

> **Reallocate across resources at fixed times. Never re-time work.**
>
> Eleven timing changes measured negative. Every change that gained re-allocated
> work between resources.

Examples of the losing kind: EDD (−3.8 pt), BACKLOAD (−4 pt), campaign splitting
(−43,104 tyres), strict horizon (−17,036), depth-before-breadth (−11.9 pt).

**Polars + DuckDB + Parquet. No pandas in the hot path.** Python 3.11,
project-local `.venv` only.

Three performance rules that are not negotiable:

- **Never write a recursive DuckDB CTE over the BOM** — it OOMs at ~29.5 GB tempdir
- **Never insert row-by-row into DuckDB for >1 K rows** — register an Arrow frame, one bulk INSERT
- **Filter on the Hive partition column `date`, not `event_ts`** — filtering `event_ts` scans everything

---

## 7. DATA LINEAGE — memorize before writing a query

- Build **stage 2** `itemCode` is the **GT code** (`"GT 1402 XPC TATA"`), not the finished SKU
- **The GT namespace trap.** The plant writes GT codes in at least four shapes. The TBR BOM keys on `GT 5001` while TBR MES `itemCode` is size-led — **zero string overlap**. `scripts/gt_namespace.py` is the single bridge; ambiguity returns `None` and nothing is guessed
- Build `productionID` is a per-tyre barcode and equals curing's `gtbarCode` — **join on that**, 99.6 % hit rate. Joining on `b.gt_code IS NOT NULL` is cartesian
- Curing `cycleStart` is **press-open, i.e. the cycle END**; `event_ts` is press-close. `duration = cycleStart - event_ts` ~ 1955 s. **The source is named backwards — do not "fix" it**
- Curing `wcID` is the press id (int in source, cast to VARCHAR everywhere)
- **Moulds are per `(plant, gt_code, press)`**, labelled `<mould>@<press>`. Forcing one primary mould per GT produced 416 K phantom double-book violations
- **The three numbers people confuse:** *built* (produced this month) · *fed* (delivered into presses, includes opening stock) · *cured in-month* (`built + opening - closing`)

---

## 8. MEASUREMENT DISCIPLINE — easiest to skip, costs the most

- **Judge on BUILT first.** In-month is tail-sensitive: a change once moved August in-month 91.4 → 94.8 % while building **2,432 fewer tyres**. That is relocating output, not creating it
- **Always report PCR and TBR separately.** A plant-total that moved 1.85 pt once hid an **8.67 pt TBR regression**
- **Diff the L11 status column, never the pass count.** A count can rise while a segment goes red
- **Two-month gate.** Eleven changes were rejected on it in one session
- **Never A/B against an existing run directory.** Use `scripts/run_arm.py`
- **`arm_scorecard.py` separates arms by ENV ONLY — it cannot A/B a FILE.** Driving a file-valued change through it produced a 0.7 pt phantom gain
- **Every fulfilment figure must name its basis.** Four denominator defects have been found; one gate passed for the entire project while dividing plant *event* rows by our *campaign* rows
- **Mean over events is not mean over time.** Inventory is a stock held over time
- **The verifier must not import `planner/`.** `scripts/verify_export.py` re-derives every check from the exported CSVs. A verifier that calls planner internals only proves the planner agrees with itself
- **Every shared master carries a provenance stamp.** Anything generated outside an arm and read inside it must be stamped, or the arm silently inherits

---

## 9. WHAT IS OPEN

### Needs a plant ruling, not a code change

1. **Press cavity count.** `l3_cavities.cavities` is back-solved, not counted — **0 of 166 presses has an integer value** (PCR p50 3.40, TBR p50 2.41; a press physically has 1, 2 or 4). We plan slower than the plant's own observed rate on 51/86 PCR and **74/79 TBR** presses. Worth roughly **+9,200 PCR / +9,900 TBR**. Two L11 invariants have been failing on this for the life of the project
2. **TBR press 169** ran 237 days in the mining window and has **zero rows** in press eligibility — ~1,300 tyres/month unreachable
3. **The month boundary.** 9,680 PCR tail + 3,309 before_t0 are the two ends of one accounting frame. `carry_out` → `carry_in` exists in code, is stamp-gated, and is unwired
4. **B12 threshold** is 2x the lot floor — 1,081 PCR / 423 TBR refused tyres would each form one legal lot
5. **The two 0.95s** (4b) need one definition
6. **Opening GT floor stock** — is 6,117 tyres (max age 55.7 h) the real number? It covers ~8 hours of curing

### Engineering, measured and open

- L7 switches rims **192 times** for **5** extra rims the partition granted. TBMPCR2 alone: 2 rims, 74 switches at 60 min. Two L11 invariants fail on exactly this
- 699 tyres of opening stock expire unused — their GTs' first campaign is seated 16–25 July against a 72 h wall
- 237 tyres vanish between L4.5 and L5 with no reason recorded
- Run directories do not record the environment they were built with (`l11_provenance.json` carries only run/month/file-fingerprints)
- No per-machine-per-day SKU variety cap and no changeover budget per day

### Deliberate non-goals — do not "fix" these

Daily build quota (B7) rejected — interior CV is already better than the plant's ·
GT inventory sitting below the G8 band is intentional · the backwards `cycleStart`
naming · per-press moulds · rolling-horizon lookahead degrades to a clean no-op
because `masters/demand/` ends where MES ends.
