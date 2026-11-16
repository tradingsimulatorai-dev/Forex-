import glob
import os

import numpy as np
import pandas as pd


REQUIRED = {"time", "open", "high", "low", "close"}
STRATEGIES = ("EMA Trend", "EMA Crossover", "RSI Reversal")


def load_csv(path):
    try:
        df = pd.read_csv(path)

        if not REQUIRED.issubset(df.columns):
            return None

        df["time"] = pd.to_datetime(
            df["time"].astype(str).str.replace(".", "-", regex=False),
            errors="coerce",
            utc=True,
        )

        for column in ("open", "high", "low", "close"):
            df[column] = pd.to_numeric(df[column], errors="coerce")

        df = df.dropna(subset=["time", "open", "high", "low", "close"])
        df = df[df["high"] >= df["low"]]
        df = df[df["close"] > 0]
        df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)

        return df if len(df) >= 300 else None

    except Exception as exc:
        print(f"Could not read {path}: {exc}")
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

    rs = gains / losses.replace(0, np.nan)
    df["rsi"] = (100 - 100 / (1 + rs)).fillna(50)

    return df


def signal(row, previous, strategy):
    if strategy == "EMA Trend":
        if row.ema20 > row.ema50 and row.close > row.ema200:
            return 1
        if row.ema20 < row.ema50 and row.close < row.ema200:
            return -1

    elif strategy == "EMA Crossover":
        if previous.ema20 <= previous.ema50 and row.ema20 > row.ema50:
            return 1
        if previous.ema20 >= previous.ema50 and row.ema20 < row.ema50:
            return -1

    elif strategy == "RSI Reversal":
        if previous.rsi < 30 and row.rsi >= 30:
            return 1
        if previous.rsi > 70 and row.rsi <= 70:
            return -1

    return 0


def simulate(df, strategy, sl_mult, tp_mult):
    outcomes = []
    index = 201

    while index < len(df) - 1:
        row = df.iloc[index]
        previous = df.iloc[index - 1]
        direction = signal(row, previous, strategy)
        atr = float(row.atr) if pd.notna(row.atr) else 0.0

        if not direction or not np.isfinite(atr) or atr <= 0:
            index += 1
            continue

        entry_index = index + 1
        entry = float(df.iloc[entry_index].open)
        risk = atr * float(sl_mult)

        if risk <= 0:
            index += 1
            continue

        stop = entry - direction * risk
        target = entry + direction * atr * float(tp_mult)
        result = None
        exit_index = entry_index

        for candle_index in range(entry_index, len(df)):
            candle = df.iloc[candle_index]

            if direction > 0:
                stop_hit = float(candle.low) <= stop
                target_hit = float(candle.high) >= target
            else:
                stop_hit = float(candle.high) >= stop
                target_hit = float(candle.low) <= target

            # Conservative assumption if both target and stop hit in one candle.
            if stop_hit:
                result = -1.0
                exit_index = candle_index
                break

            if target_hit:
                result = float(tp_mult) / float(sl_mult)
                exit_index = candle_index
                break

        if result is None:
            result = direction * (float(df.iloc[-1].close) - entry) / risk
            exit_index = len(df) - 1

        outcomes.append(float(result))
        index = exit_index + 1

    return outcomes


def stats(outcomes):
    n = len(outcomes)
    wins = sum(value > 0 for value in outcomes)
    gross_wins = sum(value for value in outcomes if value > 0)
    gross_losses = abs(sum(value for value in outcomes if value < 0))

    if gross_losses:
        pf = gross_wins / gross_losses
    elif gross_wins:
        pf = 99.0
    else:
        pf = None

    return {
        "n": n,
        "w": wins,
        "wr": wins / n * 100 if n else 0.0,
        "gw": gross_wins,
        "gl": gross_losses,
        "tot": sum(outcomes),
        "pf": pf,
    }


def run_all(folder, sl_mult=1.5, tp_mult=3.0):
    results = []

    for path in sorted(glob.glob(os.path.join(folder, "*_M15.csv"))):
        raw = load_csv(path)
        if raw is None:
            print(f"Skipped invalid or short history file: {path}")
            continue

        df = add_indicators(raw)
        split = int(len(df) * 2 / 3)
        symbol = os.path.basename(path).split("_")[0]

        for strategy in STRATEGIES:
            train = df.iloc[:split].reset_index(drop=True)

            # Include warmup candles before the test segment.
            test_start = max(0, split - 201)
            test = df.iloc[test_start:].reset_index(drop=True)

            is_stats = stats(simulate(train, strategy, sl_mult, tp_mult))
            oos_stats = stats(simulate(test, strategy, sl_mult, tp_mult))

            is_pf = is_stats["pf"]
            oos_pf = oos_stats["pf"]

            results.append({
                "sym": symbol,
                "strat": strategy,
                "from": df.time.iloc[0].strftime("%Y-%m-%d"),
                "to": df.time.iloc[-1].strftime("%Y-%m-%d"),
                "is": is_stats,
                "oos": oos_stats,
                "ok": bool(
                    oos_stats["n"] >= 30
                    and is_pf is not None
                    and oos_pf is not None
                    and is_pf > 1.0
                    and oos_pf > 1.1
                ),
            })

    return results
                
