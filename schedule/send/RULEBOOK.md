# RULEBOOK.md — the plant's operating envelope, mined from 8 months of MES

**Source:** 2025-12-01 → 2026-07-31, 3.75 M stage-2 build events, 3.71 M curing
events. Regenerate with `python -m planner.learn.plant_profile` →
`warehouse/derived/plant_profile.json`.

Every number below is **measured, not assumed**. Values are mined over the whole
8 months deliberately: a rule fitted to one month is a rule that breaks next
month. Shift day starts **07:00** (A 07-15, B 15-23, C 23-07).

Classification:

| | Meaning |
|---|---|
| **HARD** | Physically or contractually impossible to breach. Plan is invalid. |
| **SOFT** | Plant operates inside this band ≥90 % of the time. Breach = flag, not fail. |
| **TARGET** | Direction to optimise, no fixed bound. |

---

## 1. Building — measured envelope

| Metric | PCR | TBR | Rule |
|---|---:|---:|---|
| Machines | 11 | 9 | HARD |
| Distinct SKUs (8 mo) | 108 | 83 | — |
| Cycle time (s/tyre) | **p50 63** (49–78) | **p50 207** (189–220) | HARD (per machine) |
| Daily output, plant | p50 12,719 · p95 13,397 | p50 3,213 · p95 3,442 | SOFT |
| Daily output per machine | p50 1,115 · p95 1,567 | p50 359 · p95 406 | SOFT |
| **Daily output CV** | **0.116** | **0.142** | SOFT ≤0.20 |
| Lot / campaign size (units) | **p50 367** · p90 914 · p95 1,240 | **p50 82** · p90 164 · p95 211 | SOFT |
| Changeovers per machine·day | p50 2 · p95 4 · max 10 | p50 3 · p95 6 · max 8 | SOFT ≤p95 |
| Changeovers per machine·shift | p50 1 · p95 2 · max 5 | p50 1 · p95 2 · max 5 | SOFT |
| **Changeovers per plant·day** | **p50 20 · p95 26 · max 30** | **p50 29 · p95 35 · max 48** | SOFT |
| SKUs per machine·day | p50 2 · p95 4 · **max 5** | p50 3 · p95 5 · **max 7** | SOFT ≤max |
| Machines per SKU | p50 1 · p95 3 · **max 5** | p50 2 · p95 5 · **max 8** | SOFT ≤p95 |
| Unique SKUs per day | p50 22 · p95 27 | p50 25 · p95 29 | SOFT |
| **SKU stickiness** | **99.84 %** | **99.10 %** | SOFT ≥99 % |
| **Size lock** | **99.89 %** | **99.75 %** | HARD ≥99 % |

## 2. Curing — measured envelope

| Metric | PCR | TBR | Rule |
|---|---:|---:|---|
| Presses | 92 | 80 | HARD |
| Cadence (s/tyre, per press) | p50 472 (223–972) | p50 334 (78–683) | HARD |
| Daily output, plant | p50 12,636 · p95 13,291 | p50 3,138 · p95 3,396 | SOFT |
| Daily output per press | p50 152 · p95 198 · **max 224** | p50 42 · p95 50 · **max 60** | HARD ≤max |
| Campaign size (units) | **p50 1,166** · p90 6,392 | **p50 468** · p90 2,224 | TARGET ≥p50 |
| Changeovers per press·day | p50 0 · p95 0 · max 3 | p50 0 · p95 0 · max 8 | SOFT |
| **Changeovers per plant·day** | **p50 4 · p95 10 · max 21** | **p50 3 · p95 7 · max 11** | SOFT |
| **⇒ per month** | **≈ 146** | **≈ 99** | reference target |
| SKUs per press·day | p50 1 · **max 3** | p50 1 · **max 3** | SOFT ≤3 |
| Presses per SKU | p50 2 · p95 21 · max 44 | p50 4 · p95 18 · max 30 | SOFT |
| Unique SKUs per day | p50 25 · p95 29 | p50 29 · p95 33 | SOFT |
| **SKU stickiness** | **99.96 %** | **99.89 %** | SOFT ≥99 % |

## 3. Building ↔ Curing synchronisation

| Metric | PCR | TBR | Rule |
|---|---:|---:|---|
| Build→cure lag p50 | **4.32 h** | **4.81 h** | TARGET |
| p90 | 20.24 h | 20.12 h | SOFT |
| **p95** | **28.80 h** | **26.71 h** | SOFT ≤32 h |
| max observed | 2,527 h | 1,555 h | *(plant breach — see §5)* |
| Daily built vs cured | 12,381 / 12,341 | 3,101 / 3,044 | balanced to ~0.3 % |
| GT balance p50 | 4,550 | 5,157 | — |
| **GT balance p95 / max** | **8,901 / 9,501** | **13,386 / 13,965** | HARD ≤max (storage) |
| **GT shelf life** | **72 h** | **72 h** | HARD (plant-stated) |

**The plant runs near-JIT**: median green tyre is cured **4.3 h** after build, p95
under 29 h. Daily build and cure volumes match within 0.3 %, and GT stock
oscillates around ~4.5–5 k rather than accumulating.

## 3b. THE PLANT'S OPERATING FORMULAS (all measured, use these directly)

**Building**

| Quantity | Formula / value | PCR | TBR |
|---|---|---:|---:|
| machines `M` | fixed | 11 | 9 |
| cycle `c_m` | median inter-event gap | 63 s | 207 s |
| machine capacity | `86400 / c_m` | 1,371/day | 417/day |
| observed output | measured | 1,115/day | 359/day |
| **machine utilisation** | `output / capacity` | **81 %** | **86 %** |
| lot size `L` | median uninterrupted run | 367 | 82 |
| changeovers | per machine-day | 2 | 3 |
| setup `s` | plant master, size-dependent | 28/60 min | 10/24 min |

**Curing — the binding stage**

| Quantity | Formula / value | PCR | TBR |
|---|---|---:|---:|
| presses `P` | fixed | 86-92 | 78-80 |
| cadence `c_p` | `span / tyres` per press | 587 s | 2,146 s |
| press capacity | `86400 / c_p` | 147/day | 40/day |
| observed output | measured | 147/day | 40/day |
| **press occupancy** | `147 x 587 = 86,289 s` | **24.0 h/day** | 23.8 h/day |
| **=> press utilisation** | | **~100 %** | **~100 %** |
| campaign `K` | median run on one press | 1,166 (~8 d) | 468 |
| changeovers | `~P/K_days` | 4.7/day (146/mo) | 3.2/day (99/mo) |
| mould change `m_p` | CTP master | 210-430 min | 361 min |

**The single number that explains the whole gap:**

> The plant's presses are occupied **24.0 h/day — 100 %**. Ours run at 27-51 %.

Feasibility follows directly:

    span = total_press_work / (P x utilisation)
    plant : 124,248 press-h / (167 x 1.00) =  744 h   <- fits exactly
    ours  : 117,044 press-h / (167 x 0.50) = 1,402 h  <- does not

We have LESS work than the plant (117,044 vs 124,248 press-h) and the SAME
presses. The month fits with room to spare. **We lose it entirely to press idle
time**, not to capacity, not to changeovers, and not to allocation arithmetic.

A press idles whenever its GT's tyres have not arrived yet. To hold a press busy:

    build_rate(g) >= n_g x press_rate      for every GT g, continuously

so the required number of presses for a GT is the MINIMUM that clears its volume

    n_g = ceil( Q_g / (horizon_days x press_daily_rate) )

and building must then deliver GT g at exactly `n_g x press_daily_rate` per day —
no faster (WIP/aging) and no slower (press idles, span stretches).

## 4. What this tells us to build

1. **Curing changeovers are the tightest constraint: ~4–5/day/plant, ~146/month
   PCR and ~99/month TBR.** A press runs **one SKU per day** (p50 1, max 3) in
   campaigns of ~1,166 units. Any plan doing thousands is wrong — and one doing
   ~18 is *also* wrong, just in the other direction (campaigns far longer than
   the plant would ever run).
2. **Building changeovers are far looser**: 20/day PCR, 29/day TBR — because a
   building changeover is 22–60 min against a 3.5–7 h mould change.
3. **Output is deliberately flat** (CV 0.116/0.142). Constant daily quantity is
   an operating principle, not an accident.
4. **Sister-SKU/size grouping is near-absolute**: size lock 99.8 %+, stickiness
   99.1–99.96 %. A machine essentially never mixes sizes.
5. **Lot sizes are larger than assumed**: PCR p50 **367** (not 100–150), TBR p50
   **82** (close to the stated 50–70).
6. Building must be paced to curing, not run flat-out — that is how a 4.3 h
   median lag is achieved.

## 4b. THE BLOCKER: rules derived from last month's assignment log

**Any rule of the form "resource X may only handle what X handled last month" is
wrong as a HARD constraint.** The plant re-optimises assignments monthly:

| Constraint | Plant's Jan-2026 behaviour | Verdict |
|---|---|---|
| C1 press may only cure a GT it cured before | **43-47 %** of press-GT pairs NEW; 30-32 % of tyres | **blocker** |
| B1/B2/P6 machine may only build a GT it built | **40-45 %** of machine-GT pairs NEW; TBR 37 % of tyres | **blocker** |
| MAX_MACHINES_PER_SKU = 3 | TBR uses up to **6** (4 of 46 GTs) | too tight for TBR |
| B16 min demand PCR 300 / TBR 150 | plant builds 8 PCR + 2 TBR below it (0.5 % / 0.2 % volume) | minor |
| **max_gts_per_press = 5** | avg 1.8-2.0, **max 5**, zero exceed | **correct** |

Last month's assignment log is a **preference** (the plant is 99.8 % sticky, so
it is a strong one), never a feasibility set.

### `masters/allowed_machine_matrix` was never created or read

`data/masters.py` defines the schema and nothing loads it. `plan/building.py`
substituted `_gt_machine_map` (built-it-last-month) and `plan/curing.py` did the
same for presses. Coverage of what the plant actually did in Jan:

| | Plant used | "last month" rule | size-widened (`learn/allowed_matrix.py`) |
|---|---:|---:|---:|
| machine-GT | 144 pairs | 82 = **57 %** | 89 = **62 %** |
| press-GT | 324 pairs | 152 = **47 %** | 162 = **50 %** |

Size-widening adds only a few points because **size resolves for just 66 % of
GTs**. So the derivation cannot close this gap: **the plant must supply
`allowed_machine_matrix` and an equivalent press matrix.** Until then the planner
schedules a plant with roughly half the real routing flexibility, and overloading
the eligible resources -- hence the makespan overrun -- is unavoidable.

## 5. Violations *by the plant itself* in the source data

These are real and must not be treated as targets:

| Finding | Evidence |
|---|---|
| **GT shelf life breached** | Build→cure lag max **2,527 h PCR / 1,555 h TBR** (105 / 65 days) against a 72 h limit. The p95 is fine (≈29 h), so this is a long tail of stranded tyres, not normal operation. |
| **Negative GT balance** | PCR running balance reaches **−255**, i.e. more cured than built for a period — barcode/lineage gaps, not physical. |
| **Near-zero output days** | TBR daily build min **4** units, PCR min **2,511**; TBR daily cured min **0**. Shutdown/holiday days exist and are absent from any calendar we hold. |
| **Size unresolvable for ~34 %** | Size known for only 66.2 % PCR / 67.0 % TBR of transitions, so the 99.9 % size-lock figure is measured on the resolvable two-thirds. |

Consequences for evaluation: 100 % demand fulfilment on *every* month may be
unachievable where the source month itself contains shutdown days or lineage
gaps. Those months must be reported with the reason, not silently missed.

## 6. Derived input files

| File | Content |
|---|---|
| `warehouse/derived/plant_profile.json` | everything above, machine-readable |
| `warehouse/masters/changeover_building.parquet` | per-machine same/diff-size changeover minutes (plant master) |
| `warehouse/masters/ctp_mould_change.parquet` | per-press mould-change minutes (CTP) |
| `warehouse/masters/press_xwalk.parquet` | MES `wcID` → plant asset id |
| `warehouse/masters/mould_sku.parquet` | mould → SKU → size |

Cycle times, lot sizes, allowed machines/presses and press cadence are derived
at plan time from the warehouse under the active as-of cutoff, so they stay
leak-free under walk-forward.
