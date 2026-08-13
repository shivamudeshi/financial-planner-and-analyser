"""Planner tests.

The load-bearing invariant is the first one: whatever the planner proposes, the
resulting tax must be exactly zero. Everything else is about proposing the
*best* such set.
"""

from __future__ import annotations

import pytest

from fpa.lots import rebuild
from fpa.money import to_paise
from fpa.planner.sell_planner import Mode, SellPlanner
from fpa.tax.engine import TaxEngine, Term

from .conftest import TODAY

L = to_paise
FY = "2026-27"


@pytest.fixture
def planner(conn):
    def build(mode_conn=None):
        return SellPlanner(mode_conn or conn, TaxEngine(FY), fy=FY, as_of=TODAY)

    return build


class TestZeroTaxInvariant:
    def test_plan_never_produces_tax(self, conn, add, planner):
        # A messy portfolio: long winners, short winners, a loser, prior sales.
        a = add.instrument(name="Long Winner", symbol="LW", priority=90)
        b = add.instrument(name="Short Winner", symbol="SW", priority=90)
        c = add.instrument(name="Loser", symbol="LS", priority=90)
        add.buy(a, days_ago=900, qty=1000, price_rs=100)
        add.buy(b, days_ago=60, qty=500, price_rs=200)
        add.buy(c, days_ago=400, qty=300, price_rs=500)
        add.price(a, 400)
        add.price(b, 350)
        add.price(c, 200)
        rebuild(conn)

        plan = planner().plan(Mode.EXIT)
        assert plan.tax == 0
        assert plan.final.total_tax == 0

    def test_zero_tax_holds_with_gains_already_booked(self, conn, add, planner):
        i = add.instrument(priority=90)
        add.buy(i, days_ago=900, qty=2000, price_rs=100)
        # Already realised 80k of long-term gain earlier this FY.
        add.sell(i, days_ago=30, qty=800, price_rs=200)
        add.price(i, 300)
        rebuild(conn)

        p = planner().plan(Mode.EXIT)
        assert p.tax == 0
        # Only ~45k of exemption was left, so it cannot realise the full 125k.
        assert p.gain_realised <= L(45_000) + 1


class TestExemptionUse:
    def test_fills_the_exemption_exactly(self, conn, add, planner):
        i = add.instrument(priority=90)
        add.buy(i, days_ago=900, qty=1000, price_rs=100)   # gain 200/share at 300
        add.price(i, 300)
        rebuild(conn)

        p = planner().plan(Mode.EXIT)
        assert p.gain_realised == L(125_000)      # 625 shares * 200
        assert p.actions[0].quantity == 625
        assert p.actions[0].partial is True
        assert p.final.exemption_unused == 0

    def test_sells_everything_when_gain_fits_under_the_limit(self, conn, add, planner):
        i = add.instrument(priority=90)
        add.buy(i, days_ago=900, qty=100, price_rs=100)    # gain 20,000 total
        add.price(i, 300)
        rebuild(conn)

        p = planner().plan(Mode.EXIT)
        assert p.actions[0].quantity == 100
        assert p.actions[0].partial is False
        assert p.final.exemption_unused == L(105_000)

    def test_warns_that_unused_exemption_expires(self, conn, add, planner):
        i = add.instrument(priority=90)
        add.buy(i, days_ago=900, qty=10, price_rs=100)
        add.price(i, 200)
        rebuild(conn)

        p = planner().plan(Mode.EXIT)
        assert any("does not carry forward" in n for n in p.notes)


class TestShortTermHandling:
    def test_will_not_sell_a_short_term_winner_into_tax(self, conn, add, planner):
        i = add.instrument(priority=95)
        add.buy(i, days_ago=100, qty=100, price_rs=100)
        add.price(i, 300)
        rebuild(conn)

        p = planner().plan(Mode.EXIT)
        # Any short-term gain is taxed from the first rupee, so nothing is sold.
        assert p.actions == []
        assert p.tax == 0

    def test_recommends_waiting_for_the_12_month_line(self, conn, add, planner):
        i = add.instrument(name="Nearly There", priority=95)
        add.buy(i, days_ago=300, qty=100, price_rs=100)
        add.price(i, 300)
        rebuild(conn)

        p = planner().plan(Mode.EXIT)
        assert len(p.deferrals) == 1
        d = p.deferrals[0]
        assert d.days_to_long_term == 66
        assert d.long_term_date == "2026-10-18"
        assert d.tax_if_sold_now == L(4_160)   # 20,000 * 20% * 1.04 cess

    def test_long_term_lots_are_preferred_over_short_term(self, conn, add, planner):
        lt = add.instrument(name="Old", symbol="OLD", priority=90)
        st = add.instrument(name="New", symbol="NEW", priority=90)
        add.buy(lt, days_ago=900, qty=100, price_rs=100)
        add.buy(st, days_ago=100, qty=100, price_rs=100)
        add.price(lt, 200)
        add.price(st, 200)
        rebuild(conn)

        p = planner().plan(Mode.EXIT)
        assert [a.symbol for a in p.actions] == ["OLD"]


class TestModeObjectives:
    """EXIT frees the most capital; HARVEST minimises turnover. Same budget,
    deliberately opposite lot preferences."""

    @pytest.fixture
    def two_lots(self, conn, add):
        cheap = add.instrument(name="Low Gain Pct", symbol="LOW", priority=80)
        rich = add.instrument(name="High Gain Pct", symbol="HIGH", priority=80)
        # LOW: 100 gain per 1100 of market value. HIGH: 200 gain per 300.
        add.buy(cheap, days_ago=900, qty=5000, price_rs=1000)
        add.buy(rich, days_ago=900, qty=5000, price_rs=100)
        add.price(cheap, 1100)
        add.price(rich, 300)
        rebuild(conn)

    def test_exit_mode_maximises_proceeds(self, conn, add, planner, two_lots):
        p = planner().plan(Mode.EXIT)
        assert p.actions[0].symbol == "LOW"
        # 1250 shares at 1100 = 13.75L freed for the same 1.25L of gain.
        assert p.proceeds == L(13_75_000)
        assert p.gain_realised == L(125_000)

    def test_harvest_mode_minimises_turnover(self, conn, add, planner, two_lots):
        p = planner().plan(Mode.HARVEST)
        assert p.actions[0].symbol == "HIGH"
        assert p.proceeds == L(187_500)          # far less capital disturbed
        assert p.gain_realised == L(125_000)     # same basis step-up

    def test_harvest_rationale_quantifies_future_saving(self, conn, add, planner, two_lots):
        p = planner().plan(Mode.HARVEST)
        assert "rebuy" in p.actions[0].rationale
        assert "future tax" in p.actions[0].rationale


class TestLossHandling:
    def test_books_losses_to_neutralise_an_already_taxable_year(self, conn, add, planner):
        winner = add.instrument(name="Sold Winner", symbol="WIN", priority=10)
        loser = add.instrument(name="Loser", symbol="LOSE", priority=10)
        add.buy(winner, days_ago=200, qty=1000, price_rs=100)
        add.sell(winner, days_ago=5, qty=1000, price_rs=200)   # 1L short-term gain, taxable
        add.buy(loser, days_ago=200, qty=1000, price_rs=200)
        add.price(loser, 100)
        add.price(winner, 200)
        rebuild(conn)

        p = planner().plan(Mode.EXIT)
        assert p.baseline.total_tax > 0        # the year starts in tax
        assert p.tax == 0                      # and the plan clears it
        assert any(a.symbol == "LOSE" for a in p.actions)

    def test_refuses_to_waste_a_loss_against_exempt_gain(self, conn, add, planner):
        """The trap: the year's gain is already exempt, so booking a loss now
        destroys it under mandatory set-off."""
        win = add.instrument(name="Small Winner", symbol="WIN", priority=10)
        lose = add.instrument(name="Unwanted", symbol="LOSE", priority=95)
        add.buy(win, days_ago=900, qty=1000, price_rs=100)
        add.sell(win, days_ago=5, qty=1000, price_rs=150)   # 50k LTCG, fully exempt
        add.buy(lose, days_ago=900, qty=1000, price_rs=200)
        add.price(lose, 150)
        add.price(win, 150)
        rebuild(conn)

        p = planner().plan(Mode.EXIT)
        assert not any(a.symbol == "LOSE" for a in p.actions)
        assert any("destroy relief" in w for w in p.warnings)

    def test_booking_a_loss_enlarges_the_zero_tax_budget(self, conn, add, planner):
        """A loss is not merely 'not wasted' — it creates headroom.

        With 3L of long-term gain available and a 1L loss on a position we want
        out of, the right plan books the loss *and* realises 2.25L of gain
        (1.25L exemption + 1L offset), rather than stopping at the exemption.
        """
        win = add.instrument(name="Winner", symbol="WIN", priority=70)
        lose = add.instrument(name="Unwanted", symbol="LOSE", priority=95)
        add.buy(win, days_ago=900, qty=1000, price_rs=100)
        add.price(win, 400)                                  # 3L unrealised gain
        add.buy(lose, days_ago=900, qty=1000, price_rs=200)
        add.price(lose, 100)                                 # 1L unrealised loss
        rebuild(conn)

        p = planner().plan(Mode.EXIT)
        assert p.tax == 0
        assert any(a.symbol == "LOSE" for a in p.actions)     # the loss is booked
        assert p.losses_booked == L(100_000)
        # 1.25L exemption + 1L sheltered by the loss.
        assert p.long_term_gain == L(225_000)
        assert p.gain_realised == L(125_000)                  # net of the loss

    def test_losses_are_capped_at_what_gains_can_absorb(self, conn, add, planner):
        """Booking more loss than there is gain to shelter destroys the surplus."""
        win = add.instrument(name="Winner", symbol="WIN", priority=70)
        lose = add.instrument(name="Unwanted", symbol="LOSE", priority=95)
        add.buy(win, days_ago=900, qty=1000, price_rs=100)
        add.price(win, 150)                                  # only 50k of gain
        add.buy(lose, days_ago=900, qty=1000, price_rs=300)
        add.price(lose, 100)                                 # 2L of loss
        rebuild(conn)

        p = planner().plan(Mode.EXIT)
        assert p.tax == 0
        assert not any(a.symbol == "LOSE" for a in p.actions)
        assert any("not enough unrealised gain" in w for w in p.warnings)

    def test_book_losses_can_be_disabled(self, conn, add, planner):
        lose = add.instrument(name="Unwanted", symbol="LOSE", priority=95)
        add.buy(lose, days_ago=900, qty=100, price_rs=200)
        add.price(lose, 150)
        rebuild(conn)

        p = planner().plan(Mode.EXIT, book_losses=False)
        assert p.actions == []


class TestScoping:
    def test_min_priority_filters_candidates(self, conn, add, planner):
        keep = add.instrument(name="Keeper", symbol="KEEP", priority=10)
        drop = add.instrument(name="Dropper", symbol="DROP", priority=90)
        for i in (keep, drop):
            add.buy(i, days_ago=900, qty=100, price_rs=100)
            add.price(i, 200)
        rebuild(conn)

        p = planner().plan(Mode.EXIT, min_priority=60)
        assert {a.symbol for a in p.actions} == {"DROP"}

    def test_outright_sale_cost_is_quantified(self, conn, add, planner):
        i = add.instrument(priority=90)
        add.buy(i, days_ago=900, qty=1000, price_rs=100)
        add.price(i, 400)     # 3L of long-term gain
        rebuild(conn)

        p = planner().plan(Mode.EXIT)
        # Selling the lot outright: (300,000 - 125,000) * 12.5% * 1.04 = 22,750
        assert p.tax_if_sold_outright == L(22_750)
        assert p.tax_saved == L(22_750)
