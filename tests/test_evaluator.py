"""Evaluator tests.

The security tests matter most: a rules file is configuration, it gets copied
between machines and pasted from notes, and it must never be able to run
arbitrary code.
"""

from __future__ import annotations

import pytest

from fpa.rules.evaluator import Expression, RuleEvaluationError, RuleSyntaxError


class TestSecurity:
    @pytest.mark.parametrize("source", [
        "__import__('os').system('rm -rf /')",
        "().__class__.__bases__[0].__subclasses__()",
        "open('/etc/passwd').read()",
        "globals()",
        "eval('1+1')",
        "exec('x=1')",
        "getattr(x, 'y')",
        "[c for c in ().__class__.__mro__]",
        "lambda: 1",
        "x.attribute",
        "x[0]",
        "(1).__class__",
    ])
    def test_dangerous_expressions_are_rejected_at_parse_time(self, source):
        with pytest.raises(RuleSyntaxError):
            Expression(source)

    def test_only_whitelisted_functions_are_callable(self):
        with pytest.raises(RuleSyntaxError, match="Unknown function"):
            Expression("print(1)")
        assert Expression("abs(-5) == 5").evaluate({})

    def test_keyword_arguments_are_refused(self):
        with pytest.raises(RuleSyntaxError, match="Keyword arguments"):
            Expression("round(1.5, ndigits=0)")

    def test_malformed_syntax_is_reported_clearly(self):
        with pytest.raises(RuleSyntaxError, match="Could not parse"):
            Expression("a >< b")


class TestEvaluation:
    @pytest.mark.parametrize("source,ctx,expected", [
        ("a > 5", {"a": 10}, True),
        ("a > 5", {"a": 1}, False),
        ("a > 5 and b < 2", {"a": 10, "b": 1}, True),
        ("a > 5 or b < 2", {"a": 1, "b": 1}, True),
        ("not a", {"a": False}, True),
        ("-15 <= a <= 15", {"a": 0}, True),          # chained comparison
        ("-15 <= a <= 15", {"a": 20}, False),
        ("a in ['X', 'Y']", {"a": "X"}, True),
        ("a not in ['X', 'Y']", {"a": "Z"}, True),
        ("abs(a) > 10", {"a": -20}, True),
        ("max(a, b) == 7", {"a": 3, "b": 7}, True),
        ("a * 2 + 1 == 7", {"a": 3}, True),
        ("(a if b else c) == 1", {"a": 1, "b": True, "c": 2}, True),
    ])
    def test_supported_expressions(self, source, ctx, expected):
        assert Expression(source).evaluate(ctx) is expected

    def test_and_short_circuits_so_guards_work(self):
        """`has_history and rsi14 < 30` must not fail when rsi14 is absent."""
        expr = Expression("has_history and rsi14 < 30")
        assert expr.evaluate({"has_history": False}) is False

    def test_or_short_circuits(self):
        assert Expression("always or missing_field").evaluate({"always": True}) is True


class TestMissingData:
    def test_unknown_field_does_not_fire_and_names_itself(self):
        expr = Expression("nonexistent > 5")
        assert expr.evaluate({}) is False
        with pytest.raises(RuleEvaluationError, match="Unknown field 'nonexistent'"):
            expr({})

    def test_none_reading_does_not_fire(self):
        """A fund with too little history has rsi14=None. That must mean 'no
        signal', not a crash and not a spurious fire."""
        assert Expression("rsi14 < 30").evaluate({"rsi14": None}) is False

    def test_division_by_zero_does_not_fire(self):
        assert Expression("a / b > 1").evaluate({"a": 1, "b": 0}) is False

    def test_missing_from_lists_absent_fields(self):
        expr = Expression("a > 1 and b < 2")
        assert expr.missing_from({"a": 1}) == {"b"}
        assert expr.missing_from({"a": 1, "b": 2}) == set()

    def test_referenced_names_exclude_functions_and_literals(self):
        assert Expression("abs(a) > 5 and b == True").names == {"a", "b"}
