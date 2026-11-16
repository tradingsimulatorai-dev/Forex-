import ast
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = {"time", "open", "high", "low", "close"}


class RuleError(ValueError):
    pass


def load_data(csv_path):
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"History file not found: {csv_path}")

    frame = pd.read_csv(csv_path)

    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(
            "CSV is missing columns: " + ", ".join(sorted(missing))
        )

    frame = frame.copy()
    frame["time"] = pd.to_datetime(
        frame["time"],
        utc=True,
        errors="coerce",
    )

    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    if "volume" not in frame:
        frame["volume"] = 0.0
    else:
        frame["volume"] = pd.to_numeric(
            frame["volume"],
            errors="coerce",
        ).fillna(0.0)

    frame = frame.dropna(
        subset=["time", "open", "high", "low", "close"]
    )
    frame = frame[frame["high"] >= frame["low"]]
    frame = frame[frame["close"] > 0]
    frame = frame.drop_duplicates("time")
    frame = frame.sort_values("time").reset_index(drop=True)

    if len(frame) < 1000:
        raise ValueError(
            f"Only {len(frame)} candles found; at least 1,000 are required."
        )

    return frame


def ema(series, period):
    period = int(period)
    if not 1 <= period <= 500:
        raise RuleError("Indicator periods must be between 1 and 500.")

    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(series, period):
    period = int(period)
    if not 1 <= period <= 500:
        raise RuleError("Indicator periods must be between 1 and 500.")

    return series.rolling(period, min_periods=period).mean()


def calculate_atr(frame, period=14):
    period = int(period)
    if not 1 <= period <= 500:
        raise RuleError("Indicator periods must be between 1 and 500.")

    previous_close = frame["close"].shift(1)
    ranges = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return ranges.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()


def calculate_rsi(close, period=14):
    period = int(period)
    if not 1 <= period <= 500:
        raise RuleError("Indicator periods must be between 1 and 500.")

    change = close.diff()
    gains = change.clip(lower=0).ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()
    losses = (-change.clip(upper=0)).ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    relative_strength = gains / losses.replace(0, np.nan)
    return (100 - 100 / (1 + relative_strength)).fillna(50)


def build_context(frame):
    indicators_atr = calculate_atr(frame, 14)

    return {
        "open": frame["open"],
        "high": frame["high"],
        "low": frame["low"],
        "close": frame["close"],
        "volume": frame["volume"],
        "atr": lambda period=14: calculate_atr(frame, period),
        "ema": lambda series, period: ema(series, period),
        "sma": lambda series, period: sma(series, period),
        "rsi": lambda period=14: calculate_rsi(frame["close"], period),
        "cross_above": lambda a, b: (a > b) & (a.shift(1) <= b.shift(1)),
        "cross_below": lambda a, b: (a < b) & (a.shift(1) >= b.shift(1)),
        "_atr14": indicators_atr,
    }


ALLOWED_BINARY = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Mod: lambda a, b: a % b,
}

ALLOWED_COMPARE = {
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
}

ALLOWED_FUNCTIONS = {
    "ema",
    "sma",
    "rsi",
    "atr",
    "cross_above",
    "cross_below",
    "abs",
}


def evaluate_node(node, context):
    if isinstance(node, ast.Expression):
        return evaluate_node(node.body, context)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, bool)):
            return node.value
        raise RuleError("Only numeric and boolean constants are allowed.")

    if isinstance(node, ast.Name):
        if node.id in context:
            return context[node.id]
        if node.id == "True":
            return True
        if node.id == "False":
            return False
        raise RuleError(f"Unknown name: {node.id}")

    if isinstance(node, ast.BinOp):
        operation = ALLOWED_BINARY.get(type(node.op))
        if operation is None:
            raise RuleError("That arithmetic operator is not supported.")

        left = evaluate_node(node.left, context)
        right = evaluate_node(node.right, context)
        return operation(left, right)

    if isinstance(node, ast.UnaryOp):
        value = evaluate_node(node.operand, context)

        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.UAdd):
            return value
        if isinstance(node.op, ast.Not):
            return ~value if isinstance(value, pd.Series) else not value

        raise RuleError("That unary operator is not supported.")

    if isinstance(node, ast.BoolOp):
        values = [evaluate_node(value, context) for value in node.values]
        if not values:
            raise RuleError("Empty boolean expression.")

        result = values[0]
        for value in values[1:]:
            if isinstance(node.op, ast.And):
                result = result & value
            elif isinstance(node.op, ast.Or):
                result = result | value
            else:
                raise RuleError("Only and/or are supported.")
        return result

    if isinstance(node, ast.Compare):
        left = evaluate_node(node.left, context)
        result = True

        for operation_node, comparator in zip(node.ops, node.comparators):
            operation = ALLOWED_COMPARE.get(type(operation_node))
            if operation is None:
                raise RuleError("That comparison is not supported.")

            right = evaluate_node(comparator, context)
            comparison = operation(left, right)
            result = result & comparison
            left = right

        return result

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise RuleError("Only named indicator functions are allowed.")

        function_name = node.func.id
        if function_name not in ALLOWED_FUNCTIONS:
            raise RuleError(f"Function not allowed: {function_name}")

        function = context.get(function_name)
        if function is None and function_name == "abs":
            function = abs
        if function is None:
            raise RuleError(f"Function not available: {function_name}")

        if node.keywords:
            raise RuleError("Use positional arguments for indicator functions.")

        arguments = [
            evaluate_node(argument, context)
            for argument in node.args
        ]

        try:
            return function(*arguments)
        except Exception as exc:
            raise RuleError(
                f"Could not evaluate {function_name}(): {exc}"
            ) from exc

    raise RuleError(
        "Unsupported rule syntax. Use comparisons, and/or/not, "
        "arithmetic and the listed indicators."
    )


def evaluate_rule(expression, context, candle_count):
    if len(expression) > 500:
        raise RuleError("Each rule must be 500 characters or less.")

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise RuleError(
            f"Rule syntax error near: {exc.text or expression}"
        ) from exc

    result = evaluate_node(tree, context)

    if isinstance(result, pd.Series):
        signal = result.reindex(range(candle_count))
    elif isinstance(result, (bool, np.bool_)):
        signal = pd.Series(bool(result), index=range(candle_count))
    else:
        raise RuleError("A rule must produce a true/false condition.")

    return signal.fillna(False).astype(bool).to_numpy()


def trade_results(frame, signals, direction, stop_atr, target_atr,
                  start_index, end_index):
    """Enter at next candle open, with a single open trade at a time."""
    outcomes = []
    atr_values = calculate_atr(frame, 14).to_numpy()

    index = max(start_index, 201)
    end_index = min(end_index, len(frame) - 1)

    while index < end_index - 1:
        if not signals[index]:
            index += 1
            continue

        entry_index = index + 1
        entry = float(frame.iloc[entry_index]["open"])
        atr_value = atr_values[index]

        if not np.isfinite(atr_value) or atr_value <= 0:
            index += 1
            continue

        risk = float(stop_atr) * atr_value
        reward = float(target_atr) * atr_value
        stop = entry - direction * risk
        target = entry + direction * reward

        result_r = None
        exit_index = entry_index

        for candle_index in range(entry_index, end_index):
            candle = frame.iloc[candle_index]

            if direction == 1:
                stop_hit = float(candle["low"]) <= stop
                target_hit = float(candle["high"]) >= target
            else:
                stop_hit = float(candle["high"]) >= stop
                target_hit = float(candle["low"]) <= target

            # If both levels are touched inside one candle, assume stop first.
            if stop_hit:
                result_r = -1.0
                exit_index = candle_index
                break

            if target_hit:
                result_r = float(target_atr) / float(stop_atr)
                exit_index = candle_index
                break

        if result_r is None:
            final_close = float(frame.iloc[end_index - 1]["close"])
            result_r = direction * (final_close - entry) / risk
            exit_index = end_index - 1

        outcomes.append(float(result_r))
        index = exit_index + 1

    return outcomes


def summarize(outcomes, starting_balance):
    trade_count = len(outcomes)
    wins = [value for value in outcomes if value > 0]
    losses = [value for value in outcomes if value < 0]

    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))

    if gross_losses > 0:
        profit_factor = gross_wins / gross_losses
    elif gross_wins:
        profit_factor = None
    else:
        profit_factor = None

    average_win = sum(wins) / len(wins) if wins else None
    average_loss = abs(sum(losses) / len(losses)) if losses else None
    average_rrr = (
        average_win / average_loss
        if average_win is not None and average_loss
        else None
    )

    cumulative = np.cumsum([0.0] + outcomes)
    running_peak = np.maximum.accumulate(cumulative)
    drawdowns = running_peak - cumulative
    max_drawdown_r = float(drawdowns.max()) if len(drawdowns) else 0.0

    net_r = float(sum(outcomes))
    risk_fraction = 0.01
    ending_balance = starting_balance * (1 + risk_fraction * net_r)

    return {
        "trades": trade_count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / trade_count * 100) if trade_count else 0.0,
        "profit_factor": profit_factor,
        "average_win_r": average_win,
        "average_loss_r": average_loss,
        "average_rrr": average_rrr,
        "net_r": net_r,
        "max_drawdown_r": max_drawdown_r,
        "estimated_balance": ending_balance,
        "assumed_risk_per_trade_pct": 1.0,
    }


def run_backtest(csv_path, symbol, long_rule, short_rule,
                 stop_atr, target_atr, starting_balance):
    frame = load_data(csv_path)
    context = build_context(frame)

    long_signals = evaluate_rule(
        long_rule,
        context,
        len(frame),
    )
    short_signals = evaluate_rule(
        short_rule,
        context,
        len(frame),
    )

    if np.any(long_signals & short_signals):
        raise RuleError(
            "Long and short rules are both true on some candles. "
            "Change the rules so they do not conflict."
        )

    split = int(len(frame) * 2 / 3)

    in_sample_outcomes = []
    in_sample_outcomes += trade_results(
        frame,
        long_signals,
        1,
        stop_atr,
        target_atr,
        201,
        split,
    )
    in_sample_outcomes += trade_results(
        frame,
        short_signals,
        -1,
        stop_atr,
        target_atr,
        201,
        split,
    )

    out_sample_outcomes = []
    out_sample_outcomes += trade_results(
        frame,
        long_signals,
        1,
        stop_atr,
        target_atr,
        split,
        len(frame),
    )
    out_sample_outcomes += trade_results(
        frame,
        short_signals,
        -1,
        stop_atr,
        target_atr,
        split,
        len(frame),
    )

    return {
        "symbol": symbol,
        "timeframe": "M15",
        "data_from": frame["time"].iloc[0].strftime("%Y-%m-%d"),
        "data_to": frame["time"].iloc[-1].strftime("%Y-%m-%d"),
        "candles": len(frame),
        "long_rule": long_rule,
        "short_rule": short_rule,
        "stop_atr": float(stop_atr),
        "target_atr": float(target_atr),
        "risk_reward_target": float(target_atr) / float(stop_atr),
        "in_sample": summarize(
            in_sample_outcomes,
            starting_balance,
        ),
        "out_of_sample": summarize(
            out_sample_outcomes,
            starting_balance,
        ),
        "notes": [
            "Trades enter at the next candle open after a signal.",
            "If stop and target are both touched in one candle, stop is assumed first.",
            "Spread, commission and slippage are not included.",
            "The balance estimate assumes a fixed 1% of initial balance risk per R.",
        ],
            }
            
