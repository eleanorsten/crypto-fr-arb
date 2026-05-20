"""
config.py — Strategy parameters
================================
All adjustable parameters for the funding rate arbitrage strategy live here.
Do not hard-code parameters in other modules. Change values here only.

Two configurations are provided. SHARPE_OPTIMAL is the recommended production
config per the May 2026 Investment Committee Memo. CONSERVATIVE is provided
for testing and risk-averse paper trading; it is not recommended for capital
deployment.
"""

from dataclasses import dataclass, field
from typing import List, Dict


@dataclass
class StrategyConfig:
    """A single strategy parameter set."""
    name: str
    description: str

    # Entry / exit signal thresholds (in annualized funding APR, %)
    entry_threshold_apr: float
    exit_threshold_apr: float

    # Holding-period bounds (in 8-hour funding periods; 3 per day)
    min_hold_periods: int           # hold at least this long before considering exit
    max_hold_periods: int = 270     # ~90 days; force-exit safety valve

    # Signal-smoothing & confirmation (anti-noise)
    smooth_window: int = 3          # EMA span for smoothed funding signal
    confirmation_periods: int = 1   # require N consecutive smoothed reads above threshold

    # Risk controls
    basis_stop_pct: float = 1.0     # exit on |basis| > X% of spot price
    allow_reverse_trade: bool = True  # enable short-spot / long-perp in negative regimes

    # Capital & sizing
    notional_per_trade: float = 10_000.0
    max_concurrent_positions: int = 5

    # Execution costs (Binance retail tier; revise if using a different venue/tier)
    spot_taker_fee: float = 0.0010
    perp_taker_fee: float = 0.0005
    slippage_bps: float = 3.0

    # Universe
    assets: List[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL", "BNB", "XRP"])


# ---------------------------------------------------------------------------
# RECOMMENDED: Sharpe-optimal configuration (per Investment Memo § 9)
# ---------------------------------------------------------------------------
SHARPE_OPTIMAL = StrategyConfig(
    name="Sharpe-Optimal",
    description=(
        "Recommended production configuration. Targets maximum risk-adjusted "
        "return at moderate selectivity. Backtest: 17.7% annualized, Sharpe 4.4, "
        "win rate 69%, max DD -0.93% (3-year synthetic-data backtest)."
    ),
    entry_threshold_apr=40.0,
    exit_threshold_apr=5.0,
    min_hold_periods=21,        # 7 days
    confirmation_periods=1,
)


# ---------------------------------------------------------------------------
# CONSERVATIVE: 95% win-rate target — NOT recommended (see Investment Memo § 7)
# ---------------------------------------------------------------------------
CONSERVATIVE = StrategyConfig(
    name="Conservative",
    description=(
        "Hits the 95% win-rate target but at the cost of meaningful return. "
        "Backtest: 6.5% annualized, Sharpe 3.1, win rate 95.4%, max DD -0.09%. "
        "Per Investment Memo § 7, this configuration is NOT recommended for "
        "capital deployment — the return does not compensate for counterparty risk."
    ),
    entry_threshold_apr=150.0,
    exit_threshold_apr=15.0,
    min_hold_periods=45,        # 15 days
    confirmation_periods=2,
)


# ---------------------------------------------------------------------------
# DEPLOYMENT PARAMETERS — phased capital rollout per Investment Memo § 9
# ---------------------------------------------------------------------------
DEPLOYMENT = {
    "phase_1_paper": {
        "weeks": 8,
        "notional_total": 0,        # paper only
        "max_per_asset": 0,
    },
    "phase_2_limited": {
        "weeks": 8,
        "notional_total": 10_000,
        "max_per_asset": 3_000,
        "venues": {"binance": 0.60, "bybit": 0.40},
    },
    "phase_3_scale": {
        "weeks": None,              # ongoing
        "notional_total": 50_000,
        "max_per_asset": 12_500,
        "venues": {"binance": 0.50, "bybit": 0.30, "okx": 0.20},
    },
    "hard_cap": 50_000,             # do not exceed without infrastructure upgrade
}


# ---------------------------------------------------------------------------
# Alerting thresholds — used by risk_monitor.py
# ---------------------------------------------------------------------------
ALERT_THRESHOLDS = {
    "basis_warning_pct": 0.50,      # log a warning
    "basis_critical_pct": 1.00,     # flatten the position
    "funding_spike_apr": 300.0,     # unusual funding read; investigate
    "drawdown_warning_pct": 1.50,   # email the operator
    "drawdown_halt_pct": 3.00,      # halt all new entries
}


# Active config — change this single line to switch the deployed config.
ACTIVE_CONFIG: StrategyConfig = SHARPE_OPTIMAL
