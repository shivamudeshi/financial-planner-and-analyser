"""Indian capital-gains computation for listed equity and equity-oriented MFs.

Scope matches the app: s.111A short-term, s.112A long-term with the annual
exemption, plus a slab bucket for non-equity (e.g. debt MF bought on/after
2023-04-01). No F&O, no business income.

Two rules drive most of the behaviour here and are worth stating plainly:

1. **Set-off is mandatory, not optional.** Under s.70/74 a current-year or
   brought-forward capital loss *shall* be set off against capital gains of the
   same head. You cannot elect to carry a loss forward while holding gains in
   the same year. This is why booking a loss in a year when your long-term gain
   is already under the s.112A exemption genuinely destroys the loss — the
   planner treats that as a cost, not a saving.

2. **Loss ordering is discretionary.** The Act does not prescribe which gain a
   loss must be set off against, so we set off against the highest-taxed bucket
   first, which is optimal and uncontroversial. The s.112A exemption is applied
   to what remains *after* set-off, matching how the ITR utility computes it.
   That is the conservative reading; see ``_apply_exemption``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

import yaml

from ..money import to_paise

RATES_PATH = Path(__file__).with_name("rates.yaml")


class Term(str, Enum):
    SHORT = "SHORT_TERM"
    LONG = "LONG_TERM"


class Regime(str, Enum):
    """Which tax treatment an instrument's gains fall under."""

    EQUITY = "EQUITY"  # listed equity + equity-oriented MF, STT paid: s.111A / s.112A
    OTHER = "OTHER"  # debt MF post-Apr-2023 etc: slab short-term, 12.5% long-term


@dataclass(frozen=True)
class Gain:
    """One realised gain or loss. Negative ``amount`` is a loss."""

    amount: int  # paise, signed
    term: Term
    regime: Regime = Regime.EQUITY
    label: str = ""


@dataclass
class Bucket:
    key: str
    term: Term
    regime: Regime
    rate: float
    gross: int = 0
    setoff: int = 0
    exempt: int = 0

    @property
    def taxable(self) -> int:
        return max(0, self.gross - self.setoff - self.exempt)

    @property
    def tax(self) -> int:
        return round(self.taxable * self.rate)


@dataclass
class TaxResult:
    fy: str
    buckets: list[Bucket]
    stcl_used: int = 0
    ltcl_used: int = 0
    bf_stcl_used: int = 0
    bf_ltcl_used: int = 0
    carry_forward_stcl: int = 0
    carry_forward_ltcl: int = 0
    exemption_limit: int = 0
    exemption_used: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def exemption_unused(self) -> int:
        return max(0, self.exemption_limit - self.exemption_used)

    @property
    def tax_before_cess(self) -> int:
        return sum(b.tax for b in self.buckets)

    @property
    def cess(self) -> int:
        return round(self.tax_before_cess * self._cess_rate)

    @property
    def total_tax(self) -> int:
        return self.tax_before_cess + self.cess

    _cess_rate: float = 0.0

    def bucket(self, key: str) -> Bucket:
        return next(b for b in self.buckets if b.key == key)


class TaxEngine:
    """Computes liability for one financial year under one FY's parameters."""

    def __init__(self, fy: str | None = None, rates_path: Path = RATES_PATH):
        raw = yaml.safe_load(rates_path.read_text())
        self.fy = fy or raw["default_fy"]
        if self.fy not in raw["fy"]:
            raise ValueError(
                f"No tax parameters for FY {self.fy}. Add it to {rates_path.name} "
                f"(available: {', '.join(sorted(raw['fy']))})"
            )
        p = raw["fy"][self.fy]
        self.long_term_days: dict[str, int] = p["long_term_holding_days"]
        self.equity_stcg_rate: float = p["equity_stcg_rate"]
        self.equity_ltcg_rate: float = p["equity_ltcg_rate"]
        self.equity_ltcg_exemption: int = to_paise(p["equity_ltcg_exemption"])
        self.other_ltcg_rate: float = p["other_ltcg_rate"]
        self.slab_rate: float = p["slab_rate"]
        self.cess_rate: float = p["cess"]
        self.carry_forward_years: int = p["loss_carry_forward_years"]
        self.grandfather_date: str = raw["grandfather"]["date"]

    # -- holding period ---------------------------------------------------

    def is_long_term(self, days_held: int, regime: Regime, kind: str = "EQUITY") -> bool:
        key = kind if kind in self.long_term_days else ("EQUITY" if regime is Regime.EQUITY else "OTHER")
        return days_held > self.long_term_days[key]

    def term_for(self, days_held: int, regime: Regime, kind: str = "EQUITY") -> Term:
        return Term.LONG if self.is_long_term(days_held, regime, kind) else Term.SHORT

    def days_to_long_term(self, days_held: int, regime: Regime, kind: str = "EQUITY") -> int:
        key = kind if kind in self.long_term_days else ("EQUITY" if regime is Regime.EQUITY else "OTHER")
        return max(0, self.long_term_days[key] + 1 - days_held)

    # -- computation ------------------------------------------------------

    def _new_buckets(self) -> list[Bucket]:
        return [
            Bucket("OTHER_STCG", Term.SHORT, Regime.OTHER, self.slab_rate),
            Bucket("EQUITY_STCG", Term.SHORT, Regime.EQUITY, self.equity_stcg_rate),
            Bucket("EQUITY_LTCG", Term.LONG, Regime.EQUITY, self.equity_ltcg_rate),
            Bucket("OTHER_LTCG", Term.LONG, Regime.OTHER, self.other_ltcg_rate),
        ]

    def compute(
        self,
        gains: Iterable[Gain],
        *,
        bf_stcl: int = 0,
        bf_ltcl: int = 0,
    ) -> TaxResult:
        """Liability for a set of realised gains, plus brought-forward losses.

        ``bf_stcl`` / ``bf_ltcl`` are positive magnitudes of losses carried
        forward from earlier years.
        """
        buckets = self._new_buckets()
        by_key = {b.key: b for b in buckets}

        stcl = ltcl = 0
        for g in gains:
            key = f"{g.regime.value}_{'STCG' if g.term is Term.SHORT else 'LTCG'}"
            if g.amount >= 0:
                by_key[key].gross += g.amount
            elif g.term is Term.SHORT:
                stcl += -g.amount
            else:
                ltcl += -g.amount

        res = TaxResult(fy=self.fy, buckets=buckets, exemption_limit=self.equity_ltcg_exemption)
        res._cess_rate = self.cess_rate

        long_buckets = [b for b in buckets if b.term is Term.LONG]
        short_buckets = [b for b in buckets if b.term is Term.SHORT]
        by_rate = lambda bs: sorted(bs, key=lambda b: -b.rate)  # noqa: E731

        # Long-term losses are the least flexible resource, so spend them first.
        res.ltcl_used = self._absorb(ltcl, by_rate(long_buckets))
        res.bf_ltcl_used = self._absorb(bf_ltcl, by_rate(long_buckets))
        # Short-term losses can go anywhere; highest-taxed bucket first.
        res.stcl_used = self._absorb(stcl, by_rate(short_buckets) + by_rate(long_buckets))
        res.bf_stcl_used = self._absorb(bf_stcl, by_rate(short_buckets) + by_rate(long_buckets))

        res.carry_forward_ltcl = (ltcl - res.ltcl_used) + (bf_ltcl - res.bf_ltcl_used)
        res.carry_forward_stcl = (stcl - res.stcl_used) + (bf_stcl - res.bf_stcl_used)

        self._apply_exemption(res, by_key["EQUITY_LTCG"])
        self._add_notes(res, ltcl, stcl)
        return res

    @staticmethod
    def _absorb(loss: int, targets: list[Bucket]) -> int:
        """Consume ``loss`` against buckets in order. Returns amount used."""
        used = 0
        for b in targets:
            if loss <= 0:
                break
            room = max(0, b.gross - b.setoff)
            take = min(room, loss)
            b.setoff += take
            loss -= take
            used += take
        return used

    def _apply_exemption(self, res: TaxResult, eq_lt: Bucket) -> None:
        """s.112A exemption, applied to long-term equity gain remaining after set-off.

        Applying it post-set-off is the conservative reading and matches the ITR
        utility. The consequence — that a loss booked against an otherwise-exempt
        gain is destroyed — is real, and the planner is built to avoid creating
        that situation rather than to argue about the ordering.
        """
        remaining = max(0, eq_lt.gross - eq_lt.setoff)
        eq_lt.exempt = min(remaining, self.equity_ltcg_exemption)
        res.exemption_used = eq_lt.exempt

    def _add_notes(self, res: TaxResult, ltcl: int, stcl: int) -> None:
        if res.exemption_unused > 0 and (ltcl or stcl):
            res.notes.append(
                f"₹{res.exemption_unused / 100:,.0f} of the s.112A exemption went unused while "
                f"losses were set off against long-term gains — those losses were spent "
                f"sheltering gain that was already tax-free."
            )
        if res.carry_forward_stcl or res.carry_forward_ltcl:
            res.notes.append(
                f"Carrying forward STCL {res.carry_forward_stcl / 100:,.0f} and "
                f"LTCL {res.carry_forward_ltcl / 100:,.0f}. Valid {self.carry_forward_years} "
                f"assessment years, but only if the return is filed by the s.139(1) due date."
            )

    # -- planner support --------------------------------------------------

    def is_zero_tax(self, gains: Iterable[Gain], *, bf_stcl: int = 0, bf_ltcl: int = 0) -> bool:
        return self.compute(gains, bf_stcl=bf_stcl, bf_ltcl=bf_ltcl).total_tax == 0

    def grandfathered_cost(self, actual_cost: int, fmv_2018: int | None, sale_value: int) -> int:
        """s.112A cost for equity acquired before 2018-01-31.

        cost = max(actual, min(FMV on 31-Jan-2018, sale price))
        """
        if fmv_2018 is None:
            return actual_cost
        return max(actual_cost, min(fmv_2018, sale_value))
