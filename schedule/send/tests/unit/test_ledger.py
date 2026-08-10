"""GreenTireLedger basic invariants."""
from datetime import datetime, timedelta

from planner.plan.ledger import GreenTireLedger, GTEvent


def test_ledger_balance():
    l = GreenTireLedger()
    t = datetime(2026, 1, 1)
    l.add([GTEvent(ts=t, plant="PCR", gt_code="G1", qty_delta=10, source="build", lot_id="L1")])
    l.add([GTEvent(ts=t + timedelta(hours=1), plant="PCR", gt_code="G1",
                   qty_delta=-3, source="cure", lot_id="C1")])
    assert l.available("PCR", "G1", t + timedelta(hours=2)) == 7


def test_ledger_starvation():
    l = GreenTireLedger()
    t = datetime(2026, 1, 1)
    l.add([GTEvent(ts=t, plant="PCR", gt_code="G1", qty_delta=-1, source="cure", lot_id="C1")])
    st = l.starvations()
    assert st.height == 1
