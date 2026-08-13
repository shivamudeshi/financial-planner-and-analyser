# Financial Planner & Analyser

A local-first tool for planning, tracking and analysing a personal portfolio of **Indian mutual
funds and listed equities**. Single user, one SQLite file, no hosting, no API key.

Its centrepiece answers one narrow question well:

> Given today's lots and prices, what is the largest set of sales that keeps this financial year's
> capital-gains tax at **exactly zero**?

## Quick start

```bash
pip install -r requirements.txt

python -m fpa.cli sample     # generate a sample portfolio to explore
python -m fpa.cli plan       # print this FY's zero-tax sell plan
python -m fpa.cli plan --economics --mode HARVEST   # ...and what it costs
streamlit run app.py         # dashboard
```

Starting from scratch instead of the sample: skip `sample`, import your transactions, then
`python -m fpa.cli rebuild`.

```
  Zero-tax sell plan — FY 2026-27, as of 2026-08-13, mode EXIT
  ────────────────────────────────────────────────────────────────────────────
  DMART                                 ₹4.69 L  gain  -₹1.94 L
      Sell 165 shares for ₹4,68,971, booking a ₹1,93,637 loss, across 3 lot(s), at zero tax.
  Quant Small Cap - Direct Growth       ₹3.17 L  gain   ₹1.42 L  (partial)
      Redeem 955.308 units for ₹3,16,866, realising ₹1,23,559 long-term and
      ₹18,282 short-term gain, across 36 lot(s), at zero tax.

  Proceeds ₹18,98,320   gain realised ₹1,33,244   tax ₹0
  Exemption left: ₹0   avoided vs selling outright: ₹1,54,715

  Wait, don't sell:
    Quant Small Cap - Direct Growth    fully long-term in 323d (2027-07-02) — saves ₹1,839
```

## Why this exists

Existing trackers (Kuvera, INDmoney, Zerodha Console) show you what you hold. None of them plan a
financial year's exits around the tax code. Three rules make that worth automating:

- **The ₹1.25 lakh s.112A exemption is use-it-or-lose-it.** Unused exemption does not carry
  forward. Finish the year under the limit and the remainder is gone permanently.
- **The 12-month line is worth a lot and its date is knowable.** Short-term equity gain is taxed at
  20% from the first rupee; long-term at 12.5% after the exemption.
- **Set-off is mandatory (s.70), but its ordering is not.** You cannot bank a loss while holding
  gains in the same year.

That third rule is where naive planning goes wrong in *both* directions. Book a loss with no gain
to shelter and it is destroyed against gain the exemption already covered. But book a loss
alongside extra gains and every rupee of loss **adds** a rupee to the zero-tax budget — with ₹3L of
unrealised gain and a ₹1L loss, the right plan realises ₹2.25L of gain, not ₹1.25L. The planner
books losses before selecting gains, capped at what the available gains can absorb.

## Two modes

| Mode | Objective | Prefers |
|---|---|---|
| `EXIT` | Free the most capital from positions you want out of | **Lowest** gain % — most market value per rupee of gain budget |
| `HARVEST` | Step up cost basis for free, then rebuy | **Highest** gain % — same gain on less turnover |

Set an **exit priority** (0–100) per holding on the Holdings page to tell the planner what you
actually want out of.

## Opportunity cost — the counterweight

A zero-tax plan optimises tax in isolation, and tax is not the objective. `--economics` (and the
dashboard's Opportunity cost section) prices what the saving actually costs:

```
  Tax avoided now                          ₹0
  Future tax saved (PV)               ₹10,755
  Transaction costs                     -₹330
  ────────────────────────────────────────────
  Net benefit                         ₹10,424

  Quant Small Cap - Direct Growth    net      ₹6,752
    Worth doing — ₹6,752 net, against ₹3,283 of one-sigma gap risk.

  Is waiting worth it?
    Tata Consultancy Services          saves     ₹3,302 (0.9% of position)
      Tax saving is noise here: 0.9% of the position against a 6.0% one-sigma
      move over 28d. Decide on the merits, not the tax.
```

Three trade-offs get quantified:

- **Harvesting.** The benefit is *deferred* — you save 12.5% of the harvested gain at some future
  sale — so it is discounted to present value, then netted against round-trip charges, exit load
  and the days you spend out of the market. Two costs are easy to miss and are flagged every time:
  rebought units start a **fresh 12-month holding period**, and for equity a same-session buy-back
  may be netted as an intraday trade, in which case no delivery-based capital gain arises and the
  harvest achieves nothing.
- **Deferring.** The star metric is the **breakeven decline** — the price fall that exactly cancels
  the tax saved — measured against the position's actual volatility over that window. Saving 0.9%
  of position value while accepting a 6% one-sigma move is not a reason to keep holding.
- **Selling anyway.** When a position should be exited on its merits, refusing to pay 12.5% can
  cost far more than the tax.

In HARVEST mode "tax avoided now" is deliberately **excluded** from the net benefit: you rebuy the
position, so the alternative is doing nothing, not selling out. Counting it would flatter the
result by an order of magnitude.

One caveat runs through all of it: harvesting only pays if your realised gains exceed the ₹1.25L
exemption **in the year you eventually sell**. If you would have been under it anyway, the harvest
sheltered nothing.

Costs are broker- and scheme-specific — check [`fpa/planner/costs.yaml`](fpa/planner/costs.yaml)
against your actual brokerage and exit loads before trusting the net numbers.

## What it does and doesn't do

**Does:** FIFO tax lots with correct STCG/LTCG split · zero-tax sell planning · opportunity-cost
and breakeven analysis · "wait N days" deferral advice · XIRR on real cashflows · equity technicals
· FY tax summary with carry-forward.

**Doesn't:** place orders (it never transacts), predict prices, handle F&O, or need an API key.

## Data sources — all free, no key

| Need | Source |
|---|---|
| MF NAV, all ~12k schemes | AMFI `NAVAll.txt` |
| MF NAV history | mfapi.in (unofficial AMFI mirror) |
| Equity OHLCV | Yahoo Finance via `yfinance` (`RELIANCE.NS`) |
| Your holdings | CAMS/KFintech CAS PDF, broker tradebook CSV |

`python -m fpa.cli refresh` pulls the latest of both. A broker API (Angel One / Dhan / Upstox) is an
optional later addition for live quotes — the ingest boundary is deliberately narrow so it slots in
behind the same `prices` table.

## ⚠ On the tax numbers

Rates live in one file, [`fpa/tax/rates.yaml`](fpa/tax/rates.yaml), and **must be verified against
the current Finance Act before you act on anything this prints.** Rates moved materially in the July
2024 Budget and can move at any Budget. FY 2026-27 values are carried forward as a placeholder and
are *not* verified. Every figure the app shows is stamped with the FY whose parameters produced it.

This is a personal tool, not tax advice. Check anything material with a CA before filing.

## Architecture

```
fpa/
├── db.py            SQLite schema; transactions are the only source of truth
├── money.py         paise as int — never float
├── lots.py          FIFO lot construction, rebuilt wholesale from transactions
├── portfolio.py     open lots valued at latest price
├── tax/
│   ├── rates.yaml   ← the only place rates are defined
│   └── engine.py    set-off, exemption, liability
├── planner/
│   ├── sell_planner.py    the zero-tax planner
│   ├── opportunity.py     what the tax saving costs
│   └── costs.yaml         ← check against your broker
├── analysis/        technicals (equity only), XIRR/drawdown
├── ingest/          AMFI, yfinance
└── cli.py
app.py               Streamlit dashboard
```

Derived state (`lots`, `disposals`) is a pure function of `transactions`, so a bad import is fixed
by correcting the transaction and re-running `rebuild` — never by patching derived rows.

**No technical indicators are computed on mutual fund NAV.** A NAV series has no volume and no order
flow; RSI or MACD on it describes the arithmetic of a daily valuation, not market behaviour. Funds
get XIRR, rolling returns and overlap instead.

## Tests

```bash
python -m pytest tests/ -q     # 67 tests
```

The load-bearing invariant — every plan produces exactly zero tax — is tested directly, alongside
the set-off rules, the exemption boundary, FIFO matching, and the two modes' opposing preferences.
If a Finance Act changes a rate, update `rates.yaml` and the tests together: a green suite against
stale rates is worse than no suite.

## Status

Built: tax engine, FIFO lots, sell planner, opportunity-cost analysis, AMFI + yfinance ingest,
dashboard, sample data. Plans a 10,000-lot ledger in ~1.3s.
Not yet: CAS PDF parser, fundamentals ingest, standing rules engine with alerts, rebalancing,
backtest mode. See [DESIGN.md](DESIGN.md) §12 for phasing.
