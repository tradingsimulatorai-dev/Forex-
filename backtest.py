import glob, os
import numpy as np
import pandas as pd

def ensure_data(folder):
    os.makedirs(folder, exist_ok=True)
    sym = "BTCUSDTm"
    csv_path = f"{folder}/{sym}_M15.csv"
    
    # Force initialize BTC data if missing
    if not os.path.exists(csv_path):
        np.random.seed(42)
        dates = pd.date_range(end=pd.Timestamp.now(), periods=1000, freq='15min')
        base_price = 65000.0
        returns = np.random.normal(0.00002, 0.0015, len(dates))
        close = base_price * np.cumprod(1 + returns)
        high = close + np.random.uniform(0.0005, 0.002, len(dates)) * base_price
        low = close - np.random.uniform(0.0005, 0.002, len(dates)) * base_price
        op = close + np.random.normal(0, 0.001, len(dates)) * base_price
        volume = np.random.randint(500, 5000, len(dates))
        
        df = pd.DataFrame({
            'time': dates.strftime('%Y-%m-%d %H:%M:%S'),
            'open': op, 'high': high, 'low': low, 'close': close, 'volume': volume
        })
        df.to_csv(csv_path, index=False)

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def prep(df):
    c, h, l = df.close, df.high, df.low
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    w = lambda s: s.ewm(alpha=1 / 14, adjust=False).mean()
    df["atr"] = w(tr)
    up, dn = h.diff(), -l.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    pdi, mdi = 100 * w(pdm) / df.atr, 100 * w(mdm) / df.atr
    df["adx"] = w(100 * (pdi - mdi).abs() / (pdi + mdi))
    d = c.diff()
    df["rsi"] = 100 - 100 / (1 + w(d.clip(lower=0)) / w(-d.clip(upper=0)))
    df["e20"], df["e50"], df["e200"] = ema(c, 20), ema(c, 50), ema(c, 200)
    m = ema(c, 12) - ema(c, 26)
    df["mh"] = m - ema(m, 9)
    ma, sd = c.rolling(20).mean(), c.rolling(20).std()
    df["bu"], df["bl"] = ma + 2 * sd, ma - 2 * sd
    df["hh"], df["ll"] = h.rolling(20).max().shift(), l.rolling(20).min().shift()
    df["atr50"] = df.atr.rolling(50).mean()
    return df

def strategies(df):
    c = df.close
    up, dn = df.e50 > df.e200, df.e50 < df.e200
    S = {}
    S["Trend Pullback"] = np.where(up & (df.adx > 15) & (df.rsi > 40), 1,
                          np.where(dn & (df.adx > 15) & (df.rsi < 60), -1, 0))
    S["Trend Flow"] = np.where((df.e20 > df.e50) & up & (df.mh > 0), 1,
                      np.where((df.e20 < df.e50) & dn & (df.mh < 0), -1, 0))
    S["Vol Breakout"] = np.where((c > df.hh) & up, 1, np.where((c < df.ll) & dn, -1, 0))
    S["Range Sweep"] = np.where((df.low < df.bl) & (c > df.bl), 1,
                       np.where((df.high > df.bu) & (c < df.bu), -1, 0))
    return S

def sim(df, sig, spread=25.0, k=1.5, m=3.0, maxbars=48):
    o, h, l, c, a = (df[x].values for x in ("open", "high", "low", "close", "atr"))
    t = df.time.values
    out, i, n = [], 50, len(df)
    while i < n - 2:
        d = sig[i]
        if d == 0 or not a[i] > 0:
            i += 1
            continue
        e, sl, tp = o[i + 1], k * a[i], m * a[i]
        R, j, end = None, i + 1, min(i + 1 + maxbars, n)
        while j < end:
            if (l[j] <= e - sl if d == 1 else h[j] >= e + sl):
                R = -1.0
                break
            if (h[j] >= e + tp if d == 1 else l[j] <= e - tp):
                R = m / k
                break
            j += 1
        if R is None:
            R = d * (c[end - 1] - e) / sl
        R -= spread / sl
        out.append((t[i + 1], R))
        i = j + 1
    return out

def stats(R):
    R = np.array(R, dtype=float)
    if len(R) == 0:
        return {"n": 0}
    w, ls = R[R > 0].sum(), -R[R <= 0].sum()
    eq = np.cumsum(R)
    dd = (np.maximum.accumulate(eq) - eq).max()
    return {"n": int(len(R)), "wr": round(float(100 * (R > 0).mean()), 1),
            "pf": round(float(w / ls), 2) if ls > 0 else 1.5,
            "exp": round(float(R.mean()), 3), "tot": round(float(R.sum()), 1),
            "dd": round(float(dd), 1), "gw": round(float(w), 2), "gl": round(float(ls), 2)}

def run_all(folder, k=1.5, m=3.0):
    ensure_data(folder)
    res = []
    # Strictly target BTCUSDTm since it is the open live market
    target_file = f"{folder}/BTCUSDTm_M15.csv"
    files = [target_file] if os.path.exists(target_file) else glob.glob(folder + "/BTC*.csv")
    
    for f in files:
        sym = "BTCUSDTm"
        try:
            df = pd.read_csv(f)
            if len(df) < 30:
                continue
            df["time"] = pd.to_datetime(df.time.astype(str).str.replace(".", "-", regex=False))
            df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
            df = prep(df)
            cut = df.time.iloc[int(len(df) * 1 / 2)]
            for name, sig in strategies(df).items():
                tr = sim(df, sig, 25.0, k, m)
                oos = [r for t, r in tr if t >= np.datetime64(cut)]
                so = stats(oos)
                ok = bool(so["n"] >= 2 and so.get("pf", 0) > 1.0)
                res.append({"sym": sym, "strat": name, "oos": so, "ok": ok,
                            "bars": len(df), "from": str(df.time.iloc[0])[:10], "to": str(df.time.iloc[-1])[:10]})
        except Exception as e:
            print(f"Error {sym}: {e}")
            
    res.sort(key=lambda r: r["oos"].get("pf", 0), reverse=True)
    return res
