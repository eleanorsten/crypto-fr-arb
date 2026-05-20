"""
performance_report.py — Strategy Performance Attribution
==========================================================

Reads backtest results (or in production, the live trade log) and produces a
human-readable performance report. Use this for:
  - Weekly review with the OQG quant desk
  - Monthly reporting to the OQG general body
  - Quarterly review with faculty advisors

In production, point this at the live trade journal CSV instead of the
backtest output.

USAGE:
    python performance_report.py                       # full-period report
    python performance_report.py --since 2026-01-01    # year-to-date
    python performance_report.py --window 30           # last 30 days
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

import backtester as bt
from config import ACTIVE_CONFIG


# ---------------------------------------------------------------------------
# Run the strategy (or load live trade log) to produce a trades DataFrame
# ---------------------------------------------------------------------------
def get_trades(csv_path: str = "funding_data.csv") -> pd.DataFrame:
    """Run the backtest and return the trades DataFrame.

    PRODUCTION: replace this with a read of the live trade journal.
    """
    df = pd.read_csv(csv_path, parse_dates=["timestamp"])
    config = bt.BacktestConfig(
        entry_threshold_apr=ACTIVE_CONFIG.entry_threshold_apr,
        exit_threshold_apr=ACTIVE_CONFIG.exit_threshold_apr,
        min_hold_periods=ACTIVE_CONFIG.min_hold_periods,
        max_hold_periods=ACTIVE_CONFIG.max_hold_periods,
        smooth_window=ACTIVE_CONFIG.smooth_window,
        confirmation_periods=ACTIVE_CONFIG.confirmation_periods,
        basis_stop_pct=ACTIVE_CONFIG.basis_stop_pct,
        notional_per_trade=ACTIVE_CONFIG.notional_per_trade,
        spot_taker_fee=ACTIVE_CONFIG.spot_taker_fee,
        perp_taker_fee=ACTIVE_CONFIG.perp_taker_fee,
        slippage_bps=ACTIVE_CONFIG.slippage_bps,
        allow_negative_funding=ACTIVE_CONFIG.allow_reverse_trade,
    )
    backtester = bt.FundingRateBacktest(df, config)
    return backtester.run(), config


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------
def filter_trades(trades: pd.DataFrame,
                  since: Optional[pd.Timestamp] = None,
                  window_days: Optional[int] = None) -> pd.DataFrame:
    trades = trades.copy()
    trades["entry_time"] = pd.to_datetime(trades["entry_time"])
    trades["exit_time"] = pd.to_datetime(trades["exit_time"])
    if window_days is not None:
        latest = trades["exit_time"].max()
        cutoff = latest - pd.Timedelta(days=window_days)
        trades = trades[trades["exit_time"] >= cutoff]
    if since is not None:
        trades = trades[trades["exit_time"] >= since]
    return trades.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------
def render_performance(trades: pd.DataFrame, config, period_label: str) -> str:
    if len(trades) == 0:
        return (
            "==========================================================\n"
            f"  No trades closed in period: {period_label}\n"
            "=========================================================="
        )

    lines = []
    bar = "=" * 78
    sep = "-" * 78

    lines.append(bar)
    lines.append(f"  FRA-001  ·  PERFORMANCE REPORT  ·  {period_label}")
    lines.append(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  Config:    {ACTIVE_CONFIG.name}")
    lines.append(bar)
    lines.append("")

    # -------- Top line --------
    n = len(trades)
    capital = config.notional_per_trade * 5      # 5 concurrent positions
    total_pnl = trades["net_pnl"].sum()
    wins = trades[trades["win"]]
    losses = trades[~trades["win"]]
    win_rate = len(wins) / n * 100

    period_start = trades["entry_time"].min()
    period_end = trades["exit_time"].max()
    days_elapsed = (period_end - period_start).days or 1
    annualization = 365 / days_elapsed

    period_return_pct = total_pnl / capital * 100
    annualized_return = period_return_pct * annualization

    lines.append("  HEADLINE")
    lines.append(sep)
    lines.append(f"    Period:                 {period_start.date()} to {period_end.date()}  ({days_elapsed} days)")
    lines.append(f"    Closed trades:          {n}")
    lines.append(f"    Win rate:               {win_rate:.2f}%   ({len(wins)} wins, {len(losses)} losses)")
    lines.append(f"    Net P&L:                ${total_pnl:+,.2f}")
    lines.append(f"    Period return:          {period_return_pct:+.2f}%  on ${capital:,.0f} working capital")
    lines.append(f"    Annualized:             {annualized_return:+.2f}%")
    lines.append("")

    # -------- Risk metrics --------
    trades["exit_date"] = trades["exit_time"].dt.date
    daily_pnl = trades.groupby("exit_date")["net_pnl"].sum()
    if len(daily_pnl) > 1:
        full_range = pd.date_range(daily_pnl.index.min(), daily_pnl.index.max())
        daily_pnl = daily_pnl.reindex(full_range, fill_value=0)
        daily_ret = daily_pnl / capital
        sharpe = (daily_ret.mean() / daily_ret.std()) * np.sqrt(365) if daily_ret.std() > 0 else 0
        cum = daily_pnl.cumsum()
        peak = cum.cummax()
        dd = (cum - peak) / capital * 100
        max_dd = dd.min()
    else:
        sharpe = float("nan")
        max_dd = 0.0

    gp = wins["net_pnl"].sum()
    gl = abs(losses["net_pnl"].sum())
    pf = gp / gl if gl > 0 else float("inf")

    lines.append("  RISK & QUALITY")
    lines.append(sep)
    lines.append(f"    Sharpe ratio (daily-resample):  {sharpe:.2f}")
    lines.append(f"    Max drawdown:                   {max_dd:.2f}%")
    lines.append(f"    Profit factor:                  {pf:.2f}  (${gp:,.0f} won / ${gl:,.0f} lost)")
    lines.append(f"    Avg winner:                     ${wins['net_pnl'].mean():+,.2f}" if len(wins) else "    No wins")
    lines.append(f"    Avg loser:                      ${losses['net_pnl'].mean():+,.2f}" if len(losses) else "    No losses")
    lines.append(f"    Largest winner:                 ${wins['net_pnl'].max():+,.2f}" if len(wins) else "")
    lines.append(f"    Largest loser:                  ${losses['net_pnl'].min():+,.2f}" if len(losses) else "")
    lines.append(f"    Avg hold (days):                {trades['hold_days'].mean():.1f}")
    lines.append("")

    # -------- Per-asset attribution --------
    sym = trades.groupby("symbol").agg(
        n=("net_pnl", "count"),
        wr=("win", lambda x: x.mean() * 100),
        total_pnl=("net_pnl", "sum"),
        avg_pnl=("net_pnl", "mean"),
        hold=("hold_days", "mean"),
    ).round(2)
    sym["contrib_pct"] = (sym["total_pnl"] / total_pnl * 100).round(1) if total_pnl != 0 else 0
    sym = sym.sort_values("total_pnl", ascending=False)

    lines.append("  PER-ASSET ATTRIBUTION")
    lines.append(sep)
    lines.append(f"    {'Asset':<6} {'N':>5} {'WinRate':>9} {'TotalPnL':>14} {'AvgPnL':>10} {'AvgHold':>9} {'Contrib':>9}")
    for s, r in sym.iterrows():
        lines.append(f"    {s:<6} {int(r['n']):>5} {r['wr']:>8.1f}%  ${r['total_pnl']:>+11,.2f} ${r['avg_pnl']:>+8.2f} "
                     f"{r['hold']:>7.1f}d {r['contrib_pct']:>+7.1f}%")
    lines.append("")

    # -------- Direction split --------
    dirsplit = trades.groupby("direction").agg(
        n=("net_pnl", "count"),
        wr=("win", lambda x: x.mean() * 100),
        total=("net_pnl", "sum"),
    ).round(2)
    lines.append("  DIRECTION SPLIT")
    lines.append(sep)
    for d, r in dirsplit.iterrows():
        nicename = "Long-Basis (long spot / short perp)" if d == "long_basis" else "Short-Basis (short spot / long perp)"
        lines.append(f"    {nicename:<40} {int(r['n']):>4} trades  WR {r['wr']:.1f}%   ${r['total']:+,.2f}")
    lines.append("")

    # -------- Exit reason breakdown --------
    if "exit_reason" in trades.columns:
        exits = trades["exit_reason"].value_counts()
        lines.append("  EXIT REASONS")
        lines.append(sep)
        for reason, count in exits.items():
            reason_nice = {
                "funding_signal_lost": "Funding signal lost (normal exit)",
                "max_hold": "Max-hold safety valve",
                "basis_stop": "Basis-stop risk control",
                "end_of_data": "End of backtest period",
            }.get(reason, reason)
            pct = count / n * 100
            lines.append(f"    {reason_nice:<50} {count:>4} trades  ({pct:5.1f}%)")
        lines.append("")

    # -------- Verdict --------
    lines.append("  VERDICT")
    lines.append(sep)
    if annualized_return > 12 and sharpe > 3 and max_dd > -2:
        verdict = "ON TARGET   — Strategy performing within backtest expectations."
    elif annualized_return > 6 and sharpe > 1.5:
        verdict = "ACCEPTABLE  — Below backtest but still positive. Watch for drift."
    elif annualized_return > 0:
        verdict = "UNDERPERFORMING  — Diagnose; consider parameter review."
    else:
        verdict = "BREACH      — Halt new entries. Convene the desk."
    lines.append(f"    {verdict}")
    lines.append("")

    # -------- Footer --------
    lines.append(bar)
    lines.append("  END OF REPORT")
    lines.append(bar)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Performance report for FRA-001")
    parser.add_argument("--since", type=str, default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--window", type=int, default=None, help="Last N days")
    parser.add_argument("--save", action="store_true", help="Write report to example_outputs/")
    parser.add_argument("--data", type=str, default="funding_data.csv")
    args = parser.parse_args()

    print("Loading data and running strategy ...", flush=True)
    trades, config = get_trades(args.data)

    since = pd.to_datetime(args.since) if args.since else None
    filtered = filter_trades(trades, since=since, window_days=args.window)

    if args.window:
        period_label = f"LAST {args.window} DAYS"
    elif args.since:
        period_label = f"SINCE {args.since}"
    else:
        period_label = "FULL PERIOD"

    report = render_performance(filtered, config, period_label)
    print(report)

    if args.save:
        os.makedirs("example_outputs", exist_ok=True)
        tag = period_label.lower().replace(" ", "_")
        out_path = f"example_outputs/performance_{tag}.txt"
        with open(out_path, "w") as f:
            f.write(report)
        print(f"\n(report saved to {out_path})")


if __name__ == "__main__":
    main()
