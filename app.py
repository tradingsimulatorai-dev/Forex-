import glob, hmac, os, threading, time
from datetime import datetime, timedelta
import requests
from flask import Flask, jsonify, request
from backtest import run_all

app = Flask(__name__)
DATA = os.environ.get("DATA_DIR", "data")
HIST = DATA + "/hist"
KEY = os.environ.get("API_KEY", "")
TD_KEY = os.environ.get("TWELVE_KEY", "")
os.makedirs(HIST, exist_ok=True)
SYMS = {"EURUSD": "EUR/USD", "GBPUSD": "GBP/USD", "USDJPY": "USD/JPY", "USDCHF": "USD/CHF",
        "USDCAD": "USD/CAD", "AUDUSD": "AUD/USD", "NZDUSD": "NZD/USD", "EURJPY": "EUR/JPY",
        "GBPJPY": "GBP/JPY", "XAUUSD": "XAU/USD", "BTCUSD": "BTC/USD"}
STATE = {"status": "idle", "results": [], "error": ""}
FETCH = {"status": "idle", "done": 0, "total": len(SYMS), "now": "", "failed": []}
BR = {"ing": 0.0, "rows": {}}
LIVE = {"acc": None, "q": {}, "pos": [], "t": 0.0}
LOCK = threading.Lock()


def authed():
    sent = request.headers.get("X-Key") or request.form.get("key") or ""
    return bool(KEY) and hmac.compare_digest(sent, KEY)


def clean(s):
    return "".join(ch for ch in s if ch.isalnum())


def start_run():
    if not glob.glob(HIST + "/*.csv"):
        return False
    with LOCK:
        if STATE["status"] == "running":
            return False
        STATE.update(status="running", error="")

    def job():
        try:
            STATE["results"] = run_all(HIST)
            STATE["status"] = "done"
        except Exception as e:
            STATE.update(status="error", error=str(e))

    threading.Thread(target=job, daemon=True).start()
    return True


def fetch_one(sym, code):
    rows, end = {}, None
    cutoff = (datetime.utcnow() - timedelta(days=370)).strftime("%Y-%m-%d %H:%M:%S")
    for _ in range(12):
        p = {"symbol": code, "interval": "15min", "outputsize": 5000, "timezone": "UTC",
             "order": "DESC", "apikey": TD_KEY}
        if end:
            p["end_date"] = end
        r = requests.get("https://api.twelvedata.com/time_series", params=p, timeout=90).json()
        if str(r.get("code")) == "429":  # per-minute limit: wait and retry
            time.sleep(65)
            continue
        vals = r.get("values")
        if not vals:
            raise RuntimeError(r.get("message", "no data"))
        for v in vals:
            rows[v["datetime"]] = v
        oldest = vals[-1]["datetime"]
        if len(vals) < 5000 or oldest < cutoff:
            break
        end = oldest
        time.sleep(8)
    if not rows:
        raise RuntimeError("no data")
    with open(f"{HIST}/{sym}_M15.csv", "w") as f:
        f.write("time,open,high,low,close,volume\n")
        for k in sorted(rows):
            v = rows[k]
            f.write(f'{k},{v["open"]},{v["high"]},{v["low"]},{v["close"]},{v.get("volume", 0)}\n')


def fetch_all():
    FETCH.update(status="running", done=0, now="", failed=[])
    for sym, code in SYMS.items():
        FETCH["now"] = sym
        try:
            fetch_one(sym, code)
        except Exception as e:
            FETCH["failed"].append(sym + ": " + str(e)[:90])
        FETCH["done"] += 1
        time.sleep(8)
    FETCH["status"] = "done"
    start_run()


for p in glob.glob(DATA + "/*_M15.csv"):
    with open(p) as fh:
        BR["rows"][os.path.basename(p).split("_")[0]] = max(sum(1 for _ in fh) - 1, 0)
if glob.glob(HIST + "/*.csv"):
    start_run()
elif TD_KEY:
    threading.Thread(target=fetch_all, daemon=True).start()
else:
    FETCH["status"] = "nokey"


@app.post("/ping")
def ping():
    return ("pong", 200) if authed() else ("forbidden", 403)


@app.post("/need")  # EA asks: which of my pairs do you lack?
def need():
    if not authed():
        return "forbidden", 403
    names = [clean(x) for x in request.get_data(as_text=True).split(",")]
    return ",".join(s for s in names if s and BR["rows"].get(s, 0) < 500)


@app.post("/ingest")  # live candles from MT4 (kept for live trading later)
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
    BR["ing"] = time.time()
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


@app.post("/run")
def run():
    if not authed():
        return "forbidden", 403
    return "started" if start_run() else "not started"


@app.get("/state")
def state():
    now = time.time()
    last = max(BR["ing"], LIVE["t"])
    return jsonify({**STATE, "fetch": FETCH,
                    "bridge": {"ago": int(now - last) if last else None,
                               "pairs": len(BR["rows"]), "rows": sum(BR["rows"].values())},
                    "live": {"acc": LIVE["acc"], "q": LIVE["q"], "pos": LIVE["pos"],
                             "ago": int(now - LIVE["t"]) if LIVE["t"] else None}})


PAGE = r"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Strategy research</title>
<link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;600;700&display=swap" rel=stylesheet>
<style>
:root{--bg:#eef1f5;--ink:#14213d;--mut:#5b6678;--line:#d9dee7;--g:#1a7f4b;--r:#b4322b;--a:#a8650c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "Source Sans 3",system-ui,sans-serif;font-variant-numeric:tabular-nums}
.wrap{max-width:1100px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:24px;margin:0;font-weight:700;letter-spacing:-.01em}
.sub{color:var(--mut);font-size:14px;margin-bottom:14px}
.bar{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:8px;background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:14px;font-size:14px}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:8px;background:var(--r)}
.dot.on{background:var(--g)}.dot.warn{background:var(--a)}
.dot.pulse{animation:p 1.2s ease-in-out infinite}
@keyframes p{0%,100%{box-shadow:0 0 0 0 rgba(26,127,75,.5)}50%{box-shadow:0 0 0 6px rgba(26,127,75,0)}}
.pill{border:1px solid var(--line);border-radius:99px;padding:2px 12px;font-size:13px;color:var(--mut)}
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));background:#fff;border:1px solid var(--line);border-radius:10px;margin-bottom:14px}
.strip div{padding:12px 16px;border-right:1px solid var(--line)}
.strip div:last-child{border-right:0}
.strip b{display:block;font-size:22px;line-height:1.25}
.strip span{color:var(--mut);font-size:13px}
.note{border-left:4px solid var(--a);background:#fff7e8;padding:10px 14px;margin-bottom:14px;border-radius:0 6px 6px 0;font-size:14px}
h2{font-size:17px;margin:22px 0 2px}
.cap{color:var(--mut);font-size:13px;margin:0 0 8px}
.tb{overflow-x:auto;background:#fff;border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;min-width:420px}
#t1,#t2{min-width:660px}
th{font-size:13px;color:var(--mut);font-weight:600;text-align:right;padding:9px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid #edf0f4;text-align:right;white-space:nowrap}
tr:last-child td{border-bottom:0}
th:first-child,td:first-child,#t2 th:nth-child(2),td.l{text-align:left}
td small{color:var(--mut);margin-left:6px}
.m{color:#9aa3b2}
.g{color:var(--g)}.r{color:var(--r)}.a{color:var(--a)}
.b{display:inline-block;padding:1px 9px;border-radius:99px;font-size:12px;font-weight:600;border:1px solid currentColor}
</style>
<div class=wrap>
<h1>Strategy research</h1>
<div class=sub>Live data from MT4. Backtests use one year of 15-minute history downloaded by the server.</div>
<div class=bar><div id=br></div><span class=pill id=st>Waiting for data</span></div>
<div class=strip id=ac></div>
<h2>Open positions</h2><div class=tb><table id=t3></table></div>
<h2>Live prices</h2><p class=cap id=mk></p>
<div class=tb><table id=t4></table></div>
<div id=nt style="margin-top:22px"></div><div class=strip id=sp></div>
<h2>Strategies across all pairs</h2>
<p class=cap>Results on data the strategy had not seen (last third of the period).</p>
<div class=tb><table id=t1></table></div>
<h2>Each pair</h2>
<p class=cap>The range next to win rate is a 95% range. A wide range means too few trades to trust it.</p>
<div class=tb><table id=t2></table></div>
</div>
<script>
const $=i=>document.getElementById(i);
const N=v=>Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
function ago(s){return s<60?s+' sec':s<3600?Math.floor(s/60)+' min':s<86400?Math.floor(s/3600)+' h':Math.floor(s/86400)+' d'}
function bridge(b){let c='',t='Waiting for MT4. No data received yet.';
 if(b.ago!=null){const i=' · '+b.pairs+' pairs · '+b.rows.toLocaleString()+' candles';
  if(b.ago<20){c='on pulse';t='Receiving data from MT4 now'+i}
  else if(b.ago<1200){c='on';t='MT4 connected · last data '+ago(b.ago)+' ago'+i}
  else{c='warn';t='No data from MT4 for '+ago(b.ago)+'. Check the EA in MT4.'+i}}
 $('br').innerHTML='<span class="dot '+c+'"></span>'+t}
const badge=(t,c)=>'<span class="b '+c+'">'+t+'</span>';
function live(l){const a=l.acc;
 if(!a||l.ago==null||l.ago>60){$('ac').innerHTML='<div><b>-</b><span>Waiting for account data from MT4</span></div>';$('t3').innerHTML='';$('t4').innerHTML='';$('mk').innerHTML='';return}
 const bal=+a.bal,eq=+a.eq,f=eq-bal;
 $('ac').innerHTML='<div><b>'+N(bal)+'</b><span>Balance ('+a.cur+')</span></div><div><b>'+N(eq)+'</b><span>Equity</span></div><div><b class="'+(f>=0?'g':'r')+'">'+(f>=0?'+':'')+N(f)+'</b><span>Floating P/L</span></div><div><b>'+(+a.lvl>0?(+a.lvl).toFixed(0)+'%':'-')+'</b><span>Margin level</span></div><div><b>'+N(a.free)+'</b><span>Free margin</span></div>';
 let p='<tr><th>Pair<th>Side<th>Lots<th>Profit</tr>';
 p+=l.pos.length?l.pos.map(x=>'<tr><td>'+x.s+'<td>'+x.d+'<td>'+x.lot+'<td class="'+(+x.pl>=0?'g':'r')+'">'+N(x.pl)+'</tr>').join(''):'<tr><td colspan=4>No open positions</tr>';
 $('t3').innerHTML=p;
 const keys=Object.keys(l.q),op=keys.filter(s=>l.q[s].age!=null&&l.q[s].age<300),cl=keys.filter(s=>!op.includes(s));
 $('mk').innerHTML='<b class=g>Open now ('+op.length+'):</b> '+(op.join(', ')||'none')+'<br><b class=a>Closed ('+cl.length+'):</b> '+(cl.join(', ')||'none');
 let q='<tr><th>Pair<th>Market<th>Bid<th>Ask<th>Spread (points)</tr>';
 for(const s of keys){const x=l.q[s],o=op.includes(s),dec=(x.bid.split('.')[1]||'').length,m=o?'':' class=m';
  q+='<tr><td>'+s+'<td>'+(o?badge('Open','g'):badge('Closed','a')+'<small>last tick '+(x.age!=null&&x.age<99999?ago(x.age)+' ago':'unknown')+'</small>')+'<td'+m+'>'+x.bid+'<td'+m+'>'+x.ask+'<td'+m+'>'+(o?Math.round((x.ask-x.bid)*Math.pow(10,dec)):'-')+'</tr>'}
 $('t4').innerHTML=q}
function ci(w,n){const z=1.96,p=w/n,d=1+z*z/n,c=p+z*z/(2*n),m=z*Math.sqrt(p*(1-p)/n+z*z/(4*n*n));return[Math.round((c-m)/d*100),Math.round((c+m)/d*100)]}
const cls=v=>v==null?'':v>=1.1?'g':v<1?'r':'a';
const pf=v=>v==null?'-':v.toFixed(2);
function wr(w,n){if(!n)return'-';const a=ci(w,n);return Math.round(w/n*100)+'%<small>('+a[0]+'-'+a[1]+')</small>'}
function summary(R){const m={};
 for(const r of R){const o=r.oos,i=r.is;if(!o.n)continue;
  const a=m[r.strat]=m[r.strat]||{n:0,w:0,gw:0,gl:0,igw:0,igl:0,tot:0,pairs:0};
  a.n+=o.n;a.w+=Math.round(o.wr*o.n/100);a.gw+=o.gw||0;a.gl+=o.gl||0;a.tot+=o.tot;a.pairs++;
  a.igw+=i.gw||0;a.igl+=i.gl||0}
 return Object.keys(m).map(k=>{const a=m[k];a.k=k;a.pf=a.gl?a.gw/a.gl:null;a.ipf=a.igl?a.igw/a.igl:null;return a}).sort((x,y)=>(y.pf||0)-(x.pf||0))}
async function load(){
 let d;try{d=await (await fetch('/state')).json()}catch(e){$('br').innerHTML='<span class="dot warn"></span>Server is waking up, please wait up to a minute...';return}
 const R=d.results,f=d.fetch;bridge(d.bridge);live(d.live);
 $('st').textContent=f.status=='running'?'Downloading history: '+f.done+' of '+f.total+' ('+f.now+')':f.status=='nokey'&&!R.length?'Add TWELVE_KEY in Render to download data':d.status=='running'?'Running backtest...':d.status=='error'?'Error: '+d.error:R.length?'Results ready':'Waiting for data';
 const T=R.reduce((s,r)=>s+(r.oos.n||0),0),P=R.filter(r=>r.ok).length;
 let fr='',to='',days=0;
 if(R.length){fr=R.map(r=>r.from).sort()[0];to=R.map(r=>r.to).sort().pop();days=Math.round((new Date(to)-new Date(fr))/864e5)}
 const pairs=new Set(R.map(r=>r.sym)).size;
 $('sp').innerHTML='<div><b>'+pairs+'</b><span>pairs tested</span></div><div><b>'+days+' days</b><span>'+(fr?fr+' to '+to:'no data yet')+'</span></div><div><b>'+T+'</b><span>trades on unseen data</span></div><div><b>'+P+' of '+R.length+'</b><span>pair and strategy tests passed</span></div>';
 let nt='';
 if(f.failed&&f.failed.length)nt+='<div class=note><b>Could not download:</b><br>'+f.failed.join('<br>')+'</div>';
 if(R.length&&days<180)nt+='<div class=note>Only '+days+' days of data. Treat these results as early signs, not proof.</div>';
 $('nt').innerHTML=nt;
 let h='<tr><th>Strategy<th>Pairs<th>Trades<th>Win rate<th>PF<th>Earlier PF<th>Net R<th>Verdict</tr>';
 for(const a of summary(R)){
  const v=a.n<100?['Too few trades','a']:(a.pf>1.1&&a.ipf>1)?['Candidate','g']:['No edge','r'];
  h+='<tr><td>'+a.k+'<td>'+a.pairs+'<td>'+a.n+'<td>'+wr(a.w,a.n)+'<td class="'+cls(a.pf)+'">'+pf(a.pf)+'<td class="'+cls(a.ipf)+'">'+pf(a.ipf)+'<td>'+a.tot.toFixed(1)+'<td>'+badge(v[0],v[1])}
 $('t1').innerHTML=h;
 let g='<tr><th>Pair<th>Strategy<th>Trades<th>Win rate<th>PF<th>Earlier PF<th>Avg R<th>Max DD (R)<th>Verdict</tr>';
 for(const r of R){const o=r.oos;if(!o.n)continue;
  const v=r.ok?['Passed','g']:o.n<30?['Too few trades','a']:['Unstable','r'];
  g+='<tr><td>'+r.sym+'<td class=l>'+r.strat+'<td>'+o.n+'<td>'+wr(Math.round(o.wr*o.n/100),o.n)+'<td class="'+cls(o.pf)+'">'+pf(o.pf)+'<td class="'+cls(r.is.pf)+'">'+pf(r.is.pf)+'<td>'+o.exp+'<td>'+o.dd+'<td>'+badge(v[0],v[1])}
 $('t2').innerHTML=g;
}
setInterval(load,3000);load();
</script>"""


@app.get("/")
def home():
    return PAGE
