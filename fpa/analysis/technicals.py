"""Technical indicators — equity only.

Deliberately not applied to mutual fund NAV. A NAV series has no volume, no
order flow and no traded price; RSI or MACD computed on it describes the
arithmetic of a daily valuation, not market behaviour. MF analysis lives in
:mod:`fpa.analysis.returns` instead.

These are inputs to the rules you write, not signals in themselves.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pandas as pd

from ..money import pct


def price_frame(conn: sqlite3.Connection, instrument_id: int, as_of: str | None = None) -> pd.DataFrame:
    sql = "SELECT date, open, high, low, close, volume FROM prices WHERE instrument_id=?"
    args: list = [instrument_id]
    if as_of:
        sql += " AND date <= ?"
        args.append(as_of)
    df = pd.read_sql_query(sql + " ORDER BY date", conn, params=args, parse_dates=["date"])
    return df.set_index("date")


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI (exponential smoothing, not a simple mean)."""
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    return (100 - 100 / (1 + rs)).fillna(100)


def macd(s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(s, fast) - ema(s, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "signal": sig, "histogram": line - sig})


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def bollinger(s: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(s, n)
    sd = s.rolling(n, min_periods=n).std()
    return pd.DataFrame({"mid": mid, "upper": mid + k * sd, "lower": mid - k * sd})


@dataclass
class Snapshot:
    """Latest indicator readings for one instrument."""

    close: int
    sma20: float | None
    sma50: float | None
    sma200: float | None
    rsi14: float | None
    macd_hist: float | None
    atr14: float | None
    high_52w: int | None
    low_52w: int | None
    from_52w_high: float | None
    above_200dma: bool | None
    trend: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _last(s: pd.Series) -> float | None:
    if s.empty:
        return None
    v = s.iloc[-1]
    return None if pd.isna(v) else float(v)


def snapshot(conn: sqlite3.Connection, instrument_id: int, as_of: str | None = None) -> Snapshot | None:
    df = price_frame(conn, instrument_id, as_of)
    if df.empty:
        return None

    close = df["close"].astype(float)
    s20, s50, s200 = _last(sma(close, 20)), _last(sma(close, 50)), _last(sma(close, 200))
    last = int(close.iloc[-1])
    window = close.tail(252)
    hi, lo = int(window.max()), int(window.min())

    if s50 and s200:
        trend = "Uptrend" if s50 > s200 else "Downtrend"
    elif s20 and s50:
        trend = "Uptrend" if s20 > s50 else "Downtrend"
    else:
        trend = "Insufficient history"

    return Snapshot(
        close=last,
        sma20=s20,
        sma50=s50,
        sma200=s200,
        rsi14=_last(rsi(close)),
        macd_hist=_last(macd(close)["histogram"]),
        atr14=_last(atr(df.astype(float))) if {"high", "low"} <= set(df.columns) and df["high"].notna().any() else None,
        high_52w=hi,
        low_52w=lo,
        from_52w_high=pct(last - hi, hi),
        above_200dma=(last > s200) if s200 else None,
        trend=trend,
    )


def realised_volatility(
    conn: sqlite3.Connection, instrument_id: int, *, lookback: int = 252, as_of: str | None = None
) -> float | None:
    """Annualised standard deviation of daily log returns, in percent.

    Used to size the price risk of *waiting* — the counterweight to any tax
    saving. Backward-looking and therefore only a rough guide to the next few
    weeks, but it is the honest order-of-magnitude check on whether a tax
    saving is large or small next to the market noise you are accepting.
    """
    import numpy as np

    s = price_frame(conn, instrument_id, as_of)["close"].astype(float).tail(lookback)
    if len(s) < 20:
        return None
    returns = np.log(s / s.shift(1)).dropna()
    if returns.empty or returns.std() == 0:
        return None
    return float(returns.std() * (252**0.5) * 100)


def volatility_over(
    conn: sqlite3.Connection, instrument_id: int, days: int, as_of: str | None = None
) -> float | None:
    """Annualised vol rescaled to a ``days``-long window (square-root of time)."""
    annual = realised_volatility(conn, instrument_id, as_of=as_of)
    if annual is None or days <= 0:
        return None
    return annual * (days / 365) ** 0.5


def relative_strength(
    conn: sqlite3.Connection, instrument_id: int, benchmark_id: int, days: int = 252
) -> float | None:
    """Excess return over a benchmark across ``days``, in percentage points."""
    a = price_frame(conn, instrument_id)["close"].tail(days)
    b = price_frame(conn, benchmark_id)["close"].tail(days)
    if len(a) < 2 or len(b) < 2:
        return None
    return pct(int(a.iloc[-1] - a.iloc[0]), int(a.iloc[0])) - pct(
        int(b.iloc[-1] - b.iloc[0]), int(b.iloc[0])
    )
