# ENGINE LOG — `planner/engine/`, and every experiment run on it (Aug 2026)

Companion to MEMORY.md §10c. Everything tried on the curing-first engine,
including — especially — what failed and why.

```
python scripts/plan.py --demand <f> --opening <f> --month YYYY-MM --out <dir>
```

Nine phases, deterministic end to end: P0 contract · P1 masters · P2 feasibility
gate · P3 campaigns · P4 controller · P5 building · P6 shift grid · P7 fixed
point · P8 verify · P9 emit.

---

## 1. Determinism: PASS (byte-identical) — took three fixes

Two independent runs produce identical `build_schedule`, `cure_schedule`,
`press_campaigns`, `gt_events`. It did **not** hold on the first attempt:

| Source of nondeterminism | Fix |
|---|---|
| Cure sort not total (ties on `start_ts`) | total key incl. press + `cure_id` |
| DuckDB `SELECT *` has no row order | explicit `ORDER BY` |
| **`group_by().first()` picks a random group member** | pre-sort + `maintain_order` — the same press resolved to mould `HMI18` on one run and `HMI05` on the next |

The third would have made every regression test flaky and been blamed on the
scheduler.

---

## 2. Validated — 5 months, ~99%

| Month | Cure fulfil | Aging p95 | >72 h | Curing CO | Span |
|---|---:|---:|---:|---:|---:|
| Jan | 99.83% | 37.6 h | 0.38% | 44 | 736.0 |
| Mar | 99.28% | 39.3 h | 0.44% | 79 | 752.1 |
| May | 99.94% | 34.8 h | 0.32% | 52 | 736.0 |
| Jun | 98.95% | 36.8 h | 0.38% | 60 | 730.0 |
| Jul | 98.89% | 41.7 h | 0.34% | 65 | 741.7 |

Plant: 153 curing changeovers, 744 h, aging 27.6 h. No month is an outlier —
July is not the fitted case.

⚠️ **The 2.4x changeover win was measured at 95 presses, not the plant's 86.**
66 vs 153 is 2.4x only because we mounted 9 presses the plant did not run. At
the plant's own press list it is **94 vs 153 = 1.6x**. Press slack buys campaign
purity at a measured **~3.1 curing changeovers per press removed**, so the
difference was bought with mounted moulds rather than earned by sequencing.

⚠️ **These fulfilment figures are UNCAPPED.** Over-delivery on one GT offset
under-delivery on another, and 2026-01 read 100.14% -- impossible. Capped
per GT (`sum(min(cured_g, demand_g)) / sum(demand_g)`) every month lands
**1.1-1.4 pp lower**. The KPI now reports both.

---

## 3. New masters ingested — and what each was actually worth

| File | What it gave | Effect on the PLAN |
|---|---|---|
| `wcmaster 1.xlsx` | platen/rim for **173/175 presses**; eligible pairs 190 → 251 | −0.28 pp (model correct; ineligible shifts 9.9% → 5.3%) |
| `TBR BUILDING ALLOWABLE MATRIX` | **108 certified** GT-machine pairs, p50 3 vs 2 mined | ~neutral |
| `Recipemaster` + `recipelookup` | **GT→SKU 99.5%**, TBR 0% → 100% | **ZERO** — reporting only |
| `ALL PCR CTP SKUS` | PCR 239 GTs → 243 SKUs | zero |
| `mouldNo` (column, was unused) | **M_g at 2.7× resolution** | verification only — **0 infeasible schedules found** |
| Active-day rate correction | **TBR 48 → 42** | **96.53% → 99.16%** ← the only real gain |

**Five masters, one mattered.** TBR's 48 was a p95 (best-day) figure, not a
typical day — planning at it over-stated press capacity ~14%.

---

## 4. Five mapping traps — every one returns ~0 on the obvious join

| Join | Single key | Dual key needed |
|---|---:|---:|
| MES press ↔ work centre | **0/175** on `name` | **175/175 on `iD`** |
| MES GT ↔ recipe master | 56% on SAP code | **97.9%** + recipe `name` |
| MES GT ↔ TBR matrix | **0/83** on `GT CODE` | **86.7%** + description prefix |
| MES GT ↔ BOM | 55%, **0% TBR** | superseded by recipe |
| build ↔ cure | — | `productionID = gtbarCode` (99.6%) |

The ID spaces genuinely differ between systems. Any new integration hits this.

---

## 5. Curing was NOT being read wrong — confirmed by an independent key

`v_curing.recipeID` resolves **100%** (zero nulls) to `recipemaster.iD`, whose
`SAPMaterialCode` is the **finished SKU**. Route A (barcode → GT) and route B
(recipe → SKU) disagree on 100% of tyres because they name the two **ends** of
the same operation, not two answers to one question.

Every curing-derived number stands: press rates, campaign structure, aging, the
Little's Law inventory law.

---

## 6. FAILED EXPERIMENTS — do not retry without reading this

**All four were "a more correct constant". All four made the plan worse.**

| # | Attempt | Result | Why it failed |
|---|---|---|---|
| 1 | ρ gross-up 0.863/0.945 | aging 49.2 h but 227 press-days short | ρ **is** the fill ratio; grossing up books the same idle twice |
| 2 | `f_book` = 1.095 (reference plant) | peak 131 vs 92 presses | our presses run 30/31 days, not 28.3 |
| 3 | Measured booking margin 1/fill | aging 38.2 → **86.6 h**, over-72h 0.43 → 6.88% | compounds (1.044 → 1.132); mounting a press you cannot feed **starves** it instead of leaving it unmounted |
| 4 | **Per-GT rate** | **98.89% → 96.08% (achieved) / 94.66% (capacity)** | fails in **both** directions — see below |

### #4 is the most instructive

The per-GT signal is **real**, verified three ways *before* coding:

- stable across months — CV p50 **0.086**
- not a press artefact — same GT across ≥3 presses, CV p50 0.089;
  `GT 1844 XPC TML` holds 115/day over **15 presses at CV 0.06**
- physically consistent — **r(dwell, rate) = −0.772**

PCR GTs genuinely run **41–200** tyres/press-day against a 151 median, and the
flat rate is off by >20% on a **third** of them.

It still made the plan worse, in **opposite** directions:

```
flat active-day median (151/42)   98.89%   <- best
per-GT ACHIEVED      (p50 149)    96.08%   books ~11% too much press-time,
                                           packing tightens, GTs unplaceable
per-GT CAPACITY      (p50 156)    94.66%   books too little, GTs cannot finish
```

The flat rate is right **not because it is accurate per GT** — it isn't — but
because the errors cancel while the total stays correct.

> **The binding constraint is the build/cure COUPLING, not the rate.** Refining
> per-unit capacity cannot help while a GT's daily quantity is released in a
> 3-hour burst and then waits 19 hours.

### Also withdrawn: the cover law

`cover = a·draw^b` (PCR `173.1·draw^−0.470`). The law is **correct** — the plant
really does replenish high-runners more often (r = −0.65 PCR / −0.80 TBR) — but
applying it cut buffers our 22 h lag still needs: 99.16% → 98.74%, slope
150 → 203. **Re-enable only AFTER shift-level release.**

---

## 7. THE ROOT CAUSE, measured

We produce in **flow, not burst**. Lot sizes match the plant (PCR 288 vs 363
p50), burstiness matches (9.0% vs 10.0% biggest-day), we spread over **more**
days (18 vs 14). The defect is **release timing**:

| | Ours | Plant |
|---|---:|---:|
| Replenishment gap T_g (PCR) | **19.2 h** | 12.0 h |
| (machine, GT, day) block span | ~~3 h~~ **STALE** | 11 h |
| Runs per GT | 19 | 12 |

⚠️ **The 3 h block span is STALE** — it predates the release-gate fix and T_0=12.
Re-measured on the current engine, same definition: **PCR 16.47 h vol-weighted
(p50 8.81), TBR 13.37 h**, and volume-weighted delta is **0.643 in the top
quartile** which holds 239,826 of 394,225 tyres. We are **LESS bursty than the
plant (delta 0.458), not 3.7x more**. Any argument built on the 3 h figure --
including the Kingman variance calibration -- is withdrawn: it was fed
delta = 0.125 against a real ~0.6, and the 3.67x prediction matching a measured
3.46x was coincidence on a wrong input.


More runs **and** longer gaps means our runs are **bunched**. We build a day's
quantity in 3 hours then go silent for 19; the plant trickles it over 11. Under
`I = λ·W` that is the whole of our 2.3× inventory and 41.7 h aging.

**The one remaining structural change is shift-level build release** — spread
each GT's daily quantity across the three shifts instead of one block. Every
other open defect (PCR inventory, aging, the 0.34% shelf-life breach) is
downstream of it.

---

## 8. The plant's assignment rule — MINED, and it withdrew the v3 design

`scripts/mine_assignment.py`: a GT's top machine carries **94.2% (PCR) / 88.0%
(TBR)** of its month; machines per GT p50 **1 (PCR) / 2 (TBR)**; a (machine, GT)
run is active 10 of 13 days (**density 0.83**); ~3 GTs per machine-month.

**`b_g = q_g / c_m` is WITHDRAWN.** r(draw, machines) = 0.334, and a GT drawing
900/day gets the **same one machine** as one drawing 100/day. Machine count
barely responds to volume. Building it as v3 specified would have spread GTs
across the fleet — the opposite of the plant — and made our 41% stickiness
worse.

It is an **assignment** problem, not an allocation one. Implemented: 97.9% of
PCR GTs commit to one machine, loads balance 451–707 h in a 744 h month.

⚠️ The first version balanced on load alone and one PCR machine drew **1,365 h
in a 744 h month** (span 1,156 h, fulfilment 93%). **Capacity must be a HARD cap
that outranks the rim lock.**

---

## 10. Right-shift post-pass — SHIPPED (`planner/engine/rightshift.py`)

Push every build lot as late as its consuming press allows. Same lots, same
per-machine sequence, same machine — so fulfilment and changeover count are
unchanged **by construction**. It either removes lead time or does nothing; it
cannot trade one KPI for another. Runs **last** (P7b), after every placement
stage, because every earlier stage's output is an input to the backward pass.

Four limits per lot, backward along the machine chain, composed by `min`:

| limit | meaning |
|---|---|
| `press` | `min` over the lot's tyres of (its cure start) − τ_min |
| `succ` | start of the next lot on that machine − its setup |
| `gate` | latest end still clearing `arr < t_shift` into the cure's shift |
| `horizon` | H |

**A/B on one build of the code** (`PLANNER_RIGHTSHIFT=0` to disable — this flag
exists because comparing against an older run directory silently attributes
every intervening change to this pass, which is exactly what it did first time):

| month | cure ful% | cure chg | bld chg | hard | aging p50 | PCR inv | TBR inv |
|---|---|---|---|---|---|---|---|
| 2026-03 | 98.95→98.95 | 86→86 | 1647→1647 | 1→1 | 21.4→**17.1** | 12326→**10422** | 3195→**2426** |
| 2026-05 | 99.64→99.64 | 55→55 | 1608→1608 | 1→1 | 22.2→**16.8** | 11636→**9111** | 2891→**2280** |
| 2026-06 | 99.47→99.47 | 69→69 | 1705→1705 | 1→1 | 19.9→**17.1** | 11472→**10018** | 2960→**2419** |
| 2026-07 | 98.89→98.89 | 65→65 | 1719→1719 | 1→1 | 21.3→**17.6** | 12845→**10457** | 2931→**2415** |

Invariance holds on all four months. Inventory −12…−22%, aging p50 −2.8…−5.4 h.
Determinism preserved (byte-identical re-run) — but only after **flooring the
shift to whole seconds**: the margin term divides a Polars group-wise `std` by a
`mean`, and parallel float reduction is order-dependent in the last ULP. One lot
came out `3.9072982634368807` vs `...81`, invisible in every KPI and fatal to
byte-identity. FLOOR not round, so quantisation can never push a lot over a
limit it was derived from.

### Three bugs it found in itself

1. **Per-lot-END is the wrong press limit.** A lot's tyres feed cures
   progressively; tyre *i* only has to precede cure *i*. Using "lot ends before
   its FIRST cure" reported 75% of volume already-tight against a δ\*_lot of
   17.5 h — the criterion bound, not the chain. Every limit must be a **shift
   budget**, `min` over the lot's own tyres.
2. **FIFO re-pairing.** Shifting right changes rank within a GT, so the budget
   is computed against a pairing that no longer holds (4 lots, 0.06 h short).
   Repair loop re-derives the pairing and shrinks offenders — **capped at the
   lot's own shift**, since uncapped it pushes zero-shift lots left of where
   they started and collides with the predecessor (5 overlaps, sequence broken).
3. **`already_tight` hid the attribution.** A lot whose successor starts the
   instant it ends is pinned by `succ`, not by being JIT. Bucketing zero-shift
   lots separately mislabelled 45% of PCR volume as having no lever. Always tag
   the argmin, shift or no shift.

### What it measured — and it reverses the pacemaker

`succ` binds **84.7% of PCR volume / 78.9% of TBR**; `press` binds 0.1%/0.5%.
Machine capacity is the binding constraint, not the press and not the gate.
Building runs at **84%/87% utilization** (not the 47% in CLAUDE.md — that is the
old greedy planner), idle is 12%/9% and **90% of gaps are under 1 h**. Achieved
4.26 h of a 17.48 h δ\*_lot; the 13.2 h difference **is** the capacity binding,
now measured rather than bounded.

⚠️ **The pacemaker as specified — longer campaigns — makes WIP WORSE.** δ =
block/T_c is scale-invariant: stretch blocks by *f* and drop visits by *f*, δ is
unchanged while T_c grows by *f*, so the floor T_c(1−δ)/2 grows by *f* too.
Measured: 2× campaigns costs **+5.72 h** of floor to save 2.7% of capacity.
Measured δ is **0.483 (PCR) / 0.334 (TBR)**, not the 0.135 previously assumed —
so the batching floor is already **5.72 h**, and the residual W is not batching.

### Where the remaining W actually is

Controller targets `cover_h_p50 = 12.0` h; the ledger shows W = 24.4 h. The
extra 12.4 h is **intra-day phase lead**: the controller balances build against
cure *per day*, so a day that builds D and cures D has unchanged closing stock
but carries half a day of lead inside its profile. Right-shift reclaims 4.26 h
of that 12.4 h and is capacity-blocked on the rest.

Target band (PCR 4,500–4,800) needs W ≈ 8.8 h. Floor + τ is 6.2 h, so the band
is reachable in principle — but it needs **both** a lower `I*` and the phase
lead near zero, and the phase lead needs build targets at **shift** granularity
(§7), not campaign length.

---

## 9. Open

| Problem | Ours | Plant |
|---|---:|---:|
| Shelf-life breach (the 1 hard violation) | 0.34% | 0% |
| GT aging p95 | 36.3 h (was 41.7) | 27.6 h |
| PCR inventory (trend fails) | 10,457, slope +90 (was 12,845, +156) | 4,820, +38 |
| TBR inventory (trend now passes) | 2,415, slope +8 (was 2,931, +29) | 1,297 |
| Building changeovers | 1,719 | 1,631 |

⚠️ **The curing-changeover win was measured at 95 presses, not the plant's 86.**
66 vs 153 is 2.4x only because we mounted 9 presses the plant did not run. At
the plant's own 86 it is **94 vs 153 = 1.6x**. The difference was bought with
mounted moulds, and press slack buys campaign purity at a measured **~3.1
curing changeovers per press removed**.
| Build runs per machine-day (PCR) | 4.30 | 3.13 |

⚠️ **"Build stickiness 41.8% vs plant 99.8%" was never a like-for-like
comparison** — ours is LOT-level (`full_kpi._stickiness_pct`, consecutive
*lots* on a machine), the plant's is TYRE-level (consecutive *tyres*). A
277-tyre lot counts as one "stay" for us and 276 for the plant, so the plant
figure is inflated by lot size and the gap is an artefact of the denominator.
On the same footing (July 2026):

| | runs/machine-day | tyres per run | GTs/machine-day |
|---|---:|---:|---:|
| ours PCR | 4.30 | 277 | 2.60 |
| plant PCR | 3.13 | 375 | 2.30 |
| ours TBR | 5.62 | 63 | 3.32 |
| plant TBR | 4.15 | 86 | 3.13 |

We fragment ~37% more than the plant, not 2.4×. Curing stickiness is **100.0%**
on every month and both plants (a press never changes GT within a day).

### G8 daily GT inventory — the real open defect

| plant | band | mean (Jul) | p50 | days in band | × band mid |
|---|---|---:|---:|---:|---:|
| PCR | 4,500–4,800 | 10,701 | 11,722 | **0 / 34** | 2.30× |
| TBR | 1,200–1,500 | 2,442 | 2,627 | **3 / 34** | 1.81× |

Stable across months: PCR 1.95–2.30×, TBR 1.70–1.82×. It is not a band at all
but a **plateau** — PCR ramps 6 → 11,000 by day 7, sits at ~13,000 mid-month,
drains to 6,677 only at the close. The only in-band days are ramp-up and
run-down. See §10 for why (I\* = 12 h cover + ~8 h unreclaimed phase lead).

Plus: demand is **in-sample** (month M planned from month M's own output), the
8-month KB was never built (died silently), calendar is assumed 24×7, and
objectives #14 (nervousness) and #15 (robustness) are untested.
