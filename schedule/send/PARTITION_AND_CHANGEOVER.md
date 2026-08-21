# Build partition & changeover cost — defect ledger and contract

**Status: shipped. Reference run `runs/v28`, July 2026 — 95.4 % fulfilment, all caps held,
every cap consolidated to one place (§8).**

This file exists so six specific classes of error do not recur, and so three
already-measured dead ends (§4b, §4c, and the pin arms in §2) are not re-explored. Each section
records the **wrong value, the right value, how it was found, and the guard that
now prevents it**. Read §1 before touching any mined constant, and §3 before
running any month other than the one the partition was built for.

> ⚠ **[EXPERT_AUDIT.md](EXPERT_AUDIT.md) corrects four claims in this file.**
> An independent expert pass (2026-08-08) found that §4h's derived slice rule cost
> TBR **8.67 points** — hidden by a plant-total metric — and that §4i, §36's R12
> analysis, §36's "96.5 % ceiling" and §1a's R17 claim are all wrong or
> incomplete. Reference run is now **`runs/v32`, 95.81 %**. Read the audit first.
>
> **Superseded again: reference run is `runs/f_solo` — PCR 97.13 % / TBR 95.89 %,
> plant-total 96.9 %. See §4j.**
>
> **Current reference (2026-08-09): `runs/st_jul` / `runs/st_aug` —
> PCR 93.84 / 89.84, TBR 87.12 / 92.02, with ZERO runs below the B12 floor** on
> both plants and both months, by plant instruction (**§4m**). Export packs
> verified: 0 HARD / 0 SOFT / 0 EXPORT.
>
> The permissive reference is `runs/s4_jul` / `runs/s4_aug` — PCR 95.40 / 91.80,
> TBR 96.59 / 98.55 at 7.9–31.6 % sub-floor (**§4l**). Strict B12 costs 1.6–9.5
> points against it; `PLANNER_STRICT_LOT_FLOOR=0` reverses the trade without a
> code change.

Companion files: [EXPERT_AUDIT.md](EXPERT_AUDIT.md) (independent audit),
[MEMORY.md](MEMORY.md) §10d (measurement ledger),
[BUSINESS_RULES.md](BUSINESS_RULES.md) (the 46 numbered rules),
[ENGINE_FLOW_V4.md](ENGINE_FLOW_V4.md) (layer walkthrough).

---

## 1. THE RECURRING BUG CLASS: a mined statistic used as a hard constraint

Six separate defects, one root cause. A value mined from 8 months of MES is a
**description of a distribution the plant sits on both sides of**. Wiring it in
as a floor, a gate, or a flat constant deletes half of that distribution and
produces a wrong answer that looks right.

**The tell is a flat quantile band.** Our GT wait read 4.32 h at p01, p05 *and*
p10. Identical low quantiles mean a wall, not a distribution. Plant data never
looks like that. Before turning any mined number into a gate, plot its
distribution and ask what fraction of plant history the gate forbids.

### 1a. `tau*` as a hard release floor — cost 7.2 pt of fulfilment

| GT wait, h | p01 | p05 | p10 | p25 | p50 | mean | % below tau\* |
|---|---|---|---|---|---|---|---|
| plant PCR | 0.50 | 0.94 | 1.35 | 2.45 | 4.76 | 8.84 | **47 %** |
| ours (before) | **4.32** | **4.32** | **4.32** | 5.45 | 8.10 | 9.95 | **0 %** |

`tau*` is the plant's **median** coupling buffer. 47 % of PCR and 50 % of TBR
plant tyres cure sooner than it. The physical floor is `tau_min` = 0.27 h (R17),
16x smaller.

**Fix** `PLANNER_TAU_RELEASE=min` (default). 85.7 % -> 92.9 %, W 9.95 -> 8.40 h.
**Guard** R17 is checked per tyre in L11 and passes at 0. `tau*` is a preference
only — never restore it as a floor.

### 1b. `min_lot_units` as a hard split refusal — cost 6.2 pt

Machine x GT x day runs in `v_build` stage 2, full 8 months:

| | runs | p10 | p25 | p50 | below floor | July |
|---|---|---|---|---|---|---|
| PCR (floor 150) | 5,691 | 125 | 234 | 415 | **13.1 %** | 14.0 % |
| TBR (floor 70) | 6,541 | 33 | 62 | 96 | **31.0 %** | 31.0 % |

**The plant has no hard lot floor.** It runs sub-floor one time in seven (PCR)
or one in three (TBR). As a gate this was the single binding constraint on
fulfilment — all 30,615 starved tyres were tagged `would breach min_lot`, more
than the WIP cap and rim lock combined. Removing it entirely gives 97.1 % but
fragments to 18.7 %, *looser* than the plant.

**Fix** a plant-calibrated **budget**: spend exactly the plant's own sub-floor
setup count, oldest cure deadline first, then refuse.
`PLANNER_SUBFLOOR_PCR=180` / `PLANNER_SUBFLOOR_TBR=400`.
**Guard** L11 gates at the plant's share (`<= 16 %` PCR / `<= 34 %` TBR), **not
at zero**. A zero gate is stricter than the plant it imitates.

### 1c. Flat changeover minutes — every setup figure was wrong

The scorecard charged **11.3 min same / 42.4 min different to both plants**. The
plant master `v_changeover_build` says otherwise:

| | same size | different size |
|---|---|---|
| PCR machines 1–5 | **28 min** | **60 min** |
| PCR machines 6–11 | **22 min** | **42 min** |
| TBR all | **10 min** | **24 min** |

PCR same-size was understated ~2x; TBR different-size overstated 77 %. Every
"weighted setup hours" number was wrong in magnitude **and wrong by a different
factor on each plant**, so the two plants were not comparable to each other.
Corrected: PCR plant 344 h (not 172), TBR plant 148 h (not 167).

**Fix** `scripts/rulebook_scorecard.py` charges each changeover at its own
machine's rate, read from the master.
**Guard** never hardcode a changeover minute anywhere. If the master is missing,
fall back to `{"PCR": (22, 42), "TBR": (10, 24)}` and say so in the output.

### 1d. Not a constraint at all: R5 at 72 h

The plant lets 0.57 % of PCR tyres wait longer than 72 h (p99.9 = 227 h, max
4,210 h). Our hard 72 h cap is **stricter than the plant** at the tail. The cost
is small because it binds on 0.57 % of volume — recorded so it is not
"discovered" again as a bug.

### 1g. THE SAME CONSTANT WRITTEN TWICE — two live instances found

**A duplicated constant is how the "effective ceiling 8,990 against a stated
4,800" bug happened.** Two more were live until now:

| constant | place A | place B | consequence |
|---|---|---|---|
| TBR stock cap | `l7` rail **1,400** | `config.gt_wip_max` **1,500** | l7 enforced 1,400 while l11 graded against 1,500; the tighter number silently won |
| PCR changeover min | scorecard *(fixed §1c)* | **`l11` line 187: flat `(11.3, 42.4)`** | l11's own weighted invariant AND its `30.2` gate were both computed on wrong constants |

The second was the worse one. Fixing §1c in the scorecard did **not** fix l11 —
the same bug lived in two files and only one was repaired. With the correct
per-machine minutes the plant benchmark is **74.0 min/machine-day on PCR, not
30.2**, and two PCR invariants that had been "failing" now **PASS**:

| invariant | old gate | measured | **correct gate** | |
|---|---|---|---|---|
| PCR changeovers / machine-day | 2.46 | 2.59 | **2.66** | FAIL → **PASS** |
| PCR WEIGHTED co min/machine-day | 30.2 | 65.6 | **74.0** | FAIL → **PASS** |

**Fix — one block in `config.py`, read by both consumers.** See §8.

### 1e. Event-weighted vs time-weighted inventory — the rail policed the wrong number

**Mean over EVENTS is not mean over TIME.** Inventory is a stock held over time,
so hours are the weights. Averaging the running balance over *events* biases it
upward, because events cluster where activity — and therefore stock — is high:

| | event-wt mean | **time-wt mean** | bias | event daily max | **time daily max** |
|---|---|---|---|---|---|
| ours PCR | 3,928 | **3,894** | +0.9 % | 4,814 | **4,803** |
| ours TBR | 1,148 | **1,086** | **+5.7 %** | 1,603 | **1,414** |
| plant PCR | 4,886 | 4,832 | +1.1 % | — | 5,379 |
| plant TBR | 1,108 | 1,098 | +1.0 % | — | 1,272 |

Two consequences, both real:

1. **The `RAIL/LEDGER MISMATCH` assertion fired on TBR on every single run** — it
   compared the time-weighted grid against an event-weighted ledger mean. It was
   reporting the weighting bias as a leak. Two different statistics.
2. Reported breaches were overstated. "PCR daily max 5,102, 2 days over" was
   event-weighted; the physical value is 4,803 — over by **3 tyres, not 302**.

**Fix** `_time_weighted()` in L7 and `inv()` in the scorecard both sample the step
function hourly. The assertion now compares like with like. The printed table is
labelled `TIME-weighted` and shows the daily-mean max beside the rail.

**Guard** never report or gate `e["bal"].mean()`. It is not the inventory.

### 1h. `shift(1).over(machine)` on a frame sorted by the WALL-CLOCK date — build changeovers over-reported by 21 % / 38 %

**A window function reads the frame's row order, so the sort IS the measurement.**
Both exporters computed the build changeover flag as
`gt_code != gt_code.shift(1).over(["plant","machine"])` on a frame sorted by
`(plant, date, shift, machine, start_ts)` where `date` was
`start_ts.dt.strftime("%Y-%m-%d")` — the **wall-clock** date.

Shift C runs 23:00 → 07:00 and so straddles midnight. Its post-midnight rows
carry the *next* calendar date, and `"A" < "C"`, so they sorted **ahead** of that
date's A and B shifts. 297 PCR and 256 TBR slices sat immediately after a slice
that started later in real time. Every inversion invented a GT transition:

| | reported | **true** | inflation |
|---|---|---|---|
| PCR build changeovers | 967 | **799** | +21.0 % |
| TBR build changeovers | 1,108 | **800** | +38.5 % |

The true count is provable without the flag at all: a changeover is a run
boundary, so `changeovers = runs − machines` = 810 − 11 = **799** and
809 − 9 = **800**. Both match exactly.

Blast radius: sheet 5 `changeovers`/`size_changes`, sheet 7 `changeovers`, sheet
9a `build changeovers`, and sheet 10 `build_changeovers` — i.e. every changeover
number the pack has ever shown. **Not** `block_id`, `run_qty` or lot p50: the run
blocking re-sorts chronologically two lines later, so lot sizing was always
correct. **Not** the planner: L11 counts run-starts (810/341 = 2.375) and is
right; only the exporters were wrong.

**Fix** both exporters now sort `["plant","machine","start_ts"]` explicitly
before the shift, with the reason written beside it.

**Guard** never let a display sort double as the ordering for a window function.
If `shift()`/`cum_sum()` is meant to walk time, sort by the timestamp on the line
above it, every time — even when the current sort looks chronological.

**Related, same root cause:** `date` on the exported sheets was the wall-clock
date of the row's own timestamp while `plant_day`/`shift` were plant-day
coordinates, so `(date, shift)` was not a partition of the plant day. Filtering
"01 Jul, shift C" returned 352 of 4,241 PCR tyres — 1,614 of 5,631 build rows
(28.7 %, 135,379 tyres) were labelled with a date one day ahead of their plant
day. It read from the floor as "nothing is built on the night shift" and as
"curing stops a day before building". `date` is now the **plant-day** date;
`cal_date` carries the wall-clock date for anyone who needs it.

### 1f. The rail's shape was fine; its margin was not

The rail was **already** a max-daily-mean check (`_daily_mean_max`), not a
monthly mean — an early diagnosis of "wrong shape" was wrong. The real defect was
that `_cap_ok` is a **pre-flight** check on the grid, and the plan is then
reconciled (FIFO reallocation moves cure times) and the grid truncates to whole
hours, so the shipped profile drifts above what was approved: PCR 4,803 vs a
4,800 rail, TBR 1,414 vs 1,400.

**Fix** `PLANNER_RAIL_MARGIN` (now `0.94`, was 0.99) — check against 99 % of the rail so the
**stated** cap is the one honoured. Result: PCR 4,764 ≤ 4,800, TBR 1,393 ≤ 1,400,
both inside, fulfilment unchanged at 94.4 %.

**Cost, stated plainly:** the tighter TBR rail pushed two marginal TBR invariants
over their lines — changeovers/machine-day 3.49 → 3.58 (gate 3.51) and sub-floor
33.5 % → 35.7 % (gate 34 %). That is the price of honouring the cap on TBR.

**And note:** the 4,800 PCR cap is **tighter than the plant**. Measured
time-weighted, the plant runs a **4,832 mean and a 5,379 daily max** in July. We
sit at 3,894 / 4,764 — 19 % and 11 % below the plant. The cap was set by
instruction and is kept, but it is *not* a plant-derived limit; it is the same
"mined stat as a hard constraint" shape as §1a and §1b. A proper fix would be a
two-pass fixed point (place, reconcile, re-check) instead of a margin.

### 1i. An arm that differs by a FILE cannot be A/B'd by `arm_scorecard.py` — 0.7 pt of phantom gain

`scripts/arm_scorecard.py` separates arms by **environment variable only**. Every
arm inside one invocation plans against whatever masters happen to be on disk at
that moment. So when the thing under test is a *file* — `allowed_machine_matrix.parquet`,
`gt_machine_partition.parquet`, any mined master — the script cannot see the
difference, and a "table" assembled across several invocations with the file
rewritten in between has rows that came from different inputs while reading as one
experiment.

**How it showed up (2026-08-17).** A three-way July table was reported as

| arm | PCR ful% |
|---|---|
| base (no partition) | 95.8 |
| + CP-SAT partition | 96.4 |
| + partition + weightage | 96.6 |

Re-running the top arm against the state actually left on disk returned **96.0**.
Two *identical* arms in one invocation returned the same number **to the tyre** on
both plants, so the engine is deterministic and this was not noise. Re-measured
with the matrix copied into place before each arm, the real baseline is **95.1** —
the "base" row above had been planned with the *widened* matrix already on disk,
so it was never a baseline at all.

The direction of the finding survived; the size and the baseline did not. The
August gate run the same day **is** sound, because its driver script set the
matrix explicitly before each arm.

**Fix.** `arm_scorecard.py` now prints a `SHARED FILE INPUTS` banner — sha1, size
and the partition's month stamp — above every table, and its docstring states the
limitation. To A/B a file, drive it from a script that copies the file into place
before each arm. Do not put two file-states in one invocation; it is not
measuring what the column header says.

**Class.** This is the measurement-error family of §1e (mean over events vs over
time) and §4 (denominator defects), not the mined-statistic family of §1a–1c: the
engine was right and the *instrument* was wrong. Fourth instance.

---

## 2. THE PARTITION (Fix 1) — `scripts/build_gt_machine_partition.py`

### Why a partition and not a pin

The plant keeps **66.7 %** of PCR GTs on exactly one machine for a whole month
*and* holds 91.5 % same-size changeovers. We were at 27.5 % / 86.9 %. Pinning
our dynamic assignment harder was tried **twice** and made weighted setup
**worse** both times:

| arm | 1-machine GTs | machines/GT | same-size | weighted setup |
|---|---|---|---|---|
| dynamic | 27.5 % | 2.12 | 86.9 % | 242 h\* |
| pin to home machine | 32.5 % | 2.08 | 79.4 % | **289 h** ❌ |
| pin ∩ rim lock | 37.5 % | 1.95 | 82.3 % | **264 h** ❌ |

\*old flat-cost basis, see §1c.

**A rigid bad partition is worse than a fluid one.** The plant's machine→size
map is a *partition*: each machine owns a size, the GTs of a size are split
across that size's machines **once**, and nothing moves. Forcing our
load-balanced assignment to stop bouncing does not create that object — it makes
a bad assignment rigid. The script builds the object itself.

### The booking order — the plant's own, from the 8-month matrix

| tier | rule | PCR | TBR |
|---|---|---|---|
| **1 pure** | one size at ≥95 % of 8-month volume. **Booked first, filled with its own size before anything else is placed.** | 10, 7 →R13 · 1→R17 · 3→R15 · 9→R18 | 1,2,3,7,8 →R20 |
| **2 multi** | 2–3 sizes at ≥5 %. Take their historical size set, primary first. | 4→R12 · 6→R14 · 11→R16 · 5→R13,R14 · 8→R15,R17 | 4,5,6→R22.5 · 9→R20 |
| **3 flex** | the 5-size machine — the plant's own tail absorber | **TBMPCR2** (R18 61 %, R17 13 %, R14 11 %) | none |
| **4 overflow** | anything unseated → **cheapest machine to change size on**, which is then **locked to that size** so an overflow never costs a size change | | |
| **5 split** | only when no single machine has room | | |

GTs are seated **largest-first (LPT) and whole**. Big GTs (>250 h,
`PLANNER_PART_SPLIT_H`) are split across two **same-size** machines on purpose —
that split is free on changeovers and relieves the time pressure that otherwise
forces splitting *in time* and burns the B12 budget.

### The repair pass — G4, "prefer size changes where they cost least"

Greedy tier-ordered seating strands rims on the wrong machine. Measured before
the pass: **TBMPCR2 (60 min, the dearest machine) carried 395 h of R16+R17 on
top of its own R18 and took ALL 21 of our different-size changeovers**, while
**826 h sat free on 42-min machines** — TBMPCR11 alone had 306 h free and R16 is
its primary rim.

For every machine holding a rim that is not its primary, relocate that load,
preferring:
1. a machine **already locked to that rim** — the size change *disappears*
2. failing that, a machine where a size change is **cheaper**

A move is taken only if it strictly reduces cost, so the pass can never make the
plan worse.

**Effect: cost per different-size changeover 60.0 -> 48.9 min, now below the
plant's 55.7. Fulfilment +0.9 pt as a side effect.**

### PCR only — by measurement, not by assumption

| applied to | PCR same | TBR same | PCR setup | TBR setup | TBR 1-mach | fulfilment |
|---|---|---|---|---|---|---|
| **PCR** (default) | 97.7 % | 100 % | 181 h\* | **183 h\*** | 30.6 % | **93.4 %** |
| PCR,TBR | 97.7 % | 100 % | 181 h\* | 185 h\* | 34.7 % | 92.8 % |

TBR is already at 100 % same-size with or without it — two sizes across nine
near-identical machines is not a partitioning problem. It buys 4.1 pt of
stickiness for 0.6 pt of demand. `PLANNER_PARTITION_PLANTS=PCR,TBR` to enable.

The reason PCR benefits and TBR does not is visible in the spreads:
**PCR machines run 49–78 s/tyre (59 % spread) across 7 rim groups; TBR 189–219 s
(16 %) across 2.**

---

## 3. PORTABILITY CONTRACT — read before running another month

### Rebuild per month, always

```bash
python scripts/build_gt_machine_partition.py <YYYY-MM>
```

The partition is sized against **one month's demand and that month's calendar
hours**. Three bugs made it silently month-specific; all three are fixed:

| bug | was | now |
|---|---|---|
| **hardcoded 744 h month** | over-allocated a 28-day month by **10.7 %**, a 30-day by 3.3 % | `calendar.monthrange` |
| **flat plant cadence** | 58 s for every PCR machine when the mined range is **49–78 s** — ±15 % capacity error at a 95 % pack | per-machine from `cycle_time_building.parquet` |
| **no month stamp** | July's partition would be used for August **silently** — a wrong answer that looks right | `month` column + L7 guard |

The guard, verified to fire:

```
!! PARTITION IS FOR 2026-05, THIS RUN IS 2026-07 -- ignoring it and falling
   back to the dynamic assignment. Rebuild with:
   python scripts/build_gt_machine_partition.py 2026-07
```

It falls back to the dynamic assignment (95.8 %) rather than producing a wrong
plan. **Do not "fix" this by dropping the check.**

### Month-independent (never rebuild)

- the **machine x size matrix** — mined over all 8 months
- the **tier structure** (pure / multi / flex) derived from it
- **changeover minutes** — `v_changeover_build`, a plant master with no month
- the **booking order** itself

### Degradation paths, all non-fatal

| condition | behaviour |
|---|---|
| GT has no rim in `gt_size` | reported by name, skipped; L7 uses dynamic assignment for it |
| GT's rim has no locked machine | same |
| demand file for the month missing | hard error naming the file — never a silent empty partition |
| `v_changeover_build` unavailable | warns, falls back to `(22,42)/(10,24)`, cost preference off |
| a GT does not fit any single machine | tier-5 split across same-size machines, reported |
| capacity genuinely short | `!! UNSEATED` with the GT and hours |

---

## 4. WHERE WE STAND vs THE PLANT — `runs/v28`, July 2026

Regenerate with `python scripts/rulebook_scorecard.py runs/v28 2026-07`.

**Better than the plant**

| rule | metric | plant | ours |
|---|---|---|---|
| B3 | PCR same-size share | 91.5 % | **97.7 %** |
| G4 | PCR cost per different-size CO | 55.7 min | **48.9 min** |
| B4 | PCR size changes / machine-day | 0.2 | **0.0** |
| B14 | PCR GTs per machine | 5.9 | **5.5** |
| G8 | PCR GT inventory (time-wt) | 4,832 | **3,894** |
| G8 | PCR inventory daily-mean max | 5,379 | **4,764** (rail 4,800) |
| E1 | PCR GT wait mean | 8.8 h | **8.1 h** |
| R5 | GT wait max | 684.9 h | **71.4 h** |
| G1 | overbuild ratio | 1.00 | 1.00 |

**Still behind**

| rule | metric | plant | ours | why |
|---|---|---|---|---|
| G7 | **fulfilment** | 100 % | **95.4 %** | L5 horizon overflow + `no feasible release` contention |
| B12 | PCR run size p50 | 363 | **289** | `SLICE_MULT` 3.0 reaches 316, 4.0 reaches 379 — costs fulfilment (§4c) |
| B6 | PCR changeovers | 742 | **920** | follows directly from run size |
| G4 | PCR total setup h | 344 | 385 | count term, not the type term |
| P7 | GTs on 1 machine | 66.7 % | 52.5 % | |
| B7 | **daily build CV** | **0.031** | **0.13** | no daily build quota |
| C4 | cure changeovers/press-day | 0.1 | 0.4 | no minimum mould campaign |

### The one number that explains the rest

**Setup hours = count x cost per changeover.** We already win on cost
(25.1 vs 27.8 min/CO) — the same-size and cheap-machine work is done. We lose on
count: **920 vs 742 (+24 %)**, because our run is **287 vs 363 (−21 %)**.

TBR proves it with no ambiguity: **both sides have zero different-size
changeovers**, so setup is purely proportional to run count — 974 vs 889.

**The remaining setup gap is not a changeover problem. It is the run-size
problem, and it is the same thing blocking fulfilment.** Fix run size and both
move together. Do not spend more effort on size-mixing.

---

## 4b. MEASURED AND REJECTED: a daily build quota (B7)

**Do not build this.** It was proposed off a headline CV of 0.126 vs the plant's
0.031, with the reasoning that a ±15 % daily band would flatten the profile and
help the WIP cap at the same time. Every part of that was wrong.

**The interior is already at plant level.** CV on shrinking windows:

| window | plant PCR | ours PCR | plant TBR | ours TBR |
|---|---|---|---|---|
| all 31 days | 0.031 | **0.126** | 0.095 | **0.218** |
| drop days 1 + 31 | 0.031 | **0.046** | 0.096 | 0.143 |
| days 2–29 | 0.031 | **0.045** | 0.097 | **0.059** ← *we beat the plant* |

The entire apparent gap is **days 1, 30 and 31**. Only **2 of 31 days** fall
outside a ±15 % band on either plant, and both are month boundaries. A band would
do nothing to the interior — it is already inside — and would fight the boundary,
where the low volume is structural.

**The boundary is not worth much either.** Of 23,587 starved tyres, only
**1,793 (0.4 pt)** are physically unreachable from an empty machine state (a cure
at hour *h* needs its GT built by *h* − tau_min, and build cannot start before
hour 0). Carry-in is therefore capped at ~0.4 pt. We already *emit*
`carry_out.parquet` (3,333 tyres, 31 slices) but nothing consumes a carry-**in**.

Days 30–31 are low for a different reason: `l45_lots_2026-07.parquet` holds only
July cure demand, so **nothing pulls build on July 30–31**. The plant builds on
July 31 for early-August cures. That is a demand-horizon issue, not a pacing one.

**And a band would be a fourth constraint of the class that has already cost us
most.** The WIP rail alone costs **1.5 pt** while moving interior CV by 0.002:

| | fulfilment | starved | PCR daymax | PCR interior CV |
|---|---|---|---|---|
| rail on | 94.3 % | 23,587 | 4,764 | 0.046 |
| rail off | **95.8 %** | 16,509 | 6,423 | 0.044 |

**The "build spikes cause the WIP breach days" claim was also wrong.**
corr(daily build, daily inventory) = +0.52 on PCR, but peak inventory is day 16
and peak build is day 4 — they do not coincide.

**What the starvation actually is:**

| reason | tyres | share |
|---|---|---|
| PCR `would breach min_lot` | **13,833** | **59 %** |
| TBR `no feasible release` | 5,592 | 24 % |
| PCR `no feasible release` | 4,162 | 18 % |

59 % is the **run-size** problem (§4), not pacing. Fix run size.

---

## 4c. THE `would breach min_lot` REFUSAL — correct mechanism, unfinished outcome

23,587 tyres starve. The breakdown, and the verdict on each:

| reason | tyres | share | verdict |
|---|---|---|---|
| PCR `would breach min_lot` | **13,833** | 59 % | **mechanism correct, outcome not** |
| TBR `no feasible release` | 5,592 | 24 % | placement/capacity |
| PCR `no feasible release` | 4,162 | 18 % | placement/capacity |

### Why the mechanism is correct

It is the §1b budget doing its job. It stops us fragmenting past the plant's own
tolerance: **13.5 % sub-floor against the plant's 12.7 %.** Remove it and we go to
18.7 %, *looser* than the plant. Do not raise the budget to make the number go
away — that was measured (§1b, `340/800` → 23.5 % sub-floor, over the plant).

### Why the outcome is not correct

**The plant delivers 100 % while sitting at 12.7 % sub-floor.** So this volume is
not inherently unbuildable — we simply cannot place it without splitting, and the
plant can. The gap is real.

### It is NOT a lot-size-target problem — T does not move it

`T` was re-swept with the partition, the τ_min release and the rail margin all
live, and with inventory sitting 19 % below the cap so there was headroom to spend:

| T | run p50 | runs | inventory | daily max | sub-floor | **min_lot refused** | setup h | fulfilment |
|---|---|---|---|---|---|---|---|---|
| **16** | 288 | 928 | 3,893 | 4,764 | 13.5 % | **13,833** | 384 | **94.3 %** |
| 20 | 288 | 843 | 4,194 | 4,794 | 14.4 % | 14,126 | 351 | 94.0 % |
| **24** | 291 | **783** | 4,304 | 4,782 | 13.7 % | 14,053 | **324** | 93.6 % |
| plant | **363** | 753 | 4,832 | 5,379 | 12.7 % | 0 | 344 | 100 % |

Two things this settles:

1. **The refusal is invariant to T** (13,833 → 14,126 → 14,053). It is not caused
   by the lot-size target, so raising T will never fix it.
2. **The median run size is pinned at ~288 regardless of T.** T consolidates the
   *large* runs — count falls 928 → 783 — but the median does not move. Something
   other than `Q_g = r_g x T` sets the median, and that something is the
   cure-deadline grouping in L7, not the lot arithmetic.

**T = 24 is nonetheless a real operating point worth knowing:** setup **324 h
beats the plant's 344 h**, run count 783 ≈ plant 753, inventory 4,304 mean /
4,782 daily max still inside the 4,800 rail — for **0.7 pt** of fulfilment.
`PLANNER_LOT_INTERVAL_H=24`.

### The actual root cause: a 48-tyre slice quantum vs a 150 floor

Build slices are a **fixed quantum** — PCR **48 tyres at p25, p50 AND p75**, TBR
29. Another flat wall (§1). A run is therefore an integer number of slices, and a
split can only land on a slice boundary.

**The median PCR run is 6 slices = 288 tyres. Halved that is 144 + 144, against a
150 floor — both halves breach, by 6 tyres.** For both halves of a split to clear
the floor a run needs **≥ 8 slices (384 tyres)**; the target `Q_g = r_g x T` gives
6. So the median run is *arithmetically unsplittable*, and every rescue attempt
charges the B12 budget until it is exhausted — 289 further groups then refuse.
That is the whole 13,833.

### Machine-choice freedom is NOT available — 4 of 7 PCR rims are single-machine

| rim | machines | free same-size spill? |
|---|---|---|
| R13 | 3 | yes |
| R15, R18 | 2 | yes |
| **R12, R14, R16, R17** | **1** | **no — any spill is a SIZE CHANGE** |
| TBR R20 / R22.5 | 6 / 3 | yes |

So "give placement more machine freedom" costs different-size changeovers on the
majority of PCR rim groups. That lever is closed. TBR has room, but TBR has no
`min_lot` refusals at all.

### The cap is not the cause either

| rail PCR/TBR | inventory | daily max | same-size | run p50 | min_lot refused | fulfilment |
|---|---|---|---|---|---|---|
| **4,800 / 1,400** | 3,893 | 4,764 | 97.7 % | 288 | 13,833 | **94.3 %** |
| 5,400 / 1,300 *(plant's own level)* | 3,964 | 5,341 | 97.7 % | 244 | 12,679 | 94.5 % |
| off | 4,060 | **6,423** | 98.1 % | 242 | 10,483 | 95.8 % |

At the **plant's own inventory level** the cap costs only **0.2 pt**. The 1.5 pt
from removing it entirely requires a 6,423 daily max — 19 % *above* the plant. So
the cap is not what is holding the 13,833. Note also that relaxing the rail makes
runs **smaller** (288 → 242), not larger.

### Tried: floor-aware split point — neutral, kept anyway

Blind halving manufactures *two* sub-floor runs where an uneven 4 + 2 split
(192 + 96) makes only one. Implemented: search split points outward from the
middle, take the first where both sides clear the floor, else the one keeping the
larger side compliant.

| | same-size | run p50 | sub-floor | min_lot refused | setup h | fulfilment |
|---|---|---|---|---|---|---|
| blind halving | 97.7 % | 288 | 13.5 % | 13,833 | 384 | **94.3 %** |
| floor-aware | 97.7 % | 287 | **12.9 %** | 14,549 | 383 | 94.2 % |
| plant | 91.5 % | 363 | 12.7 % | 0 | 344 | 100 % |

It lands sub-floor share **exactly on the plant (12.9 % vs 12.7 %)** and costs
nothing on changeovers, but it is **fulfilment-neutral (−0.1 pt)** — the predicted
gain did not appear, because the uneven remainder then fails to place on its own.
Kept because it is the more correct rule, not because it wins.

### FIXED — `SLICE_MULT_PCR = 2.0`, and the 48 was NEVER a lot-size cap

**The 48 is not a cap anyone set.** It is emergent:

```
n_slices  = round(cure_campaign_hours / (build_band x SLICE_MULT))
slice_qty = campaign_qty / n_slices
```

PCR campaign p50 = 1,228 tyres over ~26 slices = **47-48 tyres**. `SLICE_MULT`
(PCR 1.0, TBR 3.0) is the knob, and PCR was sitting at 1.0 — the finest setting.
Nothing was capping the lot; the slice grain was simply set by the *cure* campaign
length, which had nothing to do with what a build run should be.

Swept, with everything else fixed and live:

| SLICE_MULT | slice | run p50 | runs | sub-floor | **min_lot refused** | setup h | daily max | fulfilment |
|---|---|---|---|---|---|---|---|---|
| 1.0 *(was)* | 48 | 287 | 927 | 12.9 % | **14,549** | 383 | 4,764 | 94.2 % |
| **2.0** *(shipped)* | **96** | 289 | 911 | **12.3 %** | **0** | **377** | **4,778** | **95.4 %** |
| 3.0 | 144 | 316 | 746 | 11.9 % | 0 | **315** | 4,884 | 93.0 % |
| 4.0 | 193 | **379** | 735 | 0.3 % | 0 | 309 | 4,871 | 91.6 % |
| plant | 363 | 363 | 753 | 12.7 % | 0 | 344 | 5,379 | 100 % |

**At 2.0 the `would breach min_lot` reason disappears entirely — 14,549 to zero —
and fulfilment gains 1.2 pt.** Halving the slice count doubles the slice to 96, so
a 3-slice half is 288 and a 2-slice half 192, both clear of the 150 floor. The
arithmetic trap in §4c is gone.

`RAIL_MARGIN` had to go 0.99 -> **0.97**: coarser slices mean coarser grid
buckets, so post-reconcile drift grew and the daily max hit 4,886 at 0.99. At 0.97
it is 4,778 (PCR) and 1,363 (TBR), both inside.

**Result v25 -> v27:**

| | v25 | **v27** | plant |
|---|---|---|---|
| fulfilment | 94.3 % | **95.4 %** | 100 % |
| `min_lot` starvation | 14,549 | **0** | 0 |
| PCR sub-floor | 12.9 % | **12.3 %** | 12.7 % |
| PCR same-size | 97.7 % | 96.5 % | 91.5 % |
| PCR setup h | 383 | **377** | 344 |
| PCR daily max | 4,764 | 4,778 | 5,379 (rail 4,800) |
| TBR daily max | 1,397 | **1,363** | 1,272 (rail 1,400) |
| slices per cure campaign | 30.0 | **15.2** | target ~11 |

**Costs, stated:** PCR median GT wait 4.86 -> 5.48 h (plant 4.76) and the PCR
same-day build/cure correlation slips to 0.885 against a 0.90 gate — bigger slices
occupy a machine longer, so releases push earlier. TBR sub-floor 35.7 -> 36.5 %
against the plant's 30.8 %. Net 17/34 invariants against 20, but **the 13,833-tyre
defect is closed and fulfilment clears 95 %.**

**Why the old code chose 1.0:** the docstring records that sizing slices from
*build* hours gave plant-matching runs (386 vs 363) but cost fulfilment
98.4 % -> 94.2 %. That was measured before τ_min release, the partition and the
rail existed. `SLICE_MULT` reaches the same shape by a different route and the
trade has since inverted — **re-measure a rejected experiment when its baseline
has changed.**

Remaining starvation is now entirely `no feasible release` (PCR 12,471, TBR
5,858) — genuine machine/press contention, not a shape constraint.

---

## 4d. FIX 5 (cure changeovers) WAS A PHANTOM — denominator mismatch

Reported as **0.38–0.49 mould changes per press-day against the plant's 0.08**, a
5x defect. It does not exist. The two sides were divided by different things:

* **plant** — every `(press, day)` on which that press cured. Each cure event is
  its own row, so this is press-days *occupied*: **2,658** on PCR.
* **ours** — `(press, start_date)` of each CAMPAIGN. Our campaigns run **10–12
  days**, so this counted one day per campaign: **256**. A ~10x undercount.

Apples-to-apples, press-days occupied on both sides:

| | mould changes | press-days | **per press-day** | |
|---|---|---|---|---|
| plant PCR | **217** | 2,658 | **0.08** | |
| ours PCR | **98** | 2,689 | **0.04** | **half the plant's rate** |
| plant TBR | 107 | 2,440 | 0.04 | |
| ours TBR | 93 | 2,405 | **0.04** | matched |

**We make 98 mould changes against the plant's 217 in absolute terms.** C3 is not
violated either: zero PCR campaigns fall under 40 h — ours are p50 **193 h**,
*longer* than the band, not shorter.

**Fix** both `l11_validate_plan` and `rulebook_scorecard` now expand each campaign
to the days it occupies, and the L11 gate is the plant's own 0.08 / 0.04 rather
than the stale 1.43 / 1.19. Both PASS.

**Guard** when a metric divides plant EVENT rows by our CAMPAIGN rows, check the
denominator before believing the ratio. This gate passed the whole time, which is
why the wrong number went unnoticed — a passing check is not a correct check.

## 4k. THE PLANT SUB-FLOOR FIGURES WERE MEASURED WRONG — supersedes §1b/§4c/§4g

**Every "plant run size" figure quoted before 2026-08-09 that reads p50 235,
p10 1 tyre or 39.9 % sub-floor is an ARTEFACT. Do not repeat them.**

Two errors compounded:

1. the block stream was joined to **only tyres cured within the same month**, so
   any run whose tyres cured in the next month was truncated to its in-month part;
2. blocks were split at a **1 h** gap, which cuts a single continuous machine run
   wherever the MES event stream has a routine pause.

Re-measured on the full `v_build` stage-2 stream with a **>=4 h** gap cutoff:

| | runs | p10 | **p50** | p99 | max | **sub-floor** |
|---|---|---|---|---|---|---|
| plant PCR (floor 150) | 718 | **123-137** | **363-381** | 2,609 | **40,917** | **12.7 %** |
| plant TBR (floor 70) | 870 | 29 | **89** | 448 | 5,309 | **30.8 %** |

**The plant runs LARGER blocks than we do, not smaller.** Ours at the same cutoff:
PCR 883 blocks / p50 310, TBR 1,021 / p50 87. The plant's tail is 8.3x ours on PCR
(40,917 vs 4,909) — it parks a machine on one GT for days while curing
concurrently, which costs no inventory because the block drains as it is built.

**A BLOCK-SIZE STATISTIC IS MEANINGLESS WITHOUT ITS GAP CUTOFF.** Sweep the
cutoff and state it beside every figure, on both sides of any comparison.

### The plant does NOT decouple build from cure with GT inventory

Measured July 2026, per-tyre `build -> cure` lag:

| | plant PCR | ours PCR | plant TBR | ours TBR |
|---|---|---|---|---|
| p50 lag | 4.83 h | 4.70 h | 4.96 h | 4.58 h |
| cured within 8 h | 65.3 % | 63.0 % | 63.8 % | 63.9 % |
| GTs curing on fewer days than they build | 0 / 38 | 0 / 38 | 0 / 45 | 0 / 42 |

The GT buffer is ~5 hours on both sides. A hypothesis that the plant builds a
large block and cures it over days is **refuted** — its large blocks come from
machine dedication, not from inventory.

---

## 4e. FIX 4 (the last points) — v29-ERA, SUPERSEDED

> ⚠️ **The numbers in this section are `runs/v29` (95.4 %) and were unlabelled.**
> They were quoted as current for two sessions after the baseline moved. On
> `runs/v31`/`v32` the same reasons read **PCR `no feasible release` 13,743 / 94
> groups** and **L5 `past horizon` 3,174** (3,044 PCR + 130 TBR). Always name the
> run beside a starvation figure. See §4j for the current decomposition.

| loss (v29) | tyres | pt | cause |
|---|---|---|---|
| L5 `past horizon` | **3,972** | 0.8 | 12 PCR + 2 TBR GTs whose campaigns do not fit the month |
| L7 `no feasible release` | **18,066** | 3.7 | PCR 12,471 / 131 groups · TBR 5,595 / 192 groups |

`would breach min_lot` is **gone** (§4c), so this is now the *entire* remaining
gap and it is all genuine machine/press contention plus the horizon boundary.
Neither is a cap, an inventory limit, or a shape constraint — those are all clear.

The horizon 0.8 pt needs a demand lookahead (see §4b: `l45_lots` holds only the
current month, so nothing pulls build for early next-month cures). The 3.7 pt is
contention, and §4c records that machine-choice freedom is closed on 4 of 7 PCR
rims.

---

## 4f. WHAT A ROW IN THE OUTPUT IS — slice vs run, and a `run_id` caveat

`build_schedule.parquet` and `build_by_shift.parquet` carry **one row per SLICE**,
not per setup. A slice is a **delivery** — when a batch of GT is handed to a
specific press — and after `SLICE_MULT=2.0` it is ~96 tyres on PCR, ~58 on TBR.

**100 % of slice rows are "below the 150 floor", and that is meaningless.**
Consecutive slices in a run share machine, GT and back-to-back timestamps: one
`run_id` = one setup. Always group by `run_id` before judging lot size.

| PCR | rows (slices) | setups (`run_id`) |
|---|---|---|
| count | 3,896 | **906** |
| qty p50 | 96 | **289** |
| below 150 floor | 100 % *(irrelevant)* | **12.1 %** *(plant 12.7 %)* |

**Raising `SLICE_MULT` made the output COARSER, not finer** — 48 → 96 tyres per
row, half as many rows. It did not create small lots; it removed the arithmetic
trap that was refusing volume (§4c).

### The caveat: a `run_id` group can contain a time gap

Measured on v29, **8.8 % of PCR `run_id` groups contain a gap > 1 h** (max 41.8 h).
Those are not one continuous setup, so `run_id` slightly *understates* the setup
count. Splitting at every >1 h gap gives the honest figures:

| | `run_id` groups | **true setup blocks** | p50 | **sub-floor** | plant |
|---|---|---|---|---|---|
| PCR | 906 | **1,025** | 287 | **12.4 %** | 753 · 363 · 12.7 % |
| TBR | 993 | **1,051** | 86 | **34.4 %** | 898 · 87 · 30.8 % |

**The conclusions survive** — PCR sub-floor 12.4 % is still under the plant's
12.7 %, and run p50 barely moves (289 → 287). But the **changeover count is
understated by ~13 %**: 1,014 true setups against the 895 reported, versus the
plant's 742. Quote the true-block number when comparing changeovers.

---

## 4g. THE THREE REMAINING "ISSUES" ARE ONE DIAL — the run-size frontier

PCR run size (287 vs 363), changeover count (1,014 vs 742) and TBR sub-floor
(34.4 % vs 30.8 %) are **not three defects. They are one knob, and they are all
fully solvable — each at a measured cost in fulfilment.**

### Root cause, exactly

**99 % of PCR sub-floor runs are 1-SLICE runs**, and a 1-slice run is 96 tyres
against a 150 floor. TBR: 64 % are 1-slice, 29 tyres against a 70 floor. So the
sub-floor share is almost entirely "the slice is smaller than the floor".
**Make the slice bigger than the floor and 1-slice runs become legal.**

### The frontier (true setup blocks, split at >1 h gaps)

| SLICE_MULT PCR/TBR | PCR slice | PCR run p50 | PCR sub-floor | TBR slice | TBR sub-floor | PCR setups | PCR setup h | **fulfilment** |
|---|---|---|---|---|---|---|---|---|
| **2.0 / 3.0** *(shipped)* | 96 | 287 | 12.4 % | 29 | 34.4 % | 1,025 | 377 | **95.4 %** |
| 3.5 / 6.0 | 167 | 336 | **0.5 %** | 57 | 27.9 % | 883 | 330 | 92.2 % |
| **3.5 / 8.0** | 167 | **336** | **0.5 %** | **76** | **1.9 %** | 883 | **330** | 91.5 % |
| 4.5 / 8.0 | 215 | **424** | 0.7 % | 76 | 1.9 % | **769** | **273** | 87.8 % |
| plant | 363 | 363 | 12.7 % | 87 | 30.8 % | 753 | 344 | 100 % |

Read it as a curve, not a set of options:

* **`3.5/8.0` closes all three gaps at once** — PCR run 336 (93 % of plant), PCR
  sub-floor 0.5 %, TBR sub-floor 1.9 %, and setup **330 h which BEATS the plant's
  344 h**. It costs **3.9 pt of fulfilment**.
* **`4.5/8.0` beats the plant on every changeover metric** — run 424 > 363, setups
  769 ≈ 753, setup 273 h vs 344 h. It costs **7.6 pt**.
* The shipped `2.0/3.0` is the **fulfilment-maximising** end of the same curve.

### Why the trade is real and not a bug

A bigger run occupies its machine for longer, so it is harder to fit against a
cure deadline; volume that cannot be placed is starved. `no feasible release` is
already the entire remaining loss (§4e), and every increase in run size adds to
it. This is the same mechanism the old L7 docstring recorded when it chose the
finest slice: *"a larger slice occupies a machine longer, so contention rises."*

**So these three lines in the scorecard are a CHOSEN OPERATING POINT, not
outstanding work.** Do not "fix" them without deciding which end of the curve is
wanted — and note that the plant sits at neither end: it achieves plant-level run
size AND 100 % fulfilment because it runs continuously across the month boundary
(MEMORY §10c), which a cold-start month cannot reproduce.

---

## 4h. THE SLICE RULE, DERIVED — how the lot floor is finally honoured

### Root cause of every lot-size symptom

The slice count was `n = cure_hours / (build_band x SLICE_MULT)` — **a tuned
multiplier with no relationship to either rule that actually bounds a slice.**
That is why the lot floor could never be held: nothing in the formula knew the
floor existed.

### The correct formulation

A campaign of `Q` tyres drawn over `H` hours, delivered in `n` slices:

```
slice qty  = Q/n
slice wait = H/n + (Q/n)*cadence          window + its own build time
```

Two rules bound it, from opposite sides:

```
R5  (HARD, shelf life)   wait <= SHELF - tau_min   =>  n >= (H + Q*cad)/71.7   [n_R5]
B12 (lot floor)          Q/n  >= min_lot           =>  n <= Q/min_lot          [n_B12]
```

**A legal `n` exists iff `n_R5 <= n_B12`.** Measured on the July campaign set:

| | n_R5 (p50) | n_B12 (p50) | **both satisfiable** |
|---|---|---|---|
| PCR | 3 | 8 | **100 % of campaigns** |
| TBR | 4 | 6 | **99 %** (2 campaigns / 256 tyres too slow-drawing) |

**The plant's lot size is not a policy to copy — it falls out of the shelf life
and the floor.** At `n = n_R5` the PCR slice is p50 **361 tyres**; the plant's
own build run is **363**.

### Which end of the window to sit at — `SLICE_AGGR`

Every `n` in `[n_R5, n_B12]` satisfies both rules. The choice inside is purely
how long one slice occupies a machine:

| SLICE_AGGR | PCR slice | PCR lot p50 | **PCR sub-floor** | TBR sub-floor | setup h | fulfilment |
|---|---|---|---|---|---|---|
| 0.0 = n_R5 | 342 | 368 | 0.7 % | 1.9 % | 193 | 78.0 % |
| 0.5 | 214 | 416 | 0.8 % | 0.8 % | 269 | 88.4 % |
| 0.8 | 175 | 353 | 0.4 % | 1.3 % | 310 | 90.2 % |
| **1.0 = n_B12** *(shipped)* | 155 | 312 | **0.1 %** | **1.9 %** | 338 | **91.3 %** |
| *old multiplier (v29)* | 96 | 287 | **12.4 %** | **34.4 %** | 377 | **95.4 %** |
| plant | 363 | 363 | 12.7 % | 30.8 % | 344 | 100 % |

**Take the SMALLEST legal slice.** Larger slices hold a machine longer, so
contention rises and volume starves — and they arrive in bigger lumps, so the WIP
rail binds harder (it costs 1.5 pt at slice 96 but **3.4 pt** at slice 155).

### v30 result — the floor is honoured

| | v29 | **v30** | plant | cap |
|---|---|---|---|---|
| PCR runs below floor | 12.4 % | **0.1 %** | 12.7 % | |
| TBR runs below floor | 34.4 % | **1.9 %** | 30.8 % | |
| PCR lot p50 | 287 | **312** | 363 | |
| TBR lot p50 | 86 | **92** | 87 | matched |
| TBR setups | 1,051 | **648** | 898 | fewer than plant |
| PCR GT inv daily max | 4,778 | **4,679** | 5,379 | 4,800 ✅ |
| TBR GT inv daily max | 1,363 | **1,373** | 1,281 | 1,400 ✅ |
| R5 max | 69.7 / 71.9 | **71.2 / 69.8** | 684.9 | 72 h ✅ |
| L11 invariants | 20/34 | **24/34** | | |
| **fulfilment** | **95.4 %** | **91.3 %** | 100 % | |

`gt_wip_rail_margin` went 0.97 -> **0.94** because the lumpier arrivals needed
more headroom to keep the stated cap.

### Why "all at once" is not available from a cold start

The plant holds a 363 lot AND 100 % fulfilment because **its month is a window on
a continuous process** — campaigns are already in progress at the boundary
(MEMORY §10c). Our month starts with idle machines. The two structural fixes are
carry-**in** of in-progress campaigns and a demand lookahead into next month
(§4b/§4e), together worth ~1 pt measured. Everything else is this frontier.

**`PLANNER_SLICE_AGGR`** moves along it; **`PLANNER_SLICE_MULT_PCR/_TBR`**
(non-zero) restores the old multiplier arm for A/B.

---

## 4i. IT WAS NEVER A COLD START — the opening stock was sitting unused

The "cold start" framing in §4h was **wrong**. We hold real opening GT inventory
and it was not being used to start the presses.

| | opening GT held | used | **unused** |
|---|---|---|---|
| PCR | ~~6,960~~ **4,820** | 4,190 | ~~2,770 (40 %)~~ **630 (13 %)** |
| TBR | ~~2,330~~ **1,297** | 1,010 | ~~1,264 (54 %)~~ **287 (22 %)** |

> **⚠ CORRECTED 2026-08-09 — the "held" column was the WRONG FILE.** 6,960 /
> 2,330 is `warehouse/derived/opening_gt_inventory.parquet`, a **December-31
> snapshot** (`max(latest_built) = 2025-12-31 23:59:58`), divided into **July**
> usage. July's own master `masters/opening_gt/opening_gt_2026-07.parquet` holds
> **4,820 / 1,297**. Same denominator-mismatch class as §4d. The real utilisation
> is **87 % / 78 %**, not 60 % / 46 % — opening stock is very nearly exhausted,
> and the "large pool of unused stock" this section reported does not exist.

Meanwhile **every press idled for the first 11.86 h (PCR) / 10.18 h (TBR)** —
`earliest_cure()` returned a flat `t0 + tau* + build_band` for all of them. Build
started at t0+0.00 h; the first cure could not begin until t0+11.86 h.

`EARLY_STOCK` — which lifts that floor for a GT that holds enough stock to cover
the gap — had been **off**, on a note reading *"MEASURED NET NEGATIVE, 98.9 % ->
98.7 %"*. That was the 98.9 %-era engine: before tau_min release, the partition,
the derived slice rule and the rail margin. **Its baseline no longer exists.**

Re-measured on the current engine:

| floor basis | early stock | first cure | PCR daymax | **fulfilment** |
|---|---|---|---|---|
| star | off *(was)* | 11.86 h | 4,679 | 91.3 % |
| **star** | **ON** *(shipped)* | **0.00 h** | 4,611 | **93.6 %** |
| min | off | 7.81 h | 4,592 | 90.3 % |
| slice | off | 0.27 h | 4,551 | 90.1 % |

**+2.3 points.** And note the two floor-lowering arms: they let L5 place MORE
(491,539 vs 488,860) yet fulfilment FALLS to ~90 %, because L7 cannot feed a
campaign that starts before any tyre exists. **Stock is what makes an early start
real; a smaller constant does not.** The correct bound is the GAP — stock need
only cover `(tau* + band) x draw rate`, ~72 tyres on PCR against ~120/GT held.

### v31 — the shipped plan

| | v29 | v30 | **v31** | plant | cap |
|---|---|---|---|---|---|
| **PCR runs below floor** | 12.4 % | 0.1 % | **0.2 %** | 12.7 % | ✅ |
| **TBR runs below floor** | 34.4 % | 1.9 % | **0.2 %** | 30.8 % | ✅ |
| PCR lot p50 | 287 | 312 | **309** | 363 | |
| TBR lot p50 | 86 | 92 | **141** | 87 | **above plant** |
| TBR setups | 1,051 | 648 | **665** | 898 | **below plant** |
| PCR GT inv daily max | 4,778 | 4,679 | **4,611** | 5,379 | 4,800 ✅ |
| TBR GT inv daily max | 1,363 | 1,373 | **1,373** | 1,281 | 1,400 ✅ |
| R5 max | 69.7 / 71.9 | 71.2 / 69.8 | **59.8 / 66.6** | 684.9 | 72 h ✅ |
| **fulfilment** | 95.4 % | 91.3 % | **93.6 %** | 100 % | |

~~**Still open:** 2,770 PCR and 1,264 TBR tyres of opening stock remain
unconsumed (60.2 % / 45.8 % utilisation)…~~ **CLOSED, 2026-08-09 — see §4p.**
The true residual is **630 PCR / 287 TBR (13 % / 22 %)**, it sits entirely on GTs
that have **zero** day-1 shortfall, and it is not withheld by anything: an exact
per-tyre shelf-life ladder draws the identical 4,190 / 1,010 tyres.

---

## 4j. THE RIM LOCK WAS NOT STARVING PCR — and 15 sweep directories were lying

**Reference run `runs/f_solo`, July 2026. PCR 95.66 % → 97.13 % (+1.47 pt),
same-size 96.5 → 92.7 % (plant 91.5 %), TBR untouched, 0 invariant flips.**

### 4j.1 The measurement defect first — it invalidated the evidence

`runs/hl_00 hl_01 hl_10 hl_11 rr_* sm_* fin_* perplant` — **15 directories** —
each carried an `l11_invariants.parquet` belonging to a DIFFERENT arm. Eight were
byte-identical to `runs/v31`'s, seven to `runs/v29`'s. Every HARD_LOCK arm
therefore read 93.6 % / 96.1 % same-size / 22-of-34 PASS while
`build_starved.parquet` in the same directory showed PCR starvation moving
13,743 → 5,336.

Cause: the documented seeding step `cp -r runs/<prev> runs/<new>` (§7) inherits
L1–L6 artefacts **and the previous arm's L11 result**; L7 is re-run, L11 is not.
DO-NOT #8 already forbade A/B against an older directory because `RunContext`
does not hash `PLANNER_*`. This is the same class one layer down: **the flag did
reach L7; the scoring layer never saw it.**

Fixed three ways: `scripts/run_arm.py` builds every arm FRESH from L5 (no `cp`
needed — L1–L4.5 artefacts are month-level and live in `warehouse/derived/`);
L11 deletes its own output before scoring and writes `l11_provenance.json`
fingerprinting what it read; `scripts/check_arm_fresh.py` proves it and exits 1.
The guard caught its author re-running L7 after L11 within the hour.

*The fulfilment and starvation columns previously quoted from those directories
were computed from arm-specific artefacts and stand. Every INVARIANT read from
them — same-size, weighted CO, n_g, GT inventory, R5, sub-floor, PASS count —
belongs to another arm.*

### 4j.2 "Three rims over 100 %" was the flat-cadence bug

`l7:need_h` charged the **plant-median** cadence against a per-machine `cap_h`,
while `_place` already used each machine's own rate. PCR runs 49–78 s against a
62 s median. Charged correctly:

| rim | FLAT load | **TRUE load** | **realised** | starved |
|---|---|---|---|---|
| R12 | 113.2 % | **89.5 %** | 75.9 % | 4,680 |
| R13 | 100.7 % | **80.6 %** | 75.4 % | 1,120 |
| R17 | 101.5 % | 103.1 % | 96.4 % | 830 |
| R14 | 94.1 % | 94.1 % | 77.2 % | **4,152** |
| R15 | 73.3 % | 72.7 % | 64.5 % | **2,395** |
| TOT | 86.7 % | **81.6 %** | 69.2 % | 13,743 |

**Only R17 is genuinely over, by 22 h.** And the rim story never explained the
volume anyway: **51.8 % of starved PCR tyres are on rims under 100 %**, R14
(30.2 %) is not on the over-subscribed list at all, and every starved GT has idle
hours on its own rim (R13 starves 1,120 beside **549 idle hours of its own**).

**The real mechanism is temporal fragmentation.** PCR idle is **1,430 h in 508
gaps, p50 1.72 h**, against a p50 run of **5.27 h** — only 19 % of gaps can hold
one, and `_place` walks backwards only (`st < t0` → fail), so idle time after the
ideal release is structurally unreachable. This is `CORRECTION_REGISTER.md` §H4
("slack is shredded, not absent") quantified.

### 4j.3 Relaxing HARD_LOCK: measured, priced, REJECTED

Recomputed from the arm-specific `build_schedule.parquet` (true setup blocks):

| arm | PCR built | same-size | setup h |
|---|---|---|---|
| shipped | 373,431 | **96.5 %** | 392 |
| `HARD_LOCK=0` | 381,516 (**+1.64 pt**) | **69.3 %** | 500 |
| *plant* | — | *91.5 %* | *344* |

69.3 % is far below the plant's 91.5 %, the kill line EXPERT_AUDIT §5 names.
**Rejected.** Do not revisit without new evidence.

### 4j.4 What worked instead — a targeted spill, and the criterion is ELIGIBILITY

**4 of 7 PCR rims (R12, R14, R16, R17) have exactly ONE locked machine**, so for
those a spill is the only alternative to waiting — every other machine is a size
change (§4c). Those rims get the plant's own designated flex machine
(`machine_rim_lock.tier == "flex"` → TBMPCR2, which has historically run R17 as
13 % of its volume), budgeted to an equal share of that machine's month.

| | f_base | **f_solo** | plant |
|---|---|---|---|
| **PCR fulfilment** | 95.66 % | **97.13 %** | 100 % |
| PCR starved | 13,743 | **7,919** | — |
| **PCR same-size** | 96.5 % | **92.7 %** | 91.5 % |
| PCR weighted setup h | 392 | 423 | 344 |
| PCR CO / machine-day | 2.74 | 2.80 | 2.66 |
| PCR sub-floor | 0.2 % | **0.0 %** | 12.7 % |
| PCR GT inv daily max | 4,611 | 4,636 | rail 4,800 ✅ |
| PCR R5 max | 59.8 h | 66.1 h | 72 h ✅ |
| TBR — every metric | — | **unchanged** | — |

**Costs, stated:** same-size −3.8 pt (still above plant), setup +31 h, R5 +6.3 h.

**The budget is NOT the lever — swept and it saturates:** 1×/3×/6×/12× give
95.75 / 95.71 / 95.71 / 95.71 %. Adding hours to ONE rim does nothing because no
rim is nominally full. What pays is WHICH rims may reach the flex machine.
*A sweep table was written into the code comment before it was run, and the run
falsified it. Measure first, then write the comment.*

### 4j.5 Per-machine cadence, measured alone: **0.00 pt PCR, −0.05 pt TBR**

EXPERT_AUDIT §5 ranked it +0.3–0.8 pt. **Falsified.** PCR is byte-identical
because ~44 of 48 PCR GTs bypass the `cap_h` loop via the partition
(`l7:part_of` branch). **Kept anyway** — it is the measurement basis for §4j.2,
and without it the spill is sized off a table that says R12 is 113 % when it is
89.5 %. A correctness fix worth zero fulfilment is still worth keeping when a
capacity decision is sized against it.

---

## 4j2. B16 STRANDED A WHOLE MACHINE -- feasibility checked on one side only

<!-- NOTE 2026-08-09: this section was written as a second "4k", colliding with the
     sub-floor-measurement section above. Renumbered to 4j2 rather than resequenced,
     so existing cross-references to §4k (which point at the sub-floor finding) stay
     correct. Cite this one as §4j2. -->


**Reference `runs/b16_05_coverage` / `b16_08_coverage`. TBR May 73.7 -> 90.4 %, August 75.2 -> 94.0 %. PCR untouched.**

`l2_capability.py` searched all C(9,6)=84 TT/TL partitions and scored them with
`uncovered()` -- "does every GT have a machine in its group?". It never asked the
mirror question, **"does every MACHINE have a GT in its group?"**. A machine whose
eligible GTs are all tagged the other way is dead for the whole horizon, because
B16 forbids spilling across the boundary.

| month | machine | group | eligible GTs | in-group | plant ran it |
|---|---|---|---|---|---|
| **May** | **TBMTBR7** | TL | 6 | **0** | **11,587 tyres, 89.6 % occ** |
| May | TBMTBR5 | TT | 27 | 3 | 10,425 (we: 1,293) |
| **Aug** | **TBMTBR8** | TL | 17 | **0** | n/a (forward) |
| Jun / Jul | -- | -- | -- | none stranded | -- |

**July is the only month of four with no strand, which is why it survived every
tuning pass.** One of nine machines dead is 11 % of TBR building capacity, and it
was never reported -- a machine producing zero tyres was invisible for the entire
life of the project. That silence was the defect underneath the defect.

Fix: `stranded()` beside `uncovered()`, added to the search key AFTER the GT-side
terms (an uncovered GT cannot be built at all; a stranded machine only wastes
capacity -- never trade the first for the second). `PLANNER_B16_CRITERION` =
`gt` (old) / `machine` (binary) / `coverage` (default, adds a fair-share deficit
so a machine like TBMTBR5 with 3 in-group GTs of 27 still costs the partition).
Per-machine reach is printed every run and persisted to
`b16_machine_reach_<month>.parquet`.

| | May TBR | Jun TBR | Jul TBR | Aug TBR |
|---|---|---|---|---|
| before | 73.7 % | 92.3 % | **97.2 %** | 75.2 % |
| **after** | **90.4 %** | 92.3 % | **96.0 %** | **94.0 %** |
| delta | **+16.7** | **0.0 (control)** | **-1.2** | **+18.8** |

**Costs, stated:** July TBR loses 1.2 pt -- with dead=0 on both sides the new key
changes which combo wins on make-coherence, and July's old split was better. May
TBR same-size 92.2 -> 88.6 %, sub-floor 29.1 -> 35.3 %, CO/machine-day 3.14 -> 3.50,
inventory 762 -> 926, R5 62.0 -> 69.0 h. August same-size 96.6 -> 95.2 %. Net over
four months is **+6.4 pt**; PCR is unmoved on every month.

**Recovery is 62 %, not 100 %.** May TBR total 68,397 -> 84,133 against the plant's
93,674: TBMTBR7 0 -> 10,263 and TBMTBR5 1,293 -> 9,800, but TBMTBR4 8,070 -> 6,454
and TBMTBR9 9,278 -> 6,932 gave volume back. Freeing a machine rebalances the
split; it does not purely add.

## 4l. THE L5 TAKT CAP, THE ATOMIC SPLIT, AND PER-PRESS MOULD CHANGE

Reference run **`runs/s4_jul` / `runs/s4_aug`** — PCR 95.40 / 91.80, TBR 96.59 /
98.55. Four fixes were proposed; **three shipped, one is gated off**.

| step | Jul PCR | Jul TBR | Aug PCR | Aug TBR |
|---|---|---|---|---|
| base | 94.53 | 94.45 | 90.14 | 92.69 |
| +1 atomic split (PCR) | **95.62** | 94.45 | **91.18** | 92.69 |
| +2 L5 takt cap (TBR) | 95.62 | **96.59** | 91.18 | **98.55** |
| +3 load tie-break | 95.21 | 96.35 | 91.92 | 99.28 |
| **+4 per-press mould change (shipped, 3 skips)** | **95.40** | **96.59** | **91.80** | **98.55** |

### 4l.1 L5 placed as-early-as-possible and had no view of the month

L5 sorts jobs `(plant, -qty, gt, seq)` and takes the eligible press that frees
earliest. Measured on its own output, hours clipped into the day actually spent:
**TBR August press occupancy 98.2 % on days 1-20 and 34.7 % on days 21-31**, 2.5 %
on day 31, while 5,810 tyres starved inside the busy stretch. The work content is
44,456 press-h against 58,776 available — the month only needs 75.6 % — and the
greedy spent all of it in the first two thirds.

**Delay cannot fix it, and that is the whole design constraint.** A TBR campaign
is 248 h at p50, so there is no room to "spread starts". The lever is
CONCURRENCY: 44,456 / 744 = 59.8, so August wants ~60 presses seated all month,
not 79 seated for 21 days.

Shipped as a budget on concurrently-seated presses per partition,
`N_k = clip(ceil(ALPHA·W_k/U), 1, |presses_k|)`, partitions = plant-ALL + the TBR
TT/TL dedication + the PCR rim lock. Result: **Aug d21-31 34.7 → 70.3 %,
CV 0.472 → 0.157; Jul d21-31 81.6 → 88.4 %, CV 0.211 → 0.104.**

**ALPHA = 1.0 is an interior maximum on both months**, not a tuned constant —
Jul 95.76 / **96.59** / 94.73 and Aug 98.00 / **98.55** / 97.23 at α = 0.95 / 1.00
/ 1.10. It is the takt rate itself.

**The horizon guard is structural.** The governor is consulted only when the
ungoverned placement already fits in the month, and may only relocate to another
window that also fits. An earlier prototype without this guard pushed campaigns
past month end, which under the closed-box rule is lost volume, not carry-out —
it cost PCR 389,294 → 381,678 placed. Measured on the shipped version: **0 rows
past the horizon, 0 carry-out rows, 0 gt_events outside the month.**

**TBR ONLY.** On PCR the same governor measured **−0.28 pt July / +0.18 pt
August** — mixed sign, rejected. PCR has 3.4 %/4.4 % press slack; nothing to level.

### 4l.1a RE-MEASURED 2026-08-20: the takt governor on PCR is the month-end fix

**§4l.1 rejected PCR on `-0.28 pt Jul / +0.18 pt August` — a MIXED-SIGN
fulfilment reading on a baseline that no longer exists.** Re-measured on the
current shipped baseline (`runs/FINAL_aug`, reproduced as `TF_base`), grading
BUILT rather than in-month, it is the largest scheduler gain measured on this
engine.

**The defect it fixes.** August PCR runs its presses at 100 % occupancy on days
2-10 and 39 % on day 31; building follows to 43 %. 12,686 tyres starve as
`no feasible release` while 513 idle building-machine hours sit in d27-31. Those
idle hours are **not reachable by the starved volume** — R5 bounds a build to
`[t_cure - 72 h, t_cure - tau_min]`, so the legal band is ~65-70 h wide however
long the month is (`l7_pull_release.py`, "THE LEGAL BAND IS NOT [t0, ideal]").
Month-wide idle hours say nothing about whether a run 20 days earlier can be
placed. **The only way to reach tail machine hours is to move cure seats into
the tail**, which is exactly what the concurrency budget does.

**What actually binds on PCR is the RIM sub-partition, not `ALL`:** budget ALL
81 of 86 presses, but R13 24 of 51, R14 9 of 43, R17 10 of 46. The PCR front jam
is a rim-concurrency jam — one rim claiming 51 presses in week one when the
machines allowed on that rim can never feed them.

Fresh arms via `scripts/run_arm.py`, gated by `scripts/check_arm_fresh.py`,
2026-08, `PLANNER_L7_CLOSING_BUFFER=1`. **TBR is byte-identical in every row.**

| arm | PCR BUILT | dBUILT | in-month | ful % | starved | wCO | same | R5 | L11 |
|---|---|---|---|---|---|---|---|---|---|
| `TF_base` | 409,967 | +0 | 401,222 | 94.03 | 12,686 | 86.3 FAIL | 53.7 % | 61.4 h | 31/48 |
| `TAKT_PLANTS=PCR,TBR` (`TF_taktboth`) | 414,301 | **+4,334** | 399,511 | 93.63 | 5,129 | **73.1 PASS** | 65.4 % | 70.3 h | **32/48** |
| + `TAKT_PART_PLANTS=TBR` (`TF_ppart`) | 414,952 | **+4,985** | 402,366 | **94.30** | 7,179 | 78.0 FAIL | 60.4 % | 61.9 h | 31/48 |

PCR build d27-31 as % of interior median: **89/79/57/46/45 -> 101/98/99/97/80**
(`taktboth`) or **100/93/95/81/74** (`ppart`). Cure 97/87/72/49/27 ->
99/96/100/94/82 or 94/101/94/88/68. The month-end collapse is gone either way.

**THE TAIL FILLS PARTLY BY BORROWING FROM THE INTERIOR — say it in the
headline.** BUILT by window on `taktboth`: d1-2 -416 - interior d3-26 **-15,732**
- tail d27-31 **+18,998**. Only **23 %** of the tyres that appear in the tail are
new output; the rest is interior work relocated. On `ppart`: -353 / -10,373 /
+15,051, i.e. **33 %** new. Total build machine-hours used moves 6,772 -> 6,820
of 8,184 — the gain is 48 machine-hours wide, not 19,000. This is the mirror
image of §4ad and memory `day1-gain-is-borrowed`, and it is why the arm must be
graded on BUILT and on the window split together.

**In-month falls while BUILT rises, on `taktboth`:** in-month cure
401,222 -> 399,511 (-0.40 pt) because 6,751 more tyres cure in September; total
real output (in-month + tail) 410,775 -> 415,815, **+5,040**. `ppart` moves both
the same way (+4,985 BUILT and +0.27 pt in-month), which is why it is the
recommended variant.

**The costs, in the same sentence as the gain.** `taktboth` buys
+4,334 BUILT and the weighted-changeover invariant (86.3 -> 73.1 against the
74.0 cap, the only L11 flip) at the price of R5 max 61.4 -> **70.3 h** against
the hard 72 (1.7 h of margin) and carry-out debt 14.3 h/8,586 tyres -> 27.1 h/
16,285. `ppart` buys +4,985 BUILT and +0.27 pt in-month while leaving R5 at
61.9 h and wCO still failing at 78.0. GT inventory stays inside the rail on both
(PCR daily-mean max 4,565 / 4,539 vs 4,800); sub-floor 0.0 % on both.

**ALPHA IS NOT TUNABLE HERE — the response is not even monotone.** With the PCR
sub-partition off: alpha 1.01 -> +5,169, 1.02 -> +2,284, 1.05 -> **-617**. A
0.04 change swings BUILT by 5,800 tyres. That is greedy-placement jitter and
taking the argmax of one month is the mined-constant defect class (§1). Ship
alpha = 1.0, which is the takt rate itself and TBR's two-month interior maximum,
or ship nothing. The full 10-point response table is in the code beside the flag.

**Robust across three baselines** (PCR dBUILT, TBR byte-identical throughout):

| baseline | `ppart` | `taktboth` |
|---|---|---|
| shipped | +4,985 | +4,334 |
| `PLANNER_L7_CLOSING_BUFFER=0` | +5,348 | +5,490 |
| `PLANNER_LOT_INTERVAL_H=8` | +7,745 | +5,820 |

The gain is not the closing buffer — it is LARGER without it, and the buffer's
own contribution FALLS in the winning arms (PCR +3,524 -> +3,161 / +2,368).

**THE INDEPENDENT VERIFIER (`scripts/verify_export.py`, reads only the exported
CSVs) FAILS ON ALL THREE ARMS — INCLUDING THE SHIPPED BASELINE.** Both hard
violation classes are pre-existing, so the governor introduces no new class, but
it does move the counts and this is the honest cost of a denser tail:

| arm | changeover not reserved | worst short | machine-days > 24 h | worst day |
|---|---|---|---|---|
| `TF_base` | 9 of 1,306 | 5.8 h | 1 of 594 | 26.80 h |
| `TF_ppart` | 13 of 1,278 | 7.6 h | 2 of 599 | 25.75 h |
| `TF_taktboth` | 12 of 1,250 | 8.7 h | 2 of 598 | 25.00 h |

The **worst** machine-day overrun improves (26.80 → 25.75 / 25.00 h) while the
**count** of offending days and transitions rises. Verdict on all three:
`plan is NOT physically executable (2 hard violations)`. A governor that packs
the tail leaves less slack for an unreserved changeover to hide in — the fix for
that is the changeover reservation itself, not the governor.

⚠ **NOT GATED ON JULY, AND JULY IS WHERE IT PREVIOUSLY LOST.** The partition on
disk is stamped 2026-08 and could not be rebuilt in this session, so no July arm
exists. §4l.1's `-0.28 pt July` verdict is unrefuted. **Do not ship to defaults
until a fresh July arm agrees on BUILT.** Rule P8 stays *Partial*.

### 4l.2 Split-before-starve never reached the budget it was given

`l7` split a run at the floor to rescue it, but the split terminated at
`len(grp) == 1`. A run that is a SINGLE slice has no boundary to cut on and went
straight to `no feasible release` — **27,203 PCR August tyres against a sub-floor
budget of 180 that had spent about 9. The budget was never binding; the geometry
was.** A slice is a delivery to a press, not a physical unit (§4f), so it may be
cut once. Worth **+1.09 pt July / +1.04 pt August on PCR**, sub-floor 6.9 %/8.6 %
against the plant's 12.7 % and the 16 % gate.

**PCR ONLY, by measurement.** The identical change on TBR is worth **−2.01 pt
July / −0.92 pt August**: TBR runs already sit at the floor (p50 86 against 70),
so halving an atomic slice makes two fragments that both fail — starved groups
158 → 298 while starved volume ROSE 4,482 → 6,457. This is DO-NOT 15 again.

### 4l.3 The per-press mould-change table was loaded and never used

`l5` read `press_mould_change.parquet` into `mch_press` (165 of 165 presses
match through the wcID bridge) and then reserved the plant MEDIAN at all three
placement sites. PCR presses span **210-430 min** around a 360 median, so every
press slower than the median had its next campaign started before the mould was
out: **28 August events under-reserved by up to 70 min and physically over-ran a
still-curing press.** TBR is a single 361 for every press, so this is a PCR fix.

A correctness fix, kept regardless of sign — but it also pays: **+0.62 pt August
PCR** and same-size 69.9 → 71.4 %; **−0.22 pt July PCR** but same-size restored
80.8 → 82.3 %, R5 max 71.0 → 65.7 h and inventory mean 3,802 → 3,531.

### 4l.4 REJECTED (flag kept, default off): the load-aware tie-break

`PIN_RUNS` defaults to 1 and `break`s on the first feasible machine, so `l7`'s
"prefer the latest feasible release" preference is dead code and candidate ORDER
is the only lever. Adding committed-hours as a third sort key, after lock and
same-rim:

| | PCR | TBR |
|---|---|---|
| July | −0.41 | −0.24 |
| August | **+0.74** | **+0.73** |

It was first measured on **August alone** (+0.19 / +0.75) and reproduces there.
July inverts it on both plants. The average is positive and the idea is sound,
but this is exactly DO-NOT 14, so it ships **off** as `PLANNER_LOAD_TIEBREAK=0`.

**One reason to revisit:** it buys R5 margin exactly where the takt cap is
tightest — July TBR R5 max **71.5 → 66.3 h** against a hard 72, and TBR inventory
daily max 1,314 → 1,220 against the 1,400 rail.

### 4l.5 The two numbers to watch on this stack

- **R5.** July TBR sits at **71.5 h against a hard 72 — 0.5 h of margin**, up from
  2.8 h at baseline. The takt cap causes it (69.2 → 71.5); fixes 1 and 4 do not
  add to it. Nothing breaches; §4l.4 is the lever if it ever does.
- **The rail.** July TBR daily max **1,314 / 1,400 = 6.1 % headroom**, down from
  20.7 %. August is the same 1,314. PCR 4,560 and 4,556 against 4,800.

Hard gates on both final arms: machine / press / mould double-booking 0,
setup reserved 0 of 1,912 and 1,718 transitions, R5 > 72 h 0, built-after-cure 0,
conservation 0 breaches, rows past horizon 0.

---

## 4m. STRICT B12 — ZERO RUNS BELOW THE FLOOR (plant instruction, 2026-08-09)

> *"I strictly want that NO lots below this min lot cap."* — plant, stated twice.

Shipped ON as `PLANNER_STRICT_LOT_FLOOR=1`. Reference `runs/st_jul` /
`runs/st_aug`. **Verified 0 runs below the floor** on both plants, both months,
re-derived from `build_schedule.parquet` (min run = 150 exactly on PCR,
78 and 82 on TBR against a floor of 70).

### 4m.1 The sub-floor runs were NOT small lots — they were the grouping remainder

The obvious diagnosis was wrong and cost nothing only because it was measured
first. `PLANNER_HARD_FLOOR=1` reached 3.6 %/4.5 % on TBR and stopped, and the
reason was assumed to be L4.5/L5 emitting under-floor lots. Decomposing every
sub-floor run in `runs/s4_*` by cause:

| | sub-floor runs | GT already above floor on the SAME machine | GT genuinely short |
|---|---|---|---|
| Jul PCR | 80 | **75** | 5 |
| Jul TBR | 334 | **331** | 3 |
| Aug PCR | 111 | **111** | 0 |
| Aug TBR | 138 | **138** | 0 |

**99 % of them are the grouping remainder.** `l7` phase 2a cuts a GT's slice
stream into runs at `acc >= target`; whatever is left after the last cut becomes
a group of *any size at all*, and `span_cap` (R5) can force an early cut too.
`_place` then never compared `gq` to the floor — the floor was only ever checked
on the SPLIT path. So a small group was placed with no gate whatsoever. That is
why gating splits harder could never reach zero.

### 4m.2 Three gates, because one is not enough

1. **Grouping repair** — fold a sub-floor group into its predecessor, right to
   left, whenever the merged span still respects `span_cap`. This does the real
   work; most volume is recovered here, not refused.
2. **`_place` refusal** — never place a run below the floor, on any machine.
3. **`HARD_FLOOR` forced on** — never split into one.

`ATOMIC_SPLIT` is **force-disabled** under STRICT: it works *by* creating
sub-floor runs (§4l.2 took PCR from 0.1 % to 7.9 %). The two are mutually
exclusive and the code resolves it in one place, not at every use site.

Where R5 and B12 genuinely conflict — a merge that would breach `span_cap` —
**R5 still wins and the volume becomes shortfall with its own reason**,
`below min_lot (strict B12)`, never a sub-floor run. That residue is small
(175 tyres July, 58 August, TBR only); the bulk of the loss is refused earlier at
`would breach min_lot`.

### 4m.3 The price

| | Jul PCR | Jul TBR | Aug PCR | Aug TBR |
|---|---|---|---|---|
| fulfilment permissive → strict | 95.40 → **93.84** | 96.59 → **87.12** | 91.80 → **89.84** | 98.55 → **92.02** |
| points | **−1.56** | **−9.47** | **−1.96** | **−6.53** |
| tyres | **−6,162** | **−9,278** | **−7,986** | **−5,190** |
| sub-floor share | 7.9 → **0.0 %** | 31.6 → **0.0 %** | 9.6 → **0.0 %** | 18.7 → **0.0 %** |

**It is not all cost.** Strict B12 is the best changeover result this project has
produced: TBR CO/machine-day 3.76 → 2.70 (Jul) and 2.62 → 2.10 (Aug) against a
plant benchmark of 3.56; weighted setup TBR 175 → 125 h and 128 → 102 h; PCR
504 → 457 h and 620 → 570 h; same-size PCR 82.3 → 84.1 % and 71.4 → 72.6 %.
**L11 improves on both months** (23 → 24 and 21 → 22). PCR lot p50 goes
179 → 303 on August. Fewer, larger, cleaner runs — bought with volume.

**This is stricter than the plant, deliberately and on instruction.** The plant
itself runs sub-floor 12.7 % (PCR) / 30.8 % (TBR) (§4k). Set
`PLANNER_STRICT_LOT_FLOOR=0` to restore the plant-calibrated budget, which is
worth 1.6–9.5 points of fulfilment back at the cost of the floor.

---

## 4n. THE FLOOR NEVER COST THE VOLUME — THE PACKING DID (2026-08-09)

> The trade in §4m.3 is **not fundamental**. Reference arms `runs/FIX_jul` /
> `runs/FIX_aug`. Sub-floor **0.0 %** on both plants, both months, re-derived
> independently from `build_schedule.parquet` two ways (setup blocks split at
> >1 h gaps, and emitted `run_id` groups). Min run 150 exactly on PCR, 78/82 on
> TBR against a floor of 70.

| fulfilment | Jul PCR | Jul TBR | Aug PCR | Aug TBR |
|---|---|---|---|---|
| strict, §4m as shipped | 93.84 | 87.12 | 89.84 | 92.02 |
| **strict + §4n** | **96.37** | **95.02** | **94.89** | **96.82** |
| gain | **+2.53** | **+7.90** | **+5.05** | **+4.80** |
| permissive, SAME engine (`PERM_*`) | 96.96 | 97.36 | 94.86 | 98.66 |
| **remaining price of the hard floor** | **−0.59** | **−2.34** | **+0.03** | **−1.84** |

The floor cost 1.56–9.47 pt in §4m.3. It now costs 0–2.34 pt, and on Aug PCR it
costs nothing at all. Three of four arms clear 95 %; Aug PCR misses by 0.11 pt
and the blocker is named in §4n.4.

### 4n.1 The tell: strict FREED machine time and produced less

§4m.3 reported the loss as if the floor consumed capacity. It did the opposite.
Jul TBR, permissive → strict: weighted setup **175 → 125 h**, changeovers per
machine-day **3.76 → 2.70** — and occupancy **79.6 → 71.7 %** with idle hours
**976 → 1,294**. An arm with 318 more free machine-hours built 9,278 fewer
tyres. Capacity was never the constraint, so capacity was never the explanation.

### 4n.2 Root cause — the idle time was in the wrong SHAPE

Every run is released as late as its slices allow, so it leaves a hole behind it
between the previous run's end and its own start. Those holes are sized by
**deadline spacing**; nothing makes them as long as a run.

Measured on `runs/st_jul` (strict, pre-fix):

| | idle h | holes | hole p50 | floor-minimal run |
|---|---|---|---|---|
| PCR | 1,473 | 837 | **1.00 h** | 2.84 h |
| TBR | 1,294 | 711 | **1.30 h** | 5.05 h |

A run that may not be cut below the floor needs **one contiguous hole**. It
cascades back through every sliver, hits `t0`, and is refused — beside 1,294
idle hours on the same machines. **That is the entire mechanism by which the
floor cost volume**: the permissive arm split the run into halves that fit the
slivers, and those halves are exactly what made it sub-floor.

**Three competing explanations were tested; all three are FALSE:**

| hypothesis | test | result |
|---|---|---|
| the R5 band is the binding resource | shelf life 72 → 144 h | **+0.47 pt TBR, −0.14 pt PCR** |
| the WIP rail is the binding resource | both rails → 99,999 | **+0.15 pt PCR, 0.00 pt TBR** |
| the horizon boundary is the cause | 72 h pre-horizon warm start | **+0.05 pt PCR, +0.14 pt TBR** |

Nor is it capacity or eligibility: PCR needs 220 machine-hours of 2,150 free,
TBR 446 of 1,714, and the refused GTs are locked to machines running at
**58–87 %**, not saturated ones.

**A DP that re-cuts the slice stream into the best floor-feasible partition —
the obvious fix, and the one proposed — cannot work, and the data says why.**
Every refused PCR group already sits between 150 and 300 tyres (p50 156, 0 of
123 above 2x floor) and every refused TBR group is 3 slices (p50 87). They are
already the smallest legal runs; a partition cannot make a minimum smaller. The
defect was never in the cut, it was downstream of it, in the calendar. Adding
the DP would have measured as a no-op and cost a week.

### 4n.3 Two fixes, both inside `l7`

**1. Anti-sliver packing — `PLANNER_SLIVER_PCR` / `_TBR`, default `1.0`.**
If releasing at the just-in-time start would leave a hole shorter than a
floor-sized run, abut the previous run instead. The idle time then accumulates
*after* us, contiguous, where a later run can use it. The abutted start is tried
FIRST and the just-in-time start SECOND, so closing a hole can never cost a
placement. Worth **+1.59 / +3.29 / +2.98 / +2.13 pt**. Hole p50 collapses
1.00 → 0.47 h (PCR) and 1.30 → 0.17 h (TBR).

`1.0` is the principled value — "never leave a hole no legal run could occupy" —
and it is also the measured interior maximum: positive on all four arms, where
`2.0` is mixed-sign alone and worse everywhere once make-room is on (Jul TBR
95.02 → 91.86). `1.5`, `3.0`, `6.0` all worse. **Do not tune it.**

**2. `_make_room` — targeted LNS, `PLANNER_L7_MAKEROOM`, default `1`.**
Anti-sliver packing stops new holes being made; it cannot repair a machine whose
holes were already set by the ORDER things were placed in. After fix 1, 50 % of
the remaining TBR refusal still had no hole >= its own duration anywhere in its
R5 band. So destroy-and-repair on the smallest neighbourhood that can work — one
machine: pull the runs that block the latest legal slot EARLIER, each only as far
as its own R5 floor, `t0` and its predecessor allow, then insert. Every
constraint is re-checked, the rail on the whole bundle; anything that fails rolls
the machine back exactly as it was. **Nothing here relaxes a rule.** Worth a
further **+0.94 / +4.61 / +2.07 / +2.67 pt**; 113/75 (Jul) and 148/37 (Aug) runs
rescued.

**The first version compacted the WHOLE prefix and failed: 699 rail refusals
against 6 successes.** Moving every earlier run to the front of the month buys
the 2.16 h median shortfall at the price of days of standing GT, and both plants
already sit ON their rail. Shifting only the blocking runs, only by the hours
needed, took it to 188 rescues. Same lesson for insertion points:
`PLANNER_L7_MR_POINTS` > 1 leaves July identical and costs August PCR 148 → 129
rescues, because every earlier slot puts the same stock on the floor sooner.
**`MR_POINTS = 1`.**

### 4n.4 The price, and the honest residual

Jul PCR weighted setup **457 → 421 h**, CO/machine-day **2.74 → 2.47**; Aug PCR
**570 → 556 h** and **3.11 → 2.93**; R5 max **70.6 → 66.2** (Jul PCR) and
**71.8 → 58.6** (Jul TBR), so the thin R5 margin flagged in §4m is gone. Rails
held: PCR daily max 4,622 / 4,574 against 4,800; TBR 1,330 / 1,344 against
1,400. `verify_export.py` **0 HARD / 0 SOFT / 0 EXPORT** on both months; pytest
green.

Two real costs, both stated:
* **PCR same-size share** 84.1 → 82.4 % (Jul) and 72.6 → 67.6 % (Aug). Abutting
  and make-room both choose a slot on time, not on rim; Aug PCR's same-size
  invariant flips PASS → FAIL. Jul TBR CO/machine-day 2.70 → 2.92, still well
  under the plant's 3.56.
* **GT inventory rises** — PCR time-weighted mean 3,625 → 4,035 (Jul), TBR
  1,030 → 1,228. Both move TOWARD the G8 band and TBR's mean-inventory invariant
  flips FAIL → PASS. This spends rail headroom the project was leaving unused.

**Aug PCR stops at 94.89 %, 0.11 pt short, and the blocker is the month
boundary.** 56 % of its residual (4,483 of 7,974 tyres) is cold start — a cure
early on day 1-2 whose GT must have been built in the previous month; days 1-2
alone are 72 % of it. A 72 h pre-horizon warm start closes it exactly
(94.89 → 95.00, `runs/mrpre_08`), but that is a DIAGNOSTIC: it emits build rows
before the month and is not a shippable plan. **The shippable fix is the rolling
horizon / carry-in already named as an unbuilt lever.** Do not chase this
0.11 pt with another packing knob; it is not a packing problem.

### 4n.5 Measured and REJECTED against this baseline

Re-measured because the baseline moved (fulfilment points):

| lever | Jul PCR | Jul TBR | Aug PCR | Aug TBR | verdict |
|---|---|---|---|---|---|
| `HARD_PIN=0` | −0.43 | −0.14 | −0.65 | +0.17 | reject |
| `LOAD_TIEBREAK=1` | +0.42 | −0.46 | −0.46 | +0.33 | reject, mixed sign |
| `RAIL_MARGIN=0.97` | +0.50 | +0.59 | −0.04 | +0.72 | defer — mixed on the arm that needs it |
| `L5_TAKT_PLANTS=PCR,TBR` | −2.79 | 0.00 | −2.17 | 0.00 | reject |
| `SLIVER=1.5` | −0.16 | 0.00 | −0.59 | 0.00 | reject |
| `SLIVER=2.0` | −0.20 | −3.16 | −0.76 | −0.45 | reject |
| `MR_POINTS=6` | 0.00 | 0.00 | −0.60 | 0.00 | reject |

---

## 4o. THE STALENESS WARNING WAS NOT A GATE (2026-08-09)

`l7_pull_release.py` carried a staleness guard whose own comment read *"Refuse
instead"* — and then printed a warning and fell back to the dynamic assignment.
Every July arm in this ledger ran on **August's partition**, printed the line, and
nobody read it.

Measured, identical config, July PCR:

| | stale (fallback) | partition rebuilt | delta |
|---|---:|---:|---:|
| fulfilment | 96.37 % | **96.95 %** | **+0.58 pt** |
| same-size share | 81.4 % | **91.7 %** | **+10.3 pt** (plant 91.5 %) |
| weighted CO min/machine-day | 71.9 | **63.6** | −8.3 (≈46 h setup) |
| rim purity | 92.2 % | 93.2 % | +1.0 |

It cost 0.58 pt and 10.3 pt of same-size **in the run this project was quoting as
its reference**, and it read as an engine limitation rather than a stale input.

**Fixed:** the guard now raises `SystemExit` and refuses to plan. The message names
the rebuild command. `PLANNER_ALLOW_STALE_PARTITION=1` restores the fall-back for a
deliberate A/B and announces itself on stdout.

Verified: July + August partition → refuses, exit 1. August + August partition →
plans normally. Override → warns loudly, runs.

**DO-NOT: a warning that the run continues past is not a guard.** If a wrong input
produces a wrong answer that looks like a right one, refuse. Three of this project's
worst defects (this, the stale-L11 arms, the flat-cadence basis) were all *detected*
by code that then carried on regardless. Note the same warn-and-continue guard still
exists in the `_diag_l7*.py` copies; they are diagnostics, so it is left there.

---

## 4p. "EVERYTHING IS AVAILABLE AT t0" — the ruling was ALREADY SATISFIED (2026-08-09)

**Plant ruling:** *"Assume everything is available for building from the very
start — we don't have to wait for anything."* Recorded as **B-ASSUME-1** in
`BUSINESS_RULES.md` §6b. Audit flag `PLANNER_FULL_AVAILABILITY_T0`, **default 0**.

### 4p.1 Two measurement defects invalidated the premise first

The brief for this work rested on three numbers. All three were wrong, and all
three are the **denominator class** (§4d, DO-NOT #9).

| claim | reality |
|---|---|
| "only 4,820 of the 6,960 PCR tyres in `opening_gt_inventory.parquet` are available at t0" | **Two different files.** 6,960 / 2,330 is `warehouse/derived/opening_gt_inventory.parquet`, a **December-31 snapshot** (`max(latest_built)` = 2025-12-31 23:59:58). July's own master holds **4,820 / 1,297**. Nothing is withheld between them |
| §4i: "opening stock 40 % / 54 % unused" | Same mismatch — July usage over a December denominator. True: **630 PCR (13 %) / 287 TBR (22 %)**, i.e. **87 % / 78 % utilised**. §4i corrected in place |
| "opening stock EXISTS for most of what days 1–2 need (Jul PCR 2,739 of 3,869)" | That measures whether the GT **holds** stock, not whether the stock is **spare**. Joined per GT against day-1 unfed: **spare = 0 on every affected GT, both plants, both months.** Max closable by perfect stock allocation: **0** |

### 4p.2 Every clause of the ruling was already true

Checked in code, not assumed. Materials/components/compounds are **never
referenced** in `l6_build_gate.py` or `l7_pull_release.py` — they are exploded
**downstream** in `l8_prep_explosion`, so material has never been a building
constraint in this engine. `busy = {}` in l7 (machines free at hour 0),
`free.get(pr, t0)` in l5 (presses free at hour 0), mould concurrency bounded by
**count** not by time. Build starts at **t0 + 0.00 h**. There is no ramp to remove.

### 4p.3 Two faithful implementations, both measured, both defaulted OFF

Sub-flags `PLANNER_FULL_AVAIL_RAMP` (L5) and `PLANNER_FULL_AVAIL_LADDER` (L7),
measured **one at a time** — the bundled arm was run first and was uninterpretable.
All arms fresh via `run_arm.py`; `jul_off` reproduces the pre-change baseline
**metric-for-metric with 0 invariant flips**, so the gating is provably clean.

**RAMP** — `earliest_cure`'s stock exemption is a BINARY test (`stock >= gap_tyres`),
so a GT holding **73 of the 76** tyres needed to bridge the gap got ZERO credit and
waited the full 11.86 h. Partial credit makes it continuous:
`wait = (gap_tyres − stock)/rate`. 6 PCR + 3 TBR GTs sit in that band on July.

**LADDER** — `opening_life` is the **MEDIAN** age of a GT's stock used as a hard
wall that refuses the whole group (`hold_h > opening_life → have = 0.0`). The
master is one row per tyre, so an exact per-tyre FEFO ladder replaces the proxy.
Textbook §1 defect class.

| | Jul PCR | Jul TBR | Aug PCR | Aug TBR |
|---|---|---|---|---|
| **off** (shipped) | **96.95** | **95.56** | **94.89** | **97.19** |
| ramp | 96.83 **−0.12** | 95.14 **−0.42** | 95.12 **+0.23** | 97.91 **+0.72** |
| ladder | 96.86 −0.09 | 95.56 0.00 | 94.76 −0.13 | 97.19 0.00 |
| both | 96.58 | 95.14 | 94.81 | 97.91 |

**LADDER — REJECTED, and the reason is decisive.** Opening-stock consumption is
**identical to the tyre in every arm**: PCR 4,190 / TBR 1,010 (July), PCR 4,523 /
TBR 1,018 (August), ladder on or off. **The median screen was never binding.**
Stock is exhausted by early cures long before the wall can matter, so there is no
withheld stock for an exact screen to release. It only changes *which* tyre feeds
*which* slice, which reshuffles run grouping: −0.09/−0.13 pt PCR, R5 max
65.1 → 68.3 h, day-1/2 unfed 3,869 → 4,399. Kept behind the flag only because the
median-as-constraint *shape* is still wrong and a future baseline may make it pay.

**RAMP — MIXED SIGN, defaulted OFF.** It reduces day-1/2 unfed in all four
plant-months (Jul PCR 3,869→3,595, Jul TBR 1,770→1,697, Aug PCR 3,994→3,854,
Aug TBR 1,277→864) but **total** unfed only falls on August; on July it rises
(PCR 7,582→8,332, TBR 2,794→3,253) and flips **TBR mean GT inventory below the
G8 band**. Mixed sign is the same criterion that rejected `LOAD_TIEBREAK` (§4l.4).
The mechanism is visible in the bail counters: **`cold` goes UP** (Jul 161→177,
Aug 163→167). Pulling a cure seat earlier pulls its BUILD deadline earlier too,
past t0 — so it **moves** starvation off day 1 into the rest of the month rather
than removing it. Exactly what §4i measured for `FLOOR_BASIS` min/slice.
Worth a wider sweep (more months) before shipping; **do not ship on 4 points.**

### 4p.4 What actually binds — measured, not asserted

`runs/jul_diag` (`PLANNER_L7_DIAG=1`), refused groups decomposed by whether their
ideal start precedes t0:

| | unfed | **cold** (needs to start before t0) | contention (slack ≥ 0) |
|---|---|---|---|
| Jul PCR | 7,582 | **3,464 (46 %)** | 4,118 (54 %) |
| Jul TBR | 2,794 | **1,443 (52 %)** | 1,351 (48 %) |

**The cold deficit is only 2.3 h (PCR p50) / 3.3 h (TBR p50) before t0, max 4.2 h
on PCR.** That is not an availability gap — every input is already staged. It is
a **carry-in / rolling-horizon** gap of a few hours: the plant was building at
06:00 on the 1st; we start at 07:00 with empty machines.

Closing it requires **building before t0**, which the closed-box horizon rule
forbids and `verify_export.py` fails as a **HARD** violation (every row must sit
inside the plant month). **That is a conflict between two plant rulings — full
availability at t0 vs the closed-box month — and it is for the plant to resolve,
not the planner.** Note the previously-measured 72 h pre-horizon warm start
(+0.05/+0.14 pt) was `PLANNER_DIAG_PRE_H`, which is diagnostic-only and does not
emit runnable plans; a **bounded ~4 h** pre-horizon window matched to the measured
deficit has never been tried and is not the same experiment.

### 4p.5 Measured and NOT shipped: widening the opening-stock scrap bound

`scripts/make_opening_gt.py:80` bounds opening stock at the **build→cure p99 lag
(55.92 h)**, which is *tighter* than R5's 72 h — `planner/plan/ledger.py:145`
already clamps to `min(p99, 72)`, so the two disagree. Widening it to R5 admits
**165 PCR / 61 TBR** more tyres (July; 188 / 26 August), of which **119 / 49**
land on GTs that have day-1 shortfall — worth **≤ 0.05 pt**. Not shipped: the
p99 bound exists to keep never-cured **scrap** out of inventory, so widening it
trades a data-quality guarantee for ~0.04 pt, and the ruling is about *waiting*,
not about reclassifying scrap as good stock. It also forces a full L1→L4.5 +
partition + baseline rebuild, moving the shared denominator.

---

## 4q. THE INCH LOCK AS A PRIORITY — the literal request is INFEASIBLE, and the reason is physical (2026-08-09)

> **Plant request, verbatim:** *"Remove the hard lock, make it priority-wise. If
> the dominant inch is complete we can switch to another inch. E.g. machine 9 has
> priority for 12 inch, but we have less 12-inch demand this month — so after
> completing 12 inch we switch to 13 inch and make only 13 like that."*

Arms `RP_*_jul` / `RP_*_aug`, all fresh via `run_arm.py`. `RP_base_jul`
reproduces `runs/jul_prod_v1` **metric-for-metric** (PCR 96.95 / TBR 95.56), so
the gating is provably clean.

### 4q.1 Three facts that kill the literal design, measured before building it

1. **No rim is short.** July PCR demand by rim runs 32,597 (R16) to 125,856
   (R13); August is the same shape. There is no month in this data where a
   machine "completes 12 inch" with time left over.
2. **Every rim runs every day.** Each PCR rim has an **active cure campaign on
   all 31 days**, with **7–28 presses** on that rim concurrently at p50. Cure
   demand spans day 0.0 → 31.0 for every rim on both plants, both months.
3. **R5 chains building to it.** A green tyre must cure within 72 h (wait p50
   5.8 h), so a machine feeding a rim must feed it *every day*.

**A building machine's rim sequence is a SHADOW of the cure schedule's, not an
independent choice.** Month-long sequential single-rim campaigns cannot be
scheduled on the building side alone; they would have to be created on the
presses first. This is a *curing*-side request wearing a building-side costume.

### 4q.2 The premise was also already satisfied

**9 of 11 PCR machines and 9 of 9 TBR machines already never switch rim.** All 66
July PCR switches sit on TBMPCR2 (54) and TBMPCR8 (12), and TBMPCR2's five rims
are the **targeted rim spill** (§4j.4) — the engine's existing adoption
mechanism, worth +1.47 pt. The request is not "add adoption"; it is **"make the
adoption we already do sequential"**.

### 4q.3 What the tie-break could NOT do — and why

`PLANNER_RIM_PRIORITY` was first built where §12 says rim coherence belongs: a
candidate-machine tie-break with an open-rim campaign state and a minimum hold.
**It measured BYTE-IDENTICAL to baseline on every KPI, both months.**

`HARD_PIN` breaks on the first feasible machine and the partition gives most
GTs one machine, so **the candidate list is usually length 1 and ordering it is
a no-op**. The number of rims a machine carries is decided when the SPILL is
assigned, not when a candidate list is sorted. **Gate the resource where it is
CREATED** (DO-NOT #27) applied again, one layer over.

### 4q.4 What DID work: cap the rims per machine (`RIM_MAX_CONCURRENT`)

A machine's own primary rim counts as one, so `2` admits exactly one adopted
rim; rims compete for slots by measured excess, largest first, and a rim that
misses out simply waits on its own machine.

| July PCR | ful % | same-size | rim switches | streak p50 | CO | weighted setup | idle h |
|---|---|---|---|---|---|---|---|
| base | **96.95** | 92.2 % | 66 | 1.0 | 845 | 376.3 h | 2,014 |
| K=3 | 96.65 **−0.30** | 94.6 % | 45 | 1.5 | 835 | 360.7 h | 2,036 |
| **K=2** | 96.02 **−0.93** | **95.9 %** | **34** | 2.0 | 833 | **353.7 h** | 2,101 |
| plant | 100 | 91.5 % | — | — | 742 | 344 h | — |

| August PCR | ful % | same-size | *same-size, known-rim pairs* | rim switches | setup |
|---|---|---|---|---|---|
| base | **94.89** | 65.2 % | *89.9 %* | 323 | 567.1 h |
| K=3 | 94.65 **−0.24** | 66.5 % | — | 313 | 561.8 h |
| **K=2** | 94.18 **−0.71** | **67.9 %** | ***93.7 %*** | **293** | **551.4 h** |

**TBR is untouched on every arm, both months** — it has no spill and 0 (July) /
20 (August) rim switches already. **0 L11 invariant flips** on either month.
`verify_export.py` 0 HARD / 0 SOFT / 0 EXPORT.

**Consistent sign on both months, so it is a real frontier point, not noise:
≈0.7–0.9 pt of PCR fulfilment buys 3.7/2.7 pt of same-size and 16–23 h of setup.**

### 4q.5 It halves the switches; it does NOT campaign them

| TBMPCR2, July | rims | switches | streak p50 | streak max |
|---|---|---|---|---|
| base | 5 | 54 | 3 | 16 |
| K=2 | 3 | 24 | 5 | 17 |

Halving the rims roughly **halved** the switches — proportional, not
campaigned. Two rims on one machine still alternate every 4–9 slices, because
both need feeding daily (§4q.1). The target shape of 1–2 switches per machine is
**not reachable from the building side**. Note TBMPCR8 already shows what a real
campaign looks like where demand allows it — `R15 x 112` consecutive slices —
so the engine finds the shape when the shape exists.

### 4q.6 REJECTED: `PLANNER_RIM_ADOPT` — concentration beats distribution

Route the spill by (cheapest size change, most free partition hours) instead of
to the plant's historical flex machine. The objection looked strong: TBMPCR2 is
the **dearest** PCR machine (60 min vs 42) and holds only 211 free hours where
TBMPCR11 holds 317 and TBMPCR5 310, both at 42 min.

| | Jul PCR | Aug PCR |
|---|---|---|
| fulfilment | 96.95 → **96.35** (−0.60) | 94.89 → **93.65** (−1.24) |
| same-size | 92.2 → **90.0 %** | 65.2 → 66.7 % |
| rim switches | 66 → **84** ❌ | 323 → 304 |
| weighted setup | 376.3 → 374.9 h (−1.4) | 567.1 → 546.7 h |

**Worse on fulfilment on both months and worse on switches on July.** Spreading
the spill over cheap machines gives TBMPCR11 four rims and contaminates a
machine that was pure; concentrating it on one machine keeps the other nine
clean. **The plant's designated flex machine is the right answer for a reason
the cost table does not show** — and this is the second time (after §4j.4) that
sizing on cost/load lost to the plant's revealed eligibility structure.

### 4q.7 A MEASUREMENT DEFECT FOUND HERE: August same-size is a master-data hole

**Every August same-size figure this project has quoted — 72.6 %, 67.6 %,
65.2 % — is contaminated.** 17 PCR GTs / 26,409 tyres / **6.9 % of August PCR
build slices have no row in `gt_size`**, so their rim is unknown; they cannot be
rim-locked, and both L11 and the scorecard charge an unknown-rim transition as a
**different-size** one.

| Aug PCR | changeovers | involving an unknown rim | same-size as reported | **same-size, known-rim pairs only** |
|---|---|---|---|---|
| base | 998 | **278 (27.9 %)** | 64.8 % | **89.9 %** (n=720) |
| K=2 | 994 | 278 (28.0 %) | 67.5 % | **93.7 %** (n=716) |

July is **0.0 %** uncovered, which is why July reads 92.2 % and August 65.2 % on
the same engine. So August's rim discipline is close to the plant's 91.5 %, and
K=2 **crosses it**. The fix is `gt_size` coverage, not a planner change.

**DO-NOT: never report a share whose denominator includes rows the metric is
undefined for.** Fourth instance of the denominator class after §1e, §4d and
§4p.1.

---

## 4r. SISTER-SKU GROUPING — the plant does it, it is worth real minutes, and our COST MODEL CANNOT SEE IT (2026-08-09)

> **Plant definition:** GTs that differ by only ONE component.

Signatures are mined from the **raw** workbooks by
`scripts/build_gt_sister_group.py` → `INPUT/derived/gt_sister_group.parquet`
(86 rows, deterministic, exact SKU keys only).

### 4r.1 PCR has NO sister structure — and that is a product fact, not a data fact

> ⚠ **CORRECTED 2026-08-09 by §4s.** The headline of this subsection is too
> strong and will mislead. Plant-supplied construction-cluster workbooks show
> PCR **does** carry construction structure and the plant **does** act on it
> (same-cluster adjacency 14.1 % against a 10.0 % within-machine permutation
> null, z = +7.5; realised gap **2.4 min same-cluster vs 13.9 min
> different-cluster same-rim**). What survives is the *operational* claim, and
> only in this precise form: **the PCR structure is redundant with the rim lock
> and too rare to schedule on.** 33 of 37 genuine PCR clusters are already
> single-machine single-rim, and in the July universe only **4 GTs / 6.9 % of
> demand** sit in a multi-GT cluster with a co-active partner. Read §4s before
> quoting anything below.


The prior verdict "PCR construction data is unusable" was **half wrong**. The
raw workbook has **8 component-code columns with 74–84 distinct values each**;
`planner/data/construction.py` keeps only one of them (see MEMORY §10q). The
data is fine.

**But the similarity is genuinely absent.** Over the 35 July-active PCR GTs with
signatures there are **595 pairs and ZERO at distance 1, 2 or 3**
(d=0: 2, d≥4: 593). 82 % of the workbook's signatures are globally unique. Every
PCR component code is size-specific, so all six slots move together: **the PCR
signature is a fingerprint, not a similarity metric.** Over all 8 months only
**0.31 %** of PCR pairs are d≤1, and that arm is **5 distinct GT pairs on 2
machines** — not plant behaviour.

Coverage caps at 72.9 % of GTs / 65.4 % of volume anyway, and 13 missing July
GTs — including the largest, `GT 1513 XPC1 MSIL` at 55,583 tyres — have **no row
in the workbook at all**. A refreshed workbook is needed, not better parsing.

⚠ **The 4-digit numeric-core join is UNSAFE and is banned in the script.** It
maps `GT 1513 XPC1 MSIL` to `GT1513 NEO` — a different tread. The digits encode
SIZE, not product.

### 4r.2 TBR: the plant demonstrably sequences by construction similarity

51 of 56 July GTs (91.1 %) / **95.8 % of volume**, 10 live slots, 22 groups at
d≤1, smooth distance gradient. Transitions from 8 months of `v_build`, runs split
at >4 h gaps, against a **within-machine permutation baseline** (300 reps, each
machine's own GT multiset held fixed):

| | observed | baseline mean | baseline 95 % | lift |
|---|---|---|---|---|
| d==0 | 38.44 % | 17.34 % | [16.47, 18.10] | **2.22x** |
| d≤1 | **60.18 %** | 32.31 % | [31.31, 33.13] | **1.86x** |
| d≤2 | 67.76 % | 40.27 % | [39.38, 41.21] | **1.68x** |

Far outside a baseline that already controls for each machine's restricted
repertoire. **Invariant to the gap cutoff** (>1 h gives 1.88x).

### 4r.3 And a distance-1 transition really is cheaper — a monotone dose-response

The prior "TBR same-family 6.5 vs 14.3 min, δ 0.81" was **not** a rim contrast:
**99.9 % of TBR transitions are already same-rim** (6 cross-rim in 8 months).
Controlling for **full tyre size** instead — width/aspect/rim, n=4,081:

| | n | median gap | mean | p25 | p75 |
|---|---|---|---|---|---|
| d==0 | 2,231 | **5.15 min** | 8.70 | 3.97 | 7.54 |
| d==1 | 1,138 | **8.29 min** | 12.28 | 6.21 | 10.63 |
| d≥2 | 712 | **12.10 min** | 16.88 | 9.83 | 15.62 |

Cliff's δ −0.768 (d≤1 vs d≥2, p≈0); −0.519 (d0 vs d1); −0.590 (d1 vs d≥2).
Holds inside individual size strata (295/80R22.5 δ −0.666, 10.00R20 δ −0.464;
315/80R22.5 n=64 **not significant**). **A binary size proxy cannot produce a
graded middle tier**, so this is construction, not size.

**What it does not prove:** the gap is wall-clock idle, not measured setup, and
it is observational — the plant may group sisters when it is already convenient.

### 4r.4 THE BINDING CONSTRAINT IS THE COST MASTER, NOT THE PLANNER

`cap_changeover.parquet` / `v_changeover_build` are keyed on
**(machine x same-size / different-size)** and nothing else. 20 rows, two numbers
each. **There is no GT-pair, component or sister dimension.**

**So the engine charges TBR 10 min for a sister transition and 10 min for a
non-sister same-rim one, where the plant's realised gaps are 5.2 and 12.1 min.
The cost model is flat exactly where the data shows a 2.3x spread.** Any gain
from sister sequencing is invisible in `weighted_setup_h` *by construction*.
Capturing it needs a **third tier in the master** (`same_size_sister_min`),
which is a schema change, not a planner weight — and the declared 10/24 and
28/60 must **not** be overwritten with the empirical gaps, which include idle
time and are an upper bound on setup.

### 4r.5 Two implementations, measured

**`PLANNER_SISTER_GROUP`** — candidate-machine tie-break below rim coherence.

| fulfilment Δ | Jul PCR | Jul TBR | Aug PCR | Aug TBR |
|---|---|---|---|---|
| | **+0.09** | **+0.14** | **0.00** | **+0.35** |

Positive or neutral on all four arms, no metric regresses, 0 L11 flips,
`verify_export` clean. **But it does not do what it says**: TBR sister adjacency
moves 43.0 → 42.4 % (July) and 52.9 → 54.7 % (August) — mixed and ≈0. Same
mechanism as §4q.3 — `HARD_PIN` leaves one candidate, so the key rarely fires.
The gain is reshuffling, not sisterhood.

**`PLANNER_SISTER_BUCKET_H=4`** — bounded queue reorder: round deadlines down to
a 4 h bucket and group sisters *within* a bucket. This is the bounded form of the
full similarity re-sort that cost 25,549 tyres at L5.

| | Jul PCR | Jul TBR | Aug PCR | Aug TBR |
|---|---|---|---|---|
| fulfilment Δ | −0.19 | **−0.70** | −0.23 | +0.08 |
| TBR sister adjacency | — | 43.0 → **49.3 %** | — | 52.9 → **57.2 %** |
| TBR weighted setup | — | 138.3 → 135.3 h | — | 104.3 → 102.4 h |

**It moves the mechanism — +6.3 / +4.3 pt of adjacency — and it costs
fulfilment, mixed sign.** `B=12` is worse on both (Jul TBR −1.56) *and* has
lower adjacency than `B=4`. **Rejected as a default under DO-NOT 14**, kept as
the priced option: adjacency is a property of the QUEUE, and the queue is the
deadline. The measured setup saving (−3 h / −1.9 h) is an **understatement**,
because §4r.4 means the model cannot charge the difference it is buying.

---

## 4s. CONSTRUCTION-CLUSTER WORKBOOKS — the machine map is proven, PCR structure is REAL but redundant, and both levers are MIXED-SIGN (2026-08-09)

Inputs: user-supplied `SKU_Construction_Clusters_PCR (1).xlsx` and
`..._TBR (1).xlsx` at the workspace root. Five sheets each; average-linkage
clustering on Hamming distance over the building component codes, cut at 0.3.
Ingested by **`scripts/build_sku_con_cluster.py`** →
`INPUT/derived/sku_con_cluster.parquet` (140 rows).

### 4s.1 The machine codes are NOT in the MES — derived, and proven

`3401-3411` / `3801-3809` appear **nowhere** in `v_build`; `machineName` and
`machineCode` both carry `TBMPCR7Stage2`-style names. The mapping was derived by
cross-tabulating the workbook's own `Feb`..`Jul` per-month machine column against
each GT's MES dominant machine in that month, and it is **asserted at build
time** (`--min-purity`, default 0.85 — the build fails rather than shipping a
wrong map).

| | evidence | purity | verdict |
|---|---|---|---|
| PCR 3401-3411 | 222 GT-months | 98.0-100 % | **identity** `34NN → TBMPCR<NN>Stage2` |
| TBR 3801-3809 | 415 GT-months | 95.8-100 % | **identity** `38NN → TBMTBR<NN>Stage2` |

Independently confirmed by rim signature: workbook `3402 = R14,R16,R17,R18`
matches TBMPCR2's MES mix (R18 57 %, R17 17 %, R14 16 %, R16 11 %) — the §2
tier-3 flex machine — and every pure machine matches its single rim at 90-100 %.

⚠ **`3407` IS UNVERIFIED.** It has **zero rows in the PCR workbook** (only 10 of
11 PCR machines are listed), so there is no direct evidence for it in either
test. `TBMPCR7Stage2` is assigned **by elimination** — the other ten codes are
each pinned to their identity counterpart, so nothing else is left. The builder
prints `NO EVIDENCE — by elimination` for exactly this case. TBMPCR7 carries R13
in our own partition, so a wrong guess here would matter.

### 4s.2 The TBR GT-namespace trap, again — and a PCR coverage hole

| | direct `GT`-column match | SKU bridge | verdict |
|---|---|---|---|
| TBR | **0 of 75 GTs, 0.0 % of volume** | 70/75 GTs, **99.1 %** | the `GT 5003` codes are workbook-internal; bridge or nothing |
| PCR | 69/103 GTs, 66.5 % | 70/103, 68.5 % | both routes agree on the same 70 GTs, 0 conflicts |

**PCR coverage caps at 68.5 % of Feb-Jul volume / 65.3 % of July demand**, and
the hole is at the top: `GT 1513 XPC1 MSIL` (374,617 tyres, 16.7 % of PCR
volume) has no workbook row. The workbook's `GT1513 NEO` is a **different tread
pattern** — the ban on numeric-core matching (§4r.1) still stands.

### 4s.3 A CLUSTER OF N SKUs IS NOT N GREEN TYRES

This is the measurement that changes the reading of these files. The workbook's
unit is a SKU; the planner's is a GT, and PCR runs 1.23 SKUs/GT, TBR 1.95.

| | workbook clusters | multi-**GT** clusters | GTs in one | **July-active, co-active** |
|---|---|---|---|---|
| PCR | 134 (avg 1.74 SKUs) | 38 of 134 | 95/191 = 50 % | **4 GTs / 6.9 % of July demand** |
| TBR | 32 (avg 6.2 SKUs) | 16 of 32 | 86/102 = 84 % | **41 GTs / 81.6 % of July demand** |

And PCR's 38 include **one dustbin**: cluster 132 holds 15 GTs across **all
seven rims and seven machines** — the residue average-linkage leaves at cut 0.3.
Grouping on it would *manufacture* different-size changeovers. Rule 5 of the
builder drops any cluster spanning >1 rim (`--keep-multi-rim` to disable); TBR
has zero such clusters, so the rule is PCR-only in effect.

Of the 37 real PCR clusters, **33 are already single-machine single-rim** — they
are strictly inside what the rim lock and the partition already deliver.

### 4s.4 The plant DOES follow the clusters — on both plants, including PCR

Feb-Jul, true setup blocks (consecutive same-GT on one machine, split at a
stated gap cutoff). Null = **within-machine permutation** of that machine's own
block sequence, 1,000 reps — this controls for a machine with few GTs scoring
high by chance.

| | observed | permutation null | lift | z | realised gap: same-cluster | diff-cluster same-rim |
|---|---|---|---|---|---|---|
| **PCR** | **14.09 %** | 10.02 ± 0.54 % | **+4.07** | **+7.5** | **2.39 min** (p50, n=311) | **13.91 min** (n=1,375) |
| **TBR** | **67.24 %** | 44.21 ± 0.51 % | **+23.03** | **+45.5** | **7.53 min** (n=3,419) | **15.78 min** (n=1,329) |

Invariant to the cutoff (1 h and 4 h identical). Median-difference bootstrap:
PCR −11.52 min CI95 [−12.11, −11.04]; TBR −8.25 min CI95 [−8.63, −7.82].

**This is what corrects §4r.1.** PCR construction similarity is not absent — it
is *rare, same-rim, and therefore redundant*. The distinction matters: the old
wording says "no structure", which invites someone to stop looking.

⚠ Both figures are **observational**. A 2.4 min gap may mean the plant ran those
two back-to-back *because they were the same order*, not that the changeover is
physically cheaper. And both are **invisible to `v_changeover_build`**, which
has no component dimension (§4r.4) — so any benefit cannot appear in
`weighted_setup_h` and must be read from `cluster_adj_pct` in `arm_kpi.py`.

### 4s.5 LEVER 1 — cluster bucketing in the deadline heap (`PLANNER_CLUSTER_BUCKET_H`)

Put where §4r.5 proved adjacency lives: the **queue**, not the candidate-machine
sort. Same shape as `SISTER_BUCKET_H`, different key. *(`SISTER_GROUP` was inert
because `PIN_RUNS` breaks on the first feasible machine and the partition
usually leaves one candidate — ordering a list of one orders nothing.)*

**PCR: reject outright.** July 96.95 → 95.54 (−1.41 pt) and August 94.89 → 92.82
(−2.07 pt) at 24 h, while cluster adjacency moves 2.6 → 4.4 %. There is almost
nothing to group (§4s.3), so the reorder is pure cost. `PLANNER_CLUSTER_PLANTS=TBR`
makes PCR **bit-identical** to base — verified on every arm.

**TBR: mixed sign, and it is the month that flips it.**

| bucket | Jul ful | Jul clus% | Aug ful | Aug clus% |
|---|---|---|---|---|
| off | **95.02** | 39.4 | 96.82 | 49.0 |
| 2 h | 94.38 | 40.9 | **97.31** | 49.0 |
| 4 h | 94.62 | 42.9 | 97.22 | 50.0 |
| 8 h | 94.20 | 43.2 | — | — |
| 24 h | 93.87 | 45.9 | **97.34** | 53.6 |
| 48 h | 92.97 | 47.1 | 97.05 | 51.9 |
| plant | 100 | **67.2** | 100 | **67.2** |

**No bucket size is neutral in July** — even 2 h costs 0.64 pt. So the sign is
month-dependent, not tuning-dependent.

**Price it and it is worse than it looks.** At the plant's own 8.25 min per
converted transition:

| | converted same-cluster transitions | realised setup saved | fulfilment |
|---|---|---|---|
| Jul 24 h | 301 → 350 (+49) | **+6.74 h** | **−1.15 pt ≈ 1,127 tyres** |
| Aug 24 h | 249 → 277 (+28) | +3.85 h | +0.52 pt |

**Six and a half hours of setup for eleven hundred tyres is not a trade worth
making.** In August it is free, but note that the **2 h arm gains +0.49 pt with
adjacency unchanged at 49.0 %** — that gain is reordering luck, not the cluster
mechanism. Do not read August as evidence the mechanism pays.

Correlate, offered as hypothesis not cause: TBR occupancy is **78.4 % in July
and 64.2 % in August**. A reorder costs placement exactly when the machine is
tight.

### 4s.6 LEVER 2 — the workbook's `Assigned_Machine` as a partition seed (`PLANNER_PART_SEED=wb`)

The workbook's assignment is **much closer to plant behaviour than ours**:

| agreement with the plant's own Feb-Jul dominant machine | GTs | by volume |
|---|---|---|
| our mined partition, PCR | 40.7 % | 33.4 % |
| **workbook, PCR** | **88.9 %** | **95.9 %** |
| our mined partition, TBR | 31.4 % | 15.9 % |
| **workbook, TBR** | **82.9 %** | **86.3 %** |

**But it cannot be used as the partition.** It is an observation of six months,
not a plan for one, and it is **capacity-infeasible**: July TBR TBMTBR4 140.2 %,
TBMTBR1 123.4 %, TBMTBR2 114.4 %; August TBMTBR4 142.0 %, TBMTBR7 119.1 %,
TBMTBR8 112.3 %. On PCR it leaves **TBMPCR7 with zero load** while 31-35 % of
PCR demand has no workbook row at all.

It is also **92.9 % (PCR) / 84.3 % (TBR) identical to `gt_home_machine`** — i.e.
close to the `HARD_PIN` "pin to home" arm that §2 measured net-negative twice.

So it is wired as a **tier-0 preference** inside
`scripts/build_gt_machine_partition.py`: seat each GT on its workbook machine
first, but only where that machine already runs the GT's rim and still has free
hours; everything else falls through to tiers 1-5 and the G4 repair unchanged.
The result stays capacity-feasible by construction.

| | Jul PCR | Aug PCR | Jul TBR | Aug TBR |
|---|---|---|---|---|
| base | **96.95** | 94.89 | **95.02** | 96.82 |
| seed (PCR only, default) | 96.21 **(−0.74)** | **95.13 (+0.24)** | — | — |
| seed + `PARTITION_PLANTS=PCR,TBR` | — | — | 93.31 **(−1.71)** | **97.98 (+1.16)** |

**Mixed sign on both plants.** Side effects, stated: August PCR same-size
known-rim 89.9 → **91.4 %**, rim switches 323 → 315, R5 max 66.0 → 60.7 h,
inventory 4,225 → 4,200 — every secondary axis improves in August and none
regresses in July. July TBR cluster adjacency rises 39.4 → 45.3 % *for free*
(the workbook assignment co-locates cluster-mates by construction), but at
−1.71 pt.

Partition shape, seeded vs not: PCR GTs on exactly one machine 80.5 → 82.9 %
(July), machines carrying >1 size 2 → 1; TBR 91.3 → 93.5 %. Tier-0 seats
25/54 PCR and 41/51 TBR GT-parts in July.

### 4s.7 Verdict — the frontier, not a recommendation

**Both levers ship OFF.** Every arm holds every hard cap: sub-floor **0.0 %** on
both plants and both months, WIP inside the rail (PCR ≤ 4,661 vs 4,800; TBR
≤ 1,364 vs 1,400), R5 max ≤ 71.9 h vs 72, lot p50 unchanged (PCR ~307-316,
TBR 87-88), and `verify_export.py` **0 HARD / 0 SOFT / 0 EXPORT** on all eight
packs. Nothing here is unsafe; it is priced.

Two-month validation says **mixed**, and mixed is reported as mixed. If the
plant wants cluster campaigning it can have it at a stated price; nothing in
these files pays for itself on both months.

**The one unambiguous gain is the data, not the levers**: the derived and
asserted machine map, the 99.1 % TBR bridge, `cluster_adj_pct` as a KPI, and the
proof that PCR construction structure exists but is redundant with the rim lock.

---

## 4t. CLUSTER SEQUENCING — the same signal, re-keyed. TBR pays, PCR does not, and the gain CANNOT land in priced setup (2026-08-10)

`PLANNER_CLUSTER_SEQ`, in `l7_pull_release.py`. Supersedes `CLUSTER_BUCKET_H`
(§4s.5) as the implementation of the construction-cluster signal; that flag is
retained only as the control contrast.

### 4t.1 The signal, conditioned properly

§4s.4 measured cluster adjacency over **all** transitions. Conditioned on
**same-size** transitions — the only population an intra-size continuation rule
can act on — against a within-machine permutation null:

| | observed | null | lift | p | realised gap same-cluster | different-cluster |
|---|---|---|---|---|---|---|
| PCR | **20.4 %** | 15.5 % | +4.9 pp | 0.0005 | **2.3 min** | 14.0 min |
| TBR | **71.9 %** | 57.1 % | +14.8 pp | 0.0005 | **6.6 min** | 15.8 min |

So the plant's rule is **campaign continuation inside a size**, not cross-size
grouping. The key follows the rule: `(rim, cluster)` inside a bounded deadline
bucket, never the bare cluster id.

### 4t.2 THE MACHINE TERM STARVES THE BUCKET — measure density before scoping

The literal request was to key on cluster *within the same machine and rim*
(`mrc`). Measured on the July plan, distinct GTs per bucket cell:

| B | per (bucket, **machine**) | cells with >1 GT | per (bucket, **plant**) |
|---|---|---|---|
| 2 h | 1.01 | **0.7 %** | 7.0 |
| 4 h | 1.13 | **13.2 %** | 9.6 |
| 8 h | 1.53 | 50.3 % | 13.7 |
| 24 h | 2.87 | 89.5 % | 23.8 |

**Below B = 8 the cluster term has nothing to order while the machine term
re-sorts the bucket anyway — all of the disturbance, none of the mechanism.**
Dropping the machine term (`rc`) is safe because the workbook clusters are
single-rim by construction (`multi_rim` sums to 0) and 33 of 37 real PCR
clusters are already single-machine, so the cluster term implies both.

`mrc` is worse than `rc` on **all four** plant-months. It is kept as an option
and documented as measured-worse, so the literal wording is not re-tried.

### 4t.3 The sweep — fulfilment Δ vs base, fresh arms, partition per month

| key / B | Jul PCR | Jul TBR | Aug PCR | Aug TBR |
|---|---|---|---|---|
| `mrc` 2 | −0.18 | −0.42 | −0.51 | −0.44 |
| `mrc` 4 | −0.42 | −0.81 | −0.69 | −0.34 |
| `rc` 2 | −0.10 | **+0.05** | −0.55 | **+0.40** |
| **`rc` 4** | −0.62 | **+0.18** | −0.73 | **+0.83** |
| `rc` 8 | −0.46 | −0.10 | −0.92 | +0.29 |
| `rc` 24 | −1.53 | −0.55 | — | — |

**PCR: reject, on both months, at both keys, at every bucket** — and its cluster
adjacency does not even rise (2.6 % → 2.0-3.2 % under `rc`). That is §4s.3, not
a tuning failure: 4 PCR GTs / 6.9 % of July demand sit in a multi-GT cluster
with a co-active partner. **TBR: single-peaked at B = 4 on both months
independently**, which is why B = 4 is the default when the flag is armed.

Scoped to TBR the PCR plan is **bit-identical** to base — verified on
`build_schedule.parquet` for every arm on both months.

### 4t.4 WHERE THE GAIN LANDS — not in priced setup, and not in idle either

| TBR | Jul base | Jul on | Aug base | Aug on |
|---|---|---|---|---|
| fulfilment | 95.56 | **95.74** | 97.19 | **98.02** |
| cluster adjacency | 41.8 % | **42.9 %** | 47.9 % | **49.2 %** |
| weighted setup h | 138.3 | **140.0** | 104.3 | **105.1** |
| changeovers | 830 | 840 | 598 | 600 |
| idle h | 1,409 | 1,402 | 2,372 | 2,335 |
| gap at cluster-scored transitions, h | 925.6 | 937.4 | 1,286.2 | 1,220.8 |

**Priced setup gets WORSE on both months** (+1.7 h / +0.8 h) because the extra
volume placed adds changeovers, and `v_changeover_build` charges a same-cluster
and a different-cluster same-size transition identically (§4r.4, DO-NOT #34).

**And it does not land in measured idle either.** Our own plan's inter-run gap
carries **no cluster signal at all** — July TBR base median gap is **22.8 min
same-cluster against 10.0 min different-cluster**, i.e. the wrong way round.
Our gaps are placement artefacts, not setup. So the *entire* realised benefit of
this flag is off-model: it exists on the shop floor and in no number the engine
computes. What we can observe is `cluster_adj_pct`, and the fulfilment it costs
or buys.

### 4t.5 What a third tier in the cost master would be worth — the ceiling

Priced at the plant's own realised differential (TBR 9.2 min, PCR 11.7 min per
converted transition):

| | conversions this flag buys | worth | if adjacency reached the plant's | worth | vs priced setup |
|---|---|---|---|---|---|
| Jul TBR | +11 | **1.7 h** | 42.9 → 71.9 % (+229) | **35.0 h** | 140 h → **25 %** |
| Aug TBR | +10 | **1.5 h** | 49.2 → 71.9 % (+121) | **18.6 h** | 105 h → 18 % |
| Jul PCR | 0 | 0 | 2.6 → 20.4 % (+68) | 13.2 h | 376 h → 3.5 % |
| Aug PCR | 0 | 0 | 3.8 → 20.4 % (+88) | 17.2 h | 567 h → 3.0 % |

⚠ **These are UPPER BOUNDS.** The 2.3/14.0 and 6.6/15.8 figures are realised
wall-clock gaps, which include idle; the declared master is a flat 10 min (TBR)
and 22-28 min (PCR) same-size. A third tier calibrated *inside* the declared
envelope would have a smaller differential and a proportionally smaller ceiling.
**Do not overwrite the declared minutes with the empirical gaps** — same warning
as §4r.4.

**The follow-up is therefore a schema change, `same_size_cluster_min` in
`cap_changeover` / `v_changeover_build`, and it is worth at most ~25 % of TBR
setup and ~3 % of PCR setup.** Until it exists, no amount of cluster
sequencing can show up in `weighted_setup_h`.

### 4t.6 WHICH PCR CLUSTERS THE PLANT ACTUALLY FOLLOWS

July 2026, PCR, true setup blocks (>1 h split), transitions restricted to
**same-size, both GTs clustered**. Only **two** multi-GT clusters are active:

| cluster | GTs | rim | machine — plant / ours | plant stay/tot | ours stay/tot |
|---|---|---|---|---|---|
| **1** | `GT 1402 XPC TATA`, `GT 1412 XPC MM` | R12 | M4 / M2,M4 | **65/65 = 100 %** | **8/8 = 100 %** |
| **103** | `GT2166 BLA HT`, `GT2266 RAN HT` | R16 | M11 / M11,M2 | 2/8 = 25 % | 0/5 = 0 % |
| | | | **active-cluster total** | **67/73 = 91.8 %** | **8/13 = 61.5 %** |
| | | | **all PCR same-size clustered** | **67/324 = 20.7 %** | **8/340 = 2.4 %** |

**All 67 of the plant's PCR same-cluster transitions come from cluster 1** — the
R12 pair alternating on TBMPCR4. The plant's headline 20.4 % is that ONE pair
and nothing else, which is why the PCR arm cannot pay: our runs on that pair are
longer and fewer (8 transitions against 65), so there is almost nothing to
convert. Cluster 103 is the only place a PCR gain is available at all, and it is
8 transitions wide.

### 4t.7 Verdict

**Shipped ON, both plants, by plant instruction (2026-08-10).** The measurement
rule ("positive on both months on both plants") is NOT met — PCR is −0.62 Jul /
−0.73 Aug — and the plant has accepted that cost. `PLANNER_CLUSTER_SEQ_PLANTS=TBR`
reverts PCR to bit-identical without touching the TBR gain.

The TBR frontier point is real and safe: `rc` / 4 h —
**+0.18 pt Jul TBR, +0.83 pt Aug TBR, PCR bit-identical**,
sub-floor **0.0 %** on both plants and both months, WIP inside the rail
(TBR 1,332 / 1,340 vs 1,400), R5 max 65.7 / **71.3** h vs 72, lot p50 87/88
unchanged, **0 L11 flips** in either direction, and `verify_export.py`
**0 HARD / 0 SOFT / 0 EXPORT** on all four packs.

⚠ August TBR R5 max is **71.3 h against the 72 h cap** (base 68.5). It holds,
but there is 0.7 h of headroom — re-check it on any month where this is armed.

---

## 4u. THE PARTITION BATCH — CP-SAT builder, B16 as an input, and four defects I introduced doing it (2026-08-17/18)

Nothing in this batch was in any `.md` until now; it lived only in code comments,
which is how §1i's phantom 0.7 pt survived long enough to be quoted back.

**What shipped.** `scripts/cpsat_partition.py` replaced `build_gt_machine_partition.py`
as the default builder. It needs no raw MES, so ANY month can be stamped — the old
builder could not stamp July at all, which is why every July run set
`PLANNER_PARTITION_PLANTS=` and **the partition feature shipped unused for months**.
The model proves OPTIMAL on both plants and two rebuilds are byte-identical, verified
across sessions (July sha1 `8a47e109e27d`, August `8bcb10c113bf`).

**The measured value, with the matrix held fixed** — an earlier table in `main.py`
straddled two states of `allowed_machine_matrix.parquet` and credited the partition
with 4,116 tyres that were the matrix:

| | PCR ful% | PCR unfed | PCR same-size |
|---|---|---|---|
| Jul OFF → ON | 96.1 → 96.2 | 8,670 → 8,641 | **79.0 → 81.1 %** |
| Aug OFF → ON | 91.0 → 91.1 | 6,566 → **6,856** | **74.6 → 75.4 %** |

Fulfilment is flat and August unfed goes the WRONG way. The case for the partition is
same-size — 18 and 20 fewer different-size changeovers at 42-60 min each — which is
what it was built for. **Do not quote it as a fulfilment gain.**

**PCR ONLY.** `PLANNER_PARTITION_PLANTS` was widened to `PCR,TBR` on 08-17 and reverted
on 08-18. TBR partitioning removes 15-24 changeovers/month but BUILT flips sign
(−168 Jul / +174 Aug) — a two-month-gate failure, on a deterministic engine, so not
noise. Note the first attempt at this decision compared `""` vs `PCR,TBR` — **neither
arm was `PCR`**. Run the arm you are deciding about.

**Four defects introduced by switching it on, all found by a gate, none by inspection:**

| defect | found by | cost |
|---|---|---|
| partition ignored B16, put TL GTs on a TT machine | explicit violation check | 367 lots / 9,537 tyres across the boundary |
| imbalance capped plant-wide, not per B16 group | partition row count (59 = PCR only) | August TBR INFEASIBLE |
| `DET_BUDGET=60` truncated the search | bound print (gap 51.6 %) | non-reproducible partition, every run |
| `det_time` default written in two files | `_partition_reason` output | 13 min rebuild on every plan |

**And the B16 fix is INERT at the shipped default.** `l7_pull_release.py` filters
`if r["plant"] in PARTITION_PLANTS`, so with `PCR` the TBR partition rows are built,
B16-restricted, emitted — and discarded. The violation existed only while the default
was `PCR,TBR`, i.e. for one day, because I made it so. Keep the restriction as a guard;
**credit it with nothing.**

---

## 4v. EDD / INTRA-MONTH DUE DATES — measured, LOST 3.8 pt, and the symptom that motivated it was a measurement artefact (2026-07)

Two run directories, `runs/EDD_off` and `runs/EDD_on`, have carried this result with no
document knowing it. That is why the question keeps coming back.

| | EDD off | EDD on | Δ |
|---|---|---|---|
| PCR fulfilment | **96.2 %** | 92.4 % | **−3.8 pt** |
| TBR fulfilment | **96.7 %** | 93.7 % | **−3.0 pt** |
| PCR BUILT | 385,257 | 381,156 | −4,101 |
| PCR carry-out tail | 7,416 | 19,728 | +12,312 |
| L11 invariants | 31/48 | 27/48 | −4 |

**The symptom it was aimed at does not exist.** "L5 seats 59 % of PCR volume on day 0"
attributes each campaign's whole quantity to its START — but a PCR cure campaign is a
**365 h occupancy** (TBR 491 h), and 86 day-0 campaigns is 86 of 86 presses, i.e. a
24/7 plant seating every press at hour zero. Realised delivery is already level:
**PCR day-1 share 2.04 %, first 7 days 22.7 %, daily CV 0.097; TBR 2.39 % / 22.5 % / 0.063.**
This is `MEMORY` §"mean over events ≠ mean over time" applied to campaign quantity.

**And `due_date` is NOT lost at L4.** `net_requirement` carries no timing, but
`l45_lotsize.py` re-reads `masters/demand/demand_<M>.parquet` directly, builds a per-GT
phase curve from `day`, and emits `lot_deadlines`, which `l5_cure_master.py` sorts on
behind `PLANNER_L5_EDD` (default 0). The channel exists; it is off because it loses.

**Physics:** a plant with a 72 h perishable intermediate and month-long press campaigns
cannot ship to intra-month dates from the press. It ships from finished stock; the press
runs flat out. Deferring a seat to match a day-20 date idles the press from hour 0 and
pushes the campaign tail past the boundary — which the tail column shows.

**Do not re-open without refuting the 2.04 % day-1 share.**

---

## 4w. MOST-CONSTRAINED-FIRST IN THE L7 PLACEMENT HEAP — positive on both plants at B=8, ONE MONTH ONLY, default still OFF (2026-08-19)

**The defect, and it is real.** In `l7_pull_release.py`'s placement heap the key is
`(_prio, _hkey(t_due), sc, _i, …)` where `sc` **is** `_n_elig(p, gt)`. Eligibility is the
**third** key, and `_hkey` ends in the exact cure timestamp — so two jobs essentially
never tie and **scarcity never orders this queue, the deadline does**. A GT allowed on
five machines and due an hour earlier takes a one-machine GT's only machine, and the
pinned GT starves with nowhere to go.

**Four PCR GTs have exactly one allowable machine, carrying 45,136 tyres = 11.4 % of July
PCR demand.** `TBMPCR4` is the sole machine for `GT 1402 XPC TATA` + `GT 1412 XPC MM`
(26,103 tyres) while **158,401 tyres of flexible demand also list it**; `TBMPCR3` carries
`GT 1865 ROYL RENO` against 120,126 flexible; `TBMPCR9` carries `GT 2568 HT2` against
88,834.

**The fix under test:** `PLANNER_L7_PINNED_FIRST=B` (0 = off) rounds the deadline down to
a **B-hour bucket** and orders **least-flexible-first inside the bucket**, keeping the
true deadline and every existing term below it. It does **not** drop the deadline —
this is a pull system and L5 has already fixed the cure times. Same bucketing shape as
`SISTER_BUCKET_H` / `CLUSTER_SEQ_H`; the shipped key is **nested**, not replaced, so key
arity stays homogeneous and at B = 0 the key is the shipped one.

**Measured, July 2026, five fresh `main.py plan` arms in one invocation, one frozen July
partition (sha1 `8a47e109e27d`, stamped 2026-07, CP-SAT OPTIMAL both plants), post-F1
masters (`net_requirement` `41c8d601d747`). All arms `check_arm_fresh` FRESH.**

| PCR | BUILT | ΔBUILT | in-month | ful% | tail | unfed | same% | wCO h | R5max | L11 |
|---|---|---|---|---|---|---|---|---|---|---|
| B=0 | 389,949 | +0 | 384,621 | 96.8 | 10,806 | 8,312 | 83.7 | 552.0 | 71.9 | 31/48 |
| B=4 | 390,440 | **+491** | 385,112 | 96.9 | 10,806 | 7,821 | 83.2 | 557.1 | 71.8 | 31/48 |
| B=8 | 390,600 | **+651** | 385,272 | 97.0 | 10,806 | 7,661 | 82.9 | 553.1 | 70.6 | 31/48 |
| B=24 | 391,048 | **+1,099** | 385,720 | 97.1 | 10,806 | 7,213 | 81.6 | 567.7 | 71.9 | 31/48 |

| TBR | BUILT | ΔBUILT | in-month | ful% | tail | unfed | same% | wCO h | R5max | L11 |
|---|---|---|---|---|---|---|---|---|---|---|
| B=0 | 96,318 | +0 | 94,095 | 96.6 | 3,540 | 2,625 | 100.0 | 174.3 | 71.8 | 31/48 |
| B=4 | 96,555 | **+237** | 94,332 | 96.8 | 3,540 | 2,388 | 100.0 | 172.5 | 71.8 | 31/48 |
| B=8 | 96,555 | **+237** | 94,332 | 96.8 | 3,540 | 2,388 | 100.0 | 173.2 | 71.8 | 31/48 |
| B=24 | 96,204 | **−114** | 93,990 | 96.5 | 3,531 | 2,739 | 100.0 | 172.3 | 71.8 | 31/48 |

**The tail is 10,806 PCR / 3,540 TBR in EVERY bucket arm, identical to base.** BUILT and
in-month move together by the same amount with the tail pinned, so this is **not** the
v14 / §4v relocation pattern. It creates output.

**Where the tyres come from, exactly.** `GT 1865 ROYL RENO` (TBMPCR3-only) builds 2,244
of 3,029 at B=0 and leaves **785 unfed — 26 % of its own requirement**. At every B ≥ 4 it
builds **3,029 and leaves ZERO**. That single GT is the whole PCR gain at B=4.

**And what it cannot touch — the more useful half.** The other three single-machine PCR
GTs do not move one tyre at ANY bucket: `GT 1402 XPC TATA` 20,539 built / 176 unfed,
`GT 2568 HT2` 15,364 / 172, `GT 1412 XPC MM` 4,942 / 239. Their residual starves as
**`release_before_t0`** (PCR total 3,646 → 3,309 at B=8), i.e. the build would have to
start before the month opens. **That is the ~4 h carry-in question — a PLANT RULING —
and no queue order reaches it.** Do not re-tune this flag expecting to collect them.

**B=24 is the trap.** Best PCR arm and a clean gate FAILURE: TBR BUILT −114, PCR
same-size 83.7 → 81.6 pt while weighted changeover rises 552.0 → 567.7 h, and L7's own
daily-mean-max prints **PCR 4,821 OVER the 4,800 G8 rail**. Mixed sign across plants
fails however good the headline plant looks (DO-NOT #14).

**The price at B=8, in one sentence:** +651 PCR / +237 TBR BUILT costs PCR same-size
83.7 → 82.9 pt and PCR weighted changeover 552.0 → 553.1 h, with R5 max **improving**
71.9 → 70.6 h and B12 sub-floor 0.00 % throughout.

**Two rail bases disagree, so quote both.** L7's own check is clean on BOTH plants only
at B=8 (PCR 4,753 / TBR 1,398); B=4 prints **TBR 1,401 OVER by one tyre** and B=24 prints
PCR 4,821 OVER. `scripts/arm_kpi.py` recomputes the same quantity post-reconcile and
disagrees by ~+85 PCR / −3 TBR (B=8: PCR 4,838, TBR 1,399) **against a BASE that is itself
1,402, i.e. already over on TBR**. That spread is the known §1f pre-flight-vs-post-reconcile
drift. **No arm changes any of the 48 L11 invariant statuses** — 31/48 in all four with
the same 17 failures, and the PCR G8 *mean* stays BELOW its band (4,004 → 3,906), which is
the intentional under-run, not a breach.

**B = 0 is byte-identical to the pre-flag engine** on `build_schedule`, `cure_campaigns`,
`gt_events`, `build_starved`, `cure_campaigns_reconciled`, `mould_changes` and
`l11_invariants` — verified by reverting the file and running it as its own arm
(`sc_pf0ref`), not by inspection.

**NOT ADOPTED AS THE DEFAULT.** This is a one-month result and **July is the easy month**
(its demand is the plant's own July output, 100 % achievable, no arithmetic ceiling). v12
shipped two flags on a July-only A/B and v13 reverted both. **Run August through
`scripts/ab_both_months.py` before moving this default.**

**Not the same change as §4v.** EDD *dropped* the deadline at L5 and lost 3.8 pt. This
keeps the deadline and reorders only inside a bucket at L7.

### 4w.1 THE AUGUST GATE — RUN 2026-08-19. **B = 8 REJECTED. DEFAULT STAYS 0.**

The outstanding call above, closed. Four fresh arms (`sc_apf0/4/8/24`), **one frozen
August partition** (sha1 `8bcb10c113bf`, stamped 2026-08, CP-SAT OPTIMAL, 95 rows) whose
provenance was **re-verified against the masters the arms themselves regenerate** — all
five input sha1s (`net_requirement ca7b004f8744`, `cap_machine bba5249cd1d2`,
`cap_ttl_groups 49787c6f2de0`, `gt_size f1ad3ad32766`, `allowed_machine_matrix
144a8e1d3283`) equal before and after an L2/L4/L4.5 re-run, so no rebuild was needed and
none happened (file mtime pre-dates every arm). Env `PLANNER_OPENING_GT=
opening_gt_manual_2026-08.parquet`, `PLANNER_LOT_INTERVAL_H=8`,
`PLANNER_TH_GT_WIP_RAIL_MARGIN=1.0`. All four **FRESH** under `check_arm_fresh.py`; all
four `build_schedule.parquet` sha1s distinct; `cure_campaigns.parquet` **identical in all
four**, the correct signature of an L7-only queue change.

| PCR | BUILT | ΔBUILT | in-month | ful% | tail | unfed | starv | same% | wCO h | R5max | L11 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| B=0 | 408,349 | +0 | 387,837 | 90.9 | 24,525 | 8,212 | 4,513 | 75.9 | 645.7 | 71.4 | 28/48 |
| B=4 | 410,916 | **+2,567** | 389,116 | 91.2 | 25,813 | 5,645 | 1,946 | 76.7 | 645.9 | 71.7 | 29/48 |
| B=8 | 410,752 | **+2,403** | 389,087 | 91.2 | 25,678 | 5,809 | 2,110 | 76.9 | 641.2 | 71.6 | 29/48 |
| B=24 | 410,916 | **+2,567** | 389,116 | 91.2 | 25,813 | 5,645 | 1,946 | 75.9 | 632.4 | 72.0 | 29/48 |

| TBR | BUILT | ΔBUILT | in-month | ful% | tail | unfed | starv | same% | wCO h | R5max | L11 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| B=0 | 97,182 | +0 | 94,176 | 95.4 | 4,222 | 2,799 | 1,810 | 100.0 | 137.7 | 71.1 | 28/48 |
| B=4 | 97,188 | **+6** | 94,187 | 95.4 | 4,217 | 2,793 | 1,804 | 100.0 | 136.0 | 65.6 | 29/48 |
| B=8 | 97,104 | **−78** | 94,099 | 95.3 | 4,221 | 2,877 | 1,888 | 100.0 | 136.7 | 67.7 | 29/48 |
| B=24 | 97,386 | **+204** | 94,396 | 95.6 | 4,206 | 2,595 | 1,606 | 100.0 | 133.2 | 66.8 | 29/48 |

**The two-month verdict, per bucket — the only reading that decides:**

| bucket | Jul PCR | Jul TBR | Aug PCR | Aug TBR | verdict |
|---|---|---|---|---|---|
| B=4 | +491 | +237 | +2,567 | **+6** | non-negative (TBR neutral, see below) |
| B=8 | +651 | +237 | +2,403 | **−78** | **REJECT — sign flips by month** |
| B=24 | +1,099 | **−114** | +2,567 | +204 | **REJECT — sign flips by month** |

**B = 8 is rejected on two independent counts, either sufficient.**
1. **TBR BUILT −78 on August against +237 on July.** A flag whose sign flips by month on
   a plant is fitted to a month. This is the +0.6 / −0.5 pattern that cost v12 two flags.
2. **It creates a NEW L11 FAILURE**: `TBR median GT wait vs tau*` **PASS → FAIL**
   (5.05 h → 5.78 h). **No July arm moved any of the 48 statuses.** B=24 breaks the same
   invariant (5.05 → 6.10 h).

**THE L11 PASS COUNT IS A TRAP HERE.** Every bucket reads **28/48 → 29/48**, which looks
like a strict improvement. At B=8 that is a *net* of three flips: PCR median GT wait
FAIL→PASS, PCR throughput vs plant rate FAIL→PASS, **TBR median GT wait PASS→FAIL**. The
aggregate rises while a TBR invariant goes red — the per-segment-regression-hidden-by-an-
aggregate failure mode named in `EXPERT_AUDIT.md`. **Diff the status column, never the
count.**

**Rails (G8 daily-mean-max) — both bases, neither picked.** The §1f pre-flight-vs-post-
reconcile spread is live (−67 … +64 here). **The August base is already OVER on both
plants on both bases**, so no arm can be credited with "no breach"; read as movement only.

| arm | L7 PCR | L7 TBR | `arm_kpi.py` PCR | `arm_kpi.py` TBR |
|---|---|---|---|---|
| B=0 | 4,845 OVER | 1,407 OVER | 4,852 OVER | 1,403 OVER |
| B=4 | 4,841 OVER | 1,402 OVER | 4,848 OVER | 1,399 OK |
| B=8 | **4,795 ok** | 1,402 OVER | **4,859 OVER** | 1,404 OVER |
| B=24 | 4,830 OVER | 1,420 OVER | 4,763 OK | 1,410 OVER |

**The two bases contradict each other on PCR for the single arm under decision** (B=8:
L7 clean, `arm_kpi` over) — which is itself why the rail cannot carry this call.

**Why August differs from July mechanically** — this is not a mystery result. August's PCR
gain is ~4× July's and comes from a **different cause**: `r5_shelf_life` starvation
collapses 2,592 → 0 (B=4) / 320 (B=8) while `release_before_t0` barely moves
(1,921 → 1,946 / 1,790). July's gain was machine contention on `GT 1865 ROYL RENO`.
August is a 99.5 %-loaded month, so reordering the queue changes **when green tyres wait**,
and the **R5 72 h clock — not contention — is what was killing them**. That also explains
the TBR cost: PCR pulls earlier, TBR green tyres wait longer, median GT wait crosses tau*
and 78 tyres die.

**B = 4 is the only bucket non-negative on both plants in both months**, but its August TBR
movement is **+6 tyres = +0.006 pt**, an order of magnitude below `ab_both_months.py`'s own
**0.05 pt** noise floor. The honest description is **PCR-positive, TBR-neutral**, not
"positive on both". It is a **candidate for a separate adoption decision**, not a default
this run is entitled to move. **Do not re-tune B until TBR passes** — that is retuning
until the metric agrees.

**`PLANNER_L7_PINNED_FIRST` default remains `0`.**

---

## 4x. MEASURED AND REJECTED (again): `HORIZON_MODE=strict` on July — it does not even relocate

Run in the same invocation, same partition, same masters, as the honest counterweight to
"make the tail go away".

| | PCR B=0 | PCR strict | TBR B=0 | TBR strict |
|---|---|---|---|---|
| BUILT | 389,949 | **372,913 (−17,036)** | 96,318 | **91,129 (−5,189)** |
| in-month | 384,621 | **377,265 (−7,356)** | 94,095 | **92,195 (−1,900)** |
| ful% | 96.8 | 95.0 | 96.6 | 94.6 |
| tail | 10,806 | 1,787 | 3,540 | 459 |
| in-month+tail | 395,427 | **379,052 (−16,375)** | 97,635 | **92,654 (−4,981)** |
| L11 | 31/48 | **28/48** | 31/48 | **28/48** |

**This relocates output, it does not create it — and on July it does not even relocate
it: in-month FALLS too.** The August precedent (v14) at least raised in-month while BUILT
fell; here both fall on both plants, total real output falls 16,375 / 4,981, and three L11
invariants flip to FAIL (`TBR mean GT inventory (G8)`, `TBR last-day GT inventory (G8)`,
`TBR mould changes / press-day occupied`). `GT 1402 XPC TATA` alone loses **3,432 tyres**.
Consistent with the v14 August sweep (`strict −30,572 BUILT`). The tail is real output the
plant gets in the first days of the next month; deleting the horizon deletes the tyres.

---

## 4y. A FLAT, CAPPED DAILY CURE RATE — the SHAPE is constructible, the OUTPUT is not (2026-08-19)

**Instruction under test.** *"Cure a flat 13,000/day on PCR and 3,200/day on TBR, flat from
day 1, no month-end tail; finish demand inside the month and bank the surplus (≈4,500 PCR /
≈1,200 TBR) as next month's opening GT."* 13,000 × 31 = 403,000 against 398,405 of July PCR
demand. (The plant's own figure for TBR was 3,000/day; **substituted to 3,200** because
3,000 × 31 = 93,000 is 5,020 *below* July TBR demand — it caps the plant under its
requirement by construction. The 3,000 case was measured anyway, below.)

**There was no daily rate governor in the engine at all.** `PLANNER_L5_TAKT` is a budget on
concurrently *seated presses*, TBR-only by default, and re-measured inert on the current
baseline (PCR byte-identical at every setting). So one was built: `PLANNER_L5_DAY_CAP=1`,
cap values in `CONFIG.thresholds.cure_day_cap` (§8), ledger charged at `_takt_commit` — the
one site every seat passes through. Default off; with the cap inert the layer is
**byte-identical** to the shipped one (`cure_campaigns.parquet` md5 `35ca003f4c5c…` on both).

### 4y.1 The result

Arms fresh via `scripts/run_arm.py`, gated by `check_arm_fresh.py`. July partition rebuilt
for the month (`cpsat_partition.py 2026-07`, PCR **OPTIMAL** obj 1,044 in 773 s, sha1
`8a47e109e27d`); August on the stored `8bcb10c113bf`. `PLANNER_CARRY_IN` off on both months
(`masters/carry_in/` does not exist). `floor` = `PLANNER_L5_FLOOR_BASIS=lot`.

| | Jul PCR BUILT | dBUILT | Jul PCR in-month | Aug PCR BUILT | dBUILT | Aug PCR in-month |
|---|---|---|---|---|---|---|
| base | 387,098 | +0 | 381,854 (96.1 %) | 407,125 | +0 | 386,447 (90.6 %) |
| cap | 388,519 | **+1,421** | 380,490 (95.8 %) | 403,689 | **−3,436** | 380,662 (89.2 %) |
| floor | 383,686 | −3,412 | 380,528 (95.8 %) | 404,220 | −2,905 | 384,945 (90.2 %) |
| cap+floor | 385,069 | **−2,029** | 378,765 (95.3 %) | 401,506 | **−5,619** | 380,424 (89.2 %) |

| | Jul TBR BUILT | dBUILT | Jul TBR in-month | Aug TBR BUILT | dBUILT | Aug TBR in-month |
|---|---|---|---|---|---|---|
| base | 95,312 | +0 | 93,107 (95.6 %) | 97,211 | +0 | 94,188 (95.4 %) |
| cap | 95,312 | **+0** | 93,107 (95.6 %) | 97,211 | **+0** | 94,188 (95.4 %) |
| floor | 95,458 | +146 | 93,643 (96.1 %) | 96,931 | −280 | 94,299 (95.5 %) |
| cap+floor | 95,458 | +146 | 93,643 (96.1 %) | 96,931 | −280 | 94,299 (95.5 %) |

Shape, from the governor's own scheduled-cure ledger (L11 is the whole-run count):

| arm | Jul PCR CV / day 1 | Aug PCR CV / day 1 | Jul TBR CV / day 1 | L11 Jul \| Aug |
|---|---|---|---|---|
| base | 0.0891 / 7,813 | 0.0956 / 7,251 | 0.0547 / 2,278 | 32 \| 28 of 48 |
| cap | 0.0830 / 7,748 | 0.0832 / 7,102 | 0.0547 / 2,278 | 32 \| 29 |
| floor | 0.0666 / 10,871 | 0.0623 / 10,705 | 0.0377 / 2,695 | 33 \| 30 |
| cap+floor | **0.0531** / 10,717 | **0.0460** / 10,295 | **0.0377** / 2,695 | 31 \| 29 |

**REJECTED.** The cap alone is **+1,421 BUILT on July PCR and −3,436 on August PCR** —
mixed sign across months, which fails the gate on its own. The coupled arm is negative on
both months. TBR is **byte-identical to base at 3,200/day on both months**: that is the cap
never binding, not the cap being safe.

### 4y.2 The hypothesis this existed to test, and its refutation

Every previous attempt to lower the day-1 release wall lost tyres (`WARM_RELEASE` −1,319,
`T0_STOCK_BASIS=lot` −1,670, `FLOOR_BASIS` min/slice −2,478…−6,050, `CHG_PARALLEL` −3,665),
and the recorded reason is that **the wall protects building's lead time**. The new argument
was that capping the cure rate *below* the fleet maximum leaves building slack to catch up,
so the wall could finally come down safely. That is a claim about an **interaction**, and it
is measurable directly:

| | cap alone | floor alone | additive prediction | measured cap+floor | interaction |
|---|---|---|---|---|---|
| July PCR | +1,421 | −3,412 | −1,991 | **−2,029** | −38 (−0.01 %) |
| Aug PCR | −3,436 | −2,905 | −6,341 | **−5,619** | +722 (+0.18 %) |

**The two effects are additive to within ±0.2 % of BUILT and the interaction flips sign.**
There is no coupling. A ceiling on the cure *rate* does not shorten a lead *time*.

### 4y.3 Three structural reasons, before any tuning

1. **A cap is a ceiling and day 1 is a floor problem.** Day-1 cure is 7,813 (Jul) / 7,251
   (Aug) because of `earliest_cure`'s 11.86 h wall. No ceiling raises it. Only `FLOOR_BASIS`
   does, and even at the *physical* 2.35 h lot floor day 1 reaches 10,717 / 10,295 — still
   18–21 % short of 13,000, the remainder being day-1 mould changes.
2. **Flatness is bought with the tail the instruction forbids.** The governor levels by
   pushing seats right, and right eventually means out of the month. July PCR campaigns
   crossing month end: base 134 (54,195 tyres) → cap 142 (61,059) → a 12,300/day arm 163
   (73,876); tail 10,339 → 13,044 → 20,480. *Flat* and *no tail* are opposed under this
   mechanism, not complementary. The flattest curve measured (12,300/day + floor, PCR
   scheduled CV **0.0322**) costs 13,309 tyres of in-month fulfilment (96.1 → 92.8 %).
3. **The requested numbers straddle the plant's own ceiling in opposite directions.** L5
   seats July PCR in **63,499 press-h of 63,984 available = 99.2 % press utilisation**, and
   the already-full days sit at **13,228/day**. 13,000 is 98.3 % of that — it binds on
   **2 seats** — so "13,000 × 31 = 403,000" is arithmetic about a day-1 the plant does not
   have. TBR's plateau is **3,186/day**, so 3,200 never binds at all.
   The originally requested TBR **3,000/day**, measured: BUILT −8, but in-month
   93,107 → **90,077** and tail 3,522 → **6,398**. It does not destroy tyres, it **relocates
   ~3,000 of them into next month** — the §1 relocation signature, read correctly only
   because BUILT was reported beside in-month.

### 4y.4 The two safety numbers

R5 (72 h hard) degrades with flatness — July PCR GT wait max **62.3 h base → 68.4 h cap →
70.2 h cap+floor**; a flatter cure curve holds green tyres longer, and 70.2 leaves 1.8 h.
The GT WIP rail held on every arm (PCR daily-mean max 4,557–4,669 vs 4,800; TBR 1,311–1,327
vs 1,400), so nothing here was bought by breaching G8.

### 4y.5 What the instruction's *other* half costs

The banked-surplus target (≈4,500 PCR of next-month opening GT) is in direct conflict with
finishing demand in-month at 99.2 % press utilisation. Month-end carry-forward GT measured:
base **1,486**, cap 1,340, cap+floor 1,011, and the only arm that approaches the target
(12,300/day + floor, **3,346**) does so by giving up 13,309 in-month cures. At this press
loading the plant cannot both satisfy July and hand August a 4,500-tyre float; that is a
capacity ruling, not a scheduling one.

---

## 4z. OPENING-STOCK FIRST IN THE L5 SEAT QUEUE — every stated objective moves the right way and BUILT falls on both plants (2026-08-19)

**Target under test.** Day-1 cure ≥ 13,000 PCR / ≥ 3,000 TBR. Base `runs/sc_pf8` (July,
`PLANNER_L7_PINNED_FIRST=8`): PCR day-1 **7,813** against a day-2-onward mean of 12,692;
TBR day-1 **2,278** against 3,111. Short 5,187 / 722.

### 4z.1 The defect is real, and it is NOT changeover

Day-1 PCR press time is 2,064 press-hours: **1,216 h curing, 0 h mould change, 848 h idle**.
Sixty of 86 presses do not start at hour 0, median wait 14.1 h. `earliest_cure` already
returns `t0` for a GT holding `>= gap_q` of opening stock — but **which** campaign reaches a
press first is decided by the L5 job sort, which is pure `-qty`. A large stockless campaign
(floor 11.86 h on PCR) outranks a smaller stocked one and takes the opening seat.

Measured on `runs/sc_pf8` **before writing any code**, and this is the number that justified
building the flag at all:

| | PCR | TBR |
|---|---|---|
| t0 seats the opening stock **pays for** (Σ ⌊stock/gap_q⌋) | **57** | **64** |
| campaigns actually seated at t0 | 26 | 34 |
| **unclaimed paid seats** | **31** | **30** |
| late-starting presses whose first GT holds ≥ gap_q | 38 of 60 | 24 of 45 |
| opening stock never consumed at all | 468 | 231 |

### 4z.2 The change

`PLANNER_L5_STOCK_FIRST=1` (ships **0**) promotes to the head of the queue only those
campaigns the opening stock can actually pay a t0 seat for —
`min(⌊stock/gap_q⌋, moulds (R3), eligible presses)` per GT — keeping `-qty` inside the head
and untouched from the first unpaid job onward. On July that is 54 of 276 PCR jobs and 54 of
195 TBR. **`-qty` was not displaced globally**: `PLANNER_D1_DEPTH` did that and lost 11.9 pt,
`PLANNER_L5_EDD` prefixed it and lost 3.8 pt (§4v).

`gap_q` was extracted into one function `gap_q_of()` read by both `earliest_cure` and the
promotion, because the debit site had already priced this in the wrong currency once. With
the flag off the engine is **byte-identical** — `cure_campaigns`, `build_schedule`,
`gt_events` and `l11_invariants` all sha1-match `runs/sc_pf8`, which predates the edit.

### 4z.3 The result — fresh arms, `scripts/arm_scorecard.py`, partition `8a47e109e27d` (2026-07), `check_arm_fresh` clean

| | PCR base | PCR sf1 | TBR base | TBR sf1 |
|---|---|---|---|---|
| **day-1 cure** | 7,813 | **9,076 (+1,263)** | 2,278 | **2,479 (+201)** |
| day-2 | 13,228 | **13,043** | 3,186 | **3,007** |
| day-2-onward mean | 12,692 | **12,662** | 3,111 | **3,086** |
| daily mean | 12,535 | 12,546 | 3,084 | **3,066** |
| **BUILT** | 390,600 | **386,263 (−4,337)** | 96,555 | **95,956 (−599)** |
| in-month | 385,272 | 381,841 | 94,332 | 93,543 |
| ful% | 97.0 | 96.1 | 96.8 | 96.0 |
| tail | 10,806 | 10,121 | 3,540 | 4,020 |
| unfed | 7,661 | **12,196** | 2,388 | 2,885 |
| starved | 3,309 | **7,400** | 1,322 | 1,634 |
| opening stock unused | 468 | **24** | 231 | **46** |
| same-size (L11 / `arm_kpi`) | 81.3 / 82.9 % | 81.0 / 82.7 % | 100.0 / 100.0 % | 100.0 / 100.0 % |
| weighted CO min/machine-day | 87.9 | 85.8 | 34.3 | 33.6 |
| weighted setup h (`arm_kpi`) | 553.1 | 542.3 | 173.2 | 173.8 |
| **G8 daily-mean max — L7 basis** | 4,753 | **4,799** (rail 4,800) | 1,398 | **1,407 — OVER 1,400** |
| **G8 daily-mean max — `arm_kpi` basis** | 4,838 | 4,851 (already over in base) | 1,399 | 1,399 — under |
| GT inventory, time-weighted mean | 3,725 | 3,696 | 1,224 | 1,189 |
| sub-floor run share | 0.0 % | 0.0 % | 0.0 % | 0.0 % |
| lot p50 | 304 | 304 | 80 | 80 |
| R5 max | 70.6 h | **71.9 h** (hard 72) | 71.8 h | 68.6 h |
| L11 | 31/48 | 31/48 | 31/48 | 31/48 |

**Print both G8 bases — they disagree and the disagreement is load-bearing here.** On L7's
basis TBR goes 1,398 → **1,407, over the 1,400 rail**; on `arm_kpi`'s it is 1,399 → 1,399,
under it, while PCR is *already* over on `arm_kpi`'s basis in the base arm. Neither number is
quotable alone. L11's own gate resolves it against the flag: `TBR last-day GT inventory (G8)`
goes PASS → FAIL.

**Day-1 cure rises while BUILT falls on both plants. That is relocation, not creation** — and
it is worse than relocation, because the tyres do not reappear later either: day 2 falls on
both plants, the day-2-onward mean falls on both, and the PCR daily mean moves **+11 on a
12,535 base while total output drops 4,337**. Day-1 was bought from days 2–31 *and* from the
month. It also never reaches the target: 9,076 against 13,000.

### 4z.4 The mechanism of the loss — and the pre-measurement claim it refutes

The flag does everything it was designed to do. t0 seats PCR 26 → 32 and TBR 34 → 50; late
presses PCR 60 → 54 and TBR 45 → 29; the 699 tyres of expiring opening stock become **70**.

The design note written *before* measuring argued this was "not another early-release
experiment" because `earliest_cure` is untouched and every promoted campaign was already
legal. **Per campaign that is true. In aggregate it is false.** The t0 window is a *shared
build-feed resource*: seating more campaigns in it oversubscribes building's day-1 output no
matter how each individual seat was justified.

| starvation cause, PCR | base | sf1 |
|---|---|---|
| `release_before_t0` | 3,309 | **6,889** |
| `r5_shelf_life` | **0** | **511** |
| total | 3,309 | 7,400 |
| — of which on the **promoted** GTs | 3,015 | **7,400 (100 %)** |

A promoted campaign burns its `gap_q` of stock in the first hours, then needs fresh green
that cannot be released before `t0`. **Identical signature to `PLANNER_L5_WARM_RELEASE`**
(−1,319 PCR, "+1,400 of it on the warm GTs themselves"). Fifth member of that family after
`FLOOR_BASIS` min/slice (−2,478…−6,050), `CHG_PARALLEL` (−3,665), `WARM_RELEASE` (−1,319)
and `T0_STOCK_BASIS=lot` (−1,670).

### 4z.5 Two further readings

**An unchanged L11 pass COUNT is not an unchanged plan.** 31/48 in both arms, yet two
invariants flip: `TBR last-day GT inventory (G8)` PASS → FAIL and `PCR same-day build/cure
correlation` FAIL → PASS. Diff the status column — the same trap §4w.1 records.

**August was not run.** The ship gate is non-negative BUILT on both plants on both months;
July fails it on both plants, so August could only confirm a decision already made.

### 4z.6 Verdict

**Default stays `0`.** The experiment is kept in the code, gated off, with its numbers. The
699 tyres of expiring opening stock are real waste and this flag does collect 629 of them —
it just costs **4,936 tyres of BUILT** to do it. **Day-1 curing is not limited by the cure
seat queue; it is limited by what building can release in the first ~12 hours.** Any further
attempt at this target must move that boundary, and every attempt to move it so far has lost.
Re-measure both months if the day-1 build feed ever changes.

---

## 4aa PLANT HOLIDAYS — a closed plant-day, IMPLEMENTED and measured (2026-08-20)

Rule **G3** shipped "Blocked — calendar assumed 24×7". The engine had no notion of
time-phased availability anywhere: press downtime was a scalar haircut on the cure
*rate*, never a hole in the *calendar*. This adds one.

`planner/cmbc/holiday.py` is the single source. A holiday is a **plant-day**,
`07:00 → 07:00` — 15 August closed means `[2026-08-15 07:00, 2026-08-16 07:00)`.
Configured from `masters/holidays_<month>.json` (resolved by `planner/paths.holidays`)
or `PLANNER_HOLIDAYS=2026-08-15`, per-plant with `PCR:`/`TBR:`. **Absent file + unset
env => every function is the identity => byte-identical**, verified by diffing sha1s on
all 14 parquets of a fresh arm, twice.

### 4aa.1 Two models, deliberately, because the two resources differ

| layer | model | why |
|---|---|---|
| **L5** (presses) | **PAUSE**. `free[press]` is one instant, not an interval list, so the hole is expressed as arithmetic: `add_work` spends press-hours *through* the calendar. A campaign meeting the closure keeps its press-hours and its wall-clock end moves +24 h. | A cure campaign is 8-11 days. Pushing it wholly before or after a closure is not a schedule, it is a deletion. |
| **L7** (build machines) | **BLOCK**. The closure is booked into `busy[mach]` as a pre-booked interval with sentinel GT `__PLANT_HOLIDAY__` (`_setup_s` returns 0 for it — a shutdown is not a changeover). The existing setup-aware backward walk then pushes any straddling run to end at 07:00. | A build run is `build_band` hours. The floor does not leave half a run in a machine over a shutdown. |

**Three places had to be repaired that the two models do not cover**, and each is a
distinct class:

1. `_make_room` **rebuilds** `busy[mach]` from `placed`, which holds only real runs —
   a naive rebuild silently *deletes* the booked closure. It also compacts incumbents
   *earlier*, which is precisely how a run lands inside one. Both snap through
   `holiday.fit_before`. (Cf. §6 item 25, in mirror form: a table you write and then
   overwrite.)
2. `L10.spread()` apportioned campaign qty on the **wall clock**. Left alone it credits
   the closed day with `rate x 24` tyres *and* dilutes every other shift of that
   campaign by the same factor — the visible symptom is not the holiday, it is **the
   neighbours sagging**. The denominator is now working seconds; a closed shift is
   emitted with qty 0 so the day is present-and-zero, never missing.
3. **L7 phase 1's `t_cure`** — see 4aa.3, the one that got through.

### 4aa.2 The measurement — August 2026, fresh arms, partition `8bcb10c113bf` (2026-08)

Baseline: press availability 1.0, load/unload 2.5/2.5 (plant instruction 2026-08-19).
`HOLbase` defaults, `HOLhol` `PLANNER_HOLIDAYS=2026-08-15`. Both `check_arm_fresh` FRESH.

**`BUILT` and `BUILTinm` disagree in sign, and that is the trap.** `BUILT` (the
`arm_scorecard.py` definition) sums the whole planning window — month **plus the 72 h
`HORIZON_MODE=extend` tail** — so a plan that shifts right keeps its tyres. `BUILTinm`
restricts to slices finishing before 1 Sep 07:00. A closure moves work rightward, so
this is exactly the change for which the two separate. Quoting only `BUILT` reports a
closed plant-day as free.

| plant | arm | BUILT | dBUILT | BUILTinm | dBinm | fed inm | tail | starved | R5 max | L11 |
|---|---|---|---|---|---|---|---|---|---|---|
| PCR | HOLbase | 400,467 | +0 | 399,636 | +0 | 402,874 | 2,825 | 21,705 | 64.2 h | 28/48 |
| PCR | HOLhol | 400,674 | **+207** | 398,349 | **-1,287** | 400,668 | 5,263 | 21,483 | **69.0 h** | 29/48 |
| TBR | HOLbase | 97,741 | +0 | 97,741 | +0 | 98,480 | 462 | 1,848 | 61.2 h | 28/48 |
| TBR | HOLhol | 97,381 | **-360** | 97,239 | **-502** | 98,227 | 461 | 2,196 | **69.3 h** | 29/48 |

BUILT/demand PCR 93.9 -> 93.9 %, TBR 99.0 -> 98.6 %. BUILTinm/demand PCR 93.7 -> 93.4 %,
TBR 99.0 -> 98.5 %. fed/demand PCR 94.4 -> 93.9 %, TBR 99.7 -> 99.5 %.
GT inventory time-weighted mean / daily-mean max vs rail: PCR 3,546 -> 3,614, 4,612 ->
4,596 vs 4,800; TBR 1,142 -> 1,119, 1,320 -> 1,312 vs 1,400.
**L11 gains one invariant on each plant** (TBR same-day build/cure correlation
0.805 -> 0.939); nothing goes PASS -> FAIL. `verify_export.py`: **HARD 0, SOFT 0,
EXPORT 0**.

**Enforcement, re-derived from the parquets without importing the module:** build
slices overlapping the window 196 (411.7 machine-h) -> **0**; tyres built on plant-day
15 17,017 -> **0**; tyres cured 17,958.9 -> **0**; mould-change *work* inside the window
9 changes -> **0 minutes** (3 changes span it and pause — `span - blocked == minutes`
exactly). 147 cure campaigns span the closure and pause: that is the design.

**No dip, and it is checked.** PCR cure days 1-14 are identical *to the tyre*, day 15 is
0, days 16-31 are base's 15-30 shifted right one day — a pure translation. TBR cure is
identical on days 1-3 and higher on every other day. Interior (d2-d29, holiday excluded)
CV **falls on all four series**: PCR build 0.1241 -> 0.0784, PCR cure 0.0761 -> 0.0671,
TBR build 0.0453 -> 0.0434, TBR cure 0.0237 -> 0.0227. **The one real residual is day
14**, the shift running into the closure: PCR 12,932 built against an interior mean of
13,875 (-6.8 %), TBR 3,088 against 3,419 (-9.7 %). That is the blocking model paying for
itself — a run that cannot *finish* before 07:00 is pulled wholly before it and leaves a
sliver no legal run fits. One shift, one day, named rather than smoothed.

### 4aa.3 THE DEFECT THIS CAUGHT IN ITS OWN FIRST IMPLEMENTATION

L5 and L10 were made holiday-aware and both looked clean: `cure_by_shift` read
0 / 14,623 / 14,631 across days 15/16/17. **The exported pack did not.** Sheet
`7_daily_summary` read **5,766 cured on the closed day** and 10,094 / 13,091 on the two
after, against ~14,600 either side. The export buckets on the per-slice **`cure_ts`**,
which L7 phase 1 interpolated linearly across the campaign's **wall-clock** span — and a
paused campaign's span is 24 h longer than the press-hours it draws. So the draw was
spread into the shut window and diluted everything around it: exactly the
"neighbours sag" symptom, arriving through the one path that does not go through
`cure_by_shift`. `cure_ts` is not a reporting column — `gt_events` uses it as the -qty
instant, `_cap_ok` prices the WIP rail from it, and R5 is measured to it.

The unfixed arm scored **PCR -8,317 BUILT and L11 28 -> 27**. After the fix: **+207 /
-1,287 in-month, L11 28 -> 29.** *Almost the entire apparent cost of the holiday was my
own bug.* **The only reason it was found is that two artefacts describing the same plan
disagreed.** Do not collapse them into one view, and do not accept a layer-level artefact
as proof — take the change all the way to `verify_export.py` on an exported pack.

### 4aa.4 Why the cost is far below one day — READ BEFORE QUOTING THE NUMBER

The closure removes 13,704 PCR / 3,313 TBR tyres of scheduled output; in-month BUILT
falls 1,287 / 502, i.e. **91 % / 85 % comes back**. It comes back from exactly one place:
August's base plan already tapers over its last three days — PCR builds 9,614 across
d29-31 against an interior rate of ~13,600/day, ~31,000 tyres of idle build capacity —
and the holiday shifts work into that hole (PCR d29-31 9,614 -> 19,738, **+10,124**).
**A closure is not cheap; this month happens to have an empty shelf at the end to put the
day on.** On a month whose plan runs flat to day 31, or under a horizon ruling that
closes the box, the same closure costs close to a full day. Do not generalise 1,287.

### 4aa.5 The real cost is R5, not volume

GT wait max PCR 64.2 -> **69.0 h**, TBR 61.2 -> **69.3 h**, against a **hard 72 h**.
Green tyres age through a shutdown at the same rate as anywhere else, so the closure
spends 3.0 h / 2.7 h of the remaining shelf-life margin. **A second consecutive closed
day, or this one in a month with less slack, breaches R5 — and R5 volume is lost, not
delayed.** That is the number to watch. It is also why consecutive holidays are *merged*
into one window rather than kept as two: `add_work` steps out of one window at a time.

### 4aa.6 Known scope limits — stated, not hidden

* `plan_h` / `cap_h` / `l2_ttl.LOAD_CAP` / `PLANNER_PART_UTIL` are all fractions **of
  calendar hours** and none of them subtract a closure. They are pre-flight capacity
  *estimates*; actual placement is gated by the booked intervals, so the effect is that
  those estimates read ~3 % optimistic on a month with one holiday. Two of them live
  outside `config.py` (§8).
* The engine is still 24x7 **within** an open plant-day. Sub-day shift rosters and
  per-machine PM windows remain G3-blocked.
* L11's `same-day build/cure correlation` is computed on the **wall-clock date**, not the
  plant day, unlike everything else in the engine. A 07:00 boundary splits one wall-clock
  date across an open and a closed plant-day, and on the *unfixed* arm this alone read
  0.899 vs 0.949 on the plant-day basis. It is a pre-existing basis defect that a closure
  makes visible; **not fixed here, because fixing it moves the base arm too** and that is
  a separate measurement. Recorded so the next reader does not rediscover it.

---

## 5. FLAGS — the measured trade frontier

| flag | default | effect |
|---|---|---|
| `PLANNER_STRICT_LOT_FLOOR` | **`1`** | **zero runs below B12, plant instruction (§4m).** With §4n it costs **0–2.34 pt**, not 1.56–9.47 — 96.37/95.02/94.89/96.82 against a permissive 96.96/97.36/94.86/98.66 on the same engine. `0` restores the plant-calibrated budget and its sub-floor runs |
| `PLANNER_SLIVER_PCR` / `_TBR` | **`1.0`** | **anti-sliver packing (§4n.3).** Never leave a hole shorter than a floor-sized run. **+1.59/+3.29 pt Jul, +2.98/+2.13 Aug.** `0` disables. 1.5/2.0/3.0/6.0 all measured worse — do not tune |
| `PLANNER_L7_MAKEROOM` | **`1`** | **targeted LNS: pull blockers earlier, then insert (§4n.3).** **+0.94/+4.61 pt Jul, +2.07/+2.67 Aug.** Every constraint re-checked, full rollback on failure |
| `PLANNER_L7_MR_POINTS` | `1` | insertion points per machine for make-room. **1 is the maximum** — 6 costs Aug PCR 148 → 129 rescues (§4n.3) |
| `PLANNER_L7_DIAG` | `0` | writes `l7_place_diag.parquet`: per refused run, which gate turned it away and whether a hole existed in its R5 band. Also gates the DIAGNOSTIC-ONLY overrides `PLANNER_DIAG_SHELF_H` and `PLANNER_DIAG_PRE_H`, which do NOT produce runnable plans |
| `PLANNER_L5_TAKT` | `flat` | level-loaded press-concurrency budget on L5 (§4l.1). **+2.14 pt Jul TBR / +5.86 pt Aug TBR.** `off` restores as-early-as-possible |
| `PLANNER_L5_ALPHA` | `1.0` | front-loading allowance over the takt rate. **Interior maximum on both months** — do not tune. On PCR the response is not even monotone (1.01 → +5,169, 1.02 → +2,284, 1.05 → −617 BUILT): jitter, not an optimum (§4l.1a) |
| `PLANNER_L5_ALPHA_PCR` / `_TBR` | *(unset → `PLANNER_L5_ALPHA`)* | **added 2026-08-20.** Per-plant alpha, so one knob stops serving two plants with 3.4 % and 24 % press slack. Unset = byte-identical (control arm `TF_ctl` vs `TF_base`, all six artefacts). §4l.1a |
| `PLANNER_L5_TAKT_PLANTS` | `TBR` | PCR measured mixed-sign (−0.28 Jul / +0.18 Aug) on the old baseline. ⚠ **RE-MEASURED 2026-08-20 on BUILT: `PCR,TBR` is +4,334 PCR BUILT, starvation 12,686 → 5,129, TBR byte-identical, L11 31 → 32 (§4l.1a). July still ungated — do not ship to default** |
| `PLANNER_L5_TAKT_PART` | `1` | adds the TBR TT/TL and PCR rim partitions. `0` = plant-aggregate only, worth −1.34 pt Jul / −0.32 pt Aug TBR |
| `PLANNER_L5_TAKT_PART_PLANTS` | `PCR,TBR` | **added 2026-08-20.** Which plants get the sub-partition, so TBR can keep TT/TL while PCR runs on the plant aggregate. `TBR` + takt on PCR is **+4,985 PCR BUILT and +0.27 pt in-month**, the best August arm measured (§4l.1a). Unset = byte-identical |
| `PLANNER_ATOMIC_SPLIT_PLANTS` | `PCR` | one halving of a single-slice run, charged to the B12 budget (§4l.2). **+1.09/+1.04 pt PCR.** Adding TBR costs −2.01/−0.92 ⚠ **DEAD UNDER SHIPPED CONFIG (§4ah).** `STRICT_LOT_FLOOR=1` sets `HARD_FLOOR=True` and `ATOMIC_SPLIT_PLANTS=set()` at `l7_pull_release.py:338-339`, so this flag cannot fire. The gain quoted here was measured before `STRICT_LOT_FLOOR` shipped and **is not obtainable today** |
| `PLANNER_LOAD_TIEBREAK` | `0` | committed-hours tie-break. **Mixed sign — measured and rejected, §4l.4.** ⚠ **The numbers quoted in §4l.4/§4n.5 are STALE ON BOTH AXES** — they were measured on **August's partition** (before §4o made the staleness guard a refusal, so every "July" arm silently fell back to the dynamic assignment and lost 0.58 pt / 10.3 pt of same-size) **and on `SLIVER_TBR=1.0`**, which is not the shipped configuration. The verdict "mixed sign, do not ship" is retained because it was reproduced twice, but **the magnitudes are not usable and must be re-measured before this flag is revisited** |
| `PLANNER_RIM_PRIORITY` | **`0`** | **sequential rim campaigns (§4q).** As a candidate tie-break it is BYTE-IDENTICAL to baseline; what binds is `RIM_MAX_CONCURRENT`. Costs **−0.93 pt Jul PCR / −0.71 Aug PCR** and buys same-size **92.2 → 95.9 %** and **65.2 → 67.9 %**, rim switches 66 → 34 and 323 → 293, weighted setup −22.6 h / −15.7 h. TBR untouched, 0 L11 flips. **Consistent sign on both months — a real frontier point, and the plant chooses where to sit on it** |
| `PLANNER_RIM_MAX_CONCURRENT` | `2` | distinct rims one machine may host (its own primary counts as one). Only read when `RIM_PRIORITY=1`. `3` is the softer point: −0.30 / −0.24 pt for same-size 94.6 % / 66.5 % |
| `PLANNER_RIM_MIN_CAMPAIGN_H` | `24` | minimum hours a machine holds its open rim before another may prefer it. **Inert** — it only orders candidate lists, which are usually length 1 (§4q.3) |
| `PLANNER_RIM_ADOPT` | **`0`** | route the spill by (cheapest size change, most free hours) instead of to the plant's designated flex machine. **MEASURED AND REJECTED (§4q.6): −0.60 pt Jul PCR, −1.24 Aug PCR, and rim switches 66 → 84.** Concentrating the spill on one machine beats spreading it over cheap ones |
| `PLANNER_SISTER_GROUP` | **`0`** | **sister-SKU tie-break (§4r).** GTs differing in ONE component prefer the same machine. **+0.09 / +0.14 / 0.00 / +0.35 pt — positive or neutral on all four arms**, nothing regresses. But it does **not** move sister adjacency (43.0 → 42.4 % Jul TBR), so the gain is reshuffling, not the stated mechanism. **Inert on PCR by construction** — PCR has zero distance-1 pairs. Needs `INPUT/derived/gt_sister_group.parquet`; clean no-op without it |
| `PLANNER_SISTER_BUCKET_H` | **`0`** | bounded queue reorder: group sisters within an N-hour deadline bucket. **The only lever that moves sister adjacency** (TBR 43.0 → 49.3 % Jul, 52.9 → 57.2 % Aug) — and it costs **−0.70 pt Jul TBR / +0.08 Aug TBR**, mixed sign. `12` is worse on both axes than `4`. Rejected as a default; retained as the priced option (§4r.5) |
| `PLANNER_CLUSTER_BUCKET_H` | **`0`** | bounded queue reorder on the plant's **construction clusters** (§4s.5), same shape as `SISTER_BUCKET_H`. **PCR: reject** (−1.41 Jul / −2.07 Aug pt; nothing to group — 4 GTs / 6.9 % of July demand). **TBR: mixed sign** — Jul −0.64 to −2.05 pt at every bucket from 2 h to 48 h, Aug +0.23 to +0.52 pt. Priced at the plant's own 8.25 min/transition it buys **6.74 h of setup for 1,127 tyres** in July. Needs `INPUT/derived/sku_con_cluster.parquet`; clean no-op without it |
| `PLANNER_CLUSTER_SEQ` | **`1` (ON, both plants)** | **cluster sequencing, the re-keyed successor to `CLUSTER_BUCKET_H` (§4t).** `rc` / `PCR,TBR` / `4 h`: **+0.18 Jul TBR, +0.83 Aug TBR, −0.62 Jul PCR, −0.73 Aug PCR**, 0 L11 flips, packs clean, sub-floor 0.0 %. **Default ON by plant instruction (2026-08-10) — the PCR cost is accepted, not free.** `PLANNER_CLUSTER_SEQ_PLANTS=TBR` is the fulfilment-maximising scoping (PCR bit-identical); `0` reverts entirely. **The benefit is invisible to `weighted_setup_h` BY CONSTRUCTION** — read `cluster_adj_pct` |
| `PLANNER_CLUSTER_SEQ_KEY` | **`rc`** | `(rim, cluster)`. `mrc` adds the machine term — the literal "within the same machine" wording, and **measured worse on all four plant-months**: at B ≤ 4 only 0.7-13.2 % of (bucket, machine) cells hold >1 GT, so the cluster term is inert while the machine term still re-sorts (§4t.2). `c` = the unscoped `CLUSTER_BUCKET_H` shape, control only |
| `PLANNER_CLUSTER_SEQ_H` | **`4`** | deadline bucket bounding the reorder. **Single-peaked at 4 on both months on TBR.** 24 costs −0.55 Jul TBR |
| `PLANNER_CLUSTER_SEQ_PLANTS` | **`TBR`** | adding PCR costs −0.62 Jul / −0.73 Aug and does not even raise PCR cluster adjacency |
| `PLANNER_CLUSTER_PLANTS` | **`PCR,TBR`** | scopes `CLUSTER_BUCKET_H`. `TBR` makes PCR **bit-identical** to base — verified on every arm. Out-of-scope plants get the sentinel `"~"` INSIDE the key tuple, never a different key shape: a bare `datetime` beside a tuple would make `heapq` compare across types and raise |
| `PLANNER_L7_PINNED_FIRST` | **`0`** | **most-constrained-first in the L7 placement heap (§4w).** `B` = deadline bucket in hours; least-flexible-first inside the bucket, deadline kept below it. **July: B=8 is +651 PCR / +237 TBR BUILT with the tail UNCHANGED (10,806 / 3,540), 0 L11 status flips, R5 max 71.9 → 70.6 h** — the gain is `GT 1865 ROYL RENO` (TBMPCR3-only) going 785 unfed → **0**. Costs PCR same-size 83.7 → 82.9 pt and weighted CO 552.0 → 553.1 h. **B=24 FAILS the two-plant gate (TBR −114, PCR rail 4,821 OVER).** `0` is byte-identical to the pre-flag engine (verified, not asserted). **AUGUST GATE RUN 2026-08-19 (§4w.1): B=8 REJECTED — Aug PCR +2,403 but Aug TBR −78, a SIGN FLIP against July's +237, and it turns `TBR median GT wait vs tau*` PASS → FAIL (an invariant no July arm moved). The L11 pass count rises 28 → 29 at every bucket and HIDES that regression — diff the status column, not the count. B=24 also rejected (flips the same TBR invariant). B=4 is the only two-month non-negative bucket, but Aug TBR is +6 tyres = +0.006 pt, below the 0.05 pt noise floor: PCR-positive, TBR-NEUTRAL, a candidate not a default. DEFAULT STAYS `0`** |
| `PLANNER_L5_STOCK_FIRST` | **`0`** | **opening-stock-first in the L5 seat queue (§4z).** Promotes only the campaigns the opening stock pays a t0 seat for (`min(⌊stock/gap_q⌋, moulds, eligible presses)`), `-qty` kept everywhere else. **MEASURED AND REJECTED on July, both plants: day-1 cure +1,263 PCR / +201 TBR and opening stock left to expire 699 → 70, but BUILT −4,337 PCR / −599 TBR, day 2 and day-2-onward mean FALL on both plants, starvation PCR 3,309 → 7,400 with 100 % of it on the promoted GTs, G8 TBR daymax 1,407 over the 1,400 rail and PCR R5 max 71.9 h of 72.** Day-1 rising while BUILT falls is relocation. `0` is byte-identical to the pre-flag engine (verified against `runs/sc_pf8`, not asserted) |
| `PLANNER_L5_STOCK_URGENT` | **`0`** | **the L5 seat queue made GT-inventory-aware (§4bd).** Promotes the fewest presses that can DRINK a GT's opening stock inside its own remaining shelf life (`ceil(stock / (life_h × press rate))`, capped by R3 moulds and eligible presses), head ordered soonest-to-expire first, `-qty` untouched elsewhere. August: 24 PCR campaigns on 19 GTs / 20 TBR on 20. **It hits every stated objective — addressable expired stock 1,023 → 0 PCR and 232 → 0 TBR, consumption 3,453 → 4,476 and 794 → 1,026, both new L11 invariants FAIL → PASS, PCR starvation 12,477 → 10,755 — and the volume is NOISE.** PCR BUILT +578, **TBR BUILT −738**; the null modes `alpha` and `qty` promote the IDENTICAL set and read **−4,724** and **−2,186**, mean −1,074 sd 2,059 over five settings. Weighted setup PCR 458.6 → 496.1 h. ⚠ **AUGUST ONLY.** `0` is byte-identical to the pre-flag engine on all ten artefacts (verified, not asserted) |
| `PLANNER_L5_STOCK_URGENT_MINQ` | **`0`** | minimum usable-stock tyres for a GT to be promoted. **This is the null control, not a knob (§4bd.5):** `8` drops ONE GT holding EIGHT tyres — 0.16 % of the PCR opening floor — and PCR BUILT moves **+578 → −1,268**, flipping the sign, with L11 32 → 31; `3` drops a 3-tyre TBR GT and moves TBR in-month **+483 → −97**. Never ship a mined value in it |
| `PLANNER_L5_MONTHEND_FIT` | **`off`** | **month-end completability in L5 placement (§4aw).** `prefer` ranks "finishes before `month_end`" above `st` in the candidate key — and is **BYTE-IDENTICAL to base on all 11 artefacts at N = 5/7/10**, because `en == st + dur` with `dur` equal on every candidate press, so completability is a monotone function of the key the greedy already minimises (DO-NOT #46). `require`+`_STRICT=1` refuses what cannot fit: **PCR −9,604 BUILT / −1.28 pt, TBR −1,474 / −1.37 pt** while cutting the PCR tail 12,620 → 8,783 (DO-NOT #47). `split` cuts the campaign AT the boundary leaving the press timeline bit-identical (union press-hours 109,675.79 h both arms): **PCR −85 in-month / starvation +138, TBR +29 — mixed sign, fails the gate.** ⚠ **AUGUST ONLY, no July arm.** The premise it was built on is false — `qty_fed_in_month` PRORATES a crossing campaign (DO-NOT #48) |
| `PLANNER_L5_FEED_CEIL` | **`0`** | **GT/rim build-feed ceiling in L5 placement (§4bb).** Per-(rim, `_FEED_W_H`) cap: cure drawn on a rim <= `_FEED_SLACK` x (eligible building output + R5-usable stock), every term derived at run time from the partition+home+rim-lock set L7 enforces. **It binds (37.5 % of 72 h windows) and is NOT redundant with takt** (PCR's takt budgets `PCR ALL` only, no rim term). **Its entire effect is greedy jitter:** at fixed slack 1.25 the window sweep W=68/70/71/72/73/76 gives dBUILT +826/+841/+271/+803/+60/**−54** (mean +458, sd 414) with the starvation delta flipping sign — a 1 h change in a 72 h window swings 743 tyres. Slack is non-monotone (1.20 → +1,457, 1.22 → **−33**, 1.25 → +803). One moved seat cascaded into 33 moved campaign starts. TBR byte-identical. ⚠ **AUGUST ONLY.** The premise is wrong: the shortfall is CONTIGUITY, not volume — R13 starves 2,265 and the ceiling never binds on it (DO-NOT #49) |
| `PLANNER_L5_MONTHEND_WIN_D` | `7` | window in plant-days. Inert for `prefer`. `split` saturates at 7 (N=10 identical); N=5 is worse (PCR −716 BUILT) |
| `PLANNER_L5_MONTHEND_STRICT` | `0` | makes `require` refuse rather than fall back. `require` WITHOUT it is byte-identical to base — the fallback is the whole flag |
| `PLANNER_PART_SEED` | **`""`** | `wb` seeds `build_gt_machine_partition.py` tier 0 from the workbook's `Assigned_Machine` (§4s.6). Mixed sign: PCR −0.74 Jul / +0.24 Aug; with `PARTITION_PLANTS=PCR,TBR` TBR −1.71 Jul / +1.16 Aug. It is 93 %/84 % identical to `gt_home_machine`, i.e. near the twice-rejected pin — hence a seed guarded by the existing free-hours test, never the partition itself |
| `PLANNER_PARTITION_PLANTS` | `PCR` | `""` disables (95.8 % ful, same-size 82 %); `PCR,TBR` enables TBR |
| `PLANNER_TAU_RELEASE` | `min` | `star` restores the §1a bug. Do not. |
| `PLANNER_SUBFLOOR_PCR` / `_TBR` | `180` / `400` | plant-matched. Raising to 340/800 buys ~1 pt of demand at 23.5 % sub-floor — **over the plant's 14 %** ⚠ **DEAD UNDER SHIPPED CONFIG (§4ah).** `STRICT_LOT_FLOOR=1` sets `HARD_FLOOR=True` and `ATOMIC_SPLIT_PLANTS=set()` at `l7_pull_release.py:338-339`, so this flag cannot fire. The gain quoted here was measured before `STRICT_LOT_FLOOR` shipped and **is not obtainable today** |
| `PLANNER_HARD_FLOOR` | `budget` | `1` = absolute gate (§1b bug), `off` = no floor (fragments) |
| `PLANNER_PART_SPLIT_H` | `250` | `0` = never split big GTs → 1.02 machines/GT, *stricter than the plant's 1.40*, and −1.3 pt |
| `PLANNER_SLICE_MULT_PCR` / `_TBR` | `2.0` / `3.0` | **the run-size lever — see the frontier in §4g.** `3.5/8.0` closes all three remaining gaps and beats the plant on setup for −3.9 pt; `4.5/8.0` beats the plant on every changeover metric for −7.6 pt |
| `PLANNER_PART_UTIL` | `0.95` | lowering trades same-size back for fulfilment; 0.80 gives 88.8 % / 94.0 % — **measured on the GREEDY builder. It was INERT from 2026-08-17 (when CP-SAT became the default builder and did not read it) until 2026-08-18, when `cpsat_partition.py` was made to read it. The 0.80 figures have not been re-measured on CP-SAT.** |
| `PLANNER_LOT_INTERVAL_H` | `16` | `24` beats the plant on setup (324 vs 344 h) for −0.7 pt; does NOT move run p50 or the min_lot refusal (§4c) |
| `PLANNER_RAIL_MARGIN` | `0.94` | headroom so the stated cap survives reconciliation (§1f). `1.0` leaks 3 (PCR) / 14 (TBR) tyres over |
| `PLANNER_WIP_RAIL_PCR` / `_TBR` | `4800` / `1400` | **tighter than the plant** (its time-weighted daily max is 5,379 / 1,272) |
| `PLANNER_CAD_BASIS` | `machine` | `plant` restores the flat-cadence bug (§4j.2) for A/B. Worth 0.00 pt PCR / −0.05 pt TBR on its own; kept because capacity decisions are sized against it |
| `PLANNER_RIM_SPILL` | `1` | targeted flex-machine spill for single-machine rims (§4j.4). **+1.47 pt PCR** at same-size 96.5 → 92.7 % (plant 91.5 %). `0` disables |
| `PLANNER_SPILL_MULT` | `1.0` | budget multiplier. **Swept and saturated — not a lever** (§4j.4) |
| `PLANNER_CARRY_IN` | *(unset)* | path to a prior month's `carry_out.parquet`; loads opening PRESS state. Clean no-op when absent. **Unmeasured on July** — benefit accrues to the month after the one that emits it |
| `PLANNER_FULL_AVAILABILITY_T0` | **`0`** | audit switch for the B-ASSUME-1 ruling (§4p). **The ruling is already satisfied at default** — nothing waits at t0. `1` turns on both sub-flags below. `0` is bit-identical to the pre-flag engine (verified: 0 invariant flips) |
| `PLANNER_FULL_AVAIL_RAMP` | *(follows above)* | L5 partial-credit stock seating: `wait = (gap−stock)/rate` instead of an all-or-nothing cliff. **MIXED SIGN — not shipped.** −0.12/−0.42 Jul, +0.23/+0.72 Aug; `cold` bails rise on both months (§4p.3) |
| `PLANNER_FULL_AVAIL_LADDER` | *(follows above)* | L7 exact per-tyre shelf-life ladder replacing the MEDIAN-age screen. **REJECTED.** Stock draw is identical to the tyre in every arm — the median was never binding — and reshuffling costs −0.09/−0.13 pt PCR (§4p.3) |
| `PLANNER_B16_CRITERION` | `coverage` | `gt` restores the one-sided search (§4k). `machine` = binary dead-machine test; `coverage` adds the fair-share deficit. Worth **+16.7 pt May / +18.8 pt Aug TBR, -1.2 pt Jul** |
| `--lookahead-days` (L4) | `0` | appends N days of month M+1 demand so building has something to pull at month end (§4j.6). **No-op on July 2026: `demand_2026-08.parquet` cannot exist** — demand is derived from cured MES and MES ends 2026-07-31 |
| `PLANNER_HOLIDAYS` | *(unset)* | plant-day CLOSURES, rule G3 (§4aa). `2026-08-15`, or `PCR:2026-08-15,TBR:2026-08-16`. Falls back to `masters/holidays_<month>.json`. **Absent + unset is BYTE-IDENTICAL on all 14 arm parquets** (verified twice). Aug 2026, one closed day: in-month BUILT **PCR -1,287 / TBR -502**, L11 **28 -> 29 both plants**, verify_export 0/0/0 — but **R5 max 64.2 -> 69.0 h PCR / 61.2 -> 69.3 h TBR against a hard 72**, which is the binding cost, not volume |
| `PLANNER_L7_PRE_T0_H` | **`0`** | **bounded pre-t0 BUILDING window (§4aq)** — hours before `t0` a build run may start. Cure campaigns, `t0` and `month_end` unchanged; R5 and the rail still enforced. `0` is byte-identical on all 14 arm parquets (verified). **Aug 2026: `release_before_t0` starvation 6,810 → 305 on PCR at H=8, and TOTAL starvation falls only 1,598 because `r5_shelf_life` rises 5,044 → 9,493 — R5 was always the binding gate. August BUILT FALLS 960 / 1,063 / 3,087 at H = 4 / 8 / 12 while 1,383 / 2,732 / 3,334 tyres appear that the PREVIOUS month built.** TBR's only positive BUILT cell is H=4 (+360) and it reverses at H=8 (−427). H=4 and H=12 fail L11's weighted-changeover gate. **36 % of the H=8 machine-hours double-book a machine July's shipped plan already has running**, and the pack fails `verify_export` with a build row outside the plant month. Ships OFF — the number is for the plant, not for defaults |
| `PLANNER_RESCUE_SKIP_TIERS` | **`hard`** | **which rim-lock tiers the last-resort off-lock rescue may NOT enter (§4ar).** Comma-separated; generalises `PLANNER_RESCUE_SKIP_HARD` (which still empties the set at `0`). `flex` is exempt by design. **`hard,primary` measured Aug 2026: PCR BUILT −11,239, in-month 92.59 → 90.57, starvation 12,477 → 23,465, R5 max 63.3 → 71.9 h of 72 — while same-size RISES 69.3 → 74.7 % and weighted CO FALLS 73.6 → 66.5 min/machine-day. Both halves of the changeover hypothesis moved the predicted way and it still lost: 33 h of setup saved against ~180 machine-hours of occupancy lost (83.7 → 81.5 %).** It also fails to reach the plant's 9.8 % on `primary` (lands 16.0 %) and makes `hard` WORSE (0.5 → 0.9 %). L11 32 → 33, buying exactly one invariant. TBR aggregates identical. **Default stays `hard`** |

---

## 6. DO NOT REPEAT

1. **Do not turn a mined median into a floor.** Check what share of plant history
   the gate forbids first. Flat p01/p05/p10 = you built a wall.
2. **Do not hardcode changeover minutes, cadence, or month length.** All three
   are per-machine or per-month masters and all three caused real errors.
3. **Do not gate `runs below min_lot` at zero.** The plant is at 13 % / 31 %.
4. **Do not pin harder onto a partition you did not build.** Measured worse
   twice.
5. **Do not partition TBR** without new evidence — it has nothing to gain.
6. **Do not remove the month-staleness guard.** Falling back is correct; running
   a stale partition is a wrong answer that looks right.
7. **Do not chase the changeover *type* further.** We already beat the plant on
   cost per changeover. The gap is count, i.e. run size.
8. **Do not A/B against an older run directory.** `RunContext` hashes config but
   **not** `PLANNER_*` env flags — always run both arms fresh.
9. **Do not report or gate `e["bal"].mean()`** — that is event-weighted and not
   the inventory. Use the time-weighted mean (§1e). The TBR bias is 5.7 %.
10. **Do not "fix" the rail by changing its shape** — it is already a max-daily-mean
    check. The leak was pre-flight-vs-post-reconcile drift, handled by
    `RAIL_MARGIN` (§1f).
11. **Do not add a daily build quota (B7).** Measured and rejected — §4b. Interior
    CV is already 0.046 (PCR) and 0.059 (TBR, *better than the plant*); the whole
    headline gap is days 1/30/31.
12. **Do not raise the sub-floor budget to clear `would breach min_lot`.** The
    mechanism is correct (13.5 % vs plant 12.7 %); the refusal is invariant to T
    and to the budget. The fix is placement freedom — §4c.
13. **Do not write a cap in two places.** Every enforced limit lives in the
    marked block in `config.py`; changeover minutes and cadence come from plant
    masters. Two live duplications were found (§1g) and one of them had made an
    invariant grade itself against wrong constants for the whole session.
14. **Do not judge any change on plant-TOTAL fulfilment.** Report PCR and TBR
    separately, always. A total that moved 1.85 pt hid an 8.67 pt TBR regression
    (EXPERT_AUDIT §1).
15. **Do not assume a rule tuned on PCR transfers to TBR.** TBR has a median of
    3 eligible machines per GT against PCR's 11, 3.3x the cadence and 9 machines
    not 11. `SLICE_MULT` and `PARTITION_PLANTS` are per-plant for this reason.
16. **When you fix a measurement bug, grep for it in every file.** The
    event-weighted-mean bug lived in three and was repaired in two, twice.
17. **Do not tune the WIP cap first.** At W = 8.4 h the cap permits 571 tyres/h
   against 519 demanded; it stopped being the binding constraint once §1a was
   fixed.
18. **Do not seed an arm with `cp -r` and re-run only L7.** That inherits the
    previous arm's `l11_invariants.parquet`, which is then indistinguishable
    from a real result — it happened to **15 directories at once** (§4j.1). Use
    `scripts/run_arm.py`, which builds from L5 fresh, and gate on
    `scripts/check_arm_fresh.py` before reading any number.
19. **Do not size a capacity exception off a NOMINAL load table.** No PCR rim is
    nominally full (realised occupancy 61–83 %); the binding constraint is
    temporal fragmentation — 1,430 idle hours in 508 gaps at p50 1.72 h against
    a p50 run of 5.27 h. Sizing on load gave 22 h and +0.09 pt; sizing on
    *eligibility* (rims with one locked machine) gave **+1.47 pt** (§4j.4).
20. **Do not write a measured-looking table into a comment before running it.**
    A `SPILL_MULT` sweep table was written from reasoning and the sweep
    falsified every row of it (§4j.4).
26. **Decompose a residual before building the fix for it.** `HARD_FLOOR`
    plateaued at 3.6 % and the natural diagnosis — "L4.5/L5 emit under-floor
    lots" — was wrong: 99 % of sub-floor runs were the GROUPING REMAINDER, on a
    machine where that GT was already well above the floor (§4m.1). Building the
    assumed upstream fix would have moved nothing.
27. **Gate the resource where it is CREATED, not only where it is divided.**
    The floor was checked on `l7`'s split path and never in `_place`, so a small
    group was placed with no gate at all. Splitting harder can never fix a thing
    that was never gated.
23. **A capacity governor must be bounded by the horizon it plans into.** Under
    the closed-box rule a campaign pushed past month end is LOST VOLUME, not
    carry-out. The safe shape is: consult the governor only when the ungoverned
    placement already fits, and only let it relocate to another window that also
    fits. An unbounded version cost PCR 389,294 → 381,678 placed (§4l.1).
24. **Do not level-load by delaying long jobs.** A TBR cure campaign is 248 h at
    p50; there is no room to stagger starts. The lever is CONCURRENCY — how many
    presses may be seated at once (§4l.1).
25. **A table you load and never read is a silent wrong answer.** `mch_press`
    was loaded in `l5` and the plant MEDIAN was reserved instead, so 28 August
    events physically over-ran a still-curing press while every gate passed
    (§4l.3). Grep for the variable, not just for the constant.
22. **Feasibility checked on ONE side of a bipartite assignment is not
    feasibility.** B16 verified every GT had a machine and never verified every
    machine had a GT; one machine sat dead for a whole horizon on 2 of 4 months
    and nothing reported it (§4k). Whenever you gate an assignment, gate BOTH
    sides, and PRINT the per-resource table even when it passes.
21. **Do not quote a starvation figure without naming its run.** §4e's numbers
    were `runs/v29` and were read as current for two sessions after the baseline
    moved.
28. **`warehouse/derived/opening_gt_inventory.parquet` is a DECEMBER-31 SNAPSHOT,
    not a per-month opening balance.** The per-month master is
    `masters/opening_gt/opening_gt_<M>.parquet`. Dividing one by the other is
    what produced §4i's phantom "40 % / 54 % unused stock" and sent a whole
    session after inventory that was already 87 % / 78 % consumed (§4p.1). Third
    instance of the denominator class after §4d and §1e.
29. **"The GT holds stock" is not "stock is spare".** Before attributing unfed
    volume to withheld inventory, subtract what the plan has ALREADY drawn. On
    every GT with day-1 shortfall, both plants, both months, spare = **0**
    (§4p.1).
31. **A rim/size campaign on the BUILDING side is a shadow of the CURE
    schedule.** Every PCR rim has an active cure campaign on all 31 days with
    7–28 presses concurrently, and R5 chains a build to its cure within 72 h, so
    a machine feeding a rim must feed it every day. Sequential single-rim
    campaigns cannot be created downstream of the press plan — capping rims per
    machine only halves the switches proportionally, it does not campaign them
    (§4q).
32. **Never report a share whose denominator includes rows the metric is
    undefined for.** August PCR same-size reads 65.2 % only because 27.9 % of its
    changeovers involve a GT with no `gt_size` row; among known-rim pairs it is
    **89.9 %**. Every August same-size figure this project quoted was
    contaminated. Fourth instance of the denominator class after §1e, §4d, §4p.1
    (§4q.7).
33. **Concentrate an overflow, do not spread it.** Routing the rim spill to the
    cheapest/idlest machines instead of the plant's one designated flex machine
    lost 0.60/1.24 pt AND raised rim switches 66 → 84 — it contaminates a machine
    that was pure. Second time the plant's revealed eligibility structure beat
    our cost/load table (§4q.6, §4j.4).
34. **Check the COST MASTER can express a preference before optimising for it.**
    `cap_changeover` is keyed on (machine x same/different size) only, so the
    engine charges a sister transition and a non-sister same-rim one identically
    — 10 min, where the plant's realised gaps are 5.2 and 12.1 min. A sister
    benefit is invisible in `weighted_setup_h` by construction (§4r.4).
35. **A tie-break below `HARD_PIN` is a no-op.** `PIN_RUNS` breaks on the first
    feasible machine and the partition gives most GTs one, so the candidate list
    is usually length 1. `RIM_PRIORITY` as a tie-break measured BYTE-IDENTICAL to
    baseline. Ordering a list of one orders nothing — gate the resource where it
    is created (§4q.3, and DO-NOT #27 one layer up).
36. **A bounded reorder needs BUCKET DENSITY, so measure it before adding a
    scoping term.** Keying the cluster grouping "within the same machine" left
    only 0.7 % (B=2) / 13.2 % (B=4) of (bucket, machine) cells holding more than
    one GT — the grouping term could not fire, while the scoping term re-sorted
    the bucket anyway. All of the disturbance, none of the mechanism. Drop the
    term to `(rim, cluster)` and it works; the cluster already implies its rim
    and mostly its machine (§4t.2). Same family as DO-NOT #35 — *ordering a list
    of one orders nothing*, one layer up.
30. **Verify a proposed gate is BINDING before building the exact version of
    it.** The `opening_life` median screen is a textbook §1 defect — and
    replacing it with an exact per-tyre ladder moved the stock draw by **zero
    tyres** in all four plant-months, because the screen never bound. Measure
    the counterfactual draw first; a wrong-shaped rule that never fires costs
    nothing to leave alone (§4p.3).
37. **"Every campaign I moved was already legal" does not make a change safe.**
    `STOCK_FIRST` promoted only campaigns `earliest_cure` would itself have
    seated at `t0`, so per campaign nothing was released early — and BUILT still
    fell 4,337 / 599 because the `t0` window is a **shared build-feed resource**
    and seating more campaigns in it oversubscribes day-1 building output.
    100 % of the extra starvation landed on the promoted GTs themselves.
    Per-item legality is not an aggregate feasibility argument; the shared
    resource is (§4z.4). Fifth loss in the early-release family.
38. **Day-1 (or any single-day) cure is not an objective you can optimise
    directly.** Every lever that raises it moves press seats earlier, and press
    seats earlier means less build lead time, which costs more than the day
    gains: `STOCK_FIRST` +1,263 day-1 PCR for −4,337 BUILT (§4z), `EDD` (§4v),
    `BACKLOAD`, `WARM_RELEASE`, `CHG_PARALLEL`, `FLOOR_BASIS`. **The binding
    constraint on day 1 is what building can release in the first ~12 h, not
    which campaign holds the press.** Fix that boundary or do not fix this.

39. **Do not accept a layer artefact as proof that a change worked — take it to
    the exported pack.** The holiday feature was clean in `cure_by_shift`
    (0 / 14,623 / 14,631 across the closure) while the exported
    `7_daily_summary` read **5,766 tyres cured on a shut plant-day** and sagged
    the two days after it. The two views come from different columns — L10's
    own spread vs the per-slice `cure_ts` — and only their DISAGREEMENT found
    it (§4aa.3). Two views of one plan is a feature; collapsing them is how a
    defect ships.

40. **A duration is not a span once the calendar has holes in it.** Anything of
    the form `start + hours` or `qty * hours/total` is wall-clock arithmetic and
    silently credits closed time with output. The three that bit: L5's campaign
    end, L10's `spread()` denominator, and L7 phase 1's `t_cure` interpolation.
    `planner/cmbc/holiday.py` is the only place this arithmetic lives — do not
    re-derive it at a call site. Note that **R5 is the exception and must stay
    wall-clock**: a green tyre ages through a shutdown (§4aa.5).

42. **Setup hours freed on a machine a run may no longer use are not capacity.**
    `RESCUE_SKIP_TIERS=hard,primary` did exactly what its hypothesis predicted --
    same-size +5.4 pt, weighted changeover −7.1 min/machine-day, 32.6 h of setup
    genuinely saved -- and lost 11,239 tyres, because machine OCCUPANCY fell
    83.7 → 81.5 %, about 180 machine-hours. The refused runs did not become
    cheaper work elsewhere; they became no work at all. **Report occupancy beside
    any changeover claim, or the claim is untestable** (§4ar.2).

43. **Relieving a starvation CAUSE is not producing a tyre — check where the
    refusal moves to.** The pre-t0 window took `release_before_t0` from 6,810 to
    305 on August PCR and total starvation fell only 1,598, because
    `r5_shelf_life` rose 5,044 → 9,493. A cause label names the FIRST gate that
    said no, not the binding one. Diff the whole cause vector, never one row of
    it (§4aq.3).

44. **A month total of free machine-hours is not availability.** July's last 8 h
    hold 48.1 free PCR machine-hours against a 52.7 h claim, which reads as
    ample -- and 18.95 h of the claim lands on machines July is running, because
    TBMPCR9/10 are 100 % busy while TBMPCR2/4/6/7 are idle. Check the resource
    per HOLDER before sizing anything against it (§4aq.5; same shape as #19,
    #22, and §4aj's idle-hours-vs-unmet-demand error).

45. **Building earlier is inventory, and this plan has no rail headroom to spend
    on it.** Every hour the pre-t0 window opened raised `gt_wip_rail` refusals
    (623 → 1,081 at H=8, → 1,546 at H=4) against a PCR daily-mean max of 4,522
    sitting on a 4,512 enforcement point. Any lever that moves builds earlier
    must be priced against the rail first (§4aq.4). Sixth loss in the
    early-release family after §4an's list.

41. **A blocked interval you write into a shared structure can be erased by a
    consumer that rebuilds it.** `_make_room` reconstructs `busy[mach]` from
    `placed`, which holds only real runs, so a booked closure vanishes and every
    later `_place` on that machine is free to schedule into a shut plant. Grep
    for every ASSIGNMENT to the structure, not just the reads (§4aa.1).

46. **A preference that is a MONOTONE FUNCTION of the key you already sort on is
    not a preference.** A month-end completability term (`_fits`) was ranked
    ABOVE `st` in L5's candidate key and changed nothing — `en == st + dur` with
    `dur` identical on every candidate press, so "finishes in the month" is
    implied by "starts earliest", which the greedy already minimises.
    Byte-identical to base on all 11 artefacts at three window widths. Same
    family as #35 (*ordering a list of one orders nothing*) one level up:
    **before adding a term to a greedy key, prove it is not implied by the terms
    already there** (§4aw.2).

47. **Do not grade a month-boundary change on the CARRY-OUT TAIL.** Tail tyres
    are next month's opening stock, not losses. The arm that cut the PCR tail
    hardest (12,620 → 8,783) is the arm that destroyed 9,604 BUILT tyres. The
    only way to shrink the tail without producing less is an earlier seat, which
    the greedy already takes (§4aw.5).

48. **`qty_fed_in_month` PRORATES a boundary-crossing campaign; it does not zero
    it.** Only campaigns that START after `month_end` contribute nothing — 2 of
    57 on August PCR. Any proposal premised on "a crossing campaign is lost"
    is premised on a bug that does not exist (§4aw.1).

49. **Run a NULL CONTROL before believing a scheduler gain.** Perturb a
    parameter that CANNOT physically matter — a 1 h change in a 72 h window,
    against a 72 h shelf life — and measure the spread. The build-feed ceiling
    read +803 BUILT at W=72; its neighbours read +271 (W=71) and +60 (W=73),
    mean +458 sd 414 across six equivalent settings, with the starvation delta
    flipping sign. The effect was the greedy re-settling after ONE moved seat
    (33 campaign starts moved), not the mechanism. **Three perturbed baselines
    all agreeing is NOT this test** — they resample the same jitter point
    (§4bb.3).

50. **An opening-stock recovery metric is not a volume metric.** Four arms of
    `PLANNER_L5_STOCK_URGENT` drove addressable expired opening stock from
    1,023 PCR / 232 TBR to **0 on both plants**, and three of them destroyed
    1,268–4,724 BUILT doing it. Collecting a perishing tyre and producing a
    tyre are different events; grade the second. And before costing unused
    opening stock as an opportunity, **subtract the stock sitting on GTs the
    month does not cure at all** — 656 of 1,679 PCR and 240 of 472 TBR on
    August. No scheduler can reach that 40 %; it is an order-book fact (§4bd.1).

51. **A stock-sized "early slice" is a sub-floor campaign by construction.**
    Ten of the eleven GTs holding addressable expiring stock on August hold
    LESS than the B12 cure-lot floor (PCR 311.5 / TBR 85.7), so seating a slice
    sized to the stock would break the cap it is trying to work around. Check
    the floor against the quantity BEFORE designing anything that splits a lot
    to fit an inventory position (§4bd.2).

53. **A gate that iterates a reservation map is blind to every row that never
    entered it — and BOTH of L7's feasibility gates were.** §4av named the rule
    ("any path that appends to `bs` must also write `busy`") and it took a second
    session to obey it. The closing buffer emitted 15 scheduled runs into `bs`
    and none into `busy`, so `machine double-booking` and `setup not reserved`
    printed PASS on a plan the independent verifier called NOT physically
    executable. **Grep for every structure the GATES read, not only the ones the
    PLANNER reads** (§4bf.4).

54. **"Production fits the day" is not "the day fits".** L7 packs machine-days
    to **exactly 24.00 h of production** — 0 of 617 August machine-days exceed
    24 h on production alone, and 3 breach once setup is added on top. That
    24.00 h maximum is the tell: a budget was written against the wrong
    quantity. Reserving the changeover IS the machine-day budget — once every
    production and every setup interval on a machine is disjoint, the 24 h
    limit holds by construction, and a separate day-cap would be §1g's
    duplicated-cap defect (§4bf.2).

55. **Run the null control against the OBJECTIVE, not only against BUILT.**
    `R5_FIRST_TYRE` drove tyres-past-shelf-life 26 → 0 and L11 31 → 33 on August
    TBR — and perturbing an unrelated release grid by **6 minutes** did the same
    thing in three of four physically-equivalent arms, one of them scoring the
    identical 33/52 with the identical two invariants flipping. If a physically
    meaningless change reaches your success criterion, the criterion is measuring
    the greedy, not the mechanism. Extends #49, which only ever perturbed the
    volume (§4bg.3).

52. **Press-calendar geometry is a per-MONTH fact — re-measure it on the month
    you are planning.** July had 10 of 86 PCR presses completely free for the
    whole first 72 h; August has **zero** free for the eligible GTs and a
    largest contiguous window of 7.4 h against a 57.1 h need, because August
    PCR runs 94.5 % occupied. A repair pass sized on July's calendar would have
    been built and would have found nothing (§4bd.2).

---

## 7. REPRODUCE

```bash
python scripts/build_gt_machine_partition.py 2026-07
rm -rf runs/v28 && cp -r runs/v8 runs/v28          # L1-L6 artifacts
python -m planner.cmbc.l7_pull_release   --month 2026-07 --run v28
python -m planner.cmbc.l8_prep_explosion --month 2026-07 --run v28
python -m planner.cmbc.l10_discretise    --month 2026-07 --run v28
python -m planner.cmbc.l11_validate_plan --month 2026-07 --run v28
python scripts/rulebook_scorecard.py runs/v28 2026-07
```

Expected: **fulfilment 95.4 %**, 19/34 invariants, PCR inventory 4,073 mean /
**4,778 daily max** against the 4,800 rail, TBR 1,037 / **1,363** against 1,400 --
both inside, no `RAIL/LEDGER MISMATCH` line, and **zero `would breach min_lot`
starvation**. R5 max within 72 h.

Several failures are the deliberate cost of honouring the caps and are documented
where they arise -- TBR changeovers/machine-day and sub-floor (§1f), PCR median GT
wait and build/cure correlation (§4c). Read the named section before treating any
of them as a regression. GT inventory failing *below* the G8 band is also
intentional: we sit under the plant.

Several L11 failures are **deliberate**: GT inventory is *below* the G8 band
(better than the plant), and the campaign-length bands are known open gaps. Check
the specific lines before treating a failure as a regression.

---

## 8. SINGLE SOURCE OF TRUTH — where every cap lives

**Add a cap in `config.py` or nowhere.** `Thresholds` has a marked
`SINGLE SOURCE OF TRUTH FOR THE ENFORCED CAPS` block; `l7_pull_release` and
`l11_validate_plan` both read it and neither keeps a copy.

| cap | defined in | value | read by |
|---|---|---|---|
| **lot floor (B12)** | `config.min_lot_units` | PCR 150 / TBR 70 | l7, l11, l45 |
| min demand to plan | `config.min_demand_units` | **PCR 300 / TBR 150** | l4 |
| B16 group load cap | `l2_ttl.LOAD_CAP` **(NOT in config.py)** | 0.95 of **calendar** hours | l2_ttl |
| partition machine util | `cpsat_partition.UTIL` / `PLANNER_PART_UTIL` **(NOT in config.py)** | 0.95 of calendar hours | partition builder |
| partition imbalance cap | `cpsat_partition.IMB_CAP_H` **(NOT in config.py)** | 8 h **within a B16 group** — MEASURED BINDING on 2 of 4 (plant, month) cells | partition builder |
| **GT stock rail (enforced)** | `config.gt_wip_rail` | **PCR 4800 / TBR 1400** | l7 rail, l11 |
| rail margin | `config.gt_wip_rail_margin` | **0.94** | l7 `_cap_ok` |
| G8 band (reported, wider) | `config.gt_wip_min/max` | 4500–4800 / 1200–1500 | l11 |
| **R5 shelf life** | `config.gt_shelf_life_h` | 72 h, *not env-overridable* | l7, l11 |
| daily cure rate cap | `config.cure_day_cap` | **PCR 0 / TBR 0 = OFF** — gated on `PLANNER_L5_DAY_CAP=1`; measured and REJECTED at 13,000/3,200, §4y | l5 governor only |
| plant CO rate benchmark | `config.plant_co_per_machine_day` | 2.66 / 3.56 | l11 gate |
| plant weighted CO benchmark | `config.plant_weighted_co_min_per_machine_day` | **74.0 / 35.6** | l11 gate |
| **changeover MINUTES** | `v_changeover_build` **(plant master, never hardcoded)** | PCR 28/60 m1-5, 22/42 m6-11 · TBR 10/24 | l11, scorecard, partition builder |
| per-machine cadence | `cycle_time_building.parquet` | PCR 49–78 s, TBR 189–219 s | partition builder |
| τ\* and τ_min | `warehouse/params/params_*.json` | 4.32/4.81 and 0.268 h | l7, l11 |

**Shape knobs stay in `l7_pull_release.py`** — they are algorithm parameters, not
plant limits, and each carries its measured trade in a comment beside it:
`SLICE_MULT`, `LOT_INTERVAL_H`, `SUBFLOOR_BUDGET`, `HARD_FLOOR`, `HARD_LOCK`,
`HARD_PIN`, `PARTITION_PLANTS`, `EARLY_CAP_H`, `SPAN_MULT`, `RUN_MULT`,
`MACH_UTIL_CAP`, `TAU_RELEASE`, `BUFFER_SETUP`, `R5_FIRST_TYRE`.

**THE 24 h MACHINE-DAY IS NOT A CAP AND MUST NOT BECOME ONE.** It is implied by
reserving every changeover: disjoint production and setup intervals inside a 24 h
window cannot sum to more than 24 h. `PLANNER_L7_BUFFER_SETUP=1` is the whole
enforcement, and writing a day budget beside it would be the §1g duplicated-constant
defect. Measured 2026-08-21, §4bf.

**Rule for the next change:** a number that describes the PLANT goes in
`config.py` (or is read from a master). A number that describes OUR ALGORITHM
goes in `l7` beside its measurement. Nothing goes in two places.

---

---

## 4ab. L4.5 LOT SIZING — three defects in one function, all shipping (2026-08-20)

`planner/cmbc/l45_lotsize.py` carried three separate bugs between the cure-hours
shape split and the row it writes. All three were live on every plan this project
has ever shipped, and **L11 could not see any of them** — it gates R5 on the
build→cure wait, never on campaign length against `max_lot`.

### 4ab.1 `lot_list` was never rebuilt when `n` moved

`lot_list` is built from the decile shape, then `n` is recomputed TWICE below it
— `"consolidated to floor"` and, the one that matters, `"split at 72 h ceiling"`.
Neither rebuilt the list. The row then wrote `n_lots = n` (new) beside
`lot_sizes = lot_list` (stale), and `l5_cure_master` **prefers `lot_sizes`**
(l5:1156-1160). So the R5 shelf-life split was computed, written to `n_lots`, and
silently discarded by the only layer that reads it.

### 4ab.2 The R5 ceiling was tested on the AVERAGE lot, not the largest

`if qty > mx` compares `need / n` — the MEAN lot — against
`mx = moulds × rate × GT_SHELF_LIFE_H`. But the decile shape deliberately makes
lots UNEVEN, so a GT passes on its average and still carries a tail lot far above
the ceiling. Nothing downstream re-checks.

> `GT 1482 UHL`, July: need 20,690, `n_lots` 12, mean 1,724 against `max_lot`
> 3,344 — passes. The shape emits a **4,101-tyre lot**. 1.23× over.
> `GT 5101 - 11.00R20 JS114`: `max_lot` 137.7, `lot_sizes` `[696.0]` — **5.05×**.

A lot above `mx` cannot be cured inside 72 h by construction, so this shipped
plans whose green tyres expire mid-campaign.

### 4ab.3 `qty = round(need / n)` — the last round-UP, and a G1 breach

`integer_split` exists to remove exactly this (its own docstring records the
cavity-multiple round-up that "pushed 59 GTs past their requirement by 707
tyres, breaking G1"). It never went away — it moved to this line. When
`lot_list` is None, L5 expands `[lot_qty] * n_lots`, and `round(need/n) * n` can
exceed `need` by up to n/2 tyres.

### The measurement

Over-ceiling campaigns (`lot_sizes > max_lot`), both months:

| state | Jul | Aug |
|---|---|---|
| git HEAD (shipped) | **36 rows / 72,175 tyres** | **33 rows / 74,392 tyres** |
| + 4ab.1 rebuild | 19 / 49,924 | 21 / 60,101 |
| + 4ab.2 max-check | **0 / 0** | **0 / 0** |

Production beyond requirement, measured on the 4ab.1+4ab.2 arms:

| | over-CURED | over-BUILT |
|---|---|---|
| Jul PCR | 1,153 (40 of 48 GTs) | 545 |
| Jul TBR | 1,759 (41 of 56) | 399 |
| Aug PCR | 1,141 (47 of 73) | 327 |
| Aug TBR | **1,730 (31 of 37 = 84 %)** | 98 |
| total | **5,783** | **1,369** |

Self-defeating: the same plan starves 18,311 tyres on August PCR while spending
press-hours on 5,783 nobody ordered.

### Plan effect of 4ab.1 + 4ab.2 (fresh arms, `run_arm.py`, both months)

| | BUILT | starved | first-5 dip | **last-5 dip** | R5 max | L11 |
|---|---|---|---|---|---|---|
| Jul PCR | −384 | 17,110 → 17,621 | 6,154 → 5,800 | 52,362 → **48,280** | 65.4 → **56.9 h** | 29 → **30** |
| Jul TBR | −11 | 2,197 → **1,486** | 981 → 820 | 3,971 → 3,710 | 67.8 → **62.3 h** | 29 → **30** |
| Aug PCR | **+439** | 21,059 → **18,311** | 6,675 → 5,978 | 27,401 → **16,857** | 54.3 → 62.7 h | 30 → **31** |
| Aug TBR | −147 | 1,809 → 1,654 | 894 → 906 | 3,230 → **1,988** | 64.9 → **71.7 h** | 30 → **31** |

**BUILT is net −103 across both months — noise.** That is the point: a
correctness fix that costs nothing. What it buys is a legal plan (0 R5-breaching
campaigns), the month-end dip down **38 % on August**, starvation down on three
of four cells, and **+1 L11 invariant on both months**.

⚠ **August TBR R5 reaches 71.7 h against the hard 72.** Watch it before shipping;
the ceiling may need to be tighter on TBR than `moulds × rate × 72`.

### Rules this establishes

- **Never test a ceiling on a mean when the distribution is deliberately skewed.**
  4ab.2 is §1's signature class in a new place: the shape exists to make lots
  uneven, and the guard reads the average.
- **A recomputed `n` must rebuild everything derived from it.** Writing `n_lots`
  and `lot_sizes` from different generations of the same variable is how a
  computed R5 split reaches disk and is then ignored.
- **L11 cannot gate what it does not measure.** R5 was checked on build→cure wait
  and never on campaign length against `max_lot`, so 146,567 tyres of illegal
  campaigns passed every gate for the life of the project. Add the campaign-length
  check before trusting R5 again.

### 4ab.4 The G1 "over-production" claim was my own measurement defect — SHIPS OFF

A third change was written on top of 4ab.1+4ab.2 to make `sum(lot_sizes) == need`
exactly, on the grounds that the plan over-produced 5,783 cured and 1,369 built
tyres beyond requirement.

**That number was wrong, and wrong in a way this ledger already names.** It sums
only the GTs where `cured > demand` and discards every GT that came in UNDER.
With rounding in both directions such a sum is positive by construction. The net
counts:

| | net cured − demand | net built − gross_build |
|---|---|---|
| Jul PCR | **−214** | **−20,717** |
| Jul TBR | +262 | −2,948 |
| Aug PCR | **−6,566** | **−22,367** |
| Aug TBR | +1,077 | −2,247 |

The plan **under**-produces on every cell but TBR cure, and TBR's surplus is the
cure-yield allowance (0.98202 ⇒ ~1.8 % scrap cover), not a leak. There was no G1
breach to fix.

This is the fourth member of the "sum only the positive deviations" family, after
mean-over-events (§1) and the fed-vs-BUILT trap (§4o). **Before reporting a total,
state whether it is a net or a one-sided sum.**

Measured anyway, fresh arms, against the 4ab.1+4ab.2 arms:

| | BUILT | starved | R5 max | L11 |
|---|---|---|---|---|
| Jul PCR | **−2,567** | 17,621 → 20,194 | 56.9 → 56.8 h | 30 → **33** |
| Jul TBR | −343 | 1,486 → 1,830 | 62.3 → **69.1 h** | 30 → **33** |
| Aug PCR | **+2,565** | 18,311 → **15,744** | 62.7 → **55.4 h** | 31 → 31 |
| Aug TBR | −69 | 1,654 → 1,725 | 71.7 → 71.6 h | 31 → 31 |

Mixed sign on BUILT across months ⇒ fails the two-month gate. **Ships OFF behind
`PLANNER_L45_EXACT_SPLIT=1`.**

⚠ The July **L11 30 → 33** and the August **starvation −2,567 / R5 −7.3 h** are
real and unexplained. If exact splitting is ever revisited, start from those two
results — not from an over-production claim that does not survive a net count.

---

## 4ac. CLOSING GT BUFFER — the first clean two-month, two-plant winner (2026-08-20)

`PLANNER_L7_CLOSING_BUFFER=1`. Existing code, shipped OFF, comment said "Default OFF
until measured." Measured now, fresh arms via `run_arm.py`, both months, both plants,
against the 4ab.1+4ab.2 baseline:

| | BUILT | **closing GT** | build last-5 dip | in-month | starved | R5 | L11 |
|---|---|---|---|---|---|---|---|
| Jul PCR | **+3,923** | **156 → 4,079** | 55,318 → 51,395 | **unchanged** | unchanged | unchanged | unchanged |
| Jul TBR | **+319** | 229 → 548 | 6,105 → 5,786 | unchanged | unchanged | unchanged | unchanged |
| Aug PCR | **+3,524** | **510 → 4,034** | 27,409 → 23,885 | **unchanged** | unchanged | unchanged | unchanged |
| Aug TBR | **+357** | 436 → 793 | 4,508 → 4,151 | unchanged | unchanged | unchanged | unchanged |

**+8,123 BUILT at literally zero cost.** Every one of the four guarantees in the block's
own docstring is confirmed by measurement: in-month untouched, starvation identical, R5
identical, L11 identical. It fills only genuinely idle machine time in the last 72 h.

August PCR day-31 build **3,220 → 6,577**; July PCR **762 → 4,410**. Days 1-26 are not
touched.

This also repairs the hand-off chain the block was written for: every month was consuming
opening stock it never replaced (Jul inherited 4,820 and handed over 156). PCR now hands
forward ~4,050 against a ~4,800 opening — the plant's own steady state. TBR reaches only
548/793 because its machines are less idle at month end.

**Report it on the BUILD line and the hand-off, never as fulfilment** — those tyres cure
next month, which is why in-month is unchanged by construction.

---

## 4ad. REMOVING tau* — day 1 is fixed, the month is worse. SIXTH failure of this family (2026-08-20)

`PLANNER_L5_FLOOR_BASIS=slice` — release at `tau_min` (16 min) instead of
`tau* + first-slice build` (259 min + slice). Run with `CLOSING_BUFFER=1` on the current
baseline, both months.

**Day 1 does exactly what the physics says it should:**

| | day-1 cure | day-1 press util | presses at t0 |
|---|---|---|---|
| Jul PCR | 8,782 → **13,519** | 59 % → **89 %** | 25 → **52** |
| Aug PCR | 7,902 → **12,961** | 55 % → **88 %** | 20 → **47** |

**And the month is worse on all four cells:**

| | BUILT | in-month | starved |
|---|---|---|---|
| Jul PCR | **−3,885** | 95.07 → 94.14 % | 17,621 → **21,506** |
| Jul TBR | **−1,787** | 97.87 → 95.60 % | 1,486 → **2,938** |
| Aug PCR | **−7,088** | 92.18 → 90.71 % | 18,311 → **26,183** |
| Aug TBR | **−1,501** | 97.89 → 96.42 % | 1,654 → **3,069** |

### The decomposition — this is the part to keep

August PCR, day by day against the same baseline:

| window | cure diff | build diff |
|---|---|---|
| **d1** | **+5,060** | −257 |
| d2–7 | **0** | −1,915 |
| **d8–31** | **−3,473** | −6,000+ |

Day 1 already built 13,783 and cured 7,902 — a **standing surplus of 5,881 on the floor**.
The early release let the presses eat it. **No tyre was created.** d2–7 cure is diff **zero**
because presses are at 100 % there either way; only build falls, because 47 presses now
need continuous per-GT feed from 11 machines with **32.6 % of PCR volume locked to 1–2
machines**. From d8 the build loss turns into cure loss and compounds.

**tau\* was never protecting the press. It was masking building's narrow per-GT capacity.**
Removing it does not add capacity — it exposes the real constraint sooner.

### The family, now six deep

`WARM_RELEASE` −1,319 · `FLOOR_BASIS min/slice` −2,478…−6,050 · `T0_STOCK_BASIS lot` −1,670
· `STOCK_FIRST` −4,936 · `CHG_PARALLEL` −3,665 · `slice` on the current baseline −7,088.

**Rule: day-1 cure is the easiest number in this plan to improve and the most misleading.
Grade on BUILT across the whole month.** Day-1 cure cannot rise sustainably until
`allowed_machine_matrix` widens — a PLANT RULING, since MES shows the plant itself uses
5–11 machines per GT where the matrix permits 1–2.

---

## 4ae. PINNED-FIRST HEAP KEY — the second clean two-month winner, PCR-only (2026-08-20)

`PLANNER_L7_PINNED_FIRST=8` (h), scoped by `PLANNER_L7_PINNED_FIRST_PLANTS=PCR`.
**SHIPPED as default.**

### What it does

L7 releases build slices off one global heap. The shipped key ordered purely by
release time. A GT that `allowed_machine_matrix` permits on **1–2 machines** competes
for those machines against GTs that could have gone anywhere, and loses whenever a
wide GT happens to be a few minutes earlier. The narrow GT then misses its cure
campaign and the whole campaign starves.

The new key buckets the first `PINNED_FIRST` hours of each machine's window and
lets pinned (narrow-reach) GTs claim inside the bucket first. It reorders **who
gets the scarce machine**, not how much is built.

### Measured — fresh arms, `run_arm.py`, per-month partition, both plants

| | Jul PCR | Aug PCR | Jul TBR | Aug TBR |
|---|---|---|---|---|
| BUILT | 380,536 → **386,595** (**+6,059**) | 404,342 → **409,967** (**+5,625**) | +0 | +0 |
| in-month | 95.07 → **96.59 %** (+1.52 pt) | 92.18 → **93.49 %** (+1.31 pt) | 97.87 % flat | 97.89 % flat |
| starved | 17,621 → **11,562** | 18,311 → **12,686** | 1,486 flat | 1,654 flat |
| L11 PASS | 30 → 30 | 31 → 31 | — | — |

**+11,684 tyres across the two months. No KPI regressed on either plant.**

### The mechanism, proven not assumed

Starvation decomposed by the GT's machine count in `allowed_machine_matrix`:

| | base | PINNED |
|---|---|---|
| starved tyres on GTs with ≤2 machines | 14,389 of 18,311 (**79 %**) | 5,942 of 12,686 (**47 %**) |
| `r5_shelf_life` starvation | 4,536 | **2,060** |

This is the same root cause as the six failed early-release experiments (§4ad):
**building's narrow per-GT reach.** Those tried to relieve it by releasing
earlier, which only exposed it sooner. This one relieves it by **allocating the
scarce machine to the GT that has no alternative** — the first experiment in the
family that addresses the constraint instead of the symptom.

### TBR is excluded, deliberately, on measurement

Unscoped it cost TBR **−335 (Jul) / −122 (Aug)** BUILT. TBR has 80 presses against
far fewer building machines; its binding constraint is press-side, so reordering
the build heap only perturbs a schedule that was already tight. Per the
**always-report-both-plants** rule the plant-total (+5,724 Aug) would have hidden it.

### The bug this shipped with, and the shape rule it leaves behind

First ship attempt aborted **both** arms:

```
TypeError: '<' not supported between instances of 'str' and 'int'
  l7_pull_release.py:3207  heapq.heappush(...)
```

Both plants share **one heap**. The plant scope returned a 3-tuple for PCR and a
bare `k` for TBR, so `heapq` compared a bucket int against a machine-name str.

> **RULE: a plant-scoped heap key must keep the same tuple SHAPE for every plant.**
> Excluded plants get a constant prefix — `return (0, 0, k)` — which leaves their
> ordering decided entirely by `k`, i.e. byte-identical to the shipped key, while
> the scoped plant gets the real buckets. Never `return k` bare from one branch.

`PINNED_FIRST_H <= 0.0` still returns bare `k` — that path disables the feature
for **both** plants at once, so the shapes cannot diverge.

---

## 4af. PLANT-TOTAL FULFILMENT — the one line the 2026-08-14 denominator fix missed

**Found by `schedule-forensics`, 2026-08-20. FIXED.** `l11_validate_plan.py`.

The 2026-08-14 fix corrected the fulfilment ratio to divide an
opening-stock-INCLUSIVE numerator by an opening-stock-INCLUSIVE denominator
(`cure_requirement`, less look-ahead). It was applied to the two **per-plant**
lines and not to the **plant TOTAL** twelve lines below, which kept dividing by
`gross_build` (= requirement **minus** opening stock) and skipped the `_la`
subtraction.

**The proof needs no arithmetic.** `runs/BASE_jul/l11_invariants.parquet`:

| invariant | printed |
|---|---|
| PCR demand fulfilment | 94.8 % |
| TBR demand fulfilment | 96.1 % |
| demand fulfilment (**plant TOTAL**) | **96.2 %** |

**A weighted mean cannot exceed both of its components.** Orphaned opening-stock
term 6,071 tyres; the total was overstated **+1.17 pp** on every arm this project
has ever scored.

**Fix:** the total is now literally `sum(per-plant numerators) / sum(per-plant
denominators)` — accumulated inside the same loop that prints the per-plant lines,
so it is bounded by them **by construction** and cannot drift again. Verified:
Jul 96.3 % total between 96.3 / 96.1; Aug 94.0 % between 93.5 / 96.1.

> **This is the fifth denominator defect found in this project** (§1). The pattern
> is now unmistakable: *the fix was applied where the bug was noticed, not
> everywhere the expression appears.* Same shape as the L7/L11 duplicated rail
> value and the `press_availability` second reader. **When you fix a ratio, grep
> the file for every other site that computes it.**

---

## 4ag. GT-WAIT p95 GRADED THE WRONG POPULATION — and it flipped a gate

**Found by `schedule-forensics`, 2026-08-20. FIXED.** `l11_validate_plan.py`.

`{plant} GT wait p95` was computed over **all** `build_schedule` rows, including
the `OPENING_STOCK` pseudo-rows. Those carry `start_ts == end_ts == t0`, so their
`wait_h` measures from the horizon start, not from when the tyre was actually
built last month. 35 PCR / 32 TBR rows, 3,951 / 855 tyres.

The comment explaining exactly this artifact **already sat 20 lines below**, on
the R17 check, which filters them out. The p95 check did not.

| | as coded | released rows only |
|---|---|---|
| Jul TBR GT wait p95 | **28.20 h → FAIL** (≤ 28 h) | **27.90 h → PASS** |

The shipped pack has been contradicting itself for the whole project:
`verify_export.py` re-derives the same statistic from sheet 1 (which has no
`OPENING_STOCK` rows) and prints **27.8 h** while sheet 9b prints **28.2 h**.
Nobody reconciled the two.

`R5 max` still uses all rows — correct, a tyre carried in from last month really
is ageing. Only the **release** statistic changed population.

After the fix August TBR reads **28.9 h FAIL** — a real breach on the correct
population that the wrong population had been masking in the other direction.

> **RULE: `OPENING_STOCK` rows are not releases.** Any statistic that describes
> *when we let a tyre go* must exclude them; any statistic that describes *how old
> a tyre is* must include them. Decide which one you are computing before you
> write `bs["wait_h"]`.

---

## 4ah. DEAD LEVERS — flags that ship ON and cannot change a single byte

**Found by `schedule-forensics`, 2026-08-20, each one A/B'd to byte-identical
artefacts.** Not fixed — recorded, because turning any of them on is a scheduling
change that must be measured on its own, not smuggled in as a "bug fix".

| lever | default | why it is dead | proven by |
|---|---|---|---|
| `PLANNER_MACHINE_WARM`, `PLANNER_BUILD_CARRY_IN`, `PLANNER_CARRY_IN` | **ON** | all three only populate `_warm`, whose single consumer at `l5_cure_master.py:1178` is gated on `PLANNER_L5_WARM_RELEASE` (default **0**) | `MACHINE_WARM=0` vs default → `cure_campaigns` / `carry_out` / `cure_unplaced` byte-identical on 2026-07 |
| `PLANNER_CARRY_OUT` | **ON** | its only reader requires `HORIZON_MODE == "window"`; the shipped mode is `extend`, which then unconditionally rebinds `carry` at `:2772` | `CARRY_OUT=0` vs `=1` → byte-identical |
| `PLANNER_SUBFLOOR_*`, `PLANNER_L7_SPLIT_DEPTH`, `_SPLIT_MIN`, `PLANNER_ATOMIC_SPLIT_PLANTS` | various | `STRICT_LOT_FLOOR=1` forces `HARD_FLOOR=True` and empties `ATOMIC_SPLIT_PLANTS` (`l7:338-339`) | all five raised together → **9 artefacts byte-identical** on 2026-08. Corroborating: `< floor` reads `0.0 %` in **133 of 157** stored arm logs |
| `PLANNER_RIMSET_MIN_SHARE`, `PLANNER_RIMSET_MAX` | set | consumed only by `rim_sets()`, which is called below an early `return` (`allowable.py:280`) | no `[rimset]` line exists in any run log |
| `PLANNER_L5_EDD` | `0` | **dead on 2026-08 specifically, not in code.** August's order book is unphased — all 528,165 tyres carry `deadline = 31` — so the EDD sort key is constant and reduces exactly to the default. A deadline-aware placement branch cannot be tested on this month at all | `EDD=1` vs default → **all six artefacts byte-identical** on 2026-08 |
| `PLANNER_L5_DAY_CAP` | `0` | dead **at the shipped cap value**: `CONFIG.thresholds.cure_day_cap` is `{PCR: 0, TBR: 0}` and 0 means "ungoverned", so turning the flag on governs nothing. The mechanism works; the cap it reads is empty | `DAY_CAP=1` vs default → all six artefacts byte-identical on 2026-08 |
| `PLANNER_L7_CLOSING_MIX` = `cover` / `both` | `inherit` | needs `net_requirement_<next month>.parquet` to find next month's cold presses. **`net_requirement_2026-09.parquet` does not exist**, so both modes print `cannot target cold presses -- falling back to the inherited mix` and produce the inherited plan. A data gap, not a code gap | `cover` and `both` vs default → all six artefacts byte-identical on 2026-08; the fallback line is in `runs/TF_cover/log_l7_pull_release.txt:44` |
| `_DELAY` / `_SPLIT` / `PROTECT_FIRST_H` | — | `l56_loop.py` is in **neither** `main.py`'s stage list nor `run_arm.py`'s `STAGES`; `l56_delay_<M>.parquet` exists nowhere on disk | import → `_DELAY == {} `, `_SPLIT == {}` |

**`l56_loop` RE-MEASURED 2026-08-20 ON THE CORRECTED METRIC — still negative.**
Its docstring records that the acceptance metric was changed from
`qty_fed_in_month` to BUILT and blames the old metric for the −16,548 / −18,129
verdicts. That fix was never re-run. Driven directly (`python -m
planner.cmbc.l56_loop --month 2026-08 --run <arm> --mode <m> --iters 4`), on
2026-08, plant-total BUILT per iteration:

| iter | `delay` on base | `split` on base | `delay` on the takt arm |
|---|---|---|---|
| 0 (= no hint) | **507,970** | **507,970** | **512,955** |
| 1 | 504,943 | 505,208 | 509,602 |
| 2 | 503,624 | 502,641 | 510,720 |
| 3 | 500,522 | 501,916 | 508,688 |
| 4 | 504,286 | 502,017 | — |

Every hinted iteration is worse than the unhinted one, on both modes and on both
baselines, so "keep the best" keeps iteration 0 and the loop is a **no-op that
costs five L5→L7 passes**. Iteration 0 reproduces `TF_base` (507,970) and
`TF_ppart` (512,955) to the tyre, so the harness is sound and the result is the
mechanism's. The per-GT delay grain is the stated suspect (a single late unfed
campaign delays that GT's day-one campaign too) — but the *direction* is now
confirmed on the metric its own docstring says it should have used.


### The two that are worse than dead

**`[warm-mc] … released at tau_min` is a lie in the log.** `l5_cure_master.py:1027`
prints that 20 `(plant, GT)` pairs were released early. They were not — the branch
that would do it is off. Those 20 pairs cover **129 of 251 PCR campaigns (207,496
tyres, 52.1 %)** and **65 of 185 TBR campaigns (41.1 %)**. `WARM_RELEASE=1` does
produce a different plan, so the mechanism works; it is simply unreachable.

**`early_cap` cannot be reported in ANY configuration.** Every call site is
`_place(..., EARLY_CAP_H) or _place(..., inf)`, and `_diag_last` is cleared at the
top of each `_place`. The uncapped retry therefore always overwrites the cause.
Run with `PLANNER_EARLY_CAP_H=6`: the plan **changes** (PCR +640 / TBR −189,
starved 19,965 → 19,514) and `early_cap` still reads **0**. Knock-on: the whole
starve-cause histogram reflects only the **last** `_place` attempt — the
allowable-rescue pass, a different machine population from the primary rim-locked
attempt. On `BASE_jul` that mis-attributes **7,711 of 17,621 starved PCR tyres (43.8 %)**.

> **RULE: a diagnostic counter written inside a retried function records the
> retry, not the decision.** Snapshot the cause on the *first* failure, or the
> histogram describes a code path nobody cares about.

Latent scale of the unenforced cap: **56.4 % of PCR slices (169,014 tyres)** are
released more than 2 h earlier than τ\* requires (mean 3.09 h, max 52.5 h). Given
§4ad — six failures of the early-release family — a cap here is a **candidate worth
measuring**, not an obvious win.

---

## 4ai. L4.5 R5 GATE IS CONSTANT-PASS — and two months on disk are illegal

**Found by `schedule-forensics`, 2026-08-20.**

`l45_lotsize.py:497` grades `capped = qty > mx`, but `qty` was already forced under
`mx` at `:362` and `:438`. `capped == True` on **0 of 85/84/87/91** rows across four
months — the gate cannot fail.

Worse, it grades the **mean** lot while L5 actually consumes `lot_sizes`. This is
the *same* mean-vs-max defect as §4ab.2. §4ab.2 fixed the **producer** for the
months that were re-run; the **gate** was never fixed, and the two months that were
not re-run are still on disk carrying illegal lots:

| month | GT rows with `max(lot_sizes) > max_lot` | tyres past the R5 ceiling | gate printed |
|---|---|---|---|
| 2026-05 | 27 | **43,578** | `0 PASS` |
| 2026-06 | 37 | **63,554** | `0 PASS` |

Re-run L4.5 for 2026-05 and 2026-06 before either month is planned. The gate itself
must grade `max(lot_sizes)`, not `qty`.

---

## 4aj. THE TAIL IS NOT WHERE THE LOST TYRES ARE — R5 makes the idle hours unreachable

**Found by `schedule-forensics`, 2026-08-20. CONFIRMED. This closes a false lead that
cost most of a session — read it before proposing any tail-filling change.**

The reasoning that looked airtight: the last 3 plant-days leave **665 of 792 PCR building
machine-hours idle (84 %)** while **11,562 tyres go unmet**, and every starved GT is legal
on at least one of those idle machines. Therefore fill the tail.

**Wrong, and the disproof is one line of arithmetic.** R5 bounds a build to
`[t_cure − 72 h, t_cure − tau_min]`. Query `build_starved.parquet` for slices whose R5
window even *reaches* plant-day 29:

| | starved tyres | max `t_cure` | slices whose 72 h window starts after day 29 |
|---|---|---|---|
| Jul PCR | 11,562 | **2026-07-27 01:10** | **0** |
| Aug PCR | 12,686 | 2026-08-29 03:08 | **0** |
| Jul TBR | 1,486 | 2026-07-31 14:13 | **0** |
| Aug TBR | 1,654 | 2026-08-30 00:04 | **0** |

**Zero rows on all four plant-months.** The unmet demand needed building ~20 days before
those idle hours exist. A month-total of idle hours says nothing about any run's own R5
window.

The idle *shape* says the same thing from the other side — July PCR building machines:

| | gaps | total idle | p50 gap | max | gaps >= 4 h |
|---|---|---|---|---|---|
| plant-days 1–26 | **726** | 754 h | **0.70 h** | 10.6 h | 27 (162 h) |
| plant-days 27–31 | **49** | 990 h | **9.47 h** | 110.9 h | 29 (969 h) |

A floor-minimal PCR run is 150 x 60.2 s = **2.51 h**. Interior idle is 726 slivers with a
median of 0.70 h — **below the floor run, unusable by construction** (the mechanism the
`ANTI-SLIVER PACKING` comment at `l7_pull_release.py:2547-2571` describes). Tail idle is
29 blocks holding 969 h — usable, and it arrives after every deadline.

> **RULE: idle hours and unmet demand are only comparable inside the same R5 window.**
> Comparing a month-total of one against a month-total of the other is the same class of
> error as the five denominator defects in section 1 — two populations, one ratio.

### What IS true, and it is the opposite end of the month

| | Jul PCR | Aug PCR | Jul TBR | Aug TBR |
|---|---|---|---|---|
| `release_before_t0` share of starvation | **87.9 %** | 82.5 % | **91.4 %** | 81.1 % |
| starvation on d27–d31 | **0.0 %** | 6.1 % | 10.0 % | 5.3 % |
| `t0_short_h` p50 | 3.36 | 3.31 | 2.18 | 2.15 |
| `t0_short_h` max | 4.73 | 6.61 | 5.89 | 6.73 |
| **share <= 8 h short** | **100 %** | **100 %** | **100 %** | **100 %** |

**Every starved tyre is short by less than one shift.** But read
`l7_pull_release.py:2540-2546` before reading the label: the placer walks *backwards* past
every conflicting interval, so `before_t0` means **"the backward cascade ran out of
calendar"**, not "the deadline precedes the horizon". A slice with `t_cure` on 27 July and
`t0_short_h = 3.82 h` is a **machine-contention** failure, not a cold start. The field that
separates the two, `ideal_slack_h`, is `if DIAG:`-gated and absent from the shipped artefact.

---

## 4ak. FOUR MEASUREMENT DEFECTS FOUND WITH THE TAIL — none previously in any ledger

**`schedule-forensics`, 2026-08-20. All CONFIRMED by execution.**

### 4ak.1 `arm_scorecard.py` BUILT is not clipped to the month — 18 lines above a block titled "CLIP TO THE MONTH"

`scripts/arm_scorecard.py:116-118` filters `OPENING_STOCK` and sums `qty` with **no**
`end_ts <= month_end` filter. `:136-141` clips press utilisation to the month in the same
function.

| | headline BUILT | built **after** month_end | in-month BUILT |
|---|---|---|---|
| Jul PCR | 386,595 | **1,460** | 385,135 |
| Aug PCR | 409,967 | **6,090** | 403,877 |
| Jul TBR | 96,259 | 546 | 95,713 |
| Aug TBR | 98,003 | 940 | 97,063 |

The engine already computes the clipped figure — `build_by_shift.parquet` — and the
scorecard does not read it. **Deltas between arms are unaffected** (both sides share the
convention), which is why this survived; absolute BUILT is overstated.

**Why it bites here specifically:** `PLANNER_L7_TAIL_BUILD_PULL` (`l7:147`, default 0,
**undocumented outside its own source comment**) pulls month-crossing builds back inside the
boundary. Every post-boundary slice on both months has an R5-earliest build date inside the
month, so turning it on raises the day-29/30/31 build figures by up to 6,090 on August PCR,
changes `BUILT` by **zero**, and produces **zero** extra tyres. **Quote boundary-crossing
build beside any tail-shape metric or that flag reads as a free win.**

This is the 4af pattern verbatim: the fix was applied where the bug was noticed.

### 4ak.2 Bucketing build by `floor((end_ts − t0)/86400)` misfiles the last instant of day 31

A slice ending exactly at 07:00:00 on the boundary is the last instant of plant-day 31 and
lands in "day 32". July PCR: 5 rows, **2,333 tyres**. August PCR: 5 rows, 1,706.

| day 31, % of interior median | naive `end_ts` floor | boundary-correct | engine's `build_by_shift` |
|---|---|---|---|
| Jul PCR | **15 %** | **31 %** | 30 % |
| Aug PCR | **37 %** | **48 %** | 43 % |

**Days 27–30 are unaffected — that collapse is real.** `export_shift_schedule.py:73` buckets
on `start_ts` and `verify_export.py:217` subtracts an epsilon, so the shipped pack is
correct; only ad-hoc analysis queries make this mistake. **Use `build_by_shift.parquet`; do
not re-derive plant-days by hand.**

### 4ak.3 The day-30 hole is `CLOSING_BUFFER` by design, not an anomaly

`l7_pull_release.py:3814-3848`. July PCR tail, split by source:

| plant-day | 27 | 28 | 29 | 30 | 31 |
|---|---|---|---|---|---|
| regular pull release | 8,035 | 3,653 | 2,444 | **476** | 641 |
| closing buffer | 0 | 0 | 0 | 168 | **3,755** |

**Nothing special happens on day 30** — the pull release decays monotonically. The day-31
bump is the buffer, placed there by `:3828` (sort gaps **latest-ending first**, not largest)
and `:3846-3848` (build at the **end** of the gap). Both are R5-margin optimisations. The
buffer window reaches back to `month_end − 66 h`, so it *could* fill days 29–30 and chooses
not to.

Undocumented in the same block: `CLOSING_MIX` defaults to `"inherit"`, the mode its own
comment at `:3622-3631` calls **"THE DEFECT"**, while `:3714-3719` concludes *"Keep both and
take the larger"*. The default was never moved.

### 4ak.4 L11's last-day G8 detector cannot see the remedy shipped for it

`l11_validate_plan.py:481` grades `dm[-1]`, the **day-31 hourly mean**. The closing buffer
deliberately creates its stock in the final hours of day 31.

| | L11 last-day GT inventory | GT balance at `month_end` (next month's opening) |
|---|---|---|
| Jul PCR | **1,104 FAIL** (>= 4,500) | **4,079** |
| Aug PCR | **1,787 FAIL** | 4,034 |

`EXPERT_AUDIT.md` section 4a asked for a last-day check; the check was added and grades a
statistic **3.7x below** the quantity the fix moves. `l11_invariants.parquet` says the
hand-off was not repaired; `carry_forward_gt.parquet` says it was (156 -> 4,079).

---

## 4al. `l4b_capacity_flow` IS A CONSTANT-PASS GATE FOR THE EXACT FAILURE IT EXISTS TO DETECT

**`schedule-forensics`, 2026-08-20. CONFIRMED, run in-process on both months. CODE DEFECT.**

Its docstring: *"L5 seats cure campaigns first and L7 then discovers, one slice at a time,
that building cannot feed some of them. That is a late and expensive way to learn a fact
that is decidable up front."*

| | need_h | cap_h | **short_h** | verdict | actual starvation |
|---|---|---|---|---|---|
| Jul PCR | 6,619 | 7,775 | **0.0** | FEASIBLE | **11,562 tyres** |
| Aug PCR | 7,128 | 7,775 | **0.0** | FEASIBLE | **12,686** |
| Jul TBR | 5,444 | 6,361 | **0.0** | FEASIBLE | 1,486 |
| Aug TBR | 5,426 | 6,361 | **0.0** | FEASIBLE | 1,654 |

Three independent reasons it cannot fail:

1. **No time axis.** `cap_h = days * 24 * UTIL_CAP` for the whole month (`:71-72`). The
   6,687 idle press-hours L5 leaves in the last five days count as capacity for day-3 work.
   The subset feasibility condition is checked over machine *subsets*; time is not modelled
   at all.
2. **Eligibility wider than the planner's.** It uses `cap_machine` intersect `allowable`
   intersect `rimlock` — PCR **p50 3, max 9** machines/GT. L7 then applies the partition
   with `HARD_PIN=1`: **p50 1, max 2**.
3. **`UTIL_CAP = 0.95` and `need_h` excludes changeover entirely.** July PCR building
   realises **78.6 %** over 724 changeovers (552 weighted h). Reported headroom is 1,156 h;
   the modelling errors are worth ~785 h of phantom capacity plus ~552 h of unmodelled
   setup. **The gate's headroom is smaller than its own modelling error.**

Same shape as `EXPERT_AUDIT.md` section 4c (L6's R10 test on plant-median cadence).

> **Fifth always-passing guard found in this project.** The tell is always the same: the
> gate grades a quantity some earlier line already guaranteed, or a universe wider than the
> one the consumer enforces.

### Also proven dead here: `lot_deadlines`

`l45_lotsize.py:551` writes it, `l5_cure_master.py:1279` reads it, and the only consumer is
the EDD branch at `:1437-1439` gated on `PLANNER_L5_EDD` (default 0). The default sort at
`:1441` is effectively **`(plant, -qty, gt_code, seq)`** — exactly what the L4.5 comment
claims it fixed. **Proof:** perturbing the whole `lot_deadlines` vector (reversed, +15 d mod
31) leaves `cure_campaigns.parquet` **byte-identical** (`878fd77b...`).

And **August's order book is unphased** — all 270 PCR / 183 TBR lots carry `deadline = day
31`, one distinct value; July has 31. **Deadline-aware placement is untestable on August**
regardless of the flag. Add to the 4ah dead-lever table.

---

## 4am. THE TAKT GOVERNOR ON PCR — SHIPPED. The largest scheduler gain in this project.

`PLANNER_L5_TAKT_PLANTS` default **`"TBR"` -> `"PCR,TBR"`**;
`PLANNER_L5_TAKT_PART_PLANTS` default **`"PCR,TBR"` -> `"TBR"`**.
`planner/cmbc/l5_cure_master.py:396-397` and `:506-507`.

### What it is, and why it was off

The level-loaded press-concurrency governor. It was scoped to TBR because **section 4l.1
measured PCR mixed-sign (-0.28 pt Jul / +0.18 pt Aug) on *in-month fulfilment***. That
baseline no longer exists: it predates PINNED_FIRST (4ae), CLOSING_BUFFER (4ac), the L4.5
lot-sizing fixes (4ab), cavities=2, the load/unload correction and holidays.

**The only levelling mechanism in the engine did not apply to the plant that needed it.**
PCR was dumping **74 % (Jul) / 79 % (Aug) of its entire month's press slack into the last
five plant-days**; TBR, with the governor on, dumps 28 % on both months. TBR is the control.

### Measured — fresh arms, `run_arm.py`, per-month partition, both plants

| | Jul PCR | Aug PCR | Jul TBR | Aug TBR |
|---|---|---|---|---|
| BUILT | 386,595 -> **391,288 (+4,693)** | 409,967 -> **414,952 (+4,985)** | **byte-identical** | **byte-identical** |
| in-month | 96.59 -> **96.75 % (+0.16)** | 93.49 -> **93.76 % (+0.27)** | 97.87 % | 97.89 % |
| starved | 11,562 -> **6,545** | 12,686 -> **7,179** | 1,486 | 1,654 |
| R5 max | 68.1 -> **58.5 h** | 61.4 -> 61.9 h | 62.3 h | 71.7 h |
| WEIGHTED build changeover min/machine-day | 79.5 **FAIL** -> **64.3 PASS** | 86.3 -> 78.0 (still FAIL) | 25.7 | 17.8 |
| same-size share | 55.0 -> 64.3 % | 53.7 -> 60.4 % | 99.7 % | 100 % |
| carry-out tail | 2,831 -> **7,597** | 8,586 -> **13,493** | 1,433 | 1,508 |

**+9,678 BUILT across the two months. Positive on BUILT *and* in-month on both months. TBR
byte-identical — no mixed sign anywhere.**

July PCR build profile, % of interior median (`build_by_shift`, the engine's own plant-day):

| | d27 | d28 | d29 | d30 | d31 |
|---|---|---|---|---|---|
| base | 51 | 25 | 15 | **4** | 30 |
| shipped | **100** | **99** | **80** | **46** | 40 |

### The honest cost — the tail fills partly by BORROWING from the interior

August window decomposition: interior d3–26 **-10,373**, tail d27–31 **+15,051**, net
**+4,985**. **Only 33 % of the tyres appearing in the tail are new output**; the rest is
interior work relocated. Total build machine-hours used moves 6,772 -> 6,820 of 8,184 — the
gain is **48 machine-hours wide**, not 15,000. July's interior median falls 14,163 ->
13,068 for the same reason.

**Carry-out tail nearly triples on July PCR (2,831 -> 7,597).** Those tyres cure next month.
In-month rises anyway on both months, so the tail growth is more than covered — but this is
the number to watch if the levelling is pushed further.

### Why `TAKT_PART_PLANTS` is TBR-only

The sub-partition governor helps TBR and costs PCR. With it on PCR (`taktboth`):

| | Jul PCR | Aug PCR |
|---|---|---|
| BUILT | +4,095 (vs +4,693) | +4,334 (vs +4,985) |
| in-month | +0.34 (better) | **-0.40 (worse)** |
| R5 max | 57.9 h | **70.3 h — 1.7 h of margin against the hard 72** |
| L11 PASS | 33 (vs 31) | 32 (vs 31) |

`taktboth` wins July and **loses August on in-month**, which fails the two-month gate. Its
August R5 margin of 1.7 h is the deciding objection: PCR's binding scarcity is the **rim
sub-partition** (budget ALL 81 of 86 presses, but R13 24 of 51, R14 9 of 43, R17 10 of 46),
not the plant press total, so stacking a second concurrency governor over-constrains it.

Take `taktboth` only if the plant values the weighted-changeover and same-size invariants
(L11 31 -> 33 on July) more than 651 tyres and 8.4 h of R5 margin. That is a **PLANT RULING**.

### Robustness (August PCR delta BUILT)

| baseline | shipped (`ppart`) | `taktboth` |
|---|---|---|
| shipped | +4,985 | +4,334 |
| `CLOSING_BUFFER=0` | **+5,348** | +5,490 |
| `LOT_INTERVAL_H=8` | **+7,745** | +5,820 |

**The gain is not the closing buffer** — it is larger without it, and the buffer's own output
*falls* in the winning arms (+3,524 -> +3,161). The two are partly substitutes: the buffer
was spending idle tail hours the governor then uses better.

### Alpha is NOT tunable — do not "optimise" it

With PCR's sub-partition off: alpha 1.01 -> +5,169, 1.02 -> +2,284, **1.05 -> -617**. A 0.04
change swings BUILT by 5,800 tyres. That is greedy-placement jitter, and taking the argmax of
one month is the **mined-constant defect class** (section 1). `alpha = 1.0` is the only value
with a reason behind it.

### Measured negative alongside it — do not re-run

August PCR / TBR delta BUILT: `TAIL_BUILD_PULL` -318 / 0 · `EARLY_RELEASE` -526 / -564 ·
`MAX_CAMPAIGN_H=150` **-17,599** / -1,160 · `BACKLOAD=0.30` -4,026 / -613 · `STOCK_FIRST`
-2,921 / -960 (in-month +0.18 while BUILT falls — pure relocation) · `SCARCE_PRESS` -196 /
-177 · `MAX_PRESS_PER_GT=1.5` -1,733 / -523 · `TAKT=off` 0 / **-3,968** (confirms TBR's
governor is worth 3,968 on its own). No combination beat plain takt.

`PLANNER_L5_DAY_CAP=1` is **dead at the shipped cap value** —
`CONFIG.thresholds.cure_day_cap` is `{PCR: 0, TBR: 0}` and 0 means ungoverned. Mechanism
works, cap is empty. `PLANNER_L7_CLOSING_MIX=cover`/`both` needs
`net_requirement_2026-09.parquet`, which does not exist — a **data gap, not a code gap**.

### `l56_loop` re-measured on the corrected metric — still negative

Its docstring blames the old `qty_fed_in_month` acceptance metric for the -16,548 / -18,129
verdicts, and that fix was never re-run. Plant-total BUILT by iteration
(`delay` / `split` / `delay on the takt arm`): **507,970 / 507,970 / 512,955** at iter 0, then
504,943·505,208·509,602, 503,624·502,641·510,720, 500,522·501,916·508,688. **Every hinted
iteration is worse**, so the loop keeps iter 0 and is a no-op costing five L5->L7 passes.

### The verifier still fails — on the baseline too

`scripts/verify_export.py` returns *"plan is NOT physically executable (2 hard violations)"*
on the baseline **and** on both arms. **No new violation class is introduced**, but the counts
move: changeover-not-reserved 9/1,306 -> 13/1,278; machine-days over 24 h 1/594 -> 2/599 —
while the *worst* overrun improves 26.80 -> 25.75 h. A denser tail leaves less slack for an
unreserved changeover to hide in. **Open, pre-existing, not caused by this change.**

---

## 4an. "FEED THE PRESS CONTINUOUSLY" — tested on instruction, 8 of 8 cells negative, AND THE WAIT DOES NOT MOVE

**Tested 2026-08-20 on explicit instruction, on the NEW baseline (takt on PCR, 4am), both
months, both plants, fresh arms. `PLANNER_L5_FLOOR_BASIS` = `slice` (tau_min, 16 min) and
= `feed` (feed-balanced concurrency cap).**

### The question, and why it deserved a re-test

Every build slice is cured only after the whole slice is built, and then after a further
`wait_h`:

| July PCR | |
|---|---|
| slice qty p50 | 156 tyres |
| slice build duration p50 | 2.46 h |
| `wait_h` (slice END -> cure) p50 | 5.61 h |
| **build START -> cure start p50** | **8.27 h** |
| slices where cure starts before the slice ends | **0 of 2,474** |

A press holds 2 cavities and building runs at 60.2 s/tyre, so **two tyres exist ~2 minutes
after a run starts.** The model asks for 8.27 hours. Re-testing was legitimate: the previous
rejections predate the takt governor, and ledger lesson 5 is that two experiments rejected
on the 98.9 %-era engine were both worth points once re-run.

### RESULT — negative in every cell

BUILT, against the shipped `star` baseline:

| | Jul PCR | Aug PCR | Jul TBR | Aug TBR | total |
|---|---|---|---|---|---|
| `slice` (tau_min) | **-3,634** | **-5,638** | **-1,787** | **-1,501** | **-12,560** |
| `feed` (balanced) | **-2,219** | **-5,218** | **-780** | **-1,246** | **-9,463** |

in-month: `slice` -0.96 / -1.11 / -2.27 / -1.47 pt. `feed` -0.35 / -0.69 / -0.71 / -1.83 pt.

Starvation rises everywhere; **August PCR nearly doubles, 7,179 -> 13,834.**

### THE PROOF — removing the wall does not reduce the wait. In 3 of 4 cells it RISES.

| `wait_h` p50 | star (shipped) | slice | feed |
|---|---|---|---|
| Jul PCR | 5.61 h | **5.79 h** | 5.72 h |
| Jul TBR | 7.00 h | **7.56 h** | 6.94 h |
| Aug PCR | 5.77 h | 5.37 h | 5.52 h |
| Aug TBR | 6.22 h | **6.44 h** | 6.25 h |

**This is the whole finding.** `tau*` is not what makes a tyre wait 5.61 h. Measured on
`SHIP_jul`, at the moment a build slice finishes:

| July PCR, 2,474 slices | |
|---|---|
| the target press is **already running that same GT's campaign** | **2,312 (93.5 %)** |
| the press is running a **different** GT (a real queue) | 30 (1.2 %) |
| the press is genuinely idle | ~6.5 % |

**The press is not waiting for the tyre. The tyre is waiting for the press** — it sits in the
GT buffer while the press works through a campaign whose median span is 213 h. Deleting the
release wall cannot shorten a queue, so the wait is unchanged; all the wall did was hold
back presses that would otherwise be seated at t0 against feed that does not exist.

Our per-tyre wait is also **not** worse than the plant's. July 2026 PCR MES, 399,717 cures
joined to their build barcode: plant **p10 1.36 h, p50 4.84 h, p90 21.09 h**; ours p10 0.27,
p50 5.61, p90 18.05. **0.77 h apart at the median. There is no 8-hour modelling error to
recover.**

### And this time there is not even a borrowed day-1 gain

| July PCR | d1 cure | d1 build | interior median | month BUILT |
|---|---|---|---|---|
| star | 11,716 | 13,502 | 13,068 | 387,340 |
| slice | **11,558** | 14,129 | 12,843 | 383,590 |
| feed | 12,010 | 14,025 | 12,914 | 386,439 |

| Aug PCR | d1 cure | d1 build | interior median | month BUILT |
|---|---|---|---|---|
| star | 12,131 | 14,754 | 13,758 | 408,235 |
| slice | **11,495** | 14,569 | 13,630 | 402,775 |
| feed | 13,071 | 14,889 | 13,807 | 403,488 |

`slice` loses day-1 cure on **both** months. Only `feed` gains it (Aug 12,131 -> 13,071) and
still loses 5,218 BUILT. So this is not even the `day1-gain-is-borrowed` pattern — it is a
straight loss with nothing bought.

### Attempts 7 and 8 in the same family

`WARM_RELEASE` -1,319 · `FLOOR_BASIS min/slice` -2,478..-6,050 · `T0_STOCK_BASIS lot` -1,670
· `STOCK_FIRST` -4,936 · `CHG_PARALLEL` -3,665 · `slice` on the CB baseline -7,088 · and now
`slice` -12,560 and `feed` -9,463 on the takt baseline, both plants, both months.

> **RULE, now proven rather than inferred: the release wall is not what makes a tyre wait.**
> 93.5 % of the time the press is already busy with that GT. Before proposing any change to
> `tau*`, `FLOOR_BASIS`, `WARM_RELEASE` or early release, run the one query that settles it —
> is the target press idle at build-end? If it is busy, the release rule is not the binding
> constraint and the experiment will fail. **This check costs one query and has now saved
> eight experiments' worth of compute.**

### What is still true from the original observation

The 6.5 % of slices where the press IS genuinely idle at build-end, and the **first seat of
each campaign**, remain the only places a release change can bind. That is a targeted
question, not a month-wide floor change, and it is bounded by roughly one sixteenth of the
volume the month-wide version touches.

---

## 4ao. THE INCH LOCK IS NOT FOLLOWED ON PCR — measured against the plant's own MES

**Found 2026-08-20 on a direct question ("is the inch locking followed?"). CONFIRMED
against 400,336 July MES rows with a known rim. NOT SHIPPED — the remedy costs volume and
the decision is a PLANT RULING.**

`machine_rim_lock.parquet` is mined from 8 months of MES and tags each machine with a rim
and a tier: **hard** (single rim, ~100 % purity), **primary** (dominant rim), **flex** (the
plant's designated mixer). July PCR, plant behaviour vs our plan, by volume:

| tier | **PLANT off-lock** | **OURS** |
|---|---|---|
| **hard** | **0.0 %** | **7.7 %** |
| primary | 9.8 % | 20.1 % |
| flex | 23.7 % | 52.9 % |
| **TOTAL** | **5.3 %** | **15.7 %** |

**We are 3x looser than the plant overall, and on `hard`-tier machines the plant is at
literally zero while we are not.** Volume on hard machines carrying a foreign rim:
**Jul 16,957 · Aug 14,794** tyres. Whole-PCR off-lock: Jul 60,952 · Aug 81,981.
**TBR is clean** — 0.0 % on hard and primary, both months.

### The leak is NOT the partition, and NOT `HARD_LOCK`

- **Partition: clean ON AUGUST ONLY — see 4aq, this claim is CORRECTED.** August hard tier is
  **0 of 26** PCR rows and 0 of 23 TBR. **July's partition puts two R16 GTs on the hard R18
  machine TBMPCR9**, delivering 1,127 tyres (see 4at), and `_locked()` returns `part_of[gt]` first so
  `RESCUE_SKIP_HARD` cannot touch it. Checking only the month whose partition is on disk hides
  this.
- **89 % of the hard-tier off-lock volume is on GTs that ARE in the partition, placed on a
  machine the partition did not assign — 0 of it on a partitioned (GT, machine) pair.**
- `HARD_LOCK=1` only skips the **third-pass** off-lock spill (`l7:3262`). The escape sits
  *below* it: **`ALLOWABLE_RESCUE`** (`l7:3271`, default **ON**), whose `_extra` is exactly
  `[m for m in cand if m not in _lk]` — the machines outside the lock.

The code says so in as many words: *"A historical rim lock is a preference, not a legal
ban."* That is a defensible position; what was never measured is that **it applies the same
licence to `hard` machines the plant itself never breaks.**

> **Two gates named after the rule, and the real escape is below both of them.** `HARD_LOCK`
> and `restrict_rimlock` both read as "the lock is enforced". Neither covers the last-resort
> pass. When a constraint has more than one gate, measure the OUTPUT against the master, not
> the gates — this is the sixth always-passing-guard shape in this project.

### THE THREE OPTIONS, MEASURED. Fresh arms, both months, both plants.

| | Jul PCR BUILT | Jul hard off-lock | Aug PCR BUILT | Aug hard off-lock | L11 |
|---|---|---|---|---|---|
| **as shipped** | 391,288 | **7.6 %** | 414,952 | **6.2 %** | 31 |
| **`PLANNER_RESCUE_SKIP_HARD=1`** | 390,212 (**-1,076**) | **0.9 %** | 409,511 (**-5,441**) | **0.5 %** | **32** |
| `PLANNER_ALLOWABLE_RESCUE=0` | 386,308 (**-4,980**) | 1.1 % | 398,798 (**-16,154**) | 0.7 % | **33** |

**TBR is byte-identical in all three** — it has nothing to fix.

**The middle path dominates the blanket refusal.** It reaches *better* hard-tier compliance
(0.9 / 0.5 % vs 1.1 / 0.7 %) for **6,517 tyres across two months instead of 21,134** — under
a third of the cost. With the rescue fully off, runs that would have been rescued get placed
by other paths that also leave the lock, so refusing everything is both dearer and dirtier.

`RESCUE_SKIP_HARD` keeps the rescue for `primary` and `flex`, where the plant itself mixes
(9.8 % and 23.7 %), and refuses it only on `hard`.

### Residual — TRACED IN 4at, this section is superseded

**No arm reaches 0.0 % on hard tier** — 0.9 / 0.5 % survives even with the rescue fully off
(Jul 1.1 %, Aug 0.7 %). There is a **second, smaller leak** into hard-tier machines that is
not `ALLOWABLE_RESCUE`. Not diagnosed. It is ~1 % of hard-tier volume, so it is worth one
query before anyone claims full compliance.

### THIS IS A PLANT RULING

The engineering is done and the flag ships **OFF**. The question is the plant's:

> *May a hard-locked building machine ever take a foreign inch — knowing that forbidding it
> costs roughly 6,500 tyres over two months (-0.26 pt July, -1.17 pt August on PCR), and
> that the plant's own machines have not done it once in eight months?*

Do not decide this unilaterally. **The mined lock is a statistic; whether it is a
constraint is the plant's call** — the exact distinction that cost this project 13.4 points
when `tau*` and `min_lot` were wired in as hard floors (section 1).

Note the asymmetry when putting it to them: July costs -0.26 pt, August -1.17 pt. August is
the tighter month (94.7 % press load vs 86.2 %), so the price of compliance rises exactly
when capacity is scarce.

---

## 4ap. MASTER-COMPLIANCE SWEEP — every rule graded OUTPUT vs MASTER vs PLANT

**2026-08-20/21, on `runs/SHIP2_jul` / `runs/SHIP2_aug` (shipped defaults) and the exported
CSVs. Measured directly, not delegated** — a three-agent audit tree was launched for this and
every agent died on API errors returning nothing; the fragments they emitted were mid-sentence
and were **not** used. Everything below was run by hand.

### CLEAN — verified, do not re-check

| rule | result |
|---|---|
| **R3 — concurrent presses per GT <= mould count** | **0 violations**, all four plant-months. Full master coverage: 0 GTs without a `cap_mould_<M>` row |
| **Press eligibility** | **0** `(gt, press)` campaign pairs outside `cap_press_<M>`, all four plant-months |
| **`gt_size` rim coverage** | **0** planned GTs missing a rim, all four plant-months. `gt_size` holds 583 rows, **0 null rim** |
| **B16 TT/TL** | 100 % of TBR volume tagged. Aug **clean**; Jul one leak of **77 tyres (0.7 %)** on `TBMTBR4Stage2`, the flex machine |
| **Building changeover time budget** (PCR 6 % / TBR 3.5 % of available) | **PASS on all four cells**: 4.66 / 5.73 / 1.83 / 1.30 % |
| **TBR build cadence** | plan p50 **202.35 s/tyre** vs plant in-run p50 **202.0**. Spot on |

**CLAUDE.md is STALE on `gt_size`.** It still says *"28 August GTs have no rim — the single
highest-value master-data fix"*. That gap is closed; every planned GT on both months resolves
a rim. Anyone acting on that line is chasing a fixed bug.

**A false alarm worth recording so it is not re-raised.** A naive join of `tt_tl.parquet` on
`gt_code` returns **zero overlap** with the planning namespace on both plants (PCR 0 master
rows; TBR 104 master GTs, 46 planned, overlap 0). That is the **GT namespace trap**, and the
code already handles it — `l1_preflight.py:292` and `l7:1075` join on **`sku`** via the demand
file, and say so in a comment. Via the correct bridge, coverage is **100 %**. Do not "fix"
this join.

### MASTER-DATA GAP — the crew rule in the docstring does not exist in the master

**CORRECTED 2026-08-21. My first pass called this a code defect and quantified a
"28-29 % overstatement" on TBR. That was WRONG** — it applied a 2/3 rule the data does not
contain. The correction matters more than the original claim, so it is recorded in full.

`l10_discretise.py:36` states: *"Each change needs 2 fitters (same size) or 3 (different
size), from the plant's [master]."* The master it names, `warehouse/derived/cap_changeover.parquet`,
carries **one `crew` column with the value 3 on all 20 rows** — there is no same/diff split
to read. `l10:94` loads that column and `l10:226` writes it. **The code is faithful to the
master; the docstring describes a rule the master cannot express.**

So there is no arithmetic error in sheet `4_crew_load`. What exists is a **master-data gap**:
either the plant has a same-size crew figure that was never captured in `cap_changeover`, or
the 2/3 rule in the docstring is simply not the plant's rule. **Ask the plant which.** If the
2/3 split is real, the sizes at stake are: Jul PCR 35 of 104 changes same-rim, Jul TBR 68 of
80 (85 %), Aug PCR 38 of 131, Aug TBR 57 of 66 (86 %) — TBR would move by ~28 %.

`same_rim` IS computed correctly (`l10:219`) and IS exported, so the column needed to apply
the rule is already in the pack the moment the plant supplies the second crew number.

> **The lesson is the one this project keeps relearning: a docstring is not a master.** I
> graded the output against a rule I read in a comment instead of against the file the code
> reads, and produced a confident four-row table of a defect that is not there. Grade against
> the artefact, every time.

### WHAT A SAME-SIZE vs DIFFERENT-SIZE CHANGEOVER ACTUALLY IS

From `cap_changeover.parquet`, and this is the whole reason B3/same-size share is a KPI:

| plant | machine type | **same-size** | **different-size** | ratio |
|---|---|---|---|---|
| PCR | BJ (PCR1-5) | **28 min** | **60 min** | 2.14x |
| PCR | CONTI (PCR6-11) | **22 min** | **42 min** | 1.91x |
| TBR | SAV / MESNAC | **10 min** | **24 min** | 2.40x |

"Same size" means the next GT carries the **same rim (inch)**, so the building drum stays at
its width and only the recipe and components change. "Different size" re-sets the drum.
**A different-size change costs roughly twice a same-size one on every machine in the plant** —
which is why the rim lock (4ao), the key-2 rim continuity tie-break in L7, and the same-size
share invariant all exist. It is also why `inch_lo`/`inch_hi` per machine matter: a machine
physically spans only part of the rim range (PCR3-5 are 12-16 in, PCR6-11 are 13-18 in).

### DELIBERATE TRADE, now quantified — PCR build cadence is 5.3 % optimistic

| build s/tyre | plant p25 | **plant p50** | plant mean | **our plan p50** | workbook |
|---|---|---|---|---|---|
| PCR | 49.0 | **57.0** | 69.5 | **54.0** | 63.0 |
| TBR | 181.0 | **202.0** | 218.6 | **202.35** | 207.0 |

Plant figures are the **true in-run cadence** — consecutive same-GT intervals under 10 min on
the same machine, 399,539 PCR intervals in July, so changeovers and long stops are excluded.

PCR is planned **5.3 % faster than the plant's own median**, but comfortably inside its
demonstrated range (p25 = 49.0 s). Over a month that is ~340 PCR machine-hours of optimism.
**Not fabricated capacity — the plant does hit it — but it is not the median either.** The
workbook's 63 s sits near the plant *mean* (69.5), which carries the slow tail.

### NO MECHANISM EXISTS — two rules the plant has given us that the engine cannot express

**1. Curing changeovers: min 3, max 4 per day.** Measured plant-wide:

| July | total | p50/day | days with **0** | max | **days inside [3,4]** |
|---|---|---|---|---|---|
| **ours PCR** | 104 | 6 | **12** | 14 | **3 of 31 (10 %)** |
| plant PCR | 252 | 5 | 0 | 26 | 10 of 31 (32 %) |
| **ours TBR** | 80 | 3 | 10 | 14 | **8 of 31 (26 %)** |
| plant TBR | 86 | 3 | 1 | 6 | 14 of 29 (48 %) |

Broken in **both** directions — 17 days below 3 (12 of them zero) and 11 days above 4.
**The plant does not follow it either** (32 % / 48 % compliance, and one 26-change day), so
this is a *target*, not mined behaviour. **Per press per day our max is 1**, so if the rule is
per-press we are far under, not over. **Which reading applies is an open question for the plant.**

**2. Per-machine-per-day SKU cap of 3 or 4.**

| | machine-days | over cap 3 | **over cap 4** | max | plant July |
|---|---|---|---|---|---|
| Jul PCR | 336 | 22 (6.5 %) | **3 (0.9 %)** | 5 | max **4**, over-4 **0 %** |
| Aug PCR | 330 | 65 (19.7 %) | **8 (2.4 %)** | 5 | — |
| Jul TBR | 273 | 62 (22.7 %) | **5 (1.8 %)** | 5 | max **6**, over-4 **16 %** |
| Aug TBR | 267 | 36 (13.5 %) | **7 (2.6 %)** | 5 | — |

At cap 4 we break it on 0.9–2.6 % of machine-days. **PCR: the plant never exceeds 4 and we
do. TBR: the plant exceeds 4 on 16 % of machine-days and we do on 1.8 % — we are stricter
than the plant.** No cap of either kind is enforced anywhere in the engine.

### STRICTER THAN THE PLANT — the mirror-image defect

`STRICT_LOT_FLOOR=1` gives us **0.0 % sub-floor runs**. The plant runs **13.2 % of July PCR
runs (8,857 tyres, 2.2 %)** below the 150 floor. We are more rigid than the plant on its own
rule. Related: section 4ah records that all five sub-floor/split levers are **dead** under
`STRICT_LOT_FLOOR`, so this cannot currently be relaxed by a flag.

### Residual, still untraced

Hard-tier off-lock does not reach 0.0 % in any arm — **0.9 % (Jul) / 0.5 % (Aug)** survives
after 4ao, and 1.1 / 0.7 % survives with `ALLOWABLE_RESCUE` fully off. **A second, smaller
path into hard-tier machines exists and has not been found.** ~1 % of hard-tier volume.

---

## 4aq. THE BOUNDED PRE-t0 BUILDING WINDOW — the carry-in question, finally MEASURED. Starvation moves, output does not. SHIPS OFF (2026-08-21)

**`PLANNER_L7_PRE_T0_H`, default 0 (inert — verified byte-identical to base on all
fourteen run artefacts). August 2026, both plants, fresh arms via
`scripts/run_arm.py` on the 2026-08 partition, all `check_arm_fresh` FRESH.
Baseline `runs/SHIP2_aug` reproduced exactly as `CI_base` before any delta was
read.**

§4p.4 left this open in as many words: *"a **bounded ~4 h** pre-horizon window
matched to the measured deficit has never been tried and is not the same
experiment."* It has now. `PLANNER_DIAG_PRE_H` is DIAG-gated and does not emit a
runnable plan; this flag is bounded, runs in a normal arm, and tags its rows so
the accounting stays honest.

### 4aq.1 What it does

Building — and only building — may be released `H` hours before `t0`. Cure
campaigns do not move; `t0` and `month_end` are unchanged; R5 is enforced on
every slice exactly as before; the WIP rail still books the stock. Implemented as
one new symbol, `_t0b`, defined beside `t0`: **`t0` stays the horizon anchor for
everything graded, bucketed or reported against the month; `_t0b` is the earliest
instant a build RUN may start, and it is the only thing the flag moves.** Six
call sites take `_t0b`: the `before_t0` refusal, the anti-sliver previous-end
floor, `_r5_floor`, and three inside `_make_room`.

### 4aq.2 The result

| PCR | BUILT (Aug) | dBUILT | pre_t0 (Jul) | in-month | ful % | starved | before_t0 | r5 | rail | R5 max | same % | wCO | L11 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **base** | **409,511** | +0 | 0 | 397,326 | 92.59 | 12,477 | 6,810 | 5,044 | 623 | 63.3 | 69.3 | 73.6 | 32 |
| H=4 | 408,551 | **−960** | 1,383 | 397,823 | 92.70 | 12,125 | 1,409 | 9,170 | 1,546 | 61.9 | 68.3 | **74.3 FAIL** | 31 |
| H=8 | 408,448 | **−1,063** | 2,732 | 398,890 | 92.95 | 10,879 | **305** | 9,493 | 1,081 | 63.3 | 66.6 | 72.9 | 32 |
| H=12 | 406,424 | **−3,087** | 3,334 | 397,786 | 92.69 | 12,135 | 1,380 | 9,111 | 1,644 | 69.9 | 66.2 | **74.8 FAIL** | 31 |

| TBR | BUILT (Aug) | dBUILT | pre_t0 (Jul) | in-month | ful % | starved | before_t0 |
|---|---|---|---|---|---|---|---|
| **base** | **98,003** | +0 | 0 | 96,932 | 97.89 | 1,654 | 1,341 |
| H=4 | 98,363 | **+360** | 378 | 97,670 | 98.64 | **916** | 603 |
| H=8 | 97,576 | −427 | 804 | 97,309 | 98.27 | 1,277 | 964 |
| H=12 | 97,609 | −394 | 817 | 97,356 | 98.32 | 1,231 | 332 |

`ful %` is `qty_fed_in_month / demand` with residual GTs in the denominator — the
basis the shipped scorecard quotes. R5 max on TBR is 71.7 h in every arm; GT
daily-mean max stays under both rails in every arm (PCR 4,522 → 4,567 worst, TBR
1,306 → 1,324 worst).

### 4aq.3 The starvation does not go away — it changes gate

`release_before_t0` collapses on PCR, **6,810 → 305 at H = 8 (−95 %)**, and
`r5_shelf_life` rises **5,044 → 9,493** in the same arm. Total starvation falls
only **1,598**. Three quarters of the relieved volume is re-refused one gate down.

> **The 72 h shelf life was always the binding constraint; `t0` was standing in
> front of it.** Widening the calendar backwards lets the backward cascade walk
> further, until R5 stops it. This is §4aj's finding from the other side — the
> label `release_before_t0` means "the cascade ran out of calendar", not "the
> deadline precedes the horizon", so relieving it was never going to be a
> cold-start fix.

### 4aq.4 THE TRAP: read in-month alone and H = 8 is a win. It is a 1,063-tyre loss.

August in-month rises **+1,564** (92.59 → 92.95). August's own BUILT **falls
1,063**, while **2,732 tyres appear that JULY built**. The entire in-month gain,
and more, is imported across the boundary. **This is §4ak.1's defect one boundary
along**: a scorer that sums `build_schedule.qty` unfiltered reads a 1,063-tyre
loss as a 1,669-tyre win. Split on `end_ts <= t0` or do not quote the number.

The mechanism of the PCR loss is in the counters: `gt_wip_rail` refusals go
**623 → 1,081** (H=8) and **1,546** (H=4). Building earlier *is* inventory, and
PCR already sits at its rail margin — daily-mean max 4,522 against the enforced
4,800 × 0.94 = 4,512. The window buys starvation relief with headroom that does
not exist. Same-size share pays as well: 69.3 → 66.6 % at H=8, and weighted
changeover **fails L11 outright at H=4 (74.3) and H=12 (74.8)** against 74.0.

**H is not monotone in anything.** H=12 is worse than H=8 on every PCR line; TBR's
only positive BUILT cell is H=4 (+360) and it reverses to −427 at H=8. A lever
whose sign flips with its own magnitude is a reshuffle, not a mechanism.

### 4aq.5 THE HOURS ARE NOT AVAILABLE — and the month total says they are

Checked per machine against `runs/SHIP2_jul`, the shipped July plan:

| | claim | **double-booked** | machines over |
|---|---|---|---|
| H=8 PCR | 52.7 machine-h | **18.95 h (36 %)** | 6 of 10 |
| H=8 TBR | 50.2 machine-h | **16.96 h (34 %)** | 4 of 9 |
| H=4 PCR | 32.8 machine-h | **17.80 h (54 %)** | 6 of 10 |
| H=4 TBR | 26.3 machine-h | **9.81 h (37 %)** | 4 of 9 |

July's last 8 h hold **48.1 free PCR / 45.6 free TBR machine-hours in aggregate**,
which reads as ample. It is not: TBMPCR9 and TBMPCR10 are **100 % busy** in that
window while TBMPCR2/4/6/7 are entirely idle, and the window takes the busy ones
anyway. **Even the measured recovery above is an over-estimate of what a real
carry-in could deliver.** DO-NOT #19 and #22, exactly: a month total of a resource
says nothing about any one holder of it.

### 4aq.6 It does not export

`scripts/verify_export.py` on the H = 8 pack adds a **third HARD violation the
baseline does not have** — *"build row outside the plant month: 66 start
outside"* — plus **two EXPORT reconciliation failures** (PCR sheet1 404,463 vs
sheet7 400,428; TBR 97,440 vs 96,332), because the shift pack's sheet 1 keeps the
pre-t0 rows and its sheet 7 daily summary does not. **The pack disagrees with
itself about how many tyres August built.** (The baseline's own 2 HARD violations
— unreserved changeover ×12, machine-day over 24 h ×2 — are pre-existing, §4am.)
Shipping this needs the export layer to learn the concept too.

### 4aq.7 Verdict

The carry-in question is real and this is the first bounded, runnable measurement
of it. At the sizes July can actually lend, it is worth ~1,600 tyres of PCR
starvation relief for **1,063 tyres of August output, 2.7 pt of same-size share,
and rail headroom that is not there**. The prize is not where the brief expected
it. **Ships OFF.** The number is the deliverable — put it to the plant with
§4aq.5 attached, because the machines it wants are the ones July is using.

---

## 4ar. `PLANNER_RESCUE_SKIP_TIERS` — extending the rim lock to `primary`. The changeover hypothesis is REFUTED BY ITS OWN MECHANISM. DEFAULT STAYS `hard` (2026-08-21)

**Generalises §4ao's `PLANNER_RESCUE_SKIP_HARD` boolean to a comma-separated tier
list. `hard` (default) reproduces the shipped behaviour byte-identically;
`RESCUE_SKIP_HARD=0` still empties the set. `flex` is exempt by design — the
plant's own flex machine runs 23.7 % off-lock and mixing is its job (§4q.6,
DO-NOT #33). August 2026, both plants, fresh arms, `check_arm_fresh` FRESH.**

### 4ar.1 The result

| PCR | BUILT | dBUILT | in-month | ful % | starved | of which r5 | same % | wCO min/mach-day | occ % | R5 max | tail | L11 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **`hard`** (shipped) | **409,511** | +0 | 397,326 | 92.59 | 12,477 | 5,044 | 69.3 | 73.6 | **83.7** | 63.3 | 12,620 | 32 |
| `hard,primary` | 398,272 | **−11,239** | 388,690 | **90.57** | **23,465** | **18,362** | **74.7** | **66.5** | **81.5** | **71.9** | 10,268 | **33** |

`primary` off-lock **29.1 % → 16.0 %** · `hard` off-lock **0.5 % → 0.9 % (worse)**
· weighted setup 458.6 h → 426.0 h · changeovers 838 → 817 · GT mean 4,225 → 4,221.

**TBR: every aggregate identical** — BUILT 98,003, starved 1,654 with the same
cause split, R5 71.7 h, same-size 100.0 %, weighted CO 17.8. Eight of its nine
machines are hard/primary and **none was carrying a foreign rim to begin with**,
so closing them only reshuffles which press each slice feeds. *Correction to
§4ao's wording:* at `hard,primary` TBR is not **byte**-identical — `qty`,
`cure_ts` and `wait_h` move per row while every total holds. Identical KPIs,
different file.

### 4ar.2 Both halves of the hypothesis moved the predicted way, and it still lost

The hypothesis was that fewer diff-size changeovers (42–60 min vs 22–28) buy back
machine hours that partly pay for the volume. **Tested, not assumed. Both terms
moved the right way and the trade still failed:**

- same-size share **+5.4 pt**, weighted changeover **−7.1 min/machine-day**,
  weighted setup **−32.6 h**. The setup saving is real.
- machine **occupancy fell 83.7 → 81.5 %**, which on 11 × 744 h is **≈ 180
  machine-hours lost**.

> **We saved 33 hours of setup and lost 180 hours of production.** The refused
> runs do not become cheaper work on another machine; they become no work at all.
> `r5_shelf_life` starvation goes 5,044 → 18,362 and R5 max climbs to **71.9 h**,
> one tenth of an hour under the cap.

**This is the general answer to "surely fewer changeovers pay for themselves" on
this engine, and it should be quoted before the next such proposal.** Setup time
freed on a machine a run may no longer use is not capacity. Occupancy is the
number that decides it — report it beside any changeover claim.

### 4ar.3 It does not even reach compliance

`primary` lands at **16.0 %, still above the plant's own 9.8 %**, and hard-tier
off-lock gets **worse** (0.5 → 0.9 %) because runs the rescue would have taken are
placed by other paths that also leave the lock. That is §4ao's own finding
reproduced one tier up — *refusing more is both dearer and dirtier* — and the
second, untraced leak named in §4ao's "Residual, not yet traced" is what puts the
floor under both numbers. **Trace that leak before anyone tries this again.**

### 4ar.4 The one thing it buys

L11 goes 32 → 33: *"PCR same-size share of build changeovers"* flips FAIL → PASS.
**11,239 tyres for one green cell is not a trade; it is a metric being satisfied
at the plan's expense.** Recorded here so the invariant count is never read as the
score.

**Verdict: default stays `hard`.** Kept gated with its number so nobody re-runs it
blind.

---

## 4at. THE INCH-LOCK RESIDUAL — traced to three paths, accounting closes exactly

**`schedule-forensics`, 2026-08-21, read-only. CONFIRMED. This closes the "not yet traced"
line left open in 4ao, and CORRECTS two claims in 4ao and one I reported verbally.**

### CORRECTION 1 — my "before" baseline was the wrong run

I reported the pre-fix July PCR starve split as **87.9 % `release_before_t0` / ~5 % `r5` /
6.7 % `wip_rail`**. That is `runs/FINAL_jul`, an **older arm**, not the shipped baseline.
`runs/SHIP_jul/log_l7_pull_release.txt` reads:

| cause | **SHIP_jul (true before)** | SHIP2_jul (after) | delta |
|---|---|---|---|
| `release_before_t0` | 4,001 (61.1 %) | 3,830 (50.3 %) | **−171** |
| `r5_shelf_life` | **1,479 (22.6 %)** | 3,333 (43.7 %) | +1,854 |
| `gt_wip_rail` | 1,065 (16.3 %) | 458 (6.0 %) | −607 |

**`release_before_t0`'s numerator barely moved (−171). Its share fell because the denominator
grew 16 %.** The "dramatic share shift" I described was mostly a denominator artefact of my
own comparison — the sixth denominator error in this project's log, and this one was mine.
`r5` was already at 22.6 %, not 5 %.

> **RULE: the baseline for a shipped change is the arm the change was measured against, by
> name.** Grepping a percentage out of whichever log has it finds an older arm silently.

### CORRECTION 2 — 4ao's "the partition is clean" was August-only

4ao states *"Partition: clean. August, hard tier 0 of 26 PCR rows"* and *"0 of it on a
partitioned (GT, machine) pair"*. Both were checked **on August**, whose partition is genuinely
clean (0 of 34). **July's is not:**

```
GT 2666 RAN HT   rim R16 -> TBMPCR9Stage2   (locked R18, tier hard, purity 100.0 %)
GT2776 RAN AT    rim R16 -> TBMPCR9Stage2   (locked R18, tier hard, purity 100.0 %)
```

2 of 46 PCR rows, delivering **1,127 tyres = 56.2 % of July's residual**, present in both
arms. `_locked()` returns `part_of[gt]` **first**, and `RESCUE_SKIP_HARD` filters only
`_extra` — so a hard machine inside `_lk` is untouchable. **This is a per-month defect in
`scripts/build_gt_machine_partition.py` and it is invisible to anyone who checks only the
month whose partition happens to be on disk.** The builder needs a rim-coherence assertion
and every month's partition needs re-checking.

### The three paths, and the accounting closes to the tyre

| month | **A** partition | **B** `home_of` | **C** closing buffer | total | residual measured |
|---|---|---|---|---|---|
| Jul PCR | 1,127 | 0 | 877 | **2,004** | **2,004** |
| Aug PCR | 0 | 456 | 625 | **1,081** | **1,081** |

**Path B — `home_of` is unfiltered by rim, tier and share.** The non-partition branch of
`_locked()` puts `home` in **first** with no rim check, no tier check, no share threshold.
`gt_home_machine.parquet` gives `GT 1583 NEO TATA` a rank-4 machine `TBMPCR4` at **0.3 %
historical share (279 tyres in 8 months)** — and TBMPCR4 is R12, hard, 99.9 % purity. It
enters the lock set of an R13 GT and is immune to the fix. 456 t. **Failure mode 2 exactly:
an observation set used as a capability set.**

**Path C — the closing buffer bypasses `_place` entirely.** It loops `sorted(elig[...])` —
the raw allowable matrix, no `_locked()`, no rim lock, no `_hard_mc`, no `_place` — and
appends straight to `bs`. `sorted()` is alphabetical, so **TBMPCR10, TBMPCR11, TBMPCR1** are
tried first and two are hard tier. 877 t (Jul) / 625 t (Aug). Identifiable by `press IS NULL`.

**Path D, latent, zero volume this month:** `_lk = [...] or cand`. When the lock set is empty
the whole candidate list becomes the lock set. It fires for `GT 2056 ROYL` on August carrying
no volume — but **any GT with no rim takes this door**.

---

## 4au. `r5_shelf_life` IS THE WRONG NAME, AND THE RIGHT BUCKET CANNOT BE WRITTEN

**CONFIRMED. Code defect, diagnostic only — changes no plan byte, and it is what made 4aq
hard to answer.**

The r5 growth after 4ao is **real new starvation, not relabelling**: a slice-level join shows
**zero** July slices changed label; 13 went from *placed* to `r5_shelf_life` (2,153 t).

But the condition being recorded is **not** shelf life. For every newly-starved r5 slice,
recomputing the R5-legal band from the final schedule:

| month | slices | R5 band width | run duration | **largest contiguous free gap** | slices with a fitting gap |
|---|---|---|---|---|---|
| Jul | 13 | 69.8–71.9 h | 2.12–4.21 h | **0.37–1.20 h** | **0 / 13** |
| Aug | 23 | 69.8–71.1 h | 2.09–3.44 h | **0.47–1.83 h** | **0 / 23** |

`best_gap / duration` p50 = **0.33**. The machines are **90–98 % occupied inside the R5 band**
while running only 70–91 % month-wide. **The band is 70 h wide and the run needs 2–4 h. What
is missing is a CONTIGUOUS hole, not shelf life.** Moving the cure does not help — the band
moves with it.

L7's own docstrings name the right bucket:

- `r5_shelf_life` — *"no gap inside the run's own 72 h band (R5). **Curing must move, not building.**"*
- `machine_busy` — *"every eligible machine was genuinely occupied. **THE ONLY ONE THAT MEANS 'BUY CAPACITY'.**"*

**`machine_busy` is structurally unreachable** — the candidate loop has no early `continue`
before the gate block, so every candidate writes a counter and `before_t0`/`r5` always fire
first. **It appears in 0 of 166 stored `log_l7_pull_release.txt` files.** So the one bucket
that would say *"this is contiguity, buy capacity"* can never be written, and every contiguity
failure is filed under a shelf-life name whose docstring points the reader at curing.

**Second shape of the 4ah defect:** the recorded cause describes the **last machine population
tried**, and `RESCUE_SKIP_HARD` *changes that population*. For **57 % (Jul) / 64 % (Aug)** of
newly-starved volume the r5 refusal was recorded on the **primary/flex rescue machines**, not
the GT's own lock. **Starve histograms are not comparable across this flag.**

### The verdict on the volume — it is not recoverable

The rescue was functioning as a **defragmentation device**, borrowing contiguity from another
rim's differently-shaped calendar. Removing it removes contiguity relief. Dose-response
confirms from the other end: closing `primary` as well takes `r5_shelf_life` starvation
**5,044 → 18,362** and occupancy **83.7 → 81.5 %**.

**−1,076 PCR (Jul) / −5,441 PCR (Aug) is the price of the inch lock and no resequencing
recovers it.** TBR `build_schedule.parquet` is **byte-identical** across the fix on both
months.

---

## 4av. P1 — CLOSING-BUFFER ROWS ARE INVISIBLE TO L7'S OWN FEASIBILITY GATES

**CONFIRMED by direct measurement on the shipped packs. Independent of everything above.**

The closing buffer appends to `bs` but **never writes `busy`**. `busy` is populated only in
`_place` and `_make_room`. Both L7 self-gates iterate `busy`:

- `machine double-booking : 0 PASS`
- `setup not reserved (changeover) : 0 of 1372 transitions PASS`

**Closing-buffer rows are structurally invisible to both.** L11 has no equivalent — it grades
changeover counts and minutes, not gap feasibility.

Measured on `runs/SHIP2_jul` / `SHIP2_aug`, buffer-adjacent different-GT transitions with a
gap under 22 min (the cheapest same-size changeover in the plant):

| | transitions < 22 min | of those **DIFFERENT-size** |
|---|---|---|
| Jul | 14 | **9** |
| Aug | 10 | **6** |

`TBMPCR10Stage2`, July, five consecutive **different-size** changes at **0.0 min gap**:

```
GT 1773 NEO (R13) -> GT 2366 ROYL (R16) -> GT 1503 NEO MSIL (R13)
 -> GT1915 LT XPC1 (R15) -> GT 1894 MAX (R14) ... -> GT 1513 XPC1 MSIL (R13)
```

L7 itself prices a different-size PCR changeover at **42–60 min**. That is **>= 4.2 h of
setup unbooked on one machine**, and the plan's own gate reads **PASS**.

`verify_export.py` does catch the class — it reports *"changeover time not reserved: 15 of
1384"* on July — but nothing in the planner does, so the defect ships and only the
independent verifier sees it.

> **RULE: any code path that appends to `bs` must also write `busy`.** A gate that iterates
> the reservation map cannot see rows that never entered it. Seventh always-passing guard.
>
> **OBEYED 2026-08-21 (§4bf.4).** The buffer now writes `(start, end, gt, rim)` into `busy`
> as well as `bs`, unconditionally. On a plan whose 11 artefacts are byte-identical, the
> gate goes `0 of ~1272 PASS` → `12 of 1284 transitions FAIL (6.9 h short)`. The defect
> itself is fixed behind `PLANNER_L7_BUFFER_SETUP` (default off, −100 PCR tyres, both
> HARD verifier findings to zero).

**Provenance note, same block:** `CLOSING_BUFFER` defaults to `"0"` (*"Default OFF until
measured"*) yet all four SHIP arms printed `[closing-buffer] … PCR +3,717`, so they ran with
it on — and `l11_provenance.json` records only `{run, month, fingerprint}`. **The only evidence
the flag was on is a log line.**

---

## 4aw. MONTH-END COMPLETABILITY IN L5 PLACEMENT — the premise is false, `prefer` is a structural no-op, and the only arm that shrinks the tail destroys 9,604 tyres (2026-08-21)

`PLANNER_L5_MONTHEND_FIT` = `off` (default) | `prefer` | `require` | `split`,
`PLANNER_L5_MONTHEND_WIN_D` (default 7), `PLANNER_L5_MONTHEND_STRICT`.
`planner/cmbc/l5_cure_master.py` — flag block and `_fits` in the placement key.

**The brief:** *"L5 places cure campaigns at earliest-feasible and has no idea that a
campaign ending after `month_end` contributes ZERO to in-month fulfilment."* Attack it with
a completability preference in the last N days.

### 4aw.1 THE PREMISE IS FALSE — a crossing campaign is PRORATED, not zeroed

`l7_pull_release.py`, search `frac_in_month`:

```
frac = 1.0                                if end_ts   <= month_end
     = 0.0                                if start_ts >= month_end
     = (month_end - start_ts) / duration  otherwise
```

Measured on the shipped August plan: of the **57 crossing PCR campaigns (40,997 tyres)
exactly TWO have `frac == 0`**, and both *start* after the boundary. The other 55 already
deliver their in-month share. **L5's own gate line has printed this all along** —
`campaigns crossing month end : 73 -- 46,257 campaign-tyres, only the in-month fraction is
counted`.

The 12,620-tyre PCR tail is the **out-of-month remainder of prorated campaigns**, not a set
of zeroed ones. There is no 40,997-tyre prize here. The recoverable quantity is bounded
above by the tail, and only if the same press-hours can be seated earlier.

### 4aw.2 `prefer` AND `require`(soft) ARE STRUCTURAL NO-OPS — proven by execution

`dur = qty / rate` with `rate` keyed on `(plant, gt_code)`, **not on press**;
`MOULD_LIFE_CYCLES` defaults to 0 and no holiday calendar is configured. So `en == st + dur`
**exactly and identically on every candidate press**. Completability is therefore a
**monotone function of `st`** — the key the greedy already leads on. If the earliest press
cannot finish in-month, none can; if it can, it was already chosen.

`ME_pref7` is **byte-identical to `ME_base` on all ELEVEN run artefacts** (`cure_campaigns`,
`build_schedule`, `gt_events`, `cure_campaigns_reconciled`, `build_starved`,
`build_by_shift`, `cure_by_shift`, `mould_changes`, `l11_invariants`, `carry_forward_gt`,
`carry_out`) at **N = 5, 7 and 10**. The mechanism fires — it detects 63 / 68 / 73 spilling
campaigns — and changes nothing.

> **DO-NOT #46. A preference that is a MONOTONE FUNCTION of the key you already sort on is
> not a preference.** DO-NOT #35 said *ordering a list of one orders nothing*; this is the
> same defect one level up — re-ranking by a derived key that agrees with the existing one
> orders nothing either. Before adding a term to a greedy key, check it is not implied by
> the terms already there.

### 4aw.3 The sweep — fresh arms, `run_arm.py`, all gated FRESH, `PLANNER_L7_CLOSING_BUFFER=1`

**AUGUST 2026 ONLY.** The partition on disk is stamped `2026-08` and was not rebuilt; **no
July arm exists and this is not two-month gated.**

| arm | PCR BUILT | dBUILT | in-month | ful % | tail | starved | L11 |
|---|---|---|---|---|---|---|---|
| `ME_base` | 409,511 | +0 | 397,326 | 92.59 | 12,620 | 12,477 | 32/48 |
| `ME_pref7` | 409,511 | **+0** | 397,326 | 92.59 | 12,620 | 12,477 | 32/48 |
| `ME_split7` | 409,511 | +0 | 397,241 | 92.57 | 12,567 | 12,615 | 32/48 |
| `ME_split5` | 408,795 | −716 | 397,091 | 92.53 | 12,556 | 12,776 | 32/48 |
| `ME_req7` | 399,907 | **−9,604** | 391,819 | 91.30 | **8,783** | 10,917 | 32/48 |

| arm | TBR BUILT | dBUILT | in-month | ful % | tail | starved | R5 max |
|---|---|---|---|---|---|---|---|
| `ME_base` | 98,003 | +0 | 96,932 | 97.89 | 1,508 | 1,654 | 71.7 h |
| `ME_pref7` | 98,003 | +0 | 96,932 | 97.89 | 1,508 | 1,654 | 71.7 h |
| `ME_split7` | 97,990 | −13 | 96,961 | 97.92 | 1,466 | 1,667 | 71.7 h |
| `ME_split5` | 97,990 | −13 | 96,962 | 97.92 | 1,465 | 1,667 | 71.7 h |
| `ME_req7` | 96,529 | −1,474 | 95,577 | 96.52 | 1,389 | 1,775 | 71.3 h |

### 4aw.4 `split` is arithmetically neutral BY CONSTRUCTION — and it is the third split to lose

Cutting at the boundary turns one prorated row into a `frac==1.0` row plus a `frac==0.0`
row **whose quantities are that same proration**. In-month cannot move except by integer
rounding at the cut: the L5-side prorated total moves **6 tyres on PCR, 2 on TBR**. The
press timeline is untouched — **union press-hours 109,675.79 h in both arms**, total placed
qty identical at 522,517.

What it does change is L7's problem: **17 extra campaign rows are 17 extra independent
build releases**. PCR starvation +138 and in-month −85 against TBR +29 — **mixed sign
across plants, fails the gate.**

This is the **third cure-campaign split measured on this engine** (`MAX_CAMPAIGN_H`
−43,104; `l56_loop` −18,129) and the first that is merely neutral rather than catastrophic
— *because it is the only one that does not move the press seats*. It still does not pay.
The standing condition from the `MAX_CAMPAIGN_H` block is now sharper: **a split that keeps
the seats is safe and worthless; a split that moves them is ruinous.**

### 4aw.5 THE TAIL TRAP, IN ONE ROW OF THE TABLE

**`ME_req7` cuts the PCR tail 12,620 → 8,783 — the best tail figure in the sweep — while
destroying 9,604 BUILT tyres and 1.28 pt of in-month.** It refuses 32,146 placed tyres at
L5 and loses on both plants. Read the tail column alone and it is the winning arm.

> Tail tyres are **next month's opening stock, not losses.** The only way to shrink the tail
> without producing less is to seat the work earlier, and the greedy already seats
> earliest-feasible. **Never grade this family on the tail.**

Second trap in the same table: `ME_split7` shows **BUILT-in-month +765 PCR while total
BUILT is unchanged at 409,511** — 765 tyres of build merely crossed the reporting boundary.
That is section 4ak.1's `TAIL_BUILD_PULL` reading. Quote clipped and unclipped BUILT
together or it reads as a free win.

### 4aw.6 It did NOT collapse into `PLANNER_L5_MAX_CAMPAIGN_H`

That flag is a **global span cap applied to every campaign before the sort** (−43,104 PCR
BUILT at 240 h, unfed ×6.7). This is scoped to the boundary, touches only the 12–17
campaigns that cross it, and leaves the press timeline bit-identical. Different lever,
independently measured, same verdict.

### 4aw.7 Verdict

**DEFAULT STAYS `off`.** `prefer`/`require`-soft are proven no-ops and cost nothing to keep;
`split` is neutral-to-negative and mixed-sign; `require`-strict is a 9,604-tyre loss. The
flag is kept gated off with its measurement so the experiment is not run blind again.

**What would actually move the tail** is an earlier seat, and the only lever that creates
one is press CONCURRENCY in the tail — which is the takt governor, already shipped (4am).
Its own decomposition (interior −10,373 against tail +15,051, 33 % new) is the honest
ceiling for this whole family.

---

## 4ax. THE CARRY-OUT TAIL IS STRUCTURAL — the last-week idle press time is NOT reachable

**`schedule-forensics`, 2026-08-21, read-only, per-campaign not aggregate. CONFIRMED.
This is 4aj repeated on the CURE side, and it kills the tail-filling family.
Do not fund a tail-filling experiment.**

### The question and the answer

Press capacity in the final 7 days looks ample — Jul PCR **2,889 h idle (20 %)**, Aug PCR
1,441 h (10 %), Jul TBR 4,306 h, Aug TBR 3,417 h — against carry-out tails of 6,040 / 12,620
/ 861 / 1,508. Nominally it covers 81–100 %.

**It covers almost none of it.** Every crossing campaign re-tested against the run's own press
calendar, restricted to the last 7 plant-days, mould change charged from the **warehouse** copy
of `press_mould_change.parquet`:

| | crossing | **fit wholly inside a last-7-day window** | tail tyres bought |
|---|---|---|---|
| Jul PCR | 26 | **1 of 26** | **7** of 6,164 (0.1 %) |
| Jul TBR | 11 | 2 of 11 | 138 of 949 |
| **Aug PCR** | 57 | **0 of 57** | **0** of 13,494 |
| Aug TBR | 16 | **0 of 16** | **0** of 1,595 |

Widening to 10 days: Jul PCR 1 (7 t), Aug PCR 4 (199 t), TBR 0/0.

### The proof is one fragmentation table

Crossing campaigns need **94–108 contiguous press-hours** plus a 6.0 h mould change:

| | last-7d idle | blocks | **p50 block** | >= 96 h blocks | **>= 96 h AND eligible** |
|---|---|---|---|---|---|
| Jul PCR | 2,889 h | 119 | 15.3 h | 2 (336 h) | **0** |
| Jul TBR | 4,138 h | 121 | 24.2 h | 8 (1,056 h) | **0** |
| **Aug PCR** | 1,441 h | 98 | **6.0 h** | 1 (168 h) | **0** |
| Aug TBR | 3,249 h | 116 | 18.5 h | 7 (755 h) | 3 (332 h) |

**The idle is 98–121 trailing gaps spread over 68–78 presses, and not one long enough sits on
a press the crossing GTs may use.** The binding gate is contiguous window length on an
eligible press — `best_win_h < dur_h` for every blocked campaign. **R3 blocked 0 campaigns on
three of four plant-months.** The obvious widening lever is dead too: `PLANNER_PRESS_FROM_MATRIX=1`
(+6 PCR presses) adds **zero** eligible >= 96 h last-week blocks on all four plant-months.

> **4aj, exactly, one layer up.** There it was 665 idle *building*-machine hours against
> 11,562 unmet tyres, and zero starved slices had an R5 window reaching them. Here it is
> 1,441–2,889 idle *press* hours against the tail, and zero crossing campaigns have a
> contiguous eligible window reaching them. **Aggregate idle is not an answer. Ever.**

### What IS recoverable — and it runs the wrong way

Full gate chain (contiguous eligible press window + R3 as L5 tests it + per-slice build
re-release honouring R5, `cap_machine` and contiguity, jointly):

| | engine tail | feasible campaigns | **tail recoverable** | pt | **required pull** |
|---|---|---|---|---|---|
| Jul PCR | 6,040 | 16 of 26 | **2,053** | 0.52 | **325 h p50** |
| Jul TBR | 861 | 4 of 11 | 219 | 0.22 | 279 h |
| Aug PCR | 12,620 | 17 of 57 | **1,074** | 0.25 | 136 h |
| Aug TBR | 1,508 | 9 of 16 | 730 | 0.74 | 211 h |

**Every feasible placement requires pulling a cure seat 136–325 h (5.7–13.5 days) earlier —
the exact opposite of filling the tail.** The feed side was not the blocker (0/2/1/2 campaigns
failed build re-release).

And pulling seats forward **un-does the takt levelling that bought +9,678 BUILT** (4am). The
governor is live and binding: hourly press concurrency sits at the printed budget for
**66–85 % of the month's hours**, so ~10 PCR and ~14 TBR presses are idle at any instant
**by the level-load's own construction**. 4aw already killed the L5-side version: `require`
cut the Aug PCR tail 12,620 → 8,783 while destroying **9,604 BUILT**.

---

## 4ay. THREE DIFFERENT NUMBERS ARE ALL CALLED "THE TAIL", AND TWO ARE THE WRONG POPULATION

**CONFIRMED. Not in any ledger. I quoted the wrong one repeatedly this session.**

| | `carry_out.parquet` | **engine tail** (`qty_fed − qty_fed_in_month`) | **`carry_forward_gt.parquet`** |
|---|---|---|---|
| Jul PCR | 6,164 | **6,040** | **4,357** |
| Jul TBR | 869 | 861 | 548 |
| **Aug PCR** | 13,344 | **12,620** | **4,514** |
| Aug TBR | 1,584 | 1,508 | 793 |

- `carry_out.parquet` — cure-side **press-state remainder**.
- **engine tail** — the fulfilment figure. Correct for "how much in-month output did we lose".
- **`carry_forward_gt.parquet`** — **the green tyres actually built in-month and left uncured.**
  This is the file that becomes next month's `masters/opening_gt/carryforward_gt_<next>.parquet`.

**On August PCR the gap is 2.8x: 12,620 vs 4,514.** Of the 11,413 tyres of build feeding
post-boundary Aug PCR cures, only **4,514 are built inside the month**; 6,899 are built in the
72 h planning tail and **do not exist in-month at all**. July PCR: 8,305 fed, 4,357 in-month,
3,948 after the boundary.

> **The rule:** quote the **engine tail** for fulfilment. Quote **`carry_forward_gt`** for
> "green tyres sitting on the floor" and for next month's opening stock. Saying *"those
> 12,620 tyres are already built, just cure them"* uses the wrong population by 2.8x —
> two thirds of them are not built yet.

### And "short campaigns seated late" is a count-weighted illusion

| | span p50 (unweighted) | **tail-tyre-weighted mean span** | tail tyres on campaigns > 150 h |
|---|---|---|---|
| Jul PCR | 107 h | **154 h** | 2,834 of 6,164 (**46 %**) |
| Aug PCR | 94 h | **119 h** | 4,508 of 13,494 (**33 %**) |
| Aug TBR | 108 h | **146 h** | 618 of 1,595 (39 %) |
| Jul TBR | 94 h | 84 h | 8 of 949 (1 %) |

The campaign-count median understates the length of the campaigns that carry the tyres.
Blocked durations run to 227 h (Aug PCR) and 731 h (Aug TBR). **Failure mode 1.1 applied to
the premise itself: a count median used to describe a tyre-weighted population.**

---

## 4az. OPEN LEAD — PCR press 190 idle for the first 261 h of July (PLAUSIBLE, unmeasured)

`runs/SHIP2_jul`: PCR press 190 sits idle from `07-01 07:00` to `07-11 20:00` — **261 hours**.
`GT 1916 ROYL` is eligible on press 190 per `cap_press_2026-07`, has 2 moulds, carries **876
tyres of engine tail**, and is not seated until **24 July**. Both its campaigns
(119.1 h + 107.3 h + two 6 h changes = 238 h) **fit inside that 261 h gap.**

Ruled out: `free[190] = t0` (no `masters/carry_in/` file exists, so the carry-in block is a
clean no-op), `floor_ts <= t0 + 11.86 h`, `DAY_CAP` ships off, R3 fallback instrumented at 0
decisions. **The only remaining candidate is the takt push** (`l5_cure_master.py:2426-2431`).

If that is the cause it is the levelling working as designed and is the cost side of 4am.
**But the takt inertness note at `l5_cure_master.py:770-790` is AUGUST-ONLY**, and August PCR
runs 94.5 % press-fleet load with 5 idle presses against **July's 86.2 % and 10 idle** —
**July has twice the room for the governor to bind.** Not testable read-only. Worth one July arm.

---

## 4ba. THE 2-MOULDS-PER-PRESS RULING, APPLIED TO ITS TWO REMAINING CONSUMERS — both fixes LOSE volume, both ship OFF, and one is blocked on a plant question (2026-08-21)

**The ruling, given twice:** *"One press holds 2 moulds (LH + RH). One cycle produces 2 tyres.
Both plants."* `plant_ct.CAVITIES = 2.0` honours the second half. Two consumers never got the
first half; both were built behind default-off flags and measured separately and together.

August only. Partition `INPUT/derived/gt_machine_partition.parquet` stamped **2026-08**, sha1
`809beda91344`, never moved or rebuilt. Baseline `MC_base` = the shipped SHIP2_aug
configuration (`PLANNER_L7_CLOSING_BUFFER=1`), reproduced **to the tyre** — PCR BUILT 409,511 /
in-month 397,326 (92.59 %) / tail 12,620 / starved 12,477 / R5 63.3 h; TBR 98,003 / 96,932
(97.89 %) / 1,508 / 1,654 / 71.7 h; L11 32/48. Every arm fresh via `scripts/run_arm.py`, all
gated FRESH by `check_arm_fresh.py`.

⚠ **The calendar had to be pinned.** `masters/holidays_2026-08.json` was deleted from the tree
by concurrent work mid-session, and the wrapper-root `holiday.csv` carries only a **July** PCR
row — so an unpinned August arm has no plant closure at all, while `runs/SHIP2_aug` was built
with one. Every arm here sets `PLANNER_HOLIDAYS=2026-08-15` (day 15 cures 0 on both plants in
every arm). Without that pin the baseline moves underneath the experiment. **Restore the
holiday master before the next August run.**

### 4ba.1 FIX A — R3 concurrency, `PLANNER_R3_DIV` (ships 1.0 = off)

`cap_mould_<M>.parquet` sets `max_concurrent_presses == moulds` on **100 % of its 110 rows**.
The divisor now lives in **one** module, `planner/cmbc/r3_cap.py`, read by L4.5 (the R5
campaign-length ceiling `concurrency x rate x 72 h`), L5 (placement, split/repair, self-check)
and **L11's grading invariant** — because l11 grading against raw `moulds` while l5 seats
against `moulds/2` would make the invariant pass by construction (§1g, do-not #13).

| arm | | PCR BUILT | dBUILT | ful% | TBR BUILT | dBUILT | ful% | L11 |
|---|---|---|---|---|---|---|---|---|
| `MC_base` | div 1.0 | 409,511 | +0 | 92.59 | 98,003 | +0 | 97.89 | 32/48 |
| `MC_a15` | div 1.5 | 381,726 | **−27,785** | 85.91 | 93,847 | **−4,156** | 92.73 | 31/48 |
| `MC_a20` | div 2.0 | 377,450 | **−32,061** | 84.65 | 94,640 | **−3,363** | 92.99 | 32/48 |
| `MC_aobs` | 2.0 + observed_max | 376,652 | −32,859 | 84.62 | 95,116 | −2,887 | 92.96 | 32/48 |
| `MC_aobs1` | observed_max only | 390,237 | −19,274 | 88.08 | 95,828 | −2,175 | 93.49 | 29/48 |

BUILT and in-month fall together on both plants at every setting — **destroyed capacity, not
relocated output**, which is the correct sign for removing seats the plant does not have. The
loss is concentrated: at div 2.0 six PCR GTs carry −22,195 of the −25,182 cured delta
(`GT 2476 SUP MM` −6,528, `GT T1457 STAR` −5,883, `GT 2258 RAN HPE` −3,864, `GT 1925 XPC1`
−2,846, `GT 1673 NEO` −1,900, `GT 1773 NEO` −1,174); on TBR `GT 5076 - 295/90R20 JDE XF` alone
is −2,617 of −3,524. 35 of 55 placed PCR GTs and 24 of 34 TBR end **at** their cap.

### 4ba.2 TWO MEASUREMENT DEFECTS FOUND WHILE PRICING IT

**(i) `observed_max` is not a concurrency measurement.** `_offline/l2_capability.py` builds it
as `count(DISTINCT press)` **grouped by (plant, gt, date)**, maximised over 8 months — presses
*touched in one plant-day*, not presses *running at one instant*. Our peak concurrency is an
interval sweep. So "the plan exceeds the plant's `observed_max` on Aug TBR 9 GTs / 52,325 tyres
(52.3 %) and PCR 2 / 12,435" — reproduced exactly on `MC_base` — compares **our simultaneity
against the plant's daily press-visit count**. Fifth instance of the denominator class after
§1e, §4d, §4p.1 and §4q.7. On *our* plan the two agree (daily-distinct / simultaneous p50
**1.000**, mean 1.003, both plants) only because our campaigns are long (PCR p50 213 h); the
plant's are short, so for the plant the ratio must be higher — and it is **not measurable from
any committed artefact**, it needs `v_curing`.

**(ii) The divisor contradicts the plant's own history on most of the volume.**
`floor(moulds/2) < observed_max` on **26 of 73 demanded PCR GTs carrying 267,980 of 430,423
tyres (62 %)** and **14 of 37 TBR GTs carrying 52,557 of 100,850 (52 %)**. This is *not* the
`max_horizontal(moulds, observed_max)` reconciliation leaking in: rebuilding the raw count from
`curing_item_mould_mapping 2.csv` x `mould_inv_ctp_17072026.csv` (ACTIVE) shows `moulds` was
raised on only **2 of 73 PCR GTs** (18,309 tyres — `GT 1482 UHL` 2→6, `GT 1856 ROYL` 2→3) and
**0 of 37 TBR**. The raw count still contradicts the divisor.

### 4ba.3 FIX B — the cure rate. THE PER-SKU CURE TIMES WERE ALREADY WIRED IN

The premise "the engine uses `cycle_time_curing.parquet`, keyed on press only, `slots = 4`,
`eff_ct_min` p50 35.8" is **false for the live engine**. That file is read only by diagnostics,
exporters and the retired `_retired/l1_validate.py`; it reaches no live cure rate. The live
path is `plant_ct.press_rate` over `plant_ct_cure_gt.parquet`, built by
`scripts/ingest_plant_cycle_times.py` from the plant's own workbooks and bridged through
`scripts/gt_namespace.py`. It reproduces them exactly — PCR min 10.0 / p25 12.5 / **p50 13.1** /
p75 15.0 / max 22.0 min over 230 GTs; TBR 42 / 49 / **52** / 54 / 60 over 131 — so the per-GT
dispersion the engine "cannot express" has been expressed since 2026-08-10.

Coverage, now **printed every run** (`[cure CT]` in L5's log) instead of silently taking the
plant median: **PCR 73 of 73 demanded GTs, 100 % of volume. TBR 31 of 37, 96,916 of 100,850
(96.1 %)** — six GTs / 3,934 tyres fall back to 2.078 t/press-h, largest `385/65R22.5JUH6`
(1,434), `GT 5114 - 315/80R22.5 JDC XD` (1,118), `385/65R22.5JTL` (742).

**What is actually missing is the availability haircut** — `plant_ct`'s own docstring says it
must be applied there, and it shipped OFF on 2026-08-19 by plant instruction. Volume-weighted
over August requirement (harmonic, because press-HOURS are what is conserved):

| | nameplate t/press-h | plan realised | plant realised p50 |
|---|---|---|---|
| PCR | 7.218 | **6.989** | **6.50** (156/press-day) |
| TBR | 2.103 | **2.033** | **1.83** (44/press-day) |

`PLANNER_PRESS_AVAIL_PCR` / `_TBR` added (ships 1.0/1.0); the single-valued
`PLANNER_PRESS_AVAIL` still overrides both. **Resolution moved to `plant_ct.PRESS_AVAIL`** —
L5 and L4.5 each parsed the env var separately, so a per-plant setting could not have reached
both without a third copy.

| arm | avail | PCR BUILT | dBUILT | ful% | rate | max day | days > 13,854 | TBR BUILT | dBUILT | ful% | rate | L11 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `MC_base` | 1.000 | 409,511 | +0 | 92.59 | 6.989 | 14,465 | **16** | 98,003 | +0 | 97.89 | 2.033 | 32/48 |
| `MC_b96` | 0.96 | 402,815 | −6,696 | 91.07 | 6.707 | 14,423 | 17 | 96,935 | −1,068 | 96.99 | 1.952 | 33/48 |
| `MC_b93` | .930/.900 | 406,023 | **−3,488** | 90.83 | **6.505** | 14,202 | 10 | 96,053 | **−1,950** | 95.62 | **1.832** | 33/48 |
| `MC_bmt` | .8897/.8282 | 403,891 | −5,620 | 88.19 | 6.240 | **13,558** | **0** | 95,987 | −2,016 | 94.02 | 1.688 | 32/48 |

`MC_b93`'s factors were chosen so the **plan's** realised rate lands on the plant's realised
p50; it does — 6.505 and 1.832. `MC_bmt` is the mined MTBF/MTTR pair (PCR mtbf 106.8 h / mttr
13.2 h, 4,267 down events) that shipped until 2026-08-19. **Nothing is defaulted** — a quantile
wired in as a constant is §1.

### 4ba.4 THE DAILY-RATE TEST, AND WHY ONLY THE ILLEGAL ARM PASSES IT

**TBR already passes and always did** — max day 3,477 against the 3,599 record, **0 days over
on every arm**. TBR's rate being 11 % high never produced an impossible day because TBR is not
press-bound. Do not sell a TBR haircut as fixing a problem TBR does not have.

**PCR fails on every arm but one.** Base runs a **flat 14,465/day plateau on 10 days**
(= 86 presses x 24 h x 6.989) and exceeds the plant's 8-month record on **16 of the 30 open
days**. Calibrating the rate exactly onto the plant's p50 (`MC_b93`) still leaves **10** days
over, because the plant does not also keep every press seated 24 h — *the rate was never the
whole constraint*. Only `MC_bmt` clears it.

⚠ **And `MC_bmt` breaches G8.** Its PCR GT-inventory **daily-mean max is 5,129 against the
4,800 rail** (base 4,561), time-weighted mean 3,998 → 4,167, tail 12,620 → **26,816**. Slower
presses drain the buffer more slowly, so the haircut is paid for in green tyres standing: **the
only arm that makes the daily curve physically credible makes the inventory rail illegal.**

⚠ **FIX B makes FIX A's symptom worse.** GTs whose peak concurrency exceeds `observed_max`:
PCR **2 → 5** (12,435 → 97,618 tyres), TBR 9 → 10, at `MC_b93`. A slower press needs more
presses at once for the same demand.

### 4ba.5 INTERACTION — measured, not assumed (§4y.2's method)

| arm | | A alone | B alone | additive | measured | interaction |
|---|---|---|---|---|---|---|
| `MC_ab` div2.0 + b93 | PCR | −32,061 | −3,488 | −35,549 | **−35,613** | −64 (−0.02 %) |
| | TBR | −3,363 | −1,950 | −5,313 | **−4,898** | +415 (+0.42 %) |
| `MC_abo` obs + b93 | PCR | −19,274 | −3,488 | −22,762 | **−13,001** | **+9,761 (+2.4 %)** |
| | TBR | −2,175 | −1,950 | −4,125 | **−3,528** | +597 |

**The divisor form is additive** — two independent constraints, no coupling, exactly like
§4y.2's cap/floor pair. **The `observed_max` form is not**: combined with the haircut it costs
9,761 *fewer* PCR tyres than the parts predict, because a slower press shrinks L4.5's
`max_lot = concurrency x rate x 72 h`, cutting the same volume into more and shorter campaigns
that a per-GT press clamp obstructs less. `MC_abo` is cheap only in company; do not read its
milder loss as evidence the clamp is cheap.

### 4ba.6 R5 — the margin that has to be watched

August TBR base is already 71.7 h against the hard 72 (§4ab's warning). Arms **worsen PCR**:
`MC_aobs` takes PCR to **71.7 h** (base 63.3) and `MC_abo` to 69.4. `MC_ab` is the only
combined arm that improves both (PCR 56.6, TBR 65.0). Any shipping decision here must re-read
R5, not just fulfilment.

### 4ba.7 VERDICT

Both fixes are **default OFF**. Flags-off is byte-identical to the shipped engine — verified on
all ten arm parquets plus the shared `l45_lots_2026-08.parquet`, not assumed.

- **FIX A is directionally right and blocked on a plant question, not an engineering one.**
  Whether `cap_mould.moulds` counts mould HALVES (divisor 2, the loss above is the real price)
  or already counts press-equivalent ASSEMBLIES (divisor 1, today is correct) decides ~8 points
  of PCR fulfilment. The naming evidence favours halves — `mould_inv_ctp` lists `...HMI01` and
  `...HMI02` as separate `Equnr` rows and the plant labels a press load as the pair `HM01#HM02`
  — but it is not conclusive against 62 % of PCR volume where `floor(moulds/2) < observed_max`.
  **ESCALATE.** Do not default a divisor on this evidence.
- **`PLANNER_R3_OBS` must never be defaulted.** It is a mined statistic used as a cap (§1) and
  the statistic is mis-defined (4ba.2(i)). It exists to price the variant, nothing more.
- **FIX B's per-GT cure times were never the defect** — they have been live since 2026-08-10.
  The open question is the availability factor, and the frontier is: 0.930/0.900 costs
  3,488 / 1,950 BUILT and lands the rate exactly on the plant's, but leaves 10 PCR days above
  the plant's record; 0.8897/0.8282 clears the record and breaches the GT rail. **There is no
  setting on this axis that is both credible and legal**, which says the residual is press
  *occupancy*, not press *rate*.

### 4ba.8 WHAT THIS ESTABLISHES

- **Check whether the master you are about to "fix" is even read by the live path.** The
  premise for FIX B named `cycle_time_curing.parquet`; the live rate comes from
  `plant_ct_cure_gt.parquet` and had done for eleven days. Second instance after §4p ("the
  ruling was ALREADY SATISFIED").
- **A daily-distinct count is not a concurrency.** Fifth denominator defect.
- **Report the max DAY, not only the mean rate.** Calibrating PCR's rate exactly onto the
  plant's p50 still leaves 10 days above the plant's 8-month record, because a rate and an
  occupancy are two constraints and only one was being priced.

---

## 4bb. THE GT/RIM BUILD-FEED CEILING — the gate BINDS, is NOT redundant with takt, and its entire measured effect is GREEDY JITTER. SHIPS OFF (2026-08-21)

`PLANNER_L5_FEED_CEIL` = `0` (default) | `1`, with `PLANNER_L5_FEED_W_H` (72),
`PLANNER_L5_FEED_SLACK` (1.0), `PLANNER_L5_FEED_PLANTS` (`PCR`).
`planner/cmbc/l5_cure_master.py` — flag block, `_feed_free` / `_feed_commit`, consulted in
the placement loop directly after takt.

**The brief:** L5 seats more concurrent presses on a rim than the eligible building machines
can feed in that window; the surplus is nominally scheduled and starves later in L7. August
PCR starvation is 12,477, of which 55 % `release_before_t0` and 40 % `r5_shelf_life`, and
§4au already proved both labels mean building-machine contention.

Every term is derived at run time from the month's own masters — eligible machines from the
partition + home + rim-lock set L7's `_locked` actually enforces (never `cap_machine`, ~3x
wider), a machine serving two rims apportioned by its own booked share (DO-NOT #44), cure
draw from `plant_ct.press_rate`, stock from `early_budget` (already R5-filtered). **No mined
constant anywhere** (§1).

### 4bb.1 It binds, and it is not the takt governor re-implemented

Both of the questions the work was gated on are answered **yes**:

- **Binding.** Recomputed on the realised plan the ceiling is violated in **37.5 % of 72 h
  windows**, and **R16 — the rim carrying the most starvation (4,363) — binds in 43** of them.
  Not vacuous (DO-NOT #30).
- **Not redundant with takt.** Every arm ran with the shipped takt governor ON. The L5 log
  shows PCR budgeting **exactly one partition, `PCR ALL` (81 of 86 presses)** — because
  `TAKT_PART_PLANTS` defaults to `TBR`, PCR's takt carries **no rim term at all**. The ceiling
  moved seats takt had already placed. The two constrain different resources on different keys.

**A first version of this diagnostic was wrong and is worth recording.** It resolved
eligibility from the partition alone, so **22 of 55 PCR GTs carrying 29,458 tyres contributed
cure DRAW with ZERO capacity** and every rim they touched read as over-seated (nominal binding
17.1 %, apportioned 27.2 %). L7 does not do that — it falls back to `home_of` + the rim lock.
**Sixth instance of the denominator class** after §1e, §4d, §4p.1, §4q.7, §4ba.

### 4bb.2 The sweep — fresh arms, `run_arm.py`, all gated FRESH, `CLOSING_BUFFER=1`, `HOLIDAYS=2026-08-15`

TBR is **byte-identical in every arm** (`FEED_PLANTS` defaults to PCR).

| PCR, demand 429,146 | BUILT | dBUILT | in-month | ful% | starved | R5 | L11 |
|---|---|---|---|---|---|---|---|
| `FC_base` | 409,511 | +0 | 397,326 | 92.59 | 12,477 | 63.3 h | 32/48 |
| W=72 slack 1.00 | 407,568 | **−1,943** | 393,048 | 91.59 | 11,265 | 60.8 h | 33/48 |
| W=24 slack 1.00 | 410,892 | +1,381 | 397,473 | 92.62 | 10,614 | 65.8 h | 32/48 |
| W=72 slack 1.25 | 410,314 | +803 | 398,135 | 92.77 | 11,580 | 63.3 h | 32/48 |
| W=72 slack 1.50 | 409,511 | +0 | *inert — never binds* | | | | |

### 4bb.3 THE NULL CONTROL — this is the whole result

At **fixed slack 1.25**, varying only the window width. One hour, against a 72 h shelf life,
is a parameter change with **no physical meaning at that resolution**:

| W (h) | 68 | 70 | 71 | **72** | 73 | 76 |
|---|---|---|---|---|---|---|
| dBUILT | +826 | +841 | +271 | **+803** | +60 | **−54** |
| dstarved | −825 | −854 | −220 | **−897** | −25 | **+30** |

**mean +458, sd 414, range −54..+841.** A one-hour change swings BUILT by 743 tyres and
**flips the sign of the starvation delta**. The best single arm sits **inside one sd of the
mean of physically equivalent settings**.

The slack knob is no better: 1.18 → +53, 1.20 → **+1,457**, 1.22 → **−33**, 1.25 → +803,
1.28 → inert. **Not monotone**, and moving *fewer* seats (3 at slack 1.22) is **worse** than
moving more (8 at 1.20).

**The direct action is tiny; the cascade is everything.** At slack 1.25 the ceiling moved
**one seat**, and **33 campaign starts moved** in response. The +803 is the greedy
re-settling, not 897 tyres of prevented starvation. Same signature §4am named for `ALPHA`.

### 4bb.4 What it refuses where it "wins"

A ceiling refuses seats, so the refusal must be reported beside the gain. In-month cure per
GT, base → arm:

| arm | gained | refused | net |
|---|---|---|---|
| W=72 slack 1.25 | 1,153 on **6** GTs | **344 on 3** GTs | +809 |
| W=24 slack 1.00 | 4,817 on 17 GTs | **4,670 on 26** GTs | **+147** |

The W=24 arm **churns 9,487 tyres of cure across 43 GTs to net 147** — a coin toss with a
large variance, which is what the null control independently says it is.

Robustness (dBUILT PCR) — survives all three baselines, and it does not matter, because the
null control shows the same arm's *neighbours* do not:

| baseline | W=24 s1.00 | W=72 s1.25 |
|---|---|---|
| shipped | +1,381 | +803 |
| `CLOSING_BUFFER=0` | +2,146 | +897 |
| `LOT_INTERVAL_H=8` | +950 | +421 |

W=24 turns **negative on in-month** on the `LOT_INTERVAL_H=8` baseline (−1,019, −0.23 pt)
while BUILT rises — **BUILT and in-month at different tiers, which fails the scoring rule on
its own**.

### 4bb.5 WHY THE VOLUMETRIC PREMISE IS WRONG — the transferable lesson

Month-wide, **every PCR rim except R12 has feed headroom**: R13 needs 135,871 against 158,472
feedable, R16 37,017 against 49,253. The shortfall is **not volume, it is CONTIGUITY** —
§4au measured the eligible machines at **90–98 % occupancy inside the R5 band** while running
70–91 % month-wide, largest free gap **0.47–1.83 h against a 2–4 h run**.

**A tyres/hour ceiling cannot see a hole-shape problem.** It refuses seats that were feasible
and leaves the fragmented ones exactly where they were. **R13 is the proof: it starves 2,265
tyres and the ceiling never binds on it** (worst excess −223, min free 10.6 machine-h).

The verifier's 2 pre-existing hard violations are unchanged in class (changeover-not-reserved
12/1269 → 11/1268; machine-day over 24 h 2 of 597 → 2 of 599, worst 25.17 → 25.75 h). **No new
violation class** (§4am).

**AUGUST ONLY — no July arm exists** (the partition on disk is stamped 2026-08 and must not be
rebuilt). Even had the response been clean, a knob validated on one month is not validated.

### 4bb.6 Verdict

**SHIPS OFF, and no value is selectable.** Picking the argmax of this sweep would be the
mined-constant defect (§1) with the added insult that the response is not even monotone.
Kept in the code gated off, with the numbers, because deleting a rejected experiment destroys
the evidence and invites a blind re-run.

> **DO-NOT #49: run a NULL CONTROL before believing a scheduler gain.** Perturb a parameter
> that cannot physically matter — a 1 h change in a 72 h window — and measure the spread. If
> the spread is the size of your effect, you measured the greedy, not your mechanism. Three
> baselines all agreeing is **not** this test: they resample the same jitter point. §4am
> caught it on `ALPHA` by luck of a fine sweep; this makes it the standing procedure.

---

## 4bc. TARGETED STARVATION REPAIR BY EXCHANGE — FEASIBILITY MEASURED, NOT BUILT. The premise holds and the noise floor is now known (2026-08-21)

`scripts/_diag_exchange_feasibility.py` — diagnostic only, changes no plan byte.

**The premise under test:** gap-search for starved campaigns finds nothing because the useful
contiguous capacity is *occupied by another flexible campaign*, so only an EXCHANGE can reach
it. Tested before writing the exchange, per DO-NOT #30.

Method, on `runs/SHIP2_aug`, August PCR, 80 starved rows / 12,477 tyres: eligible machines
resolved as L7's `_locked` does (partition → home → rim lock, never `cap_machine`); the R5 band
is `[t_cure − 72 h, t_cure − tau_min]` **clamped to t0**; inside it we allow **evicting any one
occupying run** — the most generous exchange conceivable — and ask whether a contiguous window
≥ the run's own build duration then exists.

| setup allowance | fits NOW (gap-search) | fits NOW + one eviction | fits at a LATER slot + eviction |
|---|---|---|---|
| 0 min | 21/80 — 3,314 | 67 — 10,678 | 80 — 12,477 |
| 28 min | 18/80 — 2,848 | 67 — 10,678 | 80 — 12,477 |
| **42 min** | **18/80 — 2,848** | **62 — 9,898** | **71 — 11,087** |
| 60 min | 16/80 — 2,530 | 61 — 9,739 | 68 — 10,628 |

**THE PREMISE HOLDS.** At a realistic 42 min different-size setup allowance, eviction takes the
geometrically reachable population from **2,848 tyres to 9,898** — a 3.5x difference, and it is
robust across the whole setup sweep. The blocking resource really is *occupancy by another
campaign*, not absence of hours.

**Two clamps that each change the answer, both instances of known defect classes:**
- **Not clamping the band to t0** reports 39/80 fitting instead of 21/80 — it counts pre-horizon
  hours the plan may never use as free capacity. That *is* the `release_before_t0` population
  (6,810 tyres) re-labelled as available. DO-NOT #44, one layer along.
- **Not charging setup** moves the gap-search figure 3,314 → 2,848. A gap equal to the run
  length is not a placeable gap.

**This does NOT contradict the "0 of 57" gap-search result** — that measured *crossing* campaigns
against *last-week* windows; this measures all starved rows across their own R5 band. Different
populations, different questions.

### 4bc.1 WHAT IS NOT MEASURED, AND THE BAR ANY BUILD MUST CLEAR

The table is an **upper bound on geometry only**. It does not check the other side of the swap
(can the evicted run be re-placed legally?), R3 mould concurrency, the GT WIP rail, press
eligibility for the moved cure, or whether the resulting plan BUILDS more.

**And §4bb.3 set the bar:** the null control there measured the August-PCR arm-level noise floor
at **sd 414 tyres, range −54..+841 BUILT across six physically equivalent settings**. Any
exchange pass returning less than roughly ±800 BUILT on one month **cannot be distinguished from
greedy re-settling**. An exchange that accepts moves on a *local* proxy (starvation down, no new
hard violation) and is then graded on a *global* re-plan is exactly the shape that produces a
number inside that band. Build it with the null control attached, or the result will not be
readable.

**NOT BUILT this session** — the feasibility is established and the measurement bar is now known;
the pass itself is the next piece of work.

---

## 4bd. OPENING GT STOCK THAT EXPIRES UNUSED — the loss is REAL and now REPORTED; the L5 fix hits every objective and its volume is NOISE. SHIPS OFF (2026-08-21)

`planner/cmbc/l5_cure_master.py` — `PLANNER_L5_STOCK_URGENT` (`0` ships | `1` | `alpha` | `qty`)
and `PLANNER_L5_STOCK_URGENT_MINQ` (`0`).
`planner/cmbc/l7_pull_release.py` — the `OPENING STOCK THAT EXPIRES UNUSED` block.
`planner/cmbc/l11_validate_plan.py` — `{plant} opening stock expired on a planned GT`.

**The defect.** The month opens with green tyres on the floor. They are loaded, partly
consumed, and the remainder silently expires. L7 printed `opening stock consumed` and nothing
else.

| | held at t0 | consumed | **EXPIRED** |
|---|---|---|---|
| Jul PCR | 4,820 | 3,951 | **869** |
| Jul TBR | 1,297 | 855 | **442** |
| Aug PCR | 5,132 | 3,453 | **1,679** |
| Aug TBR | 1,266 | 794 | **472** |

**3,462 tyres over two months, none of it aged out on arrival** — August age p50 6–15 h, max
55.9 h, **zero rows over 72 h**. It dies because its GT's first cure campaign is 273–729 h
away, and L5's seat queue is `(plant, _late, -qty, gt_code, seq)` with `_late` constant 0 —
**nothing in the key knows stock exists.**

### 4bd.1 THE POPULATION IS 40 % SMALLER THAN THE HEADLINE — decompose before fixing

The number that matters is not 1,679. Split on the realised `SHIP2_aug` plan:

| August | expired | **no cure campaign this month** | first cure past its own shelf life | other |
|---|---|---|---|---|
| PCR | 1,679 | **656 on 7 GTs** | 1,014 on 5 GTs | 9 |
| TBR | 472 | **240 on 5 GTs** | 232 on 5 GTs | 0 |

Stock on a GT the month does not cure at all is a **DEMAND fact** — there is nothing to pull
and no placement change can reach it. The **addressable** figure is **1,023 PCR / 232 TBR**,
and TBR's is a quarter of the ±800 noise floor before any work starts. Quoting 2,151 as a
scheduling opportunity overstates it by 42 %. Seventh instance of the denominator class.

### 4bd.2 THE "EARLY SLICE" DESIGN IS REFUTED TWICE, BEFORE ANY CODE (DO-NOT #30)

The natural design — seat a slice sized to the stock and leave the remainder where it is —
cannot be built legally on August:

* **B12.** The August cure-lot floor is **PCR 311.5 / TBR 85.7**. Ten of the eleven
  addressable GTs hold **less than the floor** (PCR 167/91/87/9/8, TBR 63/61/39/35/34). A
  stock-sized slice is a sub-floor campaign *by construction*. Only `GT  T1457 STAR` (661)
  clears it.
* **Geometry.** Largest contiguous free window on **any** eligible press inside the stock's own
  remaining life, charging that press's own mould change on both sides:

  | GT | needs | best window | eligible presses free in the band |
  |---|---|---|---|
  | `GT  T1457 STAR` | 57.1 h | **7.4 h** | **0 of 8** |
  | `GT2776 RAN AT` | 25.7 h | **7.4 h** | 0 of 17 |
  | `GT 1482 UHL` | 11.8 h | **7.4 h** | 0 of 12 |
  | `315/80R22.5JUL4` (TBR) | 31.4 h | 50.7 h | 4 of 25 — **fits** |

  August PCR presses run **94.5 % occupied**; allowing the stock to be drunk by several
  presses in parallel up to the R3 cap does not change the answer. Total reach of a
  no-eviction repair pass: **9 PCR + 171 TBR tyres**, and **0 once B12 is applied**. Taking an
  occupied window needs an **eviction**, which is §4bc and is a different, unbuilt change.

**This is the opposite of the July picture** (10 of 86 PCR presses free for the whole first
72 h). A lever sized on July's press calendar would have been built and would have found
nothing. **Re-measure the geometry on the month you are planning.**

So the only legal shape is a **reorder**: it does not need a free window, it takes the seat by
displacement. That is what was built.

### 4bd.3 `PLANNER_L5_STOCK_URGENT` — the seat queue made GT-inventory-aware

Promotes, per GT holding usable opening stock, the **fewest presses that can drink that stock
inside its own remaining shelf life**:
`n = min( ceil(stock / (life_h x that GT's press rate)), moulds (R3), eligible presses )`,
with `life_h = 72 − median age of that GT's own stock`, derived at run time (§1). The head is
ordered **soonest-to-expire first**; `-qty` is prefixed for the promoted jobs only and is
untouched for every other job. On August: **24 PCR campaigns on 19 GTs, 20 TBR on 20 GTs.**

Not §4z. `STOCK_FIRST` sized the promotion on **seat count** (`stock // gap_q`, gap_q ≈ 86
tyres on PCR, so a GT holding 661 bought up to 5 seats); this sizes it on the stock's own
**draw time** and buys 2. That is the direct answer to §4z's failure mode.

### 4bd.4 The measurement — fresh arms, `run_arm.py`, all nine FRESH, partition `809beda91344`, `HOLIDAYS=2026-08-15` pinned on every arm

`SP_base` reproduces `SHIP2_aug` **byte-identically on all seven plan parquets**.
`SP_null` (flag off, all three files edited) is **byte-identical to `SP_base` on all ten**.

| PCR, demand 429,146 | BUILT | dBUILT | in-month | ful% | stk consumed | expired | **addressable** | starved | tail | R5 | L11 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `SP_base` off | 409,511 | +0 | 397,326 | 92.59 | 3,453 | 1,679 | 1,023 | 12,477 | 12,620 | 63.3 h | 32/48 |
| `SP_su_1` life-order | 410,089 | **+578** | 399,655 | 93.13 | 4,476 | 656 | **0** | 10,755 | 11,539 | 67.4 h | 32/50 |
| `SP_su_alpha` gt-order | 404,787 | **−4,724** | 395,596 | 92.18 | 4,389 | 743 | 87 | 15,040 | 10,557 | 64.6 h | 32/50 |
| `SP_su_qty` −qty-order | 407,325 | **−2,186** | 397,623 | 92.65 | 4,389 | 743 | 87 | 13,589 | 10,704 | 69.5 h | 32/50 |
| `SP_q8` minq=8 | 408,243 | **−1,268** | 398,568 | 92.87 | 4,468 | 664 | 8 | 12,035 | 10,750 | 69.0 h | 31/50 |

| TBR, demand 99,019 | BUILT | dBUILT | in-month | ful% | stk consumed | expired | addressable | starved | tail | R5 | L11 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `SP_base` off | 98,003 | +0 | 96,932 | 97.89 | 794 | 472 | 232 | 1,654 | 1,508 | 71.7 h | 32/48 |
| `SP_su_1` life-order | 97,265 | **−738** | 97,415 | 98.38 | 1,026 | 240 | **0** | 2,276 | 519 | 66.7 h | 32/50 |
| `SP_su_alpha` gt-order | 97,368 | **−635** | 97,361 | 98.33 | 1,026 | 240 | 0 | 2,080 | 676 | 71.7 h | 32/50 |
| `SP_su_qty` −qty-order | 96,813 | **−1,190** | 96,766 | 97.72 | 1,026 | 240 | 0 | 2,412 | 716 | 65.4 h | 32/50 |
| `SP_q3` / `SP_q8` | 96,992 | **−1,011** | 96,835 | 97.79 | 1,026 | 240 | 0 | 2,362 | 826 | 68.3 h | 32/50 · 31/50 |

Starvation cause vector, PCR base → `SP_su_1` (DO-NOT #43 — diff the whole vector):
`release_before_t0` 6,810 → 6,477, `r5_shelf_life` 5,044 → 3,652, `gt_wip_rail` 623 → 626.
TBR base → `SP_su_1`: `release_before_t0` 1,341 → **1,951**, `below_min_lot` 226 → **325**,
`r5_shelf_life` 87 → 0. **TBR starvation rises 37 % and it is the pre-t0 release bucket** —
the §4z signature, one lever along.

Secondary KPIs, `SP_base` → `SP_su_1`: weighted setup **PCR 458.6 → 496.1 h (+8.2 %)**, TBR
94.2 → 96.3 h; PCR same-size 70.5 → 70.2 %, TBR 100.0 → 100.0 %; GT inventory time-weighted
mean PCR 3,972 → 4,047 and **daily-mean max 4,522 → 4,533 against the 4,800 rail**, TBR
1,104 → 1,100 and **1,313 → 1,323 against 1,400**; sub-floor run share **0.0 % on both plants
in every arm**; lot p50 PCR 318 → 316, TBR 122 → 111; press occupancy PCR 83.7 → 83.9 %.
`verify_export.py` reports **the same two pre-existing hard violations and no new class**
(changeover-not-reserved 12/1269 → 11/1365, machine-day over 24 h 2 → 1, worst 25.17 → 24.13 h).

### 4bd.5 THE NULL CONTROL — this is the whole result

`alpha` and `qty` promote the **identical set of campaigns** and differ only in the order of
that set **among itself**. They collect the same stock (expired 743 vs 656 PCR; **240 on TBR
in all three**), so the ordering term cannot be carrying the objective. PCR `dBUILT` across
the five settings:

| setting | `1` / `minq=2` / `minq=3` | `minq=8` | `qty` | `alpha` |
|---|---|---|---|---|
| dBUILT PCR | **+578** | **−1,268** | **−2,186** | **−4,724** |

**mean −1,074, sd 2,059, range 5,302.** The +578 sits inside one sd of the mean of settings
that are equivalent on the stated objective, and it is itself below the ±800 August-PCR floor
(§4bb.3).

**And the sharpest one: `MINQ=8` drops ONE GT holding EIGHT tyres of stock** — 0.16 % of the
PCR opening floor, 2.6 % of the 311.5-tyre B12 cure floor, a quantity that cannot legally
constitute anything — **and PCR BUILT moves +578 → −1,268, a 1,846-tyre swing that flips the
sign**, with L11 32 → 31. Dropping the **3-tyre** TBR GT (`MINQ=3`) moves TBR in-month
+483 → −97. There is no physically meaningful reading of an 8-tyre input change worth 1,846
tyres of output. **DO-NOT #49, confirmed on a second, independent lever.**

### 4bd.6 Three further readings

**The objective and the volume are decoupled — do not use the first as evidence for the
second.** Addressable expired stock goes to **0 on both plants in every arm**, including the
two that destroy 2,186 and 4,724 tyres.

**`+2,329` PCR in-month is not 2,329 tyres.** It decomposes as +578 BUILT + 1,023 extra
opening stock consumed + 1,081 of carry-out tail pulled inside the boundary (12,620 → 11,539).
Read on in-month alone this is +0.54 pt and would be one of the best August arms of the project.

**TBR BUILT is negative in every arm** (−635 … −1,190), against PCR's best +578. Mixed sign
across plants fails the ship gate on its own (DO-NOT #14). **Sixth loss in the early-release
family** after §4z, `WARM_RELEASE`, `FLOOR_BASIS`, `CHG_PARALLEL`, `T0_STOCK_BASIS=lot`.

### 4bd.7 What DID ship — the reporting, unconditionally

**The loss was never printed, and the one line that existed for it could not fire.** L7's
"never drawn" guard tested `_og_tot - _og_used > 0.5` where `_og_tot` is the **decremented
residual** (i.e. the unused stock itself) and `_og_used` is the consumption — so it asked
*"is the leftover bigger than the draw?"* and answered **no on every plant-month this project
has ever run** (1,679 vs 3,453). **Fifth always-passing guard** after `l4b_capacity_flow`
(§4al), the L4.5 R5 gate (§4ai), B16's one-sided feasibility (§4k) and the staleness warning
(§4o). The neighbouring `_og_left` in the CARRY/LEDGER reconciliation carried the same
expression under a `max(0.0, …)` and was reading 0 by arithmetic accident, not because undrawn
stock is genuinely outside the ledger — which it is, and is now stated at the site.

Shipped ON, in the base plan:

* **L7** prints `OPENING STOCK THAT EXPIRES UNUSED` — held / drawn / expired per plant, the
  cause split above, and the top GTs with their remaining life and their first cure time.
* **L11** gains `{plant} opening stock expired on a planned GT`, target **0 tyres**, graded on
  the **addressable** population only. It is neither vacuous nor unwinnable: every GT in it has
  a campaign, so the only thing wrong is *when*. August base **FAILS at 1,023 PCR / 232 TBR**;
  `SP_su_1` **PASSES both**. L11 is therefore **48 → 50 invariants**, base 32/48 → **32/50**,
  with **zero status flips on the 48 pre-existing ones**.

### 4bd.8 Verdict

**`PLANNER_L5_STOCK_URGENT` ships `0`, and no value of it is selectable** — picking the argmax
of this sweep would be the mined-constant defect with the added insult that an 8-tyre input
change flips the sign. Kept in the code with its numbers, because deleting a rejected
experiment destroys the evidence and invites a blind re-run. **AUGUST ONLY — the partition on
disk is stamped 2026-08 and must not be rebuilt; no July arm exists, and none should be read
across from §4z, whose baseline (pre-takt-governor, pre-closing-buffer) no longer exists.**

The reporting and the invariant ship ON regardless, because the 1,255 addressable tyres are a
real loss and the reason nobody fixed them for two months is that nobody could see them.

> **DO-NOT #50: an opening-stock recovery metric is not a volume metric.** Every arm here
> drove addressable expired stock to zero, and three of the four destroyed BUILT. Collecting a
> perishing tyre and producing a tyre are different events; grade the second. And before
> costing unused opening stock as an opportunity, **subtract the stock sitting on GTs the
> month does not cure at all** — 40 % of it on August, and no scheduler can reach that half.

---

## 4bf. THE PLAN WAS NOT PHYSICALLY EXECUTABLE — one unbooked changeover produced BOTH hard violations, and the fix costs 100 tyres (2026-08-21)

`PLANNER_L7_BUFFER_SETUP` (l7, default `0`), plus two GRADING fixes that ship
unconditionally: the L11 machine-day denominator and R5 at the first tyre.

**The user's words:** *"machine work more than 24 hr is not possible — how are these
issues coming"*, and *"changeover time not reserved"*. Both are real, both are the
same defect, and `scripts/verify_export.py` has been saying so on every shipped
pack while the planner's own gates printed PASS.

### 4bf.1 PRODUCTION ALONE IS NEVER OVER 24 h — IT IS EXACTLY AT 24 h

Clipping every build slice's overlap with each plant-day (07:00 → 07:00), on
`MD_base2` (= the `SHIP2_aug` configuration, reproduced fresh):

| | machine-days | production alone > 24 h | max production |
|---|---|---|---|
| Aug | 617 (`build_schedule`, both plants) | **0** | **24.00 h** |

L7 packs a machine to **exactly 24.00 h of production** and the changeovers have
nowhere to go. Production + setup then breaches on PCR: **3 of 343 machine-days,
total excess 4.22 h, worst `TBMPCR2Stage2` day 31 = 26.00 h** (22.53 h production
plus 3.47 h of setup). TBR: 0.

### 4bf.2 ONE CAUSE, AND IT IS §4av's — THE CLOSING BUFFER CHARGES NO SETUP

`_place`'s backward walk already pads both sides by `_setup_s`, so nothing it
books can breach. The closing buffer does not go through `_place`: it scans
`bs` for idle gaps and fills them **edge to edge**, charging no changeover at
either end.

On August, **all 12 unreserved transitions (11 PCR, 1 TBR) are buffer-adjacent,
and all 3 over-24 h machine-days are buffer-filled days** — every one of them on
30/31 Aug or 1 Sep, inside the 66 h buffer window. 10 of the 12 have a gap under
0.5 min, 6 have a gap of exactly 0.0 min, and 8 are DIFFERENT-size transitions
that L7 itself prices at 42–60 min:

```
TBMPCR10Stage2  GT 1634 XPC TATA -> GT 1513 XPC1 MSIL   gap 0.0 min, needs 42
TBMPCR2Stage2   GT 2258 RAN HPE  -> GT 2247 LEVI        gap 0.0 min, needs 60
TBMPCR1Stage2   GT2776 RAN AT    -> GT  T1457 STAR      gap 0.0 min, needs 60
```

**RESERVING THE SETUP *IS* THE 24 h MACHINE-DAY BUDGET.** Once every production
interval and every changeover interval on a machine is disjoint, their total
inside any 24 h window is ≤ 24 h by construction. There is no second constraint
to add, and a separate day-budget would be the duplicated-cap defect of §1g.

### 4bf.3 THE FIX, AND ITS PRICE

`_occ` now carries the GT with each interval (holidays get the `_HOL_GT`
sentinel, priced at zero — a shutdown is not a changeover), each gap carries the
GT on its left and its right, and under the flag the gap is shrunk by
`_setup_s(prev → g)` and `_setup_s(g → next)` before anything is sized into it.

Fresh arms, `run_arm.py`, all gated FRESH, partition stamped 2026-08,
`PLANNER_L7_CLOSING_BUFFER=1` + `PLANNER_HOLIDAYS=2026-08-15` on every arm.

| PCR | BUILT | dBUILT | in-month | md > 24 h | excess | unres CO | R5 1st | L11 |
|---|---|---|---|---|---|---|---|---|
| `MD_base2` | 409,511 | +0 | 397,326 | **3 / 343** | **4.22 h** | **11 / 791** | 65.58 h | 31/52 |
|  |  |  |  | worst 26.00 h (`TBMPCR2Stage2` d31, 22.53 prod + 3.47 setup) |  |  |  |  |
| `BUFFER_SETUP=1` | 409,411 | **−100** | 397,326 | **0** | **0.00** | **0** | 65.58 h | 31/52 |

| TBR | BUILT | dBUILT | in-month | md > 24 h | unres CO | R5 1st | L11 |
|---|---|---|---|---|---|---|---|
| `MD_base2` | 98,003 | +0 | 96,932 | 0 / 274 | **1 / 505** | 73.45 h | 31/52 |
| `BUFFER_SETUP=1` | 98,003 | **+0** | 96,932 | 0 | **0** | 73.45 h | 31/52 |

`scripts/verify_export.py`, which imports no planner code:

```
base   VERDICT: plan is NOT physically executable (2 hard violation(s))
         changeover time not reserved: 12 of 1269 transitions (6.9 h short)
         machine-day over 24 h: 2 of 597, worst TBMPCR10Stage2 day 30 = 25.17 h
arm    VERDICT: plan is physically executable (0 hard violation(s))
         all 597 machine-days fit 24 h incl. setup  OK (max 24.00 h)
```

**THE −100 IS NOT A CASCADE, AND THAT IS THE POINT.** 5,631 of the 5,646
`build_schedule` rows are IDENTICAL between the arms; only the 15 buffer runs
move, and the PCR buffer total goes 3,018 → 2,918. in-month, tail, starvation
and its whole cause vector, R5, same-size, weighted changeover, GT time-weighted
mean (4,150 → 4,144) and daily-mean max (4,620, unchanged), and all 52
invariants are identical. TBR pays nothing — its four buffer gaps had the slack
to absorb one 10 min reservation.

**AND THE SIGN IS NOT MEASURABLE ANYWAY.** A null control run beside it —
`PLANNER_LOT_INTERVAL_H` perturbed by **36 seconds** in a 16 h release grid —
swung PCR BUILT **0 … +706, mean +235 sd 365**. −100 is deep inside that.
So this is **not offered as a volume result**. The result is two HARD violations
going to zero on an independent verifier; the 100 tyres are the receipt.

### 4bf.4 §4av's RULE, NOW OBEYED — the buffer writes `busy`

The buffer now appends `(start, end, gt, rim)` to `busy` as well as to `bs`,
**unconditionally, not under the flag**. Nothing downstream reads `busy` except
L7's own two gates, so this cannot move a tyre — it only stops them lying:

```
before   setup not reserved (changeover) : 0 of ~1272 transitions  PASS
after    setup not reserved (changeover) : 12 of 1284 transitions  FAIL (6.9 h short)
```

on a plan whose 11 artefacts are byte-identical. Seventh always-passing guard,
now the sixth repaired.

---

## 4bg. TWO GRADING DEFECTS FOUND IN THE SAME PASS — both flip a gate, neither is behind a flag (2026-08-21)

### 4bg.1 THE MACHINE-DAY DENOMINATOR WAS THE WALL-CLOCK DATE

`l11_validate_plan.py`, the `mdays` line:
`bp.with_columns(pl.col("start_ts").dt.date())`. The plant day is 07:00 → 07:00
and every exported sheet buckets on `plant_day`; `.dt.date()` is the calendar
day, which splits the C shift in two. This file's sibling
(`export_shift_schedule.py`) carries the docstring recording that wall-clock
labelling once mislabelled **28.7 %** of build rows.

```
machine-days     PCR Jul  TBR Jul  PCR Aug  TBR Aug
calendar (old)      351      281     *355     *283
plant-day (now)     345      278     *343     *272
```
`*` re-measured this session on a fresh August arm. **The July column is quoted
from the forensics report and was NOT re-run** — no July arm exists, the
partition on disk is stamped 2026-08 and this session was instructed not to
rebuild it. Same caveat applies to the July column of the R5 table below.

Understated **1.7–4.0 %** on all four cells, so every `per machine-day` rate was
overstated in our favour. **IT FLIPS A GATE:**

| August | on 355 calendar days | on 343 plant-days | cap |
|---|---|---|---|
| PCR WEIGHTED build changeover min/machine-day | **73.6 PASS** | **76.2 FAIL** | 74.0 |

The shipped August pack's *"32 PASS of 50"* is really **31**. Not behind a flag:
a denominator is either the one the rest of the pack uses or it is wrong.

### 4bg.2 R5 WAS GRADED AT THE SLICE END, SO IT NEVER SAW THE FIRST TYRE

`wait_h` is `cure_ts − end_ts`, the wait of the **last** tyre off the drum. A
slice is built continuously, so its **first** tyre waits `wait_h + slice hours`
— and the first tyre is the one that expires.

| grade at | Jul PCR | Aug PCR | Aug TBR |
|---|---|---|---|
| slice end (old) | 71.23 | 63.27 | 71.71 |
| first tyre | **74.59** | 65.58 | **73.45** |

**118 PCR (Jul) and 26 TBR (Aug) tyres are past the 72 h shelf life and the gate
could not see any of them.** 0.03 % of volume — not a blocker, and a passing
check that is not a correct check. L11 now grades `cure_ts − start_ts` and
publishes the pro-rated **tyre count** beside the max, because a max cannot be
acted on. L7's own gate print was fixed the same way. L11 goes 50 → 52
invariants; the same August plan is **31 of 52**, not 32 of 50.

The same error sits one level down in `_place`, whose R5 test offsets by
`cums[j]` (the slice END). Fixing it changes the PLAN, so it is behind
`PLANNER_L7_R5_FIRST_TYRE`, **default off**, and it is measured below.

### 4bg.3 `R5_FIRST_TYRE` — the objective is met, the volume is NOISE, and so is the objective

PCR is **byte-identical** to base: it never approaches the bound.

| TBR | BUILT | dBUILT | in-month | starved | R5 1st tyre | > 72 h | L11 |
|---|---|---|---|---|---|---|---|
| `MD_base2` | 98,003 | +0 | 96,932 | 1,654 | 73.45 h | 26 | 31/52 |
| `R5_FIRST_TYRE=1` | 98,192 | **+189** | 97,084 | 1,465 | **70.85 h** | **0** | 33/52 |

Mechanism, traced not assumed: the tighter test refuses two runs on their pinned
machine, they **spill** to another eligible one (TBR GTs spilled past their pin
**26 → 27**, TBR runs 514 → 516), and both land. Exactly two GTs move —
`GT 5113` +87 (was the whole `r5_shelf_life` starvation) and `385/65R22.5JTL`
+102.

**THE NULL CONTROL KILLS THE +189.** Perturbing `PLANNER_LOT_INTERVAL_TBR` — a
6 to 30 **minute** change in a 16 h release grid, no physical meaning at that
resolution — gives TBR dBUILT:

| setting | 15.5 h | 15.9 h | **16.0 (base)** | 16.1 h | 16.5 h |
|---|---|---|---|---|---|
| dBUILT TBR | **−207** | +100 | 0 | +166 | **+204** |

**mean +66, sd 187, range −207 … +204.** +189 sits inside one sd of the mean of
physically equivalent settings.

**AND THE NULL CONTROL ALSO KILLS THE OBJECTIVE.** Three of those four null arms
land under 72 h with **zero** tyres over shelf life, and `MD_nt159` scores the
identical **33/52 with the identical two invariants flipping to PASS**. On
August, the volume gain, the R5 number and the L11 count are **all reachable by
accident**.

**What is NOT reachable by accident is the only reason to ship it.** With the
flag on the bound is *enforced* — `_place` and `_r5_floor` both test the first
tyre, so no placement can breach it. With it off, compliance is a coin-flip of
the greedy: this baseline breaches by 1.45 h on 26 tyres and four physically
identical plans happen not to. **A rule that holds by luck is not held.**

---

> **DO-NOT #53: a gate that iterates a reservation map is blind to every row that
> never entered it — and BOTH of L7's feasibility gates were.** §4av named the
> rule ("any path that appends to `bs` must also write `busy`") and it took a
> second session to obey it. When you add a code path that emits a scheduled row,
> grep for every structure the GATES read, not only the ones the PLANNER reads.

> **DO-NOT #54: "production fits the day" is not "the day fits".** L7 packs
> machine-days to exactly 24.00 h of production, which is feasible only if
> changeovers are free. Any capacity statement about a resource must name what it
> excludes; a 24.00 h maximum is the tell that a budget was written against the
> wrong quantity.

> **DO-NOT #55: run the null control against the OBJECTIVE, not only against
> BUILT.** `R5_FIRST_TYRE` drove tyres-past-shelf-life 26 → 0 and L11 31 → 33,
> and a 6-minute perturbation of an unrelated grid did the same thing three times
> out of four. If a physically meaningless change reaches your success criterion,
> the criterion is measuring the greedy, not the mechanism (§4bg.3; extends
> DO-NOT #49, which only ever perturbed the volume).
