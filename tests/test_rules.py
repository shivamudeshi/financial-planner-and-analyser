"""Rules engine tests.

Two behaviours carry the most weight: alerts must not repeat themselves into
uselessness, and every alert must record *why* it fired.
"""

from __future__ import annotations

import json

import pytest

from fpa.lots import rebuild
from fpa.rules.engine import Rule, RulesEngine, describe, load_rules
from fpa.rules.evaluator import Expression
from fpa.tax.engine import TaxEngine

from .conftest import TODAY

FY = "2026-27"


def rule(name="test_rule", when="unrealised_pct <= -20", **kw) -> Rule:
    defaults = dict(message="{name} down {unrealised_pct:.1f}%", severity="high",
                    cooldown_days=30, scope={}, enabled=True)
    return Rule(name=name, when=Expression(when), **{**defaults, **kw})


@pytest.fixture
def loser(conn, add):
    """A position 30% under water."""
    i = add.instrument(name="Falling Co", symbol="FALL", priority=70)
    add.buy(i, days_ago=500, qty=100, price_rs=1_000)
    add.price(i, 700)
    rebuild(conn)
    return i


@pytest.fixture
def engine(conn):
    def build(rules):
        return RulesEngine(conn, rules, tax_engine=TaxEngine(FY))
    return build


class TestFiring:
    def test_rule_fires_on_a_matching_position(self, conn, loser, engine):
        r = engine([rule()]).run(TODAY)
        assert len(r.fired) == 1
        assert r.fired[0].subject == "Falling Co"
        assert r.fired[0].severity == "high"

    def test_message_is_formatted_from_the_context(self, conn, loser, engine):
        assert engine([rule()]).run(TODAY).fired[0].message == "Falling Co down -30.0%"

    def test_non_matching_rule_stays_quiet(self, conn, loser, engine):
        assert engine([rule(when="unrealised_pct > 50")]).run(TODAY).fired == []

    def test_disabled_rule_does_not_run(self, conn, loser, engine):
        assert engine([rule(enabled=False)]).run(TODAY).fired == []

    def test_bad_message_placeholder_degrades_instead_of_crashing(self, conn, loser, engine):
        r = engine([rule(message="{nonexistent_field} broke")]).run(TODAY)
        assert len(r.fired) == 1
        assert "test_rule" in r.fired[0].message


class TestScoping:
    def test_kind_scope_filters(self, conn, add, engine):
        eq = add.instrument(name="Stock", kind="EQUITY")
        mf = add.instrument(name="Fund", kind="MF")
        for i in (eq, mf):
            add.buy(i, days_ago=500, qty=100, price_rs=1_000)
            add.price(i, 700)
        rebuild(conn)

        r = engine([rule(scope={"kind": "EQUITY"})]).run(TODAY)
        assert [a.subject for a in r.fired] == ["Stock"]

    def test_min_priority_scope_filters(self, conn, add, engine):
        low = add.instrument(name="Keeper", priority=10)
        high = add.instrument(name="Dropper", priority=90)
        for i in (low, high):
            add.buy(i, days_ago=500, qty=100, price_rs=1_000)
            add.price(i, 700)
        rebuild(conn)

        r = engine([rule(scope={"min_priority": 60})]).run(TODAY)
        assert [a.subject for a in r.fired] == ["Dropper"]

    def test_list_scope_accepts_any_member(self, conn, loser, engine):
        assert engine([rule(scope={"kind": ["EQUITY", "MF"]})]).run(TODAY).fired

    def test_portfolio_rules_do_not_run_against_instruments(self, conn, loser, engine):
        r = engine([rule(when="abs(drift_pct) > 5", scope={"level": "portfolio"})]).run(TODAY)
        assert r.fired == []       # no plan_targets set, so nothing to drift from

    def test_portfolio_rule_fires_on_allocation_drift(self, conn, loser, engine):
        conn.execute("INSERT INTO plan_targets (fy, asset_class, target_pct)"
                     " VALUES (?,'EQUITY',60)", (FY,))
        r = engine([rule(name="drift", when="abs(drift_pct) >= 7",
                         scope={"level": "portfolio"},
                         message="{asset_class} off target by {drift_pct:+.0f}")]).run(TODAY)
        assert len(r.fired) == 1
        assert r.fired[0].message == "EQUITY off target by +40"


class TestNoiseControl:
    def test_the_same_condition_does_not_fire_twice(self, conn, loser, engine):
        e = engine([rule()])
        assert len(e.run(TODAY).fired) == 1
        second = e.run("2026-08-14")
        assert second.fired == []
        assert second.already_open == 1

    def test_cooldown_suppresses_refiring_after_the_alert_is_closed(self, conn, loser, engine):
        e = engine([rule(cooldown_days=30)])
        e.run(TODAY)
        e.set_status(1, "ACTED")            # close it
        within = e.run("2026-08-20")        # 7 days later, inside cooldown
        assert within.fired == []
        assert within.suppressed_cooldown == 1

    def test_it_can_fire_again_once_the_cooldown_lapses(self, conn, loser, engine):
        e = engine([rule(cooldown_days=30)])
        e.run(TODAY)
        e.set_status(1, "ACTED")
        assert len(e.run("2026-10-01").fired) == 1

    def test_zero_cooldown_only_relies_on_the_open_alert_check(self, conn, loser, engine):
        e = engine([rule(cooldown_days=0)])
        e.run(TODAY)
        e.set_status(1, "ACTED")
        assert len(e.run("2026-08-14").fired) == 1

    def test_muting_stops_a_rule_reappearing(self, conn, loser, engine):
        e = engine([rule(cooldown_days=0)])
        e.run(TODAY)
        e.set_status(1, "MUTED")
        # Muted alerts are excluded from cooldown, but a re-fire is expected —
        # muting is per alert, not per rule. It is simply out of the open list.
        e.run("2026-08-14")
        assert [a.status for a in e.open_alerts()] == ["NEW"]
        assert len(e.open_alerts(include_muted=True)) == 2


class TestExplainability:
    def test_alert_records_the_facts_that_fired_it(self, conn, loser, engine):
        engine([rule()]).run(TODAY)
        stored = json.loads(conn.execute("SELECT context FROM alerts").fetchone()[0])
        assert stored["unrealised_pct"] == pytest.approx(-30.0, abs=0.01)
        assert stored["__subject__"] == "Falling Co"
        assert stored["__condition__"] == "unrealised_pct <= -20"

    def test_snapshot_keeps_only_referenced_fields(self, conn, loser, engine):
        engine([rule(when="unrealised_pct <= -20 and days_held > 100")]).run(TODAY)
        stored = json.loads(conn.execute("SELECT context FROM alerts").fetchone()[0])
        facts = {k for k in stored if not k.startswith("__")}
        assert facts == {"unrealised_pct", "days_held"}


class TestLifecycle:
    def test_statuses_move_through_the_lifecycle(self, conn, loser, engine):
        e = engine([rule()])
        e.run(TODAY)
        assert e.open_alerts()[0].status == "NEW"
        e.set_status(1, "ACKED")
        assert e.open_alerts()[0].status == "ACKED"
        e.set_status(1, "ACTED")
        assert e.open_alerts() == []

    def test_unknown_status_is_rejected(self, conn, loser, engine):
        e = engine([rule()])
        e.run(TODAY)
        with pytest.raises(ValueError, match="Unknown status"):
            e.set_status(1, "PROBABLY")

    def test_dry_run_persists_nothing(self, conn, loser, engine):
        r = engine([rule()]).run(TODAY, persist=False)
        assert len(r.fired) == 1
        assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


class TestRuleFile:
    def test_shipped_rules_all_compile(self):
        rules, warnings = load_rules()
        assert rules, "the default rules file should not be empty"
        assert warnings == []

    def test_shipped_rules_reference_only_real_fields(self):
        from fpa.rules.context import FIELDS

        rules, _ = load_rules()
        for r in rules:
            unknown = r.when.names - set(FIELDS)
            assert not unknown, f"rule {r.name!r} uses unknown field(s) {unknown}"

    def test_a_broken_rule_does_not_disable_the_others(self, tmp_path):
        path = tmp_path / "rules.yaml"
        path.write_text(
            "- name: fine\n  when: 'unrealised_pct < 0'\n"
            "- name: broken\n  when: '__import__(\"os\")'\n"
            "- name: also_fine\n  when: 'days_held > 10'\n"
        )
        rules, warnings = load_rules(path)
        assert [r.name for r in rules] == ["fine", "also_fine"]
        assert any("broken" in w for w in warnings)

    def test_duplicate_names_are_reported(self, tmp_path):
        path = tmp_path / "rules.yaml"
        path.write_text("- name: dup\n  when: 'a > 1'\n- name: dup\n  when: 'a > 2'\n")
        rules, warnings = load_rules(path)
        assert len(rules) == 1
        assert any("Duplicate" in w for w in warnings)

    def test_unknown_severity_falls_back_and_warns(self, tmp_path):
        path = tmp_path / "rules.yaml"
        path.write_text("- name: r\n  when: 'a > 1'\n  severity: catastrophic\n")
        rules, warnings = load_rules(path)
        assert rules[0].severity == "medium"
        assert any("severity" in w for w in warnings)

    def test_describe_renders_every_rule(self):
        rules, _ = load_rules()
        assert len(describe(rules)) == len(rules)


class TestMissingData:
    def test_a_fund_without_technicals_does_not_fire_equity_rules(self, conn, add, engine):
        """A NAV series has no RSI. The rule must stay quiet, not crash."""
        mf = add.instrument(name="Fund", kind="MF")
        add.buy(mf, days_ago=500, qty=100, price_rs=100)
        add.price(mf, 90)
        rebuild(conn)
        assert engine([rule(when="rsi14 > 70")]).run(TODAY).fired == []
