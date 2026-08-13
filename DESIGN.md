# Financial Planner & Analyser — Design

A local-first tool to plan, track, and analyse a personal portfolio of **Indian mutual funds and
listed equities**. Single user. No F&O, no intraday, no external hosting.

---

## 1. Goals and non-goals

### Goals

1. **Decide what to sell this financial year, at zero tax** — the centrepiece. Given today's lots
   and prices, find the largest set of sales that keeps the year's capital-gains tax at exactly
   zero. See §6.
2. **Track** — one combined view of MF and equity holdings with correct FIFO cost basis, realised
   and unrealised P&L, and XIRR.
3. **Plan** — yearly targets (investable surplus, asset allocation, SIP schedule) and actual vs.
   plan as the year progresses.
4. **Analyse** — technicals and fundamentals on the equity side, rolling-return and attribution
   metrics on the MF side.

### Non-goals

- Order placement. This tool never transacts. It tells you; you act in your broker app.
- Real-time / tick data. End-of-day is sufficient for a yearly planning horizon.
- Multi-user, auth, cloud hosting.
- F&O, commodities, crypto, bonds. (Debt MFs are in scope only as an asset-allocation bucket.)
- Prediction. No price forecasting, no ML on returns.

### Design principles

- **No API key required to be useful.** Every phase-1 data source is free and keyless. Broker APIs
  are an optional later enhancement, not a foundation.
- **Local-first.** One SQLite file. Data never leaves the machine.
- **Reproducible.** All derived numbers recomputable from raw transactions + price history.
- **Boring tech.** Python, SQLite, Streamlit. Nothing to operate.

---

## 2. Architecture

```
                  ┌──────────────────────────────────────────┐
   free, keyless  │  AMFI NAVAll.txt      (MF NAV, daily)     │
   data sources   │  yfinance             (equity OHLCV)      │
                  │  NSE/BSE XBRL         (fundamentals)      │
                  └────────────────┬─────────────────────────┘
                                   │
   your data      ┌────────────────▼─────────────────────────┐
   (manual/file)  │  CAMS/KFintech CAS PDF  → MF txns         │
                  │  Broker tradebook CSV   → equity txns     │
                  └────────────────┬─────────────────────────┘
                                   │
                           ┌───────▼────────┐
                           │  ingest layer  │  normalise, dedupe, upsert
                           └───────┬────────┘
                                   │
                           ┌───────▼────────┐
                           │  portfolio.db  │  SQLite — single source of truth
                           └───────┬────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
      ┌───────▼──────┐    ┌────────▼───────┐   ┌────────▼───────┐
      │  analysis    │    │  rules engine  │   │    planner     │
      │  technicals  │    │  → alerts      │   │  targets, SIP  │
      │  MF metrics  │    │                │   │  rebalancing   │
      │  fundamentals│    │                │   │                │
      └───────┬──────┘    └────────┬───────┘   └────────┬───────┘
              └────────────────────┼────────────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │  app.py — Streamlit         │
                    │  daily_job.py — cron/GH     │
                    └─────────────────────────────┘
```

### Repo layout

```
financial-planner-and-analyser/
├── fpa/
│   ├── ingest/
│   │   ├── amfi.py           # daily + historical NAV
│   │   ├── equity_prices.py  # yfinance OHLCV, corporate actions
│   │   ├── cas.py            # CAMS/KFintech CAS PDF parser
│   │   ├── tradebook.py      # broker CSV → transactions
│   │   └── fundamentals.py   # XBRL / yfinance financials
│   ├── db/
│   │   ├── schema.sql
│   │   └── migrations/
│   ├── analysis/
│   │   ├── technicals.py     # equity only
│   │   ├── mf_metrics.py     # rolling returns, alpha, overlap
│   │   ├── fundamentals.py   # ratio computation, trend flags
│   │   └── returns.py        # XIRR, TWRR, drawdown
│   ├── rules/
│   │   ├── engine.py
│   │   └── rules.yaml        # ← you edit this
│   ├── planner/
│   │   ├── targets.py
│   │   └── rebalance.py
│   ├── tax/
│   │   ├── lots.py           # FIFO lot matching
│   │   └── rates.yaml        # ← verify each Budget
│   └── config.yaml
├── app.py                    # streamlit run app.py
├── daily_job.py              # refresh prices, evaluate rules, notify
└── data/portfolio.db         # gitignored
```

---

## 3. Data sources

| Need | Source | Key | Notes |
|---|---|---|---|
| MF NAV, all schemes, daily | `amfiindia.com/spages/NAVAll.txt` | No | Plain text, ~12k schemes, updated ~11pm IST on business days |
| MF NAV history | `api.mfapi.in/mf/{code}` | No | Unofficial AMFI mirror. Convenient; treat as best-effort and cache locally |
| Equity OHLCV + splits/bonus | `yfinance`, symbols as `RELIANCE.NS` / `500325.BO` | No | Adjusted close handles corporate actions. Unofficial API — pin the version |
| Index / benchmark levels | yfinance `^NSEI`, `^NSEBANK`, `^CRSLDX` | No | Needed for alpha/beta and relative strength |
| MF holdings & transactions | CAMS/KFintech **CAS** PDF | No | Request from camsonline/kfintech, password-protected, monthly. Authoritative for units and cost |
| Equity holdings & transactions | Broker tradebook CSV export | No | Zerodha/Groww/Upstox all export. Authoritative for cost basis |
| Fundamentals | yfinance `.financials` / `.balance_sheet` | No | Coverage for Indian names is patchy but workable |
| Fundamentals, deeper | NSE/BSE quarterly XBRL filings | No | Free and authoritative, but messy parsing. Phase 3 |
| Live quotes, auto-sync holdings | Angel One SmartAPI / Dhan / Upstox | **Yes** | Free tiers exist; Zerodha Kite Connect is paid. Optional, phase 4 |

**On scraping Screener.in / Trendlyne:** they have the cleanest Indian fundamentals but no public API,
and scraping them is against their terms. Not designed in. If the XBRL route proves too painful, the
honest fallback is manual entry of a dozen ratios per stock per quarter — for a portfolio of 15–30
names that is perhaps 20 minutes a quarter.

**Reliability stance:** every external source is treated as untrusted and cached. Ingest is
idempotent and never destructive — a failed fetch leaves yesterday's data intact and logs a
staleness warning that the dashboard surfaces.

---

## 4. Data model

SQLite. Money as `INTEGER` paise, not floats. Dates as `TEXT` ISO-8601.

```sql
-- What can be held
instruments (
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL,     -- 'EQUITY' | 'MF'
  name          TEXT NOT NULL,
  isin          TEXT UNIQUE,
  symbol        TEXT,              -- 'RELIANCE' (equity)
  exchange      TEXT,              -- 'NSE' | 'BSE'
  amfi_code     TEXT,              -- scheme code (MF)
  category      TEXT,              -- 'LARGE_CAP' | 'FLEXI_CAP' | 'DEBT' | ...
  asset_class   TEXT NOT NULL,     -- 'EQUITY' | 'DEBT' | 'HYBRID' | 'GOLD'
  benchmark_id  INTEGER REFERENCES instruments(id),
  tags          TEXT,              -- JSON array, drives rule scoping
  active        INTEGER DEFAULT 1
);

-- Unified price/NAV series
prices (
  instrument_id INTEGER NOT NULL REFERENCES instruments(id),
  date          TEXT NOT NULL,
  open, high, low, close, adj_close  INTEGER,   -- paise; MF uses close only
  volume        INTEGER,
  source        TEXT NOT NULL,
  PRIMARY KEY (instrument_id, date)
);

-- Raw truth. Everything else derives from this.
transactions (
  id            INTEGER PRIMARY KEY,
  instrument_id INTEGER NOT NULL REFERENCES instruments(id),
  date          TEXT NOT NULL,
  kind          TEXT NOT NULL,     -- BUY|SELL|SIP|DIVIDEND|BONUS|SPLIT|SWITCH_IN|SWITCH_OUT
  quantity      REAL NOT NULL,     -- MF units are fractional
  price         INTEGER,           -- paise per unit
  amount        INTEGER NOT NULL,  -- paise, signed
  charges       INTEGER DEFAULT 0, -- brokerage + STT + stamp + GST
  account       TEXT,              -- folio no. or broker
  external_id   TEXT UNIQUE,       -- dedupe key from CAS/tradebook
  note          TEXT
);

-- FIFO tax lots, rebuilt from transactions
lots (
  id            INTEGER PRIMARY KEY,
  instrument_id INTEGER NOT NULL REFERENCES instruments(id),
  buy_date      TEXT NOT NULL,
  quantity      REAL NOT NULL,
  remaining_qty REAL NOT NULL,
  cost_per_unit INTEGER NOT NULL,
  grandfathered_cost INTEGER       -- s.112A, for pre-2018-01-31 buys
);

disposals (                        -- realised gains, one row per lot consumed
  id, lot_id, sell_txn_id, quantity, sale_value, cost, gain, term  -- 'STCG'|'LTCG'
);

fundamentals (
  instrument_id, period_end, period_type,  -- 'Q'|'A'
  metric TEXT, value REAL, source TEXT,
  PRIMARY KEY (instrument_id, period_end, period_type, metric)
);

-- Planning
plan_targets (year, asset_class, target_pct, target_amount, note);
plan_cashflows (year, month, expected_surplus, actual_invested);

-- Rules & alerts
alerts (
  id, rule_name, instrument_id, fired_on, severity,
  message, context TEXT,           -- JSON snapshot of why it fired
  status TEXT                      -- 'NEW'|'ACKED'|'ACTED'|'MUTED'
);
```

**Why lots matter:** Indian tax requires FIFO matching for listed equity, and the STCG/LTCG split
drives real money. Deriving lots properly from day one avoids a painful retrofit.

---

## 5. Analysis layer

### 5.1 Equity — technicals

Trend: SMA/EMA 20 / 50 / 200, price vs. 200DMA, golden/death cross state.
Momentum: RSI(14), MACD(12,26,9), rate of change 1m/3m/6m/12m.
Volatility: ATR(14), Bollinger(20,2), realised vol.
Position: distance from 52-week high/low, drawdown from peak.
Relative: relative strength vs. Nifty 50 and vs. sector index.
Volume: 20-day average, volume spike flag, OBV.

These feed the rules engine as *inputs*. They are not, on their own, buy/sell signals.

### 5.2 Equity — fundamentals

Per quarter and per year, with 3–8 quarter trend direction:

- Growth: revenue, EBITDA, PAT, YoY and QoQ
- Profitability: gross/EBITDA/net margin, ROE, ROCE
- Balance sheet: debt/equity, interest coverage, current ratio
- Cash: CFO, CFO/PAT (an accrual-quality check that catches a lot)
- Valuation: P/E, P/B, EV/EBITDA, and each vs. its own 3/5-year median — the *relative* reading is
  what matters, absolute P/E across sectors is noise
- Governance: promoter holding trend, promoter pledge %

### 5.3 Mutual funds — deliberately different

**No technical analysis on NAV.** A NAV series has no volume, no order flow, and no counterparty —
RSI or MACD on it is a category error. MF analysis is:

- **XIRR** — the only correct return metric when you're running SIPs. Headline "3-year return" on a
  fund factsheet is not your return.
- **Rolling returns** — 1/3/5-year returns computed over every rolling window, not point-to-point.
  Point-to-point return is an artifact of its start date.
- Risk: max drawdown, recovery time, standard deviation, Sharpe, Sortino
- Vs. benchmark: alpha, beta, up/down capture, and rolling excess return
- **Portfolio overlap** — pairwise common-holding % between your funds. The single most common
  problem in a retail MF portfolio is six funds that are quietly the same fund.
- Expense ratio drag, compounded over your actual holding period
- Category and AMC concentration

---

## 6. The zero-tax sell planner  ✅ built

Three features of Indian capital-gains law make an annual planner worth having, and together they
define the whole problem:

1. **The s.112A exemption is use-it-or-lose-it.** ₹1.25 lakh of long-term equity gain per financial
   year is tax-free, and unused exemption does *not* carry forward. Every year you finish under the
   limit, the remainder is gone permanently.
2. **The 12-month line is worth a lot and its date is knowable.** Short-term equity gain is taxed at
   20% from the first rupee; long-term at 12.5% after the exemption. A lot 40 days short of
   long-term is a very different asset from one 40 days past it.
3. **Set-off is mandatory (s.70), but its ordering is not.** You cannot elect to bank a loss while
   holding gains in the same year — the loss *must* be set off. This cuts both ways, and getting it
   right is most of the value here.

### The loss insight

That third point is where naive planning goes wrong, in both directions:

- Book a loss with no gain to shelter, and it is **destroyed** — set off against gain the exemption
  was already covering, buying nothing.
- But book a loss *alongside extra gains*, and every rupee of loss **adds a rupee** to the zero-tax
  budget. With ₹3L of unrealised gain and a ₹1L loss on a position you want out of, the right plan
  realises ₹2.25L of gain — ₹1.25L exemption plus ₹1L sheltered by the loss — not ₹1.25L.

So the planner books losses *before* selecting gains, and caps them at what the available gains can
actually absorb. The surplus is reported, not realised.

### Two modes

| Mode | Objective | Lot preference |
|---|---|---|
| **EXIT** | Free the most capital from positions you want out of | **Lowest** gain % — releases the most market value per rupee of gain budget |
| **HARVEST** | Step up cost basis for free | **Highest** gain % — same gain realised on less turnover, so less brokerage and less time out of the market |

Deliberately opposite preferences under the same constraint, and both are tested.

### How the constraint is enforced

Gain is linear in quantity and tax is monotonic in gain, so the planner **bisects on quantity using
the real tax engine as the oracle** — no bucket-level reasoning is duplicated, and set-off rules
live in exactly one place. Quantities round *down* to tradeable size (whole shares, 3-dp MF units);
rounding up would break the invariant. Every proposal is re-verified after quantisation.

### Outputs

- **Orders to place**, one row per instrument (your broker applies FIFO across lots itself — a
  three-year SIP is 36 lots but one redemption), with lot detail underneath for the tax record.
- **"Wait, don't sell"** — short-term lots that become long-term, with the date and the rupee cost
  of selling early.
- **Warnings** — losses held back because there is not enough gain to absorb them.
- **Unused exemption**, with the reminder that it expires on 31 March.

### Performance

The planner bisects per candidate lot, so a naive implementation is fine at 200 lots and unusable at
10,000. Probing the smallest order worth placing *before* bisecting cuts a 10,000-lot plan from 30s
to 1.3s: once the gain budget is spent, every remaining candidate would otherwise burn 48 iterations
converging on a quantity that gets discarded as dust.

---

## 7. Opportunity cost — the counterweight  ✅ built

A zero-tax plan optimises tax in isolation, and tax is not the objective. This module prices what
the saving costs, so the tax tail stops wagging the investment dog.

| Trade-off | Benefit | Cost |
|---|---|---|
| **Harvest** (sell + rebuy) | PV of `gain × 12.5%` saved at a future sale | Round-trip charges, exit load, 1–3 days out of market, **holding period resets** |
| **Defer** (wait for long-term) | `gain × (20% − 12.5%)`, or the full 20% if it becomes exempt | Price risk over the wait |
| **Sell anyway** | Exit a deteriorating position on its merits | The 12.5% you refused to pay |

**The breakeven metric.** For deferrals the decision-useful number is the price fall that exactly
cancels the tax saved, set against the position's realised volatility over that window. "Saves 0.9%
of position value against a 6.0% one-sigma move over 28 days" tells you immediately that the tax is
noise and the decision belongs on investment merits.

**Two costs that are easy to miss**, flagged on every harvest:

- Rebought units start a **fresh 12-month holding period**. If you may sell within a year,
  harvesting moves that sale from 12.5% to 20%.
- For equity, a same-session buy-back can be netted by the broker as an intraday trade — in which
  case no delivery-based capital gain arises and the harvest achieves *nothing*. Rebuy the next
  trading day.

**Counterfactual discipline.** In HARVEST mode "tax avoided now" is excluded from net benefit: you
rebuy, so the alternative is doing nothing, not selling out. Including it overstated the sample
plan's benefit by 15×. In EXIT mode it is included but labelled an upper bound, since it assumes
you would otherwise have sold every candidate position outright.

**The caveat that undercuts everything:** harvesting only pays if realised gains exceed the
exemption *in the year you finally sell*. Under it anyway, and the harvest sheltered nothing.

Cost parameters live in `fpa/planner/costs.yaml` and are broker- and scheme-specific. Exit load
dominates every other MF cost by orders of magnitude and is charged **per lot**, not per order — a
three-year SIP redemption is mostly load-free.

---

## 8. Import — CAS and tradebook  ✅ built

A CAS covers mutual funds; a broker tradebook covers equities. Together they are the whole portfolio,
and both are free.

### The importer refuses to guess

Statement layouts vary between CAMS and KFintech, change over time, and differ by AMC. A parser that
silently mis-reads one line produces a wrong cost basis → a wrong capital gain → a wrong tax number,
noticed only when it matters. So the CAS importer writes only what it can prove:

**Reconciliation.** The CAS states its own `Closing Unit Balance`. The parser recomputes it from the
transactions extracted; any scheme where the two disagree beyond rounding is rejected with the
discrepancy shown. This is what makes the parser trustworthy despite being built against a format
that cannot be exhaustively tested.

**Completeness.** A non-zero `Opening Unit Balance` means units were acquired before the statement
period — no purchase record, so no cost basis and no acquisition date. Held back by default, with a
prompt to re-request the CAS from inception. `--allow-partial` overrides.

The second guard was found by an end-to-end test through a real encrypted PDF, not by the text
fixtures: the parse reconciled perfectly and the import still silently dropped 100 units.

### Design notes

- **Charge rows are not transactions.** `*** Stamp Duty ***` and STT lines have the same shape as
  trades but no units; treating them as trades breaks the unit balance, which the reconciliation
  then catches.
- **The description is authoritative about direction.** Some statements print redemption units
  without parentheses, so sign is taken from "Redemption"/"Switch Out", not from the number.
- **Dedupe by content hash**, so overlapping statements can be re-imported safely.
- **Tradebook columns are mapped by alias**, not per broker. A missing required column is named,
  not guessed around.
- Tradebooks rarely carry charges (they live in the contract note), so those are estimated from
  `costs.yaml` and the count of estimated rows is reported, since it feeds cost basis.

---

## 9. Rules engine — ongoing alerts  ✅ built

Declarative rules in `rules.yaml`, evaluated against today's positions, producing **alerts** — never
orders. Four decisions carry the design.

### Conditions are not `eval()`

A rules file is configuration: it gets copied between machines and pasted from notes. An evaluator
that can reach `__import__` turns a config typo into arbitrary code execution. So expressions are
parsed to an AST and walked against a **whitelist** — attribute access, subscripting, lambdas,
comprehensions and imports are rejected at load time, not at evaluation time. Twelve sandbox-escape
attempts are in the suite.

Unknown *names* are errors rather than `None`, because a rule that silently evaluates a typo'd field
to nothing is a rule that silently never fires.

### Missing data means "don't fire"

A fund with three weeks of NAV history has no 200-day average. `sma200 = 0` would make every "price
above its 200 DMA" rule fire on it, so unavailable readings are `None` and a rule naming them simply
does not fire. `and` short-circuits, so `has_price_history and rsi14 < 30` works as a guard.

### Noise control is a correctness concern

An alert list you have stopped reading is worse than no alerts. Two mechanisms:

- A rule will not re-fire while an alert for the same rule *and subject* is still open.
- `cooldown_days` stops a persistent condition reappearing daily — a breached stop-loss stays
  breached, and reminding you every morning trains you to ignore the list.

### Alerts must stay explainable

Three months later the question is "why did this fire?", and a rule name does not answer it. Each
alert stores a JSON snapshot of **the fields the condition actually referenced** — not the whole
40-field context, because the two that fired the rule are the ones worth reading.

### Backtesting: the base rate is the point

`fpa rules backtest <name>` replays a rule over real price and transaction history and reports what
happened next. Crucially it reports that **against the base rate** — the median forward return
across every tested date, fired or not.

"The position fell 3% after this fired" looks like a working sell signal until you notice the market
fell 5% over every window in that period. A rule earns its place by beating the base rate, not by
being directionally right in a falling market. The `edge` figure is the difference, and a rule with
a *positive* edge is reported as counterproductive — it sold your winners.

Two honest limits, printed rather than buried: one portfolio over a few years is an anecdote, so the
verdict refuses to conclude below ten firings; and `weight_pct` is not reconstructed historically,
so rules using it are not backtestable.

No lookahead: indicators are computed over the full series then sliced by date, which is safe
because SMA, RSI and MACD are causal. Positions come from `replay_lots`, which replays transactions
to reconstruct holdings as they actually stood — the `lots` table holds *current* quantities and
cannot answer "what did I hold last March?".

### Rule-writing guidance

Prefer **few, simple, explainable** rules. A rule you can restate in one sentence is one you will
act on. A dozen tuned indicator rules will fire constantly, you will start ignoring the list, and
then the alerting is worth less than nothing.

---

## 9b. Rules engine — original sketch

Declarative YAML, evaluated every evening after prices refresh. Each rule produces alerts, never
orders.

```yaml
- name: stop_loss
  scope: {kind: EQUITY}
  when: "unrealised_pct <= -15"
  severity: high
  cooldown_days: 30
  message: "{symbol} down {unrealised_pct:.1f}% from cost — review thesis"

- name: trailing_stop
  scope: {kind: EQUITY, tags: [momentum]}
  when: "close <= peak_since_buy * 0.80"
  severity: high

- name: ltcg_threshold_approaching
  scope: {kind: [EQUITY, MF]}
  when: "330 <= days_held <= 365 and unrealised_pct > 0"
  severity: info
  message: "{name} turns long-term on {ltcg_date} — {days_to_ltcg}d away"

- name: allocation_drift
  scope: {level: portfolio}
  when: "abs(actual_pct - target_pct) >= 5"
  severity: medium

- name: concentration_risk
  scope: {kind: EQUITY}
  when: "position_pct_of_portfolio > 15"
  severity: medium

- name: valuation_stretched
  scope: {kind: EQUITY}
  when: "pe > pe_median_5y * 1.5 and unrealised_pct > 40"
  severity: medium

- name: fundamental_deterioration
  scope: {kind: EQUITY}
  when: "debt_to_equity_trend_4q == 'RISING' and roce_trend_4q == 'FALLING'"
  severity: high

- name: mf_persistent_underperformance
  scope: {kind: MF, asset_class: EQUITY}
  when: "rolling_3y_alpha < 0 and quarters_of_negative_alpha >= 6"
  severity: medium
  message: "{name}: 6+ quarters trailing {benchmark}. Switch candidate."

- name: fund_overlap
  scope: {kind: MF}
  when: "max_pairwise_overlap > 60"
  severity: info
```

**Engine mechanics**

- Each rule is evaluated against a **context dict** per instrument — every metric from §5 plus
  position facts (`days_held`, `unrealised_pct`, `position_pct_of_portfolio`, `peak_since_buy`).
- `when` is a restricted expression evaluated over that dict — an AST-walking evaluator with a
  whitelist of node types, not `eval()`.
- `cooldown_days` stops a rule re-firing daily while a condition persists.
- Every alert stores a **JSON snapshot of the context that triggered it**, so three months later you
  can see exactly why it fired.
- Alerts have a lifecycle: `NEW → ACKED → ACTED | MUTED`. Acting on an alert and recording the
  outcome is what eventually lets you ask "are my sell rules any good?"

**Backtest mode.** Replay any rule over history to see when it would have fired and what happened
next. This is the difference between rules you trust and rules you invented on a Tuesday. Worth
building before you rely on any rule with real money.

---

## 10. Yearly planner and SIP cashflow  ✅ partly built

Step-up SIP is modelled as a first-class thing, not a footnote: ₹17,000/month rising 10% a year
contributes **₹32.5 lakh** over ten years against **₹20.4 lakh** flat, and you cannot plan a year
without knowing what the instalment becomes. The increase lands on each **anniversary of the SIP
start date** — how AMCs actually implement it — not on 1 April, so a mid-year start means a mid-FY
step.

Two distinctions the module is careful about:

- **Behind plan vs. not yet due.** Early in a year most of the shortfall is simply instalments that
  have not come round yet. `behind_by` reports only what was due and unpaid.
- **SIP execution vs. any purchase.** Only `SIP`-kind transactions count as instalments. Folding in
  every `BUY` would let a one-off lump sum masquerade as instalments you never paid.

`deployable()` adds remaining SIP to what the sell planner frees, because they are one pool of
money. When remaining SIP already exceeds what a sale would free, it says so — redirecting SIP costs
nothing, while selling costs charges and a holding-period reset.

Projections report **contribution only**, with no assumed return: a projection that compounds an
invented growth rate tells you more about the assumption than about the plan.

Still to build:

- **Yearly targets** — investable surplus, target allocation by asset class, per-goal earmarking.
- **Progress** — actual vs. plan, by month, cumulative.
- **Rebalancing** — given drift, compute the minimum set of trades to return to target, preferring
  (a) redirecting *new* money over selling, and (b) selling long-term lots over short-term ones.
  Shows the tax cost of each proposed trade before you act.

---

## 11. Tax

`tax/rates.yaml`, versioned by financial year. Current assumptions for FY 2025-26 on listed equity
and equity-oriented MFs:

- Holding period for long-term: **12 months**
- STCG: **20%**
- LTCG: **12.5%** above a **₹1.25 lakh** annual exemption
- Debt MFs purchased on/after 2023-04-01: taxed at slab, no indexation
- s.112A grandfathering for equity acquired before 2018-01-31:
  `cost = max(actual_cost, min(FMV_2018_01_31, sale_price))`

**Verify these against the current Finance Act before relying on them.** Rates moved in July 2024
and can move at any Budget. The design isolates them in one YAML file precisely so this is a
one-line correction, and the dashboard shows which FY's rates it used for every number.

Outputs: realised gains by term for the FY, tax liability estimate, unrealised gains split by
short/long, tax-loss-harvesting candidates, and a "days until long-term" list.

---

## 12. Dashboard

Streamlit, one command, opens locally.

| Page | Contents |
|---|---|
| **Overview** | Net worth, allocation donut vs. target, XIRR, today's movers, open alerts |
| **Alerts** | Ranked by severity, with the triggering context, ack/act/mute |
| **Planner** | Year targets, SIP tracker, actual vs. plan, rebalance proposals |
| **Equities** | Holdings table; per-stock drill-down with price chart + indicators + fundamentals trend |
| **Funds** | Holdings, XIRR per fund, rolling returns, overlap matrix, alpha vs. benchmark |
| **Tax** | FY realised gains, liability estimate, harvesting candidates, LTCG countdown |
| **Transactions** | Full ledger, import status, data-staleness warnings |

### Scheduling

`daily_job.py` runs after market close: refresh prices → rebuild lots → recompute metrics →
evaluate rules → notify if anything fired. Local cron, or a GitHub Actions cron committing an
encrypted DB if you want it running without your laptop on. Notification via email (SMTP) or
ntfy.sh — keyless, no app to install.

---

## 13. Phasing

| Phase | Scope | Outcome |
|---|---|---|
| **1** | Schema, AMFI + yfinance ingest, CSV/manual transactions, lots, Overview + Transactions pages | You can see your real portfolio and correct XIRR |
| **2** | Rules engine, alerts, daily job, notifications, technicals | It tells you when something needs attention |
| **3** | Fundamentals ingest, MF rolling/alpha/overlap, tax page, planner, rebalancing | Full analysis depth |
| **4** | CAS PDF parser, backtest mode, optional broker API for live quotes | Convenience and confidence |

Phases 1–3 need **no API key and no paid service**.

---

## 14. Risks and open questions

**Risks**

- *yfinance is unofficial* and breaks occasionally. Mitigation: pin the version, cache everything, keep
  the ingest interface swappable so a broker API can slot in behind it later.
- *Indian fundamentals data is the genuine weak spot.* No good free structured source. XBRL parsing is
  real work; manual quarterly entry is the honest fallback.
- *CAS PDF formats change* between CAMS and KFintech and over time. Parser will need occasional repair.
  Manual entry is always available as a backstop.
- *Rules that fit the past.* Backtest mode makes this visible rather than solving it. Prefer few,
  simple, explainable rules over many tuned ones.
- *Tax rules change.* Isolated in one file, stamped on every output.

**Open questions for you**

1. Roughly how many holdings — 10, 30, 100? Under ~50 the whole thing stays trivially fast and some
   optimisation work disappears.
2. Do you want goal-based earmarking (retirement / house / emergency), or one undifferentiated pool?
3. Are there existing transaction records to import — broker tradebook CSVs, a CAS PDF, a spreadsheet —
   or does the ledger start from scratch?
4. Notifications: email, ntfy push, or just look at the dashboard?
