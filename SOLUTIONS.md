# CTP Planner — solutions to `PROBLEMS_AND_FLOW.md` V17

**14 August 2026 · companion to [PROBLEMS_AND_FLOW.md](PROBLEMS_AND_FLOW.md).**

Every claim below is tagged with how far it was taken:

| tag | meaning |
|---|---|
| **MEASURED** | a fresh A/B arm was built and read. The delta is real. |
| **VERIFIED** | re-derived from the shipped run parquets or from code, independently of the doc. |
| **PREDICTED** | mechanism established in code, magnitude estimated, arm not yet run. |

The engine analysed is `schedule/send/planner/cmbc/` (the live tree). `planner/plan/`
is retired; the top-level `CLAUDE.md` says so and is correct.

---

## 0. Executive summary — what changed after the audit

Three findings invert the document.

**1. P4 does not exist as described. Its fix is one line and is worth +5,514 TBR tyres.** — MEASURED

The doc's largest engineering item — *"6,583 of TBR's 6,816 starved tyres are rejected
by the B12 lot floor"* — is a misread label. That string is assigned by a `len()` test,
not by the floor:

```python
# l7_pull_release.py:2970
_rsn = ("would breach min_lot" if len(grp) > 1 else "no feasible release")
```

The genuine floor refusal is a *different* string set four lines later. Read at the
right grain, `runs/p3_base/build_starved.parquet` says:

| reason | tyres | rows |
|---|---|---|
| `would breach min_lot` (the `len()` heuristic) | 6,583 | 241 |
| **`below min_lot (strict B12)` (the real gate)** | **233** | **8** |

B12 blocks **233 tyres**, not 6,583. The 6,583 are blocked because **TBMTBR6 is the sole
eligible machine for 994 build-hours in a 744-hour month — 133.7 % load** — while
TBMTBR9 sits at 30.8 % and TBMTBR4 at 55.1 %, both fenced off by the TT/TL group
partition. Fixing the partition:

| August 2026 | `p3_base` (shipped) | **`p3_ttl46`** | Δ |
|---|---|---|---|
| **TBR BUILT** | 91,509 | **97,023** | **+5,514** |
| TBR starved | 6,816 | **1,302** | −81 % |
| TBR fulfilment | 89.1 % | **94.5 %** | **+5.4 pt** |
| PCR BUILT | 406,274 | 406,274 | **bit-identical** |
| TBR sub-floor run share | 0.00 % | 0.00 % | held |
| TBR R5 max wait (limit 72 h) | 68.3 h | **67.5 h** | improved |
| TBR GT inv. mean (rail 1,400) | 1,035 | 1,091 | +56 |
| TBR weighted build CO min/mach-day (plant 35.6) | 21.9 | 24.0 | +2.1 |

**2. August PCR is a capacity problem, not a search problem — and that is provable.** — VERIFIED

The in-month press ceiling, computed from the engine's own rate model:

| | plannable | **press ceiling** | achieved | **unreachable** | **search headroom** |
|---|---|---|---|---|---|
| Jul PCR | 397,288 | 396,749 | 375,966 | 539 | **20,783 (5.23 pt)** |
| Jul TBR | 97,436 | 96,994 | 93,912 | 442 | 3,082 (3.16 pt) |
| **Aug PCR** | 426,688 | **397,103** | 386,242 | **29,585 (6.93 pt)** | **10,861 (2.55 pt)** |
| **Aug TBR** | 98,743 | 97,464 | 88,686 | 1,279 | **8,778 (8.89 pt)** |

Arithmetic: `(86 × 744 − 807 mould-h) × 6.286 t/press-h = 397,131`; `(79 × 744 − 481) ×
1.672 = 97,469`. Aug PCR runs **days 2–23 at 100.0 % press occupancy — 22 consecutive
days, zero idle hours.** The doc's ~19,000-tyre deficit is understated by 55 %, because
it was measured against a plan that had *already dropped* the volume the deficit causes.

This kills the "constraints are mutually load-bearing" mystery (P8). Eight of nine
scheduler changes measured negative because **they were all graded on the month with the
least headroom.** There is nothing behind the constraint to release.

**3. The doc's own conclusions rest on two experiments that never ran.** — VERIFIED

- `PLANNER_L56_PROTECT_FIRST_H` at 24/48/72 h produces **byte-identical plans**:
  `jul_prot24`, `jul_prot48`, `jul_prot72` all hash to `d73af6e9…`
  (`cure_campaigns.parquet`) and `3bbc1d69…` (`build_schedule.parquet`). The guard
  compares against `floor_ts`, which under shipped defaults only ever takes the values
  `t0` or `t0 + 11.86 h` — so any threshold above 11.86 h disables the hint entirely.
  *"There is no middle point at this grain"* is unsupported.
- The mould-life "inert" test ran at **3,000 cycles**, but `config.py:276-279` records
  the project's own measurement as **1,344 PCR / 1,769 TBR**. The test used a constant
  the codebase itself says is wrong by 2.2×.

---

## 1. Corrections to the measurement layer

`PROBLEMS_AND_FLOW.md` §0 is right that measurement errors dominate. Six more, all
inflating in our favour, all VERIFIED here:

| # | claim in V17 | what the artefacts say |
|---|---|---|
| 1 | P4: 6,583 TBR tyres blocked by B12 | **233.** The label is a `len()` heuristic (`l7:2970`) |
| 2 | P4: "241 lots averaging 27 tyres" | 241 is the **slice** count; the **run** count is 77, p50 **85** tyres — *above* the 70 floor. `l7_pull_release.py:29-49` names all three levels |
| 3 | P6: "18,594 tyres built but cure in September" | **5,494.** 13,483 PCR + 1,793 TBR are built *after* 1 Sep 07:00, on September machine-hours. True carry-out is 4,186 PCR + 1,308 TBR — exactly the rows in `carry_forward_gt.parquet` |
| 4 | P7: "the bottom 20 GTs all start on day 26" | first-seat days are 11–32, median 27.5. One (`GT 2056 ROYL`) seats on **day 32, past month end** |
| 5 | P7: "82 of 117 mould changes … burning 493 press-hours" | `mchg_s()` charges the change *wherever it lands* (`l5:1260`). Moving a change earlier recovers **zero** hours. The recoverable quantity is induced idle, ~673 h, mostly horizon taper |
| 6 | §4: "press availability 0.8897" | that is **PCR**. TBR is **0.8282** (`params_2026-08-01.json`). TBR also loses 1.8 % at cure (`cure_yield` 0.98202) |

And one that matters for any future rate argument: **the cure-rate model is sounder than
the doc implies.** `plant_ct.py:49-55` shows nameplate × availability landing within
1.5 % of realised (PCR 150.4 vs 150.5 observed tyres/press-day; TBR 40.0 vs 40.7). It is
not a mined median. But `plant_ct.py:75-79` also records, measured today, that **+2.5
min/cycle takes Aug PCR 91.1 % → 81.2 % (BUILT −38k)**. No scheduling item in this
document is within an order of magnitude of that sensitivity.

---

## 2. The solutions, ranked by tyres per unit of effort

### S1 — Score the B16 TT/TL partition against the *restricted* eligibility matrix

**One line. MEASURED at +5,514 TBR BUILT, PCR bit-identical.**

`_offline/l2_capability.py:205` builds the eligibility set used to choose the TT/TL
split from the **raw** capability frame:

```python
for r in cm.filter(pl.col("plant") == "TBR").iter_rows(named=True):   # raw cm
    elig_by_gt.setdefault(r["gt_code"], set()).add(r["machine"])
```

But L7 applies `allowable.restrict → restrict_rimlock → restrict_rimset` first
(`l7_pull_release.py:774-777`), which cuts 813 rows to 364. **The partition search scores
candidates against an eligibility set 2.2× wider than the one the planner enforces.**
Under the raw matrix, `TL={6,9}`, `TL={5,6}` and `TL={4,6}` tie at `(0,0,0,0,2)` and the
winner is decided by the final tiebreak — lowest machine numbers. August's TL group was
chosen alphabetically.

**Fix:** build `elig_by_gt` from the restricted frame.

Replaying the existing scoring key on both months — MES-free, from committed masters only
(see §2.1b):

| month | n_tt | key on raw (today) | key on restricted (fix) | shipped file |
|---|---|---|---|---|
| 2026-07 | 6 | TL=[5,6,9] | **TL=[4,5,6]** — *unique winner, no tie* | [4,5,6] ✓ reproduced |
| 2026-08 | 7 | TL=[6,9] | **TL=[5,6]** — *2-way tie with [4,6]* | [6,9] ✗ |

July is reproduced exactly **and its winner is unique at key `(0, 0.0, 0, 0.0)`**, so the
fix is July-safe by construction.

**But S1 alone does not fix the root cause.** On August, `{4,6}` and `{5,6}` are *still* a
perfect tie at `(0, 0.0, 0, 0.0)` and `combo` breaks it exactly as before — lowest machine
number. The restricted fix moves the decision from one alphabetical tiebreak to another.
It lands on a feasible set for both shipped months, but by luck, not by measurement.

**Consequence for shipping:** the one-line fix emits `TL={5,6}` (**+5,253**), *not* the
`TL={4,6}` measured at **+5,447 / +5,514**. The 261-tyre difference has no principle
behind it — the capacity max-flow scores both at 0.0 unmet hours. **Ship `{5,6}` from the
rule rather than `{4,6}` from a hand-edit.** Hardcoding a month-specific winner is the
exact bug class [PARTITION_AND_CHANGEOVER.md](schedule/send/PARTITION_AND_CHANGEOVER.md)
§1 exists to prevent. S2 is therefore **not optional**.

Machine occupancy is the mechanism, visible directly (TBR, % of 744 h):

```
          TBR1  TBR2  TBR3  TBR4  TBR5  TBR6  TBR7  TBR8  TBR9   spread
base      94.5  82.7  89.6  55.1  70.5  97.2  81.7  84.3  30.8   66.4 pt
ttl46     80.0  83.6  88.8  81.8  67.3  88.0  78.2  78.7  81.7   21.5 pt
```

**Independent corroboration already on disk.** `warehouse/derived/` holds three July
variants, and they line up exactly with the two scoring bases:

| file | TL | = |
|---|---|---|
| `cap_ttl_groups_2026-07.pre_history.parquet` | [5,6,9] | **what raw scoring emits** |
| `cap_ttl_groups_history_2026-07.parquet` | [4,5,6] | derived from production history |
| `cap_ttl_groups_2026-07.parquet` (shipped) | **[4,5,6]** | **what restricted scoring emits** |

Someone already found July's code output wrong and overrode it from history. **The
restricted-eligibility rule re-derives that same answer from first principles, without
needing history at all** — two independent routes to `[4,5,6]`. That is the strongest
evidence available that the fix is correct rather than merely convenient.

**It also means the defect is live in two more months nobody has checked:**
`cap_ttl_groups_2026-05` and `_2026-06` are both still `TL=[5,6,9]`, i.e. raw output,
never history-corrected. Re-score them before either month is replanned.

**Risk / owner:** the TT/TL *group* is synthesised (`l2_capability.py:188-304`); **no
line reads historical TT/TL production per machine.** The evidence that TBMTBR4/5 can
build tubeless is the plant's own `allowed_machine_matrix.parquet`, which lists them for
GT 5103 / 5083 / 5078 / 11R22.5JUHE. Strong prior, not a certainty — **get the plant to
confirm before shipping.** If TBR4 is ruled TT-only, fall back to `TL={5,6}` (+5,253).

### S2 — Implement B16 step 7: refuse a capacity-infeasible partition

**PREDICTED: makes S1 permanent and month-proof.**

`BUSINESS_RULES.md:142` step 7 says *"If either group exceeds ~95 % load → report
INFEASIBLE and re-split. NEVER spill across the boundary silently."* **It is implemented
nowhere.** `uncovered()` (`l2_capability.py:215-226`) and `stranded()` (`:248-274`) are
pure coverage tests in tyres; neither has an hour cap. August spilled silently.

Add a per-machine capacity max-flow to the search key: in-group demand hours assignable to
in-group machines under restricted eligibility, capped at `MACH_UTIL_CAP × plan_h`. Raise
INFEASIBLE and re-split at `n_tt ± 1` when the best partition at `n_tt` has unmet > 0.

| TL set | unmet build-hours | ≈ tyres |
|---|---|---|
| **{4,6}** | **0.0** | 0 |
| **{5,6}** | **0.0** | 0 |
| {6,9} ← shipped | 282.3 | ~4,910 |
| {4,9} | 520.2 | ~9,046 |

**Position the term AFTER `(cnt, vol_bad, dead, deficit)` and BEFORE `makes`/`combo`** —
not ahead of `deficit`. Measured justification: July's winner is already **unique** at the
existing key, so a later-position term provably cannot move July, which removes the
regression risk entirely. Placing it ahead of `deficit` would re-open July's choice among
`{[4,5,6], [4,6,9], [5,6,9]}`, all of which are zero-unmet.

**What this term does and does not buy.** It rejects `{6,9}` decisively (282.3 h vs 0.0) —
that is the whole 5,500 tyres, and it is what makes the fix survive a month whose demand
mix moves. It does **not** separate `{4,6}` from `{5,6}`; both are feasible, and the
261-tyre gap between them is a scheduling artefact, not a capacity one. Accept `{5,6}`.

*Untested candidate if the 261 matters later:* minimise the **maximum in-group machine
load** as a further tiebreak. It is the principled expression of the actual defect (one
machine at 133.7 %) and might prefer `{4,6}`. Not run — do not quote it as a result.

Also fix `l2_capability.py:195`: `n_tt` uses **tyre-qty share** where
`BUSINESS_RULES.md:139` specifies **hours**. It does not bite in August (76.5 % vs
76.3 %) but it is a stated divergence from the rulebook.

### S2b — The rebuild is NOT MES-bound, and the hand-edit must not survive

**VERIFIED.** The B16 group search (`l2_capability.py:188-304`) reads four inputs:
`dem` (committed), `tt_tl.parquet` (committed), `allowed_machine_matrix.parquet`
(committed), and `cm`. Only `cm` is MES-derived — **and it has already been mined and
written to `warehouse/derived/cap_machine_<M>.parquet`, which is on disk and git-tracked.**
The search is therefore a pure function of committed artefacts and replays without the MES
drop. Both months were re-derived this way; the numbers in S1's table come from that
replay, not from a re-mine.

**So a hand-edited `cap_ttl_groups_<M>.parquet` is never necessary and must not be
shipped.** Add `scripts/rescore_ttl_groups.py` — read-only by default, `--write` to emit —
that runs exactly the block above from committed masters. Then the month's group file is
reproducible by anyone with the checkout, and the arm that validated it can be rebuilt.

**And close the gate that let the bad file through.** `l1_preflight.py:225-228` opens
`cap_ttl_groups_<M>.parquet` and emits **an INFO line counting rows**. That is all. There
is no feasibility check, and unlike `gt_machine_partition.parquet` there is **no month
stamp** — so a hand-edited, stale, or infeasible group file passes preflight silently.
This is the same defect class as the partition staleness guard
([PARTITION_AND_CHANGEOVER.md](schedule/send/PARTITION_AND_CHANGEOVER.md) §4o, DO-NOT #6),
one master over. Preflight should:

1. re-run the S2 scoring from committed masters and **ERROR if the file on disk differs
   from what the rule produces** (this catches hand-edits and stale months at once);
2. **ERROR on unmet > 0** for the partition actually on disk — B16 step 7, where it
   belongs.

Both are cheap because the search is 84 combinations over committed data.

### S3 — `PLANNER_LOAD_UNLOAD_MIN=2.5`

**Ceiling +9,968 PCR / +9,317 TBR. One environment variable, already implemented.**

`plant_ct.py:86-88` holds mined values of PCR 2.9 / TBR 8.3 min. The plant stated 2.5 on
2026-08-14. On TBR that is a 70 % cut and it shortens every press cycle, raising the cure
ceiling — the binding constraint on both plants.

**This is the highest-return action in the whole document and it is behind a flag that
already exists.** It is an override of a *measured* value by plant instruction
(`plant_ct.py:30-38` derives 8.3 as a near-constant residual across 49 TBR GTs), so:
never ship it without the plant's sign-off in writing, and state the override every time
the resulting numbers are quoted. If 2.5 is wrong, TBR press-hours are understated by
~5,587 h and the plan is infeasible on delivery.

### S4 — De-serialise the day-1 mould change — **MEASURED, REJECTED on BUILT**

**Predicted +800…+1,400 PCR. Measured −428 PCR / +53 TBR.** Ship it only for the
correctness reason below, not for volume.

| August, measured | BUILT | Δ | in-month | starved | press % | L11 |
|---|---|---|---|---|---|---|
| V17 baseline PCR | 407,307 | — | 91.1 % | 8,580 | 95.3 % | 26/42 |
| **+S4 PCR** | 406,879 | **−428** | 90.9 % | **8,084** | 95.0 % | **27/42** |

The mechanism below is real — the day-1 press-hours are genuinely freed — but **they do
not convert to BUILT.** That is consistent with every other attempt on P1 (partial press
start, build-priority, ledger replay, τ\* variants, level-load): day 1 is not where the
volume is. It gains an L11 invariant and cuts starvation 8,580 → 8,084, so it is defensible
as a correctness fix; it is not a volume lever. **This is now the ninth day-1 experiment
to measure neutral-or-negative — treat P1 as closed on the engineering side and reallocate
to the capacity questions in §4.**

The description below is retained because the *arithmetic* defect is real and should be
fixed regardless.

---

**Original analysis (mechanism confirmed, magnitude wrong):**

`l5_cure_master.py:1258-1260`:

```python
st = max(free.get(pr, t0), floor_ts)
if last_gt.get(pr) not in (None, gt):
    st = st + timedelta(seconds=mchg_s(p, pr))   # mould change
```

The setup is added **after** the max with `floor_ts`. These are independent resources:
`free[pr]` is when the **press** frees (the mould change consumes the press); `floor_ts`
is when the **green tyre** exists (the mould change consumes no tyres). On day 1
`free == t0` while `floor_ts` is 11.86 h away, so the change runs inside an idle window
and costs nothing. Correct arithmetic:

```python
st = max(free.get(pr, t0) + timedelta(seconds=chg), floor_ts)
```

The day-1 first-start histogram is four discrete spikes that factorise exactly as
(0 or τ\*-wall) + (0 or mould change) — VERIFIED:

```
jul_prot24 PCR   0.0h: 26   6.0: 7   7.17: 1   11.86: 23   16.36: 2   17.86: 23
aug_v17    PCR   0.0h: 19   4.5: 3   6.0: 7    7.17: 1     11.86: 21  16.36: 3
```

Day-1 head loss decomposes as **Jul PCR 841 h = 617 τ\* + 225 mould; Aug PCR 946 h =
664 + 282** — and the 841 h reproduces the doc's own *"~842 additional press-hours needed
on day 1"* exactly, so this is the whole of P1, not a slice.

| | presses in the "both" bucket | recoverable | ≈ tyres |
|---|---|---|---|
| Jul PCR | 29 | 176 h | ~1,105 |
| Aug PCR | 35 | 220 h | ~1,381 |
| Jul / Aug TBR | 15 / 14 | 90 / 84 h | ~162 / ~151 |

**Why this is not another failed τ\* relaxation:** `floor_ts` is untouched. Campaigns land
*on* the supply boundary, never before it, so the failure mode of `FLOOR_BASIS=min/slice`
(−2,478 to −6,050 BUILT) cannot apply. L5 never backfills `[free, st)` on a press
(`l5:1406`, `l5:1489` only advance `free`), so no double-booking is created; and
`l10_discretise.py:169-192` derives mould-change events only *between* consecutive
campaigns, so day-1 changes are already invisible to `mould_changes.parquet` — this
cannot move any L10/L11 changeover metric.

**Honest failure mode, state it in the arm write-up:** it concentrates ~37 PCR + ~15 TBR
mould changes into `[t0, t0+7.17 h]`. At 2–3 fitters each that is 74–111 fitters at once.
Crew rostering is unmodelled (`l10_discretise.py:229-235` says so), so this violates
nothing that is *checked* — but it is a real physical claim.

### S5 — Least-constraining-value press selection

**PREDICTED +2,000 to +3,100 PCR BUILT. Creation, not relocation.**

P7's observation is right; its mechanism is not. The driver is **press-eligibility
scarcity colliding with `-qty` seating order**:

| | |
|---|---|
| PCR GTs with ≤ 4 eligible presses | **17**, needing 3,871 press-h / 24,749 tyres |
| presses that are their only seat | **27** |
| broad-GT work parked on those 27 presses | **17,248 h**, of which **15,564 h** belongs to GTs with ≥ 10 alternatives |

```
press 307  sole seat of GT 2056 ROYL (401)   -> 745 h of other GTs from day 1 -> seated day 32
press 232  sole seat of GT 2557 HPE KIA(651) -> 692 h from day 1              -> day 30, 541 unplaced
press  23  sole seat of GT 1672 SPT (1,372)  -> 621 h from day 1              -> day 27, 663 unplaced
```

Meanwhile `GT 1513 XPC1 MSIL` (62,002 tyres) has **44 eligible presses and uses 30**.
The GTs that take the captive presses do not need them. Of PCR's 6,296 past-horizon
unplaced tyres, **3,127 sit on GTs with ≤ 4 eligible presses**.

The code already establishes the pattern: `l5_cure_master.py:1302-1315` uses `same_rim`
as a **strict tiebreak among presses that free at the same instant**, so it *"never delays
a campaign, so it cannot cost fulfilment the way queue-ordering did."* Add a second
tiebreak in the same slot (`l5:1314`):

```python
if best is None or (st, captive, same_rim, pr) < (best[0], best[4], best[3], best[1]):
```

where `captive = 1` if `pr` is in the eligible set of a not-yet-placed GT with ≤ K
alternatives (K = 4) and the current GT has > K. `st` stays the primary key, so nothing
is ever delayed.

**Failure modes:** (a) the displaced GT lands on a press with a different mounted mould,
adding a change — if PCR same-size degrades below 68.4 %, put `same_rim` ahead of
`captive`; (b) if no equally-early non-captive press exists the arm is a no-op — print
`n_diverted` so you can tell the two apart.

### S6 — The two-sided carry contract

**PREDICTED: zero on August, ~−6.3 pt on September, and that is the correct answer.**

This is the structural fix for P6, and both halves already exist unwired.

The tyre accounting is currently **correct**: the numerator is `qty_fed_in_month`, clipped
at `month_end` (`l7:3432`); carry-forward tyres are excluded from August and written to
`masters/opening_gt/carryforward_gt_2026-09.parquet` (`l7:3540`), where September's L4
nets them out. Each tyre counts once. **Therefore crediting the tail to August *would* be
a genuine double count** — the same physical tyre scored against two months' demand. That
settles the horizon ruling on arithmetic rather than opinion.

**But press capacity is double-counted, and nothing catches it.** L5 plans on
`month_end + 72 h` (`l5:774-776`), handing PCR 86 × 72 = 6,192 extra press-hours. August
consumes **4,273 of them**; `carry_out.parquet` shows **96 PCR presses still running past
`month_end`**. Meanwhile `masters/carry_in/` **does not exist on disk**, so
`l5:1165-1180` prints *"running cold (no-op)"* and September will seat campaigns from
hour 0 on 96 presses August left busy. **August borrows 4,273 September press-hours and
September never repays them** — 26,850 tyres of capacity spent in advance, on a plant
already at 100 % occupancy for 22 days.

The dormant reader is `PLANNER_CARRY_IN` (`l5:1165-1178`), which seeds `free[press]` and
`last_gt[press]` — occupancy *and* mounted mould, so a continuing campaign pays no change.
The emitters are `carry_out.parquet` (`l5:1519-1529`) and `carryforward_gt_<next>.parquet`
(`l7:3540`).

Write the contract down and enforce it as an L11 invariant:

```
opening_gt(M+1)     == carryforward_gt(M+1) ∪ undrawn_opening(M)
carry_in(M+1).press == carry_out(M).press, ends
numerator(M)        == cured in [t0(M), t0(M+1))              # unchanged
Σ_M numerator(M)    == Σ_M built(M) + opening(M₀) − closing(M_last)
```

`l7:3561-3571` already asserts the within-month half. Extend it across the boundary.

**Cost:** one directory and one invariant. **Predicted September effect: −6.3 pt.** That
is not a regression, it is the removal of a subsidy — **write the prediction down before
you run it, or someone will revert it.**

Your own memory records the closed-box vs everything-available-at-t0 conflict as
unresolved. It now has a second and larger dimension: press occupancy across the
boundary, not just the ~4 h carry-in gap. **Do not score any scheduler experiment while
this is open** — the same run reads 90.5 % or 91.5 % depending on the ruling, which is
*half of August PCR's entire search headroom*.

### S7 — The LNS hint: fix the guard, then re-grain the key

**S7a (2 lines, cheap):** the guard compares against `floor_ts`, which is a *floor*, not
a seat — it is `t0` for every campaign of a stocked GT, including one on day 25. Compute
the undelayed seat one line above (`cand` and `free` are both in scope):

```python
# insert before l5_cure_master.py:1233
_seat0 = min(max(free.get(_pr, t0), floor_ts) for _pr in cand)
...
if PROTECT_FIRST_H <= 0.0 or _seat0 >= t0 + timedelta(hours=PROTECT_FIRST_H):
    floor_ts = max(floor_ts, t0 + timedelta(hours=_d))
```

This makes 24/48/72 meaningful values for the first time. Run it before S7b — it is the
cheap test of the same hypothesis. (Values in `(0, 10.18]` also become meaningful against
the *current* code, exempting exactly the stock-covered campaigns.)

**S7b — the campaign-specific key.** Mint at `l5:808-810`, inside the loop that already
assigns `seq`:

```python
cuid = f"{plant}|{gt_code}|{mould_set}|{seq}"
```

**Uniqueness:** `(plant, gt_code, mould_set)` is a primary key of
`l45_lots_<month>.parquet` after the `n_lots > 0` filter — verified 87/87 July, 91/91
August. Hint grain goes from 87 keys to ~468.

**Stability across a reseat** — the hard part, and it is provable rather than hopeful:

> The job tuple list and its total order are a **pure function of
> `l45_lots_<month>.parquet`**, which the loop never rewrites.

1. `l56_loop._run` (`l56_loop.py:59-68`) invokes only L5 and L7. L4/L4.5 never re-run.
2. `sizes` is read at `l5:794-796` and is untouched by `_DELAY`.
3. The sort key at **`l5:859`** is `(plant, -qty, gt_code, seq)` — every component is
   pre-seating. The delay is consumed at `l5:1233-1255`, strictly inside the placement
   loop, and mutates only `floor_ts`.
4. Therefore `cuid → (qty, queue position)` is invariant; only `(press, start, end)` move.

**The one hazard, close it explicitly:** `_SPLIT` (`l5:798-807`) re-partitions `sizes`
*before* the enumerate, renumbering `seq`. Gate the campaign hint to `delay` mode and
assert `not _SPLIT`. Same for `PLANNER_L5_MAX_CAMPAIGN_H` (`l5:946-949`), which mutates
`seq` to `seq*100 + i`.

**Never key on seating output** — start-time rank, press id, or row order in
`cure_campaigns.parquet`. A delay that reorders two campaigns of one GT swaps their ranks,
the hint lands on the wrong campaign, and the loop degrades to noise.

**S7c — fix the reconcile at the same time.** `l7:3384-3398` computes `qty_unfed` as a
**FIFO residual by start time within `(plant, gt_code, press)`** — the latest campaign on
a key absorbs the whole shortfall by construction. It is a positional artefact, not an
attribution. Re-key it on `cuid`, and assert `sum(qty_fed)` per plant matches the old
grouping to within rounding before trusting it.

**Predicted: +2,000 to +6,000 July BUILT** while restoring most of the ~4,600 day-one
cures. **The failure mode, stated plainly: the +6,320 may *be* the day-1 delay** —
vacating presses at t0 may be what frees the calendar to re-form. If so, per-campaign
hinting recovers day 1 and gives back nearly all of the gain. The A/B is the only way to
know. Expect to need `--iters 8` with 468 keys.

### S8 — Correctness fixes worth shipping regardless of measured delta

| | site | issue |
|---|---|---|
| **gap currency** | `l5:696` vs `l5:540`/`l5:1420` | the t0 cover **test** uses the per-GT `_gap_q = gap_h × rate_of(plant, gt)`; the budget **debit** uses the plant-median `gap_tyres[p]`. PCR per-GT gap spans **50.8–98.1** against a flat 75.5 debit — a fast GT is charged 23 % less than it consumes, a slow one 49 % more. Debit `_gap_q`. |
| **stale comment** | `l5:662-663` | says "~72 on PCR, ~31 on TBR". With plant CT live the true values are PCR p50 **74.9**, TBR p50 **16.8** — TBR is overstated **1.9×**. This comment has misled at least one document. |
| **duplicated cavity constant** | `plant_ct.py:74` (2.0) vs `l3_cavities.parquet` (PCR **3.396**, read at `l45:134`, `l5:460`) | two numbers for one quantity, differing 70 %. `l3`'s is an *effective residual* (`tyres_per_day / cycles_per_day`), not a physical count. Exactly the §1g / DO-NOT #13 defect class. |
| **junk master** | `INPUT/derived/mould_cavity.parquet` | `curing_capacity` contains 50 / 59 / 64 / 87 / 93 / 99 / 103 / 107. Whatever that column is, it is not cavities. Nothing reads it today — keep it that way, or delete it. |
| **free mould change** | `l5:1256-1260` | 21 PCR presses resolve `last_gt` to `None`; `not in (None, gt)` is False, so they get a **free** change. Same fiction `WARM_PRESS` (`l5:997-1014`) says it removed. |
| **`GT 1772 NEO`** | `cap_press_2026-08.parquet` | **no eligible press at all** — 1,182 tyres unplaced on a master-data hole. Cheapest fix in the file, and it is not in the doc's §4 list. |

---

## 3. What NOT to build — with the reason each one fails

| proposal | verdict |
|---|---|
| **Merge same-GT slices regardless of R5 spacing** (the doc's §7 headline, "~5,500 tyres") | **Inert, then harmful.** `_place` re-derives R5 per slice from the run's own start (`l7:2342-2350`), and `ideal` is the **min** over slices (`l7:2221-2223`), so `worst ≈ merged span`. GT 5103's 33 refused runs span **750.3 h** against a hard 72 h. The merged run is refused on every machine, *and* it folds currently-placeable predecessors into an unplaceable object. |
| **R5-bounded slice merging** | **Already implemented** (`l7:2005-2026`, `span_cap = 65.19 h` on TBR) and worth **0 of 233 tyres** here — all four sub-floor runs either have no same-GT partner or a merged span of 116–327 h. `POOL_TAILS` is inert for the right reason. |
| **Press-stream consolidation (fewer presses per GT)** | R3 is not binding (GT 5103 has 9 moulds, uses 7 presses) — but the lever points the wrong way. Fewer presses = longer campaigns, and the campaign-length family is measured at **−43,104 BUILT**. |
| **Cross-GT run merging** | No mechanism. B12 is per-run-of-one-GT: `gq = sum(d["qty"] for d in grp)` where `grp` is one GT's list (`l7:2032`), dispatched per `(p, gt)`. A changeover-separated pair is two independently floor-checked runs. |
| **Machine-calendar defragmentation** | **`_make_room` already is it** (`l7:2436-2563`) — it rescued PCR 328 / TBR 150 runs in `aug_v17`. Its bail counters (`cold=92 noroom=157 rail=114`, shortfall p50 1.47 h) say incumbents cannot move earlier without breaching their own R5. TBMTBR6 has **26.6 h of total idle against 365 h of unplaced work**. A defrag pass cannot manufacture 365 hours. |
| **Pre-month build prologue** | R5 permits it (a tyre at `t0 − 54.1 h` is legal), but it books machine-hours belonging to the previous month's plan — relocation, not creation. The legitimate version already exists as `carry_forward_gt` → next month's opening stock; it is simply not aimed properly (see S9). MEASURED ceiling anyway: **+605 PCR / +289 TBR** at 24 h, and 72 h is *worse* than 24 h. |
| **Spread the opening GT evenly** (open question 7 as currently worded) | **Measured negative.** `gap_q` is a *threshold*; sub-threshold tyres are worth exactly zero seats. Even spreading takes Aug PCR from **51 potential t0 seats to 29**. See S9 for the correct ask. |
| **Level mould changes across the month** (P7 as diagnosed) | `mchg_s` charges the change wherever it lands. Moving it earlier recovers **zero hours**. And because curing binds, relocating cures earlier mostly pulls tyres out of the tail — BUILT unchanged. |
| **Cap campaign length** | −43,104 BUILT, confirmed in code: `l5:910-914` — *"splitting one campaign into N pieces creates N INDEPENDENT BUILD RELEASES, each needing its own R5-legal window."* |
| **Re-enable L9 on August** | Its "1,428 candidates, 0 moves" is **not** evidence the plan is optimal. The search is lexicographic and tier 1 (`demand short` = 7,409) is dominated by the 29,585-tyre capacity deficit, so nothing beneath tier 1 is ever consulted. L9 was correctly reporting that August PCR is infeasible. **Re-run it on July**, where tier 1 has 20,783 tyres of genuine slack. |

---

## 4. Plant decisions, re-priced

The doc's §5 asks eight questions. Four are mis-priced and one is asked backwards.

| # | question | corrected price | note |
|---|---|---|---|
| 1 | **Shelf life 72 h or 48 h?** | **existential, not incremental** | `config.py:79-85` records that *Ageing spec rev12* gives 48 h and `Recipemaster.MaxAging` gives 48 h for 493 SKUs. R5 max already reads 59.8 / 66.6 h. If 48 is live the plan is **invalid, not optimistic.** Highest-priority ruling. |
| 2 | **Horizon ruling** | **5,494 tyres, not 23,807** | and crediting it to August **double-counts September**. The real question is the *press-hour* contract (S6), which is 4,273 h ≈ 26,850 tyres. |
| 3 | B12 sub-floor (plant runs 13 % / 31 %) | **233 tyres**, not 6,583 | de-prioritise. `_setup_s` (`l7:1613-1615`) returns **0.0 s for a same-GT transition**, so B12's economic premise — amortise a setup — does not hold for the case it most often blocks. Worth knowing before the conversation, not worth having the conversation. |
| 4 | B12 residual (`min_demand_units` 300/150) | PCR 16 GTs / 2,232 gross build; TBR 3 GTs / 283 | as stated |
| 5 | **Load/unload 2.5 min** | **+19,285 tyres of ceiling** | the single highest-return item. Already flag-implemented. |
| 6 | Month-start machine state (`machine_warm_2026-08`) | ~1,291 PCR tyres of genuine cold start | prologue ceiling +605 |
| 7 | **Opening GT distribution** | **asked backwards** | see below |
| 8 | TBR recipe→GT bridge (21 of 55 unmapped) | day-1 TBR seating | as stated |
| **new** | **TT/TL group: can TBMTBR4 / TBMTBR5 build tubeless?** | **+5,514 tyres** | the group is *synthesised*, never observed. The plant's own allowable matrix says yes. **Add this to the list — it is the largest engineering-side number in the document.** |
| **new** | **Press roster 86 or 92?** | **+28,025 PCR** | largest single number here, and it is a ruling, not work |
| **new** | **Mould-clean cadence** | up to 1,238 PCR press-h | `config.py:276-279` measures 1,344/1,769 cycles, not the 3,000 BTP assumes. *Probably already inside the 0.8897 availability haircut* (an 8 h clean reads as a >4 h gap), so charging it again would double-count — but the "inert" verdict must be struck either way. |

### Question 7, restated correctly

`gap_q` is a threshold, so this is a knapsack, not a spread. Measured:

| | current t0 seats | even spread | **optimal reallocation (same total)** | to open every press |
|---|---|---|---|---|
| Jul PCR (4,820 tyres) | 57 | 45 | **77** | 244 seats = **18,097 tyres** |
| Aug PCR (4,152) | 51 | **29** | **67** | 251 seats = **18,771 tyres** |
| Aug TBR (1,028) | 52 | 29 | 62 | 163 seats = 2,776 |

The waste today is the sub-threshold remainder — **610 tyres (Aug PCR) / 721 (Jul PCR)
sitting under their GT's gap, contributing nothing.** The ask should be:

> Opening GT quantity is right. We need it held in **integer multiples of each GT's cover
> requirement** — ~75 tyres per press-stream on PCR, ~17 on TBR — concentrated on the GTs
> whose moulds are already mounted at 07:00. Reallocating the *same* 4,152 tyres would
> open 67 press-streams instead of 51: **+1,190 to +1,490 tyres at zero extra inventory.**
>
> Full day-1 coverage of all 86 PCR presses needs **~18,800 green tyres against a G8 rail
> of 4,800. That is not reachable.** Under the rail the ceiling is ~64 presses; ~22 will
> always wait ~11.9 h on day 1 unless τ\* itself is renegotiated.

This is the project's own ledger lesson **DO-NOT #33 — "concentrate an overflow, do not
spread it"** — arriving a second time, on a different resource.

**And there is an engineering-side version that needs no ruling (S9):** the closing GT
buffer (`l7:3091-3094`, `l7:3111`) rebuilds the closing floor **GT-for-GT against
`_opening0`** — it perpetuates whatever distribution the month opened with. Retarget its
*mix* (not its quantity) at the cover-optimal allocation for next month's demanded GTs.
It is R5-legal and B12-legal by its own construction. Measured evidence the current
hand-off is actively worse: `carryforward_gt_2026-08` gives **12 of 54** stocked GTs /
44 seats, against the MES snapshot's 19 / 51.

---

## 4b. THE FLAT-PROFILE ASK — the rolling boundary contract

The plant wants a constant build+cure pace for all 30/31 days. Measured answer: **days
2–31 are already there; day 1 is reachable at ~100 %; the end taper is a demand-horizon
problem, not a pacing one.** And on the way to that, one correctness defect outranks
everything else in this document.

### 4b.0 The two months are not jointly executable — MEASURED

Overlay `runs/sc_v18` (July) on `runs/ci_base` (August) on one clock:

| | roster | **peak concurrent presses** | **hours over roster** |
|---|---|---|---|
| PCR, no carry-in | 86 | **118** | **60 h** |
| TBR, no carry-in | 79 | **116** | **48 h** |
| PCR, `PLANNER_CARRY_IN` armed | 86 | 86 | **0** |
| TBR, carry-in armed | 79 | 79 | **0** |

July runs 59 PCR / 53 TBR presses past `2026-08-01 07:00` (`sc_v18/carry_out.parquet`,
1,409 + 1,386 press-h). August plans as if all 86/79 are free at 07:00. **Every day-1
figure in this project is August's share of a day on which 118 presses are running.**

Consequence: a flat profile cannot be verified from one month's artefacts. Every
boundary A/B must be scored on the **union of consecutive months**.

### 4b.1 Carry-in — MEASURED, ships on correctness alone

`runs/ci_base` → `runs/ci_on`, August 2026, fresh arms, identical env:

| | PCR | TBR |
|---|---|---|
| **BUILT** (ex-`OPENING_STOCK`) | 405,923 → **406,905 (+982)** | 96,663 → **97,416 (+753)** |
| starved | 6,894 → **3,551 (−48 %)** | 1,836 → **832 (−55 %)** |
| build/cure same-day correlation | 0.831 → **0.916** FAIL→PASS | 0.882 → **0.907** FAIL→PASS |
| median GT wait vs τ\* | 5.56 → **5.09 h** FAIL→PASS | 6.64 → **5.45 h** FAIL→PASS |
| same-size share | 68.0 % → **72.9 %** FAIL→PASS | 100 % | 
| R5 max wait | 70.9 → **68.4 h** | 70.7 → 71.3 h |
| **L11** | **27/42 → 30/42** | |
| **price** | last-day GT inv 4,767 → 4,040, PASS→**FAIL** | mean GT inv 1,238 → 1,194, PASS→**FAIL** vs the 1,200 floor |

The two closing-stock regressions are the evidence that the closing-mix fix (§4b.3) is
**not optional**: carry-in makes presses run later, which drains the closing balance.

### 4b.2 Day 1 closes — via press state, NOT via GT stock

The user's proposal was to carry the last days' output as next month's GT inventory. The
stock route provably does not close; the press route does.

| PCR day-1 press-hour budget (roster 2,064 h) | |
|---|---|
| 59 carried presses running their July campaign — **zero GT cover required** | 1,025 h |
| those presses re-seating after the campaign ends | 316 h |
| 27 cold presses, after the 11.86 h τ\* wall | 328 h |
| **total with no extra GT** | **1,669 h = 80.9 %** |
| hours still lost to the τ\* wall | **395 h** |

Cost of erasing the remaining 395 h:

| | tyres of cover needed | vs G8 rail | |
|---|---|---|---|
| PCR | **2,491** | 2,491 / 4,800 = **52 %** | rail half unused |
| TBR | **529** | 529 / 1,400 = **38 %** | |

**Day 1 reaches ~100 % on both plants using half the rail.** The 18,800-tyre figure in §4
Q7 is the cost of opening all 86 presses *from cold*; carry-in removes 59 of them from the
seating problem and the residual 27 cost 2,020 tyres. R5 permits it — 88 % of July's
hand-off still has a full 11.86 h of cover life at 07:00 (100 % on the `τ_min + 1 lot`
basis), and 100 % once §4b.3's gap-ordering fix lands.

### 4b.3 The closing mix — the measured blocker, with the allocation rule

Reproduced at the right grain on `ci_base`: **PCR 15 of 21 deficit GTs (1,008 tyres) are
below the 150 floor; TBR 17 of 18 (594) below 70.** Deficit p50 is barely half the floor.
Root cause is one line — `l7_pull_release.py:3135`, `_open_q = dict(_opening0)`: the
buffer's target *is* the inherited ~50-GT × ~90-tyre mix, so every per-GT deficit is
sub-floor by construction. Hours are 5× oversupplied (153 idle PCR machine-hours in the
last 66 h against a 2,240-tyre ask).

**The obvious allocation key is wrong.** Greedy by seat cost ignores the mounted mould:

| PCR, 27 cold presses | seats | ask | seats on the already-mounted mould | **day-1 mould changes** |
|---|---|---|---|---|
| cheapest-seat-first | 27/27 | 2,478 | 6 | **21 → 126 press-h** |
| **mounted-mould-first, then cheapest** | 27/27 | 2,538 | **25** | **2 → 12 press-h** |

60 extra PCR tyres buys back **114 press-hours ≈ 719 tyres of cure**. Key is
`(0 if mounted else 1, seat_cost, gt)` — and it cannot even be written until §4b.4 emits
the mounted mould on idle presses.

Also at `l7:3199`, gaps are sorted **largest-first**, so a top-up run lands in the biggest
hole rather than the latest one. Measured hand-off age max **63.6 h** — a tyre with 8.4 h
of shelf life covering a press that needs 11.86 h. Change the key to latest-ending-gap.

### 4b.4 The three ledgers, and what is missing from each

| what stays flat | mechanism | artefact | binding cap |
|---|---|---|---|
| **curing at 07:00** | press is **mid-campaign** — needs no t0 cover at all | `carry_out` → `carry_in` | none (occupancy) |
| **building at 07:00** | machine **mid-run**, so `earliest_cure` returns `t0+τ_min` | `build_carry_out.parquet` — **does not exist** | none (machine state) |
| the pipeline between them | GT stock for presses *not* carried in | `carry_forward_gt` → `PLANNER_OPENING_GT` | **G8 rail** |

**The rail constrains only the third.** That is why "0.37 days of production" is the wrong
yardstick — the hand-off is a coupling buffer `I = λ·W`, and 4,322 PCR tyres at 543 t/h is
W = 8.0 h, exactly one.

Three receiving-half defects, none previously found because the chain has never been run:

- **Rule T (tyres).** `carry_in` seeds `free[press]` but **creates no campaign row**, so a
  carried campaign's cures are counted in *neither* month. Leak Jul→Aug: **7,554 PCR +
  2,237 TBR**. Restated correctly, `ci_on` reads Aug PCR **90.69 → 92.47 %**, TBR
  **93.00 → 95.25 %**. Write that prediction down first — it looks like a +1.8 pt
  scheduling win and is not one.
- **Rule G (green tyres).** All 4,322 PCR carry-forward tyres are handed to M+1 as free
  opening stock, but July's carried campaigns need **7,900** of them after the boundary —
  **100 % of the hand-off is already committed**, leaving zero for M+1's cold presses.
  `carry_forward_gt` needs a `committed` flag and `usable_opening()` must subtract it.
- **Idle presses lose their mould.** `carry_out` emits only running campaigns, so the 27
  idle PCR presses (last campaign ended p50 14.4 h before the boundary, mould still on)
  arrive with `last_gt = None` and get a **free mould change** at `l5:1294-1295` — the same
  fiction `WARM_PRESS` was written to remove, one boundary over. Fix: emit one row per
  roster press, and move `last_gt[_pr] = ...` **above** the `continue` at `l5:1175-1178`.
  Bonus: `running_moulds_<M>` resolves 0 of 75 TBR presses; the planner's own carry-out
  resolves 79 of 79, so the TBR recipe-bridge gap stops mattering after the first month.

### 4b.5 The end taper is a demand-horizon defect, not a horizon-mode choice

Last-8-day lost press-hours, `sc_v18` July PCR: **1,475 h never-seated** vs 679 h
seated-but-unfed. L5 placed essentially everything (4 unplaced campaigns, 589 TBR tyres).
**The presses are idle because there is nothing left to seat** — `l45_lots_<M>.parquet`
holds only month M's cure demand. `truncate` makes it mechanically worse (`l5:778` sets
`tail_h = 0`); the measured ladder is already in the code at `l5:409-413`. **Keep
`extend` + `HORIZON_TAIL_H=72`.**

The lookahead that would fix it exists at `l4_net_requirement.py:120-152` and is **silently
a no-op**:

```
demand_2026-07.parquet : day 1..31, ~16,000/day     (MES-derived, phased)
demand_2026-08.parquet : ALL 528,165 tyres on day == 31   (order book, unphased)
```

`l4:142` filters `day <= lookahead_days` → **0 rows**, then prints *"lookahead: +3 d of
2026-08 (0 tyres) appended"* — a success message for a no-op. The comment saying the flag
is blocked because "masters/demand/ ends at 2026-07" is **stale**. Three fixes are needed
before arming it (pro-rata for unphased order books; `lookahead` is a GT-level flag so a
GT demanded in both months silently inflates M's denominator; and `l11:539-540` filters the
denominator but not the numerator). Plus a one-line seating guard so lookahead never
displaces in-month demand — prefix the `l5:862` sort key with `is_lookahead` rather than
replacing `-qty` (which is what `PLANNER_D1_DEPTH` did, at −11.9 pt).

Ceiling if it works: **Jul PCR +4,000…+9,000 BUILT** (1,475 idle press-h × 6.31).

### 4b.6 Two corrections to the framing arithmetic

**The interior rate decays.** PCR effective tyres/press-hour runs 6.545 on days 2–5 and
6.311 over the month, because `l5:862` sorts `-qty` and PCR's biggest GTs are also its
fastest — the mix drifts slow. Extrapolating the day-2–5 rate × 31 **overstates the PCR
ceiling by ~15,000 tyres**. TBR is flat at 1.67 all month (cure times 42–60 min,
homogeneous), so the TBR extrapolation is sound.

Restated honestly:

| | plannable | honest ceiling | vs the flat-rate claim |
|---|---|---|---|
| Jul PCR | 397,288 | ~396,700–398,700 (**99.9–100 %, ~0 spare**) | claim said 9,153 spare — that spare is the mix error |
| Jul TBR | 97,436 | 96,994 (99.5 %) | narrowly refuted |
| **Aug PCR** | 423,796 | **~397,100–398,700 (93.7–94.1 %)** | claim said 97.9 % — **refuted by ~16,000** |
| Aug TBR | 99,539 | 97,469 (97.9 %) | sound |

**So flattening both ends is the complete answer for July and lands exactly on the ceiling
with no margin.** For August PCR it recovers 1,910 press-h ≈ 12,052 tyres (90.8 % → ~93.7 %)
and that is all of it; the remaining ~6 pt is the load/unload ruling and the press roster.

### 4b.7 Debt-spiral warning — add an invariant

Carry-out is growing: **Jul→Aug 10,156 tyres; Aug→Sep 32,158.** A rolling contract with an
over-committed month does not reach steady state, it accumulates. `l4b_capacity_flow.py`
is building-only (`:69, :117-128`) and cannot see it. Add both: a curing term in L4B that
prints INFEASIBLE with a number before L5 seats anything, and an L11 invariant
`carry_out(M) ≤ 1.25 × carry_in(M)`.

### 4b.8 Ship order

1 carry-in (**measured**) → 2 full press-state snapshot → 4 latest-gap ordering → 3
closing-mix retarget (**month-pair test only**) → 7 Rule-T numerator → 5 build carry-out →
6 lookahead. Items 1, 2, 4, 7 are correctness fixes and ship whatever they measure.
`masters/carry_in/` must become a real directory with a month stamp and a preflight ERROR —
the same gate `gt_machine_partition` has and `cap_ttl_groups` lacks. **Do not omit it a
third time.**

---

## 5. Structural changes — the flow, not the knobs

Four changes to the architecture itself, in the order they should land.

**1. Add a curing-capacity gate to Phase A.** `l4b_capacity_flow.py` is **building-only** —
its flow graph is `source → GT → machine → sink` with `cap_h = horizon_h × UTIL_CAP`
(`:69, :117-128`), and grep for `press` returns only comments. **The engine has never
asked "can curing absorb this month?" before planning it.** That is the structural reason
P5 was found late and is still understated. Curing is the binding constraint on both
plants; the feasibility layer must model the binding constraint. Emit the press-hour
ceiling table from §0 as a preflight artefact, and let it print INFEASIBLE with a number
before L5 runs.

**2. Make the eligibility matrix a single resolved object.** The TT/TL defect exists
because `l2_capability` scores against `cm` while `l7` plans against
`restrict∘restrict_rimlock∘restrict_rimset(cm)`. Resolve eligibility **once**, write it to
one parquet, and have every layer read that. This is the same class as the
`input_derived()` / `wh_derived()` split the repo already documents — two views of one
truth, silently diverging.

**3. Make the month boundary a contract, not a convention** (S6). Two-sided carry,
asserted by an L11 invariant that spans months. Until this lands, every fulfilment number
is quoted under an undeclared ruling and month-over-month comparisons are not sound.

**4. Give the LNS loop a causal identity** (S7b/S7c). Today the loop's input
(`qty_unfed`) is a FIFO positional artefact and its output (`_DELAY`) is keyed one level
too coarse. Both ends of the feedback loop are non-causal. `cuid` fixes both with the
same key.

Two things **not** to change, both confirmed by measurement rather than taste:
**curing-first** stays (build-first was gen-1 and measured worse; R5 makes running ahead
scrap), and **long campaigns** stay (`−43,104 BUILT` when capped).

---

## 6. Execution plan

All arms fresh via `scripts/run_arm.py` — never `cp -r`, never A/B against an existing
directory (`RunContext` does not hash `PLANNER_*`). Gate every arm with
`scripts/check_arm_fresh.py`. Judge on **BUILT excluding the `OPENING_STOCK`
pseudo-machine**, PCR and TBR separately, never on a plant total.

| # | change | type | expected | status |
|---|---|---|---|---|
| 1 | **S1+S2 together** TT/TL restricted scoring + capacity gate | ~40 lines | **+5,253 TBR** (`TL={5,6}`) | **MEASURED. Ship as a pair — S1 alone still decides by alphabetical tiebreak.** Needs plant to confirm TBR5 |
| 2 | **S2b** MES-free re-scorer + preflight gate | ~80 lines | 0 tyres; removes the hand-edit | **do this before #1 lands**, or the group file has no reproducible provenance |
| 3 | **S3** `PLANNER_LOAD_UNLOAD_MIN=2.5` | flag | ceiling +19,285 | needs written plant sign-off |
| 4 | **S5** LCV press selection | ~15 lines | +2,000…+3,100 PCR | PREDICTED — highest remaining engineering item |
| 5 | **S7b/c** campaign-grain LNS + causal reconcile | ~60 lines | +2,000…+6,000 Jul | PREDICTED |
| 6 | **S6** carry contract | 1 file + 1 invariant | 0 Aug, −6.3 pt Sep (correct) | write the prediction down first |
| 7 | **S8** correctness set | small | ~0 | ship on correctness |
| 8 | Re-grade the 8 rejected changes **on July** | none | unknown — that is the point | cheapest information available |
| — | ~~**S4** day-1 mould de-serialisation~~ | 3 lines | ~~+800…+1,400~~ → **−428 PCR** | **MEASURED, REJECTED on volume.** Optional as a correctness fix (+1 L11, starved −496) |
| — | ~~**S7a** `_seat0` guard~~ | 2 lines | — | superseded — go straight to S7b once S1/S2 land |
| — | **S9** retarget the closing-buffer mix | ~30 lines | +1,190…+1,490 | month-pair test only; deprioritised with P1 closed |

### Reproduce the measured result

The TT/TL group is a **file, not a flag**. The shipped-baseline md5 is
`d056e4c6ec8e3c1ce3d63d0d10700490` — verify it on restore.

**Do not hand-edit the parquet for anything but a throwaway probe.** Per S2b the file is a
pure function of committed masters, so the arm should be built by regenerating it:

```bash
cd /c/Users/91810/Downloads/send/ctp-planner/schedule/send
export PYTHONIOENCODING=utf-8 PYTHONPATH=. \
       PLANNER_OPENING_GT=opening_gt_manual_2026-08.parquet \
       PLANNER_LOT_INTERVAL_H=8 PLANNER_TH_GT_WIP_RAIL_MARGIN=1.0
cp warehouse/derived/cap_ttl_groups_2026-08.parquet /tmp/ttl.bak

./.venv/Scripts/python.exe scripts/run_arm.py p3_base --month 2026-08     # TL={6,9}

./.venv/Scripts/python.exe scripts/rescore_ttl_groups.py 2026-08 --write  # -> TL={5,6}
./.venv/Scripts/python.exe scripts/run_arm.py p3_fix   --month 2026-08

cp /tmp/ttl.bak warehouse/derived/cap_ttl_groups_2026-08.parquet   # ALWAYS restore
md5sum warehouse/derived/cap_ttl_groups_2026-08.parquet            # d056e4c6...
```

`rescore_ttl_groups.py` must print the full tie set, not just the winner — August's is
2-way and that fact is the reason S2 exists. Run it on **both** months every time; July
having a unique winner is the regression guard.

Diagnostics: `PLANNER_L7_DIAG=1` writes `l7_place_diag.parquet` with the **measured**
refusal gate (`before_t0` / `r5` / `wip_rail` / `floor_gate`) — side-effect-free, verified
bit-identical on BUILT, starvation and all 42 invariants. **Always read the gate from
there, never from `build_starved.reason`.**

---

## 7. Where the tyres actually are — §7 rewritten

The doc's §7 adds three incompatible currencies. Separated (August):

| | PCR | TBR | owner | currency |
|---|---|---|---|---|
| **capacity deficit — cannot be created by anything** | **29,585** | 1,279 | physics | gone |
| ↳ recoverable by `LOAD_UNLOAD=2.5` | (9,968) | (9,317) | **plant, already stated** | ceiling |
| ↳ recoverable by the 86→92 press ruling | (28,025) | (7,444) | plant ruling | ceiling |
| **search headroom (total)** | **10,861** | **8,778** | | |
| ↳ **TT/TL partition (S1+S2)** | — | **5,253** | **engineering — MEASURED, banked** | **creatable** |
| ↳ ~~day-1 press idle (946 h / 567 h)~~ | ~~5,940~~ | ~~948~~ | **nine experiments, none converted — treat as unreachable** | **strike** |
| ↳ **narrow-GT past-horizon (S5)** | **3,127** | — | **engineering** | **creatable** |
| ↳ ~~day-1 mould de-serialisation (S4)~~ | ~~1,381~~ → **−428** | +53 | measured negative | **strike** |
| ↳ days 24–31 taper + mould cluster | 4,226 | ~3,000 | mostly **relocation**, not creation | discount it |
| **master data: `GT 1772 NEO` has no eligible press** | **1,182** | — | plant | creatable |
| **`min_demand_units` residual** | 2,232 | 283 | plant ruling | creatable |
| **horizon — genuine August carry-out** | **4,186** | 1,308 | plant; **crediting it double-counts September** | accounting |
| ~~horizon — tail-built~~ | ~~13,483~~ | ~~1,793~~ | **not August's under any ruling** | **delete from the ledger** |

**Revised: ~34,000 tyres behind plant decisions and physics — of which 19,285 is one
already-stated load/unload correction — and ~11,100 genuinely in engineering's gift, of
which 5,514 is measured and one line away.** The doc's "~32,000 plant / ~8,800 ours" is
the right shape but assigns 15,276 artefact tyres to the plant and understates TBR's
engineering share by 5×.

---

## 8. The rule this audit produces

The doc's §0 rule — *judge every change on BUILT* — was right and should stay. Add a
second:

> **Judge every change on the plant-month that has headroom.** Aug PCR has 2.55 pt of
> search headroom against Jul PCR's 5.23 and Aug TBR's 8.89. Grading a scheduler change
> on Aug PCR is grading it against a ceiling it cannot reach. That, not "constraints are
> mutually load-bearing", is why 8 of 9 measured negative.

And a third, which this audit is itself an instance of:

> **A reason string is not a measurement.** Three of this document's four largest
> engineering items traced back to a label, a grain, or a guard that never fired. Before
> building a fix, confirm the gate fired — `PLANNER_L7_DIAG=1` exists for exactly this,
> and it is free.
