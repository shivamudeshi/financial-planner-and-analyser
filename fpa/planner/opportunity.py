"""Opportunity cost: what a tax saving actually costs you.

The sell planner answers "what can I sell at zero tax?". That question has a
blind spot — it optimises tax in isolation, and tax is not the objective. This
module supplies the counterweight, so the tax tail stops wagging the investment
dog.

Three trade-offs get quantified:

**Harvesting** (sell and rebuy to step up cost basis). The benefit is real but
*deferred*: you save ``gain × 12.5%`` of tax at some future sale, so it is
discounted back to today. The costs are immediate — round-trip charges, exit
load, and one to three days out of the market while the round trip settles.
Two costs are easy to miss: the rebought units start a **fresh 12-month holding
period**, and for equity a same-session rebuy may be netted as an intraday
trade by your broker, in which case no delivery-based capital gain arises and
the harvest achieves nothing at all.

**Deferring** (waiting for a lot to cross 12 months). The saving is certain, the
waiting is not free. What matters is the **breakeven decline** — the price fall
that exactly cancels the tax saved — measured against the position's actual
volatility over that window. A saving of 2% of position value against a 9%
one-sigma move is not a reason to keep holding something you want to exit.

**Selling anyway.** When a position should be exited on its merits and the gain
exceeds the exemption, refusing to pay 12.5% can cost far more than the tax.
:func:`cost_of_waiting` puts a number on that.

One honest caveat runs through all of it: harvesting only pays if your eventual
realised gains *exceed the exemption in the year you finally sell*. If you would
have been under ₹1.25L anyway, the harvest sheltered nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..analysis.technicals import volatility_over
from ..money import fmt, pct, to_paise
from ..tax.engine import TaxEngine, Term
from .sell_planner import Action, Plan

COSTS_PATH = Path(__file__).with_name("costs.yaml")


@dataclass
class Costs:
    """One side of a trade, broken out so you can see what dominates."""

    brokerage: int = 0
    stt: int = 0
    exchange: int = 0
    sebi: int = 0
    stamp: int = 0
    gst: int = 0
    dp: int = 0
    exit_load: int = 0

    @property
    def total(self) -> int:
        return (self.brokerage + self.stt + self.exchange + self.sebi
                + self.stamp + self.gst + self.dp + self.exit_load)

    def __add__(self, other: "Costs") -> "Costs":
        return Costs(*(getattr(self, f) + getattr(other, f) for f in
                       ("brokerage", "stt", "exchange", "sebi", "stamp", "gst", "dp", "exit_load")))

    def breakdown(self) -> dict[str, int]:
        return {k: v for k, v in {
            "Brokerage": self.brokerage, "STT": self.stt, "Exchange": self.exchange,
            "SEBI": self.sebi, "Stamp duty": self.stamp, "GST": self.gst,
            "DP charges": self.dp, "Exit load": self.exit_load,
        }.items() if v}


@dataclass
class HarvestEconomics:
    """Is this harvest worth doing?"""

    name: str
    kind: str
    value: int
    gain: int
    future_tax_saved: int
    future_tax_saved_pv: int
    round_trip_cost: int
    costs: Costs
    gap_days: int
    gap_risk: int          # one-sigma rupee move while out of the market
    resets_holding_period: bool
    net_benefit: int
    breakeven_move_pct: float
    verdict: str


@dataclass
class DeferralEconomics:
    """Is waiting for long-term treatment worth the price risk?"""

    name: str
    value: int
    gain: int
    days_to_wait: int
    tax_saved: int
    breakeven_decline_pct: float
    volatility_pct: float | None
    risk_multiple: float | None   # volatility ÷ breakeven; >1 means noise dominates
    verdict: str


@dataclass
class OpportunityReport:
    tax_avoided_now: int = 0
    transaction_costs: int = 0
    future_tax_saved_pv: int = 0
    net_benefit: int = 0
    harvests: list[HarvestEconomics] = None
    deferrals: list[DeferralEconomics] = None
    notes: list[str] = None

    def __post_init__(self):
        self.harvests = self.harvests if self.harvests is not None else []
        self.deferrals = self.deferrals if self.deferrals is not None else []
        self.notes = self.notes if self.notes is not None else []


class OpportunityAnalyser:
    def __init__(
        self,
        conn: sqlite3.Connection,
        engine: TaxEngine | None = None,
        costs_path: Path = COSTS_PATH,
    ):
        self.conn = conn
        self.engine = engine or TaxEngine()
        cfg = yaml.safe_load(costs_path.read_text())
        self.eq = cfg["equity"]
        self.mf = cfg["mutual_fund"]
        self.opp = cfg["opportunity"]

    # -- transaction costs -------------------------------------------------

    def _params(self, kind: str) -> dict:
        return self.mf if kind == "MF" else self.eq

    def sell_costs(self, value: int, kind: str, *, days_held: int | None = None) -> Costs:
        p = self._params(kind)
        brokerage = min(round(value * p["brokerage_pct"]),
                        to_paise(p.get("brokerage_cap", 10**9)))
        exchange = round(value * p["exchange_pct"])
        sebi = round(value * p["sebi_pct"])
        exit_load = 0
        if kind == "MF" and days_held is not None and days_held < p.get("exit_load_days", 0):
            exit_load = round(value * p.get("exit_load_pct", 0))
        return Costs(
            brokerage=brokerage,
            stt=round(value * p["stt_sell_pct"]),
            exchange=exchange,
            sebi=sebi,
            gst=round((brokerage + exchange + sebi) * p["gst_pct"]),
            dp=to_paise(p.get("dp_charge", 0)),
            exit_load=exit_load,
        )

    def sell_costs_for_lots(self, lots: list[tuple[int, int]], kind: str) -> Costs:
        """Sell costs for a redemption spanning several lots.

        Exit load is charged **per lot** against its own holding period, not
        against the oldest lot in the order — a three-year SIP redemption is
        mostly load-free with only the last twelve months' instalments charged.
        Percentage charges apply to the whole turnover, and the DP charge is
        levied once per scrip per day.
        """
        total = sum(v for v, _ in lots)
        costs = self.sell_costs(total, kind)          # no exit load at this level
        p = self._params(kind)
        if kind == "MF":
            threshold = p.get("exit_load_days", 0)
            rate = p.get("exit_load_pct", 0)
            costs.exit_load = sum(
                round(v * rate) for v, held in lots if held is not None and held < threshold
            )
        return costs

    def buy_costs(self, value: int, kind: str) -> Costs:
        p = self._params(kind)
        brokerage = min(round(value * p["brokerage_pct"]),
                        to_paise(p.get("brokerage_cap", 10**9)))
        exchange = round(value * p["exchange_pct"])
        sebi = round(value * p["sebi_pct"])
        return Costs(
            brokerage=brokerage,
            stt=round(value * p["stt_buy_pct"]),
            exchange=exchange,
            sebi=sebi,
            stamp=round(value * p["stamp_buy_pct"]),
            gst=round((brokerage + exchange + sebi) * p["gst_pct"]),
        )

    # -- harvesting --------------------------------------------------------

    def _present_value(self, amount: int) -> int:
        years = self.opp["expected_holding_years"]
        return round(amount / (1 + self.opp["discount_rate"]) ** years)

    def harvest_economics(
        self, instrument_id: int, name: str, kind: str, value: int, gain: int,
        *, days_held: int | None = None, lots: list[tuple[int, int]] | None = None,
        as_of: str | None = None,
    ) -> HarvestEconomics:
        """Benefit and cost of realising ``gain`` tax-free and rebuying.

        Pass ``lots`` as ``(value, days_held)`` pairs for a multi-lot redemption
        so exit load is charged per lot; ``days_held`` is the single-lot form.
        """
        gap = self.opp["gap_days_mf"] if kind == "MF" else self.opp["gap_days_equity"]

        # Basis steps up by the gain realised, so that much escapes tax later.
        future_saved = round(gain * self.engine.equity_ltcg_rate * (1 + self.engine.cess_rate))
        future_pv = self._present_value(future_saved)

        sell = (self.sell_costs_for_lots(lots, kind) if lots is not None
                else self.sell_costs(value, kind, days_held=days_held))
        costs = sell + self.buy_costs(value, kind)
        vol = volatility_over(self.conn, instrument_id, gap, as_of)
        gap_risk = round(value * vol / 100) if vol else 0

        net = future_pv - costs.total
        breakeven = pct(net, value)

        if net <= 0:
            verdict = (f"Not worth it — {fmt(costs.total)} of costs exceeds the "
                       f"{fmt(future_pv)} of discounted future tax saved.")
        elif gap_risk > net:
            verdict = (f"Marginal — {fmt(net)} of net benefit against {fmt(gap_risk)} of "
                       f"one-sigma price risk over {gap} day(s) out of the market.")
        elif net < to_paise(self.opp["min_net_benefit"]):
            verdict = f"Positive but small ({fmt(net)}); probably not worth the effort."
        else:
            verdict = (f"Worth doing — {fmt(net)} net, against {fmt(gap_risk)} of "
                       f"one-sigma gap risk.")

        return HarvestEconomics(
            name=name, kind=kind, value=value, gain=gain,
            future_tax_saved=future_saved, future_tax_saved_pv=future_pv,
            round_trip_cost=costs.total, costs=costs, gap_days=gap, gap_risk=gap_risk,
            resets_holding_period=True, net_benefit=net,
            breakeven_move_pct=breakeven, verdict=verdict,
        )

    # -- deferral ----------------------------------------------------------

    def deferral_economics(
        self, instrument_id: int, name: str, value: int, gain: int, days: int,
        *, as_of: str | None = None, becomes_exempt: bool = False,
    ) -> DeferralEconomics:
        """Tax saved by waiting for long-term, against the risk of waiting."""
        cess = 1 + self.engine.cess_rate
        if becomes_exempt:
            saved = round(gain * self.engine.equity_stcg_rate * cess)
        else:
            rate_gap = self.engine.equity_stcg_rate - self.engine.equity_ltcg_rate
            saved = round(gain * rate_gap * cess)

        breakeven = pct(saved, value)
        vol = volatility_over(self.conn, instrument_id, days, as_of)
        multiple = (vol / breakeven) if (vol and breakeven > 0) else None

        if multiple is None:
            verdict = f"Waiting {days}d saves {fmt(saved)}. No volatility history to weigh it against."
        elif multiple > 4:
            verdict = (f"Tax saving is noise here: {breakeven:.1f}% of the position against a "
                       f"{vol:.1f}% one-sigma move over {days}d. Decide on the merits, not the tax.")
        elif multiple > 1.5:
            verdict = (f"Saving {breakeven:.1f}% of position value; {days}d volatility is "
                       f"±{vol:.1f}%. Worth waiting only if you would hold anyway.")
        else:
            verdict = (f"Clear case for waiting — {fmt(saved)} saved ({breakeven:.1f}%) against "
                       f"only ±{vol:.1f}% of {days}d volatility.")

        return DeferralEconomics(
            name=name, value=value, gain=gain, days_to_wait=days, tax_saved=saved,
            breakeven_decline_pct=breakeven, volatility_pct=vol,
            risk_multiple=multiple, verdict=verdict,
        )

    def cost_of_waiting(self, value: int, gain: int, decline_pct: float) -> tuple[int, int]:
        """The counterweight: what a hypothetical decline costs versus the tax saved.

        Returns ``(tax_saved, value_lost)``. If the second exceeds the first,
        holding on for the tax was the more expensive choice.
        """
        tax = round(gain * self.engine.equity_ltcg_rate * (1 + self.engine.cess_rate))
        return tax, round(value * decline_pct / 100)

    # -- plan-level rollup -------------------------------------------------

    def analyse(self, plan: Plan, *, as_of: str | None = None) -> OpportunityReport:
        """Net out a whole plan: tax avoided, minus what achieving it costs."""
        harvesting = plan.mode.value == "HARVEST"
        # In HARVEST mode you rebuy, so "tax you would have paid selling these
        # outright" is not the counterfactual — the alternative to harvesting is
        # doing nothing, and its benefit is entirely the future basis step-up.
        report = OpportunityReport(tax_avoided_now=0 if harvesting else plan.tax_saved)
        as_of = as_of or plan.as_of

        for ip in plan.by_instrument():
            lots = [(a.sale_value, a.days_held) for a in ip.actions]
            if harvesting and ip.gain > 0:
                h = self.harvest_economics(
                    ip.instrument_id, ip.name, ip.kind, ip.proceeds, ip.gain,
                    lots=lots, as_of=as_of,
                )
                report.harvests.append(h)
                report.future_tax_saved_pv += h.future_tax_saved_pv
                report.transaction_costs += h.round_trip_cost
            else:
                # A plain exit pays only the sell side.
                report.transaction_costs += self.sell_costs_for_lots(lots, ip.kind).total

        for d in plan.deferrals_by_instrument():
            report.deferrals.append(
                self.deferral_economics(
                    self._instrument_id(d["name"]), d["name"],
                    self._position_value(d["name"], as_of),
                    d["gain"], d["days_to_long_term"], as_of=as_of,
                )
            )

        report.net_benefit = (
            report.tax_avoided_now + report.future_tax_saved_pv - report.transaction_costs
        )
        self._notes(report, plan)
        return report

    def _instrument_id(self, name: str) -> int:
        row = self.conn.execute("SELECT id FROM instruments WHERE name=?", (name,)).fetchone()
        return row["id"] if row else 0

    def _position_value(self, name: str, as_of: str | None) -> int:
        from ..portfolio import positions

        for p in positions(self.conn, as_of):
            if p.name == name:
                return p.market_value
        return 0

    def _notes(self, report: OpportunityReport, plan: Plan) -> None:
        if plan.mode.value == "HARVEST":
            report.notes.append(
                "Net benefit here excludes 'tax avoided now' deliberately: harvesting rebuys the "
                "position, so the alternative is doing nothing, not selling out. The entire "
                "benefit is the discounted future saving from a higher cost basis."
            )
        else:
            report.notes.append(
                f"'Tax avoided' is measured against selling these same positions outright today "
                f"({fmt(plan.tax_if_sold_outright)}). If you would not have sold them all anyway, "
                f"treat it as an upper bound."
            )
        if report.transaction_costs > report.tax_avoided_now + report.future_tax_saved_pv:
            report.notes.append(
                f"This plan costs more to execute ({fmt(report.transaction_costs)}) than the tax "
                f"it saves ({fmt(report.tax_avoided_now + report.future_tax_saved_pv)}). "
                f"Trim it to the highest-conviction exits."
            )
        if plan.mode.value == "HARVEST":
            report.notes.append(
                "Rebought units start a fresh 12-month holding period. If you may need to sell "
                "within a year, harvesting moves that sale from 12.5% to 20%."
            )
            report.notes.append(
                "For equity, rebuy on the next trading day. A same-session buy-back can be netted "
                "as an intraday trade, in which case no delivery-based capital gain arises and the "
                "harvest achieves nothing."
            )
            report.notes.append(
                "Harvesting only pays if your realised gains exceed the ₹1.25L exemption in the "
                "year you eventually sell. If you would have been under it anyway, this shelters "
                "nothing."
            )
        report.notes.append(
            f"Future tax saving discounted at {self.opp['discount_rate']:.0%} over "
            f"{self.opp['expected_holding_years']} years. Costs from costs.yaml — "
            f"check them against your actual broker and scheme."
        )
