# ENGINE FLOW v4.0 — Mould-Anchored Weekly Pull (MAWP)

Supersedes the layer structure of [ENGINE_FLOW.md](ENGINE_FLOW.md) (CMBC v3). The
**architecture** of v3 is not in dispute — curing sets the rhythm, building is
released backwards from it. What v4 changes is **the order in which the three
coupled decisions are made, and what solves each one.**

Every number below is measured on July 2026 unless marked as a target. Sources:
`scripts/plant_run_baseline.py`, `scripts/plant_gap_analysis.py`, and the
experiment ledger in [MEMORY.md](MEMORY.md) §10d.

---

> ## ⚠️ BUILT AND MEASURED — read this before the design below
>
> v4 was implemented Aug 2026. Result is `runs/v12`. Full ledger in
> [MEMORY.md](MEMORY.md) §10d. What the design got right and wrong:
>
> | PCR | v6 baseline | **v12 shipped** | plant |
> |---|---:|---:|---:|
> | lot p50 | 192 | **335** | 363 |
> | build changeovers | 1,390 | **881** | 742 |
> | same-size share | 32.3 % | **78.2 %** | 91.5 % |
> | **weighted setup h** | 750 | **265** | 171 |
> | fulfilment | 98.1 % | **98.4 %** | — |
> | GT inventory | 5,119 | **6,946** | 4,772 |
>
> **RIGHT — M1/M2 (eligibility + rim lock).** The changeover defect really was a
> data-selection error. Demoting INCH 500 -> 5,000 and locking machines to the
> 8-month mined rim assignment took same-size 32 -> 78 % and setup 750 -> 265 h.
> **This is the only change in the whole plan that is not a trade.**
>
> **RIGHT — rolling horizon.** Carry-out is worth +0.8 pt fulfilment alone.
>
> **WRONG — M4 as an inventory lever.** The design equation assumed
> `r = n_g x press_rate`. Measured, our n_g is ALREADY 3.04 against the plant's
> 3.25, and we get 69 % of the nominal drain rate where the plant gets 100 %.
> The n_g seating module (`planner/cmbc/l5_ng.py`) delivers lot 451, setup 116 h
> and inventory 5,466 — better than the plant on three counts — at **85.1 %
> fulfilment.** Fifth formulation in a row with that shape.
>
> **THE STRUCTURAL FINDING — there is ONE dial, not three.** Lot size, changeover
> cost and inventory are all set by how much cure demand one build run absorbs.
> T, the span cap, the run target and the lot floor are the same dial renamed;
> each trades the others. The plant sits OFF this curve, and the candidate
> explanations are now ruled out: n_g matches, draw rate matches, overbuild is
> zero, build pacing is capacity-infeasible.
>
> **§3 of this document still stands** and is the most transferable result:
> conjunctive resource constraints cost 20-35 % of demand at 95 % utilisation;
> additive and preference formulations cost <= 0.5 pt.

## 0. WHERE v3 STANDS, MEASURED

| | v3 shipped | plant July | verdict |
|---|---:|---:|---|
| demand fulfilment | 98.1–98.5 % | — | ✅ |
| cure changeovers / month | 170 / 110 | 170 / ~80 | ✅ matched |
| R5 breaches | 0 | — | ✅ |
| press utilisation | 95.3 % | 96.2 % | ✅ |
| **build lot p50 (PCR)** | **192** | **363** | ❌ |
| **build changeovers, weighted** | **750 h** | **171 h** | ❌ 4.4× |
| **same-size share (PCR)** | **32 %** | **92 %** | ❌ |
| **GT inventory** | **5,851 / 1,986** | **4,772 / 1,743** | ❌ |

Three failures, and they are the three the plant named. Everything else is done.

---

## 1. THE THREE ROOT CAUSES — each traced to a specific input or decision

### 1.1 Lot size — the interval is monthly, the plant's is weekly

`Q_g = r_g × T`. That form is right (the plant sizes lots by **time**, not a fixed
quantity — confirmed by the plant team). The defect is `T`. v3 derives it from a
month-long inventory target and gets **T = 6.42 h**, which puts `r_g × T` below
the B12 floor on **80 % of PCR and 100 % of TBR GTs**, so the floor — not demand —
sets every lot.

Measured: at **T = 24 h** the PCR lot is 403 against the plant's 363, and build
changeovers fall to 2.05/machine-day against the plant's 2.18 — **at zero
fulfilment cost**. The lot problem is solved by planning in weekly buckets.

### 1.2 Changeovers — we plan on CAPABILITY, the plant runs on HABIT

This is the finding that matters most, and it is a data-selection defect, not an
algorithm defect:

| eligibility source | PCR machines per GT |
|---|---:|
| `cap_machine_2026-07` (**what the engine uses**) | **9.75 of 11** |
| `allowed_machine_matrix` (**mined from 8 months of MES**) | **1.7** |
| plant actual, July | **3.28** |

v3 plans against the INCH-capability union — 381 of 468 PCR pairs carry basis
`INCH`, meaning *"the machine can physically hold this rim"*, never demonstrated.
With 9.75 options per GT the scheduler spreads every GT across every machine and
mixes sizes freely. The plant uses 1.7–3.3.

Consequence, measured: **91.8 % of the plant's PCR build changeovers are
same-size (11.3 min); only 32 % of ours are, and a different-size swap costs
42.4 min.** Weighted: **750 h against the plant's 171 h.**

> **The 8-month mined matrix is the plant's revealed policy and must be the
> PRIMARY assignment basis. Capability is the escape hatch, priced, never the
> default.** This inverts v3's `basis` ranking.

### 1.3 GT inventory — set by drain rate, and drain rate is set by moulds

`I = λ·W`, `W = τ* + (Q/2)(1/r − 1/b)`, `r = n_g × press_rate`.

Solving the plant's observed `W = 8.84 h` for `r` gives **22.3 tyres/h = 3.25
concurrent presses**, against its independently measured n_g of 3.28–3.42. Ours
implies **1.99**. The model closes on both sides to two decimals.

So inventory is not a lot-sizing problem and not a release-timing problem. **It is
the number of presses concurrently mounted on a GT** — which is exactly what the
plant team described: *check mould availability first, take one or two extra
moulds, then place.*

Mould data supports it: PCR holds **367 moulds for 48 GTs** (p50 4, mean 7.6), and
`observed_max` never exceeds `moulds` — the master does not understate. We are
using 2 of a median 4.

---

## 2. MOULD FILE STATUS — checked, and the counts are sound

| field | state | impact |
|---|---|---|
| mould **count** per GT | complete, PCR 367 / TBR 382, `observed_max ≤ moulds` always | ✅ usable as the n_g cap |
| **cavity count** (`Full_Load`) | **absent** | ⚠️ press rate is derived from MES (PCR 3.43 / TBR 2.41 effective), not physical |
| **maintenance status** (`UserStatTxt`) | **0 of 1,614 rows populated** | ⚠️ every mould assumed available; no PM downtime modelled |
| `CurrStat` | 100 % populated, 100 % one value | no discrimination |

**Verdict: the mould file is not the problem for n_g.** The counts are sound and
we are under-using them by ~2×. The two real gaps (cavities, maintenance) affect
the *rate* and *availability realism*, not the concurrency decision.

---

## 3. WHY GREEDY FAILS HERE — the structural reason

Six formulations were built and measured. The pattern is unambiguous:

| formulation | constraint type | fulfilment |
|---|---|---:|
| rigid rectangle per (GT, press) | **conjunctive** | 65.9 % |
| split into k campaigns | **conjunctive** | 76.8 % |
| ragged additive seating | **additive** | **89.2 %** |
| per-GT machine pinning | conjunctive | −6.4 pt |
| GT clustering in L5 | conjunctive | −6.4 pt |
| deadline ordering, rim preference | **preference** | −0.4 pt |

> ### DESIGN RULE, earned
> **Conjunctive resource constraints — "these N resources must be simultaneously
> free" — cost 20–35 % of demand at 95 % utilisation. Additive and
> preference-based formulations cost ≤ 0.5 pt.** Express every requirement as an
> integral to accumulate or a preference to rank, never as a simultaneity test.

Greedy is not the failure. **Single-pass greedy over a coupled triple is.** Lot
size, machine assignment and press concurrency are mutually determining:

```
lot size Q  ←  interval T  and  drain rate r
drain rate r ←  n_g presses  ←  mould availability
changeover  ←  machine assignment  ←  rim purity
machine assignment  →  which runs are adjacent  →  changeover cost
```

v3 decides all three in one forward pass, so whichever is decided first
constrains the other two. That is why every fix traded against another metric.

**The counter is decomposition, not a better solver.** Each sub-problem is small
enough to solve well on its own:

| sub-problem | size | method |
|---|---|---|
| rim → machine partition | 7 rims, 11 machines | **exhaustive enumeration** (precedent: L2 solved C(9,6)=84 TT/TL partitions exactly) |
| n_g per GT per week | scalar per GT | closed form, capped by moulds |
| press seating | many GTs × presses | **additive greedy** — proven to pack (89.2 %) |
| sequence within a machine | ≤ 7 size classes | **exact TSP per machine** |
| weekly repair | whole plan | **LNS** — destroy one week, re-solve |

No MILP, no CP-SAT. Exact where small, additive where large, local search to
polish — consistent with the project's locked constraints.

---

## 4. THE v4 FLOW

```
┌──────────────────────────────────────────────────────────────────────────┐
│ M0  LEARN (unchanged from L0)  — parameters only, never policy           │
│     tau*, campaign bands, yields, cure cycle BY GT (2.0x spread — v3     │
│     uses one plant median and must stop)                                 │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ M1  ELIGIBILITY, RE-RANKED                    ⭐ FIXES CHANGEOVERS (1/2)  │
│     PRIMARY   allowed_machine_matrix  — 8-month MES, 1.7 mach/GT PCR     │
│     FALLBACK  certified / observed    — priced                           │
│     ESCAPE    inch capability         — priced high, reported, never     │
│               silent.  v3 used this as PRIMARY. That is the defect.      │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ M2  RIM → MACHINE PARTITION (horizon-fixed) ⭐ FIXES CHANGEOVERS (2/2)    │
│     load: R13 3.0 · R15 1.4 · R18 1.2 · R12 1.1 · R17 1.0 · R14 0.9 ·   │
│           R16 0.8  =  9.4 machine-equivalents on 11 machines             │
│     ceil() sums to 12 > 11, so EXACTLY TWO machines must be mixed —      │
│     ~91.8 % purity is the mechanical maximum, which is precisely the     │
│     plant's figure. SOLVE for which two: enumerate partitions, minimise  │
│     expected weighted changeover. Keep R13's 3.0 clean on three          │
│     dedicated machines; mix the low-remainder rims (R16 .8, R14 .9).     │
│     The two mixed machines are ALSO the designated overflow.             │
│     TBR: 2 rims (R20 5.2, R22.5 2.5) → 6/3, clean, already 100 %.        │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ M3  WEEKLY BUCKETS                                    ⭐ FIXES LOT SIZE   │
│     Horizon = 4–5 weeks. Plan week by week (plant's own practice).       │
│     T = the weekly interval, NOT a month-derived inventory setpoint.     │
│     Q_g = r_g x T, floored at B12 (PCR 150 / TBR 70), capped by shelf    │
│     life. Measured: T=24 h gives PCR lot 403 vs plant 363 at zero        │
│     fulfilment cost.                                                     │
│     ⚠ B12 is a FLOOR, not a target: the plant itself runs 12.7 % (PCR)   │
│       and 30.8 % (TBR) of its runs below it. Do not chase 0 %.           │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ M4  MOULD-ANCHORED CONCURRENCY                       ⭐ FIXES INVENTORY   │
│     n_g = min( moulds_g , ceil(W_g / (D_g x 24)) )   target ~3.3 PCR     │
│     This is the plant's stated method: check mould availability, take    │
│     1-2 extra, then place. n_g sets r, r sets W, W sets I = lambda x W.  │
│     ADDITIVE SEATING — never conjunctive:                                │
│         seat presses one at a time, each at its own earliest feasible    │
│         start, accumulate press-hours until W_g is met inside a window   │
│         of length W_g / n_g.                                             │
│     NO dispersion floor — swept {0, .4, .6, .8}; floor 0 gave the BEST   │
│     fulfilment (89.2 %) and inventory 4,361, already under the plant.    │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ M5  PULL RELEASE — unchanged from L7, it is correct                      │
│     release(run) = min over slices ( t_cure − tau* − cum x cadence )     │
│     Placement in GLOBAL CURE-DEADLINE ORDER (shipped v6: recovered the   │
│     wait p95 gate 32.3 → 24.3 h). Split-before-starve on failure.        │
│     Build ONLY for the next required cure window — the deadline order    │
│     already enforces this; do not add a second mechanism.                │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ M6  SIZE-BLOCKED SEQUENCING (post-pass)      ⭐ FIXES CHANGEOVER COST     │
│     Runs assigned to a machine are FIXED. Only their ORDER changes.      │
│     Group into size-class blocks, exact TSP over blocks (<= 7 classes),  │
│     deadline order inside each block.                                    │
│     ADMISSIBILITY: an ordering is legal only if EVERY run still meets    │
│     its latest feasible release. Among legal orderings take min Σc.      │
│     This is why it cannot cost inventory the way rim-preference did —    │
│     there we bought a size match with W on every run; here we take the   │
│     saving only when it is free in W.                                    │
│     ⚠ CEILING IS LOW: rim ⊥ deadline (between/within sd = 0.17 PCR).    │
│       Expect partial gains. Post-pass only — must NOT shape M2–M5.       │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ M7  LNS REPAIR — destroy one week, re-solve M3–M6, keep if better        │
│     Objective = the lexicographic tiers below. Time-boxed.               │
│     This is where "greedy is not enough" is actually answered.           │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ M8  VALIDATE + EXPLAIN — L11/L12 plus the three gates v3 lacks           │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 5. OBJECTIVE — weighted changeover replaces the count

Raw changeover counts have misled twice: `exp_split` looked like plant parity on
count and was in fact the **best weighted result we had**. From v4 onward:

```
c(i→j) =  0                     same GT
          11.3 min              same rim   (MEASURED plant July, not the master)
          42.4 min              rim differs
```

The changeover master says 22–28 / 42–60. **Measured reality is 11.3 / 42.4 —
same-size swaps are cheaper than the master claims, so the size mix matters MORE
than the master implies.** Use the measured figures; report both.

Lexicographic tiers, unchanged from v3 except tier 6/7:

| tier | objective |
|---|---|
| 1 | demand fulfilment |
| 2 | GT age ≤ 72 h (hard) |
| 3 | cure campaign integrity |
| 4 | building capacity (hard) |
| 5 | GT inventory band (G8) — **now gated, see §6** |
| 6 | **weighted** cure changeover hours |
| 7 | **weighted** build changeover hours — below tier 6, deliberately |
| 8 | rim-partition breaks — priced, reported, never silent |
| 9 | sister grouping |

---

## 6. INVARIANTS — three that v3 does not have

| invariant | target | why |
|---|---|---|
| **G8 daily GT inventory in band** | PCR 4,500–4,800 · TBR 1,200–1,500 | **v3 has NO inventory gate at all.** A WIP regression of 4,147 → 5,851 went unnoticed for the entire session. |
| **weighted build changeover h / machine-day** | ≤ plant 30.2 min (PCR) | raw count hid a 2× difference twice |
| **same-size share of build changeovers** | ≥ 70 % | direct measure of the M2 partition working |
| realised n_g (measured, not requested) | ≥ 3.0 PCR | a packer that silently degrades to 2.4 looks fine on every other metric |
| build runs below B12 floor | ≤ plant's 12.7 % / 30.8 % | **not 0** — the plant breaks its own floor |
| cure campaign length p50 | 40–75 h PCR · 200–330 h TBR | v3 tests against an "85 h band" in no source document and passes 192.9 h |

---

## 7. WHAT IS PROVEN, AND WHAT IS NOT

**Proven by measurement:**
- T = 24 h → PCR lot 403, changeovers 2.05/machine-day, zero fulfilment cost
- additive seating → fulfilment 65.9 → 76.8 → **89.2 %**, inventory **4,361, below the plant**
- deadline-ordered release → wait p95 32.3 → 24.3 h, inventory −732
- rim-aware selection → same-size 32 → 70 %, weighted 750 → 242 h
- n_g is the inventory lever: model closes to 2 decimals on both sides

**Not yet proven:**
- that M2 + M4 **compose** — each has been run alone, never together
- that additive seating reaches 95 % fulfilment (best so far 89.2 %, 20 GTs strand)
- that M6 gains anything — rim ⊥ deadline caps it, PCR-only (TBR has **zero**
  different-size swaps in the entire plant month)

**Falsified, do not retry:**
- rigid one-campaign-per-(GT,press) — 65.9 %
- per-GT machine pinning — over-constrained at 3.6 GTs/machine
- dispersion floor on concurrency — costs 9–13 pt of fulfilment, buys ~400 tyres
- "concurrency and changeover are the same lever" — ragged seating held n_g and
  same-size **collapsed to 43.5 %**; the adjacency came from rigid structure

---

## 8. MIGRATION FROM v3

M0/M5/M8 are L0/L7/L11-L12 essentially unchanged. The new work is M1 (re-rank
eligibility), M2 (rim partition), M3 (weekly buckets), M4 (mould-anchored
additive seating), M6 (size post-pass), M7 (LNS).

**Build order — each independently measurable, each falsifiable alone:**

1. **M1** — one-line basis re-rank. Highest value per line of code in the project.
2. **M3** — weekly T. Proven at T=24; no new logic.
3. **M4** — additive seating at n_g anchored to the plant's 3.25, not 4.4.
4. **M2** — rim partition. Compose with M4 and re-measure; do not tune separately.
5. **M6** — post-pass only, if M2 leaves anything on the table.
6. **M7** — last. Do not build a search until the greedy it repairs is correct.

Report on every run, in this order: **fulfilment, GT inventory, weighted
changeover hours, same-size share, realised n_g, lot p50.** Fulfilment first —
it is the only thing that has ever failed catastrophically.
