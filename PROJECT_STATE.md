# PROJECT STATE — JK Tyre CTP planning engine

**Everything done, everything known, everything open.** Written to survive a cold start.

| | |
|---|---|
| Architecture | **CMBC** — Curing-Master / Building-Constrained Pull (`Corrected_Planning_Architecture_v2.md`) |
| Rulebook | `Building Business Rules.docx` — R1–R12 + 14 ordered steps |
| Inputs | `INPUT/` — raw + derived + demand, with `MANIFEST.csv` |
| Current plan on disk | `runs/july_v4` — 96.73 % fulfilment · PCR inv 6,990 · TBR 1,876 · aging p50 10.2 h · >72 h 0.11 % · 0 hard violations |
| Companion docs | [PHASE0_FINDINGS.md](schedule/send/PHASE0_FINDINGS.md) · [ROADMAP.md](schedule/send/ROADMAP.md) · [ENGINE_FLOW.md](schedule/send/ENGINE_FLOW.md) · [MASTER_DATA.md](schedule/send/MASTER_DATA.md) · [INPUT/README.md](INPUT/README.md) |

---

## 1. THE HEADLINE

> The plant is a **curing-campaign-driven pull system** with dedicated building
> lines and a controlled **~4.5 h** coupling buffer. Curing sets the rhythm.
> **Our engine pushes; the plant pulls.** Inverting the release equation is the
> single highest-value code change outstanding.

Two independent routes agree, which is why this is settled rather than inferred:
- **The rulebook** orders capacity → cure quantity → build quantity (steps 3-5)
- **8 months of MES** shows 57–265 h cure campaigns fed by 5–8 h build campaigns

---

## 2. WHAT HAS BEEN BUILT

| # | Deliverable | Script | Status |
|---|---|---|---|
| 0 | Plant-behaviour diagnosis (10 analyses) | `phase0_diagnosis.py` | ✅ |
| 1 | True utilisation + throughput ceiling | `l3_ceiling.py` | ✅ |
| — | INPUT assembly + manifest | `build_input.py` | ✅ |
| — | GT↔SKU recipe bridge | `recipe_bridge.py` | ✅ |
| — | CTP setup workbook mining | `extract_ctp_setup.py` | ✅ |
| — | Ageing / PCR eligibility / opening GT | `build_gaps.py` | ✅ |
| 2 | **L7 pull inversion** | — | ⬜ **NEXT** |

---

## 3. MEASURED FACTS (8 months, 3.74 M tyres, per-tyre barcode join)

### Plant behaviour

| Signal | PCR | TBR |
|---|---:|---:|
| GT wait median | **4.4 h** | **4.8 h** |
| CV of that, month over month | **0.06** | 0.12 |
| Cure campaign length | 57.4 h | **265 h (11 d)** |
| Build campaign length | 7.7 h | 5.5 h |
| Cure changeovers / resource-day | 1.43 | 1.19 |
| Build changeovers / resource-day | 2.46 | 3.51 |
| Build machines per GT | **1 (HHI 1.00)** | 2 (HHI 0.67) |
| Cure presses per GT | 3 | 4 |
| Same-day cross-correlation | r = 0.92 | r = 0.94 |
| First-appearance rank ρ | +0.999 | +0.997 |
| Daily GT-mix cosine | 0.21 | 0.20 |
| Sister-SKU co-build lift | **5.02×** | **2.48×** | *(corrected by L0 — Phase 0's PCR parser failed silently)*

Cosine 0.21 with ρ 0.999 is not a contradiction — an 11-day cure campaign fed by
5.5-hour build campaigns **must** show a different daily mix and an identical
visit order. That is the pull signature.

### Capacity — the denominator for every plan

| | build util | cure util | cure ceiling | build ceiling | **MAX_FEASIBLE** | actual | achieved |
|---|---:|---:|---:|---:|---:|---:|---:|
| PCR | 75.6 % | **88.8 %** | 114,438 | **97,152** | **97,152** *(build)* | 86,593 | 89.1 % |
| TBR | 82.2 % | 83.9 % | 27,475 | **24,436** | **24,436** *(build)* | 21,762 | 89.1 % |

- **Both lines run at ~82 % of their own ceiling.** That 18 % is the entire prize.
- **TBR's two ceilings are 0.3 % apart** — balanced to within measurement error.
- **TBR has no single-machine redundancy**: lose one machine → 102.8 % required.

⚠️ **This overturned Phase 0's bottleneck claim.** Resource-day occupancy said
"building" (100.0 %/99.9 %); true time-utilisation says **PCR = curing**,
**TBR = balanced**. §8 of PHASE0_FINDINGS is marked superseded.

### Calibrated constants — all validated across 8 months

| | PCR | TBR |
|---|---:|---:|
| τ* coupling buffer | 4.4 h | 4.8 h |
| τ_min | **0.25 h** (15 min) | 0.25 h |
| τ_hard (GT shelf life) | **72 h — hardcoded** | 72 h |
| λ throughput | 516 tyres/h | 130 tyres/h |
| cure campaign band | 40–75 h | 200–330 h |
| build campaign band | 6–10 h | 4–7 h |
| build slices per cure campaign | ~7.5 | ~48 |
| visit law `n ∝ Q^b` | b = 0.570 | b = 0.689 (CV 0.05) |
| plant lot size p50 | 363 | 86 |
| *our* lot size p50 | 168 | 60 |

**τ_min = 0.25 h is confirmed three independent ways** — measured p0.1 from MES,
`Recipemaster.Minaging = 15` (minutes), and the ageing spec's "15 minutes"
for painted 2nd-stage GT.

---

## 4. ⚠️ NEW RULE — R8 TT/TL MACHINE DEDICATION (TBR)

**Why it must be month-long dedication, not per-decision sequencing.**
A TT↔TL switch is a major setup with long downtime (R8). Paying it repeatedly
destroys capacity on a line that is already balanced to 0.3 %. So the machine
group is **fixed for the whole month** and the optimiser may not break it.

### The measured split — July 2026 TBR

| | tyres | share | GTs | build hours | machines by hours |
|---|---:|---:|---:|---:|---:|
| **TT** | 66,322 | **67.7 %** | 26 | 3,936 h | **5.57** |
| **TL** | 31,698 | **32.3 %** | 30 | 1,881 h | **2.66** |
| total | 98,020 | 100 % | 56 | 5,817 h | 8.23 |

100 % of TBR July demand carries a TT/TL tag — this is measured, not extrapolated.

### The allocation

> ### 🔴 **6 machines TT · 3 machines TL** — not 5/4
>
> **5 TT / 4 TL is infeasible.** TT needs 5.57 machines of load; on 5 machines
> that is **111 %**. TL on 4 machines would sit at 67 % — slack on the wrong side.
> **6/3** gives TT 93 % and TL 89 %, both feasible, ~0.77 machine of total headroom.

### The rule, as it enters the engine

```
1. Split TBR demand into TT and TL by SKU tag        (INPUT/derived/tt_tl.parquet)
2. n_TT = round(9 * hours_TT / hours_total)          -> recompute EVERY month
   n_TL = 9 - n_TT
3. Assign machines to the TT group and the TL group ONCE, for the whole horizon
4. HARD constraint: a TT GT may only be planned on a TT machine, and vice versa
5. No TT<->TL changeover may appear anywhere in the plan
6. If either group exceeds ~95% load, report INFEASIBLE and re-split -- never
   silently spill across the boundary
```

**Recompute the split each month.** 6/3 is July's answer, not a constant. A month
at 55 % TT would want 5/4. The *rule* is fixed; the *number* is derived.

**Group membership should prefer stable machines.** TBR machines are SAV (TBM 01-03)
and MESNAC (TBM 04-09) — keep a group within one make where possible so build
cycle times stay uniform inside the group.

---

## 5. DATA — what we hold and its rule class

**Governing principle:** a rule may be **HARD** only if its master covers ~100 %
of the planning universe *and* the plant owns it. A partially-covering master
enforced hard **deletes feasible production** — that already cost 12,172 tyres.

| Asset | Rule | Coverage | Class |
|---|---|---|---|
| `allowed_press_matrix` | R3, R4 | 100 % / 100 % | 🟩 HARD |
| `Master_Building_ChangeoverTime_{pcr,tbr}` | R7, R13 | 100 % | 🟩 HARD |
| `capacity_*`, `calendar_shifts`, `cycle_time_*` | R10 | — | 🟩 HARD |
| `press_mould_change` | R14, GAP-4 | 166 presses | 🟩 HARD |
| **`curing_item_mould_mapping` + `mould_inv`** | **R3** | **PCR 100 % / TBR 98 %** | 🟩 **HARD** |
| `semi_finished_ageing` | R16 | 62 components | 🟩 HARD |
| `tt_tl` | **R8** | 88 % / **100 %** | 🟨 SOFT-high |
| `gt_size` | R6, R7 | 88 % / 100 % | 🟨 SOFT-high |
| `pcr_inch_eligibility` | R2 | 11 machines × 9 rims | 🟨 SOFT-high |
| `allowed_machine_matrix` (mined) | R2 | 100 % | 🟨 SOFT-high — habit, not authority |
| `allowable_2026-07` (plant) | R2 | 92 % / 96 % | 🟨 SOFT-high |
| `recipe_bridge` | R1 | PCR 100 % | 🟨 PCR only |
| `tbr_machine_certified` | R1, R2 | TBR 96 % | 🟨 TBR only |
| `opening_gt/` (7 months) | R1, R5 | complete | 🟩 HARD |
| `prep_changeover` | GAP-9, L8 | 71 ops | 🟨 SOFT |

### Plant changeover cost model — binary, per machine

| | same size | different size | crew | cost |
|---|---:|---:|---:|---:|
| PCR **BJ** (3401-3405) | 28 min | 60 min | 3 | 665/manday |
| PCR **CONTI** (3406-3411) | **22 min** | **42 min** | 3 | 665/manday |
| TBR (all 9) | 10 min | 24 min | 2–3 | 665/manday |

`Master_Building_ChangeoverTime_pcr.csv` applies BJ's 28/60 to all 11 machines —
**wrong for the 6 CONTI machines.** The workbook is authoritative.

### Mould change — the constraint we never modelled

**PCR 210–430 min (p50 360) · TBR 361 min flat.** Roughly **6 hours per press**.
That dwarfs every build changeover (10–60 min) we have been optimising.

---

## 6. GAP-2 ANSWERED — dedication is HABIT, not physics

PCR machine inch capability, from `CTP Set up ...xlsx` → [PCR BUILDING]:

| machines | inch range |
|---|---|
| 1, 2 | 12–20 |
| 3, 4, 5 | 12–16 |
| 6–11 | 13–? *(truncated in source; 18 inferred from production)* |

| rim | GTs | tyres | machines capable | machines used (p50) |
|---:|---:|---:|---:|---:|
| 12 | 9 | 365,637 | 5 | **1** |
| 13 | 9 | 961,520 | 11 | 3 |
| 15 | 18 | 414,464 | 11 | **1** |
| 16 | 20 | 190,857 | 11 | **1** |
| 17 | 14 | 273,769 | 8 | **1** |
| 18 | 16 | 199,986 | 8 | **1** |

> **56 of 93 PCR GTs are locked to one machine while capable on several —
> 433,798 tyres, 16.3 % of volume.** Rim 16 is the clearest case: 20 GTs,
> capable on all 11 machines, each running on exactly 1.
>
> **This is the largest throughput lever on site**, and it is now measured.
> Model dedication as SOFT with a high penalty so the optimiser can price the
> exception and report it — never break it silently.

---

## 7. MOULD FEASIBILITY — `july_v4` has 4 real violations

R3 was never enforced. Active moulds cap concurrent presses:

| plant | GT | planned presses | active moulds | over |
|---|---|---:|---:|---:|
| PCR | GT 2167 RAN HPE | 6 | 2 | **+4** |
| PCR | GT 1482 UHL | 5 | 2 | **+3** |
| PCR | GT 1865 ROYL RENO | 3 | 2 | +1 |
| TBR | GT 5110 - 275/70R22.5 JUXE SDI | 1 | **0** | +1 |

Only 4 of 94 GTs, and the plan uses just **46 %** of available moulds overall —
narrow, but a hard physical infeasibility. Belongs in L5/L6 as a cap on `P_g`.

---

## 8. OPEN ITEMS

### From the plant

| ID | Need | Why |
|---|---|---|
| GAP-3a | **Mould cavity count** (`Full_Load` is 100 % NULL) | tyres-per-cycle for R3 |
| GAP-3b | **Mould maintenance status** (`CurrStat` 100 % ACTIVE, `UserStatTxt` 100 % null) | is every mould really active? |
| GAP-3c | Mould ↔ press physical compatibility | L5 assignment |
| — | Confirm PCR machines 6-11 upper inch limit (source truncated) | raises capable counts only |

### Ours to build

- **L7 pull inversion** — the next code change
- Enforce mould cap on `P_g`
- Enforce R8 TT/TL group dedication (§4)
- `aging_limits` dedup (126 rows for 104 SKUs)

---

## 9. DECISIONS ON RECORD

**GT shelf life = 72 h, hardcoded** (`planner/config.py: GT_SHELF_LIFE_H`), not
env-overridable, single source imported by `contract.py`, `diagnostics.py`,
`verify.py`, `ledger.py`.

> Two controlled sources disagree and are deliberately **not** applied:
> `Ageing spec-20.01.2024 rev 12 (CTE0.06-FR.02)` gives **48 h** painted /
> **24 h** unpainted 2nd-stage GT and 24 h TBR Super Single;
> `Recipemaster.MaxAging` gives **48 h** for 493 SKUs.
> R5 of the rulebook says three days, which is what we follow.
> Recorded so the divergence stays visible.

---

## 10. LEVERS PROVEN DEAD — do not re-litigate

Pacemaker · cover law · WIP cap · integral observer · EDD re-sequencing ·
√D lot allocation · shift-granularity targets · cure-side pacing ·
**visit-count consolidation** (TBR already at the plant's law, +2 %; prize only
−108 min against a 6,172 min baseline).

---

## 11. MEASUREMENT RULES — each earned by a real error

1. **Every rate expression names its resource and cardinality**
   (`tyres · press⁻¹ · day⁻¹ × P_g presses`). Missing cardinality ⇒ wrong.
   *Caught 4 pooled-resource errors, incl. a 304 % cure utilisation.*
2. **Two independent routes must reconcile before a number ships.**
   *Campaign sawtooth was 4.23× the ledger — that gap found the bug.*
3. **Never a ratio of medians as a rate.** Use totals.
   *Inflated the PCR cure ceiling 2× (211,727 vs 104,857).*
4. **Sawtooth applies to lots, not campaigns** — cure runs concurrently with
   build inside a campaign.
5. **Check integer types before subtracting.** *A `u32` underflow reported
   "35 of 38 GTs violating" when the truth was 4.*
6. **Regex word boundaries fail on underscores.** *`\bTT\b` cannot match
   `..._K_TT`; that one detail made R8 look unimplementable.*
7. **Every constant validated across all 8 months** before acceptance.
   *EOQ exponent 0.5 was rejected: b = 0.689, CI [0.591, 0.776].*
