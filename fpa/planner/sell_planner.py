"""Financial-year sell planner: maximise what you exit, keep tax at zero.

Three things about Indian capital gains make an annual planner worth having:

* The s.112A exemption (₹1.25L of long-term equity gain) is **use-it-or-lose-it**.
  It does not carry forward. Every year you end under the limit, the unused part
  is gone for good.
* Short-term equity gain is taxed at 20% from the first rupee, long-term at
  12.5% after the exemption. Crossing the 12-month line is worth a lot, and the
  date it happens is knowable in advance.
* Set-off of losses is mandatory (s.70), so booking a loss in a year when your
  long-term gain is already under the exemption destroys the loss instead of
  banking it.

Two planning modes fall out of that:

``EXIT``
    You want out of certain positions. Free the most capital possible without
    triggering tax. Within the gain budget, prefer lots with the *lowest* gain
    percentage — they release the most market value per rupee of gain consumed.

``HARVEST``
    You want the free basis step-up. Realise gains up to the exemption and
    immediately rebuy, permanently reducing future tax at zero cost today.
    Here prefer the *highest* gain percentage — same gain realised on less
    turnover, so less brokerage and less time out of the market.

The planner never places orders and never claims certainty about the future. It
answers one narrow, checkable question: given today's lots and prices, what is
the largest set of sales that keeps this financial year's tax at exactly zero?
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date as Date, timedelta
from enum import Enum

from ..db import financial_year, fy_bounds
from ..lots import brought_forward, realised_gains
from ..money import fmt, pct
from ..portfolio import LotView, lot_views
from ..tax.engine import Gain, TaxEngine, TaxResult, Term

# Quantity granularity: whole shares for equity, fractional units for MFs.
MF_QTY_DP = 3

# Once the gain budget is nearly spent, bisection will happily size a lot down to
# a few rupees. No order is worth placing at that size, so anything below this is
# dropped. Skipping a sale only lowers realised gain, so the zero-tax invariant
# is unaffected.
MIN_ORDER_PAISE = 500_00


class Mode(str, Enum):
    EXIT = "EXIT"
    HARVEST = "HARVEST"


@dataclass
class Action:
    lot_id: int
    instrument_id: int
    name: str
    symbol: str
    kind: str
    buy_date: str
    quantity: float
    price: int
    sale_value: int
    cost: int
    gain: int
    term: Term
    days_held: int
    exit_priority: int
    rationale: str
    partial: bool = False

    @property
    def gain_pct(self) -> float:
        return pct(self.gain, self.cost)


@dataclass
class Deferral:
    """A short-term lot worth waiting on rather than selling now."""

    name: str
    symbol: str
    quantity: float
    gain: int
    days_to_long_term: int
    long_term_date: str
    tax_if_sold_now: int
    within_this_fy: bool

    @property
    def saving(self) -> int:
        return self.tax_if_sold_now


@dataclass
class InstrumentPlan:
    """All lot-level actions for one instrument, rolled up into one order.

    You place one redemption or one sell order per instrument; the AMC or broker
    applies FIFO across your lots itself. A SIP running for three years produces
    36 lots, and listing 36 separate sells for what is a single instruction is
    noise. Lot detail stays available underneath for the tax record.
    """

    instrument_id: int
    name: str
    symbol: str
    kind: str
    price: int
    exit_priority: int
    actions: list[Action] = field(default_factory=list)

    @property
    def quantity(self) -> float:
        return sum(a.quantity for a in self.actions)

    @property
    def proceeds(self) -> int:
        return sum(a.sale_value for a in self.actions)

    @property
    def cost(self) -> int:
        return sum(a.cost for a in self.actions)

    @property
    def gain(self) -> int:
        return sum(a.gain for a in self.actions)

    @property
    def gain_pct(self) -> float:
        return pct(self.gain, self.cost)

    @property
    def long_term_gain(self) -> int:
        return sum(a.gain for a in self.actions if a.term is Term.LONG)

    @property
    def short_term_gain(self) -> int:
        return sum(a.gain for a in self.actions if a.term is Term.SHORT)

    @property
    def lot_count(self) -> int:
        return len(self.actions)

    @property
    def is_full_exit(self) -> bool:
        return not any(a.partial for a in self.actions)

    def summary(self) -> str:
        verb = "Redeem" if self.kind == "MF" else "Sell"
        qty = f"{self.quantity:,.3f}".rstrip("0").rstrip(".")
        unit = "units" if self.kind == "MF" else "shares"
        parts = [f"{verb} {qty} {unit} for {fmt(self.proceeds)}"]
        if self.long_term_gain and self.short_term_gain:
            parts.append(
                f"realising {fmt(self.long_term_gain)} long-term and "
                f"{fmt(self.short_term_gain)} short-term gain"
            )
        elif self.gain >= 0:
            term = "long-term" if self.long_term_gain else "short-term"
            parts.append(f"realising {fmt(self.gain)} {term} gain")
        else:
            parts.append(f"booking a {fmt(-self.gain)} loss")
        parts.append(f"across {self.lot_count} lot(s), at zero tax")
        return ", ".join(parts) + "."


@dataclass
class Plan:
    fy: str
    as_of: str
    mode: Mode
    actions: list[Action] = field(default_factory=list)
    deferrals: list[Deferral] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    baseline: TaxResult | None = None
    final: TaxResult | None = None
    tax_if_sold_outright: int = 0

    @property
    def proceeds(self) -> int:
        return sum(a.sale_value for a in self.actions)

    @property
    def gain_realised(self) -> int:
        return sum(a.gain for a in self.actions)

    @property
    def long_term_gain(self) -> int:
        return sum(a.gain for a in self.actions if a.term is Term.LONG and a.gain > 0)

    @property
    def losses_booked(self) -> int:
        return -sum(a.gain for a in self.actions if a.gain < 0)

    @property
    def tax(self) -> int:
        return self.final.total_tax if self.final else 0

    @property
    def tax_saved(self) -> int:
        return max(0, self.tax_if_sold_outright - self.tax)

    @property
    def exemption_left(self) -> int:
        return self.final.exemption_unused if self.final else 0

    def by_instrument(self) -> list[InstrumentPlan]:
        """Lot-level actions rolled up into one order per instrument."""
        grouped: dict[int, InstrumentPlan] = {}
        for a in self.actions:
            ip = grouped.get(a.instrument_id)
            if ip is None:
                ip = grouped[a.instrument_id] = InstrumentPlan(
                    instrument_id=a.instrument_id, name=a.name, symbol=a.symbol,
                    kind=a.kind, price=a.price, exit_priority=a.exit_priority,
                )
            ip.actions.append(a)
        return sorted(grouped.values(), key=lambda ip: (-ip.exit_priority, -ip.proceeds))

    def deferrals_by_instrument(self) -> list[dict]:
        """Deferral advice rolled up per instrument.

        Reported against the *last* lot to cross 12 months, since that is the
        date the whole remaining position becomes long-term.
        """
        grouped: dict[str, dict] = {}
        for d in self.deferrals:
            g = grouped.setdefault(
                d.name,
                {"name": d.name, "symbol": d.symbol, "quantity": 0.0, "gain": 0,
                 "tax_if_sold_now": 0, "lots": 0, "days_to_long_term": 0,
                 "long_term_date": d.long_term_date, "within_this_fy": d.within_this_fy},
            )
            g["quantity"] += d.quantity
            g["gain"] += d.gain
            g["tax_if_sold_now"] += d.tax_if_sold_now
            g["lots"] += 1
            if d.days_to_long_term > g["days_to_long_term"]:
                g.update(
                    days_to_long_term=d.days_to_long_term,
                    long_term_date=d.long_term_date,
                    within_this_fy=d.within_this_fy,
                )
        return sorted(grouped.values(), key=lambda g: -g["tax_if_sold_now"])


class SellPlanner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        engine: TaxEngine | None = None,
        *,
        fy: str | None = None,
        as_of: str | None = None,
    ):
        self.conn = conn
        self.as_of = as_of or Date.today().isoformat()
        self.fy = fy or financial_year(self.as_of)
        self.engine = engine or TaxEngine(self.fy)

    # -- public -----------------------------------------------------------

    def plan(
        self,
        mode: Mode = Mode.EXIT,
        *,
        min_priority: int = 0,
        instrument_ids: list[int] | None = None,
        book_losses: bool = True,
    ) -> Plan:
        """Build a zero-tax sell plan for the financial year.

        ``min_priority`` filters to holdings you actually want to reduce.
        ``book_losses`` allows the planner to realise losses to create headroom;
        turn it off if you don't want loss-making positions touched.
        """
        views = self._candidates(min_priority, instrument_ids)
        booked = realised_gains(self.conn, self.fy)
        bf_stcl, bf_ltcl = brought_forward(self.conn, self.fy)

        plan = Plan(fy=self.fy, as_of=self.as_of, mode=mode)
        plan.baseline = self._tax(booked, bf_stcl, bf_ltcl)

        selected: list[Gain] = []

        # 1. If the year is already in tax, harvest losses to neutralise it.
        if plan.baseline.total_tax > 0 and book_losses:
            self._book_losses(views, booked, selected, plan, bf_stcl, bf_ltcl)

        # 2. Book losses on positions you want out of *before* choosing gains.
        #    Order matters: a realised loss enlarges the zero-tax gain budget, so
        #    selecting gains first would hide that capacity and make every loss
        #    look wasteful. Capped at what the available gains can actually absorb.
        if book_losses and mode is Mode.EXIT:
            self._book_wanted_losses(views, booked, selected, plan, bf_stcl, bf_ltcl)

        # 3. Fill the whole zero-tax capacity — exemption plus loss offsets.
        for view in self._ordered(views, mode):
            if view.gain <= 0:
                continue
            self._take(view, booked, selected, plan, bf_stcl, bf_ltcl, mode)

        plan.final = self._tax(booked + selected, bf_stcl, bf_ltcl)
        plan.tax_if_sold_outright = self._outright_tax(views, booked, bf_stcl, bf_ltcl)
        self._deferrals(views, plan)
        self._explain(plan, views)
        return plan

    # -- candidate handling ----------------------------------------------

    def _candidates(self, min_priority: int, instrument_ids: list[int] | None) -> list[LotView]:
        views = lot_views(self.conn, self.as_of)
        if instrument_ids is not None:
            views = [v for v in views if v.instrument_id in instrument_ids]
        return [v for v in views if v.exit_priority >= min_priority]

    def _ordered(self, views: list[LotView], mode: Mode) -> list[LotView]:
        """Selection order. See module docstring for why the two modes differ."""
        if mode is Mode.HARVEST:
            # Same gain on less turnover: highest gain % first.
            return sorted(views, key=lambda v: (-v.gain_pct, -v.exit_priority))
        # EXIT: free the most capital per rupee of gain budget, and honour
        # conviction first — long-term lots ahead of short-term, since a
        # short-term gain is taxed from the first rupee.
        return sorted(
            views,
            key=lambda v: (
                -v.exit_priority,
                v.term(self.engine) is not Term.LONG,
                v.gain_pct,
            ),
        )

    def _tax(self, gains: list[Gain], bf_stcl: int, bf_ltcl: int) -> TaxResult:
        return self.engine.compute(gains, bf_stcl=bf_stcl, bf_ltcl=bf_ltcl)

    def _gain_of(self, view: LotView, qty: float) -> Gain:
        share = qty / view.quantity if view.quantity else 0
        return Gain(
            amount=round(view.gain * share),
            term=view.term(self.engine),
            regime=view.regime,
            label=view.name,
        )

    def _quantise(self, view: LotView, qty: float) -> float:
        """Round *down* to a tradeable quantity — never up, or tax stops being zero."""
        if view.kind == "EQUITY":
            return float(int(qty))
        return int(qty * 10**MF_QTY_DP) / 10**MF_QTY_DP

    def _take(
        self,
        view: LotView,
        booked: list[Gain],
        selected: list[Gain],
        plan: Plan,
        bf_stcl: int,
        bf_ltcl: int,
        mode: Mode,
    ) -> None:
        """Sell as much of ``view`` as keeps total tax at zero."""
        qty = self._max_qty_at_zero_tax(view, booked, selected, bf_stcl, bf_ltcl)
        qty = self._quantise(view, qty)
        if qty <= 0 or round(qty * view.price) < MIN_ORDER_PAISE:
            return
        gain = self._gain_of(view, qty)
        # Re-verify after quantisation rather than trusting the search.
        if self._tax(booked + selected + [gain], bf_stcl, bf_ltcl).total_tax > 0:
            return

        selected.append(gain)
        # Compare against the *quantised* full size: rounding 21.9754 units down
        # to 21.975 is not a partial fill, it is the whole lot.
        partial = qty < self._quantise(view, view.quantity) - 1e-9
        plan.actions.append(
            Action(
                lot_id=view.lot.id,
                instrument_id=view.instrument_id,
                name=view.name,
                symbol=view.symbol,
                kind=view.kind,
                buy_date=view.lot.buy_date,
                quantity=qty,
                price=view.price,
                sale_value=round(qty * view.price),
                cost=round(qty * view.lot.cost_per_unit),
                gain=gain.amount,
                term=gain.term,
                days_held=view.days_held,
                exit_priority=view.exit_priority,
                partial=partial,
                rationale=self._rationale(view, gain, partial, mode),
            )
        )

    def _max_qty_at_zero_tax(
        self,
        view: LotView,
        booked: list[Gain],
        selected: list[Gain],
        bf_stcl: int,
        bf_ltcl: int,
    ) -> float:
        """Largest quantity of this lot that leaves total tax at zero.

        Gain is linear in quantity and tax is monotonic non-decreasing in gain,
        so bisection is valid and needs no bucket-level reasoning — the real tax
        engine is the oracle, which keeps set-off rules in exactly one place.
        """
        def ok(q: float) -> bool:
            return self._tax(booked + selected + [self._gain_of(view, q)], bf_stcl, bf_ltcl).total_tax == 0

        if ok(view.quantity):
            return view.quantity

        # Probe the smallest order worth placing before bisecting. Once the gain
        # budget is spent, every remaining candidate would otherwise burn 48
        # iterations converging on a quantity that gets discarded as dust — which
        # is the difference between seconds and half a minute on a large ledger.
        min_qty = MIN_ORDER_PAISE / view.price if view.price else 0.0
        if min_qty >= view.quantity or not ok(min_qty):
            return 0.0

        lo, hi = min_qty, view.quantity
        for _ in range(48):
            mid = (lo + hi) / 2
            if ok(mid):
                lo = mid
            else:
                hi = mid
        return lo

    def _book_losses(
        self,
        views: list[LotView],
        booked: list[Gain],
        selected: list[Gain],
        plan: Plan,
        bf_stcl: int,
        bf_ltcl: int,
    ) -> None:
        """Realise losses to bring an already-taxable year back to zero.

        Short-term losses first: they set off against both short- and long-term
        gains, so they neutralise the 20% bucket that a long-term loss cannot
        touch.
        """
        losers = [v for v in views if v.gain < 0]
        losers.sort(key=lambda v: (v.term(self.engine) is not Term.SHORT, v.gain))
        for view in losers:
            if self._tax(booked + selected, bf_stcl, bf_ltcl).total_tax == 0:
                break
            qty = self._quantise(view, view.quantity)
            if qty <= 0:
                continue
            gain = self._gain_of(view, qty)
            selected.append(gain)
            plan.actions.append(
                Action(
                    lot_id=view.lot.id, instrument_id=view.instrument_id, name=view.name,
                    symbol=view.symbol, kind=view.kind, buy_date=view.lot.buy_date,
                    quantity=qty, price=view.price, sale_value=round(qty * view.price),
                    cost=round(qty * view.lot.cost_per_unit), gain=gain.amount,
                    term=gain.term, days_held=view.days_held,
                    exit_priority=view.exit_priority,
                    rationale=(
                        f"Book {fmt(-gain.amount)} {gain.term.value.replace('_', ' ').lower()} "
                        f"loss to offset gains already realised this year."
                    ),
                )
            )

    def _book_wanted_losses(
        self,
        views: list[LotView],
        booked: list[Gain],
        selected: list[Gain],
        plan: Plan,
        bf_stcl: int,
        bf_ltcl: int,
    ) -> None:
        """Sell loss-making positions you want out of anyway.

        A booked loss is not free. Set-off is mandatory (s.70), so a loss first
        reduces this year's gain and only then does the exemption apply to what
        remains. Book a loss with no gain to shelter and it is simply destroyed.

        But the converse is what makes this worth doing: every rupee of loss
        booked *adds* a rupee to the zero-tax gain budget, provided there is
        unrealised gain left to spend it on. So losses are capped at the amount
        the remaining candidate gains can actually absorb, and the surplus is
        reported rather than realised.
        """
        already = {a.lot_id for a in plan.actions}
        # Gain that would otherwise be taxed: everything above the free exemption.
        available_gain = sum(v.gain for v in views if v.gain > 0)
        headroom = self._tax(booked + selected, bf_stcl, bf_ltcl).exemption_unused
        absorbable = max(0, available_gain - headroom)

        held_back: dict[str, int] = {}
        for view in sorted(views, key=lambda v: (-v.exit_priority, v.gain)):
            if view.gain >= 0 or view.lot.id in already or view.exit_priority < 60:
                continue
            qty = self._quantise(view, view.quantity)
            if qty <= 0:
                continue
            gain = self._gain_of(view, qty)
            loss = -gain.amount

            if loss > absorbable:
                held_back[view.name] = held_back.get(view.name, 0) + loss
                continue
            absorbable -= loss

            selected.append(gain)
            plan.actions.append(
                Action(
                    lot_id=view.lot.id, instrument_id=view.instrument_id, name=view.name,
                    symbol=view.symbol, kind=view.kind, buy_date=view.lot.buy_date,
                    quantity=qty, price=view.price, sale_value=round(qty * view.price),
                    cost=round(qty * view.lot.cost_per_unit), gain=gain.amount,
                    term=gain.term, days_held=view.days_held,
                    exit_priority=view.exit_priority,
                    rationale=(
                        f"Exit priority {view.exit_priority}. Booking {fmt(-gain.amount)} of loss "
                        f"adds the same amount to this year's zero-tax gain budget."
                    ),
                )
            )

        for name, loss in sorted(held_back.items(), key=lambda kv: -kv[1]):
            plan.warnings.append(
                f"Holding {name} rather than booking {fmt(loss)} of loss. There is not enough "
                f"unrealised gain left this year to set it off against, and set-off is mandatory "
                f"— booking it now would destroy relief that is worth "
                f"{fmt(round(loss * self.engine.equity_ltcg_rate))}+ in a year when you have "
                f"taxable gains."
            )

    # -- advice -----------------------------------------------------------

    def _deferrals(self, views: list[LotView], plan: Plan) -> None:
        """Short-term gain lots that become tax-free by simply waiting."""
        sold = {a.lot_id: a.quantity for a in plan.actions}
        _, fy_end = fy_bounds(self.fy)

        for view in views:
            if view.gain <= 0 or view.term(self.engine) is not Term.SHORT:
                continue
            remaining = view.quantity - sold.get(view.lot.id, 0.0)
            if remaining <= 0:
                continue
            days = view.days_to_long_term(self.engine)
            lt_date = (Date.fromisoformat(self.as_of) + timedelta(days=days)).isoformat()
            share = remaining / view.quantity
            gain = round(view.gain * share)
            plan.deferrals.append(
                Deferral(
                    name=view.name,
                    symbol=view.symbol,
                    quantity=remaining,
                    gain=gain,
                    days_to_long_term=days,
                    long_term_date=lt_date,
                    tax_if_sold_now=round(gain * self.engine.equity_stcg_rate * (1 + self.engine.cess_rate)),
                    within_this_fy=lt_date <= fy_end,
                )
            )
        plan.deferrals.sort(key=lambda d: -d.tax_if_sold_now)

    def _outright_tax(
        self, views: list[LotView], booked: list[Gain], bf_stcl: int, bf_ltcl: int
    ) -> int:
        """Tax if every candidate position were simply sold today, unplanned."""
        gains = booked + [self._gain_of(v, v.quantity) for v in views]
        return self._tax(gains, bf_stcl, bf_ltcl).total_tax

    def _rationale(self, view: LotView, gain: Gain, partial: bool, mode: Mode) -> str:
        term = "long-term" if gain.term is Term.LONG else "short-term"
        held = f"held {view.days_held}d"
        if mode is Mode.HARVEST:
            saved = round(gain.amount * self.engine.equity_ltcg_rate)
            return (
                f"Harvest {fmt(gain.amount)} {term} gain tax-free and rebuy at "
                f"{fmt(view.price)}/unit. Steps up cost basis, saving ~{fmt(saved)} "
                f"of future tax on gain you have already earned."
            )
        base = f"Exit priority {view.exit_priority}, {held}. {fmt(gain.amount)} {term} gain"
        if gain.term is Term.SHORT and gain.amount > 0:
            base += " — taxable at 20% on its own, absorbed here by available losses"
        if partial:
            return f"{base}. Partial fill, sized to stop exactly at the zero-tax limit."
        return f"{base}, fully within the zero-tax budget."

    def _explain(self, plan: Plan, views: list[LotView]) -> None:
        f = plan.final
        assert f is not None
        _, fy_end = fy_bounds(self.fy)

        if f.exemption_unused > 0:
            plan.notes.append(
                f"{fmt(f.exemption_unused)} of the ₹1.25L s.112A exemption is still unused. "
                f"It does not carry forward — anything left on {fy_end} is gone permanently."
            )
        if plan.actions:
            plan.notes.append(
                f"Plan realises {fmt(plan.gain_realised)} of gain across "
                f"{len(plan.actions)} lot(s) for {fmt(plan.proceeds)} of proceeds, at zero tax."
            )
        else:
            plan.notes.append(
                "No sales possible at zero tax. Either there is no gain headroom left this "
                "year, or nothing is flagged for exit above the priority threshold."
            )
        if plan.tax_if_sold_outright > 0:
            plan.notes.append(
                f"Selling these positions outright today would cost {fmt(plan.tax_if_sold_outright)} "
                f"in tax. The plan defers that rather than paying it."
            )
        soon = [d for d in plan.deferrals if d.within_this_fy]
        if soon:
            saved = sum(d.tax_if_sold_now for d in soon)
            plan.notes.append(
                f"{len(soon)} short-term lot(s) cross 12 months before {fy_end}. Waiting moves "
                f"them from 20% to 12.5% (and possibly to zero, under next year's exemption), "
                f"worth about {fmt(saved)}."
            )
        if f.notes:
            plan.notes.extend(f.notes)
        plan.notes.append(
            f"Computed under FY {self.fy} tax parameters. Verify rates against the current "
            f"Finance Act before acting."
        )
