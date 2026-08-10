"""Fit truncated-lognormal distributions to observed cycle/setup times."""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

import numpy as np
import polars as pl

from planner.runs.logger import log


@dataclass
class LogNormal:
    mu: float
    sigma: float
    lo: float
    hi: float

    def sample(self, rng: np.random.Generator, n: int = 1) -> np.ndarray:
        # Rejection-truncated lognormal.
        out = np.empty(n)
        i = 0
        while i < n:
            batch = rng.lognormal(mean=self.mu, sigma=self.sigma, size=n - i)
            batch = batch[(batch >= self.lo) & (batch <= self.hi)]
            k = min(len(batch), n - i)
            out[i:i + k] = batch[:k]
            i += k
        return out

    def to_dict(self) -> dict:
        return {"mu": self.mu, "sigma": self.sigma, "lo": self.lo, "hi": self.hi}


def _fit(values: np.ndarray) -> LogNormal:
    v = values[values > 0]
    if v.size < 5:
        m = float(v.mean()) if v.size else 60.0
        return LogNormal(mu=math.log(max(m, 1e-3)), sigma=0.1, lo=max(1.0, m * 0.5), hi=m * 2.0)
    logs = np.log(v)
    return LogNormal(
        mu=float(logs.mean()),
        sigma=float(max(logs.std(ddof=1), 0.05)),
        lo=float(np.quantile(v, 0.01)),
        hi=float(np.quantile(v, 0.99)),
    )


def fit_cycle_dists(learn_dir: Path) -> dict[str, LogNormal]:
    """Per-plant cure cycle distribution."""
    out: dict[str, LogNormal] = {}
    p = learn_dir / "timing" / "curing_cycle_time.parquet"
    if not p.exists():
        log.warning("dist.cycle.missing")
        return out
    df = pl.read_parquet(p)
    for (plant,), g in df.group_by(["plant"]):
        # Weight each (recipe, press) by n; expand as pseudo-samples using p50.
        w = g["n"].to_numpy()
        m = g["p50_s"].to_numpy()
        samples = np.repeat(m, np.clip(w.astype(int), 1, 500))
        out[plant] = _fit(samples)
    log.info("dist.cycle.fit", plants=list(out))
    return out


def fit_setup_dists(learn_dir: Path) -> dict[tuple[str, str], LogNormal]:
    out: dict[tuple[str, str], LogNormal] = {}
    p = learn_dir / "timing" / "building_setup_time.parquet"
    if not p.exists():
        log.warning("dist.setup.missing")
        return out
    df = pl.read_parquet(p)
    for (plant, machine), g in df.group_by(["plant", "machine"]):
        w = g["n"].to_numpy()
        m = g["p50_s"].to_numpy()
        samples = np.repeat(m, np.clip(w.astype(int), 1, 500))
        if samples.size:
            out[(plant, machine)] = _fit(samples)
    log.info("dist.setup.fit", pairs=len(out))
    return out
