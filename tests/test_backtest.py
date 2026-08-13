"""Backtest tests.

The value of a backtest is entirely in whether it can tell a real edge from a
drifting market, so most of these check that the base-rate comparison works and
that the verdicts are honest about small samples.
"""

from __future__ import annotations

from datetime import date as Date, timedelta

import pytest

from fpa.lots import rebuild, replay_lots
from fpa.money import to_paise
from fpa.rules.backtest import Backtester
from fpa.rules.engine import Rule
from fpa.rules.evaluator import Expression
from fpa.tax.engine import TaxEngine

from .conftest import TODAY

FY = "2026-27"
L = to_paise


def rule(name="r", when="unrealised_pct <= -20", **kw) -> Rule:
    return Rule(name=name, when=Expression(when), message="{name}",
                severity="high", cooldown_days=0, scope=kw.pop("scope", {}), **kw)


def price_series(add, iid, prices: list[float], *, end: str = TODAY) -> None:
    start = Date.fromisoformat(end) - timedelta(days=len(prices) - 1)
    for n, px in enumerate(prices):
        add.price(iid, px, date=(start + timedelta(days=n)).isoformat())


class TestReplayLots:
    def test_reconstructs_holdings_as_of_a_past_date(self, conn, add):
        i = add.instrument()
        add.buy(i, days_ago=500, qty=100, price_rs=100)
        add.buy(i, days_ago=200, qty=50, price_rs=200)
        add.sell(i, days_ago=100, qty=120, price_rs=300)
        rebuild(conn)

        early = (Date.fromisoformat(TODAY) - timedelta(days=300)).isoformat()
        assert sum(l.remaining_qty for l in replay_lots(conn, i, early)) == 100
        mid = (Date.fromisoformat(TODAY) - timedelta(days=150)).isoformat()
        assert sum(l.remaining_qty for l in replay_lots(conn, i, mid)) == 150
        assert sum(l.remaining_qty for l in replay_lots(conn, i, TODAY)) == 30

    def test_ignores_transactions_after_the_date(self, conn, add):
        i = add.instrument()
        add.buy(i, days_ago=10, qty=100, price_rs=100)
        rebuild(conn)
        old = (Date.fromisoformat(TODAY) - timedelta(days=50)).isoformat()
        assert replay_lots(conn, i, old) == []

    def test_handles_splits(self, conn, add):
        i = add.instrument()
        add.buy(i, days_ago=500, qty=10, price_rs=1000)
        conn.execute("INSERT INTO transactions (instrument_id, date, kind, quantity, amount)"
                     " VALUES (?,?,'SPLIT',5,0)", (i, "2026-01-01"))
        rebuild(conn)
        lots = replay_lots(conn, i, TODAY)
        assert lots[0].remaining_qty == 50
        assert lots[0].cost_per_unit == L(200)


class TestBaseRate:
    def test_a_rule_that_fires_before_falls_shows_negative_edge(self, conn, add):
        """Price rises then collapses; a drawdown rule should fire before the
        collapse and beat the base rate."""
        i = add.instrument(name="Faller")
        add.buy(i, days_ago=400, qty=100, price_rs=100)
        prices = [100 + n * 0.5 for n in range(200)] + [200 - n * 0.7 for n in range(200)]
        price_series(add, i, prices)
        rebuild(conn)

        res = Backtester(conn, TaxEngine(FY), horizons=(30, 90)).run(
            rule(when="drawdown_from_peak <= -10"), end=TODAY, step_days=7,
            start=(Date.fromisoformat(TODAY) - timedelta(days=390)).isoformat(),
        )
        assert res.count > 0
        assert res.base_rate[90] is not None
        assert res.edge(90) < 0        # did worse than an average window

    def test_edge_is_relative_not_absolute(self, conn, add):
        """In a steadily rising market a rule can be followed by gains and still
        have no edge — that is the whole reason the base rate exists."""
        i = add.instrument(name="Riser")
        add.buy(i, days_ago=400, qty=100, price_rs=100)
        price_series(add, i, [100 * (1.002 ** n) for n in range(400)])
        rebuild(conn)

        res = Backtester(conn, TaxEngine(FY), horizons=(90,)).run(
            rule(when="unrealised_pct > 0"), end=TODAY, step_days=7,
            start=(Date.fromisoformat(TODAY) - timedelta(days=390)).isoformat(),
        )
        assert res.median_forward(90) > 0       # absolute return looks great
        assert abs(res.edge(90)) < 3            # but there is no edge


class TestVerdicts:
    def test_never_fired_says_so(self, conn, add):
        i = add.instrument()
        add.buy(i, days_ago=400, qty=100, price_rs=100)
        price_series(add, i, [100.0] * 400)
        rebuild(conn)
        res = Backtester(conn, TaxEngine(FY)).run(
            rule(when="unrealised_pct <= -99"), end=TODAY, step_days=30)
        assert res.count == 0
        assert "Never fired" in res.verdict()

    def test_small_sample_refuses_to_conclude(self, conn, add):
        """A brief dip mid-history gives a handful of firings with real forward
        data. That is exactly when a backtest is most tempting to over-read."""
        i = add.instrument()
        add.buy(i, days_ago=500, qty=100, price_rs=100)
        # Flat, a 60-day dip well before the end, then flat again.
        prices = [100.0] * 200 + [50.0] * 60 + [100.0] * 240
        price_series(add, i, prices)
        rebuild(conn)

        res = Backtester(conn, TaxEngine(FY)).run(
            rule(when="unrealised_pct <= -40"), end=TODAY, step_days=30,
            start=(Date.fromisoformat(TODAY) - timedelta(days=490)).isoformat())
        assert 0 < res.count < 10
        assert res.median_forward(90) is not None      # forward data exists
        assert "too few to conclude" in res.verdict()
        assert any("anecdote" in n for n in res.notes)

    def test_insufficient_forward_history_is_distinguished_from_small_sample(self, conn, add):
        """A rule that only fires in the last few weeks cannot be judged at all,
        which is a different answer from 'too few firings'."""
        i = add.instrument()
        add.buy(i, days_ago=400, qty=100, price_rs=100)
        price_series(add, i, [100.0] * 380 + [50.0] * 20)
        rebuild(conn)
        res = Backtester(conn, TaxEngine(FY)).run(
            rule(when="unrealised_pct <= -40"), end=TODAY, step_days=30,
            start=(Date.fromisoformat(TODAY) - timedelta(days=390)).isoformat())
        assert res.count > 0
        assert "not enough forward history" in res.verdict()

    def test_portfolio_rules_are_not_backtested(self, conn, add):
        res = Backtester(conn, TaxEngine(FY)).run(
            rule(when="abs(drift_pct) > 5", scope={"level": "portfolio"}), end=TODAY)
        assert res.count == 0
        assert any("not backtested" in n for n in res.notes)


class TestMechanics:
    def test_forward_return_is_none_without_enough_future(self, conn, add):
        i = add.instrument()
        add.buy(i, days_ago=400, qty=100, price_rs=100)
        price_series(add, i, [100 - n * 0.1 for n in range(400)])
        rebuild(conn)
        res = Backtester(conn, TaxEngine(FY), horizons=(180,)).run(
            rule(when="unrealised_pct < 0"), end=TODAY, step_days=7)
        recent = [f for f in res.firings if f.date > "2026-06-01"]
        assert recent and all(f.forward[180] is None for f in recent)

    def test_firings_record_the_facts(self, conn, add):
        i = add.instrument(name="X")
        add.buy(i, days_ago=400, qty=100, price_rs=100)
        price_series(add, i, [70.0] * 400)
        rebuild(conn)
        res = Backtester(conn, TaxEngine(FY)).run(
            rule(when="unrealised_pct <= -20"), end=TODAY, step_days=30)
        assert res.count > 0
        assert "unrealised_pct" in res.firings[0].context

    def test_scope_limits_which_instruments_are_tested(self, conn, add):
        eq = add.instrument(name="Stock", kind="EQUITY")
        mf = add.instrument(name="Fund", kind="MF")
        for i in (eq, mf):
            add.buy(i, days_ago=400, qty=100, price_rs=100)
            price_series(add, i, [70.0] * 400)
        rebuild(conn)
        res = Backtester(conn, TaxEngine(FY)).run(
            rule(when="unrealised_pct <= -20", scope={"kind": "EQUITY"}),
            end=TODAY, step_days=60)
        assert {f.subject for f in res.firings} == {"Stock"}
