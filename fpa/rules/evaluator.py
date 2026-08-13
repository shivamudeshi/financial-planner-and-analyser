"""A restricted expression evaluator for rule conditions.

Rules live in a YAML file you edit, and their conditions are Python-ish
expressions like ``unrealised_pct <= -15 and days_held > 365``. The obvious
implementation is ``eval()``, and it is the wrong one: a rules file is a
configuration file, it gets copied between machines and pasted from notes, and
an evaluator that can reach ``__import__`` turns a config typo into arbitrary
code execution.

So expressions are parsed to an AST and walked with a **whitelist**. Anything
not explicitly permitted — attribute access, subscripting, lambdas, comprehensions,
imports, assignment — raises :class:`RuleSyntaxError` at load time rather than
doing something surprising at evaluation time.

Names resolve only against the context dictionary supplied by the caller. An
unknown name is an error, not ``None``, because a rule that silently evaluates a
typo'd field to nothing is a rule that silently never fires.
"""

from __future__ import annotations

import ast
import operator
from typing import Any, Callable

# Everything the evaluator is allowed to see. Note the absence of Attribute,
# Subscript, Lambda, comprehensions, and anything statement-shaped.
ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Call,
    ast.Name, ast.Load, ast.Constant, ast.IfExp, ast.List, ast.Tuple, ast.Set,
    ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
)

BIN_OPS: dict[type[ast.AST], Callable] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}

COMPARE_OPS: dict[type[ast.AST], Callable] = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
}

# Pure, side-effect-free helpers rules may call.
FUNCTIONS: dict[str, Callable] = {
    "abs": abs, "min": min, "max": max, "round": round, "len": len,
    "any": any, "all": all, "int": int, "float": float, "bool": bool,
}

CONSTANTS: dict[str, Any] = {"True": True, "False": False, "None": None}


class RuleSyntaxError(ValueError):
    """The expression uses something the evaluator will not run."""


class RuleEvaluationError(ValueError):
    """The expression is valid but could not be evaluated in this context."""


class Expression:
    """A compiled, validated rule condition."""

    def __init__(self, source: str):
        self.source = source
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise RuleSyntaxError(f"Could not parse {source!r}: {exc.msg}") from exc
        _validate(tree)
        self.tree = tree
        self.names = _referenced_names(tree)

    def __call__(self, context: dict[str, Any]) -> Any:
        return _eval(self.tree.body, context, self.source)

    def evaluate(self, context: dict[str, Any]) -> bool:
        """Evaluate to a boolean.

        A missing field or a ``None`` reading (no price history, no fundamentals)
        makes the rule *not fire*, rather than raising. Data gaps are normal and
        a rule that explodes on a fund with 10 days of NAV history is useless.
        """
        try:
            return bool(self(context))
        except RuleEvaluationError:
            return False

    def missing_from(self, context: dict[str, Any]) -> set[str]:
        return {n for n in self.names if n not in context}

    def __repr__(self) -> str:
        return f"Expression({self.source!r})"


def _validate(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise RuleSyntaxError(
                f"{type(node).__name__} is not allowed in a rule condition. "
                f"Rules may use comparisons, arithmetic, and/or/not, and the "
                f"functions {', '.join(sorted(FUNCTIONS))}."
            )
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise RuleSyntaxError("Only direct calls to named functions are allowed.")
            if node.func.id not in FUNCTIONS:
                raise RuleSyntaxError(
                    f"Unknown function {node.func.id!r}. Available: "
                    f"{', '.join(sorted(FUNCTIONS))}."
                )
            if node.keywords:
                raise RuleSyntaxError("Keyword arguments are not allowed in rule conditions.")


def _referenced_names(tree: ast.AST) -> set[str]:
    return {
        n.id for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id not in FUNCTIONS and n.id not in CONSTANTS
    }


def _eval(node: ast.AST, ctx: dict[str, Any], source: str) -> Any:
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        if node.id in CONSTANTS:
            return CONSTANTS[node.id]
        if node.id in ctx:
            value = ctx[node.id]
            if value is None:
                raise RuleEvaluationError(f"{node.id} is unavailable in {source!r}")
            return value
        raise RuleEvaluationError(
            f"Unknown field {node.id!r} in {source!r}. Check the field list with "
            f"`fpa rules fields`."
        )

    if isinstance(node, ast.BoolOp):
        # Short-circuit, so `has_history and rsi14 < 30` is a usable guard.
        if isinstance(node.op, ast.And):
            for v in node.values:
                if not _eval(v, ctx, source):
                    return False
            return True
        for v in node.values:
            if _eval(v, ctx, source):
                return True
        return False

    if isinstance(node, ast.UnaryOp):
        value = _eval(node.operand, ctx, source)
        if isinstance(node.op, ast.Not):
            return not value
        return -value if isinstance(node.op, ast.USub) else +value

    if isinstance(node, ast.BinOp):
        op = BIN_OPS[type(node.op)]
        try:
            return op(_eval(node.left, ctx, source), _eval(node.right, ctx, source))
        except ZeroDivisionError:
            raise RuleEvaluationError(f"Division by zero in {source!r}") from None

    if isinstance(node, ast.Compare):
        left = _eval(node.left, ctx, source)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval(comparator, ctx, source)
            if not COMPARE_OPS[type(op)](left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.IfExp):
        return (_eval(node.body, ctx, source) if _eval(node.test, ctx, source)
                else _eval(node.orelse, ctx, source))

    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values = [_eval(e, ctx, source) for e in node.elts]
        return set(values) if isinstance(node, ast.Set) else values

    if isinstance(node, ast.Call):
        args = [_eval(a, ctx, source) for a in node.args]
        try:
            return FUNCTIONS[node.func.id](*args)
        except (TypeError, ValueError) as exc:
            raise RuleEvaluationError(f"{node.func.id}() failed in {source!r}: {exc}") from exc

    raise RuleSyntaxError(f"Unsupported expression node {type(node).__name__}")
