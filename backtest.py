"""OHLC backtester for the Strategy Lab Flask app.

CSV files must contain: time, open, high, low, close.
The app expects run_all() to return results with sym, strat, from, to,
is, oos and ok fields.
"""
import glob
import os

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = {"time", "open", "high", "low", "close"}
STRATEGIES = ("EMA Trend", "EMA Crossover", "RSI Reversal")


def load_csv(path):
    try:
        df = pd.read_csv(path)

        if not REQUIRED_COLUMNS.issubset(df.columns):
            return None

        df = df.copy()
        df["time"] = pd.to_datetime(
            df["time"].astype(str).str.replace(".", "-", regex=False),
            errors="coerce",
            utc=True,
        )

        for column in ("open", "high", "low", "close"):
            df[column] = pd.to_numeric(df[column], errors="coerce")

        df = df.dropna(
            subset=["time", "open", "high", "low", "close"]
        )
        df = df[df["high"] >= df["low"]]
        df = df[df["close"] > 0]
        df = df.drop_duplicates("time")
        df = df.sort_values("time").reset_index(drop=True)

        return df if len(df) >= 300 else None
    except Exception:
        return None


def add_indicators(df):
    df = df.copy()

    previous_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    df["atr"] = true_range.ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14,
    ).mean()

    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

    change = df["close"].diff()
    gains = change.clip(lower=0).ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14,
    ).mean()
    losses = (-change.clip(upper=0)).ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14,
    ).mean()

    relative_strength = gains / losses.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + relative_strength))
    df["rsi"] = df["rsi"].fillna(50)

    return df


def get_signal(row, previous_row, strategy):
    """Return 1 for buy, -1 for sell, or 0 for no signal."""
    if strategy == "EMA Trend":
        if row.ema20 > row.ema50 and row.close > row.ema200:
            return 1
        if row.ema20 < row.ema50 and row.close < row.ema200:
            return -1

    elif strategy == "EMA Crossover":
        if previous_row.ema20 <= previous_row.ema50:
            if row.ema20 > row.ema50:
                return 1
        if previous_row.ema20 >= previous_row.ema50:
            if row.ema20 < row.ema50:
                return -1

    elif strategy == "RSI Reversal":
        if previous_row.rsi < 30 and row.rsi >= 30:
            return 1
        if previous_row.rsi > 70 and row.rsi <= 70:
            return -1

    return 0


def simulate(df, strategy, sl_mult, tp_mult):
    """Simulate trades; each outcome is measured in R multiples."""
    outcomes = []
    i = 201

    while i < len(df) - 1:
        row = df.iloc[i]
        previous_row = df.iloc[i - 1]
        direction = get_signal(row, previous_row, strategy)

        atr = float(row.atr) if pd.notna(row.atr) else 0.0
        if direction == 0 or not np.isfinite(atr) or atr <= 0:
            i += 1
            continue

        entry_index = i + 1
        entry_price = float(df.iloc[entry_index].open)
        risk = atr * float(sl_mult)

        if risk <= 0:
            i += 1
            continue

        stop_price = entry_price - direction * risk
        target_price = entry_price + direction * atr * float(tp_mult)
        outcome = None
        exit_index = entry_index

        for j in range(entry_index, len(df)):
            candle = df.iloc[j]

            if direction == 1:
                stop_hit = float(candle.low) <= stop_price
                target_hit = float(candle.high) >= target_price
            else:
                stop_hit = float(candle.high) >= stop_price
                target_hit = float(candle.low) <= target_price

            # If both levels are hit in one candle, assume stop-loss first.
            if stop_hit:
                outcome = -1.0
                exit_index = j
                break

            if target_hit:
                outcome = float(tp_mult) / float(sl_mult)
                exit_index = j
                break

        if outcome is None:
            final_price = float(df.iloc[-1].close)
            outcome = direction * (final_price - entry_price) / risk
            exit_index = len(df) - 1

        outcomes.append(float(outcome))
        i = exit_index + 1

    return outcomes


def calculate_stats(outcomes):
    count = len(outcomes)
    wins = sum(value > 0 for value in outcomes)
    gross_wins = sum(value for value in outcomes if value > 0)
    gross_losses = abs(sum(value for value in outcomes if value < 0))
    total = sum(outcomes)

    if gross_losses:
        profit_factor = gross_wins / gross_losses
    elif gross_wins:
        profit_factor = 99.0
    else:
        profit_factor = None

    return {
        "n": count,
        "w": wins,
        "wr": (wins / count * 100) if count else 0.0,
        "gw": gross_wins,
        "gl": gross_losses,
        "tot": total,
        "pf": profit_factor,
    }


def run_all(folder, sl_mult=1.5, tp_mult=3.0):
    """Run all example strategies against CSV files in the history folder."""
    results = []

    for path in sorted(glob.glob(os.path.join(folder, "*.csv"))):
        raw_data = load_csv(path)
        if raw_data is None:
            continue

        df = add_indicators(raw_data)
        split_index = int(len(df) * 2 / 3)

        for strategy in STRATEGIES:
            in_sample = df.iloc[:split_index].reset_index(drop=True)

            # Keep 201 earlier rows for indicator warmup; trades begin
            # within the out-of-sample period.
            out_of_sample = df.iloc[
                max(0, split_index - 201):
            ].reset_index(drop=True)

            is_stats = calculate_stats(
                simulate(in_sample, strategy, sl_mult, tp_mult)
            )
            oos_stats = calculate_stats(
                simulate(out_of_sample, strategy, sl_mult, tp_mult)
            )

            oos_pf = oos_stats["pf"]
            is_pf = is_stats["pf"]

            passed = (
                oos_stats["n"] >= 30
                and oos_pf is not None
                and is_pf is not None
                and oos_pf > 1.1
                and is_pf > 1.0
            )

            results.append({
                "sym": os.path.basename(path).split("_")[0],
                "strat": strategy,
                "from": df["time"].iloc[0].strftime("%Y-%m-%d"),
                "to": df["time"].iloc[-1].strftime("%Y-%m-%d"),
                "is": is_stats,
                "oos": oos_stats,
                "ok": bool(passed),
            })

    return results
    
