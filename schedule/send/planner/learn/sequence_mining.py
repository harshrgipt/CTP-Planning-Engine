"""Phase 1d: mine SKU-order patterns per (machine, day) using PrefixSpan."""
from __future__ import annotations

from pathlib import Path

from prefixspan import PrefixSpan  # type: ignore

from planner.config import CONFIG
from planner.data.warehouse import duck
from planner.kb.rule_types import Rule, RuleType
from planner.runs.logger import log


def mine_sequences(out_dir: Path) -> tuple[Path, list[Rule]]:
    con = duck()
    # Build sequences: per (machine, date) list of distinct-item transitions in order.
    df = con.execute("""
        WITH ordered AS (
            SELECT plant, stage, machineCode AS machine, date, event_ts, itemCode,
                   lag(itemCode) OVER (PARTITION BY machineCode, date ORDER BY event_ts) AS prev_item
            FROM v_build
            WHERE QualityStatus = '1'
        ),
        transitions AS (
            SELECT plant, stage, machine, date, itemCode
            FROM ordered
            WHERE prev_item IS NULL OR prev_item <> itemCode
        )
        SELECT plant, stage, machine, date, list(itemCode ORDER BY random()) AS seq
        FROM transitions
        GROUP BY 1,2,3,4
        HAVING length(list(itemCode)) >= 2
    """).pl()
    if df.height == 0:
        log.warning("seq.empty")
        return Path(), []

    rules: list[Rule] = []
    for (plant, stage), g in df.group_by(["plant", "stage"]):
        seqs = [row["seq"] for row in g.iter_rows(named=True)]
        min_support_days = max(2, int(len(seqs) * CONFIG.thresholds.seq_min_support_frac))
        ps = PrefixSpan(seqs)
        # Cap the alphabet by ignoring patterns of length > max
        patterns = ps.frequent(min_support_days, closed=True)
        for count, pat in patterns:
            if len(pat) < 2 or len(pat) > CONFIG.thresholds.seq_max_pattern_len:
                continue
            support = count
            confidence = count / len(seqs)
            rid = f"seq.{plant}.s{stage}.{'->'.join(pat)}"
            rules.append(Rule(
                rule_id=rid[:250], scope="sequence",
                statement={"predicate": "prefer_sequence", "params": {
                    "plant": plant, "stage": stage, "pattern": pat, "n_seqs": len(seqs)}},
                support=int(support), sample_size=int(len(seqs)),
                confidence=float(confidence),
                exception_rate=1.0 - float(confidence),
                ci_low=float(confidence),
                ci_high=float(confidence),
                p_value=0.0,
                type=RuleType.STAT,
                provenance={"miner": "prefixspan", "min_support_days": min_support_days},
            ))

    import json
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "sequences.json"
    out.write_text(json.dumps([r.model_dump(mode="json") for r in rules], indent=2, default=str))
    log.info("seq.rules", n=len(rules))
    return out, rules
