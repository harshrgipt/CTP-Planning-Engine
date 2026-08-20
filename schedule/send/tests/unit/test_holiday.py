"""The holiday calendar's arithmetic, and the OFF path that must stay identity.

The whole feature rests on two claims: with no calendar configured every
function is the identity (so every existing run is byte-identical), and with one
configured the plant-day window is 07:00 -> 07:00 rather than midnight. Both are
cheap to assert and expensive to discover wrong in a shipped pack.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from planner.cmbc import holiday


@pytest.fixture()
def off(monkeypatch):
    monkeypatch.setenv("PLANNER_HOLIDAYS", "")
    holiday.load("2026-08")
    yield
    monkeypatch.delenv("PLANNER_HOLIDAYS", raising=False)
    holiday.load("2026-08")


@pytest.fixture()
def aug15(monkeypatch):
    monkeypatch.setenv("PLANNER_HOLIDAYS", "2026-08-15")
    holiday.load("2026-08")
    yield
    monkeypatch.delenv("PLANNER_HOLIDAYS", raising=False)
    holiday.load("2026-08")


def test_off_is_identity(off):
    assert holiday.ACTIVE is False
    t = datetime(2026, 8, 15, 12, 0)
    assert holiday.next_free("PCR", t) == t
    assert holiday.free_before("PCR", t) == t
    assert holiday.add_work("PCR", t, 3600) == t + timedelta(hours=1)
    assert holiday.sub_work("PCR", t, 3600) == t - timedelta(hours=1)
    assert holiday.fit_before("PCR", t, timedelta(hours=5)) == t
    assert holiday.work_seconds("PCR", t, t + timedelta(days=2)) == 2 * 86400
    assert holiday.blocked_seconds("PCR", t, t + timedelta(days=2)) == 0.0


def test_window_is_the_plant_day_not_the_calendar_day(aug15):
    # 06:00 on the 15th is still the 14th's C shift -- the plant is OPEN.
    assert holiday.is_blocked("PCR", datetime(2026, 8, 15, 6, 0)) is False
    assert holiday.is_blocked("PCR", datetime(2026, 8, 15, 7, 0)) is True
    assert holiday.is_blocked("PCR", datetime(2026, 8, 16, 6, 59)) is True
    assert holiday.is_blocked("PCR", datetime(2026, 8, 16, 7, 0)) is False


def test_next_free_and_free_before(aug15):
    mid = datetime(2026, 8, 15, 15, 0)
    assert holiday.next_free("PCR", mid) == datetime(2026, 8, 16, 7, 0)
    assert holiday.free_before("PCR", mid) == datetime(2026, 8, 15, 7, 0)
    open_ts = datetime(2026, 8, 13, 9, 0)
    assert holiday.next_free("PCR", open_ts) == open_ts
    assert holiday.free_before("PCR", open_ts) == open_ts


def test_add_work_pauses_and_resumes(aug15):
    # 4 h of work starting 05:00 on the 15th: 2 h before the shutdown, then the
    # remaining 2 h from 07:00 on the 16th. End moves +24 h, work is unchanged.
    st = datetime(2026, 8, 15, 5, 0)
    assert holiday.add_work("PCR", st, 4 * 3600) == datetime(2026, 8, 16, 9, 0)
    # entirely before -- untouched
    assert holiday.add_work("PCR", st, 3600) == datetime(2026, 8, 15, 6, 0)
    # starting INSIDE the closure: work begins when the plant reopens
    assert holiday.add_work("PCR", datetime(2026, 8, 15, 12, 0), 3600) \
        == datetime(2026, 8, 16, 8, 0)


def test_sub_work_is_the_inverse(aug15):
    for secs in (3600, 4 * 3600, 30 * 3600, 100 * 3600):
        end = datetime(2026, 8, 17, 3, 0)
        st = holiday.sub_work("PCR", end, secs)
        assert holiday.add_work("PCR", st, secs) == end


def test_work_seconds_excludes_the_closure(aug15):
    a, b = datetime(2026, 8, 14, 7, 0), datetime(2026, 8, 17, 7, 0)
    assert holiday.work_seconds("PCR", a, b) == 2 * 86400
    assert holiday.blocked_seconds("PCR", a, b) == 86400


def test_fit_before_pulls_a_run_clear_of_the_closure(aug15):
    dur = timedelta(hours=5)
    # a run that would straddle the opening is pulled back to end AT 07:00
    st = holiday.fit_before("PCR", datetime(2026, 8, 15, 4, 0), dur)
    assert st + dur == datetime(2026, 8, 15, 7, 0)
    # a run wholly inside is pulled clear too
    st = holiday.fit_before("PCR", datetime(2026, 8, 15, 20, 0), dur)
    assert st + dur <= datetime(2026, 8, 15, 7, 0)
    # a run wholly after is untouched
    late = datetime(2026, 8, 16, 9, 0)
    assert holiday.fit_before("PCR", late, dur) == late


def test_consecutive_holidays_merge_into_one_window(monkeypatch):
    monkeypatch.setenv("PLANNER_HOLIDAYS", "2026-08-15,2026-08-16")
    holiday.load("2026-08")
    assert holiday.windows("PCR") == [(datetime(2026, 8, 15, 7, 0),
                                       datetime(2026, 8, 17, 7, 0))]
    assert holiday.add_work("PCR", datetime(2026, 8, 15, 6, 0), 2 * 3600) \
        == datetime(2026, 8, 17, 8, 0)
    monkeypatch.delenv("PLANNER_HOLIDAYS", raising=False)
    holiday.load("2026-08")


def test_per_plant_holidays(monkeypatch):
    monkeypatch.setenv("PLANNER_HOLIDAYS", "PCR:2026-08-15")
    holiday.load("2026-08")
    t = datetime(2026, 8, 15, 12, 0)
    assert holiday.is_blocked("PCR", t) is True
    assert holiday.is_blocked("TBR", t) is False
    assert holiday.add_work("TBR", t, 3600) == t + timedelta(hours=1)
    monkeypatch.delenv("PLANNER_HOLIDAYS", raising=False)
    holiday.load("2026-08")


def test_no_file_no_env_means_inactive(monkeypatch, tmp_path):
    monkeypatch.delenv("PLANNER_HOLIDAYS", raising=False)
    holiday.load("1999-01")
    assert holiday.ACTIVE is False
    assert holiday.summary() == "PLANT HOLIDAYS: none"
    assert os.environ.get("PLANNER_HOLIDAYS") is None
