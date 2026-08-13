"""SIP schedule and financial-year cashflow.

The sell planner answers what comes *out*. This answers what goes *in*, because
the two interact: capital freed by a zero-tax sale plus SIP contributions is one
pool of deployable money, and knowing the total changes what you do with it.

Step-up is modelled as a first-class thing rather than a footnote. A SIP that
rises 10% a year is a materially different commitment from a flat one — over ten
years a ₹17,000 SIP stepping up 10% annually contributes roughly ₹32.5 lakh
against ₹20.4 lakh flat — and since you cannot plan a year without knowing what
the instalment becomes, the schedule computes it explicitly.

Convention: the step-up applies on each anniversary of the SIP start date, not
on 1 April, because that is how AMCs actually implement it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date as Date

from ..db import financial_year, fy_bounds
from ..money import fmt, pct

SCHEMA = """
CREATE TABLE IF NOT EXISTS sip_plans (
    id             INTEGER PRIMARY KEY,
    instrument_id  INTEGER REFERENCES instruments(id) ON DELETE CASCADE,
    label          TEXT,
    amount         INTEGER NOT NULL,      -- paise per instalment, at start_date
    day_of_month   INTEGER NOT NULL DEFAULT 1,
    start_date     TEXT    NOT NULL,
    end_date       TEXT,
    step_up_pct    REAL    NOT NULL DEFAULT 0.0,   -- applied each anniversary
    step_up_cap    INTEGER,                        -- optional ceiling, paise
    active         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_sip_instrument ON sip_plans(instrument_id, active);
"""


@dataclass
class Instalment:
    date: str
    amount: int
    instrument_id: int | None
    label: str
    year_index: int          # 0 = first year of the SIP


@dataclass
class SipPlan:
    id: int | None
    instrument_id: int | None
    label: str
    amount: int
    day_of_month: int = 1
    start_date: str = ""
    end_date: str | None = None
    step_up_pct: float = 0.0
    step_up_cap: int | None = None

    def amount_in_year(self, year_index: int) -> int:
        """Instalment after ``year_index`` anniversaries of step-up."""
        amount = round(self.amount * (1 + self.step_up_pct / 100) ** year_index)
        if self.step_up_cap:
            amount = min(amount, self.step_up_cap)
        return amount

    def instalments(self, start: str, end: str) -> list[Instalment]:
        """Every instalment falling within ``[start, end]``."""
        first = Date.fromisoformat(max(start, self.start_date))
        last = Date.fromisoformat(min(end, self.end_date) if self.end_date else end)
        begin = Date.fromisoformat(self.start_date)
        out: list[Instalment] = []

        y, m = first.year, first.month
        while True:
            day = min(self.day_of_month, _days_in_month(y, m))
            d = Date(y, m, day)
            if d > last:
                break
            if d >= first:
                # Anniversaries elapsed since the SIP began.
                years = (d.year - begin.year) - ((d.month, d.day) < (begin.month, begin.day))
                out.append(
                    Instalment(d.isoformat(), self.amount_in_year(max(0, years)),
                               self.instrument_id, self.label, max(0, years))
                )
            m += 1
            if m > 12:
                m, y = 1, y + 1
        return out


@dataclass
class FyCashflow:
    fy: str
    planned: int = 0
    actual: int = 0
    instalments: list[Instalment] = field(default_factory=list)
    by_instrument: dict[str, int] = field(default_factory=dict)
    monthly: dict[str, int] = field(default_factory=dict)
    remaining_scheduled: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def shortfall(self) -> int:
        """Planned minus invested. Includes instalments not yet due, so early in
        the year a large shortfall is expected — compare with
        ``remaining_scheduled`` to see whether you are actually behind."""
        return self.planned - self.actual

    @property
    def behind_by(self) -> int:
        """Genuinely missed contributions: due already, but not invested."""
        return max(0, self.shortfall - self.remaining_scheduled)

    @property
    def completion_pct(self) -> float:
        return pct(self.actual, self.planned)

    @property
    def monthly_average(self) -> int:
        return round(self.planned / 12) if self.planned else 0


def ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(SCHEMA)


def add_plan(
    conn: sqlite3.Connection,
    amount_rupees: float,
    *,
    instrument_id: int | None = None,
    label: str = "SIP",
    day_of_month: int = 1,
    start_date: str | None = None,
    end_date: str | None = None,
    step_up_pct: float = 0.0,
    step_up_cap_rupees: float | None = None,
) -> int:
    from ..money import to_paise

    ensure_schema(conn)
    with conn:
        return conn.execute(
            "INSERT INTO sip_plans (instrument_id, label, amount, day_of_month, start_date,"
            " end_date, step_up_pct, step_up_cap) VALUES (?,?,?,?,?,?,?,?)",
            (instrument_id, label, to_paise(amount_rupees), day_of_month,
             start_date or Date.today().isoformat(), end_date, step_up_pct,
             to_paise(step_up_cap_rupees) if step_up_cap_rupees else None),
        ).lastrowid


def load_plans(conn: sqlite3.Connection) -> list[SipPlan]:
    ensure_schema(conn)
    return [
        SipPlan(
            id=r["id"], instrument_id=r["instrument_id"], label=r["label"] or "SIP",
            amount=r["amount"], day_of_month=r["day_of_month"], start_date=r["start_date"],
            end_date=r["end_date"], step_up_pct=r["step_up_pct"], step_up_cap=r["step_up_cap"],
        )
        for r in conn.execute("SELECT * FROM sip_plans WHERE active=1 ORDER BY id")
    ]


def fy_cashflow(conn: sqlite3.Connection, fy: str, as_of: str | None = None) -> FyCashflow:
    """Planned vs actual SIP contributions for a financial year."""
    start, end = fy_bounds(fy)
    flow = FyCashflow(fy=fy)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM instruments")}

    for plan in load_plans(conn):
        for inst in plan.instalments(start, end):
            flow.instalments.append(inst)
            flow.planned += inst.amount
            key = names.get(inst.instrument_id, inst.label)
            flow.by_instrument[key] = flow.by_instrument.get(key, 0) + inst.amount
            flow.monthly[inst.date[:7]] = flow.monthly.get(inst.date[:7], 0) + inst.amount

    # Only SIP-kind transactions count as SIP execution. Folding in every BUY
    # would let a one-off lump sum masquerade as instalments you never paid.
    sql = ("SELECT COALESCE(SUM(amount + charges), 0) FROM transactions"
           " WHERE kind = 'SIP' AND date BETWEEN ? AND ?")
    args: list = [start, end]
    targets = [p.instrument_id for p in load_plans(conn) if p.instrument_id]
    if targets:
        sql += f" AND instrument_id IN ({','.join('?' * len(targets))})"
        args += targets
    flow.actual = conn.execute(sql, args).fetchone()[0]
    flow.remaining_scheduled = sum(
        i.amount for i in flow.instalments if i.date > (as_of or Date.today().isoformat())
    )

    _notes(flow, as_of or Date.today().isoformat(), end)
    return flow


def _notes(flow: FyCashflow, as_of: str, fy_end: str) -> None:
    if not flow.planned:
        flow.notes.append(
            "No SIP plans recorded. Add one with `fpa sip add` so the year's inflow is "
            "planned alongside what the sell planner frees."
        )
        return

    stepped = {i.year_index for i in flow.instalments}
    if len(stepped) > 1:
        first = min(i.amount for i in flow.instalments)
        last = max(i.amount for i in flow.instalments)
        flow.notes.append(
            f"A step-up takes effect during FY {flow.fy}: instalments run from {fmt(first)} "
            f"to {fmt(last)}."
        )
    if as_of < fy_end:
        remaining = sum(i.amount for i in flow.instalments if i.date > as_of)
        flow.notes.append(
            f"{fmt(remaining)} still to be invested between {as_of} and {fy_end}."
        )


def project(
    plans: list[SipPlan], years: int, *, start: str | None = None
) -> list[tuple[str, int]]:
    """Total contribution per financial year over a horizon.

    Shows what a step-up actually commits you to. Contribution only — no return
    assumption, because a projection that compounds an invented return tells you
    more about the assumption than about the plan.
    """
    begin = Date.fromisoformat(start) if start else Date.today()
    out = []
    for n in range(years):
        fy = financial_year(Date(begin.year + n, max(4, begin.month), 1).isoformat())
        fy_start, fy_end = fy_bounds(fy)
        total = sum(
            i.amount for p in plans for i in p.instalments(fy_start, fy_end)
        )
        out.append((fy, total))
    return out


def deployable(freed_by_sales: int, flow: FyCashflow, as_of: str) -> tuple[int, list[str]]:
    """Capital available to redeploy: sale proceeds plus SIP still to come.

    The two are usually considered separately, which is how people end up
    selling to fund something their SIP was already going to cover.
    """
    remaining_sip = sum(i.amount for i in flow.instalments if i.date > as_of)
    total = freed_by_sales + remaining_sip
    notes = [
        f"{fmt(freed_by_sales)} freed by the sell plan plus {fmt(remaining_sip)} of SIP still "
        f"to come this year = {fmt(total)} to deploy."
    ]
    if remaining_sip > freed_by_sales and freed_by_sales > 0:
        notes.append(
            "Your remaining SIP exceeds what the sell plan frees. If the goal is to fund a new "
            "position, redirecting SIP is cheaper than selling — no transaction costs, no "
            "holding-period reset."
        )
    return total, notes


def _days_in_month(year: int, month: int) -> int:
    import calendar

    return calendar.monthrange(year, month)[1]
