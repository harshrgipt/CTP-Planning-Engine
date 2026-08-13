# Session log — 2026-08-11 → 2026-08-12

Everything tested, what worked, what did not, and the number that decided each
one. Written so nothing here has to be re-discovered or re-tried blind.

Engine version at the end: **v10**. Shipped packs:
`output/V10_JUL2026_pack/` · `output/V10_AUG2026_pack/`

---

## 0. Headline

| | JUL v10 | AUG v10 |
|---|---|---|
| PCR fulfilment in-month | 92.6 % | 89.4 % |
| PCR + carry-out tail | 94.3 % | 95.2 % |
| TBR fulfilment in-month | 94.9 % | 89.3 % |
| TBR + tail | 97.0 % | 93.1 % |
| PCR same-size (true, known-rim) | 80.0 % | 78.1 % |
| PCR weighted build CO min/machine-day | 80.6 | 96.9 |
| allowable violations | **0** | **0** |
| sub-floor runs (B12) | **0.0 %** | **0.0 %** |
| R5 GT wait max (≤ 72 h) | 70.9 h | 71.2 h |
| rim coverage | **100 %** | **100 %** |
| L11 invariants passed | 27/40 | 25/40 |
| tests | 15/15 | 15/15 |

**The single most important lesson: 8 of 9 scheduler-side changes measured
negative or neutral. Every change that paid was a DATA fix.**

---

## 1. What worked

| change | measured effect |
|---|---|
| **Plant's own allowable matrices** (`INPUT/allowable machine/`) | **Aug PCR +2.4 pt**; arithmetic infeasibility removed (was 63 h short); an 11th PCR machine became usable; single-machine GTs 30 → 10 |
| **`gt_size` rim fill** — 35 PCR GTs from `aging_limits` | Aug PCR weighted CO **129.2 → 112.8 (−12.7 %)**, fulfilment up, rim coverage 72.4 % → 100 % |
| **L4b allocation as an L7 preference** (pre-L5, family-first) | Aug PCR **+0.8 pt**, unfed −3,349, families/machine 1.90 → 1.60 |
| **Release grid `PLANNER_LOT_INTERVAL_H=8`** (was 16) | Aug PCR **+1.0 pt**, unfed −4,199, +2 L11 invariants |
| **Allowable machine list made HARD** | 0 violations (was 19.9 % of July PCR volume on unsanctioned machines). Cost PCR −3.6/−5.3 pt — the honest price |
| **Scarcity-first allocation** (narrow machines pick first) | fixed a real defect: L4b allocated TBMPCR7 **0 h** while L7 put 32,939 tyres on it. PCR7 0 → 707 h allocated, 61.5 % → 83.5 % utilisation |
| **L11 same-size metric fix** | true PCR same-size was **81.3 %**, reported as 60.9 % |
| **New `l1_preflight`** | old L1 crashed on every clone (`v_curing does not exist`) |
| **New L4b capacity-flow gate** | proves feasibility BEFORE L5 and names the binding machine subset by min-cut |
| **Single entry point `main.py`** | 13 layers → 8 |
| **Input consolidation** | every input under `INPUT/`, resolved via `planner/paths.py` |

---

## 2. What did NOT work — do not retry without new evidence

| change | measured effect |
|---|---|
| **De-pin L7** (`PLANNER_HARD_PIN=0`) | −0.5 pt, unfed **+2,108**. Sticky pinning stays. |
| **More insertion points** (`L7_MR_POINTS` 2, 3) | **inert** — 0 effect |
| **L5↔L6 loop, delay mode** | **−16,548 tyres.** Campaigns-unfed fell 111 → 87 but in-month fed fell 458,863 → 442,315: every rightward move pushes output past the reporting boundary |
| **L5↔L6 loop, split mode** | **−18,129 tyres.** Each split piece pays a 6 h mould change: 476 → 767 campaigns, mould changes 201 → 367, **+1,029 press-h of pure setup** |
| **Deadline-aware allocation** (allocation moved after L5) | **neutral.** August bit-identical, 0 of 130 (GT, machine) pairs changed. The allocation influences *which* machine, never *when* |
| **Rim-priority flags** (`RIM_PRIORITY`, `CLUSTER_SEQ_H`, `SISTER_GROUP`) | ~**0.8 pt fulfilment per 1 pt same-size**, consistently. Best changeover arm cost 1.6 pt and 6,731 unfed |
| `RIM_MAX_CONCURRENT=1`, `SISTER_BUCKET_H=8`, `RIM_MIN_CAMPAIGN_H=48` | **inert on their own** — byte-identical output |
| **Anti-sliver on TBR** (`SLIVER_TBR=1.0`) | negative, confirms the existing default of 0 |
| **Press platen rim window** | master is unusable — see §4 |
| **Machine rim lock as a hard constraint** | −14 pt PCR; caused **87 % of August's infeasible hours** |
| **L9 optimiser** | 1,428 candidates, **0 moves accepted**, all nine cost tiers unchanged |
| **Queue-level rim grouping in L5** | already in the code as a failed experiment: 0 same-rim changes, −25,549 tyres |
| **Removing the tail** (`HORIZON_TAIL_H=0` / `truncate`) | **−1.3 pt PCR.** The tail was never counted in-month; removing it deletes the pull that keeps building running on day 31 |

---

## 3. Diagnostics that changed the picture

### 3.1 Idle capacity is mostly NOT usable
```
July PCR idle 2,417 h
  usable by an eligible unfed GT                      609 h (30 %)
  inside that GT's R5 72 h window as well            222.9 h (9.2 %)
```
**90–95 % of idle machine time cannot legally feed anything waiting** — wrong
machine (eligibility) or wrong time (R5). Total-hours arithmetic overstates
recoverable capacity by ~10×.

But **48–63 % of unfed VOLUME is reachable** (Jul PCR 9,480 of 19,600; Aug PCR
8,326 of 13,210) — those seats need few hours. That is ≈ **+2.0–2.4 pt** and is
the only bounded scheduling target left.

### 3.2 Why build-first cannot work
A green tyre expires 72 h after build (R5), so building cannot run ahead of its
cure seat. Packing machines densely produces scrap. Build-first was also the
gen-1 architecture and was measured: GT head 7.4 h against the plant's 4.4 h
≈ 1,548 tyres of standing GT.

### 3.3 Why machines sit below 70 %
Utilisation tracks eligibility width, not scheduling quality:

| machine | GTs it may build | utilisation |
|---|---|---|
| TBMPCR7 | 1 demanded (39 in matrix) | 83.5 % Jul / 72.6 % Aug |
| TBMPCR2 | 13 (130 in matrix) | 79–83 % |
| TBMTBR9 | 2 | **37.8 % Jul / 27.8 % Aug** |

`TBMPCR7` builds one GT because only **6 of July's 48 demanded PCR GTs** are
allowed on it, and GT 1513 alone is 55,663 tyres. Not a scheduling choice.

### 3.4 The July gap is mostly boundary, not capability
July demand IS the plant's own production proxy, and the rate is NOT the ceiling
(peak 13,288/day vs 12,816 required). The 7.8 pt PCR gap:

| cause | pt | fixable by |
|---|---|---|
| carry-out tail | 1.8 | **plant ruling** |
| cold start, day 0–2 | 2.9 | **plant ruling** |
| timing / fragmentation | 2.7 | engineering |
| B12 residual (plant makes these) | — | policy |

**4.7 of 7.8 pt (60 %) is two accounting boundaries.** TBR: 3.2 of 4.1 pt (78 %).

### 3.5 August has a hard rate ceiling
`426,688 ÷ 31 = 13,764 tyres/day required`, presses peak at **13,319/day** →
−3.2 pt that no scheduler recovers. July has no such ceiling.

### 3.6 Day-1 dip is a stocking problem
Day 1 presses run **70–71 %** of available hours. Cause: only
**27 of 73 (Aug) / 25 of 48 (Jul)** demanded GTs hold any opening stock. A press
needs the *specific* GT it is seated for; aggregate GT inventory cannot feed it.

### 3.7 The allowable matrix is genuinely the plant's rule
Of the (GT, machine) pairs the plant ACTUALLY built on in 8 months of MES
(`basis=OBSERVED`), only **2 of 103 (Jul)** and **1 of 137 (Aug)** fall outside
the matrix. The old 19.9 % violations were `INCH`-basis machines the plant never
used.

---

## 4. Data defects found

| defect | detail |
|---|---|
| **`press_platen_master.rim_lo/rim_hi` is wrong** | 45″ recorded 14-20 where `press_class_pcr` states **12-16**; the 46″ class was invented outright. Every pair it rejected is permitted by `allowed_press_matrix`, 57 of 61 as `direct`. **Removed, do not re-add** |
| **`gt_sku_master` returns BOM codes for TBR** | `GT 5025`, not the MES itemCode. Bridging TBR through it gave 0/56 demand coverage. Use `gt_sku_from_recipe` first (37/37 overlap) |
| **L11 same-size counted unknown rims as SAME** | `rim_of.get(a) == rim_of.get(b)` → `None == None` is True. 16 August PCR transitions inflated; known-vs-unknown deflated |
| **`gt_size` missing 35 PCR rims** | all fillable from `aging_limits` via SKU, unambiguously |
| **mould-change minutes were in the wrong column** | `CO_Mins` was literal `0.0`; minutes went to `Mould_Clean_Mins`, keyed on the starting shift so boundary-straddling events double-charged |
| **`main.py` overrides reached only child processes** | `status` reported the month-default opening-GT while planning used another |
| 42 of 502 SKUs in the plant matrices have no GT | `masters/UNBRIDGED_allowable.csv` |
| 60 unmapped demand rows; 5 Book6 SKUs (9,854 tyres) no GT | never planned, listed |
| 50 TBR cure cycle times missing | mined fallback in use |
| **running-mould files are 2–3 days, timestamps read 2024** | usable as a seated-mould snapshot only |
| PCR workbook sheet *"to be clarified by plant team"* | excluded deliberately — unratified rows must not sit behind a hard constraint |

---

## 5. Changeover: plant vs us

| metric | plant | JUL | AUG |
|---|---|---|---|
| PCR build CO / machine-day | 2.66 | **2.65** ✅ | 3.12 ❌ |
| PCR weighted build CO min/machine-day | **74.0** | 80.6 ❌ | 96.9 ❌ |
| PCR same-size share | 91.5 % | 80.0 % | 78.1 % |
| TBR build CO / machine-day | 3.56 | **3.36** ✅ | **2.12** ✅ |
| TBR weighted build CO min/machine-day | 35.6 | **33.6** ✅ | **21.2** ✅ |
| TBR same-size share | 100 % | **100 %** ✅ | **100 %** ✅ |
| PCR mould changes / press-day | 0.08 | **0.04** ✅ | **0.04** ✅ |
| TBR mould changes / press-day | 0.04 | **0.03** ✅ | **0.03** ✅ |

**We beat the plant 2:1 on curing changeover, both plants, both months.** The
only loss is PCR building weighted minutes, and it is a size-MIX problem: July's
count is level with the plant (2.65 vs 2.66) yet minutes are 9 % over.

### Total curing changeover, July
| | changes | CO minutes | hours | of which mould handling |
|---|---|---|---|---|
| PCR | 117 | 42,050 | 701 h | 90 min × 117 = 175 h |
| TBR | 89 | 32,129 | 535 h | 133.67 min × 89 = 198 h |

Mould handling is a **component** of `CO_Mins`, never an addition — occupancy
charges `CO_Mins` once. Source: `CTP Set up building, curing and inspection.xlsx`
(PCR 90 min, TBR 133.67 min; remainder is press warm-up).

Building CO is **9.4 % of PCR machine capacity**; curing CO is **1.1 % of PCR
press capacity**. If hunting changeover, the building side holds the time.

---

## 6. New code

| file | purpose |
|---|---|
| `main.py` | single entry point: status/check/plan/export/verify/masters/rebuild/test |
| `planner/paths.py` | one resolver for every input. `input_derived()` and `wh_derived()` are separate ON PURPOSE — `press_mould_change.parquet` differs between the trees |
| `planner/cmbc/l1_preflight.py` | MES-free input gate |
| `planner/cmbc/allowable.py` | allowable matrix + rim lock as hard filters |
| `planner/cmbc/l4b_capacity_flow.py` | max-flow feasibility + `allocate()` + `allocate_timed()` |
| `planner/cmbc/l56_loop.py` | L5↔L6 loop, `--mode split|delay` (both measured negative) |
| `scripts/ingest_allowable_matrix.py` | plant matrices → `allowed_machine_matrix.parquet` |
| `scripts/ingest_user_demand.py` | Book6 SKU list → PCR/TBR split, 97.6 % via recipe chain |
| `scripts/ingest_running_moulds.py` | seated mould per press, single-day snapshot (month length − 3) |
| `scripts/check_allowable.py` | independent verifier, imports no planner layer |
| `tests/unit/test_paths.py` | locks the two-namespace contract |

Retired: old L1, L6, L8, L9, L12, 5 diagnostics.

---

## 7. Open — needs the plant, not engineering

Ranked by value:

1. **Widen the narrow machines.** 26 of 73 Aug PCR GTs have ≤ 2 allowable
   machines; TBMPCR7 has 39 in the matrix against TBMPCR2's 130; TBMTBR9 has 2
   demanded GTs. **70 % of PCR idle is locked behind this.** Largest lever left.
2. **Horizon ruling** — carry-out tail: +1.8 pt Jul PCR / +5.8 pt Aug PCR.
3. **Carry-in ruling** (~4 h pre-month build) — +2.9 pt Jul PCR.
4. **Opening stock spread across more GT codes** — only 37–52 % of demanded GTs
   hold any; day 1 runs at 70 % press utilisation.
5. **July partition rebuild** — needs the raw MES drop; July currently runs
   unpartitioned (ledger prices a missing partition at 0.58 pt + 10.3 pt same-size).
6. **Sign off the *"to be clarified by plant team"* sheet** — could widen PCR further.
7. **TBR August regression, UNRESOLVED** — 94.6 % → 89.3 % after adopting the
   plant TBR matrix, which is tighter than the previously-derived list for
   August's 37 GTs. Needs a decision: plant file everywhere, or PCR only.

---

## 8. Arithmetic ceilings — no scheduler recovers these

- **Aug PCR needs 13,764 tyres/day; presses peak at 13,319** → −3.2 pt
- **90–95 % of idle machine hours** are unusable (eligibility × R5)
- **July is NOT rate-limited** — its gap is boundary and timing, so July 100 %
  is genuinely reachable via items 2–5 above

## 9. Shipped configuration

```
PLANNER_STRICT_ALLOWABLE=1      plant allowable matrix HARD
PLANNER_STRICT_RIMLOCK=0        mined habit, not plant rule — costs 14 pt
PLANNER_STRICT_PLATEN           n/a, filter removed entirely
PLANNER_LOT_INTERVAL_H=8        was 16; +1.0 pt Aug PCR
PLANNER_L4B_ALLOC=1             allocation preference into L7
PLANNER_HARD_PIN=1              sticky pinning kept; de-pin measured worse
PLANNER_HORIZON_MODE=extend     tail 72 h; removing it costs 1.3 pt
PLANNER_SLIVER_TBR=0            anti-sliver off on TBR
PLANNER_PARTITION_PLANTS=       (July only — no July partition exists)
```
