# CMBC BUILD LOG — the v3 engine, layer by layer

Build record for `planner/cmbc/`. One section per layer: what it does, what it
found, what was wrong first. Companion to [ENGINE_FLOW.md](ENGINE_FLOW.md) (the
design) and [PROJECT_STATE.md](../../PROJECT_STATE.md) (the findings).

| | |
|---|---|
| Architecture | CMBC v3.0 — Curing-Master / Building-Constrained Pull |
| Module | `planner/cmbc/` — new, parallel to the old `planner/` engine |
| Status | **L0 · L1 · L2 · L2.5 · L3 · L4 · L4.5 built and passing** |
| Next | L5 — cure campaign master |

Run order:

```bash
python -m planner.cmbc.l0_learn --as-of 2026-08-01 --months 8
python -m planner.cmbc.l1_validate       --month 2026-07
python -m planner.cmbc.l2_capability     --month 2026-07
python -m planner.cmbc.l25_cie           --month 2026-07
python -m planner.cmbc.l3_ceiling        --month 2026-07
python -m planner.cmbc.l4_net_requirement --month 2026-07
python -m planner.cmbc.l45_lotsize       --month 2026-07
```

---

## L0 — Continuous learning · `l0_learn.py`

Parameters only, never policy. Reads **plant MES only** — verified zero
references to `runs/` or any plan artefact. Emits `warehouse/params/params_<as_of>.json`
plus 3 tables.

| parameter | PCR | TBR |
|---|---:|---:|
| τ\* coupling buffer | **4.32 h** (CV 0.058) | **4.81 h** (CV 0.108) |
| τ_min / τ_warn | 0.27 / 28.8 h | 0.27 / 26.8 h |
| build campaign p50 | 7.6 h | 5.4 h |
| cure campaign p50 | 58.5 h | 210.7 h |
| cure yield | 0.99712 | 0.98202 |
| build yield | **null — no signal** | **null — no signal** |
| MTBF / MTTR | 106.8 h / 13.2 h | 56.4 h / 11.7 h |
| press availability | 0.8897 | 0.8282 |
| changeover observed p50 | 13.8 min | 9.6 min |
| visit law | a −2.4715, b **0.570** (CV 0.053) | a −2.2655, b **0.689** (CV 0.054) |
| **sister lift** | **5.02×** (CI 4.98–5.06) | **2.48×** (CI 2.45–2.52) |

### Defects found and fixed

1. **Campaign runs never broke on a gap** — a press returning to the same GT
   weeks later read as one campaign, giving a cure p90 of 858 h (PCR) / 1,326 h
   (TBR), longer than the month. First fix used a 24 h gap cutoff; that was an
   assumption and the band was sensitive to it (TBR p50 moved 265 → 218 h).
   **Final fix: break on `v_curing.mouldNo` change** — the physical event is in
   the data at 100% population, so no proxy is needed. Bands returned to the
   Phase 0 range.
2. **A 6 h gap cutoff split campaigns on every breakdown** (TBR MTTR is 11.7 h)
   even though the mould stays mounted throughout. Same fix.
3. **PCR sister lift silently vanished** — PCR GT codes carry no rim
   (`GT 1402 XPC TATA`); it lives in `ALL PCR CTP SKUS.xlsx`. The row dropped
   rather than erroring. Now resolved via lookup and reports coverage.
4. **Visit-law intercept was not emitted**, forcing L2.5 to hardcode it.
   Now emitted; L2.5 hard-fails if absent.

> ### ⚠️ Correction that propagates
> **PCR sister lift is 5.02×, not 1.6×. TBR is 2.48×, not 3.6×.**
> Phase 0's rim parser failed silently on PCR. **The ordering reverses — PCR
> groups by rim more strongly than TBR.** Tier-9 weights that were "scaled to
> 3.6× vs 1.6×" are inverted, and the architecture note *"do not build heavy PCR
> sister-clustering logic"* was based on the broken figure.

---

## L1 — Validation & constraint compilation · `l1_validate.py`

Three outputs: validation report, **rule table as DATA** (20 rules), **cost
table** (16 entries, 9 tiers). Rules in a table can be retuned without a release
and carry their backing data in the same row.

**Result: 0 ERROR · 1 WARN.**

| severity | finding |
|---|---|
| WARN | 2 GTs have no mould in the master but WERE cured in MES → curable, unknown count |
| info | 17 GTs below min_demand → residual policy |
| info | B16 split computed live: TT 67.7% → 6 TT / 3 TL |

### Defects found and fixed

1. **First run reported "2 GTs uncurable" as an ERROR.** Both were cured in July
   on real moulds — the master is stale, not the demand infeasible. Enforcing it
   would have deleted 254 tyres. Same trap as the 12,172-tyre matrix incident.
2. **My reconciliation then compared incompatible ID namespaces.**
   `v_curing.mouldNo` is `'-#GC02'`, `'0'`, `'0#0'`; master `Mold_Name` is
   `'17575R16RANHPHMI01'`. All 475 observed moulds "missed" the master purely on
   string form, producing a spurious *"50 GTs stale"* warning.
   **Fix: reconcile on CURABILITY** (was this GT ever cured?), which is
   identity-independent. Count still comes from the master alone.
3. **`gt_size` was built from the wrong source** — derived from `tt_tl`
   (TBR matrix + mould mapping), so 6 PCR SKUs had no rim although every one is
   in `ALL PCR CTP SKUS.xlsx`. Mapping via **GT code** instead of SKU:
   **PCR 42/48 → 48/48, TBR 56/56.** R6/R7 no longer degrade.

---

## L2 — Capability model · `l2_capability.py`

Five tables: `cap_machine`, `cap_press`, `cap_mould`, `cap_ttl_groups`,
`cap_changeover`.

### Eligibility is a union with provenance, never an intersection

| plant | GTs | pairs | BOTH | CERTIFIED | OBSERVED | INCH | machines/GT |
|---|---:|---:|---:|---:|---:|---:|---:|
| PCR | 48 | 468 | 0 | **0** | 87 | **381** | **11** |
| TBR | 56 | 184 | 121 | 47 | 16 | 0 | 3 |

**PCR has zero certified pairs** — GAP-2 made concrete. Its whole eligibility
rests on the inch-range statement plus observation. Median 11 machines per GT
against an observed habit of 1.

| basis | penalty | meaning |
|---|---:|---|
| BOTH / CERTIFIED | 0 | matrix-backed |
| OBSERVED | 200 | ran, absent from matrix — possibly stale |
| INCH | 500 | capability-derived, never demonstrated |
| *+ dedication break* | +10,000 | tier-8, buyable and reported |

### B16 TT/TL groups

| group | machines | makes |
|---|---|---|
| **TT (6)** | TBM1, TBM2, TBM3, TBM4, TBM7, TBM8 | MESNAC + SAV |
| **TL (3)** | TBM5, TBM6, TBM9 | MESNAC |

**Defect:** a make-aligned split (TT=MESNAC, TL=SAV) looked clean but stranded
**30 GTs** with no eligible machine in their own group. Fixed by exhaustively
searching all C(9,6)=84 partitions, feasibility first, make coherence as a
tiebreak. **Eligibility and machine make are independent constraints.**

### Changeover, per machine

| | same | different | crew |
|---|---:|---:|---:|
| PCR **BJ** (3401-05) | 28 | 60 | 3 |
| PCR **CONTI** (3406-11) | **22** | **42** | 3 |
| TBR (all 9) | 10 | 24 | 2–3 |

The CSV applies BJ's 28/60 to all 11 PCR machines — **overcharging the six CONTI
machines by ~40%**. The workbook is authoritative.

---

## L2.5 — Campaign Intelligence Engine · `l25_cie.py`

**Advisor, not generator.** Every output carries confidence + provenance and may
be rejected downstream.

### Mould sets — connected components of the GT↔mould graph

| plant | sets | GTs | shared | demand in shared sets |
|---|---:|---:|---:|---:|
| PCR | 44 | 48 | 4 | **29.9%** |
| TBR | 54 | 56 | 2 | 13.0% |

Only 4 shared sets on PCR but they carry 30% of demand. **GTs sharing a mould
cannot cure concurrently on separate presses** — a constraint the old engine
never modelled.

### Proposals

| plant | GTs | campaigns | qty/camp | high conf |
|---|---:|---:|---:|---:|
| PCR | 41 | 554 | 456 | 32 |
| TBR | 46 | 825 | 88 | 42 |

Reconciles with practice: **PCR 456/campaign vs plant p50 363; TBR 88 vs 86.**
Confidence is evidence count (historical campaigns of that GT), not model fit.

---

## L3 — Throughput ceiling · `l3_ceiling.py`

| plant | cure/press | mould | build | **MAX_FEASIBLE** | binding |
|---|---:|---:|---:|---:|---|
| PCR | 114,438 | 354,204 | **97,152** | **97,152/wk** | **BUILD** |
| TBR | 27,475 | 93,671 | **24,436** | **24,436/wk** | **BUILD** |

**Building binds on both lines**, ~9% headroom. Moulds are 3.6× off binding.

Derived cavities (`Full_Load` is 100% NULL): **PCR 3.43, TBR 2.41** — *effective*,
including part-loading, not physical.

### Defects found and fixed — both caught by reconciliation

First run said **PCR 102.1% / TBR 111.4% — INFEASIBLE**. But July's demand *is*
July's actual production, so a ceiling below it is self-refuting.

1. **Availability double-counted.** Effective cavities derive from *observed*
   tyres/day, which already contains every stoppage. Multiplying by L0's
   availability removed downtime twice.
2. **Press availability applied to build machines.** `avail` comes from cure
   gaps — it describes presses. On building it pushed TBR's ceiling *below*
   actual output. Same resource-cardinality class as the earlier `P_g` and
   `build_cap` errors.

**Fix: demonstrated capacity — p90 resource-day × 7, on both stages.**
Achievable by construction; no availability haircut, because the observed day
already contains downtime.

> ⚠️ **Supersedes** the earlier `scripts/l3_ceiling.py` figures
> (104,857 cure-bound / 26,552 build-bound). Those used calendar time and a
> ratio-of-medians rate.

---

## L4 — Net cure requirement · `l4_net_requirement.py`

| plant | demand | from stock | net cure | yield | gross build | **plannable** |
|---|---:|---:|---:|---:|---:|---:|
| PCR | 398,405 | 4,820 | 393,585 | 0.9971 | 394,749 | **393,625** |
| TBR | 98,020 | 1,297 | 96,723 | 0.9820 | 98,525 | **97,971** |

**Load vs L3: PCR 91.5%, TBR 90.5%.** Opening stock covers 1.2% of demand;
0 tyres expired at epoch.

Yield now reads from **L0**, not a local query — two places deriving the same
quantity drift, and the downstream one is the one nobody checks.

### Two limitations stated, not hidden

- **FG stock is not netted.** `demand` is production-derived, so FG is already
  implicit. `fg_stock` column exists, defaults to 0, ready for a real order book.
- **Build-side yield is unmeasurable.** `QualityStatus` is `'1'` across all
  3,749,707 rows. Grossing up uses cure loss only; build yield reports `None`
  rather than a fabricated 1.0. If build scrap is ~1%, we under-build ~4,900/mo.

---

## L4.5 — Lot sizing & consolidation · `l45_lotsize.py`

| plant | GTs | lots | qty/lot | consolidated | split@72h | below floor |
|---|---:|---:|---:|---:|---:|---:|
| PCR | 41 | 549 | 438 | 5 | 0 | **0** |
| TBR | 46 | 822 | 90 | 3 | 0 | **0** |

**1,371 cure lots.** Residual policy holds 17 GTs / 1,678 tyres.

### The floor is derived, and it agrees with the policy floor

```
campaign_min_h = mould_change_h × (1−0.15)/0.15  →  34 h
min_cure_lot   = cavities × cycles in 34 h
```

| | derived | B12 fixed |
|---|---:|---:|
| PCR | **216** | 150 |
| TBR | **61** | 70 |

Two independent routes — mould-mount economics vs set policy — within ~40%.
First defensible basis R9's floor has had.

### Defects found and fixed

1. **Floor scaled with mould count** — a 43-mould GT got a floor of 9,288 tyres.
   The floor answers *"is it worth mounting a mould at all?"*, so it is
   **per mould**. Mounting more raises throughput, not the minimum worth running.
2. **Sub-floor GTs were forced into undersized lots** — a GT above `min_demand`
   but below `min_cure_lot` got one lot below the floor, the silent round-down
   step 6 forbids. Now routed to residual with quantity intact.

> ### THE LOT IS THE CURE LOT — build slices have NO minimum
> Printed in the gate output deliberately. The plant runs 58.5 h / 210.7 h cure
> campaigns while breaking R9 at the building level (7.6 h / 5.4 h, 2.46 / 3.51
> changeovers per resource-day): building absorbs changeovers so curing does not.
> A build-side minimum would reverse that trade and recreate the head gap.

---

## MEASUREMENT RULES — each earned by a real error

1. **Every rate names its resource and cardinality** — `tyres · press⁻¹ · day⁻¹ × P_g presses`.
2. **Two independent routes must reconcile before a number ships.**
3. **Never a ratio of medians as a rate.** Use totals.
4. **Sawtooth applies to lots, not campaigns.**
5. **Check integer types before subtracting** — a `u32` underflow reported 35 violations where there were 4.
6. **Regex word boundaries fail on underscores** — `\bTT\b` cannot match `..._K_TT`.
7. **Every constant validated across all 8 months.**
8. **Verify two keys share a namespace before treating a mismatch as evidence** — *(new, L1: mouldNo vs Mold_Name)*.
9. **Never apply a factor twice under two names** — *(new, L3: availability inside an observed rate)*.
10. **A ceiling below demonstrated output is self-refuting** — *(new, L3: use it as an assertion)*.

---

## OPEN

| item | blocks |
|---|---|
| Mould **cavity count** (`Full_Load` 100% NULL) | exact ceiling; cavities are derived |
| Mould maintenance status (`CurrStat` 100% ACTIVE) | availability realism |
| Mould↔press physical compatibility | L5 assignment |
| PCR machines 6-11 upper inch limit (source truncated) | can only ADD capability |
| Real order book + FG stock | forward planning (not backtest) |
| Build-stage reject rate | R15 grossing-up |
| 2 GTs cured but absent from mould master | data quality |

---

## Namespace / coverage audit — engine flow v3

Triggered by the sister-SKU question. Every asset the engine joins was checked for
**key resolution against the planned universe**, because the rim bug proved a join can
fail silently and read as a finding about the plant.

### Real defects

**1. `gt_size.parquet` was SKU-keyed — FIXED.**
368 of 445 rows carried a blank `gt_code`, so every rim lookup keyed on GT resolved
**4 of 86** plan GTs. Consumers affected: L5's press tiebreak, L10's `same_rim` column,
`scripts/rule_compliance.py`. The reported "0 of 188 same-rim, sister grouping SKIPPED"
was a **join failure in the measurement, not a gap in the plan**.
Fixed by populating `gt_code` from the demand SKU bridge → **86/86 resolve**.
True figure: **100 of 188 changes are same-rim (53%) vs a 35% chance baseline = 1.5× lift**
(TBR 66/90 = 73%, PCR 34/98 = 35%). No plan number changed — the schedule was always
doing this. B15/P1/P2/P3 moves SKIPPED → PARTIAL; compliance 83% → 86%.

**2. `press_mould_change.press` is a SAP press number, the schedule uses wcID — FIXED (data).**
0 of 165 plan presses resolved. Not corrupting: every consumer (L0, L4.5, L5, L9, L10)
takes a **plant-wide median**, so no number was wrong — the per-press detail was simply
discarded, and PCR presses span **210–430 min (2×)**. A `wc_id` column now carries the
bridge (166/166), written at source in `scripts/extract_ctp_setup.py`. L10 charges real
per-press minutes: 1,130 h → 1,132 h (the aggregate barely moves; TBR is uniform at 361
and PCR's median sits near its mix-weighted mean).

### Tried and reverted

**Per-press mould-change cost as an L5 press tiebreak.** Made it worse: PCR changes
98 → 110, mould-hours 1,130 → 1,225, fulfilment 98.4 → 98.0%, L11 14 → 13.
A cheap-to-change press attracts every GT, so work spreads over more presses and creates
more changes than the cheaper rate saves. **Press continuity dominates press rate.**
Kept as a comment in `l5_cure_master.py` so it is not retried.

### False alarms — coverage looks broken, design is correct

| asset | apparent | why it is fine |
|---|---|---|
| `tt_tl` by `gt_code` | 0/104 | every consumer joins by `sku`; real coverage 98/104 = 94% |
| `opening_gt` | 52/104 | file holds exactly 52 GTs; all 52 resolve |
| `params[changeover]` | 0/20 machines | keyed by **plant** by design; per-machine values live in L2's `CHG` table |
| `prep_requirement` | no `gt_code` | aggregated to (plant, shift, comp) by design |

### Dead assets — written, never read

`recipe_bridge.parquet` (45%), `sku_construction.parquet` (52%), and
`mould_cavity.parquet` (56%, and it mixes GT codes and SKU codes in one `sku` column).
`mould_cavity` is read only by L1's validation report; L3 derives cavities from MES.
Their coverage numbers are not plan defects — but nothing should start consuming them
without re-keying first.

### Rule this establishes

This was the **fourth** identifier-namespace failure (mould master L1, mould concurrency
L5, BOM keys L8, rim). It slipped past the existing "verify two keys share a namespace"
rule because the join *partially* worked — 4 of 86 — so it never looked empty enough to
question. Strengthened rule:

> **A coverage figure belongs next to every joined metric, not just the metric.**
> A metric derived from a join reports `hit/total` alongside its value, so a
> partial resolution is visible at the point of use rather than inferred later.
