# BUSINESS_RULES.md

## Objective (north star — stated by planning, Aug 2026)

> **Produce 100% demand with the fewest changeovers, longest practical
> campaigns, grouped sister SKUs, balanced machine utilization, synchronized
> Building & Curing, minimum WIP/aging, and zero constraint violations.**

Every rule below serves that sentence. Where two rules conflict (e.g. P8/P9
"minimum presses" vs S4 "72h shelf life"), resolve in favour of the objective:
zero violations first, then demand, then changeovers/campaigns, then WIP/aging,
then utilisation balance.


Plant business rules for the Building + Curing planner, as specified by the
planning team. Rule text is **verbatim as given**; the status and implementation
columns are added so this doubles as a compliance checklist.

Status legend:

| | Meaning |
|---|---|
| **Enforced** | The planner actively implements this and it holds in output |
| **Partial** | Biased toward it, or measured but not constrained |
| **Open** | Not implemented yet |
| **Blocked** | Cannot implement without data the plant has not supplied |

**Scorecard: 48 rules — 22 Enforced · 10 Partial · 13 Open · 3 Blocked.**

*(Corrected Aug 2026: the table has always had 48 rows — B1–B16, P1–P9, C1–C8,
S1–S5, G1–G8, E1–E2 — and every count in the previous header was wrong. `PARTITION_AND_CHANGEOVER.md` still says "46 numbered rules"; 48 is the count.)*

Recent changes (2026-08-09, newest): **THE HARD FLOOR IS NO LONGER PAID FOR IN FULFILMENT.** The −1.56/−9.47/−1.96/−6.53 pt in the note below was a PACKING defect, not the price of B12. Runs are released just-in-time, so each one leaves a hole sized by deadline spacing (p50 1.00 h PCR / 1.30 h TBR) against a floor-minimal run of 2.84 h / 5.05 h; an indivisible run cascades past every sliver to `t0` and is refused beside 1,294 idle hours. Two fixes in `l7` — anti-sliver packing (`PLANNER_SLIVER_*`, default 1.0) and a targeted LNS make-room pass (`PLANNER_L7_MAKEROOM`, default 1) — take fulfilment to **96.37 / 95.02 (Jul PCR/TBR) and 94.89 / 96.82 (Aug)** with sub-floor still **0.0 %**, verified two ways from `build_schedule.parquet`. Reference runs `FIX_jul` / `FIX_aug`; export packs 0 HARD / 0 SOFT / 0 EXPORT. R5, the WIP rails, the rim locks and TT/TL are untouched — all three were tested as causes and all three are false. See [PARTITION_AND_CHANGEOVER.md](PARTITION_AND_CHANGEOVER.md) §4n.

Recent changes (2026-08-09, later): **L5 TAKT CAP** — press concurrency levelled to the month's own work content, TBR only (P8 Open→Partial; +2.14 pt Jul / +5.86 pt Aug TBR; PARTITION §4l.1). · **ATOMIC-SLICE SPLIT** — `l7`'s split-before-starve terminated at `len(grp)==1`, so the B12 sub-floor budget of 180 sat at ~9 spent while 27,203 PCR tyres starved; one halving is now allowed and charged to the same budget (**PCR only**, +1.09/+1.04 pt; §4l.2). · **PER-PRESS MOULD CHANGE (correctness)** — `l5` loaded `press_mould_change.parquet` and reserved the plant MEDIAN anyway, so 28 August events under-reserved by up to 70 min and physically over-ran a still-curing press (§4l.3). · **REJECTED:** load-aware machine tie-break, mixed sign across months (§4l.4). **R5 note: July TBR now runs at 71.5 h against the hard 72 — 0.5 h of margin.**

Recent changes (2026-08-09): **HORIZON IS NOW A CLOSED BOX** — plant ruling: only demand filled inside the month counts; anything past month end is discarded as unfulfilment (`PLANNER_HORIZON_MODE=truncate`, MEMORY §11c). This REPLACES the rolling-horizon/carry-out design. Cost vs the old window: worst −0.50 pt (Aug PCR). Consequence: the month now ends with ~0 GT stock, so G8 'every day including the last' is **unreachable** under this rule — the two are mutually exclusive. · **changeover time now OCCUPIES the machine** (was costed but never reserved — 45 % of it had nowhere to happen; cost 2.1-3.3 pt of fulfilment, correctly) · **fulfilment is now IN-MONTH output** (carry-out tail excluded, opening stock still counted) · **`verify_export.py` now HARD-checks machine-day feasibility**. Earlier: **B16 added** (TT/TL machine dedication, §1a) · **B12 Open→Enforced** (lot floors + minimum demand, §1b) · **C2 unblocked** (tube-type master now derived).

Measured against the Jan/Feb 2026 leak-free walk-forward (see
[MEMORY.md §10b](MEMORY.md)).

---

## 1. Building Rules

| # | Rule | Status | Where / note |
|---|---|---|---|
| B1 | A machine should preferably produce the SKUs it has historically produced. | **Enforced** | MPM rules order candidates; `_gt_machine_map` restricts the feasible set to machines that have actually built that GT — `plan/building.py` |
| B2 | A machine should normally build only the tyre sizes it has historically handled. | **Enforced** | Implied by B1: the candidate set is historical, so sizes are too |
| B3 | Prefer producing the same tyre size before switching to a different size. | **Enforced (live engine)** | ⚠ **STATUS CORRECTED 2026-08-09.** The old note credited `plan/timing_lookup.py` and "sister-SKU clusters order the lots" — both are the **RETIRED gen-1 engine** and neither runs. The live mechanism is the key-2 rim term of the candidate-machine sort in `cmbc/l7_pull_release.py` (search `KEY 2: RIM CONTINUITY`), a tie-break below the rim lock; changeover minutes come from `cap_changeover.parquet`. Measured July PCR **92.2 %** same-size (plant 91.5 %). **August reads 65.2 % but that is a master-data artefact, not behaviour**: 27.9 % of August PCR changeovers involve a GT with no row in `gt_size`, and among known-rim pairs the figure is **89.9 %**. See `PARTITION_AND_CHANGEOVER.md` §4q |
| B4 | Limit the number of size changes on each machine. | **Enforced** | Static GT→machine partition per rim group — PCR size changes **0.0/machine-day vs plant 0.2**, same-size share 97.7% vs plant 91.5%. `scripts/build_gt_machine_partition.py` → `INPUT/derived/gt_machine_partition.parquet`, consumed by `cmbc/l7_pull_release.py`. See [PARTITION_AND_CHANGEOVER.md](PARTITION_AND_CHANGEOVER.md) |
| B5 | Do not change tyre size too frequently on the same machine. | **Implemented behind `PLANNER_RIM_PRIORITY`; the literal plant request is INFEASIBLE and the reason is physical** | The plant asked for sequential single-rim campaigns ("after completing 12 inch we switch to 13 inch and make only 13 like that"). **It cannot be built on this demand**: every PCR rim has an active cure campaign on **all 31 days** of both July and August, with 7–28 presses running that rim concurrently at p50, and R5 forces a green tyre to cure within 72 h of being built (wait p50 5.8 h). A machine feeding a rim must therefore feed it *every day*. What IS achievable is capping the rims per machine — `PLANNER_RIM_MAX_CONCURRENT=2` — which on July PCR takes rim switches **66 → 34**, same-size **92.2 → 95.9 %** and weighted setup **376 → 354 h** for **−0.93 pt**. 9 of 11 PCR machines and 9 of 9 TBR machines already never switch rim. See `PARTITION_AND_CHANGEOVER.md` §4q |
| B6 | Limit the number of building changeovers per machine per day. | **Open** | Measured (`avg_skus_per_machine_day` ≈ 2.5–2.9) but uncapped |
| B7 | Keep daily production stable; avoid large fluctuations between days. | **Enforced in effect (no constraint needed)** | **Interior CV now at plant level and a daily quota was measured and REJECTED.** Days 2–29: PCR **0.046** vs plant 0.031; TBR **0.059** vs plant 0.097 — *TBR is flatter than the plant*. The headline 0.126/0.218 is entirely days 1/30/31, i.e. the horizon boundary: only **2 of 31 days** fall outside a ±15% band. A band would not touch the interior and would be a fourth constraint of the class that has cost most — the WIP rail alone costs 1.5 pt while moving CV by 0.002. See [PARTITION_AND_CHANGEOVER.md](PARTITION_AND_CHANGEOVER.md) §4b |
| B8 | Use realistic machine production rates based on historical performance. | **Enforced** | Per-machine cadence mined from inter-event gaps: PCR **58 s**, TBR **204 s** per tyre. Replaced hardcoded 45/90 s |
| B9 | Limit the number of different machines used to build the same SKU. | **Partial** | `gt_continuation_bonus_h = 36` biases toward fewer; no hard cap |
| B10 | Prefer continuing production of a SKU on the same machine instead of splitting it across multiple machines. | **Enforced** | GT run-continuation bonus — `plan/building.py` |
| B11 | Merge same-day production of the same SKU into one campaign. | **Enforced** | Demand is per (GT, day) → one lot; continuation keeps it on one machine |
| B12 | Avoid creating very small building batches. | **Enforced as a HARD FLOOR (plant instruction, 2026-08-09) — 0 runs below the floor, verified** | Minimum lot **PCR 150 / TBR 70**. `PLANNER_STRICT_LOT_FLOOR=1` (default ON) gives a measured **0.0 % sub-floor on both plants, both months** (min run 150 PCR / 78-82 TBR), re-derived from `build_schedule.parquet` two ways — setup blocks split at >1 h gaps, and emitted `run_id` groups. Three gates: grouping repair, `_place` refusal, `HARD_FLOOR` forced on; `ATOMIC_SPLIT` force-disabled because it works BY creating sub-floor runs. **COST, AFTER PARTITION §4n: −0.59 pt (Jul PCR), −2.34 (Jul TBR), **+0.03** (Aug PCR), −1.84 (Aug TBR)** against a permissive arm on the same engine — down from −1.56/−9.47/−1.96/−6.53. The old figures measured a packing defect, not the rule: an indivisible run needs ONE contiguous hole and the calendar only had 1.0-1.3 h slivers. Fulfilment is now **96.37 / 95.02 / 94.89 / 96.82**, three of four arms over 95 %; Aug PCR's 0.11 pt shortfall is 56 % month-boundary cold start and needs carry-in, not lot sizing. It still buys the changeover result: PCR weighted setup 457→421 h (Jul), CO/machine-day 2.74→2.47, TBR 2.70 vs a 3.56 benchmark. **Stricter than the plant, on instruction** — the plant runs sub-floor 12.7 % (PCR) / 30.8 % (TBR). `PLANNER_STRICT_LOT_FLOOR=0` restores the plant-calibrated budget. See [PARTITION_AND_CHANGEOVER.md](PARTITION_AND_CHANGEOVER.md) §4m, §4n |
| B13 | Build extra GT only when there is a risk that curing will starve. | **Open** | Building is demand-driven, not curing-pull. Relates to E1 |
| B14 | Limit the number of different SKUs assigned to a machine each month. | **Enforced** | Falls out of the partition: PCR **5.5 GTs/machine vs plant 5.9** |
| B15 | Group construction-similar SKUs on the same building machines whenever possible. | **Implemented behind a flag, TBR only — `PLANNER_SISTER_GROUP`** | ⚠ **STATUS CORRECTED 2026-08-09** (was "Partial", crediting the retired `learn/sister_sku.py`). Sister groups are now built from the RAW construction workbooks by `scripts/build_gt_sister_group.py` → `INPUT/derived/gt_sister_group.parquet`, using the plant's own definition: **GTs differing in exactly ONE component slot** (Hamming distance 1 over the component signature). Consumed as a candidate-machine tie-break in `cmbc/l7_pull_release.py` (`_sistkey`). **TBR only, by measurement — PCR has NO sister structure**: over 595 July GT pairs there are zero at distance 1, 2 or 3, because every PCR component code is size-specific so all six slots change together. See §1c below and `PARTITION_AND_CHANGEOVER.md` §4r. ⚠ **AMENDED 2026-08-09 (§4s).** The plant-supplied construction-cluster workbooks (`scripts/build_sku_con_cluster.py` -> `INPUT/derived/sku_con_cluster.parquet`) show **PCR construction structure DOES exist** — the plant's same-cluster adjacency is 14.1 % against a 10.0 % permutation null (z = +7.5), with a realised gap of 2.4 min vs 13.9 min for a different-cluster same-rim transition. "No sister structure" was too strong. What holds is that it is **redundant with the rim lock**: 33 of 37 genuine PCR clusters are already single-machine single-rim, and only 4 GTs / 6.9 % of July demand sit in a co-active multi-GT cluster. Grouping PCR on clusters costs **−1.41 pt (Jul) / −2.07 pt (Aug)** and is rejected. TBR: 41 GTs / 81.6 % of July demand are groupable |
| **B16** | **Dedicate whole machines to TT or TL for the entire horizon. Split the TBR machines into a TT group and a TL group sized from that month's demand, and never plan a TT/TL changeover on any machine.** | **Partial — split now BOTH-SIDED** | **Data now available** — `INPUT/derived/tt_tl.parquet`, 100% of TBR demand tagged. See §1a below. Supersedes the press-level C2 on the building side |

### 1b. B12 — lot sizing and minimum demand  ⭐ standing rule

**The lot is the CURE lot. Build slices have NO minimum.**

R9/B12 says avoid small batches. Phase 0 shows the plant honours that at the
**cure campaign** level (57 h / 265 h campaigns, 1.43 / 1.19 changeovers per
resource-day) and **deliberately violates it at the building level** (5.5–7.7 h
campaigns, 2.46 / 3.51 changeovers). Building absorbs changeovers so curing does
not have to. That trade is correct and the engine must not reverse it.

| Level | Floor | Enforcement |
|---|---|---|
| **Cure lot** — the real lot | hard minimum | must not be broken |
| **Build slice** — a delivery, not a lot | **none** | deliberately small: this IS the JIT feed |

> ⚠️ Applying one minimum across both either fragments cure campaigns or forces
> building to run ahead — recreating the head gap. **Do not add a build-slice
> minimum check.** Comment it in code; someone will eventually try to "fix" it.

**The floors:**

| | minimum lot | minimum horizon demand to plan |
|---|---:|---:|
| **PCR** | **150** | **300** |
| **TBR** | **70** | **150** |

A GT whose horizon demand falls below the minimum is **not planned** — it is not
worth a machine setup for a handful of tyres. It goes to the **residual policy**,
never a silent drop and never a silent round-up.

**Residual policy** — when demand cannot reach the floor (e.g. 200 tyres against
a 4,000-tyre economic lot), choose explicitly:
1. consolidate into the next campaign for that mould set, even if late
2. over-produce to stock, if carrying cost < changeover cost
3. flag to the planner as an exception **with both costs shown**

Silently rounding up builds dead stock; silently dropping loses demand. Both are
failures. This is a business call and lives in the cost table, not in code.

**Upper bound comes from R5, not B12:**

```
max_cure_lot(SKU) <= cure_rate(SKU) x 72 h
```

Campaign length is bounded where build rate would outrun cure rate long enough to
push the oldest tyre past shelf life. On PCR — dedicated 1:1 machines at high
occupancy — this bites earlier than expected. **Compute per SKU; 72 h is not slack.**

**Derived floor (preferred once computable):**

```
min_cure_lot = moulds_active x cavities x (campaign_min_hours x 60 / cycle_min)
```

The floor should fall out of mould count and mount cost, not policy. Blocked on
**mould cavity count** (`Full_Load` is 100% NULL). Until then the fixed floors above apply.

---

### 1a. B16 — TT/TL machine dedication (TBR)  ⭐ standing rule

**Why dedication and not sequencing.** A TT↔TL switch is a major setup with long
downtime (see R8 of `Building Business Rules.docx`). Paying it repeatedly destroys
capacity on a line already balanced to 0.3% between its build and cure ceilings.
So the machine group is **fixed for the whole horizon** and the optimiser may not
break it — this is a hard constraint, not a weighted preference.

**The split is DERIVED each month, never hardcoded.**

```
1. Tag every TBR demand row TT or TL          -> INPUT/derived/tt_tl.parquet
2. hours_TT = qty_TT / build_rate ;  hours_TL likewise
3. n_TT = round(9 * hours_TT / hours_total)  ;  n_TL = 9 - n_TT
4. Assign machines to groups ONCE for the horizon
5. HARD: a TT GT may only be planned on a TT machine, and vice versa
6. HARD: zero TT<->TL changeovers anywhere in the plan
7. If either group exceeds ~95% load -> report INFEASIBLE and re-split.
   NEVER spill across the boundary silently.
```

**July 2026 worked example** (100% of TBR demand carries a tag — measured, not estimated):

| | tyres | share | GTs | build hours | machines needed |
|---|---:|---:|---:|---:|---:|
| **TT** | 66,322 | **67.7%** | 26 | 3,936 h | **5.57** |
| **TL** | 31,698 | 32.3% | 30 | 1,881 h | **2.66** |
| total | 98,020 | 100% | 56 | 5,817 h | 8.23 |

> **July answer: 6 machines TT · 3 machines TL.**
>
> **5/4 is infeasible** — TT needs 5.57 machines of load, so 5 machines would sit
> at **111%**. 6/3 gives TT 93% and TL 89%, both feasible, ~0.77 machine spare.
> Recompute every month: a month at 55% TT would want 5/4. The *rule* is fixed;
> the *number* is derived.

**Group membership.** Prefer keeping a group within one machine make — TBR is
SAV (TBM 01–03) and MESNAC (TBM 04–09) — so build cycle times stay uniform
inside a group.

---

## 2. New Planning Rules

| # | Rule | Status | Where / note |
|---|---|---|---|
| P1 | Cluster sister SKUs together before scheduling so that similar products are planned in the same campaign. | **Not implemented (live engine); available behind `PLANNER_SISTER_BUCKET_H`** | ⚠ **STATUS CORRECTED 2026-08-09** — was "Enforced" citing `learn/sister_sku.py` and `_sequence_order`, both in the **RETIRED gen-1 engine**. The live L7 places in strict global cure-deadline order, so campaign membership is set by the deadline, not by similarity. `PLANNER_SISTER_BUCKET_H=4` groups sisters within a 4 h deadline bucket — the bounded form of the queue re-sort that cost 25,549 tyres at L5. Measured: TBR sister adjacency 43.0 → **49.3 %** for **−0.70 pt** TBR fulfilment (July). **Off by default** — the trade is real and the plant should choose it. ⚠ **EXTENDED 2026-08-09 (§4s.5):** `PLANNER_CLUSTER_BUCKET_H` does the same thing on the plant's own **construction clusters** rather than mined sisters. TBR cluster adjacency 39.4 -> 45.9 % (Jul) / 49.0 -> 53.6 % (Aug) at a 24 h bucket, against the plant's 67.2 %. **Mixed sign: −1.15 pt July, +0.52 pt August**, and NO bucket from 2 h to 48 h is neutral in July — so it is the month, not the tuning. Priced at the plant's own 8.25 min per converted transition, July buys 6.74 h of setup for 1,127 tyres. Off by default; `PLANNER_CLUSTER_PLANTS=TBR` leaves PCR bit-identical |
| P2 | Keep sister SKUs on the same machine whenever possible to minimize setup and changeover time. | **Implemented behind `PLANNER_SISTER_GROUP`, but it does not bind** | ⚠ **STATUS CORRECTED 2026-08-09** (was "Open"). `_sistkey` in `cmbc/l7_pull_release.py` prefers a machine whose last block was a sister. **Measured: it does not move sister adjacency at all** (TBR 43.0 → 42.4 %), because `HARD_PIN` gives most runs a single feasible machine — the candidate list is usually length 1, so ordering it is a no-op. **Adjacency is a property of the QUEUE, not of machine choice** — see P1. Fulfilment effect is small and positive (July PCR +0.09, TBR +0.14 pt) but it is reshuffling, not the intended mechanism |
| P3 | Use component similarity (belt, body ply, etc.) when grouping SKUs, since these components drive setup time. | **Data built and validated; the COST MODEL cannot price it** | Signatures come from the raw workbooks (`scripts/build_gt_sister_group.py`). **The premise is confirmed on TBR**: controlling for full tyre size, the plant's own realised gap between consecutive runs is **5.15 min at distance 0, 8.29 at distance 1, 12.10 at distance ≥2** — a monotone dose-response, Cliff's δ −0.768 (d≤1 vs d≥2), p≈0, n=4,081. **But the changeover master has no component dimension**: `cap_changeover.parquet` is keyed on (machine × same-size/different-size) only, so our engine charges TBR 10 min for a sister and a non-sister transition alike. Capturing this needs a **third tier in the master**, not a planner weight. See `PARTITION_AND_CHANGEOVER.md` §4r. ⚠ **CONFIRMED ON BOTH PLANTS 2026-08-09 (§4s.4)** using the plant's own construction clusters and a within-machine permutation null: PCR realised gap **2.39 min same-cluster vs 13.91 min different-cluster same-rim** (CI95 on the median difference [−12.11, −11.04], n=311/1,375); TBR **7.53 vs 15.78 min** (CI95 [−8.63, −7.82], n=3,419/1,329). The premise now holds on PCR too. The cost master still cannot price it, so the effect is read from `cluster_adj_pct` in `scripts/arm_kpi.py`, never from `weighted_setup_h` |
| P4 | Maintain a nearly constant daily production quantity throughout the planning horizon. | **Partial** | Same as B7 — measured, not constrained |
| P5 | Avoid under-utilized machines; every scheduled machine should have meaningful monthly production. | **Enforced** | Load balancing via bounded preference; util **87.9–91.8%**, no machine left idle |
| P6 | Assign each GT code only to its historically validated machine(s) unless there is strong evidence or capacity constraints requiring an exception. | **Enforced** | The partition seats a GT only on machines that historically ran its rim; capacity is the sanctioned exception and is reported (`UNSEATED`, tier-5 split) |
| P7 | Avoid unnecessary machine splitting for GT codes that historically run on a single machine. | **Partial** | Partition raised PCR GTs-on-one-machine **27.5% → 52.5%** (plant 66.7%), machines/GT 2.12 → 1.52 (plant 1.40). Big GTs are split across *same-size* machines on purpose — going to 1.02 was **stricter than the plant** and cost 1.3 pt |
| P8 | Use only the minimum number of presses required to satisfy demand; do not activate unnecessary presses. | **Partial (TBR Enforced, PCR Open)** | **L5 now caps CONCURRENTLY SEATED presses per partition at the takt rate** `N_k = ceil(W_k/U)` — TBR August 61 of 79, July 75 of 79 (`PLANNER_L5_TAKT=flat`, PARTITION §4l.1). This is P8 in its usable form: not "fewer presses ever" but "no more seated at once than the month's work content needs". Measured **+2.14 pt Jul / +5.86 pt Aug on TBR**, press occupancy CV 0.472 → 0.157 (Aug). **PCR excluded — measured mixed-sign** (−0.28 Jul / +0.18 Aug) with only 3.4–4.4 % press slack to level. PCR still spreads by historical CDF |
| P9 | Balance press utilization across only the required active presses instead of spreading demand across all available presses. | **Open** | Same root cause as P8 — `plan/curing.py` needs press-count minimisation |

## 3. Curing Rules

| # | Rule | Status | Where / note |
|---|---|---|---|
| C1 | A press should preferably cure the SKUs it has historically cured. | **Enforced** | Weighted-CDF press pick from historical usage — `plan/curing.py` |
| C2 | A press should process only one tube type (TL/TT) at a time and limit tube-type changes. | **Open** | **Unblocked** — tube type now derived: `INPUT/derived/tt_tl.parquet` (TBR 100%, PCR 88% of demand). Building side is covered by **B16**; the press-side constraint is still to be implemented |
| C3 | Avoid changing moulds for very small curing quantities. | **Open** | No minimum mould campaign |
| C4 | Limit the number of curing changeovers per day. | **Open** | ~264 K counted and **uncosted** — needs the mould-change time matrix (see §7) |
| C5 | Release surplus presses early if they are no longer required. | **Open** | Not implemented |
| C6 | Activate new press allocations only when required by demand. | **Open** | Same as P8 |
| C7 | Protect starving SKUs by giving them higher priority. | **Enforced** | FIFO pairing in `plan/sync.py`; starvation events = **0** |
| C8 | Never assign a press to a SKU that cannot currently be built. | **Enforced** | Cure events are generated only from GT supply on the ledger |

## 4. Building–Curing Synchronization Rules

| # | Rule | Status | Where / note |
|---|---|---|---|
| S1 | Building should continuously feed curing without causing starvation. | **Enforced** | `plan/sync.py` FIFO pairing; starvation **0**, sync **100%** |
| S2 | Do not produce excessive GT inventory. | **Partial** | Avg WIP cut 5.6× to **~298**; no explicit ceiling |
| S3 | Maintain GT inventory within storage limits. | **Blocked** | No storage capacity supplied. Needs max GT racks/floor stock per plant |
| S4 | Respect GT shelf life and consume older GT first (FIFO). | **Partial** | FIFO enforced; shelf-life limit not — needs `masters/aging_rules.parquet` |
| S5 | Building output should closely match curing requirements. | **Partial** | Sync 100%, but building still runs ahead — GT aging p95 **101 h vs 32 h** actual |

## 5. General Planning Rules

| # | Rule | Status | Where / note |
|---|---|---|---|
| G1 | Never exceed customer demand unless explicitly allowed. | **Enforced** | Lots are derived from demand exactly; no overbuild path |
| G2 | Respect all allowable machine and press constraints. | **Enforced** | Derived from history (B1/P6). Will tighten with `allowed_machine_matrix` |
| G3 | Respect machine capacities and shift calendars. | **Blocked** | Capacities yes; **calendar assumed 24×7** — needs `masters/calendar.parquet` |
| G4 | Respect real changeover times. | **Enforced, and now cost-*directed*** | Per-machine from `v_changeover_build`: PCR m1–5 **28/60**, m6–11 **22/42**, TBR **10/24**. A repair pass moves size changes to the cheapest machine (or to one already on that size, eliminating the change): PCR **48.9 min per different-size CO vs plant 55.7**. ⚠ Never hardcode these — a flat 11.3/42.4 made every setup figure wrong, per §1c |
| G5 | Schedule periodic mould cleaning after the specified production cycles. | **Blocked** | Needs the cleaning interval (cycles) and duration per mould |
| G6 | Start planning using the previous month's plant setup and machine assignments where appropriate. | **Open** | Each month starts cold apart from opening GT WIP. Carrying last month's machine state forward would cut month-boundary changeovers |
| G7 | Compare every generated schedule against the historical plant schedule and improve measurable KPIs. | **Enforced** | `replay/compare.py` + `replay/full_kpi.py` on every run |
| G8 | **Daily green-tyre inventory must stay inside PCR 4,500–4,800 and TBR 1,200–1,500 — on every day, including the last day of the month.** | **Partial — now fully MEASURED** | Band in `config.gt_wip_max/min`; enforced in `plan/window_plan.py` as a daily cap on build against projected stock. **Now measured on BOTH halves.** Mean: PCR 3,851 / TBR 989 — *below* the band, deliberately (we sit under the plant's 4,832 / 1,743). **Last day: PCR 486 / TBR 195 against floors of 4,500 / 1,200 — a hard FAIL, and it is the one G8 explicitly names.** `l11_validate_plan._daily_mean_l11` added the last-day check Aug 2026; it is a **DETECTOR, never a controller**. Verified root cause is the DEMAND HORIZON, not pacing: build and cure collapse *together* on day 31 (PCR to 25% / 32% of interior, TBR 7% / 13%), so WIP = cum(build) − cum(cure) → 0 by construction. Fix is `l4 --lookahead-days` (built, no-op on July — no August demand can exist). **Never force a closing-stock floor**: it means building tyres with no cure to consume them and destroys the audited `built == fed`. |

## 6. Additional Rules (recommended by planning)

| # | Rule | Status | Where / note |
|---|---|---|---|
| E1 | Do not create unnecessary early production. Build as close as practical to the curing requirement to minimize GT aging and WIP. | **Partial** | Biggest remaining gap. Building finishes in ~708 h while curing trails, so tyres wait. Aging **101 h vs 32 h** actual. Needs due-date-paced release |
| E2 | Prioritize demand fulfillment with the fewest possible changeovers. When multiple schedules achieve similar demand, choose the one with longer campaigns, fewer setups, and more stable machine utilization. | **Partial** | Encoded in `optimize/objective.py` weights (demand ×1000, changeover ×1, WIP ×0.5, cure-wait ×2), but the SA/Tabu/LNS optimizer has not been run on these schedules yet |

---

## 6b. Stated planning ASSUMPTIONS (not measured — handed down)

Everything else in this file is a rule the plant gave us or a behaviour we mined
from MES. This section is different: it records assumptions the domain authority
**instructed** us to make. Future readers must know these were **assumed, not
measured**, because no amount of data will confirm them.

| # | Assumption | Stated | Status | Where / note |
|---|---|---|---|---|
| **B-ASSUME-1** | **"Assume everything is available for building from the very start — we don't have to wait for anything."** At `t0` the plant is fully ready: materials, components, compounds, machines, moulds all staged. No ramp-up, no warm-up, no staging delay. Building runs at full rate from hour 0. Applies to **every month**, not one. | Plant, 2026-08-09 | **Enforced — and already true before the ruling** | See below. Audit flag `PLANNER_FULL_AVAILABILITY_T0` |

### B-ASSUME-1 — what the engine already does, verified line by line

The ruling was checked against the code rather than assumed to need work. It was
**already satisfied on every clause**:

| clause | where | verdict |
|---|---|---|
| materials / components / compounds | never referenced in `l6_build_gate.py` or `l7_pull_release.py`; components are exploded **downstream** in `l8_prep_explosion` | **vacuously true** — material has never been a building constraint in this engine |
| building machines free at hour 0 | `l7_pull_release.py` `busy: dict[str, list] = {}` | **true** — build starts at `t0 + 0.00 h` |
| presses free at hour 0 | `l5_cure_master.py` `free: dict[str, datetime] = {}`, then `free.get(pr, t0)` | **true** |
| moulds | concurrency bounded by mould **count** (R3); no time-phased availability | **true** |
| opening GT available at `t0` | `l7_pull_release.py` loads `masters/opening_gt/opening_gt_<M>.parquet` and credits it as supply at `t0` | **true**, subject only to R5 |

**R5 (72 h shelf life) is untouched and stays** — it is a quality constraint, not
an availability window, and the ruling explicitly preserves it. The WIP rails,
the B12 lot floor, TT/TL and the rim locks are likewise untouched.

### The one thing the ruling does NOT cover, stated plainly

A press curing at `t0` needs a green tyre that **physically exists** at `t0`. The
only pre-horizon source is opening stock, and opening stock is **already drawn to
zero** on every GT that has day-1 shortfall (measured: spare = 0). The residual
is therefore **cure-before-build**, which is downstream physics, not input
availability — and the ruling does not create inventory that does not exist.

Measured July, `runs/jul_diag`: the day-1 runs need to start only **2.3 h (PCR
p50) / 3.3 h (TBR p50)** before `t0`, max 4.2 h on PCR. That is a **carry-in /
rolling-horizon** gap of a few hours, not an availability gap. Closing it means
**building before `t0`**, which the closed-box horizon rule forbids and
`verify_export.py` fails as a HARD violation (every row must sit inside the plant
month). **That conflict is for the plant to rule on, not for the planner to
resolve.** See [PARTITION_AND_CHANGEOVER.md](PARTITION_AND_CHANGEOVER.md) §4p.

---

## 7. What is still needed from the plant

Ordered by how much it unblocks:

| Need | Unblocks |
|---|---|
| `masters/calendar.parquet` — shifts, holidays, planned maintenance | G3; also makes utilization honest (we assume 24×7) |
| **Mould-change time matrix** — `from_gt, to_gt, press, minutes` | C3, C4, P8, P9 — curing changeovers are currently uncosted |
| **GT storage capacity** per plant | S3 |
| **Mould cleaning interval** (cycles) + duration | G5 |
| `masters/aging_rules.parquet` — GT shelf life min/max hours | S4 — **partly resolved**: `Ageing spec-20.01.2024 rev12` gives min/max for 62 components (`INPUT/derived/semi_finished_ageing.parquet`). GT shelf life is **hardcoded 72 h** by instruction; the spec's 48 h/24 h figures are recorded but not applied |

Everything else in the master list is already derived from MES history at
acceptable accuracy — opening inventory (±1.5% vs ground truth), cycle times,
allowed machines, lot sizes.

## 8. Priority of open work

1. **B16 — TT/TL machine dedication.** Standing hard rule, data ready, and it bounds every other TBR building decision. Do this before machine-assignment work.
2. **E1 / S5 — pace building to curing demand.** Largest measurable gap (aging 101 h vs 32 h) and needs no new plant data.
3. **P8 / P9 — minimise active presses.** Currently the planner spreads across all presses, directly contradicting two stated rules.
4. **P2 — pin sister groups to machines.** Small change to the continuation bonus; should cut changeovers further.
5. **B4 / B5 / B6 / B14 — campaign-length and changeover caps.** Straightforward once E1 is in, since both need the same release-pacing machinery.
6. **G6 — carry previous month's machine assignments forward.**
