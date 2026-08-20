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
| `PLANNER_L5_ALPHA` | `1.0` | front-loading allowance over the takt rate. **Interior maximum on both months** — do not tune |
| `PLANNER_L5_TAKT_PLANTS` | `TBR` | PCR measured mixed-sign (−0.28 Jul / +0.18 Aug) and is excluded |
| `PLANNER_L5_TAKT_PART` | `1` | adds the TBR TT/TL and PCR rim partitions. `0` = plant-aggregate only, worth −1.34 pt Jul / −0.32 pt Aug TBR |
| `PLANNER_ATOMIC_SPLIT_PLANTS` | `PCR` | one halving of a single-slice run, charged to the B12 budget (§4l.2). **+1.09/+1.04 pt PCR.** Adding TBR costs −2.01/−0.92 |
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
| `PLANNER_PART_SEED` | **`""`** | `wb` seeds `build_gt_machine_partition.py` tier 0 from the workbook's `Assigned_Machine` (§4s.6). Mixed sign: PCR −0.74 Jul / +0.24 Aug; with `PARTITION_PLANTS=PCR,TBR` TBR −1.71 Jul / +1.16 Aug. It is 93 %/84 % identical to `gt_home_machine`, i.e. near the twice-rejected pin — hence a seed guarded by the existing free-hours test, never the partition itself |
| `PLANNER_PARTITION_PLANTS` | `PCR` | `""` disables (95.8 % ful, same-size 82 %); `PCR,TBR` enables TBR |
| `PLANNER_TAU_RELEASE` | `min` | `star` restores the §1a bug. Do not. |
| `PLANNER_SUBFLOOR_PCR` / `_TBR` | `180` / `400` | plant-matched. Raising to 340/800 buys ~1 pt of demand at 23.5 % sub-floor — **over the plant's 14 %** |
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

41. **A blocked interval you write into a shared structure can be erased by a
    consumer that rebuilds it.** `_make_room` reconstructs `busy[mach]` from
    `placed`, which holds only real runs, so a booked closure vanishes and every
    later `_place` on that machine is free to schedule into a shut plant. Grep
    for every ASSIGNMENT to the structure, not just the reads (§4aa.1).

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
`MACH_UTIL_CAP`, `TAU_RELEASE`.

**Rule for the next change:** a number that describes the PLANT goes in
`config.py` (or is read from a master). A number that describes OUR ALGORITHM
goes in `l7` beside its measurement. Nothing goes in two places.

---
