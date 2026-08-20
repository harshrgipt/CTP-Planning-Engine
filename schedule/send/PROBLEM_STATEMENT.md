# Problem statement — CTP synchronized building + curing planner

State of the problem as of 2026-08-13. Every number here is measured on the
shipped engine, not estimated.

---

## 1. The question the engine answers

> Given one month's demand, the plant's masters, and ~8 months of MES history:
> **which green tyre is built on which machine at which hour, so the press that
> needs it is neither starved nor over-fed, and no tyre sits past its 72-hour
> shelf life?**

It is not a forecaster and not an ERP. It converts a month of demand into an
hour-by-hour executable building and curing schedule for two plants.

## 2. The physical system

| | PCR (passenger car radial) | TBR (truck & bus radial) |
|---|---|---|
| building machines | 11 | 9 |
| curing presses | 86 | 79 |
| build cadence | ~50 s/tyre (~72/h per machine) | ~140 s/tyre |
| cure rate | 6.1 tyres per press-hour | 1.6 tyres per press-hour |
| full-rate day | 13,288 tyres | 3,180 tyres |
| July demand | 397,288 plannable | 97,436 plannable |

**A green tyre (GT) is perishable: 72 hours from build to cure (rule R5).**
Building and curing therefore cannot be planned independently. GT inventory is a
**synchronization buffer, not a production target**.

The plant day runs 07:00 → 07:00, shifts A (07-15) / B (15-23) / C (23-07).

## 3. The architecture — curing proposes, building disposes

The plant is curing-first, so the engine is too:

```
L1 preflight -> L4 net requirement -> L4.5 lot sizing -> partition
   -> L5 cure campaigns -> L7 pull release of building
   -> L10 shift grid -> L11 invariants -> BTP export
```

A cure campaign is placed first; building is released **backwards** from it:

```
release(slice) = t_cure - tau* - build_duration(slice)
```

The inverse (build first, cure follows) was the previous generation and was
measured: GT head 7.4 h against the plant's 4.4 h, ~1,548 tyres of standing
inventory. It is not a design option.

## 4. Hard constraints — none may be traded for fulfilment

| constraint | value | rule |
|---|---|---|
| GT shelf life | **72 h**, not env-overridable | R5 |
| build lot floor | PCR 150 / TBR 70 tyres | B12 |
| minimum demand to plan | PCR 300 / TBR 150 | B12 residual |
| GT WIP rail | PCR 4,800 / TBR 1,400 | G8 |
| allowable building machine | plant matrix, **hard** | R2 |
| allowable press | plant matrix, **hard** | R3 |
| concurrent presses per GT | ≤ its active mould count | R3 |
| TT/TL group split | hard where confirmed | B16 |
| physical inch capability | hard | R6/R7 |

Current plan: **0 allowable violations · 0.0 % sub-floor runs · R5 max 71.7 h ·
GT inventory below both rails**, both months, both plants.

## 5. How success is measured

Three numbers that are routinely confused:

| number | definition |
|---|---|
| **built** | green tyres produced this month |
| **fed** | tyres delivered into presses (includes opening stock) |
| **cured in-month** | the fulfilment numerator: `built + opening − closing` |

Reporting rules, both learned from measurement errors that hid regressions:
- **always report PCR and TBR separately** — a plant total that moved 1.85 pt
  once hid an 8.67 pt TBR regression;
- **inventory is a stock held over time**, so time-weighted, never event-weighted;
- **in-month is TAIL-SENSITIVE, so always report BUILT beside it.** A change
  that raises in-month while BUILT falls is *relocating* output across the month
  boundary, not creating it. A press-eligibility fix scored +3.4 pt of August
  in-month while building 2,432 fewer tyres and was nearly shipped on that
  strength; `scripts/arm_scorecard.py` now prints BUILT / in-month / tail /
  total for every arm;
- **every fulfilment figure must name its basis.** A sweep harness summed
  `qty_fed_in_month` — the *fed* column — and reported it as in-month for a whole
  session, inflating July by ~0.6 pt and reversing the sign of a PCR/TBR
  comparison. The five basis defects found in this project are the single most
  productive bug class in it. `scripts/ab_both_months.py` reads L11 and prints
  the basis string.

## 6. Current performance

| | Jul PCR | Jul TBR | Aug PCR | Aug TBR |
|---|---|---|---|---|
| **BUILT** (tyres the plan produces) | **96.7 %** | **97.7 %** | **95.7 %** | **93.5 %** |
| **fulfilment in-month** (the KPI) | **96.2 %** | **95.7 %** | **91.4 %** | **89.4 %** |
| including carry-out tail | 97.8 % | 97.7 % | 96.8 % | 93.0 % |
| same-size share (plant 91.5 / 100) | 80.4 % | 100 % | 75.2 % | 100 % |
| weighted CO min/machine-day (plant 74.0 / 35.6) | 78.7 | 33.6 | 99.8 | 21.8 |
| changeovers/machine-day (plant 2.66 / 3.56) | 2.65 | 3.36 | 3.15 | 2.18 |
| mould changes/press-day (plant 0.08 / 0.04) | 0.04 | 0.03 | 0.04 | 0.03 |

**We beat the plant on curing changeover 2:1 on both plants and both months, and
match it on PCR building changeover count.** Days 2–31 run at 100 % of available
press-hours and above the plant's own daily mean (13,288 vs 12,301).

## 7. The problem, precisely stated

July's demand **is the plant's own July production**, so the plant achieved
100 %. We achieve 95.6 %. The 4.4-point gap decomposes as:

| loss | tyres | pt | class |
|---|---|---|---|
| carry-out tail (cured 1–2 days into August) | 6,545 | 1.64 | **definitional** — real output |
| cold start, day 0–2 | 3,347 | 0.84 | **closed-box boundary** |
| month-end taper, day 28+ | 2,553 | 0.64 | horizon boundary |
| interior, day 3–27 | 2,544 | 0.64 | **our scheduling** |
| min_lot floor (plant runs 12.7 %/30.8 % sub-floor) | 2,232 | 0.56 | **our policy** |
| B12 residual, never planned | 1,117 | 0.28 | our policy |

**Roughly 70 % of the gap is definitional or self-imposed.** Under the plant's
own operating rules — continuous running, sub-floor runs permitted, residual GTs
made — the same schedule reads ~99 %.

### 7.1 The day-1 problem (the sharpest sub-problem)

```
plant day 1: 12,682 PCR   (99 % of its own monthly mean)
ours  day 1:  8,313 PCR   (50 of 86 presses idle for the first 11.86 h)
plant daily CV 0.109 over 242 days -- it has NO month-start dip
```

**Cause:** at 07:00 the plant's 11 machines were already mid-run and its 4,820
tyres of floor stock sat on the GTs those machines were feeding. We model every
machine and press as starting idle, so building can only reach **9 GTs in the
first 2 hours** while 30 GTs' presses are waiting.

**This is a missing input, not a missing algorithm.** Six scheduling approaches
have been measured against it (section 9); one gained 0.3 pt, five lost more
than they gained.

### 7.2 Capacity is not the binding constraint

```
PCR: gross_build required 393,639 · BUILT 380,254 (96.6 %)
     machine occupancy 77.6 %  ->  1,830 hours IDLE
```

But only **9.2 % of that idle time can legally feed anything waiting** — the rest
is locked by eligibility (wrong machine) or R5 (wrong time). Total-hours
arithmetic overstates recoverable capacity by ~10x.

## 8. Open problems, ranked by value

| # | problem | worth | owner |
|---|---|---|---|
| 1 | **Narrow allowable machines.** 26 of 73 Aug PCR GTs have ≤2 allowable machines; TBMPCR7 has 39 GTs in the matrix against TBMPCR2's 130; TBMTBR9 has 2 demanded GTs and runs at 27.8 %. **70 % of PCR idle is locked behind this.** | largest single item | **plant** |
| 2 | **Horizon ruling** — does the carry-out tail count? | +1.6 pt Jul / +5.1 pt Aug | **plant** |
| 3 | **Strict B12** — the plant runs 12.7 % PCR / 30.8 % TBR sub-floor; we run 0.0 %. On TBR this is 97 % of the entire build-feed gap. | +0.56 pt Jul PCR / +1.61 pt Jul TBR | **plant** |
| 4 | **30-June machine state** — which GT each of the 11 PCR and 9 TBR machines was building. **20 rows.** Would let day 1 start warm. | ~0.8 pt | **plant** |
| 5 | **Opening stock distribution** — quantity is verified correct (Little's law: PCR 4,820 vs 4,676 implied; TBR 1,297 vs 1,167) but sits on 25 of 48 / 27 of 56 demanded GTs. | part of day 1 | **plant** |
| 6 | **Interior starvation, 2,544 tyres.** 222 R5-legal idle hours exist on allowable machines. Needs a targeted repair pass. | ~0.6 pt | **engineering** |
| 7 | **July partition** — none exists; July runs unpartitioned. Rebuild needs raw MES. | ~0.6 pt + same-size | **data** |
| 8 | **TBR press→GT bridge** — 0 % resolved; PCR resolves 67 of 86. | part of day 1 | engineering |
| 9 | **August TBR regression** 94.6 → 90.2 % after adopting the plant's TBR allowable matrix, which is tighter than the derived list for August's 37 GTs. **Unresolved.** | 4.4 pt Aug TBR | **plant decision** |

## 9. Ruled out by measurement — do not retry without new evidence

| approach | result |
|---|---|
| de-pin L7 (`HARD_PIN=0`) | −0.5 pt, unfed +2,108 |
| L5↔L6 loop, delay mode | −16,548 tyres |
| L5↔L6 loop, split mode | −18,129 (each piece pays a 6 h mould change) |
| deadline-aware allocation | neutral; 0 of 130 machine assignments changed |
| rim-priority flags | ~0.8 pt fulfilment per 1 pt same-size |
| hard rim lock | −14 pt; caused 87 % of August's infeasible hours |
| L9 optimiser | 1,428 candidates, 0 moves accepted |
| historical machine share as ordering | −0.4 pt |
| removing the 72 h tail | −1.3 pt |
| **depth-before-breadth queue order** | **−11.9 pt PCR** — destroys the scarcity priority large campaigns depend on |
| day-1 floor: lot / stagger / τ_min / warm-open | all raise day 1, all lose more across the month |

| **press continuity (`WARM_PRESS`)** | **July +0.6 PCR, August −0.5 PCR — month-fitted, reverted v13** |
| **t0 stock basis `lot`** | **July +0.1 TBR, August −0.7 TBR and −2 invariants — reverted v13** |

**Pattern: 11 of 12 scheduler-side changes measured negative or neutral. Every
change that paid was a data fix** — the plant allowable matrices (+2.4 pt Aug
PCR), the `gt_size` rim fill (−12.7 % weighted CO), the L4b allocation (+0.8 pt),
and press continuity — which then failed the two-month gate and was reverted.

**A single month cannot distinguish "this helps" from "this fits July."** Since
v13 a change ships only if it is non-negative on **both** months, per plant;
`scripts/ab_both_months.py` enforces it. Two months is the floor, not the goal —
add a third as soon as its demand and opening GT land.

## 10. Locked-in technical constraints

Classical statistics and pattern mining only — no ML, RL or LLM. Heuristic greedy
plus SA/Tabu/LNS — no CP-SAT or MILP. Polars + DuckDB + Parquet, no pandas in the
hot path. Python 3.11, project-local `.venv`. Every layer communicates only
through files on disk, which is what makes each layer re-runnable and every
number traceable to a parquet.

The independent verifier (`scripts/verify_export.py`) must never import
`planner/` — a verifier that calls planner internals only proves the planner
agrees with itself.
