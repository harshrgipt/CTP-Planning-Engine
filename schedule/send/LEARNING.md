# LEARNING LOG — what the plant taught us, month by month

Cumulative walk-forward. Snapshot *k* has seen the first *k* months and **nothing after**. Sixteen miners: the nine specified, plus seven added because this engine has already been burned by not having them.

| # | Miner | Why it exists |
|---|---|---|
| 1 | Machine Preference | specified |
| 2 | Press Preference | specified |
| 3 | Building-Curing Synchronization | specified |
| 4 | Real Capacity | specified |
| 5 | Changeover | specified |
| 6 | SKU Stickiness | specified |
| 7 | Campaign & Lot Size | specified |
| 8 | Bottleneck | specified |
| 9 | Utilization | specified |
| 10 | GT Inventory (Little's Law) | the plant holds `I = λ·W`, W≈9h. Without a setpoint WIP climbed 4× and no KPI saw it |
| 11 | GT Aging | 72h shelf life is the binding HARD rule; 6.9% of one plan was scrap, unreported |
| 12 | Scrap / Loss | `build/cure − 1` is LOSS, not drift. Target 1.000 and you under-deliver 0.5–2.0% |
| 13 | Mould Capacity M_g | `M_g` caps `n_g`, so the rectangle model rests on it. We only had a lower bound |
| 14 | Eligibility Churn | 40–47% of pairs are NEW monthly. Gating on history starves the plan |
| 15 | Calendar / Downtime | Jan has a near-shutdown day (3,068 vs 12,666); 24×7 cannot represent it |
| 16 | Size Lock | 99.89% — belongs in the candidate SET as a hard prefilter, not a score term |


---

## Snapshot 1 — through 2025-12 (as-of 2026-01-01, 1 month seen)

**1. Machine Preference**

- A GT's top machine carries 100% of its volume (median); a GT uses 1 machines.
- => machine preference is a RANKING signal, strong but not exclusive.

**2. Press Preference**

- A GT spreads over 2 presses (median), top press taking 54%.
- => presses are POOLED per GT, not dedicated.

**3. Building-Curing Synchronization**

- TBR: build->cure lag p50 4.4h, p95 27.6h; 65% cured in the SAME SHIFT, 93% within a day.
- PCR: build->cure lag p50 4.3h, p95 27.1h; 69% cured in the SAME SHIFT, 94% within a day.
- TBR: corr(built, cured) per GT-day = 0.933, both stages active on 99% of GT-days.
- PCR: corr(built, cured) per GT-day = 0.943, both stages active on 99% of GT-days.
- => the plant builds a GT the SAME DAY it cures it. Build lead is ONE SHIFT, not one day.

**4. Real Capacity**

- TBR: press does 42 tyres/day (p50), 50 p95, across 80 presses.
- PCR: press does 154 tyres/day (p50), 198 p95, across 88 presses.
- PCR: machine does 1155 tyres/day across 11 machines.
- TBR: machine does 354 tyres/day across 9 machines.
- => rate = 3 x floor(480/eff_CT); dwell time understates it ~3x.

**5. Changeover**

- TBR: 854 building changeovers over 9 machines.
- PCR: 555 building changeovers over 10 machines.
- PCR: 170 curing mould changes over 61 presses.
- TBR: 80 curing mould changes over 49 presses.
- => campaign == window, so changeovers = sum_g n_g - |P| in closed form. No search needed.

**6. SKU Stickiness**

- PCR build: 2.04 SKUs per resource-day; 29.9% of resource-days run a SINGLE SKU.
- TBR build: 3.06 SKUs per resource-day; 15.8% of resource-days run a SINGLE SKU.
- PCR cure: 1.03 SKUs per resource-day; 96.7% of resource-days run a SINGLE SKU.
- TBR cure: 1.01 SKUs per resource-day; 98.9% of resource-days run a SINGLE SKU.
- => a press NEVER changes GT within a day (100% stickiness). Hold the mount for a full day minimum.

**7. Campaign & Lot Size**

- TBR: a GT is cured on 15 days of the month using 2 presses (mean 2.73).
- PCR: a GT is cured on 14 days of the month using 3 presses (mean 4.41).
- => n_g x D_g = area_g is INVARIANT. Area is fixed by demand; only the SHAPE is a decision. Flatten a peak with n_g-1, never n_g+1.

**8. Bottleneck**

- TBR: 80 presses vs 9 machines; 38 tyres/press-day vs 338 tyres/machine-day.
- PCR: 88 presses vs 11 machines; 143 tyres/press-day vs 1146 tyres/machine-day.
- => CURING is the capacity constraint (Theory of Constraints: subordinate building to it). BUILDING is the COUPLING constraint -- few machines, so it decides WHEN a press gets fed.

**9. Utilization**

- TBR: a press is active 30.1 days per month.
- PCR: a press is active 30.0 days per month.
- => machine utilisation is an OUTPUT, not a target. The plant idles building ~22% ON PURPOSE because curing is the constraint; a non-bottleneck running faster makes only WIP.

**10. GT Inventory (Little's Law)  [ADDED]**

- PCR: inventory changes +62/day with sd 534 -- it OSCILLATES, it does not climb.
- TBR: inventory changes +11/day with sd 197 -- it OSCILLATES, it does not climb.
- => I = lambda x W with W ~ 9h: the plant holds ~9 HOURS of production as green tyres. The stock IS the lag.
- => the constraint is NO TREND, not a tight band. sd(dI) ~ 530 on a level of ~4,800 -- a days-in-band test would fail the plant itself.

**11. GT Aging  [ADDED]**

- TBR: age p50 4.4h, p95 27.6h, p99 44.0h; 0.11% exceed the 72h shelf life.
- PCR: age p50 4.3h, p95 27.1h, p99 49.6h; 0.36% exceed the 72h shelf life.
- => 72h is a HARD rule (scrap beyond it) and the plant runs an order of magnitude inside it. Any plan breaching it is not a plan.

**12. Scrap / Loss  [ADDED]**

- PCR: 0.609% of green tyres are built and never cured => build/cure target 1.0061.
- TBR: 1.604% of green tyres are built and never cured => build/cure target 1.0160.
- => build/cure - 1 is the LOSS RATE, not drift. Targeting 1.000 under-delivers by exactly this much.

**13. Mould Capacity M_g  [ADDED]**

- TBR: M_g median 4 moulds per GT (max 33), across 56 GTs.
- PCR: M_g median 5 moulds per GT (max 39), across 41 GTs.
- => M_g caps n_g, so it bounds the whole rectangle model. This is a LOWER BOUND -- a mould never mounted in the window is invisible.

**14. Eligibility Churn  [ADDED]**

- => 40-47% of machine-GT and press-GT pairs are NEW every month, carrying 30-37% of volume.
- => history RANKS candidates; capability GATES them. Gating on history starves the plan -- it once left 542 press-days unserved while 25.6% of press-shifts held no mould at all.

**15. Calendar / Downtime  [ADDED]**

- PCR: 31 producing days, p50 12799/day, worst 8738; 0 days below half the median.
- TBR: 31 producing days, p50 3127/day, worst 2057; 14 days below half the median.
- => the plant is NOT 24x7-uniform. Low days are real downtime and must come from a calendar master -- we do not have one.

**16. Size Lock  [ADDED]**

- => a building machine essentially NEVER changes rim size (99.89% PCR / 99.75% TBR).
- => that belongs in the CANDIDATE SET as a hard prefilter, not as a score term -- as a soft term it never reaches the assignment layer.


---

## Snapshot 2 — through 2026-01 (as-of 2026-02-01, 2 months seen)

**1. Machine Preference**

- A GT's top machine carries 100% of its volume (median); a GT uses 1 machines.
- => machine preference is a RANKING signal, strong but not exclusive.

**2. Press Preference**

- A GT spreads over 2 presses (median), top press taking 56%.
- => presses are POOLED per GT, not dedicated.
  - *changed since last snapshot:* 1 measurement(s) moved

**3. Building-Curing Synchronization**

- TBR: build->cure lag p50 4.3h, p95 27.7h; 67% cured in the SAME SHIFT, 93% within a day.
- PCR: build->cure lag p50 4.4h, p95 30.1h; 68% cured in the SAME SHIFT, 92% within a day.
- TBR: corr(built, cured) per GT-day = 0.943, both stages active on 99% of GT-days.
- PCR: corr(built, cured) per GT-day = 0.944, both stages active on 99% of GT-days.
- => the plant builds a GT the SAME DAY it cures it. Build lead is ONE SHIFT, not one day.
  - *changed since last snapshot:* 4 measurement(s) moved

**4. Real Capacity**

- TBR: press does 42 tyres/day (p50), 52 p95, across 80 presses.
- PCR: press does 150 tyres/day (p50), 195 p95, across 92 presses.
- PCR: machine does 1121 tyres/day across 11 machines.
- TBR: machine does 356 tyres/day across 9 machines.
- => rate = 3 x floor(480/eff_CT); dwell time understates it ~3x.
  - *changed since last snapshot:* 4 measurement(s) moved

**5. Changeover**

- TBR: 1,562 building changeovers over 9 machines.
- PCR: 1,076 building changeovers over 10 machines.
- PCR: 310 curing mould changes over 78 presses.
- TBR: 161 curing mould changes over 65 presses.
- => campaign == window, so changeovers = sum_g n_g - |P| in closed form. No search needed.
  - *changed since last snapshot:* 4 measurement(s) moved

**6. SKU Stickiness**

- PCR build: 2.05 SKUs per resource-day; 29.7% of resource-days run a SINGLE SKU.
- TBR build: 2.85 SKUs per resource-day; 18.3% of resource-days run a SINGLE SKU.
- PCR cure: 1.03 SKUs per resource-day; 97.0% of resource-days run a SINGLE SKU.
- TBR cure: 1.01 SKUs per resource-day; 98.8% of resource-days run a SINGLE SKU.
- => a press NEVER changes GT within a day (100% stickiness). Hold the mount for a full day minimum.
  - *changed since last snapshot:* 4 measurement(s) moved

**7. Campaign & Lot Size**

- TBR: a GT is cured on 14 days of the month using 2 presses (mean 2.94).
- PCR: a GT is cured on 13 days of the month using 3 presses (mean 3.93).
- => n_g x D_g = area_g is INVARIANT. Area is fixed by demand; only the SHAPE is a decision. Flatten a peak with n_g-1, never n_g+1.
  - *changed since last snapshot:* 2 measurement(s) moved

**8. Bottleneck**

- PCR: 92 presses vs 11 machines; 132 tyres/press-day vs 1103 tyres/machine-day.
- TBR: 80 presses vs 9 machines; 37 tyres/press-day vs 333 tyres/machine-day.
- => CURING is the capacity constraint (Theory of Constraints: subordinate building to it). BUILDING is the COUPLING constraint -- few machines, so it decides WHEN a press gets fed.
  - *changed since last snapshot:* 2 measurement(s) moved

**9. Utilization**

- PCR: a press is active 29.3 days per month.
- TBR: a press is active 29.4 days per month.
- => machine utilisation is an OUTPUT, not a target. The plant idles building ~22% ON PURPOSE because curing is the constraint; a non-bottleneck running faster makes only WIP.
  - *changed since last snapshot:* 2 measurement(s) moved

**10. GT Inventory (Little's Law)  [ADDED]**

- PCR: inventory changes +45/day with sd 789 -- it OSCILLATES, it does not climb.
- TBR: inventory changes +5/day with sd 176 -- it OSCILLATES, it does not climb.
- => I = lambda x W with W ~ 9h: the plant holds ~9 HOURS of production as green tyres. The stock IS the lag.
- => the constraint is NO TREND, not a tight band. sd(dI) ~ 530 on a level of ~4,800 -- a days-in-band test would fail the plant itself.
  - *changed since last snapshot:* 2 measurement(s) moved

**11. GT Aging  [ADDED]**

- TBR: age p50 4.3h, p95 27.7h, p99 49.4h; 0.22% exceed the 72h shelf life.
- PCR: age p50 4.4h, p95 30.1h, p99 64.0h; 0.70% exceed the 72h shelf life.
- => 72h is a HARD rule (scrap beyond it) and the plant runs an order of magnitude inside it. Any plan breaching it is not a plan.
  - *changed since last snapshot:* 2 measurement(s) moved

**12. Scrap / Loss  [ADDED]**

- PCR: 0.641% of green tyres are built and never cured => build/cure target 1.0064.
- TBR: 1.333% of green tyres are built and never cured => build/cure target 1.0133.
- => build/cure - 1 is the LOSS RATE, not drift. Targeting 1.000 under-delivers by exactly this much.
  - *changed since last snapshot:* 2 measurement(s) moved

**13. Mould Capacity M_g  [ADDED]**

- PCR: M_g median 4 moulds per GT (max 40), across 61 GTs.
- TBR: M_g median 4 moulds per GT (max 34), across 65 GTs.
- => M_g caps n_g, so it bounds the whole rectangle model. This is a LOWER BOUND -- a mould never mounted in the window is invisible.
  - *changed since last snapshot:* 2 measurement(s) moved

**14. Eligibility Churn  [ADDED]**

- => 40-47% of machine-GT and press-GT pairs are NEW every month, carrying 30-37% of volume.
- => history RANKS candidates; capability GATES them. Gating on history starves the plan -- it once left 542 press-days unserved while 25.6% of press-shifts held no mould at all.

**15. Calendar / Downtime  [ADDED]**

- PCR: 61 producing days, p50 12699/day, worst 3068; 0 days below half the median.
- TBR: 61 producing days, p50 3175/day, worst 920; 2 days below half the median.
- => the plant is NOT 24x7-uniform. Low days are real downtime and must come from a calendar master -- we do not have one.
  - *changed since last snapshot:* 2 measurement(s) moved

**16. Size Lock  [ADDED]**

- => a building machine essentially NEVER changes rim size (99.89% PCR / 99.75% TBR).
- => that belongs in the CANDIDATE SET as a hard prefilter, not as a score term -- as a soft term it never reaches the assignment layer.


---

## Snapshot 3 — through 2026-02 (as-of 2026-03-01, 3 months seen)

**1. Machine Preference**

- A GT's top machine carries 100% of its volume (median); a GT uses 1 machines.
- => machine preference is a RANKING signal, strong but not exclusive.

**2. Press Preference**

- A GT spreads over 2 presses (median), top press taking 54%.
- => presses are POOLED per GT, not dedicated.
  - *changed since last snapshot:* 1 measurement(s) moved

**3. Building-Curing Synchronization**

- TBR: build->cure lag p50 4.4h, p95 27.0h; 66% cured in the SAME SHIFT, 93% within a day.
- PCR: build->cure lag p50 4.4h, p95 29.7h; 68% cured in the SAME SHIFT, 92% within a day.
- TBR: corr(built, cured) per GT-day = 0.940, both stages active on 99% of GT-days.
- PCR: corr(built, cured) per GT-day = 0.948, both stages active on 99% of GT-days.
- => the plant builds a GT the SAME DAY it cures it. Build lead is ONE SHIFT, not one day.
  - *changed since last snapshot:* 4 measurement(s) moved

**4. Real Capacity**

- TBR: press does 44 tyres/day (p50), 52 p95, across 80 presses.
- PCR: press does 152 tyres/day (p50), 198 p95, across 94 presses.
- TBR: machine does 361 tyres/day across 9 machines.
- PCR: machine does 1122 tyres/day across 11 machines.
- => rate = 3 x floor(480/eff_CT); dwell time understates it ~3x.
  - *changed since last snapshot:* 4 measurement(s) moved

**5. Changeover**

- PCR: 1,628 building changeovers over 10 machines.
- TBR: 2,497 building changeovers over 9 machines.
- PCR: 479 curing mould changes over 84 presses.
- TBR: 240 curing mould changes over 70 presses.
- => campaign == window, so changeovers = sum_g n_g - |P| in closed form. No search needed.
  - *changed since last snapshot:* 4 measurement(s) moved

**6. SKU Stickiness**

- PCR build: 2.08 SKUs per resource-day; 29.2% of resource-days run a SINGLE SKU.
- TBR build: 3.00 SKUs per resource-day; 16.0% of resource-days run a SINGLE SKU.
- PCR cure: 1.03 SKUs per resource-day; 96.6% of resource-days run a SINGLE SKU.
- TBR cure: 1.01 SKUs per resource-day; 98.8% of resource-days run a SINGLE SKU.
- => a press NEVER changes GT within a day (100% stickiness). Hold the mount for a full day minimum.
  - *changed since last snapshot:* 3 measurement(s) moved

**7. Campaign & Lot Size**

- TBR: a GT is cured on 15 days of the month using 2 presses (mean 2.86).
- PCR: a GT is cured on 11 days of the month using 2 presses (mean 3.65).
- => n_g x D_g = area_g is INVARIANT. Area is fixed by demand; only the SHAPE is a decision. Flatten a peak with n_g-1, never n_g+1.
  - *changed since last snapshot:* 2 measurement(s) moved

**8. Bottleneck**

- PCR: 94 presses vs 11 machines; 131 tyres/press-day vs 1121 tyres/machine-day.
- TBR: 80 presses vs 9 machines; 39 tyres/press-day vs 343 tyres/machine-day.
- => CURING is the capacity constraint (Theory of Constraints: subordinate building to it). BUILDING is the COUPLING constraint -- few machines, so it decides WHEN a press gets fed.
  - *changed since last snapshot:* 2 measurement(s) moved

**9. Utilization**

- TBR: a press is active 28.7 days per month.
- PCR: a press is active 28.6 days per month.
- => machine utilisation is an OUTPUT, not a target. The plant idles building ~22% ON PURPOSE because curing is the constraint; a non-bottleneck running faster makes only WIP.
  - *changed since last snapshot:* 2 measurement(s) moved

**10. GT Inventory (Little's Law)  [ADDED]**

- PCR: inventory changes +21/day with sd 684 -- it OSCILLATES, it does not climb.
- TBR: inventory changes +8/day with sd 155 -- it OSCILLATES, it does not climb.
- => I = lambda x W with W ~ 9h: the plant holds ~9 HOURS of production as green tyres. The stock IS the lag.
- => the constraint is NO TREND, not a tight band. sd(dI) ~ 530 on a level of ~4,800 -- a days-in-band test would fail the plant itself.
  - *changed since last snapshot:* 2 measurement(s) moved

**11. GT Aging  [ADDED]**

- TBR: age p50 4.4h, p95 27.0h, p99 45.7h; 0.18% exceed the 72h shelf life.
- PCR: age p50 4.4h, p95 29.7h, p99 62.1h; 0.66% exceed the 72h shelf life.
- => 72h is a HARD rule (scrap beyond it) and the plant runs an order of magnitude inside it. Any plan breaching it is not a plan.
  - *changed since last snapshot:* 2 measurement(s) moved

**12. Scrap / Loss  [ADDED]**

- PCR: 0.562% of green tyres are built and never cured => build/cure target 1.0056.
- TBR: 1.303% of green tyres are built and never cured => build/cure target 1.0130.
- => build/cure - 1 is the LOSS RATE, not drift. Targeting 1.000 under-delivers by exactly this much.
  - *changed since last snapshot:* 2 measurement(s) moved

**13. Mould Capacity M_g  [ADDED]**

- PCR: M_g median 4 moulds per GT (max 40), across 76 GTs.
- TBR: M_g median 4 moulds per GT (max 34), across 73 GTs.
- => M_g caps n_g, so it bounds the whole rectangle model. This is a LOWER BOUND -- a mould never mounted in the window is invisible.
  - *changed since last snapshot:* 2 measurement(s) moved

**14. Eligibility Churn  [ADDED]**

- => 40-47% of machine-GT and press-GT pairs are NEW every month, carrying 30-37% of volume.
- => history RANKS candidates; capability GATES them. Gating on history starves the plan -- it once left 542 press-days unserved while 25.6% of press-shifts held no mould at all.

**15. Calendar / Downtime  [ADDED]**

- PCR: 89 producing days, p50 12741/day, worst 3068; 0 days below half the median.
- TBR: 89 producing days, p50 3226/day, worst 920; 2 days below half the median.
- => the plant is NOT 24x7-uniform. Low days are real downtime and must come from a calendar master -- we do not have one.
  - *changed since last snapshot:* 2 measurement(s) moved

**16. Size Lock  [ADDED]**

- => a building machine essentially NEVER changes rim size (99.89% PCR / 99.75% TBR).
- => that belongs in the CANDIDATE SET as a hard prefilter, not as a score term -- as a soft term it never reaches the assignment layer.


---

## Snapshot 4 — through 2026-03 (as-of 2026-04-01, 4 months seen)

**1. Machine Preference**

- A GT's top machine carries 100% of its volume (median); a GT uses 1 machines.
- => machine preference is a RANKING signal, strong but not exclusive.

**2. Press Preference**

- A GT spreads over 3 presses (median), top press taking 53%.
- => presses are POOLED per GT, not dedicated.
  - *changed since last snapshot:* 1 measurement(s) moved

**3. Building-Curing Synchronization**

- TBR: build->cure lag p50 4.4h, p95 26.4h; 67% cured in the SAME SHIFT, 94% within a day.
- PCR: build->cure lag p50 4.3h, p95 29.7h; 69% cured in the SAME SHIFT, 92% within a day.
- TBR: corr(built, cured) per GT-day = 0.934, both stages active on 99% of GT-days.
- PCR: corr(built, cured) per GT-day = 0.952, both stages active on 99% of GT-days.
- => the plant builds a GT the SAME DAY it cures it. Build lead is ONE SHIFT, not one day.
  - *changed since last snapshot:* 4 measurement(s) moved

**4. Real Capacity**

- PCR: press does 152 tyres/day (p50), 198 p95, across 96 presses.
- TBR: press does 43 tyres/day (p50), 52 p95, across 80 presses.
- TBR: machine does 358 tyres/day across 9 machines.
- PCR: machine does 1126 tyres/day across 11 machines.
- => rate = 3 x floor(480/eff_CT); dwell time understates it ~3x.
  - *changed since last snapshot:* 4 measurement(s) moved

**5. Changeover**

- PCR: 2,294 building changeovers over 11 machines.
- TBR: 3,392 building changeovers over 9 machines.
- TBR: 340 curing mould changes over 72 presses.
- PCR: 616 curing mould changes over 86 presses.
- => campaign == window, so changeovers = sum_g n_g - |P| in closed form. No search needed.
  - *changed since last snapshot:* 4 measurement(s) moved

**6. SKU Stickiness**

- PCR build: 2.09 SKUs per resource-day; 28.0% of resource-days run a SINGLE SKU.
- TBR build: 2.99 SKUs per resource-day; 13.5% of resource-days run a SINGLE SKU.
- PCR cure: 1.03 SKUs per resource-day; 96.6% of resource-days run a SINGLE SKU.
- TBR cure: 1.01 SKUs per resource-day; 98.6% of resource-days run a SINGLE SKU.
- => a press NEVER changes GT within a day (100% stickiness). Hold the mount for a full day minimum.
  - *changed since last snapshot:* 3 measurement(s) moved

**7. Campaign & Lot Size**

- TBR: a GT is cured on 15 days of the month using 2 presses (mean 2.90).
- PCR: a GT is cured on 10 days of the month using 2 presses (mean 3.50).
- => n_g x D_g = area_g is INVARIANT. Area is fixed by demand; only the SHAPE is a decision. Flatten a peak with n_g-1, never n_g+1.
  - *changed since last snapshot:* 2 measurement(s) moved

**8. Bottleneck**

- PCR: 96 presses vs 11 machines; 129 tyres/press-day vs 1125 tyres/machine-day.
- TBR: 80 presses vs 9 machines; 39 tyres/press-day vs 343 tyres/machine-day.
- => CURING is the capacity constraint (Theory of Constraints: subordinate building to it). BUILDING is the COUPLING constraint -- few machines, so it decides WHEN a press gets fed.
  - *changed since last snapshot:* 1 measurement(s) moved

**9. Utilization**

- TBR: a press is active 29.2 days per month.
- PCR: a press is active 29.0 days per month.
- => machine utilisation is an OUTPUT, not a target. The plant idles building ~22% ON PURPOSE because curing is the constraint; a non-bottleneck running faster makes only WIP.
  - *changed since last snapshot:* 2 measurement(s) moved

**10. GT Inventory (Little's Law)  [ADDED]**

- PCR: inventory changes +18/day with sd 603 -- it OSCILLATES, it does not climb.
- TBR: inventory changes +4/day with sd 151 -- it OSCILLATES, it does not climb.
- => I = lambda x W with W ~ 9h: the plant holds ~9 HOURS of production as green tyres. The stock IS the lag.
- => the constraint is NO TREND, not a tight band. sd(dI) ~ 530 on a level of ~4,800 -- a days-in-band test would fail the plant itself.
  - *changed since last snapshot:* 2 measurement(s) moved

**11. GT Aging  [ADDED]**

- TBR: age p50 4.4h, p95 26.4h, p99 44.4h; 0.16% exceed the 72h shelf life.
- PCR: age p50 4.3h, p95 29.7h, p99 61.9h; 0.67% exceed the 72h shelf life.
- => 72h is a HARD rule (scrap beyond it) and the plant runs an order of magnitude inside it. Any plan breaching it is not a plan.
  - *changed since last snapshot:* 2 measurement(s) moved

**12. Scrap / Loss  [ADDED]**

- PCR: 0.510% of green tyres are built and never cured => build/cure target 1.0051.
- TBR: 1.417% of green tyres are built and never cured => build/cure target 1.0142.
- => build/cure - 1 is the LOSS RATE, not drift. Targeting 1.000 under-delivers by exactly this much.
  - *changed since last snapshot:* 2 measurement(s) moved

**13. Mould Capacity M_g  [ADDED]**

- TBR: M_g median 4 moulds per GT (max 34), across 76 GTs.
- PCR: M_g median 4 moulds per GT (max 40), across 87 GTs.
- => M_g caps n_g, so it bounds the whole rectangle model. This is a LOWER BOUND -- a mould never mounted in the window is invisible.
  - *changed since last snapshot:* 2 measurement(s) moved

**14. Eligibility Churn  [ADDED]**

- => 40-47% of machine-GT and press-GT pairs are NEW every month, carrying 30-37% of volume.
- => history RANKS candidates; capability GATES them. Gating on history starves the plan -- it once left 542 press-days unserved while 25.6% of press-shifts held no mould at all.

**15. Calendar / Downtime  [ADDED]**

- TBR: 120 producing days, p50 3200/day, worst 920; 2 days below half the median.
- PCR: 120 producing days, p50 12702/day, worst 3068; 0 days below half the median.
- => the plant is NOT 24x7-uniform. Low days are real downtime and must come from a calendar master -- we do not have one.
  - *changed since last snapshot:* 2 measurement(s) moved

**16. Size Lock  [ADDED]**

- => a building machine essentially NEVER changes rim size (99.89% PCR / 99.75% TBR).
- => that belongs in the CANDIDATE SET as a hard prefilter, not as a score term -- as a soft term it never reaches the assignment layer.


---

## Snapshot 5 — through 2026-04 (as-of 2026-05-01, 5 months seen)

**1. Machine Preference**

- A GT's top machine carries 100% of its volume (median); a GT uses 1 machines.
- => machine preference is a RANKING signal, strong but not exclusive.

**2. Press Preference**

- A GT spreads over 3 presses (median), top press taking 53%.
- => presses are POOLED per GT, not dedicated.

**3. Building-Curing Synchronization**

- TBR: build->cure lag p50 4.5h, p95 26.7h; 66% cured in the SAME SHIFT, 93% within a day.
- PCR: build->cure lag p50 4.3h, p95 29.4h; 69% cured in the SAME SHIFT, 93% within a day.
- TBR: corr(built, cured) per GT-day = 0.924, both stages active on 99% of GT-days.
- PCR: corr(built, cured) per GT-day = 0.952, both stages active on 99% of GT-days.
- => the plant builds a GT the SAME DAY it cures it. Build lead is ONE SHIFT, not one day.
  - *changed since last snapshot:* 3 measurement(s) moved

**4. Real Capacity**

- TBR: press does 42 tyres/day (p50), 51 p95, across 81 presses.
- PCR: press does 150 tyres/day (p50), 198 p95, across 99 presses.
- TBR: machine does 352 tyres/day across 9 machines.
- PCR: machine does 1116 tyres/day across 11 machines.
- => rate = 3 x floor(480/eff_CT); dwell time understates it ~3x.
  - *changed since last snapshot:* 4 measurement(s) moved

**5. Changeover**

- TBR: 4,225 building changeovers over 9 machines.
- PCR: 2,888 building changeovers over 11 machines.
- TBR: 419 curing mould changes over 78 presses.
- PCR: 759 curing mould changes over 86 presses.
- => campaign == window, so changeovers = sum_g n_g - |P| in closed form. No search needed.
  - *changed since last snapshot:* 4 measurement(s) moved

**6. SKU Stickiness**

- PCR build: 2.09 SKUs per resource-day; 27.6% of resource-days run a SINGLE SKU.
- TBR build: 2.98 SKUs per resource-day; 11.1% of resource-days run a SINGLE SKU.
- PCR cure: 1.03 SKUs per resource-day; 96.7% of resource-days run a SINGLE SKU.
- TBR cure: 1.01 SKUs per resource-day; 98.7% of resource-days run a SINGLE SKU.
- => a press NEVER changes GT within a day (100% stickiness). Hold the mount for a full day minimum.
  - *changed since last snapshot:* 4 measurement(s) moved

**7. Campaign & Lot Size**

- TBR: a GT is cured on 15 days of the month using 2 presses (mean 2.93).
- PCR: a GT is cured on 10 days of the month using 2 presses (mean 3.50).
- => n_g x D_g = area_g is INVARIANT. Area is fixed by demand; only the SHAPE is a decision. Flatten a peak with n_g-1, never n_g+1.
  - *changed since last snapshot:* 1 measurement(s) moved

**8. Bottleneck**

- TBR: 81 presses vs 9 machines; 37 tyres/press-day vs 337 tyres/machine-day.
- PCR: 99 presses vs 11 machines; 123 tyres/press-day vs 1111 tyres/machine-day.
- => CURING is the capacity constraint (Theory of Constraints: subordinate building to it). BUILDING is the COUPLING constraint -- few machines, so it decides WHEN a press gets fed.
  - *changed since last snapshot:* 2 measurement(s) moved

**9. Utilization**

- PCR: a press is active 28.9 days per month.
- TBR: a press is active 29.2 days per month.
- => machine utilisation is an OUTPUT, not a target. The plant idles building ~22% ON PURPOSE because curing is the constraint; a non-bottleneck running faster makes only WIP.
  - *changed since last snapshot:* 1 measurement(s) moved

**10. GT Inventory (Little's Law)  [ADDED]**

- PCR: inventory changes +15/day with sd 587 -- it OSCILLATES, it does not climb.
- TBR: inventory changes +3/day with sd 147 -- it OSCILLATES, it does not climb.
- => I = lambda x W with W ~ 9h: the plant holds ~9 HOURS of production as green tyres. The stock IS the lag.
- => the constraint is NO TREND, not a tight band. sd(dI) ~ 530 on a level of ~4,800 -- a days-in-band test would fail the plant itself.
  - *changed since last snapshot:* 2 measurement(s) moved

**11. GT Aging  [ADDED]**

- TBR: age p50 4.5h, p95 26.7h, p99 44.8h; 0.15% exceed the 72h shelf life.
- PCR: age p50 4.3h, p95 29.4h, p99 61.3h; 0.66% exceed the 72h shelf life.
- => 72h is a HARD rule (scrap beyond it) and the plant runs an order of magnitude inside it. Any plan breaching it is not a plan.
  - *changed since last snapshot:* 2 measurement(s) moved

**12. Scrap / Loss  [ADDED]**

- PCR: 0.492% of green tyres are built and never cured => build/cure target 1.0049.
- TBR: 1.645% of green tyres are built and never cured => build/cure target 1.0165.
- => build/cure - 1 is the LOSS RATE, not drift. Targeting 1.000 under-delivers by exactly this much.
  - *changed since last snapshot:* 2 measurement(s) moved

**13. Mould Capacity M_g  [ADDED]**

- TBR: M_g median 4 moulds per GT (max 34), across 79 GTs.
- PCR: M_g median 4 moulds per GT (max 40), across 97 GTs.
- => M_g caps n_g, so it bounds the whole rectangle model. This is a LOWER BOUND -- a mould never mounted in the window is invisible.
  - *changed since last snapshot:* 2 measurement(s) moved

**14. Eligibility Churn  [ADDED]**

- => 40-47% of machine-GT and press-GT pairs are NEW every month, carrying 30-37% of volume.
- => history RANKS candidates; capability GATES them. Gating on history starves the plan -- it once left 542 press-days unserved while 25.6% of press-shifts held no mould at all.

**15. Calendar / Downtime  [ADDED]**

- PCR: 150 producing days, p50 12639/day, worst 3068; 0 days below half the median.
- TBR: 150 producing days, p50 3129/day, worst 884; 3 days below half the median.
- => the plant is NOT 24x7-uniform. Low days are real downtime and must come from a calendar master -- we do not have one.
  - *changed since last snapshot:* 2 measurement(s) moved

**16. Size Lock  [ADDED]**

- => a building machine essentially NEVER changes rim size (99.89% PCR / 99.75% TBR).
- => that belongs in the CANDIDATE SET as a hard prefilter, not as a score term -- as a soft term it never reaches the assignment layer.


---

## Snapshot 6 — through 2026-05 (as-of 2026-06-01, 6 months seen)

**1. Machine Preference**

- A GT's top machine carries 100% of its volume (median); a GT uses 1 machines.
- => machine preference is a RANKING signal, strong but not exclusive.

**2. Press Preference**

- A GT spreads over 3 presses (median), top press taking 50%.
- => presses are POOLED per GT, not dedicated.
  - *changed since last snapshot:* 1 measurement(s) moved

**3. Building-Curing Synchronization**

- TBR: build->cure lag p50 4.7h, p95 26.7h; 65% cured in the SAME SHIFT, 93% within a day.
- PCR: build->cure lag p50 4.2h, p95 28.8h; 70% cured in the SAME SHIFT, 93% within a day.
- TBR: corr(built, cured) per GT-day = 0.921, both stages active on 99% of GT-days.
- PCR: corr(built, cured) per GT-day = 0.954, both stages active on 99% of GT-days.
- => the plant builds a GT the SAME DAY it cures it. Build lead is ONE SHIFT, not one day.
  - *changed since last snapshot:* 4 measurement(s) moved

**4. Real Capacity**

- PCR: press does 150 tyres/day (p50), 198 p95, across 102 presses.
- TBR: press does 42 tyres/day (p50), 50 p95, across 82 presses.
- TBR: machine does 351 tyres/day across 9 machines.
- PCR: machine does 1103 tyres/day across 11 machines.
- => rate = 3 x floor(480/eff_CT); dwell time understates it ~3x.
  - *changed since last snapshot:* 4 measurement(s) moved

**5. Changeover**

- TBR: 5,101 building changeovers over 9 machines.
- PCR: 3,482 building changeovers over 11 machines.
- PCR: 873 curing mould changes over 89 presses.
- TBR: 505 curing mould changes over 79 presses.
- => campaign == window, so changeovers = sum_g n_g - |P| in closed form. No search needed.
  - *changed since last snapshot:* 4 measurement(s) moved

**6. SKU Stickiness**

- PCR build: 2.09 SKUs per resource-day; 26.9% of resource-days run a SINGLE SKU.
- TBR build: 2.99 SKUs per resource-day; 10.8% of resource-days run a SINGLE SKU.
- PCR cure: 1.03 SKUs per resource-day; 96.9% of resource-days run a SINGLE SKU.
- TBR cure: 1.01 SKUs per resource-day; 98.6% of resource-days run a SINGLE SKU.
- => a press NEVER changes GT within a day (100% stickiness). Hold the mount for a full day minimum.
  - *changed since last snapshot:* 4 measurement(s) moved

**7. Campaign & Lot Size**

- PCR: a GT is cured on 11 days of the month using 2 presses (mean 3.56).
- TBR: a GT is cured on 15 days of the month using 2 presses (mean 2.93).
- => n_g x D_g = area_g is INVARIANT. Area is fixed by demand; only the SHAPE is a decision. Flatten a peak with n_g-1, never n_g+1.
  - *changed since last snapshot:* 1 measurement(s) moved

**8. Bottleneck**

- TBR: 82 presses vs 9 machines; 37 tyres/press-day vs 337 tyres/machine-day.
- PCR: 102 presses vs 11 machines; 119 tyres/press-day vs 1102 tyres/machine-day.
- => CURING is the capacity constraint (Theory of Constraints: subordinate building to it). BUILDING is the COUPLING constraint -- few machines, so it decides WHEN a press gets fed.
  - *changed since last snapshot:* 2 measurement(s) moved

**9. Utilization**

- TBR: a press is active 29.3 days per month.
- PCR: a press is active 29.0 days per month.
- => machine utilisation is an OUTPUT, not a target. The plant idles building ~22% ON PURPOSE because curing is the constraint; a non-bottleneck running faster makes only WIP.
  - *changed since last snapshot:* 2 measurement(s) moved

**10. GT Inventory (Little's Law)  [ADDED]**

- PCR: inventory changes +13/day with sd 591 -- it OSCILLATES, it does not climb.
- TBR: inventory changes +4/day with sd 143 -- it OSCILLATES, it does not climb.
- => I = lambda x W with W ~ 9h: the plant holds ~9 HOURS of production as green tyres. The stock IS the lag.
- => the constraint is NO TREND, not a tight band. sd(dI) ~ 530 on a level of ~4,800 -- a days-in-band test would fail the plant itself.
  - *changed since last snapshot:* 2 measurement(s) moved

**11. GT Aging  [ADDED]**

- TBR: age p50 4.7h, p95 26.7h, p99 44.4h; 0.14% exceed the 72h shelf life.
- PCR: age p50 4.2h, p95 28.8h, p99 60.5h; 0.65% exceed the 72h shelf life.
- => 72h is a HARD rule (scrap beyond it) and the plant runs an order of magnitude inside it. Any plan breaching it is not a plan.
  - *changed since last snapshot:* 2 measurement(s) moved

**12. Scrap / Loss  [ADDED]**

- PCR: 0.473% of green tyres are built and never cured => build/cure target 1.0047.
- TBR: 1.888% of green tyres are built and never cured => build/cure target 1.0189.
- => build/cure - 1 is the LOSS RATE, not drift. Targeting 1.000 under-delivers by exactly this much.
  - *changed since last snapshot:* 2 measurement(s) moved

**13. Mould Capacity M_g  [ADDED]**

- PCR: M_g median 4 moulds per GT (max 40), across 100 GTs.
- TBR: M_g median 4 moulds per GT (max 35), across 80 GTs.
- => M_g caps n_g, so it bounds the whole rectangle model. This is a LOWER BOUND -- a mould never mounted in the window is invisible.
  - *changed since last snapshot:* 2 measurement(s) moved

**14. Eligibility Churn  [ADDED]**

- => 40-47% of machine-GT and press-GT pairs are NEW every month, carrying 30-37% of volume.
- => history RANKS candidates; capability GATES them. Gating on history starves the plan -- it once left 542 press-days unserved while 25.6% of press-shifts held no mould at all.

**15. Calendar / Downtime  [ADDED]**

- PCR: 181 producing days, p50 12519/day, worst 3068; 0 days below half the median.
- TBR: 181 producing days, p50 3125/day, worst 884; 4 days below half the median.
- => the plant is NOT 24x7-uniform. Low days are real downtime and must come from a calendar master -- we do not have one.
  - *changed since last snapshot:* 2 measurement(s) moved

**16. Size Lock  [ADDED]**

- => a building machine essentially NEVER changes rim size (99.89% PCR / 99.75% TBR).
- => that belongs in the CANDIDATE SET as a hard prefilter, not as a score term -- as a soft term it never reaches the assignment layer.


---

## Snapshot 7 — through 2026-06 (as-of 2026-07-01, 7 months seen)

**1. Machine Preference**

- A GT's top machine carries 100% of its volume (median); a GT uses 1 machines.
- => machine preference is a RANKING signal, strong but not exclusive.

**2. Press Preference**

- A GT spreads over 3 presses (median), top press taking 50%.
- => presses are POOLED per GT, not dedicated.

**3. Building-Curing Synchronization**

- TBR: build->cure lag p50 4.8h, p95 26.9h; 65% cured in the SAME SHIFT, 93% within a day.
- PCR: build->cure lag p50 4.2h, p95 28.7h; 69% cured in the SAME SHIFT, 93% within a day.
- PCR: corr(built, cured) per GT-day = 0.953, both stages active on 99% of GT-days.
- TBR: corr(built, cured) per GT-day = 0.917, both stages active on 99% of GT-days.
- => the plant builds a GT the SAME DAY it cures it. Build lead is ONE SHIFT, not one day.
  - *changed since last snapshot:* 4 measurement(s) moved

**4. Real Capacity**

- PCR: press does 150 tyres/day (p50), 198 p95, across 106 presses.
- TBR: press does 42 tyres/day (p50), 50 p95, across 84 presses.
- TBR: machine does 350 tyres/day across 9 machines.
- PCR: machine does 1108 tyres/day across 11 machines.
- => rate = 3 x floor(480/eff_CT); dwell time understates it ~3x.
  - *changed since last snapshot:* 4 measurement(s) moved

**5. Changeover**

- TBR: 5,913 building changeovers over 9 machines.
- PCR: 4,137 building changeovers over 11 machines.
- PCR: 1,009 curing mould changes over 89 presses.
- TBR: 597 curing mould changes over 80 presses.
- => campaign == window, so changeovers = sum_g n_g - |P| in closed form. No search needed.
  - *changed since last snapshot:* 4 measurement(s) moved

**6. SKU Stickiness**

- PCR build: 2.11 SKUs per resource-day; 26.8% of resource-days run a SINGLE SKU.
- TBR build: 2.99 SKUs per resource-day; 11.2% of resource-days run a SINGLE SKU.
- PCR cure: 1.03 SKUs per resource-day; 96.9% of resource-days run a SINGLE SKU.
- TBR cure: 1.01 SKUs per resource-day; 98.6% of resource-days run a SINGLE SKU.
- => a press NEVER changes GT within a day (100% stickiness). Hold the mount for a full day minimum.
  - *changed since last snapshot:* 2 measurement(s) moved

**7. Campaign & Lot Size**

- TBR: a GT is cured on 15 days of the month using 2 presses (mean 2.97).
- PCR: a GT is cured on 11 days of the month using 2 presses (mean 3.66).
- => n_g x D_g = area_g is INVARIANT. Area is fixed by demand; only the SHAPE is a decision. Flatten a peak with n_g-1, never n_g+1.
  - *changed since last snapshot:* 2 measurement(s) moved

**8. Bottleneck**

- TBR: 84 presses vs 9 machines; 36 tyres/press-day vs 337 tyres/machine-day.
- PCR: 106 presses vs 11 machines; 115 tyres/press-day vs 1111 tyres/machine-day.
- => CURING is the capacity constraint (Theory of Constraints: subordinate building to it). BUILDING is the COUPLING constraint -- few machines, so it decides WHEN a press gets fed.
  - *changed since last snapshot:* 2 measurement(s) moved

**9. Utilization**

- TBR: a press is active 29.1 days per month.
- PCR: a press is active 28.9 days per month.
- => machine utilisation is an OUTPUT, not a target. The plant idles building ~22% ON PURPOSE because curing is the constraint; a non-bottleneck running faster makes only WIP.
  - *changed since last snapshot:* 2 measurement(s) moved

**10. GT Inventory (Little's Law)  [ADDED]**

- PCR: inventory changes +12/day with sd 563 -- it OSCILLATES, it does not climb.
- TBR: inventory changes +2/day with sd 146 -- it OSCILLATES, it does not climb.
- => I = lambda x W with W ~ 9h: the plant holds ~9 HOURS of production as green tyres. The stock IS the lag.
- => the constraint is NO TREND, not a tight band. sd(dI) ~ 530 on a level of ~4,800 -- a days-in-band test would fail the plant itself.
  - *changed since last snapshot:* 2 measurement(s) moved

**11. GT Aging  [ADDED]**

- TBR: age p50 4.8h, p95 26.9h, p99 44.2h; 0.14% exceed the 72h shelf life.
- PCR: age p50 4.2h, p95 28.7h, p99 59.9h; 0.64% exceed the 72h shelf life.
- => 72h is a HARD rule (scrap beyond it) and the plant runs an order of magnitude inside it. Any plan breaching it is not a plan.
  - *changed since last snapshot:* 2 measurement(s) moved

**12. Scrap / Loss  [ADDED]**

- PCR: 0.479% of green tyres are built and never cured => build/cure target 1.0048.
- TBR: 1.990% of green tyres are built and never cured => build/cure target 1.0199.
- => build/cure - 1 is the LOSS RATE, not drift. Targeting 1.000 under-delivers by exactly this much.
  - *changed since last snapshot:* 2 measurement(s) moved

**13. Mould Capacity M_g  [ADDED]**

- PCR: M_g median 4 moulds per GT (max 42), across 103 GTs.
- TBR: M_g median 5 moulds per GT (max 36), across 82 GTs.
- => M_g caps n_g, so it bounds the whole rectangle model. This is a LOWER BOUND -- a mould never mounted in the window is invisible.
  - *changed since last snapshot:* 2 measurement(s) moved

**14. Eligibility Churn  [ADDED]**

- => 40-47% of machine-GT and press-GT pairs are NEW every month, carrying 30-37% of volume.
- => history RANKS candidates; capability GATES them. Gating on history starves the plan -- it once left 542 press-days unserved while 25.6% of press-shifts held no mould at all.

**15. Calendar / Downtime  [ADDED]**

- PCR: 211 producing days, p50 12578/day, worst 3068; 0 days below half the median.
- TBR: 211 producing days, p50 3117/day, worst 884; 4 days below half the median.
- => the plant is NOT 24x7-uniform. Low days are real downtime and must come from a calendar master -- we do not have one.
  - *changed since last snapshot:* 2 measurement(s) moved

**16. Size Lock  [ADDED]**

- => a building machine essentially NEVER changes rim size (99.89% PCR / 99.75% TBR).
- => that belongs in the CANDIDATE SET as a hard prefilter, not as a score term -- as a soft term it never reaches the assignment layer.


---

## Snapshot 8 — through 2026-07 (as-of 2026-08-01, 8 months seen)

**1. Machine Preference**

- A GT's top machine carries 100% of its volume (median); a GT uses 2 machines.
- => machine preference is a RANKING signal, strong but not exclusive.
  - *changed since last snapshot:* 1 measurement(s) moved

**2. Press Preference**

- A GT spreads over 4 presses (median), top press taking 46%.
- => presses are POOLED per GT, not dedicated.
  - *changed since last snapshot:* 1 measurement(s) moved

**3. Building-Curing Synchronization**

- TBR: build->cure lag p50 4.8h, p95 26.7h; 65% cured in the SAME SHIFT, 93% within a day.
- PCR: build->cure lag p50 4.3h, p95 28.8h; 69% cured in the SAME SHIFT, 93% within a day.
- TBR: corr(built, cured) per GT-day = 0.918, both stages active on 99% of GT-days.
- PCR: corr(built, cured) per GT-day = 0.950, both stages active on 99% of GT-days.
- => the plant builds a GT the SAME DAY it cures it. Build lead is ONE SHIFT, not one day.
  - *changed since last snapshot:* 4 measurement(s) moved

**4. Real Capacity**

- TBR: press does 42 tyres/day (p50), 50 p95, across 84 presses.
- PCR: press does 152 tyres/day (p50), 198 p95, across 112 presses.
- TBR: machine does 352 tyres/day across 9 machines.
- PCR: machine does 1108 tyres/day across 11 machines.
- => rate = 3 x floor(480/eff_CT); dwell time understates it ~3x.
  - *changed since last snapshot:* 2 measurement(s) moved

**5. Changeover**

- TBR: 6,800 building changeovers over 9 machines.
- PCR: 4,876 building changeovers over 11 machines.
- TBR: 691 curing mould changes over 80 presses.
- PCR: 1,133 curing mould changes over 91 presses.
- => campaign == window, so changeovers = sum_g n_g - |P| in closed form. No search needed.
  - *changed since last snapshot:* 4 measurement(s) moved

**6. SKU Stickiness**

- PCR build: 2.14 SKUs per resource-day; 25.8% of resource-days run a SINGLE SKU.
- TBR build: 3.00 SKUs per resource-day; 11.2% of resource-days run a SINGLE SKU.
- PCR cure: 1.03 SKUs per resource-day; 97.0% of resource-days run a SINGLE SKU.
- TBR cure: 1.01 SKUs per resource-day; 98.7% of resource-days run a SINGLE SKU.
- => a press NEVER changes GT within a day (100% stickiness). Hold the mount for a full day minimum.
  - *changed since last snapshot:* 4 measurement(s) moved

**7. Campaign & Lot Size**

- PCR: a GT is cured on 12 days of the month using 2 presses (mean 3.68).
- TBR: a GT is cured on 15 days of the month using 2 presses (mean 2.99).
- => n_g x D_g = area_g is INVARIANT. Area is fixed by demand; only the SHAPE is a decision. Flatten a peak with n_g-1, never n_g+1.
  - *changed since last snapshot:* 2 measurement(s) moved

**8. Bottleneck**

- PCR: 112 presses vs 11 machines; 110 tyres/press-day vs 1118 tyres/machine-day.
- TBR: 84 presses vs 9 machines; 36 tyres/press-day vs 339 tyres/machine-day.
- => CURING is the capacity constraint (Theory of Constraints: subordinate building to it). BUILDING is the COUPLING constraint -- few machines, so it decides WHEN a press gets fed.
  - *changed since last snapshot:* 2 measurement(s) moved

**9. Utilization**

- PCR: a press is active 28.8 days per month.
- TBR: a press is active 29.3 days per month.
- => machine utilisation is an OUTPUT, not a target. The plant idles building ~22% ON PURPOSE because curing is the constraint; a non-bottleneck running faster makes only WIP.
  - *changed since last snapshot:* 2 measurement(s) moved

**10. GT Inventory (Little's Law)  [ADDED]**

- PCR: inventory changes -11/day with sd 589 -- it OSCILLATES, it does not climb.
- TBR: inventory changes -2/day with sd 163 -- it OSCILLATES, it does not climb.
- => I = lambda x W with W ~ 9h: the plant holds ~9 HOURS of production as green tyres. The stock IS the lag.
- => the constraint is NO TREND, not a tight band. sd(dI) ~ 530 on a level of ~4,800 -- a days-in-band test would fail the plant itself.
  - *changed since last snapshot:* 2 measurement(s) moved

**11. GT Aging  [ADDED]**

- TBR: age p50 4.8h, p95 26.7h, p99 43.6h; 0.13% exceed the 72h shelf life.
- PCR: age p50 4.3h, p95 28.8h, p99 59.3h; 0.63% exceed the 72h shelf life.
- => 72h is a HARD rule (scrap beyond it) and the plant runs an order of magnitude inside it. Any plan breaching it is not a plan.
  - *changed since last snapshot:* 2 measurement(s) moved

**12. Scrap / Loss  [ADDED]**

- PCR: 0.463% of green tyres are built and never cured => build/cure target 1.0046.
- TBR: 1.971% of green tyres are built and never cured => build/cure target 1.0197.
- => build/cure - 1 is the LOSS RATE, not drift. Targeting 1.000 under-delivers by exactly this much.
  - *changed since last snapshot:* 2 measurement(s) moved

**13. Mould Capacity M_g  [ADDED]**

- PCR: M_g median 4 moulds per GT (max 45), across 107 GTs.
- TBR: M_g median 5 moulds per GT (max 38), across 83 GTs.
- => M_g caps n_g, so it bounds the whole rectangle model. This is a LOWER BOUND -- a mould never mounted in the window is invisible.
  - *changed since last snapshot:* 2 measurement(s) moved

**14. Eligibility Churn  [ADDED]**

- => 40-47% of machine-GT and press-GT pairs are NEW every month, carrying 30-37% of volume.
- => history RANKS candidates; capability GATES them. Gating on history starves the plan -- it once left 542 press-days unserved while 25.6% of press-shifts held no mould at all.

**15. Calendar / Downtime  [ADDED]**

- TBR: 242 producing days, p50 3145/day, worst 884; 4 days below half the median.
- PCR: 242 producing days, p50 12642/day, worst 3068; 0 days below half the median.
- => the plant is NOT 24x7-uniform. Low days are real downtime and must come from a calendar master -- we do not have one.
  - *changed since last snapshot:* 2 measurement(s) moved

**16. Size Lock  [ADDED]**

- => a building machine essentially NEVER changes rim size (99.89% PCR / 99.75% TBR).
- => that belongs in the CANDIDATE SET as a hard prefilter, not as a score term -- as a soft term it never reaches the assignment layer.


---

## What is still MISSING and cannot be mined

| Gap | Consequence today |
|---|---|
| Press platen master (rim range per press) | eligibility is history-derived; press matrix lists 114 PCR presses vs ~87 real |
| Machine certification list | median GT shows only 2 eligible machines — the engine must override it to plan at all |
| True mould count `M_g` | we infer a LOWER bound; `M_g` caps `n_g`, so the rectangle model rests on it |
| Plant calendar / planned downtime | 24×7 assumed; real low-production days are invisible |
| Customer demand file | ours is derived from the same month's output, so planning that month is in-sample |
| Bladder / PM / breakdown log | no downtime model, so robustness cannot be tested |