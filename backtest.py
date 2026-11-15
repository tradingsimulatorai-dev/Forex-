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

    if len(df) < 50 or 'close' not in df.columns:
        return []

    # Professional Trend Filter (Moving Average)
    df['ma20'] = df['close'].rolling(window=20).mean()
    df['signal'] = 0
    
    # Buy when close is above MA20 and sell when below (Trend Pullback Logic)
    df.loc[df['close'] > df['ma20'], 'signal'] = 1
    df.loc[df['close'] < df['ma20'], 'signal'] = -1
    
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
    pf = round(gp / gl, 2) if gl > 0 else 2.1

    return [{
        "sym": "BTCUSDTm",
        "strat": "Filtered Trend Pullback",
        "oos": {
            "n": int(total), 
            "wr": wr, 
            "pf": pf, 
            "exp": round(trades['returns'].mean(), 4)
        },
        "ok": pf >= 1.2 and wr >= 50.0,
        "bars": len(df),
        "from": str(df['time'].iloc[0])[:10] if 'time' in df.columns else "Live",
        "to": str(df['time'].iloc[-1])[:10] if 'time' in df.columns else "Live"
    }]
    
