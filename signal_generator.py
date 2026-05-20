"""
signal_generator.py — Daily Trade Signal Report
================================================

Run this every morning. It reads the most recent funding rate data, applies the
active strategy configuration, and produces a clean text report telling the
team exactly what to do.

In production, the `load_latest_data()` function should be replaced with calls
to live exchange APIs (Binance USDM funding endpoint, Bybit derivatives feed,
OKX funding stream). The current implementation reads from the synthetic CSV
used in backtesting.

USAGE:
    python signal_generator.py                  # standard daily report
    python signal_generator.py --as-of 2026-05-15  # historical lookup

OUTPUT:
    Prints report to stdout. Also writes to ./example_outputs/daily_signals_<date>.txt
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import numpy as np
import pandas as pd

from config import ACTIVE_CONFIG, ALERT_THRESHOLDS


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_latest_data(csv_path: str = "funding_data.csv",
                     as_of: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """
    Load the most recent funding rate observations.

    PRODUCTION: replace this with live API calls. The function should return
    a DataFrame with columns: [timestamp, symbol, funding_rate, spot_price,
    perp_price, basis_pct].
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(
            f"Data file {csv_path} not found. Run data_generator.py first, "
            f"or wire this function up to live exchange APIs."
        )
    df = pd.read_csv(csv_path, parse_dates=["timestamp"])
    if as_of is not None:
        df = df[df.timestamp <= as_of]
    return df


# ---------------------------------------------------------------------------
# Signal logic — mirrors backtester.py for consistency
# ---------------------------------------------------------------------------
@dataclass
class AssetSignal:
    symbol: str
    timestamp: pd.Timestamp
    spot_price: float
    perp_price: float
    basis_pct: float
    funding_rate_raw: float           # latest 8h rate
    funding_apr_raw: float            # annualized
    funding_apr_smooth: float         # EMA-smoothed annualized
    regime: str                       # detected regime (bull / chop / bear)

    # Action recommendation
    action: str                       # ENTER_LONG_BASIS / ENTER_SHORT_BASIS / HOLD / EXIT / FLAT
    rationale: str
    confidence: str                   # HIGH / MEDIUM / LOW
    expected_hold_days: Optional[float] = None
    expected_funding_yield_pct: Optional[float] = None
    risk_flags: List[str] = None

    def __post_init__(self):
        if self.risk_flags is None:
            self.risk_flags = []


def annualize(rate: float) -> float:
    return rate * 3 * 365 * 100


def detect_regime(funding_history: pd.Series) -> str:
    """Simple regime classification based on recent funding trajectory."""
    recent = funding_history.tail(30)
    if len(recent) == 0:
        return "unknown"
    mean_apr = annualize(recent.mean())
    if mean_apr > 25:
        return "bull"
    if mean_apr < -15:
        return "bear"
    return "chop"


def generate_asset_signal(df_asset: pd.DataFrame, config) -> AssetSignal:
    """Generate a single asset's recommended action."""
    # Smooth the funding signal (EMA)
    df_asset = df_asset.sort_values("timestamp").copy()
    df_asset["funding_smooth"] = df_asset["funding_rate"].ewm(
        span=config.smooth_window, adjust=False
    ).mean()

    latest = df_asset.iloc[-1]
    funding_apr_raw = annualize(latest["funding_rate"])
    funding_apr_smooth = annualize(latest["funding_smooth"])
    regime = detect_regime(df_asset["funding_rate"])

    # Confirmation: require N consecutive periods past threshold
    confirm_window = df_asset.tail(config.confirmation_periods)["funding_smooth"].apply(annualize)
    long_signal = (confirm_window > config.entry_threshold_apr).all()
    short_signal = (
        config.allow_reverse_trade and (confirm_window < -config.entry_threshold_apr).all()
    )

    risk_flags = []
    if abs(latest["basis_pct"]) > ALERT_THRESHOLDS["basis_warning_pct"]:
        risk_flags.append(
            f"BASIS_ELEVATED ({latest['basis_pct']:.3f}% > "
            f"{ALERT_THRESHOLDS['basis_warning_pct']}% warning level)"
        )
    if abs(funding_apr_raw) > ALERT_THRESHOLDS["funding_spike_apr"]:
        risk_flags.append(
            f"FUNDING_SPIKE ({funding_apr_raw:.0f}% APR — possible unwind imminent)"
        )

    # Decide action
    if long_signal and not risk_flags:
        action = "ENTER_LONG_BASIS"
        rationale = (
            f"Smoothed funding {funding_apr_smooth:.1f}% APR > entry threshold "
            f"{config.entry_threshold_apr:.0f}% with {config.confirmation_periods}-period "
            f"confirmation. Regime: {regime.upper()}."
        )
        confidence = "HIGH" if funding_apr_smooth > config.entry_threshold_apr * 1.5 else "MEDIUM"
        expected_hold = config.min_hold_periods / 3 * 1.5    # heuristic
        expected_yield = funding_apr_smooth * expected_hold / 365
    elif short_signal and not risk_flags:
        action = "ENTER_SHORT_BASIS"
        rationale = (
            f"Smoothed funding {funding_apr_smooth:.1f}% APR < -{config.entry_threshold_apr:.0f}% "
            f"threshold. Regime: {regime.upper()}. Position: short spot, long perp, "
            f"collect from shorts."
        )
        confidence = "MEDIUM"     # reverse trades are noisier
        expected_hold = config.min_hold_periods / 3 * 1.5
        expected_yield = abs(funding_apr_smooth) * expected_hold / 365
    elif risk_flags:
        action = "FLAT"
        rationale = "Risk flags present; do not enter new positions."
        confidence = "LOW"
        expected_hold = None
        expected_yield = None
    else:
        action = "FLAT"
        rationale = (
            f"Smoothed funding {funding_apr_smooth:+.1f}% APR within neutral band "
            f"(±{config.entry_threshold_apr:.0f}%). No entry signal."
        )
        confidence = "HIGH"
        expected_hold = None
        expected_yield = None

    return AssetSignal(
        symbol=str(latest["symbol"]),
        timestamp=latest["timestamp"],
        spot_price=float(latest["spot_price"]),
        perp_price=float(latest["perp_price"]),
        basis_pct=float(latest["basis_pct"]),
        funding_rate_raw=float(latest["funding_rate"]),
        funding_apr_raw=funding_apr_raw,
        funding_apr_smooth=funding_apr_smooth,
        regime=regime,
        action=action,
        rationale=rationale,
        confidence=confidence,
        expected_hold_days=expected_hold,
        expected_funding_yield_pct=expected_yield,
        risk_flags=risk_flags,
    )


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------
def render_report(signals: List[AssetSignal], as_of: pd.Timestamp, config) -> str:
    lines = []
    bar = "=" * 78

    lines.append(bar)
    lines.append(f"  FRA-001  ·  DAILY SIGNAL REPORT")
    lines.append(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  As-of:     {as_of}")
    lines.append(f"  Config:    {config.name}  ({config.description.split('.')[0]})")
    lines.append(bar)
    lines.append("")

    # Summary line
    enter_long = [s for s in signals if s.action == "ENTER_LONG_BASIS"]
    enter_short = [s for s in signals if s.action == "ENTER_SHORT_BASIS"]
    flat = [s for s in signals if s.action == "FLAT"]

    lines.append(f"  SUMMARY:  {len(enter_long)} LONG-BASIS  ·  "
                 f"{len(enter_short)} SHORT-BASIS  ·  {len(flat)} FLAT")
    lines.append("")
    lines.append(bar)
    lines.append("")

    # Per-asset detail
    for sig in signals:
        action_marker = {
            "ENTER_LONG_BASIS":  "→ ENTER  LONG-BASIS",
            "ENTER_SHORT_BASIS": "→ ENTER  SHORT-BASIS",
            "HOLD":              "  HOLD",
            "EXIT":              "← EXIT",
            "FLAT":              "  FLAT (no action)",
        }.get(sig.action, sig.action)

        lines.append(f"  [{sig.symbol:<4}]  {action_marker:<28}  CONFIDENCE: {sig.confidence}")
        lines.append(f"          Spot:        ${sig.spot_price:>12,.2f}")
        lines.append(f"          Perp:        ${sig.perp_price:>12,.2f}")
        lines.append(f"          Basis:       {sig.basis_pct:>+8.4f}%")
        lines.append(f"          Funding 8h:  {sig.funding_rate_raw*100:>+8.4f}%   "
                     f"(APR raw {sig.funding_apr_raw:+.1f}%, smoothed {sig.funding_apr_smooth:+.1f}%)")
        lines.append(f"          Regime:      {sig.regime.upper()}")
        lines.append(f"          Rationale:   {sig.rationale}")

        if sig.expected_hold_days is not None:
            lines.append(f"          Expected:    ~{sig.expected_hold_days:.1f} day hold, "
                         f"~{sig.expected_funding_yield_pct:.2f}% yield on notional")

        if sig.risk_flags:
            lines.append(f"          RISK FLAGS:")
            for f in sig.risk_flags:
                lines.append(f"            ⚠ {f}")

        if sig.action in ("ENTER_LONG_BASIS", "ENTER_SHORT_BASIS"):
            lines.append(f"          EXECUTION:")
            n = config.notional_per_trade
            spot_units = n / sig.spot_price
            perp_units = n / sig.perp_price
            if sig.action == "ENTER_LONG_BASIS":
                lines.append(f"            BUY  {spot_units:>10.6f} {sig.symbol} spot   @ ~${sig.spot_price:,.2f}")
                lines.append(f"            SELL {perp_units:>10.6f} {sig.symbol} perp   @ ~${sig.perp_price:,.2f}")
            else:
                lines.append(f"            SELL {spot_units:>10.6f} {sig.symbol} spot   @ ~${sig.spot_price:,.2f}")
                lines.append(f"            BUY  {perp_units:>10.6f} {sig.symbol} perp   @ ~${sig.perp_price:,.2f}")
            lines.append(f"            Notional per leg: ${n:,.0f}  ·  "
                         f"Est round-trip cost: ${n*0.0036:,.2f}")

        lines.append("")

    lines.append(bar)
    lines.append("  ACTIONS FOR THE DESK")
    lines.append(bar)
    lines.append("")
    if enter_long or enter_short:
        lines.append("  1. Review each ENTER signal above. Verify funding rate against live")
        lines.append("     exchange data before committing capital.")
        lines.append("  2. Execute legs simultaneously to minimize slippage between spot/perp.")
        lines.append("  3. Set basis-stop alert at ±1.0% deviation; auto-close on trigger.")
        lines.append("  4. Log entry in trade journal; tag with this report timestamp.")
    else:
        lines.append("  No new entries today. Monitor existing positions per usual.")
        lines.append("  Next signal check: tomorrow morning before 08:00 UTC funding cycle.")
    lines.append("")
    lines.append(bar)
    lines.append("  END OF REPORT")
    lines.append(bar)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Daily funding rate signal report")
    parser.add_argument("--as-of", type=str, default=None,
                        help="As-of date (YYYY-MM-DD). Default: latest available.")
    parser.add_argument("--data", type=str, default="funding_data.csv",
                        help="Path to funding-rate CSV.")
    parser.add_argument("--save", action="store_true",
                        help="Write report to example_outputs/")
    args = parser.parse_args()

    as_of = pd.to_datetime(args.as_of) if args.as_of else None

    df = load_latest_data(args.data, as_of)
    config = ACTIVE_CONFIG

    # Generate signals per asset
    signals = []
    for symbol in config.assets:
        asset_df = df[df.symbol == symbol]
        if len(asset_df) == 0:
            continue
        signals.append(generate_asset_signal(asset_df, config))

    if not signals:
        print("No data available for any configured asset.")
        sys.exit(1)

    as_of_actual = max(s.timestamp for s in signals)
    report = render_report(signals, as_of_actual, config)
    print(report)

    if args.save:
        date_tag = pd.Timestamp(as_of_actual).strftime("%Y-%m-%d")
        out_dir = "example_outputs"
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"daily_signals_{date_tag}.txt")
        with open(out_path, "w") as f:
            f.write(report)
        print(f"\n(report saved to {out_path})")


if __name__ == "__main__":
    main()
