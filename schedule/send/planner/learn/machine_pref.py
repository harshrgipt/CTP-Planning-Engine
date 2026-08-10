"""Phase 1b: Machine Preference Matrix (MPM).

MPM[sku, machine] = P(machine | sku, normal quality). Bootstrap 95% CI via
Wilson score interval (fast, closed-form) plus a chi-square non-uniformity
test per SKU. Emits `sku_machine.preferred` candidate rules when
p >= mpm_preferred_p AND ci_low >= mpm_preferred_ci_low.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
from scipy.stats import chisquare  # type: ignore

from planner.config import CONFIG
from planner.data.warehouse import duck
from planner.kb.promoter import wilson_ci
from planner.kb.rule_types import Rule, RuleType
from planner.runs.logger import log


def build_mpm(out_dir: Path) -> tuple[Path, list[Rule]]:
    con = duck()
    # Only "normal" runs count toward preference — bad quality events don't set MPM policy.
    df = con.execute(
        """
        SELECT plant, stage, itemCode AS sku, machineCode AS machine, count(*) AS n
        FROM v_build
        WHERE QualityStatus = '1'
        GROUP BY 1,2,3,4
        """
    ).pl()
    if df.height == 0:
        log.warning("mpm.empty")
        return Path(), []

    totals = df.group_by(["plant", "stage", "sku"]).agg(pl.col("n").sum().alias("sku_total"))
    mpm = df.join(totals, on=["plant", "stage", "sku"])
    mpm = mpm.with_columns(
        (pl.col("n") / pl.col("sku_total")).alias("p"),
    )
    # Wilson CI per row (vectorized via python row apply — sub-second on ~10k rows)
    cis = mpm.select(["n", "sku_total"]).to_numpy()
    lows, highs = [], []
    for k, tot in cis:
        lo, hi = wilson_ci(int(k), int(tot))
        lows.append(lo); highs.append(hi)
    mpm = mpm.with_columns(pl.Series("ci_low", lows), pl.Series("ci_high", highs))

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "mpm.parquet"
    mpm.write_parquet(out, compression="zstd")

    # Chi-square p-value per (plant, stage, sku): tests non-uniformity across machines.
    p_rows = []
    for (plant, stage, sku), g in mpm.group_by(["plant", "stage", "sku"], maintain_order=False):
        obs = g["n"].to_list()
        if len(obs) >= 2 and sum(obs) >= 10:
            try:
                _, p = chisquare(obs)
            except Exception:
                p = 1.0
        else:
            p = 1.0
        p_rows.append({"plant": plant, "stage": stage, "sku": sku, "chi2_p": float(p)})
    p_df = pl.DataFrame(p_rows)
    mpm = mpm.join(p_df, on=["plant", "stage", "sku"], how="left")
    mpm.write_parquet(out, compression="zstd")
    log.info("mpm.written", rows=mpm.height, path=str(out))

    # Emit rule candidates
    th = CONFIG.thresholds
    rules: list[Rule] = []
    cand = mpm.filter((pl.col("p") >= th.mpm_preferred_p) & (pl.col("ci_low") >= th.mpm_preferred_ci_low))
    for row in cand.iter_rows(named=True):
        rid = f"mpm.{row['plant']}.s{row['stage']}.{row['sku']}.{row['machine']}"
        rules.append(Rule(
            rule_id=rid,
            scope="sku_machine",
            statement={"predicate": "prefer_machine", "params": {
                "plant": row["plant"], "stage": row["stage"], "sku": row["sku"], "machine": row["machine"]}},
            support=int(row["n"]),
            confidence=float(row["p"]),
            exception_rate=1.0 - float(row["p"]),
            sample_size=int(row["sku_total"]),
            ci_low=float(row["ci_low"]),
            ci_high=float(row["ci_high"]),
            p_value=float(row.get("chi2_p", 1.0)),
            type=RuleType.STAT,
            provenance={"miner": "machine_pref", "method": "wilson+chisquare"},
        ))
    log.info("mpm.candidates", n=len(rules))
    return out, rules
