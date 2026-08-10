# MASTER DATA — what we have, what it covers, hard or soft

**Governing principle.** A rule may be enforced **HARD** only if its master covers
~100 % of the planning universe *and* the plant owns it. A partially-covering
master enforced hard **deletes feasible production** — that already happened once
(12,172 tyres lost to a matrix that was 99.6 % stale, not violated).

Coverage below is measured against **July demand** — the real planning universe:
**PCR 48 GTs / 48 SKUs · TBR 56 GTs / 56 SKUs**.

---

## VERDICT TABLE

| File | Rule | Purpose | rows | PCR | TBR | **Class** |
|---|---|---|---:|---:|---:|---|
| `warehouse/derived/allowed_press_matrix` | R3, R4 | GT→press eligibility | 2,392 | **100 %** | **100 %** | 🟩 **HARD** |
| `masters/Master_Building_ChangeoverTime_{pcr,tbr}.csv` | R7, R13 | changeover min + crew | 11 / 9 | **100 %** | **100 %** | 🟩 **HARD** |
| `warehouse/derived/capacity_{press,machine}_day` | R10 | capacity | 172 / 20 | — | — | 🟩 **HARD** |
| `warehouse/derived/calendar_shifts` | R10 | shift calendar | 3 | — | — | 🟩 **HARD** |
| `warehouse/derived/cycle_time_{curing,building}` | R3, R10 | cycle / cadence | 172 / 20 | — | — | 🟩 **HARD** |
| `warehouse/derived/allowed_machine_matrix` *(mined)* | R2 | machine eligibility | 420 | 100 % | 100 % | 🟨 **SOFT-high** — mined, not plant-owned |
| `masters/allowable_2026-07.parquet` *(plant)* | R2 | SKU→machine eligibility | 519 | 92 % | 96 % | 🟨 **SOFT-high** — plant-owned but incomplete |
| `masters/Master_Mapping_Mould_SKU.csv` | R3, R14 | mould ↔ SKU | 2,208 | 94 % | 93 % | 🟨 **SOFT** |
| `warehouse/derived/lot_size` *(mined)* | R9 | campaign/lot bands | 191 | 100 % | 100 % | 🟨 **SOFT** — a band, never a hard qty |
| `warehouse/derived/gt_size` | R6 | rim inch / size family | 122 | **71 %** | **64 %** | 🟧 **SOFT — and blocking R7** |
| `warehouse/derived/opening_gt_inventory` | R1, R5 | opening stock + age | 90 | **65 %** | **64 %** | 🟧 **SOFT — weakens a HARD rule** |
| `warehouse/derived/sku_construction` | R7 | 15 construction components | 230 | **0 %** | 96 % | 🟥 TBR only |
| `warehouse/derived/tbr_machine_certified` | R2 | TBR certified matrix | 670 | 0 % | 96 % | 🟥 TBR only |
| `warehouse/derived/gt_sku_master` | R1 | SKU ↔ GT | 475 | 98 % | **0 %** | 🟥 PCR only |
| — *(no file)* | R6 | **sister-SKU groups** | — | mined | mined | 🟨 **SOFT by construction** |
| — *(no file)* | R8 | **TT / TL split** | — | **✗** | **✗** | 🟥 **CANNOT IMPLEMENT** |

---

## THE FIVE THINGS THIS AUDIT CHANGED

### 1. The changeover master is binary, not a from→to matrix

| | same size | different size | crew same | crew diff | cost |
|---|---:|---:|---:|---:|---:|
| PCR | **28 min** | **60 min** | 2 | 3 | 665 / manday |
| TBR | **10 min** | **24 min** | 2 | 3 | 665 / manday |

All 11 PCR and 9 TBR machines covered. **This is the plant's own cost model and it
applies to both plants.**

Consequences:
- **GAP-1 is largely dissolved.** R7 no longer needs a PCR construction matrix — the
  plant does not cost changeovers that way. Our 15-component Huber distance is a
  *refinement* of a binary model, useful for TBR sequencing, not a prerequisite.
- **R13 crew data exists** (2 vs 3 men, 665/manday) — it was listed as GAP-4/missing.
- **R7's real dependency is `size`, not construction.** See #2.

### 2. `gt_size` covers only 71 % / 64 % — and now blocks R7

R6 (same-inch) *and* R7 (same-size changeover cost) both key on size. At 71 %/64 %,
**29–36 % of the planning universe has no parsed size**, so neither rule can be hard
and R7's cost model cannot even be evaluated for a third of GTs.

**This is now the highest-value cheap fix in the data layer.** Raising size-parse
coverage toward 100 % unlocks the plant's own changeover model on both plants.

### 3. R8 (TT / TL) cannot be implemented at all

Only **15 of 1,724** July demand rows carry any TT/TUBE marker. There is no
tube-type / tubeless flag in any master we hold. R8 is a plant rule we physically
cannot evaluate.

→ **NEW GAP-10: TT/TL classification per SKU.** Add to the plant letter. Until it
lands, R8 must be *inert*, not guessed — a wrong TT/TL split causes exactly the
major setup the rule exists to prevent.

### 4. The plant's own eligibility matrix covers *less* than our mined one

`allowable_2026-07` (plant) = 92 %/96 %. `allowed_machine_matrix` (mined) = 100 %.

Enforcing the plant matrix hard would block 4–8 % of demand. Enforcing the mined one
hard would encode habit as law — and GAP-2 exists precisely because we don't know
whether PCR's HHI 1.00 dedication is physical or habitual.

**Therefore R2 is SOFT-high on both sources**: violate only with a priced, reported
exception. Never silent, never blocking. This is the rule that already cost 12,172
tyres when treated as hard.

### 5. `opening_gt_inventory` at 65 %/64 % weakens a HARD rule

R1 (netting) and R5 (age control) are hard rules resting on a master that covers
two-thirds of GTs. Missing opening stock ⇒ over-building, which is the exact failure
R1 exists to prevent. Either complete it or state the assumption explicitly per GT.

---

## RULE CLASS — final assignment

| Rule | Class | Basis |
|---|---|---|
| R1 Demand & inventory netting | **HARD** | ⚠️ but opening stock only 65 %/64 % |
| R2 SKU–machine eligibility | **SOFT-high, priced** | no source is both complete and authoritative |
| R3 Mold-based quantity | **HARD** for press capacity · **SOFT** for mould count | press matrix 100 %; mould map 94 %/93 %, no cavity count |
| R4 Curing capacity alignment | **HARD** | press capacity + cycle time complete |
| R5 GT age ≤ 72 h | **HARD** | non-negotiable; 0 breaches target |
| R6 Same SKU / same inch | **SOFT — cure high, build low** | measured trade; size only 71 %/64 % |
| R7 Minimum changeover | **HARD cost model, SOFT sequencing** | plant matrix complete; blocked on size coverage |
| R8 TT / TL separation | **INERT** | 🟥 no data — GAP-10 |
| R9 Campaign / batch minimums | **SOFT band** | learned, never a fixed constant |
| R10 Capacity & availability | **HARD** | complete |
| R11 Exception replanning | trigger | — |
| R12 Plan validation | **HARD** | gate |
| R13 Mold-change crew | **HARD** | ✅ crew + cost now available from changeover master |
| R14 Mold↔press compatibility | **SOFT** | 666/872 observed pairs, no physical master |
| R15 Yield grossing-up | **HARD** | derivable from MES status flags |
| R16 Semi-finished shelf life | **INERT** | GAP-8 |
| R17 GT buffer floor τ ≥ τ_min | **HARD** | Phase E |
| R18 Frozen horizon | **HARD** | Phase G |

---

## GAP REGISTER — updated

| ID | Need | Was | Now |
|---|---|---|---|
| GAP-1 | PCR construction/changeover matrix | P1 blocking | 🟢 **Largely dissolved** — plant uses a binary same/different-size model, held for both plants |
| GAP-2 | PCR SKU→machine **eligibility** vs dedication | P0 | 🔴 **Still P0** — decides whether HHI 1.00 is physical or habitual |
| GAP-3 | Mould cavity count, maintenance, physical compat | P0 | 🟡 partial — `mouldNo` complete, cavity count missing |
| GAP-4 | Mould-change crew | P1 | 🟢 **Partly closed** — 2/3 men, 665/manday from changeover master |
| GAP-5 | True utilisation | P0 | 🟢 **CLOSED** by L3 |
| **GAP-10** | **TT / TL classification per SKU** | — | 🔴 **NEW, P1** — R8 cannot run without it |
| **GAP-11** | **Size parse coverage 71 %/64 % → 100 %** | — | 🟠 **NEW, internal fix — highest cheap value.** Unlocks R6 and R7 on both plants. |
| GAP-12 | Opening GT stock coverage 65 %/64 % | — | 🟠 **NEW** — weakens hard rules R1/R5 |

**GAP-11 and GAP-12 are ours to fix, not the plant's.** Both are parse/coverage work
on data we already hold, and both currently degrade rules classified as HARD.
