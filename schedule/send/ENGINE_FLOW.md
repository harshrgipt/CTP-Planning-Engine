# ENGINE FLOW v3.0 — the phase-wise plan we follow

Three sources, one flow. Nothing in the engine exists without a home here.

| Source | Gives | Authority |
|---|---|---|
| `Building Business Rules.docx` | 14 ordered steps + **R1–R12** | **Plant rulebook — highest** |
| `Corrected_Planning_Architecture_v2/v3` | Layers **L0–L13**, CMBC design | Architecture |
| [PHASE0_FINDINGS.md](PHASE0_FINDINGS.md) · [ROADMAP.md](ROADMAP.md) · [MASTER_DATA.md](MASTER_DATA.md) | 8-month measured behaviour | Empirical |

> ### The rulebook and the data agree
> The docx orders **capacity → cure quantity → build quantity** (steps 3-5).
> Eight months of MES show curing owning 57–265 h campaigns fed by 5–8 h build
> campaigns at a controlled 4.4/4.8 h buffer. **Two independent routes, same
> architecture.** The engine is curing-first because the plant is.

**v3.0 adds three layers to v2.0:** `L2.5` Campaign Intelligence (advisor),
`L4.5` Lot Sizing & Demand Consolidation, and the Decision Cost Engine at
`L1` (compile) + `L9` (evaluate).

---

## THE FLOW

```
┌──────────────────────────────────────────────────────────────────────────┐
│ L0   CONTINUOUS LEARNING — nightly, rolling 8-month window               │
│      PARAMETERS ONLY. Never policy.                                      │
│      τ*, CV(τ), campaign bands, changeover durations, yields,            │
│      MTBF/MTTR, cure cycle by mould×press, observed lot sizes            │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ L1   VALIDATION & CONSTRAINT COMPILATION            steps 13-14 · R12    │
│      Validate demand · BOM · routing · eligibility · calendars ·         │
│      capacity · inventory · mould master                                 │
│      Compile R1–R18 → declarative rule table (rules become DATA)         │
│      ┌────────────────────────────────────────────────┐                  │
│      │ DECISION COST ENGINE (compile side)            │                  │
│      │ cost table as DATA · tier-assigned · tunable   │                  │
│      └────────────────────────────────────────────────┘                  │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ L2   CAPABILITY MODEL                              steps 6-9 · R2,R8,R14 │
│      PCR dedication map (HHI 1.00) · PCR inch eligibility                │
│      TBR eligibility (HHI 0.67) · B16 TT/TL machine groups               │
│      Mould master · mould↔press compat · changeover matrix               │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ L2.5 CAMPAIGN INTELLIGENCE ENGINE        ── ADVISOR, NOT GENERATOR       │
│      A. Parameter estimation (safe)                                      │
│         campaign bands · qty distribution + CV · sister lift w/ CI ·     │
│         observed lot sizes by mould set · split/merge triggers           │
│      B. Proposal generation (constrained)                                │
│         candidate campaign shapes + confidence + provenance              │
│      ⚠ CIE PROPOSES. L3 ceiling and L6 gate DISPOSE.                     │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ L3   TRUE BOTTLENECK & THROUGHPUT CEILING (RCCP)      step 3 · R3,R10    │
│      cure_ceiling  = Σ(moulds × cavities × 1440/cycle × press_avail)     │
│      build_ceiling = Σ(machines × avail_min × rate / (1+co_frac))        │
│      MAX_FEASIBLE  = min(cure, build, prep)                              │
│      → the denominator every downstream plan is scored against           │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ L4   NET CURE REQUIREMENT                          steps 1-2 · R1,R5,R15 │
│      demand − FG stock − usable GT stock, grossed for yield              │
│      NB: netted to CURES, not builds                                     │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ L4.5 LOT SIZING & DEMAND CONSOLIDATION (R9)              ── NEW in v3    │
│      0. DROP GTs below min_demand   PCR 300 · TBR 150                    │
│      1. Aggregate to MOULD-SET level, not SKU level                      │
│      2. Consolidate across horizon until min_cure_lot met                │
│      3. Sister-SKU consolidation   PCR 5.02x · TBR 2.48x  (L0-measured)  │
│      4. Round to cavity multiples + whole cure cycles                    │
│      5. Cap at max_cure_lot = cure_rate × 72 h   (R5 upper bound)        │
│      6. Residuals → explicit policy, NEVER silent round-up or drop       │
│      ⚠ THE LOT IS THE CURE LOT. Build slices have NO minimum.            │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
        ╔═════════════════════════════════════════════════════════════╗
│ L5     ║  CURE CAMPAIGN MASTER PLAN         ⭐ THE PLAN IS BORN      ║
        ║  mould-set × press-group × [t_start, t_end]   step 4 · R4   ║
        ║  length from learned band: 40–75 h PCR · 200–330 h TBR      ║
        ║  maximise productive press-hours · minimise mould changes   ║
        ╚═════════════════════════════════════════════════════════════╝
                                    │
                          ┌─────────┴─────────┐
│ L6   BUILDING FEASIBILITY GATE              │ ── INFEASIBLE?             │
│      Can the RATE LIMITER feed this?        │    RESHAPE AT L5.          │
│      dedication · capacity · τ window       │    Never patch downstream. │
│      step 5 · R2,R6,R8,R10                  │                            │
                          └─────────┬─────────┘
                                    │ feasible
┌──────────────────────────────────────────────────────────────────────────┐
│ L7   PULL RELEASE OF BUILDING            ⭐ THE CODE FIX   step 12 · R5   │
│      release(b) = slice.t_start − τ* − build_duration(b)                 │
│      assert τ_min ≤ wait ≤ 72 h                        R17               │
│      ✗ NOT  cure_ts = max(cure_ts, supply_ts)                            │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ L8   PREP / MIXING BACK-EXPLOSION                              R16       │
│      tread · ply · belt · bead · compound by shift, own shelf clocks     │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ L9   COUPLED OPTIMISATION — search over CURE campaigns  steps 10-11 · R7 │
│      moves: move/merge/split/extend/shrink cure campaign ·               │
│             swap mould↔press · re-slot mould change into planned down ·  │
│             shift compound batch · adjust τ* in [τ_min,τ_warn] ·         │
│             relax dedication (priced + reported, never silent)           │
│      ┌────────────────────────────────────────────────┐                  │
│      │ DECISION COST ENGINE (evaluate side)           │                  │
│      │ LEXICOGRAPHIC tiers — costs apply WITHIN a tier│                  │
│      └────────────────────────────────────────────────┘                  │
│      time-box: 90 s interactive · 15 min monthly                         │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ L10  TIME DISCRETISATION & SEQUENCING                          R13       │
│      shift → hour · mould-change slotting · crew levelling               │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ L11  VALIDATION — corrected invariants (§ below)          step 13 · R12  │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ L12  EXPLAINABILITY · KPIs · GAP-TO-CEILING               step 14        │
│      achieved / MAX_FEASIBLE, gap attributed to named causes             │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
┌──────────────────────────────────────────────────────────────────────────┐
│ L13  EVENT-DRIVEN REPLAN (R11) — per-event blast radius ──► back to L0   │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## L4.5 — LOT SIZING (R9). The layer v2.0 was missing.

### The distinction that makes R9 work

R9 says: practical batch quantities, complete the campaign, avoid very small
batches. Phase 0 shows the plant honours that **at the cure campaign level**
(57 h / 265 h, 1.43 / 1.19 changeovers per resource-day) and **deliberately
violates it at the building level** (5.5–7.7 h, 2.46 / 3.51 changeovers).

So it is not one rule. It is two, with two different floors:

| Level | Floor | Enforcement |
|---|---|---|
| **Cure lot** — *the real lot* | hard minimum | must not be broken |
| **Build slice** — *a delivery, not a lot* | **none** | deliberately small: that IS the JIT feed |

> ⚠️ **THE LOT IS THE CURE LOT. Build slices have NO minimum.**
> Applying one minimum across both either fragments cure campaigns or forces
> building to run ahead — which recreates the head gap. Comment this in code:
> someone will eventually "fix" the missing build-side check.

### Minimum lot — the fixed floors

| | min lot (build) | min demand to plan at all |
|---|---:|---:|
| **PCR** | **150** | **300** |
| **TBR** | **70** | **150** |

A GT whose horizon demand is below `min_demand` is **not planned** — it is not
worth a machine setup for a handful of tyres. It is routed to the residual
policy, never silently dropped.

### Minimum lot — the derived floor (preferred, once computable)

```
min_cure_lot(SKU) = moulds_active × cavities × (campaign_min_hours × 60 / cycle_min)

campaign_min_hours ≈ mould_change_h × (1 − target_co_frac) / target_co_frac
```

The floor is not a policy number — it falls out of mould count and mount cost.
A TBR mould set locked in presses for 11 days *has* a minimum economic lot.
At 1.19 cure changeovers/resource-day TBR already runs near its economic floor.

**Status: `cavities` is still missing** (`Full_Load` 100% NULL). Until it lands,
the fixed floors above apply and the derived formula stays scaffolding.

### The upper bound — from R5, not R9

```
max_cure_lot(SKU) ≤ cure_rate(SKU) × 72 h
```

Campaign length is bounded where build rate would outrun cure rate long enough
to push the oldest tyre past the shelf life. **On PCR — dedicated 1:1 machines
at high occupancy — this bites earlier than expected. Compute per SKU; do not
assume 72 h is slack.**

### Residual policy — step 6, and it matters more than it looks

Some demand genuinely cannot reach the floor: 200 tyres against a 4,000-tyre
minimum lot. **Do not silently round up** (that is how you build 3,800 tyres of
dead stock) and **do not silently drop**. Route to an explicit choice:

- consolidate into the next scheduled campaign for that mould set, even if late
- deliberately over-produce to stock, if carrying cost < changeover cost
- flag to the planner as an exception with **both costs shown**

That is a business call. It belongs in the DCE table, not in code.

### Feasibility checks at L4.5 — reject before planning

| Check | Action on failure |
|---|---|
| `demand ≥ min_demand` (300 / 150) | drop from plan → residual policy |
| `net_demand ≥ min_cure_lot` | route to residual policy, do not plan |
| `lot ≤ max_cure_lot` (72 h bound) | split into multiple campaigns |
| `lot % cavities == 0` | round to cavity multiple |
| `lot` maps to whole cure cycles | round up |
| mould set free for `campaign_min_hours` | reschedule or reassign |
| `P_g ≤ active_moulds(g)` | cap concurrent presses |

---

## PLANT STEP → LAYER → RULE

| # | Plant's stated step | Layer | Rule |
|---|---|---|---|
| 1 | Net SKU demand after finished-goods inventory | L4 | R1 |
| 2 | Usable GT inventory and its age | L4 | R1, R5 |
| 3 | Active moulds and curing-press capacity | L2, L3 | R3, R10 |
| 4 | **Tyres to be cured, by SKU and shift** | **L5** | R4 |
| 5 | **GT quantity to be built** | **L6** | R4 |
| 6 | SKU→machine eligibility matrix | L2, L6 | **R2** |
| 7 | Separate TT and TL machine groups | L2, L6 | **R8 / B16** |
| 8 | Prefer machine already running same SKU | **L7** | R6 |
| 9 | Prefer same inch / size family | **L7 (build) · L5 (cure)** | R6 |
| 10 | Evaluate outgoing→incoming changeover code | L9 | **R7** |
| 11 | Lowest total changeover cost sequence | L9 | R7 |

> **CORRECTED 2026-08-09 — rows 8 and 9 said L6.** R6 is not implemented in
> `l6_build_gate.py` at all; L6 is a quantity gate and makes no machine choice.
> Size/SKU continuity lives in exactly two places, both as *candidate tie-breaks*
> rather than as queue ordering:
> * **`l7_pull_release.py`**, the `_rimkey` / key-2 term of the candidate-machine
>   sort (search `KEY 2: RIM CONTINUITY`). This is the dominant one — it took PCR
>   same-size from 32.3 % to 91.7 %.
> * **`l5_cure_master.py`**, the `same_rim` term of the press choice (search
>   `same_rim =`), which makes a mould change a same-size change where it can.
>
> Note also that **L9 is not run in the shipped pipeline** — `scripts/run_arm.py`
> skips it because it overwrites `cure_campaigns.parquet`, which is L5's own
> output. Rows 10 and 11 therefore describe a layer that exists but does not
> execute; the changeover cost that is actually paid is the one L7 reserves.
| 12 | Every built GT cured within 72 h | L7, L11 | **R5** |
| 13 | Validate material, machine, mould, shift capacity | L11 | R10, R12 |
| 14 | Publish plan with exceptions | L12, L13 | R11, R12 |

**Steps 4→5 are the pivot.** Cure quantity is decided *before* build quantity.
Our current engine does the reverse — that is what L7 fixes.

---

## OBJECTIVE — lexicographic tiers, costs apply WITHIN a tier

| Tier | Objective | Costs |
|---|---|---|
| 1 | Demand fulfilment | late demand **2,000** / tyre-day |
| 2 | GT age ≤ 72 h (R5) | *hard — no price* |
| 3 | Cure campaign integrity | split campaign **300** · **lot below min 2,000** · mould change `[GAP-4 actual]` |
| 4 | Building capacity (R10) | *hard gate — no price* |
| 5 | Buffer setpoint \|τ−τ*\| | press starvation **λ × idle_h × margin** · GT inventory **50×\|τ−τ*\|**, cliff below τ_min · over-produce to min lot **50 × surplus × days** · residual deferred **800**/day |
| 6 | Cure changeover | learned matrix |
| 7 | Building changeover | learned × constraint-capacity factor — **deliberately below tier 6** |
| 8 | Dedication / stickiness | PCR **10,000+** (near-hard) · TBR **200** |
| 9 | Sister grouping | **PCR 500 · TBR 200** *(scaled to 5.02× vs 2.48× — ORDER REVERSED from the v3 source doc, which used Phase 0's broken PCR figure)* |

**"Lot below minimum" at 2,000 sits deliberately above "split campaign" at 300** —
splitting a campaign is recoverable; dropping below the economic lot is not.

**Tier 7 below tier 6 is measured, not chosen.** The plant absorbs 2.46/3.51 build
changeovers per resource-day to hold cure at 1.43/1.19. The optimiser must not
reverse that trade. No accumulation of tier-9 penalties can buy a tier-1 violation.

---

## VALIDATION INVARIANTS (L11)

**Delete** — these fail a *correct* plant:
❌ daily build mix ≈ cure mix *(observed cosine 0.21)* · ❌ minimise WIP toward
zero · ❌ build changeover count as a pass/fail gate

| Invariant | Target |
|---|---|
| First-appearance rank correlation | ≥ 0.95 *(obs 0.999)* |
| Same-day cross-correlation | ≥ 0.90 *(obs 0.92 / 0.94)* |
| Median GT wait vs τ* | ±20% *(4.4 h / 4.8 h)* |
| CV(GT wait) | ≤ 0.10 / 0.15 *(obs 0.06 / 0.12)* |
| GT wait p95 / max | ≤ 28 h / ≤ 72 h, **0 breaches** |
| Cure campaign length | 40–75 h PCR · 200–330 h TBR |
| Build slices per cure campaign | ≈ 7.5 / 48, ±30% |
| PCR dedication adherence | 100%, or priced + reported |
| Cure changeovers / resource-day | ≤ 1.43 / 1.19 |
| **Cure lots below min_cure_lot** | **0** |
| **Lots exceeding the 72 h bound** | **0** |
| **Residual demand flagged, not rounded** | **100%** |
| **Concurrent presses ≤ active moulds** | **0 violations** |
| Press idle from GT starvation | 0 |
| **Build slice minimum** | **NO CHECK — deliberate. Comment in code.** |

---

## BUILD SEQUENCE

| # | Deliverable | Blocked on | Status |
|---|---|---|---|
| 1 | **L3 ceiling calculator** | — | ✅ `l3_ceiling.py` |
| 2 | **L7 pull inversion** | — | ⬜ **NEXT** — ≈1,548 tyres PCR GT, two lines |
| 3 | L1 + L2 + hierarchical campaign model | GAP-2 *(now measured)* | ⬜ |
| 4 | **DCE at L1** | — | ⬜ cost table as data — before CIE |
| 5 | **L4 + L4.5 lot sizing** | cavities | ✅ L4 · ⬜ L4.5 |
| 6 | L5 cure campaign master | — | ⬜ |
| 7 | L6 feasibility gate | — | ⬜ |
| 8 | **CIE at L2.5** | — | ⬜ priors + empirical check on min lot |
| 9 | L10 + L11 | — | ⬜ |
| 10 | L9 coupled optimiser | — | ⬜ |
| 11 | L12 explainability | — | ⬜ ship early — overrides corrupt L0 |
| 12 | L8 prep explosion | — | ⬜ *(GAP-9 closed)* |
| 13 | L0 continuous + L13 event replan | — | ⬜ |

**DCE before CIE** — CIE needs an objective to be scored against.

---

## GAP STATUS — corrected against what we have measured

The v3.0 source doc lists GAP-3 and GAP-5 as P0 blockers. **Both have moved:**

| Gap | v3.0 doc says | **Actual** |
|---|---|---|
| GAP-5 true utilisation | P0 blocking | 🟢 **CLOSED** — `l3_ceiling.py`. PCR build 75.6% / cure 88.8% → **curing binds**; TBR 82.2% / 83.9% → **balanced**. This *overturned* the building-bottleneck claim. |
| GAP-3 mould master | P0 blocking | 🟡 **PARTIAL** — counts + status from `mould_inv` + `curing_item_mould_mapping` (PCR 100% / TBR 98%). **Cavities still missing** (`Full_Load` 100% NULL) → `min_cure_lot` formula not yet computable. |
| GAP-2 PCR eligibility | P0, unknown | 🟢 **MEASURED** — inch capability found in `CTP Set up…xlsx`. **56 of 93 GTs locked to 1 machine while capable on several — 433,798 tyres, 16.3% of volume. Dedication is HABIT, not physics.** |
| GAP-4 mould change | P1 | 🟢 **CLOSED** — PCR 210–430 min, TBR 361 min, crew 2–3 |
| GAP-9 prep shop | P2 | 🟢 **CLOSED** — 71 operations, 33 machines |

**Only genuinely blocking now: mould cavity count.** It gates `min_cure_lot`
(step 2 of L4.5) and the exact cure ceiling. Everything else can proceed on the
fixed floors PCR 150 / TBR 70.

---

## CALIBRATED CONSTANTS (8-month validated — do not guess)

| | PCR | TBR |
|---|---:|---:|
| τ* coupling buffer | **4.4 h** | **4.8 h** |
| τ_min / τ_warn / τ_hard | 0.25 / 28 / **72 h** | 0.25 / 28 / **72 h** |
| CV(τ) target | ≤ 0.10 | ≤ 0.15 |
| λ throughput | 516 tyres/h | 130 tyres/h |
| **min lot (build)** | **150** | **70** |
| **min demand to plan** | **300** | **150** |
| cure campaign band | 40–75 h | 200–330 h |
| build campaign band | 6–10 h | 4–7 h |
| build slices per cure campaign | ~7.5 | ~48 |
| cure changeovers/resource-day (cap) | 1.43 | 1.19 |
| build changeovers/resource-day (expected) | 2.46 | 3.51 |
| mould change | 210–430 min | 361 min |
| build machines per GT | **1 (HHI 1.00)** | 2 |
| true build / cure utilisation | 75.6% / **88.8%** | 82.2% / 83.9% |
| **MAX_FEASIBLE / week** | **97,152** *(build binds)* | **24,436** *(build binds)* |
| achieved vs ceiling | 82.6% | 82.0% |
| sister-SKU co-build lift | **5.02×** | **2.48×** |
| **B16 TT/TL split (July)** | n/a | **6 TT · 3 TL** |
