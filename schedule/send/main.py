"""CTP PLANNER -- the single entry point.  Everything runs from here.

    python main.py status   --month 2026-08              what is on disk
    python main.py check    --month 2026-08              input gate only
    python main.py plan     --month 2026-08              inputs -> BTP pack
    python main.py export   --month 2026-08 --run NAME   re-export an existing run
    python main.py verify   --month 2026-08              independent verifier
    python main.py masters  --month 2026-08 --demand ... --sheet ...
    python main.py rebuild  --month 2026-08              offline miners (needs raw MES)
    python main.py test

Every subcommand accepts trailing KEY=VALUE pairs, which become PLANNER_* env
overrides for that invocation:

    python main.py plan --month 2026-08 PLANNER_L7_MAKEROOM=0

DESIGN NOTES THAT ARE NOT OBVIOUS

  * The run directory is ALWAYS rebuilt from L5. Never seed one with `cp -r`:
    15 arm directories once carried another arm's scorecard and a flag worth
    8,085 tyres read as free (PARTITION section 7).

  * PLANNER_OPENING_GT is threaded through EVERY step from one place. Both the
    planner and the exporters read it; exporting with a different value than the
    arm was built with changes only the GT_Inventory column of the BTP
    fulfilment sheets while every other figure stays identical -- a diff that
    reads like a scheduling change and is not.

  * The partition is rebuilt only when the file on disk is not already stamped
    for the target month, because `build_gt_machine_partition.py` mines v_build
    and needs the 4.4 GB raw MES a clone does not have. `check` reads that stamp
    first and BLOCKS on a mismatch, so reusing it can never mean using a stale
    one.

  * THERE IS NO `optimize` MODE. L9 was wired correctly (L5 -> L9 -> L7 -> L10
    -> L11) and MEASURED on 2026-08 with a 300 s budget: it searched 1,428
    candidates, accepted 0 moves, and reported "NO IMPROVEMENT FOUND. Plan left
    unchanged." All nine cost tiers ended where they started, cure_campaigns
    came out byte-identical to L5's own output, and 0 of 40 invariants moved.
    The search is lexicographic, so tier 1 (demand short = 7,409) rejects every
    move that would gain below it. L9 is in planner/cmbc/_retired/ with that
    measurement recorded; re-measure there if the baseline ever moves.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
PY = sys.executable


# ---------------------------------------------------------------------------
# process plumbing
# ---------------------------------------------------------------------------
def _env(overrides: list[str], opening_gt: str | None) -> dict[str, str]:
    """Build the child environment AND mirror the overrides into this process.

    `status` and the partition-stamp check run in-process and call
    `planner.paths`, which reads os.environ directly. Setting only the child env
    made `status` report the month-default opening-GT file while every planning
    step used the one that was actually passed -- the same class of mismatch
    that silently changes GT_Inventory in the BTP fulfilment sheets.
    """
    over: dict[str, str] = {}
    for kv in overrides:
        if "=" not in kv:
            raise SystemExit(f"expected KEY=VALUE, got {kv!r}")
        k, v = kv.split("=", 1)
        over[k] = v
    if opening_gt:
        over["PLANNER_OPENING_GT"] = opening_gt
    os.environ.update(over)
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(ROOT)}


def _step(tag: str, cmd: list[str], env: dict[str, str],
          run_dir: Path | None = None, *, quiet: bool = True) -> str:
    t0 = time.time()
    print(f"  [{tag}] {' '.join(cmd[2:])[:86]}", flush=True)
    cp = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    if run_dir is not None and run_dir.exists():
        # The partition staleness guard, the rim-spill report and the carry-in
        # line are PRINTED, not written to parquet. Without this log there is no
        # evidence of which partition the run actually used.
        (run_dir / f"log_{tag}.txt").write_text(cp.stdout, encoding="utf-8")
    if cp.returncode != 0:
        sys.stderr.write(cp.stdout[-6000:] + "\n" + cp.stderr[-4000:] + "\n")
        raise SystemExit(f"!! {tag} failed ({cp.returncode})")
    if not quiet:
        print(cp.stdout)
    print(f"       ok  {time.time() - t0:5.1f}s", flush=True)
    return cp.stdout


def _banner(title: str, **kv) -> None:
    print(f"\n  {title}")
    for k, v in kv.items():
        if v is not None:
            print(f"  {k:<12} {v}")
    print()


# ---------------------------------------------------------------------------
# partition: rebuild only when the stamp is wrong
# ---------------------------------------------------------------------------
def _partition_ok(month: str) -> bool:
    import polars as pl
    from planner import paths
    f = paths.input_derived("gt_machine_partition.parquet")
    if not f.exists():
        return False
    return pl.read_parquet(f)["month"].unique().to_list() == [month]


def _ensure_partition(month: str, env: dict[str, str], force: bool,
                      quiet: bool) -> None:
    if _partition_ok(month) and not force:
        print(f"  [03_partition] already stamped {month} -- reusing "
              f"(--force-partition to rebuild; needs raw MES)\n")
        return
    _step("03_partition", [PY, "scripts/build_gt_machine_partition.py", month],
          env, quiet=quiet)


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------
def cmd_check(a, env) -> None:
    cmd = [PY, "-m", "planner.cmbc.l1_preflight", "--month", a.month]
    if a.strict:
        cmd.append("--strict")
    _step("00_preflight", cmd, env, quiet=False)


def _plan_core(a, env) -> Path:
    """L4 -> L4.5 -> partition -> L5 -> L7 -> L10 -> L11. Always fresh."""
    month, quiet = a.month, not a.verbose
    run_name = a.run or f"plan_{month}"
    run_dir = ROOT / "runs" / run_name

    cmd = [PY, "-m", "planner.cmbc.l1_preflight", "--month", month]
    if getattr(a, "strict", False):
        cmd.append("--strict")
    _step("00_preflight", cmd, env, quiet=False)

    _step("01_l4_net_requirement",
          [PY, "-m", "planner.cmbc.l4_net_requirement", "--month", month], env,
          quiet=quiet)
    _step("02_l45_lotsize",
          [PY, "-m", "planner.cmbc.l45_lotsize", "--month", month], env, quiet=quiet)
    _ensure_partition(month, env, getattr(a, "force_partition", False), quiet)

    if run_dir.exists():
        shutil.rmtree(run_dir)                      # FRESH. Never inherit.

    _step("04_l5_cure_master",
          [PY, "-m", "planner.cmbc.l5_cure_master", "--month", month,
           "--out", run_name], env, run_dir, quiet=quiet)

    for tag, mod in (("05_l7_pull_release", "l7_pull_release"),
                     ("06_l10_discretise", "l10_discretise"),
                     ("07_l11_validate_plan", "l11_validate_plan")):
        _step(tag, [PY, "-m", f"planner.cmbc.{mod}", "--month", month,
                    "--run", run_name], env, run_dir, quiet=quiet)
    return run_dir


def _export(a, env, run_name: str, out_dir: Path, *, quiet: bool) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _step("08_gt_sku_share",
          [PY, "-m", "scripts.build_gt_sku_share", "--month", a.month,
           "--from-demand"], env, quiet=quiet)
    _step("09_btp_export",
          [PY, "-m", "scripts.export_btp_format", "--run", run_name,
           "--month", a.month, "--out", str(out_dir / "btp")], env, quiet=quiet)
    if not a.btp_only:
        # The 13-sheet verification pack. verify_export.py reads ITS csvs, not
        # the BTP workbooks, so skipping this makes `verify` impossible.
        _step("10_shift_pack",
              [PY, "scripts/export_shift_schedule.py", run_name, a.month,
               str(out_dir)], env, quiet=quiet)


def cmd_plan(a, env) -> None:
    run_name = a.run or f"plan_{a.month}"
    out_dir = Path(a.out) if a.out else ROOT / "output" / f"{a.month}_pack"
    _banner(f"PLAN  {a.month}  ->  runs/{run_name}  ->  {out_dir}",
            opening_gt=env.get("PLANNER_OPENING_GT", "(month default)"))
    a.run = run_name
    _plan_core(a, env)
    _export(a, env, run_name, out_dir, quiet=not a.verbose)
    print(f"\n  DONE  runs/{run_name}  ->  {out_dir}\n")


def cmd_export(a, env) -> None:
    if not a.run:
        raise SystemExit("export needs --run")
    out_dir = Path(a.out) if a.out else ROOT / "output" / f"{a.month}_pack"
    _banner(f"EXPORT  runs/{a.run}  ->  {out_dir}",
            opening_gt=env.get("PLANNER_OPENING_GT", "(month default)"))
    _export(a, env, a.run, out_dir, quiet=not a.verbose)
    print(f"\n  DONE  {out_dir}\n")


def cmd_verify(a, env) -> None:
    out_dir = Path(a.out) if a.out else ROOT / "output" / f"{a.month}_pack"
    _banner(f"VERIFY  {out_dir}")
    _step("verify", [PY, "scripts/verify_export.py", str(out_dir), a.month],
          env, quiet=False)


def cmd_masters(a, env) -> None:
    _banner(f"MASTERS  {a.month}")
    if a.demand:
        cmd = [PY, "-m", "scripts.ingest_orderbook_demand", "--xlsx", a.demand,
               "--month", a.month]
        if a.sheet:
            cmd += ["--sheet", a.sheet]
        _step("demand", cmd, env, quiet=False)
    if a.opening_pcr or a.opening_tbr:
        cmd = [PY, "-m", "scripts.ingest_manual_opening_gt", "--month", a.month,
               "--age-h", str(a.age_h)]
        if a.opening_pcr:
            cmd += ["--pcr", a.opening_pcr]
        if a.opening_tbr:
            cmd += ["--tbr", a.opening_tbr]
        _step("opening_gt", cmd, env, quiet=False)
    if not (a.demand or a.opening_pcr or a.opening_tbr):
        print("  nothing to do -- pass --demand and/or --opening-pcr/--opening-tbr\n")


def cmd_rebuild(a, env) -> None:
    """The offline miners. Each needs the raw MES drop; they fail loudly without it."""
    _banner(f"REBUILD  {a.month}   (needs raw MES -- curing/, o_production/, ...)")
    for tag, mod in (("l0_learn", "_offline.l0_learn"),
                     ("l2_capability", "_offline.l2_capability"),
                     ("l3_ceiling", "_offline.l3_ceiling")):
        _step(tag, [PY, "-m", f"planner.cmbc.{mod}", "--month", a.month], env,
              quiet=not a.verbose)
    _ensure_partition(a.month, env, True, not a.verbose)
    print("\n  DONE  frozen inputs rebuilt\n")


def cmd_status(a, env) -> None:
    import polars as pl
    from planner import paths
    _banner(f"STATUS  {a.month}")
    rows = [
        ("demand", paths.demand(a.month)),
        ("opening_gt", paths.opening_gt(a.month)),
        ("params (L0)", paths.WH_PARAMS / "params_2026-08-01.json"),
        ("cap_machine (L2)", paths.wh_derived(f"cap_machine_{a.month}.parquet")),
        ("cap_press (L2)", paths.wh_derived(f"cap_press_{a.month}.parquet")),
        ("l3_cavities (L3)", paths.wh_derived("l3_cavities.parquet")),
        ("net_requirement (L4)", paths.wh_derived(f"net_requirement_{a.month}.parquet")),
        ("l45_lots (L4.5)", paths.wh_derived(f"l45_lots_{a.month}.parquet")),
        ("partition", paths.input_derived("gt_machine_partition.parquet")),
    ]
    for name, p in rows:
        mark = "ok " if p.exists() else "MISSING"
        extra = ""
        if p.exists() and p.suffix == ".parquet":
            try:
                extra = f"{pl.read_parquet(p).height:,} rows"
            except Exception:                                    # noqa: BLE001
                extra = ""
        print(f"  {mark:<8} {name:<26} {extra:>12}  {p.name}")
    print(f"\n  partition stamped for {a.month}: "
          f"{'YES' if _partition_ok(a.month) else 'NO -- rebuild needed'}")
    runs = sorted(d.name for d in (ROOT / "runs").iterdir() if d.is_dir())
    print(f"  runs/: {', '.join(runs) if runs else '(none)'}\n")


def cmd_test(a, env) -> None:
    _banner("TEST")
    _step("pytest", [PY, "-m", "pytest", "tests/", "-q"], env, quiet=False)


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        prog="main.py", description="CTP planner -- inputs in, BTP pack out.",
        epilog="Trailing KEY=VALUE pairs become PLANNER_* env overrides.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, *, month=True):
        if month:
            p.add_argument("--month", required=True, help="YYYY-MM")
        p.add_argument("--opening-gt", default=None,
                       help="opening-stock file; bare name resolves inside "
                            "masters/opening_gt/")
        p.add_argument("-v", "--verbose", action="store_true")

    def planlike(p):
        common(p)
        p.add_argument("--run", default=None, help="run directory under runs/")
        p.add_argument("--out", default=None, help="export directory")
        p.add_argument("--btp-only", action="store_true",
                       help="skip the 13-sheet shift pack (verify needs it)")
        p.add_argument("--force-partition", action="store_true")
        p.add_argument("--strict", action="store_true",
                       help="preflight treats WARN as blocking")

    p = sub.add_parser("check", help="input gate only")
    common(p)
    p.add_argument("--strict", action="store_true")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("plan", help="inputs -> checks -> schedule -> BTP pack")
    planlike(p)
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("export", help="re-export an existing run")
    planlike(p)
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("verify", help="independent verifier over the exported pack")
    common(p)
    p.add_argument("--out", default=None)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("masters", help="ingest this month's demand / opening GT")
    common(p)
    p.add_argument("--demand", default=None, help="order-book xlsx")
    p.add_argument("--sheet", default=None)
    p.add_argument("--opening-pcr", default=None)
    p.add_argument("--opening-tbr", default=None)
    p.add_argument("--age-h", type=float, default=24.0)
    p.set_defaults(fn=cmd_masters)

    p = sub.add_parser("rebuild", help="offline miners L0/L2/L3 + partition")
    common(p)
    p.set_defaults(fn=cmd_rebuild)

    p = sub.add_parser("status", help="what is on disk for this month")
    common(p)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("test", help="pytest")
    common(p, month=False)
    p.set_defaults(fn=cmd_test)

    a, rest = ap.parse_known_args()
    env = _env(rest, a.opening_gt)
    a.fn(a, env)


if __name__ == "__main__":
    main()
