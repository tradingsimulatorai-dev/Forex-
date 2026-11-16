import json
import os
import subprocess
import threading
from datetime import date, datetime, timedelta, timezone

import pandas as pd
from flask import Flask, jsonify, render_template_string, request

from backtest import run_backtest


app = Flask(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "data")
HIST_DIR = os.path.join(DATA_DIR, "hist")
os.makedirs(HIST_DIR, exist_ok=True)

# Dukascopy instrument IDs for six commonly used major pairs.
PAIRS = {
    "EURUSD": "Euro / US Dollar",
    "GBPUSD": "British Pound / US Dollar",
    "AUDUSD": "Australian Dollar / US Dollar",
    "USDCHF": "US Dollar / Swiss Franc",
    "USDCAD": "US Dollar / Canadian Dollar",
    "USDJPY": "US Dollar / Japanese Yen",
}

NODE_DOWNLOAD_SCRIPT = r"""
const { getHistoricalRates } = require("dukascopy-node");

async function main() {
  const [instrument, fromDate, toDate] = process.argv.slice(-3);

  const candles = await getHistoricalRates({
    instrument: instrument.toLowerCase(),
    dates: { from: fromDate, to: toDate },
    timeframe: "m15",
    format: "json"
  });

  process.stdout.write(JSON.stringify(candles));
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
"""

STATE_LOCK = threading.Lock()
JOB_LOCK = threading.Lock()

STATE = {
    "status": "idle",
    "message": "Ready",
    "error": "",
    "result": None,
    "updated_at": None,
}


def download_history(symbol, start_date, end_date):
    """Download one year of M15 candles from Dukascopy."""
    output_file = os.path.join(HIST_DIR, f"{symbol}_M15.csv")

    completed = subprocess.run(
        [
            "node",
            "-e",
            NODE_DOWNLOAD_SCRIPT,
            symbol.lower(),
            start_date,
            end_date,
        ],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )

    if completed.returncode != 0:
        error_text = completed.stderr.strip() or "Dukascopy download failed"
        raise RuntimeError(f"{symbol}: {error_text[-1500:]}")

    try:
        candles = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{symbol}: downloader returned invalid JSON"
        ) from exc

    if isinstance(candles, dict):
        candles = candles.get("data", candles.get("values", []))

    if not isinstance(candles, list) or not candles:
        raise RuntimeError(
            f"{symbol}: no historical candles were returned"
        )

    rows = []

    for candle in candles:
        timestamp = candle.get("timestamp", candle.get("time"))
        if timestamp is None:
            continue

        if isinstance(timestamp, (int, float)):
            timestamp = pd.to_datetime(
                timestamp,
                unit="ms",
                utc=True,
            )
        else:
            timestamp = pd.to_datetime(
                timestamp,
                utc=True,
                errors="coerce",
            )

        if pd.isna(timestamp):
            continue

        rows.append({
            "time": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "open": candle.get("open"),
            "high": candle.get("high"),
            "low": candle.get("low"),
            "close": candle.get("close"),
            "volume": candle.get("volume", 0),
        })

    frame = pd.DataFrame(rows)

    if frame.empty:
        raise RuntimeError(f"{symbol}: downloaded data was empty")

    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(
            frame[column],
            errors="coerce",
        )

    frame = frame.dropna(
        subset=["time", "open", "high", "low", "close"]
    )
    frame = frame.drop_duplicates("time")
    frame = frame.sort_values("time")

    if len(frame) < 300:
        raise RuntimeError(
            f"{symbol}: only {len(frame)} candles received"
        )

    frame.to_csv(output_file, index=False)
    return len(frame)


def run_job(job_data):
    """Download required history and run a single user's backtest."""
    if not JOB_LOCK.acquire(blocking=False):
        return False

    try:
        symbol = job_data["symbol"]

        with STATE_LOCK:
            STATE.update(
                status="downloading",
                message=f"Checking one-year M15 history for {symbol}",
                error="",
                result=None,
            )

        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=365)
        # The Dukascopy downloader uses an exclusive end date.
        end = today + timedelta(days=1)

        csv_path = os.path.join(HIST_DIR, f"{symbol}_M15.csv")
        needs_download = True

        if os.path.exists(csv_path):
            try:
                cached = pd.read_csv(csv_path, usecols=["time"])
                cached["time"] = pd.to_datetime(
                    cached["time"],
                    utc=True,
                    errors="coerce",
                )
                cached = cached.dropna()

                if not cached.empty:
                    first_day = cached["time"].min().date()
                    last_day = cached["time"].max().date()

                    if (
                        first_day <= start + timedelta(days=3)
                        and last_day >= today - timedelta(days=5)
                    ):
                        needs_download = False
            except Exception:
                needs_download = True

        if needs_download:
            with STATE_LOCK:
                STATE.update(
                    status="downloading",
                    message=f"Downloading one year of M15 data for {symbol}",
                )

            count = download_history(
                symbol,
                start.isoformat(),
                end.isoformat(),
            )
            print(f"Dukascopy: downloaded {count} candles for {symbol}")

        with STATE_LOCK:
            STATE.update(
                status="backtesting",
                message="Running the strategy on historical candles",
            )

        result = run_backtest(
            csv_path=csv_path,
            symbol=symbol,
            long_rule=job_data["long_rule"],
            short_rule=job_data["short_rule"],
            stop_atr=job_data["stop_atr"],
            target_atr=job_data["target_atr"],
            starting_balance=job_data["starting_balance"],
        )

        with STATE_LOCK:
            STATE.update(
                status="done",
                message="Backtest complete",
                result=result,
                error="",
                updated_at=datetime.now(timezone.utc).isoformat(),
            )

        return True

    except Exception as exc:
        print(f"Backtest job failed: {exc}")

        with STATE_LOCK:
            STATE.update(
                status="error",
                message="Download or backtest failed",
                error=str(exc),
                result=None,
            )

        return True

    finally:
        JOB_LOCK.release()


def start_job(job_data):
    if JOB_LOCK.locked():
        return False

    with STATE_LOCK:
        STATE.update(
            status="queued",
            message="Backtest queued",
            error="",
            result=None,
        )

    thread = threading.Thread(
        target=run_job,
        args=(job_data,),
        daemon=True,
    )
    thread.start()
    return True


PAGE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Forex Strategy Backtester</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #152235;
      --muted: #617087;
      --line: #dfe5ee;
      --blue: #155fa0;
      --bg: #f3f6fa;
      --green: #16794f;
      --red: #b42318;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 15px/1.55 system-ui, sans-serif;
    }
    main { max-width: 1000px; margin: 30px auto; padding: 0 16px 48px; }
    h1 { margin: 0 0 4px; font-size: 29px; }
    h2 { margin: 0 0 12px; font-size: 20px; }
    p { color: var(--muted); }
    .card {
      background: white;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 20px;
      margin: 16px 0;
    }
    label { display: block; font-weight: 650; margin: 12px 0 5px; }
    input, select, textarea {
      width: 100%;
      border: 1px solid #cbd4e0;
      border-radius: 8px;
      padding: 10px;
      font: inherit;
      background: white;
    }
    textarea { min-height: 76px; font-family: ui-monospace, monospace; }
    .two { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    button {
      border: 0;
      border-radius: 9px;
      padding: 11px 17px;
      margin-top: 16px;
      background: var(--blue);
      color: white;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }
    button:disabled { opacity: .55; cursor: wait; }
    code, pre {
      background: #f1f4f8;
      border-radius: 6px;
      padding: 2px 5px;
    }
    pre {
      overflow-x: auto;
      padding: 12px;
      line-height: 1.5;
    }
    .muted { color: var(--muted); font-size: 13px; }
    .status { font-weight: 700; }
    .error { color: var(--red); white-space: pre-wrap; }
    .metrics {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 10px;
    }
    .metric {
      padding: 13px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fbfcfe;
    }
    .metric span { display: block; color: var(--muted); font-size: 13px; }
    .metric b { display: block; font-size: 22px; }
    table { width: 100%; border-collapse: collapse; }
    td, th { border-bottom: 1px solid var(--line); padding: 9px; text-align: left; }
    @media (max-width: 620px) {
      .two { grid-template-columns: 1fr; gap: 0; }
      main { margin-top: 18px; }
    }
  </style>
</head>
<body>
<main>
  <h1>Forex Strategy Backtester</h1>
  <p>
    Write entry rules using the supported expression format.
    One-year M15 history is fetched automatically when needed.
  </p>

  <section class="card">
    <h2>Your strategy</h2>
    <form id="form">
      <div class="two">
        <div>
          <label for="symbol">Forex pair</label>
          <select id="symbol" name="symbol">
            {% for symbol, label in pairs.items() %}
            <option value="{{ symbol }}">{{ symbol }} — {{ label }}</option>
            {% endfor %}
          </select>
        </div>
        <div>
          <label for="starting_balance">Starting balance ($)</label>
          <input id="starting_balance" name="starting_balance"
                 type="number" min="100" value="10000" required>
        </div>
      </div>

      <label for="long_rule">Long / Buy entry rule</label>
      <textarea id="long_rule" name="long_rule" required>cross_above(ema(close, 9), ema(close, 21))</textarea>

      <label for="short_rule">Short / Sell entry rule</label>
      <textarea id="short_rule" name="short_rule" required>cross_below(ema(close, 9), ema(close, 21))</textarea>

      <div class="two">
        <div>
          <label for="stop_atr">Stop-loss (ATR multiplier)</label>
          <input id="stop_atr" name="stop_atr" type="number"
                 min="0.1" max="20" step="0.1" value="1.5" required>
        </div>
        <div>
          <label for="target_atr">Take-profit (ATR multiplier)</label>
          <input id="target_atr" name="target_atr" type="number"
                 min="0.1" max="40" step="0.1" value="3" required>
        </div>
      </div>

      <button id="submit" type="submit">Run backtest</button>
    </form>
  </section>

  <section class="card">
    <h2>Rule examples</h2>
    <p class="muted">
      Available values: <code>open</code>, <code>high</code>,
      <code>low</code>, <code>close</code>, <code>volume</code>.
      Available indicators: <code>ema(series, period)</code>,
      <code>sma(series, period)</code>, <code>rsi(period)</code>,
      <code>atr(period)</code>.
    </p>
    <p class="muted">
      Use <code>cross_above(a, b)</code>, <code>cross_below(a, b)</code>,
      comparisons such as <code>close &gt; ema(close, 50)</code>,
      and the words <code>and</code>, <code>or</code>, <code>not</code>.
    </p>
    <pre>RSI entry:
Long:  cross_above(rsi(14), 30)
Short: cross_below(rsi(14), 70)

Price above trend and RSI confirmation:
Long:  close &gt; ema(close, 200) and rsi(14) &gt; 50
Short: close &lt; ema(close, 200) and rsi(14) &lt; 50</pre>
    <p class="muted">
      This is a rule-expression form, not a natural-language interpreter.
      It never runs Python code supplied by the user.
    </p>
  </section>

  <section class="card">
    <h2>Backtest status</h2>
    <div id="status" class="status">Ready</div>
    <div id="error" class="error"></div>
  </section>

  <section class="card">
    <h2>Result</h2>
    <div id="result" class="muted">Run a backtest to see results.</div>
  </section>

  <p class="muted">
    Historical simulation is not a promise of future results.
    Spread, commission and slippage are not included in this MVP.
    Render Free may erase downloaded data on restart or spin-down, so a
    later request can need another data download.
  </p>
</main>

<script>
const form = document.getElementById("form");
const button = document.getElementById("submit");
const statusBox = document.getElementById("status");
const errorBox = document.getElementById("error");
const resultBox = document.getElementById("result");

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&#39;"
  })[ch]);
}

function renderResult(r) {
  const o = r.out_of_sample;
  const i = r.in_sample;
  resultBox.innerHTML = `
    <p><b>${esc(r.symbol)}</b> · 15-minute candles ·
       ${esc(r.data_from)} to ${esc(r.data_to)}</p>
    <div class="metrics">
      <div class="metric"><span>Out-of-sample win rate</span><b>${o.win_rate.toFixed(1)}%</b></div>
      <div class="metric"><span>Out-of-sample trades</span><b>${o.trades}</b></div>
      <div class="metric"><span>Profit factor</span><b>${o.profit_factor == null ? "-" : o.profit_factor.toFixed(2)}</b></div>
      <div class="metric"><span>Average win / loss</span><b>${o.average_rrr == null ? "-" : o.average_rrr.toFixed(2) + ":1"}</b></div>
      <div class="metric"><span>Net result</span><b>${o.net_r.toFixed(2)}R</b></div>
      <div class="metric"><span>Maximum drawdown</span><b>${o.max_drawdown_r.toFixed(2)}R</b></div>
    </div>
    <h3>Earlier period (in-sample)</h3>
    <p>${i.trades} trades · ${i.win_rate.toFixed(1)}% wins ·
       profit factor ${i.profit_factor == null ? "-" : i.profit_factor.toFixed(2)}</p>
  `;
}

async function poll() {
  const response = await fetch("/api/state");
  const data = await response.json();

  statusBox.textContent = data.message || data.status;
  errorBox.textContent = data.error || "";

  if (data.status === "done" && data.result) {
    renderResult(data.result);
    button.disabled = false;
    return;
  }

  if (data.status === "error") {
    button.disabled = false;
    return;
  }

  if (["queued", "downloading", "backtesting"].includes(data.status)) {
    setTimeout(poll, 2500);
  } else {
    button.disabled = false;
  }
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  button.disabled = true;
  errorBox.textContent = "";
  resultBox.textContent = "Waiting for backtest...";
  statusBox.textContent = "Submitting...";

  const body = Object.fromEntries(new FormData(form).entries());

  try {
    const response = await fetch("/api/backtest", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body)
    });
    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.error || "Could not start the backtest");
    }

    poll();
  } catch (error) {
    statusBox.textContent = "Could not start";
    errorBox.textContent = error.message;
    button.disabled = false;
  }
});
</script>
</body>
</html>
"""


@app.get("/")
def home():
    return render_template_string(PAGE, pairs=PAIRS)


@app.get("/api/state")
def api_state():
    with STATE_LOCK:
        return jsonify(STATE)


@app.post("/api/backtest")
def api_backtest():
    payload = request.get_json(silent=True) or {}

    symbol = str(payload.get("symbol", "")).upper()
    if symbol not in PAIRS:
        return jsonify({"error": "Choose one of the listed Forex pairs."}), 400

    long_rule = str(payload.get("long_rule", "")).strip()
    short_rule = str(payload.get("short_rule", "")).strip()

    if not long_rule or not short_rule:
        return jsonify({"error": "Both entry rules are required."}), 400

    try:
        stop_atr = float(payload.get("stop_atr", 1.5))
        target_atr = float(payload.get("target_atr", 3.0))
        starting_balance = float(payload.get("starting_balance", 10000))
    except (TypeError, ValueError):
        return jsonify({"error": "Numeric settings are invalid."}), 400

    if not 0.1 <= stop_atr <= 20:
        return jsonify({"error": "Stop ATR must be between 0.1 and 20."}), 400

    if not 0.1 <= target_atr <= 40:
        return jsonify({"error": "Target ATR must be between 0.1 and 40."}), 400

    if not 100 <= starting_balance <= 100000000:
        return jsonify({"error": "Starting balance must be between 100 and 100,000,000."}), 400

    job_data = {
        "symbol": symbol,
        "long_rule": long_rule,
        "short_rule": short_rule,
        "stop_atr": stop_atr,
        "target_atr": target_atr,
        "starting_balance": starting_balance,
    }

    if not start_job(job_data):
        return jsonify({
            "error": "A backtest is already running. Wait for it to finish."
        }), 409

    return jsonify({"status": "queued"}), 202


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
)
    
