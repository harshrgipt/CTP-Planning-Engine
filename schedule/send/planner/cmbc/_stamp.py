"""PROVENANCE STAMP for shared masters generated OUTSIDE an A/B arm.

WHY THIS EXISTS
  `scripts/run_arm.py` runs L5 -> L11 only. L4 (`net_requirement_<M>.parquet`)
  and L4.5 (`l45_lots_<M>.parquet`) are NOT re-derived per arm, so every arm
  inherits whatever the last `main.py plan` left in `warehouse/derived/`.

  That is DO-NOT #18 one layer up. The 2026-08-08 incident gave 15 run
  directories another arm's `l11_invariants.parquet`; this is the same shape,
  except the contaminated artefact is a SHARED MASTER and `check_arm_fresh.py`
  does not look at `warehouse/` at all. A single `main.py plan` with an
  L4.5-affecting flag silently re-bases every subsequent arm, and nothing
  detects it.

  It matters concretely: `n_lots` -- how many cure campaigns a GT gets -- is
  decided in L4.5 and drives campaign count, concurrency, campaign length and
  build run size. It has therefore never been A/B'd. `PLANNER_L45_CAMP_STAT`
  measured on a full-pipeline harness is NOT comparable with any arm measured on
  the frozen one.

  So: every layer that writes a shared master stamps the flags that produced it,
  and preflight refuses to plan when the stamp disagrees with the current env.
  Fourth master to need this -- gt_machine_partition had it, cap_ttl_groups did
  not, carry_in got it on 2026-08-14. The rule is now explicit: anything
  generated outside the arm and read inside it must carry a stamp.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

# Flags that change what L4 / L4.5 emit. Add here when a new one lands -- a flag
# that changes a shared master and is absent from this list is invisible.
WATCHED = (
    "PLANNER_L45_CAMP_STAT", "PLANNER_LOOKAHEAD_PRORATA", "PLANNER_OPENING_GT",
    # PLANNER_MIN_DEMAND_PCR / _TBR were REMOVED 2026-08-18: `Thresholds` uses
    # env_prefix "PLANNER_TH_", so neither name reaches any setting. Setting one
    # changed the stamp -- forcing a spurious re-run -- and changed no behaviour.
    # The live one is PLANNER_TH_MIN_DEMAND_UNITS (JSON only), already below.
    "PLANNER_TH_MIN_LOT_UNITS", "PLANNER_TH_MIN_DEMAND_UNITS",
    "PLANNER_L45_CAMP_MIN_H", "PLANNER_SLICE_MULT",
    # Added 2026-08-17 after auditing this list against every `os.environ` read
    # in l4_net_requirement.py and l45_lotsize.py -- it was the only live flag
    # missing, and it is not a small one: it sets each GT's CONCURRENCY in L4.5
    # ("concurrency is chosen, not inherited", l45_lotsize.py line 297), capped by
    # the physical mould count. Changing it rewrites l45_lots_<M>.parquet while
    # the stamp still matched, which is precisely the failure this file exists to
    # stop. Re-run that audit whenever a flag is added to either layer.
    "PLANNER_L45_CONC_FLOOR",
)


def env_fingerprint(extra: dict | None = None) -> dict:
    d = {k: os.environ.get(k, "") for k in WATCHED}
    if extra:
        d.update({str(k): str(v) for k, v in extra.items()})
    blob = json.dumps(d, sort_keys=True)
    return {"flags": d, "sha1": hashlib.sha1(blob.encode()).hexdigest()[:12]}


def write(target: Path, extra: dict | None = None) -> None:
    """Write `<target>.provenance.json` beside a shared master."""
    fp = env_fingerprint(extra)
    target.with_suffix(".provenance.json").write_text(
        json.dumps(fp, indent=2), encoding="utf-8")


def check(target: Path, extra: dict | None = None,
          remedy: str = "Re-run L4/L4.5") -> str | None:
    """Reason string if the master was built under different flags, else None.

    `remedy` names the layer to re-run. It is a parameter because this guard now
    covers three masters from two different layers, and telling someone to
    "re-run L4/L4.5" when their B16 split is stale sends them to the wrong file.
    """
    side = target.with_suffix(".provenance.json")
    if not target.exists():
        return f"{target.name} is missing"
    if not side.exists():
        return (f"{target.name} carries no provenance stamp -- it predates the "
                f"guard, or was written by a layer that does not stamp. "
                f"{remedy} for this month.")
    try:
        rec = json.loads(side.read_text(encoding="utf-8"))
    except Exception as exc:                                   # noqa: BLE001
        return f"{side.name} unreadable: {exc}"
    now = env_fingerprint(extra)
    if rec.get("sha1") != now["sha1"]:
        diff = {k: (rec.get("flags", {}).get(k, ""), v)
                for k, v in now["flags"].items()
                if rec.get("flags", {}).get(k, "") != v}
        if not diff:
            # SHA MOVED BUT NO FLAG VALUE DID -- so WATCHED itself grew or shrank.
            # The old message printed "built under DIFFERENT flags: {}", which
            # reads as a bug in the guard rather than the expected consequence of
            # adding a flag to the list. It is expected, it is not a false alarm
            # (the stamp genuinely no longer describes the current contract), and
            # the fix is the same re-run -- but say which it is.
            _added = sorted(set(now["flags"]) - set(rec.get("flags", {})))
            _gone = sorted(set(rec.get("flags", {})) - set(now["flags"]))
            return (f"{target.name} was stamped before the WATCHED flag list "
                    f"changed (stamp {rec.get('sha1')}, current {now['sha1']}; "
                    f"added {_added or 'none'}, removed {_gone or 'none'}). No "
                    f"flag VALUE differs -- the stamp simply predates the new "
                    f"contract. {remedy} to re-stamp.")
        return (f"{target.name} was built under DIFFERENT flags "
                f"(stamp {rec.get('sha1')}, current {now['sha1']}): {diff}. "
                f"{remedy}, or unset the flag.")
    return None
