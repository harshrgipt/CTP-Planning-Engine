# Month-end dip & fulfilment plan — assessment before implementation

**Written 2026-08-21. NOTHING HAS BEEN APPLIED.** All five proposals assessed against the
current shipped defaults and the measurement record. Each is marked
**ALREADY SHIPPED** / **BLOCKED** / **TESTABLE** / **REJECTED-BY-MEASUREMENT**.

Read `PARTITION_AND_CHANGEOVER.md` §4aj, §4am, §4ae, §4aw, §4ax before acting on any of it.

---

## 0. FIRST — the baseline numbers in the proposal do not match the shipped plan

The proposal quotes **PCR 90.6 % / TBR 93.7 %**, PCR day-31 build **10.5k**, TBR **2.18k**,
starvation **9.5–10.8k**. None of those is the current shipped state.

| | proposal | **shipped `runs/SHIP2_aug`** |
|---|---|---|
| Aug PCR fulfilment | 90.6 % | **92.59 %** |
| Aug TBR fulfilment | 93.7 % | **97.89 %** |
| Aug PCR day-31 build | 10.5k | **8,485** |
| Aug TBR day-31 build | 2.18k | **1,156** |
| Aug PCR starved | 9.5–10.8k | **12,477** |

90.68 % is `RL_aug` — the **`PLANNER_ALLOWABLE_RESCUE=0`** experiment, not a baseline.
**Whatever pack the proposal was read from, it is not the shipped plan.** Every number below
is from `runs/SHIP2_jul` / `runs/SHIP2_aug` and the engine's own `build_by_shift` /
`cure_by_shift`, never hand-derived (hand-deriving plant-days is §4ak.2 and I made that
mistake twice this session).

**Current shipped fulfilment:** Jul PCR **96.49 %** · Jul TBR **97.87 %** ·
Aug PCR **92.59 %** · Aug TBR **97.89 %**.

---

## 1. Regenerate a trustworthy baseline — **DO THIS FIRST, and it is my fault it is needed**

**Status: ACTIONABLE, blocking everything else.**

`masters/holidays_2026-08.json` was deleted during this session's holiday work (by me), and
the wrapper-root `holiday.csv` that now feeds the loader carries **only a July row**. So:

- `runs/SHIP2_aug` was built with **15 August closed** (`booked on 20 machines`).
- An unpinned August arm today has **no closure at all**.
- Any comparison across that boundary is contaminated. The last surgeon had to pin
  `PLANNER_HOLIDAYS=2026-08-15` on every arm to keep the baseline still.

**The plant said 15 August was a trial.** So the decision is yours and it is not free:
removing it *raises* August BUILT (the closure cost ~1,287 in-month PCR tyres when measured),
which means **every August number in this session moves.**

The proposal's reporting split is right and the engine already produces all six — but two of
them are routinely confused (§4ay), so use these exact sources:

| quantity | source | Aug PCR |
|---|---|---|
| built in August | `build_by_shift.parquet` | 402,612 |
| cured in August | `qty_fed_in_month` | 397,326 |
| cured in the September tail | `qty_fed − qty_fed_in_month` | **12,620** |
| **GT physically carried into September** | **`carry_forward_gt.parquet`** | **4,514** |
| starved press demand | `qty_unfed` | 12,477 |
| daily profile | `build_by_shift` / `cure_by_shift` | — |

**The tail and the carry-forward differ by 2.8x** and are not interchangeable. "Those tyres
are already built, just cure them" is true of 4,514, not 12,620 — the rest are built in the
72 h tail and do not exist in August.

---

## 2. PCR curing levelled by plant-wide takt — **ALREADY SHIPPED**

`l5_cure_master.py:495` `PLANNER_L5_TAKT_PLANTS` default **`"PCR,TBR"`**
`l5_cure_master.py:611` `PLANNER_L5_TAKT_PART_PLANTS` default **`"TBR"`**

Exactly the configuration the proposal describes. Shipped 2026-08-20, ledger §4am, gated on
two months and two plants:

| | Jul PCR | Aug PCR | TBR |
|---|---|---|---|
| BUILT | +4,693 | **+4,985** | byte-identical |
| in-month | +0.16 pt | **+0.27 pt** | 0.00 |
| starved | 11,562 → 6,545 | 12,686 → 7,179 | unchanged |
| weighted changeover | 79.5 **FAIL** → 64.3 **PASS** | 86.3 → 78.0 | — |

Jul PCR d27–31 build, % of interior median: **51/25/15/4/30 → 100/99/80/46/40**.

> **The +4,985 is already inside the 92.59 %.** Counting it again as a future gain
> double-counts. The proposal's "expected outcome" does exactly that.

**Honest cost, from §4am:** the tail fills partly by borrowing. August window decomposition —
interior d3–26 **−10,373**, tail d27–31 **+15,051**, net +4,985. **Only 33 % of the tyres
appearing in the tail are new output.** Total machine-hours moved 6,772 → 6,820 of 8,184: the
gain is **48 machine-hours wide**, not 15,000.

---

## 3. Constrained GTs first in building — **ALREADY SHIPPED**

`l7_pull_release.py:949-950` `PLANNER_L7_PINNED_FIRST_PLANTS` default **`"PCR"`**,
`PLANNER_L7_PINNED_FIRST` default **`"8"`**. Ledger §4ae.

| | Jul PCR | Aug PCR | TBR |
|---|---|---|---|
| BUILT | +6,059 | **+5,625** | −335 / −122 → **excluded** |
| in-month | +1.52 pt | +1.31 pt | −0.43 / −0.16 |
| starved on GTs with <=2 machines | 79 % → 47 % | | |

PCR-only, exactly as the proposal says. **Also already inside the 92.59 %.**

> **Correction to a claim I made repeatedly and that the proposal inherits:** the mechanism is
> NOT that `allowed_machine_matrix` is too narrow. Measured against 8 months of MES, the
> matrix holds **864 PCR pairs against 176 the plant ever used** — it is ~5x *wider* than
> plant practice, and it is tighter than the plant on **0 of 23 starved PCR GTs**. Narrow
> per-GT reach is a **tooling fact** (one mould set, one machine), not a matrix artefact.
> **Do not send the plant a matrix-widening request.**

---

## 4. Terminal-state objective for the final 3–5 days — **THE ONLY GENUINELY NEW ITEM**

**Status: TESTABLE, not yet built. This is where the remaining effort should go.**

The diagnosis is correct — placement has no terminal preference. But three measured results
constrain the design hard, and a naive version will reproduce them:

**(a) A hard daily cap was measured and lost.** `PLANNER_L5_DAY_CAP` 13,000/3,200 —
Jul PCR BUILT +1,421 but in-month **381,854 → 380,490**; Aug PCR BUILT **−3,436**. The
proposal's "rolling three-day average, not a hard daily cap" is the right correction and it
should be kept.

**(b) "Penalize post-boundary curing" was measured as `PLANNER_L5_MONTHEND_FIT=require`
(§4aw) and it is the worst arm in the table:** Aug PCR tail **12,620 → 8,783** — the best
tail number produced all session — while destroying **9,604 BUILT** and −1.28 pt. Its softer
sibling `prefer` is a **structural no-op, byte-identical at N = 5, 7 and 10**, because
`dur = qty/rate` does not depend on the press, so completability is monotone in start time —
the key the greedy already leads on. **Any terminal objective must avoid collapsing into
either of those two.**

**(c) The premise "campaigns crossing month-end contribute zero" is false.** They are
**prorated** (`l7_pull_release.py:4434`). Of 57 crossing Aug PCR campaigns, **2** have
`frac == 0`; the other 55 already deliver their in-month share — 27,503 of 40,997 tyres.
**The prize is the 13,494 spill, not the 40,997.**

**The suggested priority order needs one change.** "Maximize in-month cured" first and
"maximize total physically built" third will select `require`-like moves — relocation that
raises in-month while destroying BUILT. In this engine **BUILT is the tail-insensitive
grading number** and in-month is the reporting number. Put **BUILT and in-month at the same
tier**, or the objective optimises the boundary rather than the plant.

**What I would test, in order:**
1. Rolling 3-day build/cure band penalty, terminal window only, default off.
2. Closing-GT-inventory band as a **soft** target — never a floor. `l4_net_requirement.py:110`
   states plainly that forcing a closing-stock floor "means building tyres with no cure to
   consume them inside the horizon, which destroys the audited built == fed invariant".
3. Unfed-campaign penalty — already the dominant term; verify it is not already saturating.

---

## 5. Next-month demand as lookahead — **BUILT, BLOCKED ON DATA, AND THE RIGHT ANSWER**

**Status: mechanism EXISTS and is a clean no-op today.**

`l4_net_requirement.py:100-123` implements exactly this — `--lookahead-days`, default 0 —
and its comment block is the same diagnosis the proposal makes, written earlier:

> *"`l45_lots_<M>.parquet` holds only month M's cure demand, so NOTHING PULLS BUILD on the
> last days of M … That is the G8 last-day failure, and it is a DEMAND-HORIZON defect, not a
> pacing one. The plant builds on July 31 for early-August cures."*
>
> *"This is the ONLY correct fix for that failure. Do NOT instead force a closing-stock floor."*

**The blocker is data, and it is one file.** `masters/demand/` holds `demand_2026-07` and
`demand_2026-08`. **There is no `demand_2026-09`.** August exists only because the plant sent
an order book (`August_Demand_PCR_TBR_Classification.xlsx`) — MES-derived demand ends
2026-07-31.

> **ACTION FOR THE PLANT: send the September order book.** That single file unblocks the one
> mechanism in this engine that is designed to fix the month-end dip at its cause. Ingest with
> `scripts/ingest_orderbook_demand.py`, then run August with `--lookahead-days 5`.

It can be measured **today** without waiting, on a month that has a successor: run **July with
August as lookahead**. That is a real two-month measurement and needs no new data. It is the
single highest-value experiment on this list.

---

## What not to do — agreed, with the measurements behind each

Every item in the proposal's "what not to do" list is correct. The numbers:

| do not | why |
|---|---|
| force day 31 to the monthly average | `DAY_CAP` 13,000/3,200: Aug PCR **−3,436** |
| shorten the 72 h horizon to reduce carry-out | tail tyres are September's opening stock; shortening deletes them |
| move tail curing in without checking BUILT | `require`: tail −3,837 for **−9,604 BUILT** |
| activate the mould divisor before the plant rules | div 2.0: PCR **−32,061**, TBR −3,363 — and `floor(moulds/2) < observed_max` on **62 % of PCR volume** |
| use a cure-rate haircut alone | plant-p50 calibration still leaves 10 days over the record; the only arm that clears it **breaches G8** (5,129 vs 4,800) and triples the tail |
| judge on in-month alone | §4ak.1, §4aw — three separate arms this session looked like wins on one axis |

**One more, from tonight's evidence:** the BTP screen's `Running Moulds: 7` counts **press
loads**, not mould pieces. The plant's own MES writes `mouldNo = HM01#HM02` on **99.9 % of
rows** — one "mould" is the LH+RH pair. Odd counts are correct. The mould-divisor question is
about whether `mould_inv_ctp_17072026.csv` uses the same convention, and only the plant can say.

---

## Realistic expectation — lower than the proposal's

The proposal expects "roughly 1–1.5 additional fulfilment points from constrained-first
ordering" plus more from takt levelling. **Both are already shipped and already counted.**
Re-running cannot deliver them again.

What is actually left, measured:

| lever | status | realistic |
|---|---|---|
| takt levelling (#2) | shipped | **0 additional** |
| constrained-first (#3) | shipped | **0 additional** |
| clean holiday baseline (#1) | actionable | moves August ~+1,287 in-month if 15 Aug is dropped |
| terminal objective (#4) | untested | unknown; bounded below `require`'s −9,604 and above `prefer`'s 0 |
| **September lookahead (#5)** | **blocked on one file** | **the only structural fix; measurable today on July+August** |

And the ceiling that no scheduling change reaches: **Aug PCR carries 18,139 tyres of demand
above the plant's own observed cure ceiling** (L11: 426,688 vs 408,549 — FAIL). That is a
capacity question, not a plan question.

**Recommendation: do #1, then measure #5 on July-with-August-lookahead.** Everything else is
either already in the number or measured negative.
