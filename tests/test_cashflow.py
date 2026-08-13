from __future__ import annotations

import pytest

from fpa.money import to_paise
from fpa.planner import cashflow
from fpa.planner.cashflow import SipPlan

L = to_paise


def plan(**kw) -> SipPlan:
    base = dict(id=None, instrument_id=None, label="SIP", amount=L(17_000),
                day_of_month=5, start_date="2026-04-05", end_date=None,
                step_up_pct=0.0, step_up_cap=None)
    return SipPlan(**{**base, **kw})


class TestSchedule:
    def test_twelve_instalments_in_a_full_year(self):
        got = plan().instalments("2026-04-01", "2027-03-31")
        assert len(got) == 12
        assert got[0].date == "2026-04-05"
        assert got[-1].date == "2027-03-05"

    def test_all_instalments_equal_without_step_up(self):
        assert {i.amount for i in plan().instalments("2026-04-01", "2027-03-31")} == {L(17_000)}

    def test_day_clamps_to_short_months(self):
        got = plan(day_of_month=31, start_date="2026-01-31").instalments(
            "2026-02-01", "2026-03-31")
        assert [i.date for i in got] == ["2026-02-28", "2026-03-31"]

    def test_start_and_end_dates_bound_the_schedule(self):
        got = plan(start_date="2026-07-05", end_date="2026-09-30").instalments(
            "2026-04-01", "2027-03-31")
        assert [i.date for i in got] == ["2026-07-05", "2026-08-05", "2026-09-05"]


class TestStepUp:
    def test_step_up_applies_on_the_anniversary_not_on_1_april(self):
        """AMCs step up on the SIP's own anniversary, so a mid-year start means
        the increase lands mid-financial-year."""
        p = plan(start_date="2026-07-05", step_up_pct=10)
        got = p.instalments("2027-04-01", "2028-03-31")
        by_date = {i.date: i.amount for i in got}
        assert by_date["2027-06-05"] == L(17_000)      # still year 0
        assert by_date["2027-07-05"] == L(18_700)      # first anniversary, +10%

    def test_compounds_across_years(self):
        p = plan(step_up_pct=10)
        assert p.amount_in_year(0) == L(17_000)
        assert p.amount_in_year(1) == L(18_700)
        assert p.amount_in_year(2) == L(20_570)
        assert p.amount_in_year(10) == pytest.approx(L(44_093), rel=1e-4)

    def test_cap_limits_the_step_up(self):
        p = plan(step_up_pct=10, step_up_cap=L(25_000))
        assert p.amount_in_year(3) == L(22_627)
        assert p.amount_in_year(10) == L(25_000)

    def test_ten_year_projection_shows_what_step_up_commits_you_to(self):
        """The number worth seeing before committing: 10% annual step-up on
        ₹17k contributes far more over a decade than a flat SIP."""
        flat = cashflow.project([plan()], 10, start="2026-04-01")
        stepped = cashflow.project([plan(step_up_pct=10)], 10, start="2026-04-01")
        assert sum(a for _, a in flat) == L(20_40_000)          # 17k x 120
        assert sum(a for _, a in stepped) == pytest.approx(L(32_51_558), rel=1e-3)
        assert stepped[-1][1] > flat[-1][1] * 2


class TestFyCashflow:
    def test_planned_totals_the_year(self, conn):
        cashflow.add_plan(conn, 17_000, day_of_month=5, start_date="2026-04-05")
        flow = cashflow.fy_cashflow(conn, "2026-27", as_of="2026-08-13")
        assert flow.planned == L(2_04_000)          # 17k x 12
        assert flow.monthly_average == L(17_000)

    def test_actual_counts_only_sip_transactions(self, conn, add):
        """A one-off lump sum must not masquerade as instalments you never paid."""
        i = add.instrument(kind="MF")
        cashflow.add_plan(conn, 17_000, instrument_id=i, day_of_month=5,
                          start_date="2026-04-05")
        conn.execute(
            "INSERT INTO transactions (instrument_id, date, kind, quantity, price, amount)"
            " VALUES (?,?, 'SIP', 10, ?, ?)", (i, "2026-05-05", L(1_700), L(17_000)))
        add.buy(i, days_ago=30, qty=100, price_rs=1_700)     # lump sum, not a SIP
        flow = cashflow.fy_cashflow(conn, "2026-27", as_of="2026-08-13")
        assert flow.actual == L(17_000)
        assert flow.completion_pct == pytest.approx(8.33, rel=0.01)

    def test_behind_by_separates_missed_from_not_yet_due(self, conn):
        """Early in the year a big shortfall is normal — most instalments are
        simply not due yet. Only unpaid *due* instalments mean you are behind."""
        cashflow.add_plan(conn, 17_000, day_of_month=5, start_date="2026-04-05")
        flow = cashflow.fy_cashflow(conn, "2026-27", as_of="2026-08-13")
        assert flow.remaining_scheduled == L(1_19_000)     # Sep-Mar, 7 instalments
        assert flow.shortfall == L(2_04_000)               # nothing recorded as SIP
        assert flow.behind_by == L(85_000)                 # Apr-Aug, 5 due and unpaid

    def test_remaining_contribution_is_reported(self, conn):
        cashflow.add_plan(conn, 17_000, day_of_month=5, start_date="2026-04-05")
        flow = cashflow.fy_cashflow(conn, "2026-27", as_of="2026-08-13")
        # Aug 13: Apr-Aug paid (5), Sep-Mar remaining (7).
        assert any("₹1,19,000 still to be invested" in n for n in flow.notes)

    def test_mid_year_step_up_is_called_out(self, conn):
        cashflow.add_plan(conn, 17_000, day_of_month=5, start_date="2025-07-05",
                          step_up_pct=10)
        flow = cashflow.fy_cashflow(conn, "2026-27", as_of="2026-08-13")
        assert any("step-up takes effect" in n for n in flow.notes)

    def test_no_plans_is_reported_not_silently_zero(self, conn):
        flow = cashflow.fy_cashflow(conn, "2026-27")
        assert flow.planned == 0
        assert any("No SIP plans recorded" in n for n in flow.notes)


class TestDeployable:
    def test_combines_sale_proceeds_with_remaining_sip(self, conn):
        cashflow.add_plan(conn, 17_000, day_of_month=5, start_date="2026-04-05")
        flow = cashflow.fy_cashflow(conn, "2026-27", as_of="2026-08-13")
        total, notes = cashflow.deployable(L(50_000), flow, "2026-08-13")
        assert total == L(50_000) + L(1_19_000)
        assert "to deploy" in notes[0]

    def test_warns_when_selling_was_unnecessary(self, conn):
        """If the remaining SIP already exceeds what a sale would free,
        redirecting it is cheaper than selling."""
        cashflow.add_plan(conn, 17_000, day_of_month=5, start_date="2026-04-05")
        flow = cashflow.fy_cashflow(conn, "2026-27", as_of="2026-08-13")
        _, notes = cashflow.deployable(L(10_000), flow, "2026-08-13")
        assert any("redirecting SIP is cheaper than selling" in n for n in notes)
