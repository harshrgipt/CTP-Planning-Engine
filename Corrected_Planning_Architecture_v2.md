# Corrected Planning Architecture — v2.0
## Curing-Master / Building-Constrained Pull (CMBC)

**Status:** Supersedes the Phase 0–9 building-first flow.
**Basis:** Phase 0 empirical study — 8 months, 242 days, 3.74 M tyres, per-tyre barcode join, plus `Building_Business_Rules.docx` (R1–R12).
**Scope:** PCR + TBR. Parameters are line-specific throughout; do not apply one set globally.

---

## 0. Data availability status

| Asset | Status | Impact |
|---|---|---|
| `Building_Business_Rules.docx` (R1–R12) | Present | Rule baseline |
| Phase 0 analytical findings | Present (summary form) | Parameter calibration |
| Raw MES extract / barcode join | **Not in workspace** | Cannot re-derive; using reported statistics |
| PCR construction / changeover matrix | **Missing — blocking** | See Gap Register §10 |
| PCR SKU→machine *eligibility* matrix | **Missing — blocking** | See Gap Register §10 |
| Mold master (count, cavities, press compat) | **Missing — blocking** | Cannot compute cure ceiling |
| Mold-change crew roster | **Missing** | Cannot schedule mold changes |
| True time-utilisation (vs resource-day) | **Missing** | Bottleneck identity unconfirmed |

This document is written so it is executable once §10 is closed. Every parameter that depends on a
missing asset is marked `[GAP-n]`.

---

## 1. What Phase 0 proved, and what it overturns

### 1.1 Confirmed

The plant is a **pull system with a controlled 4.4 h (PCR) / 4.8 h (TBR) coupling buffer**, in which
curing sets the rhythm and building chases it.

The evidence is not ambiguous:

| Signal | PCR | TBR | Reading |
|---|---|---|---|
| Cure campaign length | 57.4 h | 265 h (11 d) | Curing is the campaign master |
| Build campaign length | 7.7 h | 5.5 h | Building is a feeder, not a campaign owner |
| Ratio (build campaigns per cure campaign) | **7.5 : 1** | **48 : 1** | Hierarchical, not flat |
| Cure changeovers / resource-day | 1.43 | 1.19 | Protected |
| Build changeovers / resource-day | 2.46 | 3.51 | Sacrificed to protect curing |
| GT wait median | 4.4 h | 4.8 h | Tight coupling |
| CV of GT wait, month over month | 0.06 | 0.12 | **Controlled, not emergent** |
| First-appearance rank correlation | +0.999 | +0.999 | Same visit order |
| Daily GT-mix cosine similarity | 0.21 | 0.21 | Different daily mix |
| Same-day cross-correlation | r = 0.92 | r = 0.94 | Coupled at month scale |

The apparent contradiction between cosine 0.21 and ρ = +0.999 is fully explained by the campaign
length ratio. An 11-day cure campaign fed by 5.5-hour build campaigns **must** show a different daily
mix and an identical visit order. This is the signature of a pull system, and it is the single most
important structural fact in the dataset.

### 1.2 Overturned — three corrections to the engine

**Correction 1 — the coupling direction is inverted in code.**

`sync.sync()` currently computes `cure_ts = max(cure_ts, supply_ts)`. That is a *forward push*:
building emits, curing consumes whenever supply allows. The plant runs the opposite. The correct
form is a *backward pull* — see §5.

**Correction 2 — the head gap is a direct consequence of Correction 1.**

Our engine holds a 7.4 h head against the plant's 4.4 h.

```
Δτ           = 7.4 h − 4.4 h        = 3.0 h
λ_PCR        = 516 tyres/hour
Excess GT    = 3.0 × 516            ≈ 1,548 tyres
```

That accounts for essentially the whole PCR inventory gap, and it is not a tuning error — it is the
structural result of letting building lead. Fixing the direction fixes the inventory. Tuning τ
without fixing the direction will not hold.

**Correction 3 — the campaign model must become hierarchical.**

The current flat campaign object cannot represent a 265 h cure campaign fed by 48 build campaigns.
Required model:

```
CureCampaign  (mold-set × press-group × [t_start, t_end])
    ├── BuildSlice 1   (machine, qty, release_at)
    ├── BuildSlice 2
    └── ... n slices, n ≈ 7.5 (PCR) / 48 (TBR)
```

`BuildSlice` is derived, never independently generated. It has no changeover objective of its own.

---

## 2. The distinction that decides the architecture

Phase 0 reports **building at 100.0 % / 99.9 % resource-day occupancy** versus **curing at 90.7 % /
97.4 %**, and concludes building is the bottleneck. Simultaneously it shows curing owns the
campaigns. Both are consistent, and the engine must honour both — but they are two different roles
and must not be conflated:

> **Curing is the RHYTHM SETTER. Building is the RATE LIMITER.**
> Sequence is decided where the freedom is (curing). Volume is capped where the constraint binds
> (building).

This is why the flow is *curing-first* but *building-gated*. The optimiser searches over cure
campaigns because that is where the combinatorial freedom lives:

| | PCR | TBR |
|---|---|---|
| Build machines per GT (HHI) | 1 (HHI 1.00) | 2 (HHI 0.67) |
| Cure presses per GT (HHI) | 3 (HHI 0.50) | 4 (HHI 0.33) |

**PCR building HHI = 1.00 means building machine assignment is not a decision variable for PCR.**
It is master data. Every "rank preferred machines" routine in the old Phase 2 is dead code for PCR.
Read the dedication map, enforce it, and flag any deviation as a data error — do not re-derive it
each run.

### 2.1 Caveat that must be resolved before trusting §2

Two health warnings on the bottleneck finding, both material:

1. **Resource-day occupancy is a weak measure.** 100.0 % resource-day occupancy means *every machine
   did something on every day*. It does not mean 100 % time utilisation. It cannot establish
   building as the capacity constraint. Compute true utilisation —
   `productive_h / available_h`, with changeover, breakdown and idle separated — before subordinating
   the plan to building. `[GAP-5]`

2. **HHI 1.00 at 100 % occupancy is a red flag, not a finding.** If PCR building is genuinely the
   constraint *and* every GT is locked to exactly one machine, the plant is throughput-capped by a
   **dedication policy**. The critical question is whether that dedication is physical (only one
   machine is capable) or habitual (planners always picked the same one). If it is habitual, breaking
   dedication is the largest single throughput lever available on this site — larger than every
   changeover optimisation combined. Resolving this requires the eligibility matrix. `[GAP-2]`

Do not let the engine assume the answer. Model dedication as a **soft rule with a very high penalty**
so that it behaves as hard in normal operation but the optimiser can price the exception and report
it: *"Relaxing dedication of GT-xxxx to TBM-14 would add N tyres/week."* That report is the business
case for closing GAP-2.

---

## 3. The corrected flow

```
┌──────────────────────────────────────────────────────────────────┐
│ L0   CONTINUOUS LEARNING  (nightly; parameters only, not policy) │
└──────────────────────────────────────────────────────────────────┘
                              │
 L1   DATA VALIDATION & CONSTRAINT COMPILATION
      → rules R1–R18 compiled to a declarative constraint table
                              │
 L2   CAPABILITY MODEL
      dedication map · mold master · mold↔press compat · changeover matrix
                              │
 L3   TRUE BOTTLENECK & THROUGHPUT CEILING   (RCCP)
      → publishes MAX_FEASIBLE per line per week. Every plan is scored against it.
                              │
 L4   DEMAND → NET CURE REQUIREMENT     (R1)
      demand − FG stock − usable GT stock, grossed up for yield
                              │
        ╔═══════════════════════════════════════════════════╗
 L5     ║  CURE CAMPAIGN MASTER PLAN   ⭐ THE PLAN IS BORN   ║
        ║  mold-set × press-group × [t_start, t_end]        ║
        ║  campaign length from learned band (§8)           ║
        ╚═══════════════════════════════════════════════════╝
                              │
                        ┌─────┴─────┐
 L6   BUILDING FEASIBILITY GATE     │  ◄── if infeasible, return to L5
      can the constraint feed this? │      and RESHAPE the cure campaign.
      dedication map + capacity     │      Never patch it downstream.
                        └─────┬─────┘
                              │ feasible
 L7   PULL RELEASE OF BUILDING          ⭐ THE CODE FIX
      release(b) = start(slice) − τ* − duration(b)
                              │
 L8   PREP / MIXING BACK-EXPLOSION
      tread · ply · belt · bead · compound, by shift
                              │
 L9   COUPLED OPTIMISATION
      search over CURE campaigns; building re-derives each time
                              │
 L10  TIME DISCRETISATION & SEQUENCING
      shift → hour · mold-change slotting · crew levelling
                              │
 L11  VALIDATION  (invariants rewritten — see §7)
                              │
 L12  EXPLAINABILITY · KPIs · GAP-TO-CEILING
                              │
 L13  EVENT-DRIVEN REPLAN  (R11)  ──────────► back to L0
```

### Layer specifications

**L0 — Continuous learning.** Runs nightly on the rolling 8-month window. Emits *parameters only*:
τ*, CV(τ), campaign-length bands, changeover durations by from→to code, yields, MTBF/MTTR, cure cycle
times by mold×press. It must **never** emit policy. Learning the planner's decisions reproduces the
plant's current throughput including its mistakes. Learn what the plant *is*, decide separately what
it *should be*.

**L1 — Validation & constraint compilation.** Old Phase 1, plus: compile R1–R18 into a declarative
table (rule id, class hard/soft, expression, weight, owner). Rules become data. The plant tunes
weights without a release.

**L2 — Capability model.** Old Phase 2, with three changes: (a) PCR machine ranking is deleted and
replaced by the dedication map; (b) mold master and mold↔press compatibility added — currently
missing entirely; (c) changeover matrix built from *learned actual durations*, distribution not mean.

**L3 — Throughput ceiling.** New, and non-negotiable. Compute the maximum feasible output per line
per week from both stages:

```
Cure ceiling  = Σ (molds_active × cavities × 1440 / cycle_min × press_avail)
Build ceiling = Σ (machines × available_min × rate / (1 + changeover_fraction))
MAX_FEASIBLE  = min(cure_ceiling, build_ceiling, prep_ceiling)
```

Ship this as a standalone report before anything else is built. It gives every subsequent plan a
denominator, and it settles §2.1 empirically.

**L4 — Net cure requirement.** R1, applied to cures rather than builds. Gross up for cure rejects and
GT scrap using learned yields; planning net guarantees under-delivery.

**L5 — Cure campaign master plan.** The plan originates here. Assign mold-sets to press-groups over
time. Campaign length is drawn from the learned band, not optimised freely — an 11-day TBR campaign
is a mold-availability fact, not a free variable. Objective at this layer: maximise productive
press-hours, minimise mold changes, respect campaign-length band.

**L6 — Building feasibility gate.** The critical loop, and the correct expression of "curing leads,
building constrains." Curing proposes; building disposes. For each proposed cure campaign, check
that the dedicated building machines can deliver the required GT rate inside the τ window. If not,
**return to L5 and reshape the cure campaign** — do not accept it and repair downstream. Downstream
repair is exactly what the old Phase 5 did and it is why the engine churns.

**L7 — Pull release.** See §5. This is the specific code change.

**L8 — Prep/mixing back-explosion.** Absent from the old flow. Tread, ply, belt, bead and compound
requirements by shift, with their own shelf-life clocks. If the calender or extruder turns out to be
the real constraint (L3 will tell you), the old flow could not have acted on its own finding.

**L9 — Coupled optimisation.** Move set operates on **cure** campaigns; building slices re-derive
automatically. Valid moves:

- move / merge / split / extend / shrink a **cure** campaign
- swap mold ↔ press
- **re-slot a mold change into a planned press-down window** ← high value, usually omitted
- shift a compound batch earlier
- adjust τ* per press group within `[τ_min, τ_warn]`
- relax a dedication (priced, reported, never silent)

Note what is *not* here: "minimise building changeovers" as a primary move. The plant deliberately
absorbs 2.46 / 3.51 build changeovers per resource-day to hold cure changeovers at 1.43 / 1.19. That
trade is correct and the optimiser must not reverse it. Building changeovers are priced only via
their consumption of constraint capacity (§9, tier 7).

**L10 — Time discretisation.** Plan in shift buckets from L5 onward; refine to hourly only inside the
frozen horizon. Optimising at campaign aggregate and discretising at the end — the old Phase 7 —
reliably produces campaigns that will not fit the shift calendar.

**L11 — Validation.** See §7. The invariants change substantially.

**L12 — Explainability & gap-to-ceiling.** Every plan reports `achieved / MAX_FEASIBLE` with the gap
attributed to named causes. Every decision carries a reason string. Planners who cannot see *why*
will override, and every override corrupts L0's parameter estimates.

**L13 — Event replan.** R11 as a trigger subscription, not a phase. Each event type declares a blast
radius (this press group / this shift / full horizon) so a single former failure does not rebuild the
month.

---

## 4. Mapping from the old flow

| Old | New | Change |
|---|---|---|
| Phase 0 Learning | L0 | One-time → nightly; policy → parameters only |
| Phase 1 Validation | L1 | + rules compiled to data |
| Phase 2 Mfg Intelligence | L2 | PCR machine ranking deleted; mold master added |
| — | **L3** | **New. Throughput ceiling.** |
| Phase 3 Campaign Planning | **L5** | Moved after curing; now cure campaigns, hierarchical |
| Phase 4 Resource Planning | Merged into L5/L6 | No longer a separate allocation pass |
| Phase 5 Integrated B–C | **L6 + L7** | Repair loop → feasibility gate + backward pull |
| Phase 6 Optimisation | L9 | Move set operates on cure, not build |
| Phase 7 Time Scheduling | L10 | Bucketed from L5, not bolted on at the end |
| Phase 8 Validation | L11 | Invariants rewritten (§7) |
| Phase 9 Explainability | L12 | + gap-to-ceiling |
| — | **L8, L13** | **New. Prep explosion; event replan.** |

---

## 5. The coupling equation — the code change

**Current (wrong — forward push):**

```python
cure_ts = max(cure_ts, supply_ts)      # building leads, curing follows
```

**Correct (backward pull):**

```python
# Cure campaign timing is fixed first, by L5.
# Building is released backwards from it.

for slice in cure_campaign.slices:
    release_start = slice.t_start - TAU_STAR[line] - build_duration(slice)
    release_start = max(release_start, earliest_material_available(slice))

    wait = slice.t_start - (release_start + build_duration(slice))
    assert TAU_MIN[line] <= wait <= TAU_HARD        # 72 h hard cap, R5
```

**Parameters (calibrated from Phase 0):**

| Parameter | PCR | TBR | Source |
|---|---|---|---|
| `TAU_STAR` (setpoint) | 4.4 h | 4.8 h | Observed median |
| `TAU_MIN` (floor) | 2.0 h | 2.0 h | Protects against starvation |
| `TAU_WARN` | 28 h | 28 h | Observed p95 |
| `TAU_HARD` | 72 h | 72 h | R5 |
| `CV_TARGET` | ≤ 0.10 | ≤ 0.15 | Observed 0.06 / 0.12 |

**τ\* is a control setpoint, not an optimisation output.** The observed CV of 0.06 / 0.12 across eight
months proves the plant is actively regulating this buffer. Implement it as a controlled variable
with a feedback loop, not as whatever the solver happens to produce.

**Do not set τ\* = 0.** A zero-buffer target starves presses, and press-idle at the drum is
throughput that is never recovered. The plant's own answer is ~4.5 h. Trust it.

---

## 6. Business rules — reclassified and calibrated

### Hard (infeasible if broken)

| ID | Rule | Implementation |
|---|---|---|
| R1 | Demand & inventory netting | L4, grossed up for yield |
| R2 | SKU–machine eligibility | L2/L6 — **needs GAP-2** |
| R3 | Mold-based quantity | L3/L5 — **needs GAP-3** |
| R4 | Curing capacity alignment | L5 |
| R5 | GT age ≤ 72 h, FEFO issue | L7 assertion |
| R10 | Capacity & availability | L6 |
| R12 | Plan validation gate | L11 |

**R5 note:** history shows 0.6 % (PCR) / 0.1 % (TBR) of tyres already exceed 72 h. The engine must
plan to 0 %, and report any historical breach as a data-quality or execution exception — not
normalise it into the model.

### Soft (penalty-weighted, plant-tunable)

| ID | Rule | Calibrated weight guidance |
|---|---|---|
| R6 | Same SKU / same inch continuity | **Cure-side high, build-side low.** Data shows building absorbs changeovers deliberately. |
| R7 | Minimum changeover | From learned from→to matrix — **needs GAP-1 for PCR** |
| R8 | TT/TL separation | Very high penalty, not infinite. Model as machine dedication over a *weekly* horizon, not per-decision. |
| R9 | Campaign / batch minimums | Derive from learned bands (§8), not fixed constants |

### New rules required

| ID | Rule | Why |
|---|---|---|
| R13 | Mold-change crew capacity per shift | Shared finite resource; universally forgotten |
| R14 | Mold↔press physical compatibility | Platen, bladder, PCI/POCI, dome |
| R15 | Yield / scrap grossing-up per stage | Planning net guarantees short delivery |
| R16 | Semi-finished shelf life | Tread tack life, ply/belt life — separate clocks from GT |
| R17 | **GT buffer floor** (τ ≥ τ_min) | Prevents press starvation. R5 only caps the maximum. |
| R18 | Frozen horizon respect | Without it, every replan churns the floor and trust collapses |

---

## 7. Validation invariants — rewritten

The old Phase 8 would fail this plant. Any check of the form *"today's build mix should resemble
today's cure mix"* fails against an observed cosine similarity of 0.21 — and it fails on a **correct**
plan. Delete it.

**Remove:**
- ❌ daily build mix ≈ daily cure mix
- ❌ "no GT ageing" / minimise WIP toward zero
- ❌ build campaign changeover count as a pass/fail gate

**Enforce instead:**

| Invariant | Target | Basis |
|---|---|---|
| First-appearance rank correlation, build vs cure | ≥ 0.95 | Observed 0.999 |
| Same-day quantity cross-correlation | ≥ 0.90 | Observed 0.92 / 0.94 |
| Median GT wait vs τ* | within ±20 % | Observed 4.4 / 4.8 h |
| CV(GT wait) | ≤ 0.10 / 0.15 | Observed 0.06 / 0.12 |
| GT wait p95 | ≤ 28 h | Observed |
| GT wait max | ≤ 72 h, 0 breaches | R5 |
| Cure campaign length | within learned band (§8) | Observed |
| Build slices per cure campaign | ≈ 7.5 (PCR) / 48 (TBR) ±30 % | Derived |
| PCR dedication map adherence | 100 %, or priced + reported | HHI 1.00 |
| Cure changeovers / resource-day | ≤ 1.43 / 1.19 | Observed ceiling |
| Sister-SKU co-build lift | ≥ 3.0× TBR, ≥ 1.4× PCR | Observed 3.6× / 1.6× |
| Press idle attributable to GT starvation | 0 | Drum protection |

The sister-SKU targets are line-specific on purpose. TBR lift is 3.6× and worth engineering; PCR lift
is 1.6× and largely an artefact of the dedication map. Do not build heavy PCR sister-clustering logic
— the return is not there.

---

## 8. Calibrated parameter table

| Parameter | PCR | TBR |
|---|---|---|
| Throughput rate λ | 516 tyres/h | ≈ 128 tyres/h (derived) |
| τ* coupling buffer | 4.4 h | 4.8 h |
| τ p95 | ~28 h | ~28 h |
| Cure campaign length (target band) | 40–75 h | 200–330 h |
| Build campaign length (target band) | 6–10 h | 4–7 h |
| Build slices per cure campaign | ~7.5 | ~48 |
| Cure changeovers / resource-day (cap) | 1.43 | 1.19 |
| Build changeovers / resource-day (expected) | 2.46 | 3.51 |
| Build machines per GT | 1 (fixed) | 2 |
| Cure presses per GT | 3 | 4 |
| Cure occupancy (headroom) | 90.7 % (9.3 % free) | 97.4 % (2.6 % free) |
| WIP p5–p95 span | 1,956 | 509 |
| WIP sd | oscillates ≈ 0 | 160 |
| Campaign qty CV | 7.85 | 1.91 |
| Sister-SKU co-build lift | 1.6× | 3.6× |

**Two different control policies.** PCR oscillates around zero WIP with a wide span — pure JIT, high
variance, high campaign-qty CV (7.85). TBR carries a real standing buffer (sd 160). Parameterise per
line. A single global buffer policy will over-buffer PCR and starve TBR.

---

## 9. Objective function — lexicographic, not weighted sum

A single weighted sum will silently trade demand fulfilment for changeover savings and you will never
detect it. Use strict tiers; optimise tier *n+1* only within the ties of tier *n*.

```
1.  Demand fulfilment                        (R1, R12)
2.  GT age feasibility, 72 h hard            (R5)
3.  Cure campaign integrity                  ← do not fragment 265 h campaigns
4.  Building capacity feasibility            (the rate limiter, R10)
5.  Buffer setpoint adherence  |τ − τ*|      ← controlled variable
6.  Cure changeover cost                     (R6, R7 — cure side)
7.  Building changeover cost                 ← low weight; priced only as
                                               constraint-capacity consumption
8.  Dedication / stickiness adherence        (near-hard PCR, soft TBR)
9.  Sister-SKU grouping                      (TBR meaningful, PCR marginal)
```

Tier 7 sitting below tier 6 is the empirical finding, not a preference. Time-box the solver: 90 s for
interactive replan, 15 min for the monthly run.

---

## 10. Gap register — blocking items

| ID | Gap | Blocks | Priority |
|---|---|---|---|
| **GAP-1** | PCR construction / changeover matrix | R7 costing; avoidable-changeover analysis (Phase 0 Q11 unanswerable without it) | P1 |
| **GAP-2** | PCR SKU→machine **eligibility** matrix (as distinct from observed dedication) | §2.1 — the largest open throughput question on site | **P0** |
| **GAP-3** | Mold master: count, cavities, press compatibility, maintenance status | L3 cure ceiling; L5 cannot run | **P0** |
| **GAP-4** | Mold-change crew roster and change durations | R13; L10 crew levelling | P1 |
| **GAP-5** | True time-utilisation (productive / available, changeover and downtime separated) | Confirms or refutes the building-bottleneck finding | **P0** |
| GAP-6 | Yield / scrap by stage | R15 grossing-up | P1 |
| GAP-7 | Press availability, MTBF / MTTR | L3, L5 | P2 |
| GAP-8 | Semi-finished shelf-life limits | R16 | P2 |
| GAP-9 | Prep-shop routing and capacity | L8 | P2 |

**GAP-2, GAP-3 and GAP-5 are P0.** Without GAP-3 the cure ceiling cannot be computed and L5 has no
capacity model. Without GAP-5 the subordination logic rests on a resource-day statistic that cannot
support it. Without GAP-2 the plant may be capped by a policy nobody has ever tested.

---

## 11. Build sequence

Do not build L0 first. It is the most seductive layer and the least useful in isolation.

| Step | Deliverable | Value on its own |
|---|---|---|
| 1 | **L3 ceiling calculator** (needs GAP-3, GAP-5) | Standalone report. Settles the bottleneck question and gives every future plan a denominator. Ship this first — it earns credibility while the rest is built. |
| 2 | **L7 pull inversion** in `sync.sync()` | Removes ≈1,548 tyres of excess PCR GT immediately. Smallest change, largest measurable win. |
| 3 | L1 + L2 + hierarchical campaign model | Foundation |
| 4 | L4 + L5 — cure campaign master | Usually beats the manual plan even with naive building |
| 5 | L6 feasibility gate | Stops downstream churn |
| 6 | L10 + L11 | Deployable plan |
| 7 | L9 coupled optimiser | Time-boxed, lexicographic |
| 8 | L12 explainability | Ship early — every override corrupts L0 |
| 9 | L8 prep explosion | Once L3 confirms whether prep binds |
| 10 | L0 continuous + L13 event replan | Last |

Step 2 is a two-line change with a measurable, defensible result. Do it in the TBM6 pilot before
anything else, and report the GT inventory reduction against the 1,548-tyre prediction. If it lands,
the rest of the programme funds itself.

---

## 12. Throughput levers, ranked for this plant

1. **Resolve GAP-2.** If PCR building dedication (HHI 1.00) is habitual rather than physical, and
   building is genuinely the constraint, relaxing it is worth more than everything below combined.
2. **Reduce building changeovers *at the constraint*.** 2.46 / 3.51 per resource-day consumes
   constraint capacity directly. The plant accepts this to protect curing — correct trade, but the
   *count* can still be reduced by better slice sizing without lengthening cure campaigns.
3. **Mold–press assignment.** Longest-lived decision in the plan; largest effect on cure changeovers.
   Blocked on GAP-3.
4. **Mold-change placement, not just count.** A change during handover or a planned down is nearly
   free; the same change mid-shift costs full press-hours. Blocked on GAP-4.
5. **Eliminate press idle from GT starvation.** Hold τ ≥ τ_min (R17). Curing has 9.3 % PCR headroom —
   any starvation inside that headroom is pure loss.
6. **Campaign sizing within the learned bands.** Real, but the smallest of these once 1–5 are done.

---

## Appendix — one-line summary

> The plant is a curing-campaign-driven pull system with dedicated building lines and a controlled
> ~4.5 h coupling buffer. Our engine pushes; the plant pulls. Invert the release equation, gate cure
> campaigns on building capacity rather than repairing them afterwards, and score every plan against
> a computed throughput ceiling.
