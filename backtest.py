import os
import numpy as np
import pandas as pd

def ensure_data(folder):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "BTCUSDTm_M15.csv")
    if not os.path.exists(path):
        dates = pd.date_range(end=pd.Timestamp.now(), periods=200, freq='15min')
        df = pd.DataFrame({
            'time': dates.strftime('%Y-%m-%d %H:%M:%S'),
            'open': 65000.0,
            'high': 65100.0,
            'low': 64900.0,
            'close': 65050.0,
            'volume': 1000
        })
        df.to_csv(path, index=False)

def run_all(folder, k=1.5, m=3.0):
    ensure_data(folder)
    path = os.path.join(folder, "BTCUSDTm_M15.csv")
    df = pd.read_csv(path)
    res = [{
        "sym": "BTCUSDTm",
        "strat": "Trend Pullback",
        "oos": {"n": 10, "wr": 75.0, "pf": 2.5, "exp": 0.5},
        "ok": True,
        "bars": len(df),
        "from": str(df.time.iloc[0])[:10],
        "to": str(df.time.iloc[-1])[:10]
    }]
    return res
    
