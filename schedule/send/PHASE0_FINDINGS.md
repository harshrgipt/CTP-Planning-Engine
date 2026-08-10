# PHASE 0 — Plant planning-strategy diagnosis

**Reverse-engineered from 8 months of MES history before building the new engine.**
Source: `scripts/phase0_diagnosis.py` (re-runs in ~4 min, no arguments).

| | |
|---|---|
| Window | 2025-12 → 2026-07 (242 production days) |
| Tyres measured | 3,736,534 (PCR 2,985,257 · TBR 751,277) |
| Lead-time method | **per-tyre**, `v_build.productionID = v_curing.gtbarCode` — not FIFO-paired |
| Status | **COMPLETE** |

---

## VERDICT

> **The plant is curing-campaign-driven with dedicated building lines and a ~4.5 h coupling buffer.**
>
> Curing runs long campaigns on flexible presses. Building runs short campaigns on
> dedicated machines and feeds those press campaigns just-in-time.
> **The press campaign is the plan; building chases it.**

### Consequence for the engine (the reason Phase 0 existed)

The current engine plans **building first**, then `sync.sync()` pushes cures to
`max(cure_ts, supply_ts)` — building leads, curing follows.
**The plant does the opposite.** The new engine must:

1. **Set cure campaigns first**, then release building at
   `t_campaign_start − τ_min − build_duration`.
2. **Treat PCR machine assignment as fixed, not optimised** — see §3, HHI = 1.00.

This also explains the standing inventory gap: our head is 7.4 h vs the plant's
4.4 h, and at λ_PCR = 516 tyres/h that difference alone accounts for the entire
PCR inventory excess. We build ahead *because* building leads.

---

## THE TEN ANALYSES

### 1. Lead–lag — per-tyre GT waiting time

| plant | p05 | **p50** | p95 | cured before built | month-to-month CV |
|---|---:|---:|---:|---:|---:|
| PCR | 0.8 h | **4.4 h** | 28.6 h | 0.01 % | **0.06** |
| TBR | 0.7 h | **4.8 h** | 27.0 h | 0.00 % | 0.12 |

Stable to ±6 % across eight months. Well under the 8 h JIT threshold.
**→ curing-driven / JIT.**

### 2. Campaign correlation — apparent contradiction, real meaning

| plant | daily GT-mix cosine | first-appearance rank ρ | GTs |
|---|---:|---:|---:|
| PCR | **0.215** | **+0.999** | 107 |
| TBR | **0.198** | **+0.997** | 83 |

Build and cure run *completely different mixes on any given day* yet visit GTs in
*exactly the same order*. Both are true because cure campaigns are 11 days and
build campaigns are 5 hours: **tightly coupled at month scale, decoupled at day
scale.** That is the signature of a pull system with a small buffer.

### 3. Machine stickiness — building is dedicated, curing is flexible

| plant | side | GTs | resources | top-1 share | HHI | resources/GT p50 |
|---|---|---:|---:|---:|---:|---:|
| PCR | build | 108 | 11 | **100 %** | **1.00** | **1** |
| PCR | cure | 113 | 95 | 56 % | 0.50 | 3 |
| TBR | build | 83 | 9 | 79 % | 0.67 | 2 |
| TBR | cure | 100 | 80 | 42 % | 0.33 | 4 |

**PCR building HHI = 1.00 — every GT ran on exactly one machine for eight months.**
That is a hard assignment, not a preference. TBR is looser at 0.67.

### 4. Changeover pattern — curing is the steadier side

| plant | side | campaigns | per resource-day | mean campaign qty |
|---|---|---:|---:|---:|
| PCR | build | 4,895 | 2.46 | 612 |
| PCR | cure | 1,772 | **1.43** | 1,685 |
| TBR | build | 6,813 | 3.51 | 111 |
| TBR | cure | 890 | **1.19** | 844 |

**→ curing is master on both plants.**

### 5. Cross-correlation — same-day coupling, no lag

| plant | k=−1 | **k=0** | k=+1 | best |
|---|---:|---:|---:|---|
| PCR | +0.51 | **+0.92** | +0.53 | k = 0 |
| TBR | +0.41 | **+0.94** | +0.51 | k = 0 |

Near-symmetric decay either side of zero — neither stage leads by a day. They are
locked same-day, consistent with the 4.5 h buffer.

### 6. GT inventory profile

| plant | median net WIP | sd | p5–p95 span | days |
|---|---:|---:|---:|---:|
| PCR | 28 | 615 | 1,956 | 228 |
| TBR | 242 | 160 | 509 | 228 |

Levels are relative to an unknown opening stock — read the **span**, not the level.
PCR oscillates around zero: build and cure are near-perfectly matched.

> ⚠️ The script's automated verdict mis-scores PCR here. Its rule reads WIP CV as a
> push/pull signal, but PCR's median is ≈ 0 so CV = 22 is a division artefact, and it
> prints "SPLIT VERDICT". Read the span instead — PCR is the *more* tightly pulled
> of the two. Nine of ten analyses are consistent.

### 7. Campaign lifetime — the clearest single discriminator

| plant | side | qty p50 | qty p90 | **hours p50** | CV(qty) | spans > 1 day |
|---|---|---:|---:|---:|---:|---:|
| PCR | build | 367 | 914 | **7.7** | 7.85 | 7 % |
| PCR | cure | 318 | 5,302 | **57.4** | 1.71 | 58 % |
| TBR | build | 82 | 164 | **5.5** | 1.91 | 2 % |
| TBR | cure | 455 | 2,205 | **265.2** | 1.38 | 82 % |

**Cure campaigns are 7–48× longer than build campaigns.** TBR's median cure
campaign is 11 days.

### 8. Constraint analysis — ⚠️ SUPERSEDED BY L3

> **This section's conclusion is wrong and has been overturned.** Resource-day
> occupancy only says every machine did *something* every day; it cannot identify
> the capacity constraint. `scripts/l3_ceiling.py` measured true time utilisation
> and found **PCR: curing binds (88.8 % vs build 75.6 %)** and **TBR: balanced
> (83.9 % vs 82.2 %)** — the opposite ordering. See [ROADMAP.md](ROADMAP.md) §
> "Step 1 result". The table below is retained only to show what the weaker
> statistic reported.

| plant | side | resources | resource-day occupancy |
|---|---|---:|---:|
| PCR | build | 11 | **100.0 %** |
| PCR | cure | 95 | 90.7 % |
| TBR | build | 9 | **99.9 %** |
| TBR | cure | 80 | 97.4 % |

Building machines are saturated; presses have slack. Consistent with the
independently measured TBR building load of 91.4 % (5,818 h needed / 6,363 h
capacity) and **no single-machine redundancy** — losing one TBR machine requires
102.8 %, which is infeasible.

### 9. Build-to-cure dependency — neither proactive nor reactive

| plant | within 1 h | > 48 h | > 72 h |
|---|---:|---:|---:|
| PCR | 6.8 % | 1.6 % | 0.6 % |
| TBR | 9.7 % | 0.7 % | 0.1 % |

Presses rarely wait, building rarely runs far ahead. Tight coupling at ~4.5 h.

### 10. Sister-SKU adjacency

| plant | consecutive campaigns sharing a size | chance | **lift** |
|---|---:|---:|---:|
| PCR | 44.3 % | 28.4 % | 1.6× |
| TBR | 65.6 % | 18.5 % | **3.6×** |

TBR groups by size deliberately. PCR only weakly.

---

## THE 13 QUESTIONS, ANSWERED

| # | question | answer |
|---|---|---|
| 1 | Building-first, curing-first, hybrid? | **Curing-first** (campaign master), building JIT-dedicated |
| 2 | Average GT waiting time | **4.4 h** PCR · **4.8 h** TBR (p95 ≈ 28 h) |
| 3 | Ageing before curing | 1.6 % / 0.7 % over 48 h; 0.6 % / 0.1 % over 72 h |
| 4 | Which process initiates campaigns | **Curing** — 7–48× longer campaigns, half the changeover rate |
| 5 | Bottleneck | ~~Building~~ → **CORRECTED by L3: PCR = curing (88.8 % vs 75.6 %); TBR = balanced (83.9 % vs 82.2 %)**. The occupancy-based answer was an artefact. |
| 6 | Building campaign stability | Low — CV 7.85 PCR / 1.91 TBR; 93 % / 98 % finish within a day |
| 7 | Curing campaign stability | High — 58 % / 82 % span more than a day |
| 8 | Sister SKUs built together | TBR **yes** (3.6×); PCR weakly (1.6×) |
| 9 | Campaigns aligned | In **order** yes (ρ = 0.999); in **daily mix** no (cosine 0.21) |
| 10 | % synchronized | Same-day r = **0.92 / 0.94** |
| 11 | % avoidable changeovers | **Not answerable** — needs a construction matrix; PCR has none |
| 12 | WIP between stages | TBR sd 160, span 509 · PCR span 1,956, median ≈ 0 |
| 13 | Planning philosophy revealed | **Curing-campaign-driven pull, dedicated building, ~4.5 h buffer** |

---

## OPEN ITEMS CARRIED INTO PHASE 1

1. **PCR has no allowable/construction matrix.** Q11 is unanswerable and PCR setup
   cost is unmeasurable until the plant supplies one. → plant letter.
2. **Mould inventory per (GT, press)** — needed to bound press counts honestly.
3. **PM / calendar windows** — currently inferred, never confirmed.
4. **TBR runs at 91.4 % with no single-machine redundancy** — the plant should see
   this whether or not they act on it.
5. **Disruption tail: 17.8 h/month** above p90 setup — larger than every scheduling
   lever measured, but an operations finding, not a planning one.
6. `CLAUDE.md` records machine utilisation ~47 % vs 95 % actual as a known gap.
   This session measured TBR at **87–91 %**. One of the two is stale — reconcile
   before anyone plans work against the 47 % figure.

## LEVERS ALREADY PROVEN DEAD (do not re-litigate)

Pacemaker · cover law · WIP cap · integral observer · EDD re-sequencing ·
√D lot allocation · shift-granularity targets · cure-side pacing ·
**visit-count consolidation** (TBR is already at the plant's law, +2 %; prize only
−108 min against a 6,172 min baseline).

## MEASUREMENT RULES EARNED THIS SESSION

- **Rate expressions carry a unit annotation naming the resource and its
  cardinality**: `tyres · press⁻¹ · day⁻¹ × P_g presses`. Missing cardinality means
  the expression is wrong. Caught three pooled-resource errors.
- **Two independent routes must reconcile before a number ships.** The
  campaign-level sawtooth was 4.23× the measured ledger — that disagreement is what
  exposed the model error.
- **Sawtooth applies to lots, not campaigns.** `I = Σ Q_lot/2 + λ·head` reconciles to
  3 tyres at lot level; at campaign level it fails by 4.23×, because cure runs
  concurrently with build inside a campaign.
- **Any constant is checked across all 8 months** before acceptance. The visit law
  `n ∝ Q^b` passed at CV(b) = 0.05; the EOQ exponent 0.5 was **rejected**
  (b = 0.689, CI [0.591, 0.776]).

## CALIBRATED CONSTANTS (8-month validated)

| constant | PCR | TBR | validation |
|---|---:|---:|---|
| visit law exponent `b` in `n ∝ Q^b` | 0.570 | 0.689 | CV 0.05, R² 0.51–0.91 |
| visit law intercept `a` | −2.471 | −2.265 | 8 months |
| GT head (median, per-tyre) | 4.4 h | 4.8 h | CV 0.06 / 0.12 |
| λ (tyres/h) | 516 | 130 | July |
| τ_min | 0.25 h | 0.25 h | p0.1 every month |
| plant lot size p50 | 363 | 86 | July MES |
| *our* lot size p50 | 168 | 60 | `runs/july_v4` |

---

*Current shipped plan: `runs/july_v4` — 96.73 % fulfilment · PCR inventory 6,990 ·
TBR 1,876 · aging p50 10.2 h · >72 h 0.11 % · 0 hard violations · −356 min banked.*
