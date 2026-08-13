"""Opportunity-cost tests.

The point of this module is to be the honest counterweight to the planner, so
the cases that matter most are the ones where it says *don't*.
"""

from __future__ import annotations

import pytest

from fpa.lots import rebuild
from fpa.money import to_paise
from fpa.planner.opportunity import OpportunityAnalyser
from fpa.planner.sell_planner import Mode, SellPlanner
from fpa.tax.engine import TaxEngine

from .conftest import TODAY

L = to_paise
FY = "2026-27"


@pytest.fixture
def an(conn) -> OpportunityAnalyser:
    return OpportunityAnalyser(conn, TaxEngine(FY))


def _volatile_history(add, iid: int, *, swing: float, days: int = 120) -> None:
    """Alternating price series with a controlled per-day move."""
    from datetime import date as Date, timedelta

    start = Date.fromisoformat(TODAY) - timedelta(days=days)
    for n in range(days):
        px = 100 * (1 + swing) if n % 2 else 100 * (1 - swing)
        add.price(iid, round(px, 2), date=(start + timedelta(days=n)).isoformat())


class TestTransactionCosts:
    def test_equity_sell_includes_stt_and_dp(self, an):
        c = an.sell_costs(L(100_000), "EQUITY")
        assert c.stt == L(100)          # 0.1% of 1,00,000
        assert c.dp == L(15.93)
        assert c.total > c.stt

    def test_equity_buy_has_stamp_duty_but_no_dp(self, an):
        c = an.buy_costs(L(100_000), "EQUITY")
        assert c.stamp == L(15)         # 0.015%
        assert c.dp == 0

    def test_mf_costs_are_far_lower_than_equity(self, an):
        assert an.sell_costs(L(100_000), "MF").total < an.sell_costs(L(100_000), "EQUITY").total

    def test_exit_load_applies_within_a_year(self, an):
        early = an.sell_costs(L(100_000), "MF", days_held=100)
        late = an.sell_costs(L(100_000), "MF", days_held=400)
        assert early.exit_load == L(1_000)      # 1%
        assert late.exit_load == 0
        # Exit load dominates every other MF cost by orders of magnitude.
        assert early.total > late.total * 50

    def test_costs_add(self, an):
        a = an.sell_costs(L(100_000), "EQUITY")
        b = an.buy_costs(L(100_000), "EQUITY")
        assert (a + b).total == a.total + b.total


class TestHarvestEconomics:
    def test_future_saving_is_discounted_not_taken_at_face_value(self, conn, add, an):
        i = add.instrument()
        add.price(i, 100)
        h = an.harvest_economics(i, "X", "EQUITY", L(500_000), L(100_000))
        # 12.5% + cess on 1L = 13,000 nominal, discounted 5 years at 10%.
        assert h.future_tax_saved == L(13_000)
        assert h.future_tax_saved_pv < h.future_tax_saved
        assert h.future_tax_saved_pv == pytest.approx(L(8_072), rel=0.01)

    def test_harvest_is_rejected_when_costs_exceed_the_benefit(self, conn, add, an):
        """A large position with a tiny embedded gain: all cost, no benefit."""
        i = add.instrument()
        add.price(i, 100)
        h = an.harvest_economics(i, "X", "EQUITY", L(10_00_000), L(2_000))
        assert h.net_benefit < 0
        assert "Not worth it" in h.verdict

    def test_exit_load_can_sink_an_mf_harvest(self, conn, add, an):
        i = add.instrument(kind="MF")
        add.price(i, 100)
        held = an.harvest_economics(i, "F", "MF", L(500_000), L(100_000), days_held=100)
        free = an.harvest_economics(i, "F", "MF", L(500_000), L(100_000), days_held=400)
        assert held.costs.exit_load == L(5_000)
        assert held.net_benefit < free.net_benefit
        assert free.net_benefit > 0

    def test_holding_period_reset_is_always_flagged(self, conn, add, an):
        i = add.instrument()
        add.price(i, 100)
        assert an.harvest_economics(i, "X", "EQUITY", L(100_000), L(10_000)).resets_holding_period


class TestDeferralEconomics:
    def test_saving_is_the_rate_gap_when_gain_stays_taxable(self, conn, add, an):
        i = add.instrument()
        add.price(i, 100)
        d = an.deferral_economics(i, "X", L(1_000_000), L(100_000), 60)
        # (20% - 12.5%) * 1L * 1.04 cess = 7,800
        assert d.tax_saved == L(7_800)
        assert d.breakeven_decline_pct == pytest.approx(0.78, rel=0.01)

    def test_saving_is_the_full_rate_when_the_gain_becomes_exempt(self, conn, add, an):
        i = add.instrument()
        add.price(i, 100)
        d = an.deferral_economics(i, "X", L(1_000_000), L(100_000), 60, becomes_exempt=True)
        assert d.tax_saved == L(20_800)      # 20% + cess, avoided entirely

    def test_volatile_position_is_told_the_tax_saving_is_noise(self, conn, add, an):
        """The case this module exists for.

        A 0.8% tax saving against a large one-sigma move is not a reason to keep
        holding something you want to exit.
        """
        i = add.instrument()
        _volatile_history(add, i, swing=0.25)

        d = an.deferral_economics(i, "X", L(1_000_000), L(100_000), 60)
        assert d.volatility_pct is not None
        assert d.risk_multiple > 4
        assert "noise" in d.verdict
        assert "Decide on the merits, not the tax" in d.verdict

    def test_calm_position_gets_a_clear_case_for_waiting(self, conn, add, an):
        i = add.instrument()
        _volatile_history(add, i, swing=0.0005)

        d = an.deferral_economics(i, "X", L(1_000_000), L(100_000), 60)
        assert d.risk_multiple < 1.5
        assert "Clear case for waiting" in d.verdict

    def test_no_history_is_reported_rather_than_guessed(self, conn, add, an):
        i = add.instrument()
        add.price(i, 100)
        d = an.deferral_economics(i, "X", L(1_000_000), L(100_000), 60)
        assert d.volatility_pct is None
        assert "No volatility history" in d.verdict


class TestCostOfWaiting:
    def test_a_decline_can_dwarf_the_tax_saved(self, an):
        """The counterweight: 12.5% of gain saved against 10% of position lost."""
        tax, lost = an.cost_of_waiting(L(10_00_000), L(100_000), decline_pct=10)
        assert tax == L(13_000)
        assert lost == L(100_000)
        assert lost > tax * 7


class TestPlanRollup:
    def test_report_nets_costs_against_tax_saved(self, conn, add, an):
        i = add.instrument(priority=90)
        add.buy(i, days_ago=900, qty=1000, price_rs=100)
        add.price(i, 400)
        rebuild(conn)

        plan = SellPlanner(conn, TaxEngine(FY), fy=FY, as_of=TODAY).plan(Mode.EXIT)
        r = an.analyse(plan)
        assert r.tax_avoided_now > 0
        assert r.transaction_costs > 0
        assert r.net_benefit == (
            r.tax_avoided_now + r.future_tax_saved_pv - r.transaction_costs
        )

    def test_harvest_net_benefit_excludes_tax_avoided_now(self, conn, add, an):
        """Harvesting rebuys the position, so 'tax you would have paid selling
        out' is the wrong counterfactual — the alternative is doing nothing."""
        i = add.instrument(priority=90)
        add.buy(i, days_ago=900, qty=1000, price_rs=100)
        add.price(i, 400)
        rebuild(conn)

        p = SellPlanner(conn, TaxEngine(FY), fy=FY, as_of=TODAY).plan(Mode.HARVEST)
        r = an.analyse(p)
        assert p.tax_saved > 0                 # the plan itself reports a saving
        assert r.tax_avoided_now == 0          # but it does not count here
        assert r.net_benefit == r.future_tax_saved_pv - r.transaction_costs
        assert any("alternative is doing nothing" in n for n in r.notes)

    def test_exit_load_is_charged_per_lot_not_per_order(self, an):
        """A three-year SIP redemption is mostly load-free; only the last year's
        instalments are charged."""
        lots = [(L(100_000), 900), (L(100_000), 500), (L(100_000), 100)]
        c = an.sell_costs_for_lots(lots, "MF")
        assert c.exit_load == L(1_000)         # 1% of only the 100-day lot
        # Charging the whole order at the youngest lot's age would be 3x that.
        assert an.sell_costs(L(300_000), "MF", days_held=100).exit_load == L(3_000)

    def test_harvest_mode_reports_per_instrument_economics(self, conn, add, an):
        i = add.instrument(priority=90)
        add.buy(i, days_ago=900, qty=1000, price_rs=100)
        add.price(i, 400)
        rebuild(conn)

        plan = SellPlanner(conn, TaxEngine(FY), fy=FY, as_of=TODAY).plan(Mode.HARVEST)
        r = an.analyse(plan)
        assert r.harvests
        assert r.future_tax_saved_pv > 0
        assert any("fresh 12-month holding period" in n for n in r.notes)
        assert any("exemption in the year you eventually sell" in n for n in r.notes)
