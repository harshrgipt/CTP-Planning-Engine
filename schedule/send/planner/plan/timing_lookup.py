"""Cycle- and setup-time lookups from KB stat rules (or defaults)."""
from __future__ import annotations

from pathlib import Path

import polars as pl

from planner.runs.logger import log


class TimingLookup:
    """Wraps the timing parquets emitted by `learn.timing.compute_timing`.

    - `cycle_time_s(plant, machine, gt)` — seconds per unit (median).
    - `setup_time_s(plant, machine, from_sku, to_sku)` — median gap when different.
    """

    def __init__(self, learn_dir: Path):
        self._cycle = pl.DataFrame()
        self._setup = pl.DataFrame()
        ct = learn_dir / "timing" / "curing_cycle_time.parquet"
        st = learn_dir / "timing" / "building_setup_time.parquet"
        if ct.exists():
            self._cycle = pl.read_parquet(ct)
        if st.exists():
            self._setup = pl.read_parquet(st)
        self._build_by_machine: dict[tuple[str, str], float] = {}
        self._build_by_plant: dict[str, float] = {}
        self._load_build_cadence()
        self._cure_by_press: dict[tuple[str, str], float] = {}
        self._cure_by_plant: dict[str, float] = {}
        self._load_cure_cadence()
        self._mould_chg: dict[tuple[str, str], float] = {}
        self._mould_chg_plant: dict[str, float] = {}
        self._load_mould_change()
        # Plant-supplied changeover master + GT->size, for the real setup model.
        self._chg: dict[tuple[str, str], tuple[float, float]] = {}
        self._gt_size: dict[str, str] = {}
        self._load_setup_master()
        log.info("timing_lookup.loaded", cycle=self._cycle.height, setup=self._setup.height,
                 build_machines=len(self._build_by_machine))

    def _load_build_cadence(self) -> None:
        """Observed seconds-per-tyre on each building machine.

        Measured as the median gap between consecutive stage-2 build events on
        that machine, capped at 1h so shift breaks and outages don't inflate it.
        Reads the warehouse views, which honour the active as-of cutoff, so this
        stays leak-free under walk-forward. Same plan-time-derived-stat pattern
        the curing planner already uses for its press map.
        """
        from planner.data.warehouse import duck
        try:
            df = duck().execute("""
                WITH g AS (
                    SELECT plant, machineCode AS machine,
                           date_diff('second',
                               lag(event_ts) OVER (PARTITION BY plant, machineCode ORDER BY event_ts),
                               event_ts) AS gap
                    FROM v_build
                    WHERE stage = 2 AND QualityStatus = '1'
                )
                SELECT plant, machine, median(gap) AS p50
                FROM g WHERE gap BETWEEN 1 AND 3600
                GROUP BY 1, 2
            """).pl()
        except Exception as e:  # noqa: BLE001 -- empty/absent warehouse
            log.warning("timing_lookup.build_cadence_unavailable", err=str(e))
            return
        for r in df.iter_rows(named=True):
            if r["p50"]:
                self._build_by_machine[(r["plant"], r["machine"])] = float(r["p50"])
        for plant in {p for p, _m in self._build_by_machine}:
            vals = sorted(v for (p, _m), v in self._build_by_machine.items() if p == plant)
            self._build_by_plant[plant] = vals[len(vals) // 2]
        log.info("timing_lookup.build_cadence", plants=self._build_by_plant)

    def _load_cure_cadence(self) -> None:
        """Press SERVICE time per tyre -- how long a press is busy with one tyre.

        Three different numbers live in this data and must not be confused:

        1. `cycleStart - event_ts` ~1931s -- dwell of a tyre inside the press.
           A wcID starts a new tyre every ~268s, so a wcID is not one exclusive
           press and dwell is not its per-tyre occupancy.
        2. span/tyres ~590s -- observed *throughput*, i.e. service + changeover
           + idle smeared over every tyre. Using this as service time is
           circular: it makes modelled capacity equal whatever the plant
           happened to produce, so demand always looks like ~100% load and can
           never show headroom. It also stretches press timelines 2.2x and
           manufactures queues that do not exist.
        3. median inter-cure gap ~268s PCR / 385s TBR -- cadence while actually
           running, i.e. the burst rate between two consecutive tyres.

        **We use (2).** The plant states its curing capacity is ~13,500/day PCR,
        and the data agrees exactly: p50 12,664 / max 13,812 per day on 86
        presses = 147 per press-day = 587s. So the plant IS capacity-limited at
        that rate and throughput == capacity is a fact here, not the circular
        artefact it would be otherwise. Using the burst rate (3) instead models
        ~26,000/day -- roughly double the real presses -- and yields a schedule
        that cannot be executed.
        """
        from planner.data.warehouse import duck
        try:
            # SHIFT-BASED, not span/tyres. span/tyres divides by ALL elapsed
            # time including idle days, so it understates what a press does
            # while actually running. The shift median is the real rate and it
            # reconciles exactly with the cycle model:
            #   effective_CT = (raw_dwell + 2.3) / 0.94  = 35.1 min PCR
            #   floor(480/35.1) = 13 cycles/shift x 4 slots (2 moulds x 2
            #   cavities) = 52 tyres/shift = 156/day  == measured actual.
            # span/tyres gave 605 s/tyre against a true 28800/52 = 554 s, i.e.
            # we were understating press capacity by ~13% PCR / ~23% TBR.
            df = duck().execute("""
                WITH s AS (
                    SELECT plant, wcID::VARCHAR AS press,
                           CAST(event_ts - INTERVAL 7 HOUR AS DATE) AS d,
                           CASE WHEN hour(event_ts) BETWEEN 7 AND 14 THEN 'A'
                                WHEN hour(event_ts) BETWEEN 15 AND 22 THEN 'B'
                                ELSE 'C' END AS sh,
                           count(*) AS n
                    FROM v_curing WHERE statuscritical = 'Normal'
                    GROUP BY 1, 2, 3, 4
                )
                SELECT plant, press, 28800.0 / NULLIF(median(n), 0) AS s_per_tyre
                FROM s GROUP BY 1, 2 HAVING count(*) > 10
            """).pl()
        except Exception as e:  # noqa: BLE001
            log.warning("timing_lookup.cure_cadence_unavailable", err=str(e))
            return
        for r in df.iter_rows(named=True):
            if r["s_per_tyre"] and r["s_per_tyre"] > 0:
                self._cure_by_press[(r["plant"], r["press"])] = float(r["s_per_tyre"])
        for plant in {p for p, _m in self._cure_by_press}:
            vals = sorted(v for (p, _m), v in self._cure_by_press.items() if p == plant)
            self._cure_by_plant[plant] = vals[len(vals) // 2]
        log.info("timing_lookup.cure_cadence", plants=self._cure_by_plant)

    def _load_mould_change(self) -> None:
        """Press mould-change seconds from the CTP master (210-430 min PCR,
        361 min TBR), keyed to MES wcID through the pressbarCode crosswalk."""
        from planner.data.warehouse import duck
        con = duck()
        try:
            df = con.execute("""
                SELECT x.plant, x.press, m.mould_change_min * 60.0 AS s
                FROM v_press_xwalk x
                JOIN v_ctp_mould_change m
                  ON x.plant = m.plant AND x.asset_id = m.asset_id
            """).pl()
            for r in df.iter_rows(named=True):
                self._mould_chg[(r["plant"], r["press"])] = float(r["s"])
        except Exception as e:  # noqa: BLE001
            log.warning("timing_lookup.mould_change_absent", err=str(e))
        try:
            for plant, med in con.execute(
                "SELECT plant, median(mould_change_min) * 60.0 FROM v_ctp_mould_change GROUP BY 1"
            ).fetchall():
                self._mould_chg_plant[plant] = float(med)
        except Exception:  # noqa: BLE001
            pass
        log.info("timing_lookup.mould_change", presses=len(self._mould_chg),
                 plant_defaults=self._mould_chg_plant)

    def mould_change_s(self, plant: str, press: str | None = None) -> float:
        """Seconds lost to a mould change on this press (0 if unknown)."""
        if press is not None:
            v = self._mould_chg.get((plant, press))
            if v:
                return v
        return self._mould_chg_plant.get(plant, 0.0)

    def cure_cadence_s(self, plant: str, press: str | None = None) -> float:
        """Observed press-seconds per tyre (throughput-based, not in-press dwell)."""
        if press is not None:
            v = self._cure_by_press.get((plant, press))
            if v:
                return v
        v = self._cure_by_plant.get(plant)
        if v:
            return v
        return self.cure_cycle_s(plant)

    def build_cycle_s(self, plant: str, machine: str | None = None) -> float:
        """Observed seconds per tyre, per machine where known."""
        if machine is not None:
            v = self._build_by_machine.get((plant, machine))
            if v:
                return v
        v = self._build_by_plant.get(plant)
        if v:
            return v
        # No history at all (cold start): fall back to coarse plant defaults.
        return 45.0 if plant == "PCR" else 90.0

    def cure_cycle_s(self, plant: str, recipe: int | None = None, press: str | None = None) -> float:
        if self._cycle.height == 0:
            return 1800.0 if plant == "PCR" else 2400.0
        f = self._cycle.filter(pl.col("plant") == plant)
        if recipe is not None:
            f = f.filter(pl.col("recipe") == recipe)
        if press is not None:
            f = f.filter(pl.col("press") == press)
        if f.height == 0:
            return 1800.0 if plant == "PCR" else 2400.0
        return float(f["p50_s"].median() or 1800.0)

    def _load_setup_master(self) -> None:
        """Plant changeover master + a GT->size map to drive it.

        The plant charges changeover by whether the transition is SAME size or
        DIFFERENT size (PCR 28/60 min, TBR 10/24, varying per machine), so a
        flat mined median cannot express it. Size comes from the construction
        mappings, widened via the mould->SKU master.
        """
        from planner.data.plant_masters import _size_of
        from planner.data.warehouse import duck
        con = duck()
        try:
            df = con.execute(
                "SELECT plant, machine, same_size_min, diff_size_min FROM v_changeover_build"
            ).pl()
            for r in df.iter_rows(named=True):
                self._chg[(r["plant"], r["machine"])] = (
                    float(r["same_size_min"]) * 60.0, float(r["diff_size_min"]) * 60.0)
        except Exception as e:  # noqa: BLE001 -- master not supplied
            log.warning("timing_lookup.changeover_master_absent", err=str(e))

        # NB: PCR construction's `gt_code` drops the "GT " prefix that the MES
        # itemCode carries -- `gt_code_updated` is the one that actually joins.
        # TBR GT codes are size-led ("10.00 R 20 JDC3") and are parsed directly
        # in _size_for_gt, since TBR construction keys on "GT 5001" instead.
        for sql in (
            "SELECT gt_code_updated, size FROM v_construction_pcr "
            "WHERE gt_code_updated IS NOT NULL AND size IS NOT NULL",
            "SELECT c.gt_code_updated, m.size FROM v_construction_pcr c "
            "JOIN v_mould_sku m ON c.sku = m.sku "
            "WHERE c.gt_code_updated IS NOT NULL AND m.size IS NOT NULL",
            "SELECT gt_code, description FROM v_construction_tbr "
            "WHERE gt_code IS NOT NULL AND description IS NOT NULL",
        ):
            try:
                for gt, raw in con.execute(sql).fetchall():
                    s = _size_of(raw)
                    if gt and s:
                        self._gt_size.setdefault(str(gt).strip(), s)
            except Exception:  # noqa: BLE001 -- view may be absent
                continue
        log.info("timing_lookup.setup_master", machines=len(self._chg),
                 gt_sizes=len(self._gt_size))

    def _size_for_gt(self, gt: str) -> str | None:
        """Size of a GT code: master lookup, else parse it off the code itself.

        TBR GT codes lead with the size ("10.00 R 20 JDC3"), so they resolve
        without any master at all. PCR codes ("GT 1765 ROYL") do not and need
        the construction mapping.
        """
        s = self._gt_size.get(gt)
        if s:
            return s
        from planner.data.plant_masters import _size_of
        if gt and gt[:1].isdigit():
            s = _size_of(gt)
            if s:
                self._gt_size[gt] = s
                return s
        return None

    def setup_s(self, plant: str, machine: str, from_sku: str | None, to_sku: str) -> float:
        if not from_sku or from_sku == to_sku:
            return 0.0
        # Plant master first, when both sizes resolve.
        chg = self._chg.get((plant, machine))
        if chg:
            a, b = self._size_for_gt(from_sku), self._size_for_gt(to_sku)
            if a and b:
                return chg[0] if a == b else chg[1]
        if self._setup.height == 0:
            return chg[1] if chg else 900.0
        f = self._setup.filter(
            (pl.col("plant") == plant)
            & (pl.col("machine") == machine)
            & (pl.col("from_sku") == from_sku)
            & (pl.col("to_sku") == to_sku)
        )
        if f.height:
            return float(f["p50_s"][0])
        # fallback: mean setup on this machine
        f = self._setup.filter((pl.col("plant") == plant) & (pl.col("machine") == machine))
        if f.height:
            return float(f["p50_s"].median() or 900.0)
        return chg[1] if chg else 900.0
