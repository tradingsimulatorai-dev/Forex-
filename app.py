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
SIGNALS = []  # Pending AI trade signals for MT4
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
            
            # AI Autonomous Decision Making
            best_strat = None
            max_pf = 0
            for r in results:
                if r["ok"] and r["oos"].get("pf", 0) > max_pf:
                    max_pf = r["oos"]["pf"]
                    best_strat = r
            
            if best_strat:
                STATE["selected_strategy"] = f"{best_strat['strat']} on {best_strat['sym']} (PF: {max_pf})"
                # If Profit Factor is strong, trigger autonomous trade signal
                if max_pf >= 1.2:
                    action_type = "BUY" if best_strat["oos"]["exp"] > 0 else "SELL"
                    sig = {"sym": best_strat["sym"], "action": action_type, "lots": 0.1}
                    SIGNALS.append(sig)
                    STATE["last_signal"] = sig
            else:
                STATE["selected_strategy"] = "No strong edge found (Waiting for better market conditions)"

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
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "Source Sans 3",system-ui,sans-serif;font-variant-numeric:tabular-nums}
.wrap{max-width:1100px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:24px;margin:0;font-weight:700}
.sub{color:var(--mut);font-size:14px;margin-bottom:14px}
.bar{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:8px;background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:14px}
.ai-box{background:#14213d;color:#fff;padding:16px;border-radius:10px;margin-bottom:14px;font-size:15px;box-shadow:0 4px 6px rgba(0,0,0,0.1)}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:8px;background:var(--r)}
.dot.on{background:var(--g)}.dot.warn{background:var(--a)}
.pill{border:1px solid var(--line);border-radius:99px;padding:2px 12px;font-size:13px;color:var(--mut)}
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));background:#fff;border:1px solid var(--line);border-radius:10px;margin-bottom:14px}
.strip div{padding:12px 16px;border-right:1px solid var(--line)}
.strip b{display:block;font-size:22px}
.strip span{color:var(--mut);font-size:13px}
.tb{overflow-x:auto;background:#fff;border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;min-width:420px}
#t1,#t2{min-width:660px}
th{font-size:13px;color:var(--mut);font-weight:600;text-align:right;padding:9px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid #edf0f4;text-align:right;white-space:nowrap}
th:first-child,td:first-child,#t2 th:nth-child(2),td.l{text-align:left}
.g{color:var(--g)}.r{color:var(--r)}.a{color:var(--a)}
.b{display:inline-block;padding:1px 9px;border-radius:99px;font-size:12px;font-weight:600;border:1px solid currentColor}
</style>
<div class=wrap>
<h1>🤖 AI Autonomous Trading System</h1>
<div class=sub>Live data from MT4. AI runs quantitative backtests and executes trades autonomously.</div>
<div class=ai-box>
  <b>AI Status:</b> <span id=ai_status>-</span><br>
  <b>AI Selected Strategy & Pair:</b> <span id=ai_strat>-</span>
</div>
<div class=bar><div id=br></div><span class=pill id=st>Waiting</span></div>
<div class=strip id=ac></div>
<h2>Open positions</h2><div class=tb><table id=t3></table></div>
<h2>Live prices</h2><p class=cap id=mk></p>
<div class=tb><table id=t4></table></div>
<h2>Strategies across all pairs</h2>
<div class=tb><table id=t1></table></div>
<h2>Each pair</h2>
<div class=tb><table id=t2></table></div>
</div>
<script>
const $=i=>document.getElementById(i);
const N=v=>Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
async function load(){
 let d;try{d=await (await fetch('/state')).json()}catch(e){return}
 $('ai_status').textContent = d.status.toUpperCase();
 $('ai_strat').textContent = d.selected_strategy || 'Analyzing...';
 $('st').textContent = d.status;
 const R=d.results;
 if(d.bridge.ago!=null){
  $('br').innerHTML='<span class="dot on"></span>MT4 Connected · '+d.bridge.pairs+' pairs · '+d.bridge.rows.toLocaleString()+' candles';
 }
 const l=d.live;
 if(l.acc){
  const bal=+l.acc.bal,eq=+l.acc.eq,f=eq-bal;
  $('ac').innerHTML='<div><b>'+N(bal)+'</b><span>Balance</span></div><div><b>'+N(eq)+'</b><span>Equity</span></div><div><b class="'+(f>=0?'g':'r')+'">'+(f>=0?'+':'')+N(f)+'</b><span>Floating P/L</span></div>';
  let p='<tr><th>Pair<th>Side<th>Lots<th>Profit</tr>';
  p+=l.pos.length?l.pos.map(x=>'<tr><td>'+x.s+'<td>'+x.d+'<td>'+x.lot+'<td class="'+(+x.pl>=0?'g':'r')+'">'+N(x.pl)+'</tr>').join(''):'<tr><td colspan=4>No open positions</tr>';
  $('t3').innerHTML=p;
 }
}
setInterval(load, 2000); load();
</script>"""

@app.get("/")
def home():
    return PAGE
            
