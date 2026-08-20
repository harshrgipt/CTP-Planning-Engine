# CTP Planner — problems, flow, and current output
**Version V17 · 14 August 2026 · every number below is measured on a real run, not estimated.**

This document is written so you can find solutions yourself. It states what the
engine does, what it produces, what is wrong, and — for each problem — what has
already been tried and what the measurement said. Nothing here is a proposal.

---

## 0. How to read the numbers (this matters more than it sounds)

Six of today's findings were **measurement errors, not engine errors**. Every one
inflated the result in our favour. Before trusting any figure, know which basis
it is on.

| number | what it means | trap |
|---|---|---|
| **BUILT** | green tyres produced on real machines | `build_schedule` contains an `OPENING_STOCK` pseudo-machine row (Aug PCR 3,775) that is **floor stock, not production**. Must be excluded. |
| **in-month** | tyres **cured** before 07:00 on the 1st of next month | **tail-sensitive** — rises when cures move earlier, even if fewer tyres are made |
| **carry-out tail** | built this month, cures 1–3 days into next | real output the horizon rule excludes |
| **fed** | tyres delivered into presses, **includes opening stock** | never quote as in-month |
| **press utilisation** | must use `cure_campaigns_reconciled`, not `cure_campaigns` | a campaign building never fed still carries its hours → books an **empty press as busy** |
| **daily cured** | must be `press-hours × rate`, not `gt_events` counts | the ledger credits cures in ~155-tyre **lumps**; one day read 14,732 against a ~13,000 ceiling |

**Rule that came out of this: judge every change on BUILT. A change that raises
in-month while BUILT falls has relocated output across the month boundary, not
created it.**

Defects found and fixed today, all in measurement:

1. `fed` basis quoted as in-month — inflated July ~0.6 pt
2. `OPENING_STOCK` counted as production — +3,800 PCR
3. `INCH` capability read as production history — overstated an allowable conflict 3×
4. shared GT supply double-counted (twice) — invented 2,690 and 1,400 tyres that don't exist
5. LNS acceptance metric was `qty_fed_in_month` — read −16,548 when the truth was positive
6. press utilisation from scheduled hours — TBR read 96.9 % when real is 89.9 %
7. press roster 86 vs 92 — **plant ruled 86, always**

---

## 1. The flow we currently follow

Two phases. **Curing proposes, building disposes** — the plant is curing-first,
so a cure campaign is placed first and building is released *backwards* from it.

```
Phase A — what and how much
  preflight  → input gate (MES-free): allowable machine, PCR inch, TT/TL,
               press, mould, cycle time, opening GT, partition stamp
  L4         → net requirement per GT
  L4.5       → lot sizing (the CURE lot; build slices have no minimum)
  L4b        → max-flow feasibility: can building feed this month at all?
  partition  → static GT→machine map (month-stamped)

Phase B — where and when
  L5         → CURE CAMPAIGN MASTER. The plan is born here.
               seats each campaign on a press: earliest free + the mould change
               that choice forces (cheapest insertion)
  L7         → PULL RELEASE of building.  release = t_cure − τ_min − build_duration
               ~4,000 lines; this is where the real work happens
  L10        → time discretisation, shift grid, mould changes
  L11        → 42 invariants
  BTP export → the shift pack
```

**Every layer communicates only through files on disk.** That is what makes a
layer re-runnable and every number traceable to a parquet.

### Key mechanisms inside this flow

| mechanism | what it does |
|---|---|
| **pull equation** | `release = t_cure − τ_min − build_duration`. τ_min = 0.27 h |
| **earliest_cure floor** | a press may start at t0 only if its GT holds `(τ* + band) × rate` ≈ **72 tyres** of cover; otherwise it waits **11.86 h** |
| **horizon mode** | `extend` — plans the month **+72 h**, so campaigns may finish next month |
| **machine pin** | one machine per GT for the whole month; a second opens only when the first hits `MACH_UTIL_CAP` |
| **B12 lot floor** | build runs ≥ **150 PCR / 70 TBR**. Enforced hard (`STRICT_LOT_FLOOR=1`) |
| **R5 shelf life** | 72 h, applied with a **6 h safety margin** → effectively 66 h |
| **G8 WIP rail** | GT inventory ≤ 4,800 PCR / 1,400 TBR |
| **R3** | concurrent presses per GT ≤ its mould count |
| **closing GT buffer** | tops the closing floor back toward opening, built in the last 72 h |
| **LNS loop (July only)** | L7 reports unfed → per-GT delay hint → L5 reseats → keep only if BUILT rises |

---

## 2. Current output — V17

### Volume

| | plannable | **BUILT** (real machines) | **in-month** | % | carry-out tail | in-month + tail |
|---|---|---|---|---|---|---|
| **Jul PCR** | 397,288 | **387,159** | **379,410** | **95.5 %** | 12,426 | 98.6 % |
| **Jul TBR** | 97,436 | **96,272** | **93,344** | **95.8 %** | 3,295 | 99.2 % |
| **Aug PCR** | 426,688 | **407,307** | **388,713** | **91.1 %** | 23,807 | 96.7 % |
| **Aug TBR** | 98,743 | **91,603** | **87,980** | **89.1 %** | 3,838 | 93.0 % |

### Utilisation (corrected: unfed removed, 86 PCR / 79 TBR roster)

| | building | curing (real) |
|---|---|---|
| Jul PCR | **79.0 %** | **93.2 %** |
| Jul TBR | **79.9 %** | **95.8 %** |
| Aug PCR | **83.1 %** | **95.2 %** |
| Aug TBR | **76.3 %** | **90.1 %** |

### Changeover vs the plant's own 8-month baseline

| | PLANT | Jul PCR | Aug PCR | PLANT TBR | Jul TBR | Aug TBR |
|---|---|---|---|---|---|---|
| build CO / machine-day | **1.84** | 2.72 | 3.22 | **3.12** | 3.40 | 2.20 |
| weighted CO min/machine-day | **74.0** | 82.7 | **107.9** | **35.6** | 34.0 | **22.0** |
| same-size share | **91.5 %** | 79.9 % | **68.4 %** | 100 % | 100 % | 100 % |
| mould changes/press-day | **0.060** | 0.045 | 0.050 | **0.040** | 0.040 | 0.033 |

We **beat the plant on mould changes and on all TBR changeover metrics**; we are
**worse on PCR building changeover**, badly so in August.

### Capacity ceilings (with changeover and mould change deducted)

| | cure ceiling/month | build ceiling/month | binding |
|---|---|---|---|
| **PCR** | **~397,000** | 433,000–441,000 | **CURE** |
| **TBR** | **~97,000** | 114,000 | **CURE** |

**Building can do ~13,955 tyres/day; curing only ~12,810.** Building exceeds
curing by **1,146 tyres/day on PCR** and 555 on TBR, every day. That is why
building sits at 76–83 % — it has nothing more it can legally build.

---

## 3. THE PROBLEMS

### P1 — Day 1 curing collapses

**PCR cures ~7,000 on day 1 against ~13,500 on a normal day.**

| | presses running at 07:00 | day-1 press-hours | day-1 cured |
|---|---|---|---|
| Jul PCR (no LNS) | **26 of 86** | 1,222 of 2,064 (59 %) | **7,873** |
| Jul PCR (V17, LNS on) | **1 of 86** | 499 (24 %) | **3,226** |
| Aug PCR | **19 of 86** | 1,118 (54 %) | **7,084** |
| Aug TBR | 33 of 79 | 1,342 (71 %) | 2,247 |

**Why:** at 07:00 the floor holds 4,847 tyres but they sit on **20 of 57**
demanded August GTs. A press may start at t0 only if its GT holds ~72 tyres of
cover; the rest wait **11.86 h** for the first fresh build. Building starts from
zero — the first legal lot takes ~2.3 h.

**Loss: ~5,400–6,000 tyres, both months.** ~842 additional press-hours would be
needed on day 1 to reach 13,000.

**Already tried and measured:**

| | result |
|---|---|
| partial press start | **already implemented** — GT 1513 starts 6 of its 7 presses on 490 stock |
| build-priority for waiting presses (hard / capped) | day 1 **−313 to −435** |
| ledger replay (pull cures earlier) | **51 of 87 GTs have zero ledger slack** → 126 tyres |
| lower the τ\* floor (`slice`/`stagger`/`min`) | −2,478 to −6,050 BUILT |
| build earlier / level-load | −2,124 BUILT; GT hits the 4,800 rail, R5 reaches 71.9 h |
| `machine_warm` | July **has** the file and is only marginally better than August, which lacks it |

---

### P2 — The LNS loop buys the month by destroying day 1

**July: +6,320 monthly BUILT, −4,647 day-one cures. Presses at 07:00 drop 26 → 1.**

**Why:** the hint is keyed `(plant, gt_code)` and applied as a floor —

```python
_DELAY[(plant, gt_code)] = delay_h
floor_ts = max(floor_ts, t0 + delay_h)
```

so **one unfed late campaign delays every campaign of that GT**, including a
day-one campaign that has opening stock, a running mould, and could start at
07:00.

**Tried:** a time guard (`PLANNER_L56_PROTECT_FIRST_H` at 24/48/72 h). All three
**reject every iteration** — the loop keeps the baseline. Protecting the opening
window removes the entire gain. **There is no middle point at this grain.**

**The hint needs to be campaign-specific** — `(plant, gt, lot index, seq)` —
so a later failure never moves an earlier success. Not yet built.

---

### P3 — Unfed tyres

| | unfed | dominant gate | mechanism |
|---|---|---|---|
| **Aug PCR** | **5,838** | `before_t0` 63 %, R5 37 % | build release lands before the month opens |
| **Aug TBR** | **6,816** | `before_t0` 97 % | *not* a boundary problem — see below |
| Jul PCR | 5,247 | `before_t0` 81 % | |
| Jul TBR | 283 | | |

**Careful: the reason strings in `build_starved.parquet` are a heuristic, not the
measured gate** — `"would breach min_lot" if len(grp) > 1 else "no feasible release"`.
The real gate comes from `PLANNER_L7_DIAG=1`.

**TBR's real mechanism:** its refused groups need **4.7 contiguous hours**; their
single eligible machine has **11.8 free hours but a best contiguous gap of
0.97 h**, and `n_cand = 1`. The backward walk exhausts the month and falls off
the start. It is **machine-calendar fragmentation**, not the boundary.

**Only ~16 % of starved volume has any legal home** (right machine × contiguous
window × inside R5).

---

### P4 — TBR's 28-tyre build slices vs the 70-tyre floor

**6,583 of TBR's 6,816 starved tyres are rejected by the B12 lot floor, across
241 lots averaging 27 tyres.**

**Why:** `GT 5103` cures at **1.73 tyres/press-hour** across 7 presses.
Building runs at ~17 tyres/hour. A slice feeding one press stream is **28 tyres**
— the machine builds it in 1.6 h while the press takes 16 h to consume it.
28 < 70, so it is refused. Slices that happen to be consecutive on a machine
merge into a legal run and place (91 slices, 2,517 tyres); isolated ones don't
(99 lots, 2,757 tyres).

August concentrates demand on **34 GTs vs July's 46**, so each spreads over more
presses → more parallel slow streams → more sub-floor slices.

**Already tried:**

| | result |
|---|---|
| `POOL_TAILS` (pool remainders in one R5 window) | **inert** — GT 5103's slices are 258 h apart, the window is 72 h |
| bigger slices (`LOT_INTERVAL_H` 24 / 48) | **−6,183 / −39,501 BUILT**; unfed 8,580 → 48,081 |
| relax the floor (plant-calibrated budget) | **+1,291 only**; `nofloor` == `budget`, so the floor was never the sole blocker |
| L4.5 remainder merging | **already implemented** — but it governs *cure* lots, not build runs |

**Not yet tried: merging same-GT slices on the same machine into one ≥70 run
regardless of R5 spacing** — the build can be contiguous even when the cures are
258 h apart. Respects B12 fully.

---

### P5 — August is over-committed on press capacity

**August PCR needs 66,166 press-hours; the month has 63,984. Short 2,988 h
≈ 19,000 tyres — about half the gap.**

August demands **13,764 tyres/day for 31 consecutive days**. The plant's mean is
12,301, its p95 is 13,291, its best day in 8 months was 13,792.

**July fits** (−496 h spare), which is why July lands at 95.5 % and August at 91.1 %.

---

### P6 — Half the August gap is the month boundary, not production

| August PCR loss | tyres | share |
|---|---|---|
| **built AFTER 1 Sep 07:00** | **13,483** | **36 %** |
| **L5 never seated (no press-hour)** | **10,801** | **28 %** |
| starved | 5,838 | 15 % |
| tail + closing GT | 5,111 | 13 % |
| opening-stock / yield residue | ~2,742 | 7 % |

**18,594 tyres are built but cure in September.** They exist. Only the horizon
rule excludes them.

---

### P7 — Mould changes cluster in the last third

**82 of 117 PCR mould changes land in days 21–30, burning 493 press-hours.**

**Why:** L5 orders campaigns scarcity-first by `-qty`. Big GTs take campaigns of
**369 h (max 656 h — 27 days on one press)** and hold the presses through the
first half. Every low-volume GT is deferred: **the bottom 20 GTs by volume all
start on day 26**, each needing its own mould change.

Day 24 has 8.2 changes and 95 idle press-hours against a good day's 1.4 and 37.

**Tried:** campaign-length cap (240/168/120 h) → **−43,104 BUILT**, unfed
6,403 → 43,270. Long campaigns are what let building feed a press in one
continuous run; fragment them and building can't reach any piece.

---

### P8 — Constraints are mutually load-bearing

Every loosening costs more than it returns. This is why 8 of 9 scheduler changes
measure negative.

| loosen | what breaks | cost |
|---|---|---|
| widen the machine pin | runs shorten → B12 rejects the pieces | −260 BUILT, unfed ↑ |
| cap campaign length | more campaigns → building can't feed them | **−43,104** |
| build earlier | GT hits the 4,800 rail, R5 reaches 71.9 h | −2,124 |
| shorten the horizon tail | volume deleted, not relocated | −6,890 to −30,572 |
| 100 % press availability | cures pull earlier → releases fall before t0 | unfed 8,580 → **24,118** |

---

## 4. Assumptions the plant has not signed off

| assumption | value | risk if wrong |
|---|---|---|
| **GT shelf life** | **72 h** | `config.py` records that *Ageing spec rev12* says **48 h**. If 48 is live, the plan is invalid, not optimistic |
| **`R5_SAFETY_H`** | **6.0 h** | every R5 check is against 66 h, not 72 — a hidden 8 % haircut |
| **load/unload** | PCR 2.9 / TBR **8.3** min | plant says **2.5** — TBR was 70 % overstated. Worth **+2.1 / +2.8 pt** in-month |
| **press availability** | **0.8897** | 11 % downtime priced silently into every cure rate |
| **cure rate** | **6.29 tyres/press-h** | plant's realised is 5.96. **+2.5 min/cycle costs PCR ~10 pt** — the most sensitive input in the model |
| **cavities** | **2.0** both plants | a 1-cavity press would be double-counted |
| **`MACH_UTIL_CAP`** | **0.95** | machine "full" at 95 %, never 100 % |
| **`min_demand_units`** | 300 / 150 | 2,458 August tyres never planned |
| **running-mould snapshot** | day 28, **not 07:00 on the 1st** | treated as proof of mounted state; it is 3–4 days stale |
| **TBR running moulds** | **45 of 75 presses resolve** | 21 of 55 recipes have no bridge at all |

---

## 5. Open questions for the plant

1. **Shelf life — 72 h or 48 h?** Two controlled documents disagree with what we use.
2. **Horizon ruling** — does the carry-out tail count? Worth **23,807 August tyres**.
3. **B12 sub-floor** — the plant runs 13 % PCR / 31 % TBR sub-floor; we run 0.0 %.
4. **B12 residual** — 2,458 August tyres of real demand never planned.
5. **Load/unload** — is 2.5 min correct for TBR, against a mined 8.3?
6. **Month-start machine state** — 20 rows: which GT each machine was mid-run on at 07:00. `machine_warm_2026-08` does not exist.
7. **Opening GT distribution** — quantity is right, spread is not (20 of 57 GTs).
8. **TBR recipe→GT bridge** — 21 of 55 recipes unmapped.

---

## 6. Everything measured today (do not repeat without new evidence)

| change | result |
|---|---|
| `WARM_PRESS` (press continuity) | correctness fix — **day 1 +1,500 both months**, 0 illegal t0 starts |
| closing GT buffer | **+3,817 / +1,088 BUILT**; closing GT 161 → 5,208 |
| tail-build pull | +808 |
| LNS with BUILT acceptance | July **+6,320**, August correctly **rejects all** |
| LNS opening-window guard (24/48/72 h) | rejects all iterations — no middle point |
| `T0_STOCK_BASIS=lot` | July-fitted: Aug TBR −0.7, −2 invariants |
| press-matrix union (+6 presses) | in-month +3.4 pt but **BUILT −2,432** — relocation |
| campaign-length cap | **−43,104** |
| horizon modes (strict/truncate/window) | −17,470 to −30,572 |
| `LOT_INTERVAL_H` 24 / 48 | −6,183 / −39,501 |
| build priority (hard / capped) | −291 to −5,393 |
| widen machine pin | −260 to −279 |
| de-pin (`HARD_PIN=0`) | −495 |
| takt governor (off / α) | **inert on PCR** |
| mould-aware press choice | **already implemented** at `l5_cure_master.py:1225` |
| mould life 3,000 cycles | **inert** — busiest press cures 5,964 of 6,000 |
| relax B12 to plant-calibrated budget | +1,291 August TBR |
| L9 optimiser | 1,428 candidates, **0 moves accepted** |

**Score: every change that paid was a data or measurement fix. Every change to
the search measured negative or neutral.**

---

## 7. Where the remaining tyres actually are

| | tyres | owner |
|---|---|---|
| horizon ruling (already built, cures 1–3 Sep) | **23,807** | plant |
| TBR remainder merging (respects B12 fully) | **~5,500** | **engineering — not yet built** |
| B12 residual | 2,458 | plant |
| B12 sub-floor | up to 6,583 | plant |
| campaign-specific LNS hint | ~2,500 + day 1 | **engineering — not yet built** |
| day-1 seating headroom (R3-limited) | ~832 | engineering |

**~32,000 tyres in the plant's gift; ~8,800 in ours.**
