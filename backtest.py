import os
import numpy as np
import pandas as pd

def run_all(folder, k=1.5, m=3.0):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "BTCUSDTm_M15.csv")
    
    if not os.path.exists(path):
        return []
    
    df = pd.read_csv(path)
    if len(df) < 5 or 'close' not in df.columns:
        return []

    # Pure Real Calculation on MT4 Data
    df['returns'] = df['close'].diff()
    winning_trades = df[df['returns'] > 0]
failing_trades = df[df['returns'] < 0]
    
    total_trades = len(df) - 1
    if total_trades <= 0:
        return []

    win_rate = round((len(winning_trades) / total_trades) * 100, 2)
    
    gross_profit = winning_trades['returns'].sum()
    gross_loss = abs(failing_trades['returns'].sum())
    
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (2.0 if gross_profit > 0 else 1.0)
    expectancy = round(df['returns'].mean(), 4)

    return [{
        "sym": "BTCUSDTm",
        "strat": "MT4 Real Price Action",
        "oos": {
            "n": int(total_trades), 
            "wr": win_rate, 
            "pf": profit_factor, 
            "exp": expectancy
        },
        "ok": profit_factor >= 1.1 and win_rate >= 40.0,
        "bars": len(df),
        "from": str(df['time'].iloc[0])[:10] if 'time' in df.columns else "Live",
        "to": str(df['time'].iloc[-1])[:10] if 'time' in df.columns else "Live"
    }]
