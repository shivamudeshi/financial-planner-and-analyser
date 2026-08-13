"""Rule loading, evaluation, and the alert lifecycle.

Rules are declarative and live in ``rules.yaml``, which you edit. They produce
**alerts**, never orders — the tool tells you, you act in your broker app.

Three decisions shape the design:

**Every alert stores the context that triggered it.** Three months later "why did
this fire?" is the question you actually have, and a rule name alone does not
answer it. The JSON snapshot means an alert stays explainable after the prices
that caused it have moved on.

**Cooldowns are per rule *and* per instrument.** A stop-loss condition stays true
every day once breached; without a cooldown the alert list becomes a wall of the
same fact and you stop reading it. An alert you have stopped reading is worse
than no alert.

**Firing is idempotent per day.** Running the daily job twice does not double up.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date as Date, timedelta
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..db import financial_year
from ..tax.engine import TaxEngine
from .context import instrument_contexts, portfolio_contexts
from .evaluator import Expression, RuleSyntaxError

RULES_PATH = Path(__file__).with_name("rules.yaml")

SEVERITIES = ("info", "medium", "high")
STATUSES = ("NEW", "ACKED", "ACTED", "MUTED")


@dataclass
class Rule:
    name: str
    when: Expression
    message: str = ""
    severity: str = "medium"
    cooldown_days: int = 30
    scope: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    note: str = ""

    @property
    def level(self) -> str:
        return self.scope.get("level", "instrument")

    def matches(self, ctx: dict) -> bool:
        """Does this rule apply to this subject at all?"""
        for key, expected in self.scope.items():
            if key == "level":
                continue
            if key == "min_priority":
                if ctx.get("exit_priority", 0) < expected:
                    return False
                continue
            actual = ctx.get(key)
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    def render(self, ctx: dict) -> str:
        """Format the message against the context, tolerating gaps."""
        if not self.message:
            return self.name.replace("_", " ").capitalize()
        try:
            return self.message.format(**ctx)
        except (KeyError, IndexError, ValueError):
            return f"{self.name}: {self.message}"


@dataclass
class Alert:
    rule_name: str
    subject: str
    instrument_id: int | None
    severity: str
    message: str
    context: dict
    fired_on: str
    id: int | None = None
    status: str = "NEW"


@dataclass
class RunResult:
    fired: list[Alert] = field(default_factory=list)
    suppressed_cooldown: int = 0
    already_open: int = 0
    rules_evaluated: int = 0
    subjects_evaluated: int = 0
    warnings: list[str] = field(default_factory=list)

    def by_severity(self) -> dict[str, list[Alert]]:
        out: dict[str, list[Alert]] = {s: [] for s in SEVERITIES}
        for a in self.fired:
            out.setdefault(a.severity, []).append(a)
        return out


def load_rules(path: Path | str = RULES_PATH) -> tuple[list[Rule], list[str]]:
    """Parse and compile the rules file.

    A bad condition is reported with its rule name and the rest still load — one
    typo should not silently disable your whole alerting.
    """
    raw = yaml.safe_load(Path(path).read_text()) or []
    rules: list[Rule] = []
    warnings: list[str] = []
    seen: set[str] = set()

    for entry in raw:
        name = entry.get("name")
        if not name:
            warnings.append("A rule has no name and was skipped.")
            continue
        if name in seen:
            warnings.append(f"Duplicate rule name {name!r}; the later one was skipped.")
            continue
        seen.add(name)

        try:
            when = Expression(entry["when"])
        except KeyError:
            warnings.append(f"Rule {name!r} has no `when` condition; skipped.")
            continue
        except RuleSyntaxError as exc:
            warnings.append(f"Rule {name!r} was skipped: {exc}")
            continue

        severity = entry.get("severity", "medium")
        if severity not in SEVERITIES:
            warnings.append(
                f"Rule {name!r} has unknown severity {severity!r}; treated as 'medium'."
            )
            severity = "medium"

        rules.append(Rule(
            name=name, when=when, message=entry.get("message", ""), severity=severity,
            cooldown_days=int(entry.get("cooldown_days", 30)),
            scope=entry.get("scope", {}) or {}, enabled=bool(entry.get("enabled", True)),
            note=entry.get("note", ""),
        ))
    return rules, warnings


class RulesEngine:
    def __init__(
        self,
        conn: sqlite3.Connection,
        rules: list[Rule] | None = None,
        *,
        rules_path: Path | str = RULES_PATH,
        tax_engine: TaxEngine | None = None,
    ):
        self.conn = conn
        self.warnings: list[str] = []
        if rules is None:
            rules, self.warnings = load_rules(rules_path)
        self.rules = rules
        self.tax_engine = tax_engine or TaxEngine()

    # -- evaluation -------------------------------------------------------

    def run(self, as_of: str | None = None, *, persist: bool = True) -> RunResult:
        as_of = as_of or Date.today().isoformat()
        fy = financial_year(as_of)
        result = RunResult(warnings=list(self.warnings))
        active = [r for r in self.rules if r.enabled]
        result.rules_evaluated = len(active)

        subjects: list[tuple[int | None, str, dict]] = [
            (p.instrument_id, p.name, ctx)
            for p, ctx in instrument_contexts(self.conn, engine=self.tax_engine, as_of=as_of)
        ]
        subjects += [(None, c["name"], c) for c in portfolio_contexts(self.conn, fy, as_of)]
        result.subjects_evaluated = len(subjects)

        for instrument_id, subject, ctx in subjects:
            is_portfolio = ctx.get("kind") == "PORTFOLIO"
            for rule in active:
                if (rule.level == "portfolio") != is_portfolio:
                    continue
                if not rule.matches(ctx):
                    continue
                if not rule.when.evaluate(ctx):
                    continue

                if self._open_alert(rule.name, subject):
                    result.already_open += 1
                    continue
                if self._in_cooldown(rule, subject, as_of):
                    result.suppressed_cooldown += 1
                    continue

                alert = Alert(
                    rule_name=rule.name, subject=subject, instrument_id=instrument_id,
                    severity=rule.severity, message=rule.render(ctx),
                    context=_snapshot(ctx, rule), fired_on=as_of,
                )
                if persist:
                    alert.id = self._insert(alert)
                result.fired.append(alert)

        return result

    def _open_alert(self, rule_name: str, subject: str) -> bool:
        """An unacknowledged alert for the same rule and subject already exists."""
        return self.conn.execute(
            "SELECT 1 FROM alerts WHERE rule_name=? AND context LIKE ? AND status='NEW'",
            (rule_name, f'%"__subject__": "{subject}"%'),
        ).fetchone() is not None

    def _in_cooldown(self, rule: Rule, subject: str, as_of: str) -> bool:
        if rule.cooldown_days <= 0:
            return False
        cutoff = (Date.fromisoformat(as_of) - timedelta(days=rule.cooldown_days)).isoformat()
        return self.conn.execute(
            "SELECT 1 FROM alerts WHERE rule_name=? AND context LIKE ? AND fired_on > ?"
            " AND status != 'MUTED'",
            (rule.name, f'%"__subject__": "{subject}"%', cutoff),
        ).fetchone() is not None

    def _insert(self, alert: Alert) -> int:
        with self.conn:
            return self.conn.execute(
                "INSERT INTO alerts (rule_name, instrument_id, fired_on, severity, message,"
                " context, status) VALUES (?,?,?,?,?,?,?)",
                (alert.rule_name, alert.instrument_id, alert.fired_on, alert.severity,
                 alert.message, json.dumps(alert.context), alert.status),
            ).lastrowid

    # -- lifecycle --------------------------------------------------------

    def set_status(self, alert_id: int, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}; expected one of {STATUSES}.")
        with self.conn:
            self.conn.execute("UPDATE alerts SET status=? WHERE id=?", (status, alert_id))

    def open_alerts(self, *, include_muted: bool = False) -> list[Alert]:
        sql = "SELECT * FROM alerts WHERE status IN ('NEW','ACKED')"
        if include_muted:
            sql = "SELECT * FROM alerts WHERE 1=1"
        rows = self.conn.execute(
            sql + " ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1"
            " ELSE 2 END, fired_on DESC"
        )
        return [_row_to_alert(r) for r in rows]

    def history(self, subject: str | None = None, limit: int = 200) -> list[Alert]:
        sql, args = "SELECT * FROM alerts", []
        if subject:
            sql += " WHERE context LIKE ?"
            args.append(f'%"__subject__": "{subject}"%')
        return [_row_to_alert(r) for r in
                self.conn.execute(sql + " ORDER BY fired_on DESC LIMIT ?", args + [limit])]


def _snapshot(ctx: dict, rule: Rule) -> dict:
    """Store the fields the rule actually used, plus identity.

    Keeping only the referenced fields makes the stored reason readable — the
    full context is 40 fields, and the two that fired the rule are the ones you
    want to see.
    """
    snap = {"__subject__": ctx.get("name", ""), "__rule__": rule.name,
            "__condition__": rule.when.source}
    for field_name in sorted(rule.when.names):
        if field_name in ctx:
            value = ctx[field_name]
            snap[field_name] = round(value, 4) if isinstance(value, float) else value
    return snap


def _row_to_alert(row: sqlite3.Row) -> Alert:
    ctx = json.loads(row["context"]) if row["context"] else {}
    return Alert(
        id=row["id"], rule_name=row["rule_name"], subject=ctx.get("__subject__", ""),
        instrument_id=row["instrument_id"], severity=row["severity"],
        message=row["message"], context=ctx, fired_on=row["fired_on"], status=row["status"],
    )


def describe(rules: Iterable[Rule]) -> list[dict]:
    return [
        {"name": r.name, "severity": r.severity, "condition": r.when.source,
         "cooldown_days": r.cooldown_days, "scope": r.scope or "all",
         "enabled": r.enabled, "note": r.note}
        for r in rules
    ]
