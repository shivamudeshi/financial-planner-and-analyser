"""Tax engine tests.

These encode the rules the planner depends on. If a Finance Act changes a rate,
update rates.yaml and these expectations together — a green suite against stale
rates is worse than no suite.
"""

from __future__ import annotations

import pytest

from fpa.money import to_paise
from fpa.tax.engine import Gain, Regime, TaxEngine, Term

L = to_paise


@pytest.fixture
def eng() -> TaxEngine:
    return TaxEngine("2025-26")


def lt(rupees: float) -> Gain:
    return Gain(L(rupees), Term.LONG, Regime.EQUITY)


def st(rupees: float) -> Gain:
    return Gain(L(rupees), Term.SHORT, Regime.EQUITY)


class TestExemption:
    def test_ltcg_under_exemption_is_free(self, eng):
        assert eng.compute([lt(100_000)]).total_tax == 0

    def test_ltcg_exactly_at_exemption_is_free(self, eng):
        assert eng.compute([lt(125_000)]).total_tax == 0

    def test_only_the_excess_is_taxed(self, eng):
        res = eng.compute([lt(225_000)])
        # (2,25,000 - 1,25,000) * 12.5% = 12,500, + 4% cess = 13,000
        assert res.total_tax == L(13_000)
        assert res.exemption_used == L(125_000)
        assert res.exemption_unused == 0

    def test_unused_exemption_is_reported(self, eng):
        res = eng.compute([lt(40_000)])
        assert res.exemption_unused == L(85_000)


class TestShortTerm:
    def test_stcg_taxed_from_the_first_rupee(self, eng):
        # No exemption applies to s.111A gains.
        res = eng.compute([st(10_000)])
        assert res.total_tax == L(2_080)  # 20% + 4% cess

    def test_stcg_and_ltcg_are_taxed_separately(self, eng):
        res = eng.compute([st(50_000), lt(125_000)])
        assert res.bucket("EQUITY_LTCG").taxable == 0
        assert res.bucket("EQUITY_STCG").taxable == L(50_000)
        assert res.total_tax == L(10_400)


class TestSetOff:
    def test_short_term_loss_offsets_short_term_gain(self, eng):
        assert eng.compute([st(50_000), st(-50_000)]).total_tax == 0

    def test_short_term_loss_may_offset_long_term_gain(self, eng):
        # STCL is the flexible one: s.70 lets it cross into long-term gains.
        res = eng.compute([lt(325_000), st(-200_000)])
        assert res.bucket("EQUITY_LTCG").setoff == L(200_000)
        assert res.total_tax == 0

    def test_long_term_loss_cannot_offset_short_term_gain(self, eng):
        res = eng.compute([st(100_000), lt(-100_000)])
        assert res.bucket("EQUITY_STCG").setoff == 0
        assert res.total_tax == L(20_800)
        assert res.carry_forward_ltcl == L(100_000)

    def test_loss_is_spent_on_the_highest_taxed_bucket_first(self, eng):
        # 20% short-term should absorb the loss before 12.5% long-term.
        res = eng.compute([st(100_000), lt(300_000), st(-100_000)])
        assert res.bucket("EQUITY_STCG").setoff == L(100_000)
        assert res.bucket("EQUITY_LTCG").setoff == 0

    def test_unabsorbed_loss_carries_forward(self, eng):
        res = eng.compute([st(20_000), st(-90_000)])
        assert res.total_tax == 0
        assert res.carry_forward_stcl == L(70_000)

    def test_brought_forward_loss_is_applied(self, eng):
        res = eng.compute([st(100_000)], bf_stcl=L(100_000))
        assert res.total_tax == 0
        assert res.bf_stcl_used == L(100_000)


class TestWastedRelief:
    def test_loss_against_exempt_gain_is_flagged(self, eng):
        """The trap the planner exists to avoid.

        LTCG of 1,00,000 is already fully exempt. Booking a 1,00,000 long-term
        loss changes the tax bill by nothing and destroys the loss.
        """
        without = eng.compute([lt(100_000)])
        with_loss = eng.compute([lt(100_000), Gain(L(-100_000), Term.LONG)])
        assert without.total_tax == with_loss.total_tax == 0
        assert with_loss.carry_forward_ltcl == 0  # the loss is simply gone
        assert any("exemption" in n for n in with_loss.notes)


class TestHoldingPeriod:
    def test_365_days_is_still_short_term(self, eng):
        assert eng.term_for(365, Regime.EQUITY) is Term.SHORT

    def test_366_days_is_long_term(self, eng):
        assert eng.term_for(366, Regime.EQUITY) is Term.LONG

    def test_days_remaining_to_long_term(self, eng):
        assert eng.days_to_long_term(300, Regime.EQUITY) == 66
        assert eng.days_to_long_term(400, Regime.EQUITY) == 0


class TestGrandfathering:
    def test_uses_higher_of_cost_and_capped_fmv(self, eng):
        # s.112A: cost = max(actual, min(FMV_2018, sale))
        assert eng.grandfathered_cost(L(100), L(500), L(800)) == L(500)

    def test_fmv_capped_at_sale_price(self, eng):
        assert eng.grandfathered_cost(L(100), L(500), L(300)) == L(300)

    def test_actual_cost_wins_when_higher(self, eng):
        assert eng.grandfathered_cost(L(900), L(500), L(800)) == L(900)


class TestRatesFile:
    def test_unknown_fy_fails_loudly(self):
        with pytest.raises(ValueError, match="No tax parameters"):
            TaxEngine("1999-00")
