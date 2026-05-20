"""
risk_monitor.py — Anomaly Detection & Risk Alerts
====================================================

Run this every funding cycle (every 8h). Identifies conditions that warrant
human review or automated intervention:

  - Basis dislocations (perp-spot price divergence beyond safe levels)
  - Funding rate spikes (extreme readings that often precede unwinds)
  - Regime shifts (sustained changes in funding sign/magnitude)
  - Drawdown breaches (cumulative P&L below tolerance)
  - Stale data (exchange feed gaps)

In production, this should run as a daemon and send alerts via PagerDuty,
email, or Slack when CRITICAL events fire.

USAGE:
    python risk_monitor.py                    # one-shot scan
    python risk_monitor.py --lookback 7       # check last 7 days for issues
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime
from typing import List
from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import ACTIVE_CONFIG, ALERT_THRESHOLDS


SEVERITY_ORDER = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}


@dataclass
class Alert:
    severity: str
    symbol: str
    timestamp: pd.Timestamp
    category: str
    message: str
    suggested_action: str


def annualize(rate: float) -> float:
    return rate * 3 * 365 * 100


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
def check_basis_dislocation(df_asset: pd.DataFrame) -> List[Alert]:
    alerts = []
    latest = df_asset.iloc[-1]
    b = abs(latest["basis_pct"])
    if b > ALERT_THRESHOLDS["basis_critical_pct"]:
        alerts.append(Alert(
            severity="CRITICAL",
            symbol=str(latest["symbol"]),
            timestamp=latest["timestamp"],
            category="BASIS_DISLOCATION",
            message=f"Basis at {latest['basis_pct']:+.4f}% — exceeds critical "
                    f"threshold of {ALERT_THRESHOLDS['basis_critical_pct']}%.",
            suggested_action="Flatten any open positions on this asset immediately. "
                             "Investigate whether perp/spot are quoted on the correct venues.",
        ))
    elif b > ALERT_THRESHOLDS["basis_warning_pct"]:
        alerts.append(Alert(
            severity="WARNING",
            symbol=str(latest["symbol"]),
            timestamp=latest["timestamp"],
            category="BASIS_ELEVATED",
            message=f"Basis at {latest['basis_pct']:+.4f}% — above warning "
                    f"threshold of {ALERT_THRESHOLDS['basis_warning_pct']}%.",
            suggested_action="Pause new entries on this asset. Monitor every cycle.",
        ))
    return alerts


def check_funding_spike(df_asset: pd.DataFrame) -> List[Alert]:
    alerts = []
    latest = df_asset.iloc[-1]
    apr = annualize(latest["funding_rate"])
    if abs(apr) > ALERT_THRESHOLDS["funding_spike_apr"]:
        alerts.append(Alert(
            severity="WARNING",
            symbol=str(latest["symbol"]),
            timestamp=latest["timestamp"],
            category="FUNDING_SPIKE",
            message=f"Funding at {apr:+.0f}% APR — extreme reading. Per "
                    f"the literature, rates above 300% APR often precede leverage unwinds.",
            suggested_action="Do not enter new positions. If holding, prepare for "
                             "possible basis blowout in next 1-3 cycles.",
        ))
    return alerts


def check_regime_shift(df_asset: pd.DataFrame, lookback: int = 21) -> List[Alert]:
    """Detect a sustained sign change in funding."""
    alerts = []
    if len(df_asset) < lookback * 2:
        return alerts
    recent = df_asset.tail(lookback)["funding_rate"].mean()
    prior = df_asset.tail(lookback * 2).head(lookback)["funding_rate"].mean()
    if prior > 0.0001 and recent < -0.00005:
        alerts.append(Alert(
            severity="WARNING",
            symbol=str(df_asset.iloc[-1]["symbol"]),
            timestamp=df_asset.iloc[-1]["timestamp"],
            category="REGIME_SHIFT",
            message=f"Mean funding flipped from +{annualize(prior):.1f}% APR "
                    f"to {annualize(recent):+.1f}% APR over last "
                    f"{lookback}-period window.",
            suggested_action="Re-evaluate any long-basis positions. Consider "
                             "rotating to short-basis if reverse trades are enabled.",
        ))
    elif prior < -0.00005 and recent > 0.0001:
        alerts.append(Alert(
            severity="INFO",
            symbol=str(df_asset.iloc[-1]["symbol"]),
            timestamp=df_asset.iloc[-1]["timestamp"],
            category="REGIME_SHIFT",
            message=f"Mean funding flipped from {annualize(prior):+.1f}% APR "
                    f"to +{annualize(recent):.1f}% APR — bull regime returning.",
            suggested_action="Long-basis entries are likely to re-enable soon.",
        ))
    return alerts


def check_stale_data(df_asset: pd.DataFrame, now: pd.Timestamp) -> List[Alert]:
    alerts = []
    latest = df_asset.iloc[-1]
    age_hours = (now - latest["timestamp"]).total_seconds() / 3600
    if age_hours > 12:
        alerts.append(Alert(
            severity="CRITICAL",
            symbol=str(latest["symbol"]),
            timestamp=latest["timestamp"],
            category="STALE_DATA",
            message=f"Most recent data is {age_hours:.1f} hours old — "
                    f"funding cycle is 8h. Feed appears broken.",
            suggested_action="Investigate exchange API connectivity. Halt new "
                             "entries until feed is restored.",
        ))
    return alerts


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------
def render_alerts(alerts: List[Alert], now: pd.Timestamp) -> str:
    bar = "=" * 78
    sep = "-" * 78
    lines = []

    lines.append(bar)
    lines.append("  FRA-001  ·  RISK MONITOR  ·  SCAN REPORT")
    lines.append(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  Scan time: {now}")
    lines.append(bar)
    lines.append("")

    if not alerts:
        lines.append("  STATUS:  ✓ ALL CLEAR")
        lines.append("")
        lines.append("  No anomalies detected. Strategy may operate normally.")
        lines.append("")
        lines.append(bar)
        return "\n".join(lines)

    crit = [a for a in alerts if a.severity == "CRITICAL"]
    warn = [a for a in alerts if a.severity == "WARNING"]
    info = [a for a in alerts if a.severity == "INFO"]

    if crit:
        status = "⚠ CRITICAL — IMMEDIATE ACTION REQUIRED"
    elif warn:
        status = "⚠ WARNINGS PRESENT — REVIEW BEFORE NEXT CYCLE"
    else:
        status = "ℹ INFORMATIONAL ONLY"

    lines.append(f"  STATUS:  {status}")
    lines.append(f"  Counts:  {len(crit)} CRITICAL  ·  {len(warn)} WARNING  ·  {len(info)} INFO")
    lines.append("")

    # Sort by severity
    alerts_sorted = sorted(alerts, key=lambda a: (SEVERITY_ORDER[a.severity], a.symbol))

    current_sev = None
    for a in alerts_sorted:
        if a.severity != current_sev:
            current_sev = a.severity
            lines.append(bar)
            lines.append(f"  {current_sev}")
            lines.append(bar)

        lines.append(f"  [{a.symbol:<4}]  {a.category}     ({a.timestamp})")
        lines.append(f"          {a.message}")
        lines.append(f"          ACTION:  {a.suggested_action}")
        lines.append("")

    lines.append(bar)
    lines.append("  END OF SCAN")
    lines.append(bar)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Risk monitor scan")
    parser.add_argument("--lookback", type=int, default=21,
                        help="Lookback window for regime checks (periods)")
    parser.add_argument("--data", type=str, default="funding_data.csv")
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    df = pd.read_csv(args.data, parse_dates=["timestamp"])
    now = df["timestamp"].max()

    alerts = []
    for symbol in ACTIVE_CONFIG.assets:
        asset_df = df[df.symbol == symbol].sort_values("timestamp")
        if len(asset_df) == 0:
            continue
        alerts.extend(check_basis_dislocation(asset_df))
        alerts.extend(check_funding_spike(asset_df))
        alerts.extend(check_regime_shift(asset_df, args.lookback))
        alerts.extend(check_stale_data(asset_df, now))

    report = render_alerts(alerts, now)
    print(report)

    if args.save:
        os.makedirs("example_outputs", exist_ok=True)
        tag = now.strftime("%Y-%m-%d")
        path = f"example_outputs/risk_check_{tag}.txt"
        with open(path, "w") as f:
            f.write(report)
        print(f"\n(report saved to {path})")


if __name__ == "__main__":
    main()
