# Independent expert audit — findings and corrections

**Run by the `tyre-planning-architect` agent, 2026-08-08. Read-only study of the
whole engine, verified against `runs/v29` / `v30` / `v31` and the warehouse.**

Reference run after acting on it: **`runs/v32` — 95.81 % fulfilment, every hard
cap held, best R5 margin of any arm shipped.**

This file records what an independent expert pass found **wrong in work that had
already been reviewed and documented**. Its value is not the fixes — those are in
the code — it is the four failure modes it exposes, all of which had survived
multiple self-reviews.

Companion: [PARTITION_AND_CHANGEOVER.md](PARTITION_AND_CHANGEOVER.md) (defect
ledger), [MEMORY.md](MEMORY.md), [BUSINESS_RULES.md](BUSINESS_RULES.md).

---

## 1. THE MAJOR FINDING — a plant-total metric hid an 8.67-point regression

The derived slice rule (§4h of the ledger) was measured on **plant-total
fulfilment only**. Split by plant it reads completely differently:

| run | PCR ful | **TBR ful** | total | TBR lot p50 | TBR R5 max |
|---|---|---|---|---|---|
| v29 | 95.89 % | **94.08 %** | 95.53 % | 86 | 71.9 h |
| **v31** *(was shipped)* | 95.74 % | **85.41 %** | 93.68 % | 141 | 66.6 h |
| **v32** *(now shipped)* | 95.74 % | **96.06 %** | **95.81 %** | **87** | **61.0 h** |
| plant | — | — | 100 % | 86.5 | 142.6 h |

**TBR lost 8.67 points and the plant-total number moved only 1.85, so it read as
an acceptable trade.** It was not a trade; it was a regression on one plant
masked by an aggregate.

### Root cause

The derived rule takes `n = n_B12 = Q/min_lot`, which **sets the slice equal to
the lot floor**. On TBR that lengthens the run 86 → 141 tyres, so it needs a
**54 % longer contiguous machine gap**. TBR cannot supply one:

| | PCR | TBR |
|---|---|---|
| eligible machines per GT (median) | **11** | **3** |
| machine occupancy | 61–83 % | 45–84 % |
| cadence | 49–78 s | 189–219 s |
| machines | 11 | 9 |

Measured on v31: TBR machine occupancy **fell** 77.8 → 70.5 % while starvation
**rose** 8.6 pt. 9 × 744 × 0.073 = 489 idle machine-hours ≈ 8,500 tyres at 207 s
— almost exactly the 8,529 lost. Capacity was idle; the *gaps* were the wrong
shape.

### The "cold start" explanation was wrong, and the agent killed it with data

Splitting TBR unfed volume by campaign start hour:

| TBR unfed | campaigns starting < 48 h | **≥ 48 h** |
|---|---|---|
| v29 | 5.9 % | 5.2 % |
| v31 | 13.5 % | **16.7 %** |

Campaigns starting after hour 48 — where the month boundary is irrelevant —
degraded **3.2×**. Run length acting everywhere, not a horizon artefact.

### Fix

**Per-plant slice rule.** PCR keeps the derived R5/B12 rule; TBR uses the finer
legacy arm (`SLICE_MULT_TBR = 3.0`). `SLICE_MULT` and `PARTITION_PLANTS` were
already per-plant for exactly this reason; the derived rule was not.

**TBR is a different plant, not a smaller PCR.** Any rule tuned on PCR must be
measured on TBR separately before it ships.

---

## 2. THREE BROKEN MEASUREMENTS IN CODE THAT HAD BEEN "FIXED"

### 2a. L11's G8 invariant was event-weighted — violating this repo's own DO-NOT #9

`l11_validate_plan.py:253` read `ivt["bal"].mean()`. The ledger's DO-NOT list
says *"Do not report or gate `e["bal"].mean()`. It is not the inventory."*

| | L11 (event-wt) | correct (time-wt) | **bias** |
|---|---|---|---|
| PCR | 4,061 | 3,851 | +5.5 % |
| **TBR** | **1,000** | **895** | **+11.7 %** |

The same bug had been found and fixed in `l7_pull_release` and in
`rulebook_scorecard` — **one bug, three files, two repaired.** Exactly the pattern
already recorded as §1g, repeated. Now uses `_tw_mean_l11()`.

### 2b. R17 was permanently failing on an artifact

The ledger states *"R17 is checked per tyre in L11 and passes at 0."* It did not
— v31 showed **19 PCR / 20 TBR breaches**, every one an `OPENING_STOCK` row.
Those rows carry `start_ts == end_ts == t0`, so `wait_h` is measured from the
horizon start rather than from when the tyre was actually built (last month).
R17 is a **release** rule and does not apply to carried-in stock.

Physically harmless — but **a hard-rule guard that always fails is a dead
signal** and would not have caught a real R17 breach. It began failing when
`EARLY_STOCK` was turned on and nobody looked. Now excludes opening stock;
reads 0/0 PASS.

### 2c. Doc/code drift on the rail margin — in the file whose purpose is preventing drift

`config.gt_wip_rail_margin` = **0.94**. `PARTITION_AND_CHANGEOVER.md` §5 and §8
both said **0.97**, and §8 is titled *"SINGLE SOURCE OF TRUTH — where every cap
lives."* Consequence: the PCR rail was enforcing an effective **4,512, not the
stated 4,800** — 288 tyres of headroom given away silently.

---

## 3. CORRECTIONS TO CLAIMS IN THE LEDGER

| ledger claim | verdict | correct number |
|---|---|---|
| §4i: "2,770 PCR + 1,264 TBR opening stock unused, worth a look" | **overstated ~5×** | Measured against *usable* stock (4,820 / 1,297 within shelf life, not the 6,960 / 2,330 total held), utilisation is **86.9 % / 82.2 %**. Unused-and-usable is **861 tyres = 0.17 pt**. Not worth a session. |
| §36: "R12 needs 775 h against 707 h (109.6 %); R13 98.9 %, R17 98.2 %" | **incomplete** | **Three rims are over 100 %**: R12 **113.3 %**, R13 **102.3 %**, R17 **101.6 %**. Total over-subscription 153 h against 1,093 h of slack in R15/R16/R18. A single R12→TBMPCR2 flex exception **cannot** fix R13 — that is a 3-machine group 48 h over. |
| §36: "96.5 % is the architecture ceiling" | **artefact** | Computed from v31's broken TBR arm. The same releases applied from a corrected baseline start at ~95.8 %. The ceiling belongs to the operating point, not the architecture. |
| §1a: "R17 passes at 0 in L11" | **false since EARLY_STOCK** | See §2b. |

---

## 4. NEW ISSUES FOUND, NOT YET FIXED

### 4a. G8 collapses at month end, in the direction nobody checks

Daily-mean GT stock, last three days: **PCR 2,954 → 1,497 → 486; TBR 276 → 206 →
41.** G8 says *"every day, including the last day of the month"*. We end at ~10 %
of the band's floor, so closing stock ≈ 0 and **next month starts cold** — which
is the steady state G8 exists to enforce. L11 only tests the mean against the
band, so a month-end collapse is invisible. **Add a last-day check.**

### 4b. The horizon assignment measures load in the wrong currency

`need_h = qty x plant_cad/3600` uses the **plant-median** cadence while `cap_h`
is real hours and `_place` uses **per-machine** cadence. PCR spans 49–78 s — a
±26 % error. Replicating the assignment and converting to real hours:

| TBR machine | assigned (real h) | vs 707 h cap |
|---|---|---|
| TBMTBR6 (219 s) | 748 | **106 %** |
| TBMTBR5 (209 s) | 714 | 101 % |
| TBMTBR4 (213 s) | 360 | **51 %** |
| TBMTBR9 (207 s) | 399 | **56 %** |

Slow machines over-committed, fast ones showing full with 61 h free. **This is
the same "flat plant cadence" error already fixed in the partition builder (§3 of
the ledger) and left in L7.**

### 4c. L6 cannot fail on the actual failure, and nothing consumes its output

L5's and L6's docstrings both promise *"if infeasible, reshape at L5, never patch
downstream."* There is no write-back — L6 emits `l6_infeasible.parquet` and
stops; the reshaping happens in L7's reconcile, which is the downstream patch the
architecture says it exists to avoid.

Its R10 test is per-plant-per-day on the **plant-median** cadence: aggregate cure
draw is **548 tyres/h against 677 build capacity (PCR)**, **141 against 158
(TBR)**, **zero hours over capacity all month** — while 27,972 tyres starve. It
structurally cannot detect the failure it exists to detect.

### 4d. B16 (TT/TL dedication) is soft where the rulebook says hard

`_cand()` falls back to the full machine set when the TT/TL group is empty
(`… or e`), so a TT↔TL changeover is possible. BUSINESS_RULES §1a says *"HARD:
zero TT↔TL changeovers anywhere in the plan."*

### 4e. Documentation points at the retired engine

`BUSINESS_RULES.md` statuses are largely scored against `planner/plan/` (C7
"starvation = 0", S1 "sync 100 %", E1 "aging 101 h") and the workspace-root
`CLAUDE.md` documents that engine as the architecture. **Neither mentions
`planner/cmbc/`** — which is the engine actually being run.

---

## 5. RANKED NEXT STEPS

| # | change | expected | risk |
|---|---|---|---|
| 1 | ~~Per-plant slice rule~~ | **+2.1 pt — DONE, v32** | — |
| 2 | ~~Fix L11 G8 + R17~~ | **0 pt, 2 dead signals restored — DONE** | — |
| 3 | Per-machine cadence in the horizon assignment (§4b) | +0.3–0.8 pt | low |
| 4 | Rail margin 0.94 → 0.97 (§2c) | +0.1–0.3 pt | low; reject the moment a stated cap breaches |
| 5 | Month-end G8 check (§4a) | 0 pt, closes a blind spot | none |
| 6 | Targeted rim spill sized to the measured 153 h excess | +1.0–1.8 pt | **reject if same-size < 91.5 %** (plant) |
| 7 | Close the L6 → L5 loop (§4c) | structural | do last |

**Explicitly not recommended:** a daily build quota (§4b of the ledger); raising
the sub-floor budget; pinning harder onto the partition; partitioning TBR;
chasing changeover *type* further; the 861 unused opening tyres; or tuning the
WIP cap before W is under 9 h.

---

## 6. THE FOUR FAILURE MODES THIS AUDIT EXPOSES

Every one of these had survived several self-reviews. They generalise.

1. **An aggregate metric can hide a per-segment regression.** Plant-total
   fulfilment moved 1.85 pt while one plant lost 8.67. **Always report per plant.**
2. **Fixing a bug in one file does not fix it in the others.** The
   event-weighted-mean bug lived in three files and was repaired in two — the
   second time this exact pattern has been recorded (§1g was the first).
3. **A guard that always fails is worse than no guard.** R17 broke the moment
   `EARLY_STOCK` shipped and nobody noticed, because a failing line in a list of
   failing lines is invisible.
4. **A plausible explanation is not a verified one.** "Cold start" was accepted
   for the TBR loss without being tested. One split by campaign start hour
   refuted it in a single query.

**And the meta-finding: an independent adversarial pass found four real defects
in work that had been reviewed, documented and shipped.** Budget for one.
