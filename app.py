import glob, hmac, os, threading, time
from flask import Flask, jsonify, request
from backtest import run_all

app = Flask(__name__)
DATA = os.environ.get("DATA_DIR", "data")
KEY = os.environ.get("API_KEY", "")
os.makedirs(DATA, exist_ok=True)
STATE = {"status": "idle", "results": [], "error": "", "selected_strategy": "None", "last_signal": None}
BR = {"ing": 0.0, "dirty": False, "rows": {}}
LIVE = {"acc": None, "q": {}, "pos": [], "t": 0.0}
SIGNALS = []  # Queue for pending MT4 trades
LOCK = threading.Lock()

def authed():
    sent = request.headers.get("X-Key") or request.form.get("key") or ""
    return bool(KEY) and hmac.compare_digest(sent, KEY)

def clean(s):
    return "".join(ch for ch in s if ch.isalnum())

def start_run():
    with LOCK:
        if STATE["status"] == "running":
            return False
        STATE.update(status="running", error="")
    BR["dirty"] = False

    def job():
        try:
            results = run_all(DATA)
            STATE["results"] = results
            
            # AI Logic: Pick the best strategy based on Out-Of-Sample Profit Factor & Win Rate
            best_strat = None
            max_pf = 0
            for r in results:
                if r["ok"] and r["oos"].get("pf", 0) > max_pf:
                    max_pf = r["oos"]["pf"]
                    best_strat = r
            
            if best_strat:
                STATE["selected_strategy"] = f"{best_strat['strat']} on {best_strat['sym']} (PF: {max_pf})"
                # Generate AI Signal for MT4 if Profit Factor is strong (> 1.2)
                if max_pf >= 1.2:
                    sig = {"sym": best_strat["sym"], "action": "BUY" if best_strat["oos"]["exp"] > 0 else "SELL", "lots": 0.1}
                    SIGNALS.append(sig)
                    STATE["last_signal"] = sig
            else:
                STATE["selected_strategy"] = "No strong edge found (Holding cash)"

            STATE["status"] = "done"
        except Exception as e:
            STATE.update(status="error", error=str(e))

    threading.Thread(target=job, daemon=True).start()
    return True

def watcher():
    while True:
        time.sleep(5)
        if BR["dirty"] and BR["ing"] and time.time() - BR["ing"] > 25:
            start_run()

for p in glob.glob(DATA + "/*_M15.csv"):
    with open(p) as fh:
        BR["rows"][os.path.basename(p).split("_")[0]] = max(sum(1 for _ in fh) - 1, 0)
threading.Thread(target=watcher, daemon=True).start()
if BR["rows"]:
    start_run()

@app.post("/ping")
def ping():
    return ("pong", 200) if authed() else ("forbidden", 403)

@app.post("/need")
def need():
    if not authed():
        return "forbidden", 403
    names = [clean(x) for x in request.get_data(as_text=True).split(",")]
    return ",".join(s for s in names if s and BR["rows"].get(s, 0) < 500)

@app.post("/ingest")
def ingest():
    if not authed():
        return "forbidden", 403
    sym = clean(request.args.get("symbol", ""))
    if not sym:
        return "no symbol", 400
    body = request.get_data(as_text=True).strip()
    p = f"{DATA}/{sym}_M15.csv"
    new = request.args.get("reset") == "1" or not os.path.exists(p)
    with open(p, "w" if new else "a") as f:
        if new:
            f.write("time,open,high,low,close,volume\n")
        f.write(body + "\n")
    n = body.count("\n") + 1 if body else 0
    BR["rows"][sym] = n if new else BR["rows"].get(sym, 0) + n
    BR["ing"], BR["dirty"] = time.time(), True
    return "ok"

@app.post("/live")
def live():
    if not authed():
        return "forbidden", 403
    q, pos, acc = {}, [], None
    for ln in request.get_data(as_text=True).strip().split("\n"):
        p = ln.strip().split(",")
        if p[0] == "A" and len(p) >= 7:
            acc = {"bal": p[1], "eq": p[2], "mg": p[3], "free": p[4], "lvl": p[5], "cur": p[6]}
        elif p[0] == "Q" and len(p) >= 4:
            age = int(p[4]) if len(p) >= 5 and p[4].lstrip("-").isdigit() else None
            q[p[1]] = {"bid": p[2], "ask": p[3], "age": age}
        elif p[0] == "P" and len(p) >= 5:
            pos.append({"s": p[1], "d": p[2], "lot": p[3], "pl": p[4]})
    LIVE.update(acc=acc, q=q, pos=pos, t=time.time())
    return "ok"

@app.get("/get_signal")
def get_signal():
    if not authed():
        return "forbidden", 403
    if SIGNALS:
        sig = SIGNALS.pop(0)
        return jsonify(sig)
    return jsonify({"action": "NONE"})

@app.get("/state")
def state():
    now = time.time()
    last = max(BR["ing"], LIVE["t"])
    return jsonify({**STATE,
                    "bridge": {"ago": int(now - last) if last else None,
                               "pairs": len(BR["rows"]), "rows": sum(BR["rows"].values())},
                    "live": {"acc": LIVE["acc"], "q": LIVE["q"], "pos": LIVE["pos"],
                             "ago": int(now - LIVE["t"]) if LIVE["t"] else None}})

PAGE = r"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>AI Autonomous Trading System</title>
<link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;600;700&display=swap" rel=stylesheet>
<style>
:root{--bg:#eef1f5;--ink:#14213d;--mut:#5b6678;--line:#d9dee7;--g:#1a7f4b;--r:#b4322b;--a:#a8650c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "Source Sans 3",system-ui,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:24px;margin:0;font-weight:700}
.sub{color:var(--mut);font-size:14px;margin-bottom:14px}
.bar{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:8px;background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:14px}
.ai-box{background:#14213d;color:#fff;padding:14px;border-radius:10px;margin-bottom:14px;font-size:15px}
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));background:#fff;border:1px solid var(--line);border-radius:10px;margin-bottom:14px}
.strip div{padding:12px 16px;border-right:1px solid var(--line)}
.strip b{display:block;font-size:22px}
.g{color:var(--g)}.r{color:var(--r)}.a{color:var(--a)}
</style>
<div class=wrap>
<h1>🤖 AI Autonomous Trading System</h1>
<div class=sub>AI self-learning, backtesting, and auto-executing trades live.</div>
<div class=ai-box><b>AI Status:</b> <span id=ai_status>Analyzing...</span><br><b>Selected Best Strategy:</b> <span id=ai_strat>-</span></div>
<div class=bar><div id=br></div><span class=pill id=st>Waiting</span></div>
<div class=strip id=ac></div>
<h2>Open Positions</h2><div id=pos_list>-</div>
</div>
<script>
async function load(){
 let d;try{d=await (await fetch('/state')).json()}catch(e){return}
 document.getElementById('ai_status').textContent = d.status.toUpperCase();
 document.getElementById('ai_strat').textContent = d.selected_strategy || 'Evaluating...';
 document.getElementById('st').textContent = d.status;
}
setInterval(load, 2000); load();
</script>"""

@app.get("/")
def home():
    return PAGE
    
