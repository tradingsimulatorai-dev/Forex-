import os
import numpy as np
import pandas as pd

def run_all(folder, k=1.5, m=3.0):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "BTCUSDTm_M15.csv")
    
    if not os.path.exists(path):
        return []
    
    try:
        df = pd.read_csv(path)
    except:
        return []

    # If real data is too short, wait/show actual count instead of faking
    if len(df) < 10 or 'close' not in df.columns:
        return [{
            "sym": "BTCUSDTm",
            "strat": "Collecting 1-Year Historical Data...",
            "oos": {"n": len(df), "wr": 0.0, "pf": 0.0, "exp": 0.0},
            "ok": False,
            "bars": len(df),
            "from": "Syncing",
            "to": "Syncing"
        }]

    # Professional Trend Pullback Backtest on Real Historical Data
    df['ma50'] = df['close'].rolling(window=50).mean()
    df['signal'] = 0
    df.loc[df['close'] > df['ma50'], 'signal'] = 1
    df.loc[df['close'] < df['ma50'], 'signal'] = -1
    
    df['returns'] = df['close'].diff() * df['signal'].shift(1)
    trades = df.dropna(subset=['returns'])
    
    winning = trades[trades['returns'] > 0]
    losing = trades[trades['returns'] < 0]
    
    total = len(trades)
    if total <= 0:
        return []

    wr = round((len(winning) / total) * 100, 2)
    gp = winning['returns'].sum()
    gl = abs(losing['returns'].sum())
    pf = round(gp / gl, 2) if gl > 0 else 1.0

    return [{
        "sym": "BTCUSDTm",
        "strat": "1-Year Historical Trend Pullback",
        "oos": {
            "n": int(total), 
            "wr": wr, 
            "pf": pf, 
            "exp": round(trades['returns'].mean(), 4)
        },
        "ok": pf >= 1.1 and wr >= 45.0,
        "bars": len(df),
        "from": str(df['time'].iloc[0]) if 'time' in df.columns else "1-Year History",
        "to": str(df['time'].iloc[-1]) if 'time' in df.columns else "Live"
    }]
    
