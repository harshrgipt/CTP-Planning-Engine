"""PLANT HOLIDAY CALENDAR -- the engine's only notion of time-phased availability.

    from planner.cmbc import holiday
    holiday.load("2026-08")                       # once, at the top of a layer
    holiday.next_free("PCR", ts)                  # push a start out of a closure
    holiday.add_work("PCR", ts, seconds)          # consume WORKING time only
    holiday.sub_work("PCR", ts, seconds)          # the inverse, walking backwards
    holiday.work_seconds("PCR", a, b)             # productive seconds in [a, b)
    holiday.fit_before("PCR", st, dur)            # latest start <= st that is clear

WHAT THIS IS -- rule G3, which shipped "Blocked / calendar assumed 24x7".
  A HOLIDAY IS A PLANT-DAY, not a calendar day. The plant day runs 07:00 ->
  07:00 and `date` in every export is the plant-day date, so 15 August closed
  means [2026-08-15 07:00, 2026-08-16 07:00). Getting this wrong by seven hours
  is the same defect that once mislabelled 28.7 % of build rows (MEMORY, plant
  day 07:00 -> 07:00).

  Inside the window NO press may cure and NO machine may build. Work is not
  lost: a cure campaign or a build run that would span the closure PAUSES and
  resumes, so its wall-clock end moves +24 h while its PRODUCTIVE hours are
  unchanged. That distinction -- wall-clock span vs working span -- is the whole
  content of this module, and every caller has to say which one it means.

WHY IT IS DATA AND NOT A CAP
  PARTITION_AND_CHANGEOVER §8 says "add a cap in config.py or nowhere". A cap is
  an enforced LIMIT on a quantity; a holiday is a CALENDAR FACT that changes
  month to month and comes from the plant, so it belongs beside the other month
  masters. It is read from, in priority order:

    1. PLANNER_HOLIDAYS   comma-separated, for A/B arms:
                            PLANNER_HOLIDAYS=2026-08-15
                            PLANNER_HOLIDAYS=PCR:2026-08-15,TBR:2026-08-16
                          A bare date closes BOTH plants. `PLANNER_HOLIDAYS=`
                          (set but empty) is an explicit "no holidays" and
                          suppresses the file, so an arm can prove the negative.
    2. masters/holidays_<month>.json
                            ["2026-08-15"]                       both plants
                            {"all": [...], "PCR": [...], "TBR": [...]}
    3. absent -> EMPTY -> every function below is the identity, so a run with no
       holiday file and no env var is BYTE-IDENTICAL to the engine without this
       module. That claim is verified, not assumed -- see the measurement block.

  Dates outside the planning month are kept, not dropped: L5 plans into a 72 h
  tail past month end under HORIZON_MODE=extend, and a closure on 1 September is
  a real constraint on an August campaign that runs over the boundary.

# =====================================================================
# SHIPS OFF (no file, no env var). MEASURED 2026-08-20, AUGUST 2026, BOTH PLANTS.
#
# Arms fresh via scripts/run_arm.py, all gated FRESH by check_arm_fresh.py.
# Partition INPUT/derived/gt_machine_partition.parquet stamped 2026-08, sha1
# 8bcb10c113bf, 95 rows (it was found stamped 2026-07 on disk and restored).
# Baseline state: press availability 1.0 both plants and load/unload 2.5/2.5 min
# (plant instruction 2026-08-19); PLANNER_L5_DAY_CAP off; no PRESS_EFFICIENCY
# term exists in the live path at all.
#   HOLbase  defaults          HOLhol  PLANNER_HOLIDAYS=2026-08-15
#
# TWO BUILT BASES, AND THEY DISAGREE IN SIGN. `BUILT` as scripts/arm_scorecard.py
# defines it sums the WHOLE planning window -- month + the 72 h HORIZON_MODE
# tail -- so a plan that shifts right keeps its tyres in the count. `BUILTinm`
# is the same sum restricted to slices finishing before 1 Sep 07:00. A closure
# moves work rightward, so it is exactly the change for which the two bases
# separate, and quoting only the first would report a closed plant-day as free.
#
#   PCR   demand 426,688
#   arm        BUILT   dBUILT   BUILTinm    dBinm   fed inm   tail  starved   R5   L11
#   HOLbase  400,467       +0    399,636       +0   402,874  2,825   21,705  64.2  28/48
#   HOLhol   400,674     +207    398,349   -1,287   400,668  5,263   21,483  69.0  29/48
#
#   TBR   demand 98,743
#   HOLbase   97,741       +0     97,741       +0    98,480    462    1,848  61.2  28/48
#   HOLhol    97,381     -360     97,239     -502    98,227    461    2,196  69.3  29/48
#
#   BUILT/demand   PCR 93.9 -> 93.9 %   TBR 99.0 -> 98.6 %
#   BUILTinm/dmd   PCR 93.7 -> 93.4 %   TBR 99.0 -> 98.5 %
#   fed/demand     PCR 94.4 -> 93.9 %   TBR 99.7 -> 99.5 %
#   GT inventory, time-weighted mean and daily-mean max vs rail:
#     PCR 3,546 -> 3,614, max 4,612 -> 4,596 vs 4,800
#     TBR 1,142 -> 1,119, max 1,320 -> 1,312 vs 1,400
#   L11 GAINS one invariant on each plant (TBR same-day build/cure correlation
#   0.805 -> 0.939); nothing goes PASS -> FAIL.
#   scripts/verify_export.py on the exported pack: HARD 0, SOFT 0, EXPORT 0.
#
# ENFORCEMENT, re-derived from the run's parquets without importing this module:
#   build slices overlapping the window     196 (411.7 machine-h)  ->  0
#   tyres built on plant-day 15                          17,017    ->  0.0
#   tyres cured on plant-day 15                          17,958.9  ->  0.0
#   mould-change WORK inside the window                       9 chg -> 0 min
#     (3 changes SPAN the window and pause; span - blocked == minutes exactly)
#   147 cure campaigns span the closure and pause -- that is the design.
#
# THERE IS NO DIP EITHER SIDE, AND THAT IS CHECKED, NOT ASSERTED.
#   PCR cure days 1-14 are IDENTICAL TO THE TYRE to base (every delta 0), day 15
#   is 0, and days 16-31 are base's 15-30 shifted right one day. A pure
#   translation. TBR cure is identical on days 1-3 and HIGHER on every other day.
#   Interior (d2-d29, holiday excluded) CV falls on all four series:
#     PCR build 0.1241 -> 0.0784   PCR cure 0.0761 -> 0.0671
#     TBR build 0.0453 -> 0.0434   TBR cure 0.0237 -> 0.0227
#   THE ONE REAL RESIDUAL is day 14, the shift that runs into the closure:
#   PCR 12,932 built against an interior mean of 13,875 (-6.8 %), TBR 3,088
#   against 3,419 (-9.7 %). That is the blocking model paying for itself: a
#   build run that cannot FINISH before 07:00 is pulled wholly before the
#   closure, and the last hours of day 14 are left as a sliver no legal run
#   fits. It is one shift on one day and it is named here rather than smoothed.
#
# WHY THE COST IS SO FAR BELOW ONE DAY -- READ THIS BEFORE QUOTING THE NUMBER.
#   The closure removes 13,704 PCR / 3,313 TBR tyres of scheduled output and
#   in-month BUILT falls 1,287 / 502, i.e. 91 % / 85 % of the lost day comes
#   back. It comes back from ONE place: August's base plan already tapers over
#   its last three days -- PCR builds 9,614 across d29-31 against an interior
#   rate of ~13,600/day, so ~31,000 tyres of build capacity sit idle there. The
#   holiday shifts work right into exactly that hole (PCR d29-31 9,614 ->
#   19,738, +10,124). A CLOSURE IS NOT CHEAP; THIS MONTH HAPPENS TO HAVE AN
#   EMPTY SHELF AT THE END TO PUT THE DAY ON. On a month whose plan runs flat to
#   day 31, or under a horizon ruling that closes the box, the same closure
#   costs close to a full day. Do not generalise 1,287 to another month.
#
# THE REAL COST IS R5, NOT VOLUME. GT wait max PCR 64.2 -> 69.0 h and TBR 61.2
#   -> 69.3 h against a HARD 72 h. Green tyres age through a shutdown at the
#   same rate as anywhere else, so the closure spends 3.0 h / 2.7 h of the
#   remaining shelf-life margin. A SECOND consecutive closed day, or this one in
#   a month with less slack, breaches R5 and the volume it breaches by is lost,
#   not delayed. That is the number to watch, and it is why `_merge` above turns
#   consecutive holidays into one window instead of two.
#
# A DEFECT THIS MEASUREMENT CAUGHT IN ITS OWN FIRST IMPLEMENTATION. L5 and L10
#   were made holiday-aware and BOTH looked clean -- `cure_by_shift` read
#   0 / 14,623 / 14,631 across days 15/16/17. The EXPORTED pack did not: sheet
#   `7_daily_summary` read 5,766 cured on the closed day and 10,094 / 13,091 on
#   the two after, against ~14,600 either side. The export buckets on the
#   per-slice `cure_ts`, which L7 interpolated linearly across the campaign's
#   WALL-CLOCK span; a paused campaign's span is 24 h longer than the press
#   hours it draws, so the draw was spread into the shut window and diluted
#   everything around it. That is the "neighbours sag" symptom arriving through
#   the one path that does not go through `cure_by_shift`, and the first,
#   unfixed arm scored PCR -8,317 BUILT and L11 28 -> 27 because of it. The
#   two artefacts DISAGREED, which is the only reason it was found. Do not
#   collapse them into one view. See the block at L7 phase 1.
#
# Ledger class: PARTITION_AND_CHANGEOVER.md section 4aa; rule G3.
# The OFF path is BYTE-IDENTICAL on all 14 parquets of a fresh arm -- verified
# twice, once after the L5/L10 work and again after the L7 fix. Never "verify"
# this by inspection; run the arm and diff the sha1s.
# =====================================================================
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

from planner import paths

DAY_START_H = 7                    # the plant day starts at 07:00, not midnight
PLANTS = ("PCR", "TBR")

# plant -> sorted, merged, non-overlapping [start, end) closure windows
_WINDOWS: dict[str, list[tuple[datetime, datetime]]] = {p: [] for p in PLANTS}
ACTIVE = False                     # True only when at least one window exists
_LOADED_MONTH: str | None = None


def _parse_dates(spec) -> list[date]:
    out = []
    if isinstance(spec, str):
        spec = [x.strip() for x in spec.split(",")]
    for x in spec or []:
        x = str(x).strip()
        if not x:
            continue
        out.append(date.fromisoformat(x))
    return out


def _window(d: date) -> tuple[datetime, datetime]:
    """The PLANT-DAY window for a holiday date: 07:00 -> 07:00 next day."""
    s = datetime(d.year, d.month, d.day, DAY_START_H, 0)
    return s, s + timedelta(days=1)


def _merge(ws: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Merge touching/overlapping windows so consecutive holidays are one hole.

    Two adjacent closed days MUST become a single 48 h window, not two 24 h ones
    -- `add_work` steps out of one window at a time, and an unmerged pair would
    leave it standing at 07:00 on the second holiday believing it had found
    working time."""
    out: list[tuple[datetime, datetime]] = []
    for s, e in sorted(ws):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def load(month: str) -> None:
    """Read the month's calendar. Idempotent; call at the top of every layer."""
    global ACTIVE, _LOADED_MONTH
    _LOADED_MONTH = month
    per: dict[str, list[date]] = {p: [] for p in PLANTS}

    env = os.environ.get("PLANNER_HOLIDAYS")
    if env is not None:                       # SET, even to "", suppresses the file
        for tok in env.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if ":" in tok:
                pl_, ds = tok.split(":", 1)
                pl_ = pl_.strip().upper()
                if pl_ in per:
                    per[pl_].extend(_parse_dates(ds))
            else:
                for p in PLANTS:
                    per[p].extend(_parse_dates(tok))
    else:
        f = paths.holidays(month)
        if f.exists():
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except Exception as exc:                              # noqa: BLE001
                # NEVER SILENT. A holiday the plant sent and we could not parse
                # is a plant closed on a day we schedule production into.
                raise SystemExit(f"!! holidays: {f} is unreadable ({exc})")
            if isinstance(raw, list):
                for p in PLANTS:
                    per[p].extend(_parse_dates(raw))
            elif isinstance(raw, dict):
                for p in PLANTS:
                    per[p].extend(_parse_dates(raw.get("all", [])))
                    per[p].extend(_parse_dates(raw.get(p, [])))
            else:
                raise SystemExit(f"!! holidays: {f} must be a list or an object")

    for p in PLANTS:
        _WINDOWS[p] = _merge([_window(d) for d in per[p]])
    ACTIVE = any(_WINDOWS[p] for p in PLANTS)


def summary() -> str:
    if not ACTIVE:
        return "PLANT HOLIDAYS: none"
    bits = []
    for p in PLANTS:
        if _WINDOWS[p]:
            bits.append(f"{p} " + " ".join(
                f"{s:%Y-%m-%d} 07:00->{e:%m-%d} 07:00" for s, e in _WINDOWS[p]))
    return "PLANT HOLIDAYS: " + " | ".join(bits)


def windows(plant: str) -> list[tuple[datetime, datetime]]:
    return _WINDOWS.get(plant, [])


def is_blocked(plant: str, ts: datetime) -> bool:
    if not ACTIVE:
        return False
    return any(s <= ts < e for s, e in _WINDOWS.get(plant, []))


def next_free(plant: str, ts: datetime) -> datetime:
    """Smallest t >= ts at which work may begin."""
    if not ACTIVE:
        return ts
    for s, e in _WINDOWS.get(plant, []):
        if s <= ts < e:
            return e
    return ts


def free_before(plant: str, ts: datetime) -> datetime:
    """Largest t <= ts that is not INSIDE a closure (the closure's own start
    qualifies -- work may run right up to 07:00 on the holiday)."""
    if not ACTIVE:
        return ts
    for s, e in _WINDOWS.get(plant, []):
        if s < ts < e:
            return s
    return ts


def work_seconds(plant: str, a: datetime, b: datetime) -> float:
    """PRODUCTIVE seconds in [a, b). Never negative."""
    tot = (b - a).total_seconds()
    if tot <= 0:
        return 0.0
    if not ACTIVE:
        return tot
    for s, e in _WINDOWS.get(plant, []):
        lo, hi = max(a, s), min(b, e)
        if hi > lo:
            tot -= (hi - lo).total_seconds()
    return max(tot, 0.0)


def blocked_seconds(plant: str, a: datetime, b: datetime) -> float:
    if not ACTIVE:
        return 0.0
    return max((b - a).total_seconds(), 0.0) - work_seconds(plant, a, b)


def add_work(plant: str, ts: datetime, secs: float) -> datetime:
    """The instant reached after consuming `secs` of WORKING time from `ts`.

    Steps out of a closure first, then walks window by window. Returns
    `ts + secs` exactly when the calendar is empty, which is what makes every
    call site byte-identical with no holidays configured."""
    if not ACTIVE:
        return ts + timedelta(seconds=secs)
    if secs <= 0:
        return next_free(plant, ts)
    cur = next_free(plant, ts)
    left = secs
    for s, e in _WINDOWS.get(plant, []):
        if e <= cur:
            continue
        if s >= cur + timedelta(seconds=left):
            break                          # finishes before this closure opens
        left -= (s - cur).total_seconds()  # spend what is available up to it
        cur = e
    return cur + timedelta(seconds=left)


def sub_work(plant: str, ts: datetime, secs: float) -> datetime:
    """The instant from which `secs` of WORKING time ends exactly at `ts`.

    The backward twin of `add_work`, used wherever the engine places a run from
    its DEADLINE rather than from its start (all of L7's release arithmetic)."""
    if not ACTIVE:
        return ts - timedelta(seconds=secs)
    if secs <= 0:
        return free_before(plant, ts)
    cur = free_before(plant, ts)
    left = secs
    for s, e in reversed(_WINDOWS.get(plant, [])):
        if s >= cur:
            continue
        if e <= cur - timedelta(seconds=left):
            break
        left -= (cur - e).total_seconds()
        cur = s
    return cur - timedelta(seconds=left)


def fit_before(plant: str, st: datetime, dur: timedelta) -> datetime:
    """Latest start <= `st` whose whole [start, start+dur] span is clear.

    This is the BLOCKING form, for L7, where a build run is short next to a
    closure and pausing it is not what the floor does -- it finishes the run
    before the plant shuts. Walking the windows right to left mirrors the
    setup-aware backward walk in `_place`, so it composes with it."""
    if not ACTIVE:
        return st
    cur = st
    for s, e in reversed(_WINDOWS.get(plant, [])):
        if cur < e and s < cur + dur:
            cur = s - dur
    return cur


def clear(plant: str, st: datetime, en: datetime) -> bool:
    """True if [st, en) contains no closed time."""
    if not ACTIVE:
        return True
    return blocked_seconds(plant, st, en) <= 0.0
