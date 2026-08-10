from __future__ import annotations

from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Paths(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLANNER_", env_file=".env", extra="ignore")

    root: Path = Field(default_factory=lambda: Path(__file__).resolve().parent.parent)

    @property
    def raw_curing(self) -> Path:
        return self.root / "curing"

    @property
    def raw_o_production(self) -> Path:
        return self.root / "o_production"

    @property
    def raw_consumption(self) -> Path:
        return self.root / "io_production_consumption"

    @property
    def raw_bom(self) -> Path:
        return self.root / "bom_pcr_tbr"

    @property
    def raw_construction(self) -> Path:
        return self.root / "Sku construction mapping"

    @property
    def warehouse(self) -> Path:
        return self.root / "warehouse"

    @property
    def masters(self) -> Path:
        return self.root / "masters"

    @property
    def runs(self) -> Path:
        return self.root / "runs"


class Thresholds(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLANNER_TH_", env_file=".env", extra="ignore")

    hard_confidence: float = 0.995
    hard_support_min: int = 500
    hard_exception_max: float = 0.005
    hard_p_value_max: float = 0.001
    soft_confidence: float = 0.80
    soft_support_min: int = 100
    soft_ci_low: float = 0.70
    stat_min_sample: int = 30

    mpm_preferred_p: float = 0.60
    mpm_preferred_ci_low: float = 0.40
    # How many hours of extra queue wait a fully-preferred machine is worth.
    # Small => balance load across machines; large => honour preference at any
    # cost (the original hard-coded 1000 piled every lot onto one machine).
    mpm_delay_tolerance_h: float = 24.0
    # How many hours of extra queue wait it is worth to keep a GT running on the
    # machine already building it. Pure load-balancing splits every GT across
    # every machine, which balances beautifully and wrecks changeovers; this is
    # the counterweight. Total bonus budget (mpm + continuation) bounds how far
    # machine loads may drift apart, and so bounds makespan above the mean.
    # Tuned once on the Jan-2026 fold (util flattens above 36 while changeovers
    # keep falling, and GT aging degrades by 72). Frozen from Feb onward so the
    # remaining months stay a genuine held-out test.
    gt_continuation_bonus_h: float = 36.0
    # HARD plant constraint: a green tyre must be cured within this many hours
    # of being built, or it is scrap. Bounds how far building may run ahead of
    # curing (rules S4 / E1).
    #
    # FIXED AT 72 h BY INSTRUCTION -- do not tune, do not override per SKU.
    # Import GT_SHELF_LIFE_H (below) rather than re-declaring 72.0 anywhere.
    #
    # Two controlled sources disagree with this figure and are deliberately NOT
    # applied: `Ageing spec-20.01.2024 rev12 (CTE0.06-FR.02)` gives 48 h for a
    # painted 2nd-stage GT and 24 h unpainted / TBR Super Single, and
    # `Recipemaster.MaxAging` gives 48 h for 493 SKUs. R5 of
    # `Building Business Rules.docx` says three days, which is what we follow.
    # Recorded so the divergence is visible, not silently lost.
    gt_shelf_life_h: float = 72.0
    # Target press utilisation when sizing demand. Queue wait diverges as load
    # approaches 1.0, so planning at capacity breaches the shelf life no matter
    # how good the sequencing is. The plant itself ran ~0.89 (PCR) / 0.98 (TBR).
    curing_load_target: float = 0.92
    # Plant rule B16: a GT whose demand for the horizon falls below this is not
    # worth a machine setup -- drop it rather than pay a changeover for a
    # handful of tyres.
    # DEAD for the live path -- read only by the retired planner/plan/demand.py:129.
    # The authoritative setting is `min_demand_units` below. Two live-looking copies
    # of one number is the duplicated-constant bug this project has been bitten by.
    min_demand_pcr: int = 300
    min_demand_tbr: int = 150
    # Rule C4: cap distinct GTs a press may serve in the horizon. Each extra GT
    # on a press is another 3.5-7h mould change, so a press serving a dozen GTs
    # burns more time changing than curing. Opening a spare press is cheaper.
    max_gts_per_press: int = 5
    # HARD: never release a green tyre more than this far ahead of the curing
    # that will consume it. A tyre not cured within gt_shelf_life_h is scrap, so
    # building faster than curing does not create inventory -- it creates waste.
    # Legacy open-loop pacer only. On the curing-first path the build lead is
    # NOT a tunable: shift_grid makes a tyre eligible in the shift after it is
    # built, so exactly one shift of lead is required and any more is pure age.
    # building.py derives it from SHIFT_S rather than reading this.
    gt_build_ahead_h: float = 24.0
    # See curing.py: extending a GT beyond its historical presses breaks C1 and
    # multiplies changeovers without fixing aging. Off unless deliberately set.
    allow_press_extension: bool = False
    # Open-loop CONWIP degenerates to a rate pacer (see building.py). Needs
    # curing scheduled first to supply a real release signal. Off by default.
    conwip_enabled: bool = False
    # Press packing budget as a fraction of the month. Plant runs 3 shifts 24/7,
    # and mould-change time is charged separately, so this is 1.0 -- distinct
    # from curing_load_target, which only trims DEMAND.
    press_budget_frac: float = 1.0
    # Campaign length in days for the windowed press assignment. Plant runs
    # ~1,166 units PCR / ~468 TBR per campaign, both about 8 days.
    # Windows are a substitute for the build corridor, not a complement -- with
    # dedication they are pure changeover cost. 31 = one window = no windowing.
    # 999 = a single window. Windowing is a substitute for the build corridor,
    # and with stock-pull it is pure changeover cost.
    cure_campaign_days: int = 999
    # Planning horizon H used to size n_g = N_g*tau_g/H. Makespan is a
    # CONSTRAINT (the month), never an objective.
    cure_horizon_days: int = 31
    # Common replenishment interval T_0. Lots are sized Q_g = r_g * T_0 so every
    # GT is rebuilt on the SAME cadence; a constant lot size instead makes the
    # gap scale as 1/r_g (11x spread across GTs) and starves slow-moving GTs.
    # 24h IN-WINDOW, not the 47h recovered from |G|*H/T_0 - |M| = 1519. That 47h
    # is a HORIZON AVERAGE over a ~50% duty cycle: 47 * 0.45..0.50 = 21..23h
    # active. Four independent measurements agree on ~24h -- 22.4+24.8 unique
    # SKUs built per day against ~45 live GTs, 88%/86% of GT-days with both
    # stages active, aging p95 28h ~= 0.95*24, and aging p50 4.8h with 64% cured
    # inside one shift. Building a GT every 47h while its presses drain it every
    # 24h is a 2x starvation generator applied to every GT.
    # 24 -> 12 (Aug 2026). The 24h derivation above is sound as a description of
    # the PLANT's cadence, but it is not the cost-minimising choice for us.
    # Inventory here is a Q/2 sawtooth -- I ~ half a run per GT -- so it is set
    # by BATCH SIZE, not by the base-stock target. Proof: cutting the controller
    # target 4x (4,650 -> 1,163) moved realized PCR inventory only 7,327 ->
    # 6,832, i.e. it floors. Halving T_0 halves Q_g and moves the floor itself.
    # Validated on four months, every metric improving and changeovers flat
    # (splitting a lot does not split the RUN -- adjacent same-GT lots merge, so
    # no changeover is added):
    #     month     PCR inv        cure ful%      aging p50    bld changeovers
    #     2026-03   8,296 -> 6,778  99.00 -> 99.26  13.1 -> 10.1  1,691 -> 1,699
    #     2026-05   6,156 -> 5,719  99.73 -> 99.85  11.1 ->  9.2  1,572 -> 1,589
    #     2026-06   7,391 -> 6,735  99.61 -> 99.67  12.8 -> 11.3  1,683 -> 1,702
    #     2026-07   7,327 -> 6,692  99.04 -> 99.35  12.6 -> 11.4  1,748 -> 1,735
    # Below 12h it REVERSES (8h: 6,843; 6h: 6,887) -- the run stops shrinking
    # because the merge floor is reached, while shorter lots add setup. 12h is a
    # measured optimum, not a fitted one; re-check it if lot merging changes.
    replenish_interval_h: float = 12.0
    # PLANT RULE (G7): daily green-tyre inventory must sit inside these bands,
    # EVERY day including the last. This is the closed-loop the engine lacked:
    # the daily identity built(g,d) = cured(g,d) holds WIP flat only if curing
    # executes perfectly, but presses lose ~11% of shifts to setup/starve/idle,
    # so the surplus accumulated monotonically -- PCR WIP ramped to 23,400 and
    # closing stock reached 4.8x opening. Capping build against projected WIP
    # converts that drift into honest shortfall.
    # Total band 5,700-6,300 also reconciles with the measured opening GT of
    # 5,948, i.e. the plant runs at steady state month to month.
    gt_wip_min: dict[str, int] = {"PCR": 4500, "TBR": 1200}
    gt_wip_max: dict[str, int] = {"PCR": 4800, "TBR": 1500}

    # ================= SINGLE SOURCE OF TRUTH FOR THE ENFORCED CAPS =========
    # These four dicts are the ONLY place these numbers exist. `l7_pull_release`
    # and `l11_validate_plan` both read them; neither defines its own copy.
    #
    # WHY THIS BLOCK EXISTS. The same limit used to be written twice with two
    # different values and nobody noticed:
    #   * TBR stock: l7 enforced a 1,400 rail while l11 graded against the 1,500
    #     G8 band upper. The tighter number silently won.
    #   * changeover minutes: l11 charged a flat (11.3, 42.4) to PCR when the
    #     plant master `v_changeover_build` says 28/60 (machines 1-5) and 22/42
    #     (6-11) -- so its "weighted changeover" invariant, AND the 30.2 gate
    #     derived from it, were both computed on wrong constants.
    # A duplicated constant is how the "effective ceiling 8,990 against a stated
    # 4,800" bug happened. Add a cap HERE or nowhere.

    # ENFORCED hard ceiling on daily GT stock, time-weighted daily mean.
    # DISTINCT FROM the G8 band above: the band is the business rule's target
    # range, this is the instructed hard cap and it may be TIGHTER (TBR 1,400 vs
    # the band's 1,500 upper). Enforced by l7's rail, reported by l11.
    # NB measured time-weighted, the plant itself runs a 4,832 mean and a 5,379
    # daily max on PCR, so 4,800 is tighter than the plant it imitates.
    gt_wip_rail: dict[str, int] = {"PCR": 4800, "TBR": 1400}

    # Headroom the rail keeps back so the STATED cap survives reconciliation.
    # l7's check is a pre-flight on an hourly grid; FIFO reallocation afterwards
    # drifts the realised profile up. At 0.99 the shipped daily max was 4,886
    # against a 4,800 rail once slices coarsened; 0.97 held it at 4,778, and the
    # derived slice rule (PARTITION §4h) needed 0.94 because lumpier arrivals
    # drift further. THE VALUE BELOW IS THE ONE IN FORCE -- this comment used to
    # say 0.97 beside a 0.94 value, which is the doc/code drift EXPERT_AUDIT §2c
    # found in the file whose stated purpose is preventing drift.
    # Measured at 0.94, July 2026: PCR daily max 4,611-4,636 against the 4,800
    # rail, TBR 1,291 against 1,400 -- both inside, i.e. the margin is currently
    # costing headroom it does not need. Raising it is EXPERT_AUDIT §5 item 4,
    # still open; reject the moment a stated cap breaches.
    gt_wip_rail_margin: float = 0.94

    # PLANT BENCHMARKS, July 2026, measured with the CORRECT per-machine
    # changeover minutes from `v_changeover_build`. l11 gates against these.
    #   PCR 279 machine-days · 742 changeovers · 344.3 h weighted
    #   TBR 250 machine-days · 889 changeovers · 148.2 h weighted
    plant_co_per_machine_day: dict[str, float] = {"PCR": 2.66, "TBR": 3.56}
    plant_weighted_co_min_per_machine_day: dict[str, float] = {"PCR": 74.0,
                                                               "TBR": 35.6}
    # HARD cap on daily GT inventory. 0 = OFF, and it ships OFF -- see below.
    # Set PLANNER_TH_GT_WIP_CAP or edit here to arm it.
    #
    # MEASURED INFEASIBLE AT PCR 5,000 / TBR 1,500 against the current press
    # campaign structure. Three enforcement mechanisms were built and all three
    # fail, for two distinct reasons:
    #
    #   1. Deferring PLACED lots (ledger envelope): a GT's press is mounted for
    #      a fixed window, so supply slipping more than its replenishment
    #      interval (T_c ~ 19h) misses the window and the cure is lost. A 22h
    #      mean deferral cost 80% of curing while inventory ROSE to 293,686.
    #   2. Trimming DAILY BUILD TARGETS: `remaining` takes the volume back but
    #      `want = cure_d + (I* - stock)` never draws the backlog down, so the
    #      same excess is re-deferred every day -- 5,155,577 tyres deferred over
    #      31 days against a 392,272-tyre month. Build ended at 19% of demand.
    #
    # The root cause is arithmetic, not implementation. A cap enforced by
    # trimming build cuts THROUGHPUT, not lead time: I = lambda x W, and
    # trimming reduces lambda. To cut I at constant lambda you must cut W.
    #   PCR draw 12,852/day = 536/h  ->  cap 5,000 = 9.3h of cover
    #   current W 14.35h -> 7,332 tyres.  Floor is tau + Tc(1-delta)/2 = 5.97h.
    # 9.3h sits ABOVE the floor, so the cap is reachable -- but only by removing
    # ~5h of phase lead first. Arm the cap AFTER W is under 9h, where it becomes
    # a harmless safety net instead of an output limiter.
    gt_wip_cap: dict[str, int] = {"PCR": 0, "TBR": 0}
    # MINIMUM RUNNABLE LOT (rule B12 / R9). A supervisor will not set up a
    # machine for a handful of tyres. Without a floor the plan emitted PCR p50
    # 76 and TBR p50 23, with 61%/98% of lots below 150 units and 101 lots of a
    # SINGLE tyre -- unrunnable as printed, and the direct cause of 6.88/8.78
    # runs per machine-day against the plant's 3.13/4.15.
    # A GT whose WHOLE MONTH is below the floor is exempt: it is a genuinely
    # tiny requirement, not a fragmentation artefact, and rounding it up would
    # breach G1.
    min_lot_units: dict[str, int] = {"PCR": 150, "TBR": 70}
    # SKIP GTs whose WHOLE-MONTH demand is below this. 0 disables.
    #
    # DISABLED -- THE PLANT BUILDS THESE. Demand here IS the plant's own July
    # output, so anything we refuse to plan is demand the plant demonstrably
    # satisfied. This rule discarded 17 GTs / 1,701 tyres outright, and
    # RULEBOOK.md 4b already recorded the plant building 8 PCR + 2 TBR GTs below
    # the threshold. The lot FLOOR (min_lot_units) is the real economic guard;
    # this one only deletes orders before they are ever considered.
    # Was {"PCR": 300, "TBR": 150}.
    # TURNED ON by plant instruction, Aug 2026. Was {0, 0}.
    # MEASURED COST on July 2026 is recorded in PARTITION_AND_CHANGEOVER §4l --
    # read it before changing this again. It was zeroed originally because it
    # discarded GTs the plant demonstrably built.
    # THIS IS THE AUTHORITATIVE MINIMUM DEMAND. `min_demand_pcr`/`_tbr` above are
    # the same rule in scalar form, read ONLY by the retired planner/plan/demand.py
    # -- do not add a reader for them.
    min_demand_units: dict[str, int] = {"PCR": 300, "TBR": 150}
    # NB: no rho_target here on purpose. rho = T*/H is measured against ONE
    # month's demand, so carrying it as a constant overfits every other month.
    # window_plan charges the mould change per campaign instead -- exact, and
    # it self-corrects with the actual fragmentation of the plan.
    # Press dedication is an OUTPUT of packing, not an input. See curing.py.
    dedicate_presses: bool = False
    # Each GT gets a staggered ACTIVE WINDOW of this fraction of the horizon.
    # Plant runs a GT on 13.9 of 31 days (45%) and is dormant the rest.
    gt_window_frac: float = 0.45
    # Mould cleaning. Measured from MouldCountLH in our own 8-month history --
    # the counter tops out at 1,344 (PCR) / 1,769 (TBR) cycles, NOT the 3,000
    # BTP assumes, so cleans here are ~2x more frequent. 480 min each.
    mould_clean_cycles_pcr: int = 1344
    mould_clean_cycles_tbr: int = 1769
    mould_clean_min: float = 480.0

    seq_min_support_frac: float = 0.01
    seq_max_pattern_len: int = 4

    cluster_max_size_full_perm: int = 8
    cluster_beam_width: int = 200


class OptimizerWeights(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLANNER_W_", env_file=".env", extra="ignore")

    missed_demand: float = 1000.0
    changeover_min: float = 1.0
    wip_area: float = 0.5
    curing_wait: float = 2.0
    soft_penalty: float = 5.0
    stability_edit: float = 0.5


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLANNER_", env_file=".env", extra="ignore")

    paths: Paths = Field(default_factory=Paths)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    weights: OptimizerWeights = Field(default_factory=OptimizerWeights)

    seed: int = 42
    log_level: str = "INFO"


CONFIG = Config()












# --------------------------------------------------------------------------
# GT shelf life: ONE constant, hardcoded, not env-overridable.
#
# `Thresholds.gt_shelf_life_h` is pydantic-settings and therefore reachable via
# PLANNER_TH_GT_SHELF_LIFE_H. The shelf life is a plant safety limit, not a
# tuning knob, so the engine reads this module constant instead and every
# module that needs it imports from here rather than re-declaring 72.0.
GT_SHELF_LIFE_H: float = 72.0
