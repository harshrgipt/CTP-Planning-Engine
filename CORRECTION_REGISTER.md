# CORRECTION REGISTER — JK Tyre CTP planning engine (Aug 2026)

Consolidated from the review session. Companion to `ENGINE_LOG.md` and `MEMORY.md`.
Ordered by what ships when, not by when it was found.

Shipped state at time of writing: **PCR 6,692 / TBR 1,777 / 99.35% / 1 hard violation**,
`PLANNER_TRUE_STOCK` and `PLANNER_MAX_PRESS_PCR` both gated off.

---

## A. COMMIT 1 — ship together

Two shape-changing fixes plus two free riders. Re-run the full six-sheet review after.

### A1 · Controller accumulator (defect #9) — largest single change

**Problem:** 902 of 1,818 TBR lots (49.6%) under 60 units; 349 lots ≤ 20; lots of 1 and 2 tyres.
The lot-splitter floor cannot fix this — if the controller says "build 2 today", a lot of 2 is emitted.
The floor must move up into the controller.

**Mechanism** — per GT, hold daily target in an unreleased accumulator:

```
acc_g += u_g(d)
release when   acc_g ≥ L_min_g          (lot floor)
            or age(acc_g) > A_max_g      (freshness escape)
            or deadline_flush            (see below)
```

- `L_min_g` set **per family**, mined as p10 of observed plant run sizes. Not a flat 60.
- `A_max_g` is essential — without it a 2/day GT never releases.
- **Deadline flush** — the accumulator has no forward visibility. A GT that accumulates all
  month and releases on day 29 satisfies the lot floor and misses the month. Flush when the
  cure demand deadline approaches regardless of accumulated quantity.

**KPI:** lots <60 → <5% · runs/machine-day 5.62 → toward plant 4.15 · setup% falls with it.
**Trade-off:** low-volume GTs carry up to 30 days cover in one lot; tail inventory rises slightly.
Accept — the alternative is a 2-tyre lot no supervisor will run.

### A2 · Union eligibility with tiered cost (defect #6)

**Not** a hard gate. Eligibility is a cost, ranked over admissible options.

| tier | source | pairs | penalty | rate cap |
|---|---|---:|---|---|
| `BOTH` | matrix ∧ material MES | 156 | 0 | — |
| `OBSERVED` | material MES only | 16 | small | 1.5 × max monthly MES |
| `TRIAL` | sub-material MES only | 19 | large | **1.0 ×** max monthly MES |
| `CERTIFIED` | matrix only, never run | 170 | large | 0.5 × peer rate |
| `neither` | nothing | — | **hard block** | — |

```
cost(g,m) = setup + run + π_tier
π_BOTH = 0  <  π_OBS  <<  π_CERT ≈ π_TRIAL
```

**Materiality gate for OBSERVED:** `cum_tyres ≥ 100 ∧ distinct_months ≥ 2 ∧ last_produced ≤ 6 months`.

**Rules attached:**
- Decay **demotes to `CERTIFIED` penalty, never blocks.** Under observed-primary a 6-month gap
  would delete a GT's only route.
- `NEW` tag for NPI SKUs — no MES history by definition. Cap at ramp rate (~50% of peer).
  Must never fall through the "no proven route" branch silently.
- Source is **plant MES actuals only**, from a dated frozen snapshot. Never our own plan output,
  or the engine ratifies its own misplacements next run.
- Certified is a **tiebreak only**, never a hard preference.
- `under_certified[]` split three ways: `NO_ROUTE` (escalate to plant) ·
  `CAPACITY_SHORT` (certify a second machine) · `UNPROVEN_ONLY` (confirm before execution).

**Do not tune `π_CERT` to a target band.** The 5–15% figure was predicted under the assumption
the routing graph collapsed, which the census disproved (union p50 = 3, BOTH covers 88% of volume).
Set `π_CERT` by principle — fires only when proven routes are saturated — then **report** the outcome.
2% is a finding, not a miss.

### A3 · R-metric fix (defect #10) — diagnostic only, no plan change

Fires on 56/56 TBR GTs while TBR fulfils 98.89%. Both cannot be true. Denominator is wrong:
press draw is computed over **nominal mounted hours**, building over intermittent visits.

```
R_g = Σ_m (h_active[m,g] · rate[m,g])  /  Σ_p (h_mounted[p,g] · cure_rate[p,g])
```

Both sides over **scheduled** hours. R < 1 then means *this press is mounted longer than building
can feed it* — fix by shortening the mount or adding a visit, not by adding capacity.
Expect 5–15 GTs, not 56. If it still fires on ~all, the metric is still wrong.

### A4 · Horizon overhang (defect #3) — one parameter

Building falls to 34% of capacity on 31/07 because no demand exists after 31/07 **in the model**.
The real plant builds early-August green tyres on the 30th.

**Plan `H + 7` days using month M+1 opening demand, truncate output at H.**

Recovers most of the 34%/47%/72% tail with no controller change. Does not fix underlying B1
run-ahead. Needs M+1 demand at plan time — confirm the order book supports 7-day lookahead.

---

## B. COMMIT 2 — setup cost, then family sequencing

Ship together. The cost function is useless unsequenced; the sequencer is blind uncosted.

### B1 · Learn the changeover cost (defect #8 foundation)

The 15-component construction vector converts changeover from a **count** into a **distance**.
This is why `TBMTBR2` alternating JDE ↔ JUH3+ at 12-of-15 components looked identical to a
1-component swap: the engine had no way to see the difference.

```
t_setup(i,j) = w_0 + Σ_k w_k · δ_k(i,j) + ε        k = 1..15
```

Fit by **non-negative least squares** — no component change can physically shorten a setup, and
NNLS damps the multicollinearity from components that always co-change (tread/belt).
894 TBR changeovers against 15 parameters is comfortable identification.

Gate: R² sensible, all weights ≥ 0.

### B2 · Family sequencing (defects #4, #5, #7, #8)

Cluster SKUs so only **low-weight** components differ within a family.
Major setup = between families. Minor setup = within.

| level | decision | class |
|---|---|---|
| 1 | GT → machine (tiered cost, mould, capacity) | assignment |
| 2 | group GTs into families per machine | pattern mining |
| 3 | sequence **families** across machine-month (~3–5 nodes) | ATSP, exact |
| 4 | sequence SKUs within family | ATSP on minor setups |
| 5 | size lots | rule-based |

This is the Economic Lot Scheduling Problem with major/minor setups — the standard formulation
for a machine running several families where between-family cost dominates.

**Current state:** mean 7.2 of 15 components change per changeover; 54.3% are major (≥8).
**Target:** major changeovers 485 → <100.

**Lot size cap — the tyre-specific constraint:**

```
Q*_g = min(  sqrt(2·D_g·S_g / h),                        ← economic
             (shelf_life − head) · λ_cure_g  )           ← physical
```

**Green tyre shelf life is the hard stop on campaign length.** This is why tyre plants cannot use
classic EOQ, and why defect #7 (uncompressed campaigns) and the aging KPI are the same constraint
seen from two ends. **Print which term binds for every GT** — it tells a planner whether a campaign
is short for economic or shelf-life reasons.

**Trade-off:** longer campaigns raise the sawtooth Q/2 and green tyre age. TBR is at 1,777 against
plant 1,743 — essentially no headroom. The cap is what makes this safe.

---

## C. INTEGRITY FIXES — before trusting any generated plan

Not optimizations. The difference between a plan you can review and one that lies quietly.

| # | Fix | Why |
|---|---|---|
| C1 | **`build_cap` bug** — returns 243 for a GT that built 55,550 | Units or aggregation error (per-machine vs fleet, per-hour vs per-day). Blocks the R diagnostic |
| C2 | **A4 horizon as hard precondition** — `end_i ≤ H` before placement | T₀=12 hid the symptom; the placement path is unfixed. Failures → `unplaced[]` with per-machine rejection reasons |
| C3 | **Name the hard violation** — shelf-life breach as GT/lot/build ts/cure ts/age | A count is not reviewable. A row is |
| C4 | **Determinism gate** — re-run one month, confirm byte-identical | Existing machinery. Use it as the gate that says a batch is trustworthy |

---

## D. BLOCKED ON PLANT DATA

| # | Request | Why it matters |
|---|---|---|
| D1 | **Mould inventory per GT/SKU** — count and serviceability | Press allocation is bounded by moulds, not by our choice. `mouldNo` is currently verification-only; promote `presses_mounted(g) ≤ M_g` to a hard P2 constraint once confirmed |
| D2 | **Plant calendar** — working days, shift pattern, PM windows per machine and press | Engine assumes 24×7. No tyre plant runs 24×7. This is defect #11 and the largest unexamined assumption in the engine |
| D3 | **Matrix authority** — 3 questions, below | Determines whether the tier penalties are right |

**Matrix letter — three questions:**

1. Your matrix forbids 9 pairs your MES shows in sustained production —
   `10.00R20_JUH3+` on TBM 2 is 45,079 tyres over 8 months, most recently 23 July.
   Which is authoritative?
2. Your matrix permits **170** (GT, machine) pairs with zero production in 8 months, and is silent
   on **35** pairs your MES shows in sustained production (11.1% of TBR volume).
   **Decompose the 170 before sending** — 25 matrix GTs weren't built at all this period
   (≈75 pairs at p50 3). Ask only about the residual: a **live** GT with a certified machine never
   touched in 8 months. Name examples: `11R22.5JDH` → TBM 3 (4,626 built elsewhere),
   `255/70R22.5JTH1` → TBM 5, `11R22.5JTHSD` → TBM 4.
   For those: **is the tooling still available and the machine still configured, or is this paper
   certification?** Live-but-unneeded and dead-and-forgotten look identical in MES and need
   opposite handling.
3. What is the matrix's revision date, and who change-controls it?

**Also worth telling them now:** their own planning system also schedules `10.00R20_JUH3+` on an
uncertified machine. That is a process-control gap independent of which engine plans.

---

## E. KEEP — do not regress

| Item | Evidence |
|---|---|
| Gate removal → continuous-time precedence (A2) | +0.42 pp, −2,018 inventory. Biggest single win |
| Right-shift post-pass (P7b) | −2,388 inventory, invariant by construction |
| T₀ 24→12 | +0.31 pp, −635 inventory, −13 changeovers |
| τ_min = 0.25 h | Measured hard process floor, 8 months both plants |
| Float-floor determinism | Non-negotiable |
| **Cures generated from the ledger** (#2 clean) | Architectural property — zero orphan cures. Nothing may erode it |
| **Press distribution** (#12 clean) | p10 1,053 / p50 1,193 vs their 24 presses under 1,000. Family campaigns pull toward longer mounts — **treat p10 < 900 as a regression** |

---

## F. OFF — do not retry without reading why

| Item | Verdict |
|---|---|
| **Press cap** (`PLANNER_MAX_PRESS_PCR`) | **Withdrawn — delete the flag.** Press count is set by mould inventory, a physical asset. We were fitting a constraint we should have asked for. Press *allocation* remains ours |
| `PLANNER_TRUE_STOCK` | **Off.** Removes a buffer the presses currently need. Affordable only after supply-timing variance falls |
| Matrix hard enforcement | **Falsified.** Would have deleted 12,172 tyres of genuinely feasible volume on a stale master |
| Observed-primary eligibility | **Symmetric error.** Absence of production ≠ incapability. Overflow routes show zero tyres in a period where the primary route coped |
| √D lot sizing · cover law · WIP cap · integral observer · EDD re-sequence · cure-side pacing cap | All previously failed. Leave off |
| ρ gross-up · `f_book` 1.095 · measured booking margin · per-GT rate | ENGINE_LOG §6. All four were "a more correct constant". All four made the plan worse |

---

## G. DOCUMENT CORRECTIONS

| Doc | Wrong | Right |
|---|---|---|
| ENGINE_LOG §3 | "108 certified GT-machine pairs" | **108 GTs / 326 pairs** (108 × p50 3 ≈ 324) |
| ENGINE_LOG §7 | "block span 3 h vs plant 11 h" | **Stale** — predates gate fix and T₀=12. Real: PCR 16.47 h, TBR 13.37 h, vol-wtd δ 0.643 in top quartile. **We are less bursty than the plant, not 3.7× more** |
| ENGINE_LOG §2 | "beat the plant on changeovers 2.4×" | **66 vs 153 was measured at 95 presses.** At 86 it is 94 vs 153 = **1.6×**. The difference was bought with mounted moulds |
| Session working note | Kingman δ calibration | **Withdrawn** — fed δ = 0.125 against a real ~0.6. The 3.67× prediction matching 3.46× was coincidence on a wrong input |

---

## H. STILL OPEN — not scheduled, do not lose

| # | Item | Status |
|---|---|---|
| H1 | **Substitutable buffer** — press slack ↔ inventory absorb one variance; both-removed is the worst cell (96.28%) | Strongest result of the session. Explains all 15 lever rows. Press slack is now a **fixed** currency set by mould inventory, so inventory is the only one we can spend |
| H2 | **B1 pacing** — build runs 1,200–1,800/day ahead for 3 weeks, then day 31 at 44% of demand. Daily build CV 0.233 vs plant 0.031 | A4 removes the horizon artifact only. The run-ahead remains |
| H3 | **Continuous-time corridor (Fix 3′)** — two-sided cumulative bound against press draw, not day-level demand | Deferred. Upper bound kills run-ahead, lower bound kills the collapse. Needs H2 understood first |
| H4 | **B3 — `succ` pins 85% of volume**; 90% of idle gaps under 1 h | Slack is shredded, not absent. ATSP setup-minimisation (B2 above) makes it contiguous and usable |
| H5 | **G8 inventory plateau** — PCR 2.30× band mid, 0/34 days in band | Downstream of H2/H3 |
| H6 | **B5 — demand is in-sample** | Every ENGINE_LOG §2 number is *reconstruction* accuracy, not *planning* accuracy |
| H7 | **Objectives #14 nervousness, #15 robustness — untested** | #14: `Σ|plan_v2 − plan_v1| / Σ plan_v1` under rolling daily replan. #15: Monte Carlo on mined downtime distribution. Both need H6 first |
| H8 | Concentration, not average — #4 setup% mean 3.22% passes, but 42% of machine-days exceed 3.5% and TBMTBR6 hits 6.56% | Expect B2 family grouping to flatten it. Re-measure after |

---

## I. FALSIFIED THIS SESSION — recorded so they don't recur

Method note: every one was caught by **reconciling a master against reality before acting on it**.
That check is the most valuable habit in this project. Apply it to the mould inventory (D1) before
it becomes a hard P2 constraint.

| Claim | Verdict | Killed by |
|---|---|---|
| A1 and B2 are one defect, two symptoms | ❌ | Press count moves `ineligible` only; true-stock moves inventory and drives idle 4.4 → 11.9% |
| Fulfilment holds or improves at 86 presses | ❌ | 99.35 → 97.39; at 83, 95.26 |
| Curing changeovers fall with press count | ❌ **wrong sign** | 66 → 81 → 94. **≈3.1 changeovers per press removed.** Press slack buys campaign purity |
| ρ(86) = 0.985 so η ≈ 0.985 | ❌ | η ≈ 0.89. The 151/press-day rate is measured at 86 presses — it already nets out setup, idle, starvation. Booking a productive-fraction on top double-counts the same idle |
| Bursty release is the bill | ⚠️ premise wrong | Span is 16.5 h, not 3 h. Mechanism real but confined to Q1 = 3.7% of volume. Actual starvation drivers run at δ 0.87–1.0 — a **rate/assignment mismatch**, not variance |
| 12.4% of TBR volume is infeasible (#6) | ❌ | 99.6% was a stale master. Genuine violations: **2 pairs, 52 tyres, 0.05%** |
| Matrix is a document; demote to prior | ❌ | 156 BOTH pairs carry 662,200 tyres = **88% of volume**. It's a superset with a lapsed update process — permissive in the tail, silent on 35 real routes |
| Observed-only cuts the routing graph to a third | ❌ | Measured on observed-only, which was never the proposal. **Union p50 = 3** — full flexibility retained |
| OBSERVED-only = 9 pairs | ❌ | Artifact of one plan's routing. Census: **35 raw / 16 material**, 11.1% of volume |
| π_CERT should land 5–15% of volume | ❌ stale | Predicted under the collapse assumption. Expect 0–5%. **Report, don't tune to it** |

Confirmed: κ is strongly Pareto (50% of starvation from 12.5% of PCR GTs) · P* interior optimum
at 90 presses (`unfilled` non-monotone 4,484 → 3,262 → 4,740 → 5,141) — **noted but not actionable,
press count is not ours to set**.
