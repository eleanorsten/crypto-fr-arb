"""
Funding Rate Arbitrage Backtester v2 - Smoothed Signal Strategy
================================================================
Improvements over v1:
1. Smoothed funding rate signal (EMA) to avoid noise-driven entries/exits
2. Minimum hold period to overcome transaction costs
3. Hysteresis: separate enter/exit thresholds with confirmation
4. Realistic capital efficiency: position sizing and concurrent trades
5. Per-asset capital allocation
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import List, Optional, Dict

@dataclass
class Trade:
    symbol: str
    entry_time: pd.Timestamp
    exit_time: Optional[pd.Timestamp] = None
    direction: str = "long_basis"
    entry_spot: float = 0
    entry_perp: float = 0
    exit_spot: float = 0
    exit_perp: float = 0
    entry_funding_apr: float = 0
    funding_collected: float = 0
    fee_cost: float = 0
    spot_pnl: float = 0
    perp_pnl: float = 0
    net_pnl: float = 0
    n_funding_periods: int = 0
    exit_reason: str = ""


@dataclass
class BacktestConfig:
    entry_threshold_apr: float = 20.0
    exit_threshold_apr: float = 0.0
    min_hold_periods: int = 9        # ~3 days minimum
    max_hold_periods: int = 180      # ~60 days maximum
    smooth_window: int = 3           # EMA window for funding signal
    basis_stop_pct: float = 1.0
    notional_per_trade: float = 10000.0
    spot_taker_fee: float = 0.0010
    perp_taker_fee: float = 0.0005
    slippage_bps: float = 3.0
    allow_negative_funding: bool = True
    confirmation_periods: int = 2     # Require N consecutive periods above threshold
    

def annualize_funding(rate: float) -> float:
    return rate * 3 * 365 * 100


class FundingRateBacktest:
    def __init__(self, data: pd.DataFrame, config: BacktestConfig):
        self.data = data.sort_values(["symbol", "timestamp"]).reset_index(drop=True)
        self.config = config
        self.trades: List[Trade] = []
        
        # Pre-compute smoothed funding signal per asset
        smoothed = []
        for sym in self.data.symbol.unique():
            mask = self.data.symbol == sym
            sym_data = self.data[mask].copy()
            sym_data["funding_smooth"] = sym_data["funding_rate"].ewm(
                span=config.smooth_window, adjust=False
            ).mean()
            smoothed.append(sym_data)
        self.data = pd.concat(smoothed, ignore_index=True)
        self.data["funding_apr"] = annualize_funding(self.data["funding_rate"])
        self.data["funding_apr_smooth"] = annualize_funding(self.data["funding_smooth"])
    
    def round_trip_cost(self) -> float:
        spot_leg = self.config.spot_taker_fee + self.config.slippage_bps / 10000
        perp_leg = self.config.perp_taker_fee + self.config.slippage_bps / 10000
        return 2 * (spot_leg + perp_leg) * self.config.notional_per_trade
    
    def open_trade(self, row, direction: str) -> Trade:
        return Trade(
            symbol=row.symbol,
            entry_time=row.timestamp,
            direction=direction,
            entry_spot=row.spot_price,
            entry_perp=row.perp_price,
            entry_funding_apr=row.funding_apr,
        )
    
    def close_trade(self, trade: Trade, row, reason: str) -> Trade:
        n = self.config.notional_per_trade
        trade.exit_time = row.timestamp
        trade.exit_spot = row.spot_price
        trade.exit_perp = row.perp_price
        trade.exit_reason = reason
        
        spot_units = n / trade.entry_spot
        perp_units = n / trade.entry_perp
        
        if trade.direction == "long_basis":
            trade.spot_pnl = spot_units * (trade.exit_spot - trade.entry_spot)
            trade.perp_pnl = perp_units * (trade.entry_perp - trade.exit_perp)
        else:
            trade.spot_pnl = spot_units * (trade.entry_spot - trade.exit_spot)
            trade.perp_pnl = perp_units * (trade.exit_perp - trade.entry_perp)
        
        trade.fee_cost = self.round_trip_cost()
        trade.net_pnl = trade.spot_pnl + trade.perp_pnl + trade.funding_collected - trade.fee_cost
        return trade
    
    def run(self) -> pd.DataFrame:
        cfg = self.config
        
        for symbol in self.data.symbol.unique():
            sym_data = self.data[self.data.symbol == symbol].reset_index(drop=True)
            
            # Track confirmation counters
            long_signal_count = 0
            short_signal_count = 0
            
            open_trade: Optional[Trade] = None
            hold_periods = 0
            
            for i, row in sym_data.iterrows():
                # Update confirmation counters using smoothed signal
                if row.funding_apr_smooth > cfg.entry_threshold_apr:
                    long_signal_count += 1
                else:
                    long_signal_count = 0
                
                if row.funding_apr_smooth < -cfg.entry_threshold_apr:
                    short_signal_count += 1
                else:
                    short_signal_count = 0
                
                # If position open, accumulate funding & check exit
                if open_trade is not None:
                    if open_trade.direction == "long_basis":
                        funding_pay = cfg.notional_per_trade * row.funding_rate
                    else:
                        funding_pay = cfg.notional_per_trade * (-row.funding_rate)
                    open_trade.funding_collected += funding_pay
                    open_trade.n_funding_periods += 1
                    hold_periods += 1
                    
                    # Exit logic
                    should_exit = False
                    exit_reason = ""
                    
                    if hold_periods >= cfg.min_hold_periods:
                        if open_trade.direction == "long_basis":
                            if row.funding_apr_smooth < cfg.exit_threshold_apr:
                                should_exit = True
                                exit_reason = "funding_signal_lost"
                        else:
                            if row.funding_apr_smooth > -cfg.exit_threshold_apr:
                                should_exit = True
                                exit_reason = "funding_signal_lost"
                    
                    if hold_periods >= cfg.max_hold_periods:
                        should_exit = True
                        exit_reason = "max_hold"
                    
                    if abs(row.basis_pct) > cfg.basis_stop_pct:
                        should_exit = True
                        exit_reason = "basis_stop"
                    
                    if should_exit:
                        open_trade = self.close_trade(open_trade, row, exit_reason)
                        self.trades.append(open_trade)
                        open_trade = None
                        hold_periods = 0
                        long_signal_count = 0
                        short_signal_count = 0
                
                # Entry logic: require confirmation
                if open_trade is None:
                    if long_signal_count >= cfg.confirmation_periods:
                        open_trade = self.open_trade(row, "long_basis")
                    elif cfg.allow_negative_funding and short_signal_count >= cfg.confirmation_periods:
                        open_trade = self.open_trade(row, "short_basis")
            
            # Force close at end
            if open_trade is not None:
                open_trade = self.close_trade(open_trade, sym_data.iloc[-1], "end_of_data")
                self.trades.append(open_trade)
        
        return self.summarize()
    
    def summarize(self) -> pd.DataFrame:
        records = []
        for t in self.trades:
            records.append({
                "symbol": t.symbol,
                "direction": t.direction,
                "entry_time": t.entry_time,
                "exit_time": t.exit_time,
                "entry_funding_apr": t.entry_funding_apr,
                "hold_periods": t.n_funding_periods,
                "hold_days": t.n_funding_periods / 3,
                "funding_collected": t.funding_collected,
                "spot_pnl": t.spot_pnl,
                "perp_pnl": t.perp_pnl,
                "basis_pnl": t.spot_pnl + t.perp_pnl,
                "fee_cost": t.fee_cost,
                "net_pnl": t.net_pnl,
                "return_pct": t.net_pnl / self.config.notional_per_trade * 100,
                "exit_reason": t.exit_reason,
                "win": t.net_pnl > 0,
            })
        return pd.DataFrame(records)


def compute_performance(trades_df: pd.DataFrame, config: BacktestConfig,
                        years: float = 3.0) -> dict:
    if len(trades_df) == 0:
        return {"n_trades": 0}
    
    trades_df = trades_df.copy()
    trades_df["exit_date"] = pd.to_datetime(trades_df["exit_time"]).dt.date
    
    total_pnl = trades_df["net_pnl"].sum()
    n_trades = len(trades_df)
    win_rate = trades_df["win"].mean() * 100
    wins = trades_df.loc[trades_df["win"], "net_pnl"]
    losses = trades_df.loc[~trades_df["win"], "net_pnl"]
    avg_win = wins.mean() if len(wins) > 0 else 0
    avg_loss = losses.mean() if len(losses) > 0 else 0
    
    # Realistic capital: ~5 concurrent positions across 5 assets
    capital = config.notional_per_trade * 5
    total_return_pct = total_pnl / capital * 100
    annualized_return = total_return_pct / years
    
    # Daily PnL for Sharpe
    daily_pnl = trades_df.groupby("exit_date")["net_pnl"].sum()
    # Fill missing days with zeros
    if len(daily_pnl) > 0:
        full_range = pd.date_range(daily_pnl.index.min(), daily_pnl.index.max())
        daily_pnl = daily_pnl.reindex(full_range, fill_value=0)
        daily_return = daily_pnl / capital
        sharpe = (daily_return.mean() / daily_return.std()) * np.sqrt(365) if daily_return.std() > 0 else 0
    else:
        sharpe = 0
    
    # Drawdown
    cum_pnl = daily_pnl.cumsum() if len(daily_pnl) else pd.Series([0])
    running_max = cum_pnl.cummax()
    drawdown = (cum_pnl - running_max) / capital * 100
    max_dd = drawdown.min() if len(drawdown) else 0
    
    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    # Avg per-trade return
    avg_return_pct = trades_df["return_pct"].mean()
    
    return {
        "n_trades": n_trades,
        "total_pnl": total_pnl,
        "total_return_pct": total_return_pct,
        "annualized_return_pct": annualized_return,
        "win_rate_pct": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_return_per_trade_pct": avg_return_pct,
        "profit_factor": profit_factor,
        "sharpe": sharpe,
        "max_drawdown_pct": max_dd,
        "avg_hold_days": trades_df["hold_days"].mean(),
    }


if __name__ == "__main__":
    df = pd.read_csv("/home/claude/arbitrage_research/funding_data.csv", parse_dates=["timestamp"])
    
    print("="*70)
    print("FUNDING RATE ARBITRAGE - SMOOTHED SIGNAL STRATEGY")
    print("="*70)
    
    config = BacktestConfig(
        entry_threshold_apr=20.0,
        exit_threshold_apr=0.0,
        min_hold_periods=9,
        max_hold_periods=180,
        smooth_window=3,
        confirmation_periods=2,
        basis_stop_pct=1.0,
    )
    
    print(f"\nConfig: entry={config.entry_threshold_apr}% APR, "
          f"exit={config.exit_threshold_apr}% APR, "
          f"min_hold={config.min_hold_periods}p ({config.min_hold_periods/3:.1f}d)")
    print(f"Round-trip cost: ${FundingRateBacktest(df, config).round_trip_cost():.2f} "
          f"({FundingRateBacktest(df, config).round_trip_cost()/config.notional_per_trade*100:.2f}% of notional)")
    
    backtester = FundingRateBacktest(df, config)
    trades_df = backtester.run()
    perf = compute_performance(trades_df, config, years=3.0)
    
    print("\n" + "="*70)
    print("PERFORMANCE")
    print("="*70)
    for k, v in perf.items():
        if isinstance(v, float):
            print(f"  {k:30s}: {v:>12.2f}")
        else:
            print(f"  {k:30s}: {v:>12}")
    
    print("\n=== Per-asset ===")
    print(trades_df.groupby("symbol").agg(
        n_trades=("net_pnl", "count"),
        win_rate=("win", lambda x: x.mean()*100),
        avg_pnl=("net_pnl", "mean"),
        total_pnl=("net_pnl", "sum"),
        avg_hold_days=("hold_days", "mean"),
    ).round(2))
    
    print("\n=== Exit reasons ===")
    print(trades_df["exit_reason"].value_counts())
    
    trades_df.to_csv("/home/claude/arbitrage_research/v2_trades.csv", index=False)
