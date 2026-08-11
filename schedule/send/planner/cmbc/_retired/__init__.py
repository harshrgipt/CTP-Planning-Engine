"""Layers removed from the pipeline because NOTHING reads their output.

    l6_build_gate      L7 never read l6_infeasible/l6_build_load; it runs its
                       own B16 gate. Report only.
    l8_prep_explosion  zero readers except scripts/export_cmbc_xlsx.py, which
                       is not the BTP path.
    l12_explain        never wired into an arm.
    _diag_*            diagnostics kept for reference.

    l9_optimise        REMOVED 2026-08-11 AFTER MEASUREMENT, not on suspicion.
                       Wired correctly as L5 -> L9 -> L7 -> L10 -> L11 and run
                       on 2026-08 with a 300 s budget:

                         searched 1,428 candidates, accepted 0 moves in 300 s
                         >>> NO IMPROVEMENT FOUND. Plan left unchanged.

                       All nine cost tiers ended exactly where they started;
                       cure_campaigns.parquet came out byte-identical to L5's
                       own output; 0 of 40 L11 invariants moved; built volume
                       unchanged on both plants (PCR 406,294 / TBR 96,885).
                       The search is LEXICOGRAPHIC -- a move that worsens a
                       higher tier is rejected however much it gains below --
                       and tier 1 (demand short = 7,409) blocks everything.
                       Re-measure here if the baseline moves; ledger lesson 5
                       is that rejected experiments can become worth points.

    l1_validate        Its three outputs (cost_table, rule_table, l1_findings)
                       had exactly one consumer between them: L9 read
                       cost_table. With L9 retired it has none. It also needs
                       raw MES. Superseded by l1_preflight.

These still import and run; they are simply not on the path from inputs to the
BTP pack. Verified: dropping L6 and L8 leaves every plan artefact bit-identical
and the BTP pack matching the shipped one on all 34 sheets.
"""
