from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

from planner.config import CONFIG


@dataclass
class RunContext:
    run_id: str
    seed: int
    dir: Path
    started_ts: float

    @classmethod
    def new(cls, tag: str = "run") -> "RunContext":
        cfg_json = CONFIG.model_dump_json()
        h = hashlib.sha1(cfg_json.encode()).hexdigest()[:8]
        ts = time.strftime("%Y%m%dT%H%M%S")
        run_id = f"{ts}-{tag}-{h}"
        rd = CONFIG.paths.runs / run_id
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "config.json").write_text(cfg_json)
        seed = CONFIG.seed
        random.seed(seed)
        np.random.seed(seed)
        return cls(run_id=run_id, seed=seed, dir=rd, started_ts=time.time())

    def path(self, *parts: str) -> Path:
        p = self.dir.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def to_json(self) -> str:
        return json.dumps({**asdict(self), "dir": str(self.dir)}, default=str)
