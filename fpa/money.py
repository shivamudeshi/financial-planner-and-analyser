"""Money is stored and computed in paise as ``int``. Never float.

Rupee floats break at the third decimal and tax thresholds are exact numbers,
so every amount that crosses the DB or the tax engine is an integer count of
paise. Conversion to rupees happens only at the display edge.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

PAISE = 100
LAKH = 100_000


def to_paise(rupees: float | str | Decimal) -> int:
    """Rupees -> paise, half-up at the paisa."""
    return int(Decimal(str(rupees)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * PAISE)


def to_rupees(paise: int) -> Decimal:
    return (Decimal(paise) / PAISE).quantize(Decimal("0.01"))


def fmt(paise: int, *, decimals: bool = False) -> str:
    """Format paise in the Indian grouping convention: 12,34,567."""
    neg = paise < 0
    rupees = abs(Decimal(paise) / PAISE)
    whole = int(rupees)
    frac = rupees - whole

    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail

    if decimals:
        s += f".{int(frac * 100):02d}"
    return f"{'-' if neg else ''}₹{s}"


def fmt_compact(paise: int) -> str:
    """Lakh/crore shorthand for dashboard tiles."""
    r = abs(paise) / PAISE
    sign = "-" if paise < 0 else ""
    if r >= 1e7:
        return f"{sign}₹{r / 1e7:.2f} Cr"
    if r >= 1e5:
        return f"{sign}₹{r / 1e5:.2f} L"
    if r >= 1e3:
        return f"{sign}₹{r / 1e3:.1f} K"
    return f"{sign}₹{r:.0f}"


def pct(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else 100.0 * numerator / denominator
