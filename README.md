# FRA-001 — Funding Rate Arbitrage System
## Operations Manual

**System:** Delta-neutral perpetual futures basis trade
**Status:** Research-validated · Paper-trading required before deployment

---

## What this is

A crypto funding rate arbitrage system that collects the funding payments paid between perpetual futures longs and shorts every 8 hours. The strategy is delta-neutral — long spot, short perpetual (or the reverse in negative-funding regimes) — and earns income from the funding mechanism rather than directional price moves.

**For the full client-facing rationale and the deployment recommendation, see `INVESTMENT_MEMO.md`.** This README is for the desk that will operate the system.

---

## File layout

```
funding_arb_bot/
├── INVESTMENT_MEMO.md       Client-facing committee memo (decision document)
├── README.md                This file (team operations manual)
├── config.py                All strategy parameters in one place
├── data_generator.py        Synthetic funding-rate generator (for testing)
├── backtester.py            Validated backtest engine
├── signal_generator.py      Daily "what to trade today" report
├── performance_report.py    P&L attribution report (use weekly + monthly)
├── risk_monitor.py          Anomaly scanner (run every 8h funding cycle)
├── analysis.py              Side-by-side comparison of strategy configs
├── funding_data.csv         3-year synthetic dataset (16,425 obs)
└── example_outputs/         Sample reports — what to expect
```

---

## Quick start

```bash
# 1. Install dependencies (numpy, pandas)
pip install numpy pandas

# 2. Generate synthetic data (if not present)
python data_generator.py

# 3. Get today's signal recommendations
python signal_generator.py --save

# 4. Run risk monitor (do this every 8h cycle in production)
python risk_monitor.py --save

# 5. Weekly performance review
python performance_report.py --window 7 --save

# 6. Monthly performance review
python performance_report.py --window 30 --save
```

All scripts accept `--help` for full options.

---

## Daily operations

The desk should establish a simple daily cadence:

**Morning (before 08:00 UTC funding cycle):**
1. Run `python risk_monitor.py` — confirm no CRITICAL alerts before any new entries.
2. Run `python signal_generator.py` — review each ENTER signal.
3. For each ENTER signal: verify the funding rate against the live exchange (Binance/Bybit/OKX) — do not trust stale data.
4. Execute the trades through your venue. Both legs simultaneously when possible.
5. Log the trade in your journal with: timestamp, symbol, direction, entry prices, signal report ID.

**Midday & Evening (16:00 UTC, 00:00 UTC funding cycles):**
1. Run `python risk_monitor.py` — scan for emerging issues.
2. If any position has hit `max_hold` or the funding signal has dropped, plan exits for next cycle.

**Weekly:**
1. Run `python performance_report.py --window 7 --save` — review with the desk.
2. Cross-check live P&L against the system's calculated P&L. Differences must be reconciled.

**Monthly:**
1. Run `python performance_report.py --window 30 --save`.
2. Present to OQG general body.

---

## Switching between configurations

`config.py` defines two configurations. The active one is set via `ACTIVE_CONFIG` at the bottom of the file.

**Recommended:** `SHARPE_OPTIMAL` (default). Targets ~17% annualized at Sharpe 4.4.

**Not recommended:** `CONSERVATIVE`. Hits the 95% win-rate target but only earns 6.5% annualized — see `INVESTMENT_MEMO.md` § 7 for why this is not worth deploying.

To switch:
```python
# In config.py, last line:
ACTIVE_CONFIG: StrategyConfig = SHARPE_OPTIMAL  # or CONSERVATIVE
```

All scripts read from `ACTIVE_CONFIG` — no other edits needed.

---

## Tuning parameters

Parameters live in `config.py`. The validated values were found via the optimization sweep documented in the memo. Do not change without re-running the backtester and confirming Sharpe, win-rate, and drawdown stay within acceptable ranges.

If you change `entry_threshold_apr`, `exit_threshold_apr`, `min_hold_periods`, or `confirmation_periods`, **always** re-run:

```bash
python performance_report.py --save
```

Acceptance criteria for a new parameter set:
- Sharpe ≥ 2.5
- Win rate ≥ 60%
- Max drawdown ≥ -2.0%
- Profit factor ≥ 4

Anything failing these should not be deployed.

---

## Wiring up live data (the bit that's not done yet)

The current codebase reads from a CSV (`funding_data.csv`) of synthetic data. **Before any capital deployment**, the following must be replaced with live exchange API calls:

| Function | File | What it does | Production replacement |
|---|---|---|---|
| `load_latest_data()` | `signal_generator.py` | Reads funding rates | Binance `/fapi/v1/fundingRate` + Bybit `/v5/market/funding/history` + OKX `/api/v5/public/funding-rate` |
| `get_trades()` | `performance_report.py` | Reads trade history | Read from the team's live trade journal CSV or database |
| `load_latest_data()` | `risk_monitor.py` | Reads basis & funding | Same as signal_generator |

The function signatures and return formats are documented in each file. A junior dev can wire these up in 1-2 days using the python `requests` or `httpx` library plus the exchange Python SDKs (`python-binance`, `pybit`, `okx`).

**Additional production needs** beyond just data wiring (per the Investment Memo § 6):
- Order management system with idempotency, retries, and exchange-side reconciliation
- Real-time basis monitor (not 8h cadence — sub-second)
- Circuit breaker that can flatten all positions on a single command
- Alerting infrastructure (PagerDuty / Slack / SMS)

Budget 4–8 weeks of focused engineering for production readiness.

---

## Example output

See `example_outputs/` for samples of what each report looks like. The signal generator output is designed so anyone on the desk can read it and know exactly what to do — no quant background required.

---

## Common pitfalls (from the desk)

**1. Trusting the smoothed signal blindly.** The EMA-smoothed funding rate lags the raw rate by ~2 periods. In a fast unwind (rate flipping from +60% to -60% in a single cycle), the smoothed signal will still say "enter long-basis" while the raw rate says "danger." Always cross-check raw funding before executing.

**2. Forgetting to reconcile.** Exchange-reported positions can differ from the system's internal book by small amounts (rounding, partial fills). Reconcile every funding cycle. Differences over 1% of notional must be investigated before next entry.

**3. Running both directions on the same asset simultaneously.** Don't. Pick one direction per asset per cycle. Concurrent long-basis and short-basis on BTC is not a hedge — it's just paying fees twice.

**4. Holding through a basis blowout.** The basis stop at 1% is there for a reason. Disabling it because "it'll come back" is how funds blow up. Two of the three biggest crypto fund collapses (Three Arrows, FTX-adjacent prop) had a "we'll wait it out" mindset.

**5. Sizing up too fast.** The recommendation is to start at $10K paper, scale to $25K, then $50K — over months, not weeks. If you find yourself wanting to scale faster because "it's working," that's the moment to slow down.

---

## Escalation

| Event | Who decides | Timeline |
|---|---|---|
| New parameter set | Quant desk + president | 1 week review |
| Capital scale-up | President + faculty advisor | 1 month observation required |
| Adding a new venue | Quant desk + ops | 2 week paper trade required |
| Adding a new asset | Quant desk | Backtest + 1 week paper |
| Removing an asset | Anyone on the desk | Same cycle |
| Halt due to alert | Anyone on the desk | Immediate |
| Resume after halt | President | Post-mortem required first |

---

## Final note

This system is a piece of quantitative infrastructure. Treat it like one. The math is solid, the code is clean, but the operational discipline is what determines whether you make money or lose it. Read the memo. Follow the playbook. Don't improvise on live capital.

When in doubt, halt and ask.

— *Quant Research*
*Funding.Arb · v1.0 · May 2026*
