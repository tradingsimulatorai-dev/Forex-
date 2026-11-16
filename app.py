import glob
import json
import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone

import pandas as pd
from flask import Flask, jsonify, request

from backtest import run_all


app = Flask(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "data")
HIST_DIR = os.path.join(DATA_DIR, "hist")
os.makedirs(HIST_DIR, exist_ok=True)

# Six major pairs. Change this list if you want different pairs.
PAIRS = [
    "EURUSD",
    "GBPUSD",
    "AUDUSD",
    "USDCHF",
    "USDCAD",
    "USDJPY",
]

SL_MULT = 1.5
TP_MULT = 3.0

STATE = {
    "status": "starting",
    "message": "Preparing historical data",
    "results": [],
    "error": "",
    "updated_at": None,
}
LOCK = threading.Lock()
JOB_LOCK = threading.Lock()


# Calls the dukascopy-node package installed through package.json.
NODE_DOWNLOAD_SCRIPT = r"""
const { getHistoricalRates } = require("dukascopy-node");

async function main() {
  const [instrument, from, to] = process.argv.slice(-3);

  const data = await getHistoricalRates({
    instrument: instrument.toLowerCase(),
    dates: { from, to },
    timeframe: "m15",
    format: "json"
  });

  process.stdout.write(JSON.stringify(data));
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
"""


def download_pair(pair, start_date, end_date):
    """Download a pair's M15 data and save it in app.py's CSV format."""
    output_path = os.path.join(HIST_DIR, f"{pair}_M15.csv")

    completed = subprocess.run(
        [
            "node",
            "-e",
            NODE_DOWNLOAD_SCRIPT,
            pair.lower(),
            start_date,
            end_date,
        ],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )

    if completed.returncode != 0:
        message = completed.stderr.strip() or "Dukascopy download failed"
        raise RuntimeError(f"{pair}: {message[-1500:]}")

    try:
        candles = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{pair}: downloader returned invalid JSON") from exc

    if isinstance(candles, dict):
        # Accommodate libraries that wrap the candle list in a result object.
        candles = candles.get("data", candles.get("values", []))

    if not isinstance(candles, list) or not candles:
        raise RuntimeError(f"{pair}: no candles returned for the requested dates")

    rows = []
    for candle in candles:
        timestamp = candle.get("timestamp", candle.get("time"))
        if timestamp is None:
            continue

        # Dukascopy-node returns timestamps as Unix milliseconds.
        if isinstance(timestamp, (int, float)):
            dt = pd.to_datetime(timestamp, unit="ms", utc=True)
        else:
            dt = pd.to_datetime(timestamp, utc=True, errors="coerce")

        if pd.isna(dt):
            continue

        rows.append({
            "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "open": candle.get("open"),
            "high": candle.get("high"),
            "low": candle.get("low"),
            "close": candle.get("close"),
            "volume": candle.get("volume", 0),
        })

    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError(f"{pair}: downloaded data contains no usable candles")

    for column in ["open", "high", "low", "close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame = frame.dropna(subset=["time", "open", "high", "low", "close"])
    frame = frame.drop_duplicates(subset=["time"]).sort_values("time")

    if len(frame) < 300:
        raise RuntimeError(
            f"{pair}: only {len(frame)} usable candles received; "
            "not enough for the backtest"
        )

    frame.to_csv(output_path, index=False)
    return len(frame)


def fetch_and_backtest(force_download=True):
    """Download a rolling year of history, then run the backtest."""
    if not JOB_LOCK.acquire(blocking=False):
        return

    try:
        with LOCK:
            STATE.update(
                status="downloading",
                message="Downloading one year of M15 data for six pairs",
                error="",
            )

        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=365)
        start_text = start.isoformat()
        end_text = end.isoformat()

        # Download sequentially to avoid overloading the free Render instance.
        for pair in PAIRS:
            path = os.path.join(HIST_DIR, f"{pair}_M15.csv")

            if not force_download and os.path.exists(path):
                continue

            with LOCK:
                STATE["message"] = f"Downloading {pair} M15 history"

            count = download_pair(pair, start_text, end_text)
            print(f"Downloaded {count} candles for {pair}")

        with LOCK:
            STATE.update(
                status="backtesting",
                message="Running backtests",
            )

        results = run_all(HIST_DIR, SL_MULT, TP_MULT)

        with LOCK:
            STATE.update(
                status="done",
                message="Download and backtest complete",
                results=results,
                error="",
                updated_at=datetime.now(timezone.utc).isoformat(),
            )

    except Exception as exc:
        print(f"Data/backtest job failed: {exc}")
        with LOCK:
            STATE.update(
                status="error",
                message="Data download or backtest failed",
                error=str(exc),
            )
    finally:
        JOB_LOCK.release()


def start_job(force_download=True):
    thread = threading.Thread(
        target=fetch_and_backtest,
        args=(force_download,),
        daemon=True,
    )
    thread.start()


@app.get("/")
def home():
    return """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Forex Strategy Lab</title>
      <style>
        body {
          max-width: 1000px; margin: 32px auto; padding: 0 16px;
          font: 16px system-ui, sans-serif; color: #172033;
          background: #f4f6fa;
        }
        h1 { color: #123557; }
        .box {
          background: white; border: 1px solid #dce2eb;
          border-radius: 12px; padding: 18px; margin: 16px 0;
        }
        button {
          border: 0; border-radius: 8px; padding: 10px 16px;
          color: white; background: #1261a0; font-size: 15px;
        }
        table { width: 100%; border-collapse: collapse; background: white; }
        th, td { text-align: left; padding: 10px; border-bottom: 1px solid #e5e9f0; }
        .small { color: #5d687a; font-size: 14px; }
        .error { color: #b42318; }
      </style>
    </head>
    <body>
      <h1>Forex Strategy Lab</h1>
      <p class="small">
        Dukascopy M15 historical data; six currency pairs; rolling one-year test.
        Example strategies only, not financial advice.
      </p>
      <div class="box">
        <div id="status">Loading status...</div>
        <p><button onclick="runAgain()">Download again and run backtest</button></p>
      </div>
      <div class="box">
        <h2>Results</h2>
        <div id="results">Waiting for results...</div>
      </div>
      <script>
        async function refresh() {
          try {
            const response = await fetch("/state");
            const data = await response.json();
            const status = document.getElementById("status");
            status.innerHTML =
              "<b>Status:</b> " + data.status +
              "<br><b>Progress:</b> " + data.message +
              (data.updated_at ? "<br><b>Updated:</b> " + data.updated_at : "") +
              (data.error ? '<br><span class="error"><b>Error:</b> ' + data.error + "</span>" : "");

            const rows = data.results || [];
            if (!rows.length) {
              document.getElementById("results").textContent =
                data.status === "error" ? "No results. Check the error above." :
                "Data download/backtest is running or has not completed yet.";
              return;
            }

            let html = "<table><thead><tr>" +
              "<th>Pair</th><th>Strategy</th><th>OOS trades</th>" +
              "<th>Win rate</th><th>Profit factor</th><th>Total R</th>" +
              "</tr></thead><tbody>";

            for (const item of rows) {
              const o = item.oos || {};
              html += "<tr><td>" + item.sym + "</td>" +
                "<td>" + item.strat + "</td>" +
                "<td>" + (o.n || 0) + "</td>" +
                "<td>" + Number(o.wr || 0).toFixed(1) + "%</td>" +
                "<td>" + (o.pf == null ? "-" : Number(o.pf).toFixed(2)) + "</td>" +
                "<td>" + Number(o.tot || 0).toFixed(2) + "R</td></tr>";
            }

            document.getElementById("results").innerHTML =
              html + "</tbody></table>";
          } catch (error) {
            document.getElementById("status").textContent =
              "Could not reach the app yet. Render may be waking it up.";
          }
        }

        async function runAgain() {
          await fetch("/run", { method: "POST" });
          refresh();
        }

        refresh();
        setInterval(refresh, 5000);
      </script>
    </body>
    </html>
    """


@app.get("/state")
def state():
    with LOCK:
        return jsonify(STATE)


@app.post("/run")
def run():
    # Starts a fresh download and backtest. This is intended for testing.
    start_job(force_download=True)
    return jsonify({"started": True, "status": "downloading"}), 202


# On a fresh Render process, download again because Free filesystem data
# disappears after spin-down, restart, or redeploy.
start_job(force_download=True)
    
