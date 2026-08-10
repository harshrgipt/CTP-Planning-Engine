# ROADMAP — Curing-Master / Building-Constrained Pull (CMBC)

Implements `Corrected_Planning_Architecture_v2.md`. One row per layer, updated as
each ships. **Read [PHASE0_FINDINGS.md](PHASE0_FINDINGS.md) first** — it carries the
empirical basis and the measurement rules.

| | |
|---|---|
| Architecture | CMBC v2.0 — curing sets rhythm, building gates volume |
| Current plan on disk | `runs/july_v4` (old building-first engine) |
| Build order | Architecture §11 — **not** L0 first |

---

## STATUS

| Step | Layer | Deliverable | Status |
|---|---|---|---|
| — | **Phase 0** | Plant-behaviour diagnosis | ✅ **DONE** → `scripts/phase0_diagnosis.py` |
| 1 | **L3** | True utilisation + throughput ceiling | ✅ **DONE** → `scripts/l3_ceiling.py` |
| 2 | **L7** | Pull inversion in `sync.sync()` | ⬜ **NEXT** |
| 3 | L1+L2 | Constraint table + capability model + hierarchical campaign | ⬜ |
| 4 | L4+L5 | Cure campaign master plan | ⬜ |
| 5 | L6 | Building feasibility gate | ⬜ |
| 6 | L10+L11 | Discretisation + rewritten invariants | ⬜ |
| 7 | L9 | Coupled optimiser (lexicographic) | ⬜ |
| 8 | L12 | Explainability + gap-to-ceiling | ⬜ |
| 9 | L8 | Prep/mixing back-explosion | ⬜ |
| 10 | L0+L13 | Continuous learning + event replan | ⬜ |

---

## STEP 1 RESULT — L3 ceiling (GAP-5 CLOSED)

### True time utilisation

| | build prod h | non-prod h | avail h | **build util** | **cure util** |
|---|---:|---:|---:|---:|---:|
| PCR | 48,255 | 15,896 | 63,888 | **75.6 %** | **88.8 %** |
| TBR | 42,917 | 9,567 | 52,224 | **82.2 %** | **83.9 %** |

Build productive time = `tyres × cadence`. Cure busy time = **interval union of
`[event_ts, cycleStart]` per press** — co-cured tyres do not share a timestamp, so
any per-tyre sum double-counts occupancy.

### Throughput ceiling (tyres/week)

| plant | cure ceiling | build ceiling | **MAX_FEASIBLE** | actual | achieved |
|---|---:|---:|---:|---:|---:|
| PCR | **104,857** | 114,703 | **104,857** (cure) | 86,593 | **82.6 %** |
| TBR | 26,623 | **26,552** | **26,552** (build) | 21,762 | **82.0 %** |

**Both lines run at ~82 % of their own ceiling.** That 18 % is the total prize
available to planning — every lever measured so far lives inside it.

**TBR's two ceilings are 0.3 % apart** (26,623 vs 26,552). The line is balanced to
within measurement error; there is no single bottleneck to exploit.

### ⚠️ This overturns the Phase 0 bottleneck finding

| | Phase 0 said | L3 says |
|---|---|---|
| measure | resource-day occupancy | true time utilisation |
| PCR | build 100.0 % vs cure 90.7 % → **building** | build 75.6 % vs cure 88.8 % → **CURING** |
| TBR | build 99.9 % vs cure 97.4 % → building | build 82.2 % vs cure 83.9 % → **BALANCED** |

Architecture §2.1 predicted this exact failure: *"100 % resource-day occupancy
means every machine did something every day. It cannot establish building as the
capacity constraint."* Confirmed — **the Phase 0 bottleneck claim was an artefact
of the weaker statistic.**

**Consequence for §2 of the architecture:** the framing "curing is the rhythm
setter, building is the rate limiter" is **wrong for PCR** — curing is *both*.
For TBR neither binds alone. The curing-first flow still holds (it rests on
campaign length and changeover protection, not on the bottleneck identity), but
the *justification* changes and L6's gate must be two-sided, not building-only.

---

## GAP REGISTER — live status

| ID | Gap | Priority | Status |
|---|---|---|---|
| GAP-5 | True time-utilisation | P0 | ✅ **CLOSED** by L3 |
| GAP-3 | Mold master | P0 | 🟡 **PARTIAL** — `mouldNo` 100 % populated in MES: 239 PCR / 289 TBR moulds, 628/493 (GT,mould) pairs, 872/666 (mould,press) pairs. **Still missing: cavity count, maintenance status, physical compatibility.** |
| GAP-2 | PCR SKU→machine *eligibility* (vs observed dedication) | P0 | 🔴 **OPEN — plant letter.** Largest single throughput question on site. |
| GAP-1 | PCR construction/changeover matrix | P1 | 🔴 OPEN — plant letter. Blocks R7 and Phase 0 Q11. |
| GAP-4 | Mold-change crew roster + durations | P1 | 🔴 OPEN |
| GAP-6 | Yield / scrap by stage | P1 | 🟡 Derivable from MES status flags — not yet done |
| GAP-7 | Press availability, MTBF/MTTR | P2 | 🟡 Derivable from press idle gaps — not yet done |
| GAP-8 | Semi-finished shelf life | P2 | 🔴 OPEN |
| GAP-9 | Prep-shop routing and capacity | P2 | 🔴 OPEN |

**Note on the architecture doc's §0:** it lists "Raw MES extract / barcode join —
Not in workspace". That is **incorrect for this workspace** — the MES is present
and both Phase 0 and L3 ran on it directly. GAP-5 was closable immediately and
GAP-3/6/7 are partly derivable. Only GAP-1, GAP-2, GAP-4, GAP-8, GAP-9 genuinely
need the plant.

---

## STEP 2 — L7 pull inversion (NEXT)

**The change.** `planner/plan/sync.py` currently does a forward push:

```python
cure_ts = max(cure_ts, supply_ts)          # building leads, curing follows
```

Replace with a backward pull from fixed cure campaign timing:

```python
release_start = slice.t_start - TAU_STAR[line] - build_duration(slice)
release_start = max(release_start, earliest_material_available(slice))
wait = slice.t_start - (release_start + build_duration(slice))
assert TAU_MIN[line] <= wait <= TAU_HARD    # 72 h, R5
```

**Parameters** (Phase 0 calibrated, 8 months):

| | PCR | TBR |
|---|---:|---:|
| `TAU_STAR` | 4.4 h | 4.8 h |
| `TAU_MIN` | 2.0 h | 2.0 h |
| `TAU_WARN` (p95) | 28 h | 28 h |
| `TAU_HARD` (R5) | 72 h | 72 h |
| `CV_TARGET` | ≤ 0.10 | ≤ 0.15 |

**Prediction to test:** our head is 7.4 h vs the plant's 4.4 h; at λ_PCR = 516
tyres/h that is `3.0 × 516 ≈ 1,548` tyres of excess PCR GT. Ship behind a flag,
A/B on one build against `july_v4`, and report the realised reduction against
1,548.

**Do not set τ\* = 0.** Zero buffer starves presses, and press-idle is throughput
never recovered. The plant's own answer is ~4.5 h.

---

## INVARIANTS TO REWRITE AT L11 (architecture §7)

**Delete** — these fail a *correct* plant:
- ❌ daily build mix ≈ daily cure mix (observed cosine 0.21)
- ❌ minimise WIP toward zero
- ❌ build changeover count as a pass/fail gate

**Enforce instead:** first-appearance rank ρ ≥ 0.95 · same-day cross-correlation
≥ 0.90 · median GT wait within ±20 % of τ\* · CV(wait) ≤ 0.10/0.15 · p95 ≤ 28 h ·
max ≤ 72 h with 0 breaches · cure changeovers/resource-day ≤ 1.43/1.19 · PCR
dedication 100 % or priced · sister lift ≥ 3.0× TBR / 1.4× PCR · zero press idle
from GT starvation.

---

## OBJECTIVE — lexicographic tiers (architecture §9)

1. Demand fulfilment · 2. GT age ≤ 72 h · 3. Cure campaign integrity ·
4. Building capacity feasibility · 5. `|τ − τ*|` · 6. Cure changeover cost ·
7. Building changeover cost *(low — priced only as constraint-capacity use)* ·
8. Dedication adherence · 9. Sister-SKU grouping.

Tier 7 below tier 6 is the empirical finding: the plant absorbs 2.46/3.51 build
changeovers per resource-day to hold cure at 1.43/1.19. **The optimiser must not
reverse that trade.**

---

## MEASUREMENT RULES (carried from Phase 0 — these caught real errors)

1. **Rate expressions name the resource and its cardinality**:
   `tyres · press⁻¹ · day⁻¹ × P_g presses`. Missing cardinality ⇒ wrong.
   *Caught 4 pooled-resource errors, including the 304 % cure utilisation above.*
2. **Two independent routes must reconcile before a number ships.**
   *The campaign sawtooth was 4.23× the ledger; that disagreement found the bug.*
3. **Never a ratio of medians as a rate.** Use totals.
   *Inflated the PCR cure ceiling 2× (211,727 vs 104,857).*
4. **Sawtooth applies to lots, not campaigns** — cure runs concurrently with build
   inside a campaign.
5. **Every constant checked across all 8 months** before acceptance.

---

## LEVERS PROVEN DEAD — do not re-litigate

Pacemaker · cover law · WIP cap · integral observer · EDD re-sequencing ·
√D lot allocation · shift-granularity targets · cure-side pacing ·
visit-count consolidation (TBR already at the plant's law, +2 %; prize −108 min
against a 6,172 min baseline).
