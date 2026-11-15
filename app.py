import glob, hmac, os, threading, time
from flask import Flask, jsonify, request
from backtest import run_all

app = Flask(__name__)
DATA = os.environ.get("DATA_DIR", "data")
KEY = os.environ.get("API_KEY", "")
os.makedirs(DATA, exist_ok=True)
STATE = {"status": "idle", "results": [], "error": ""}
BR = {"last": 0.0, "dirty": False, "rows": {}}
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
            STATE["results"] = run_all(DATA)
            STATE["status"] = "done"
        except Exception as e:
            STATE.update(status="error", error=str(e))

    threading.Thread(target=job, daemon=True).start()
    return True


def watcher():  # auto-run 25s after MT4 stops sending
    while True:
        time.sleep(5)
        if BR["dirty"] and BR["last"] and time.time() - BR["last"] > 25:
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
    BR["last"], BR["dirty"] = time.time(), True
    return "ok"


@app.post("/run")
def run():
    if not authed():
        return "forbidden", 403
    return "started" if start_run() else "already running"


@app.get("/state")
def state():
    ago = int(time.time() - BR["last"]) if BR["last"] else None
    return jsonify({**STATE, "bridge": {"ago": ago, "pairs": len(BR["rows"]),
                                        "rows": sum(BR["rows"].values())}})


PAGE = r"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Strategy research</title>
<link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;600;700&display=swap" rel=stylesheet>
<style>
:root{--bg:#eef1f5;--ink:#14213d;--mut:#5b6678;--line:#d9dee7;--teal:#0b6e6e;--g:#1a7f4b;--r:#b4322b;--a:#a8650c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "Source Sans 3",system-ui,sans-serif;font-variant-numeric:tabular-nums}
.wrap{max-width:1100px;margin:0 auto;padding:20px 16px 40px}
header{margin-bottom:14px}
h1{font-size:24px;margin:0;font-weight:700;letter-spacing:-.01em}
.sub{color:var(--mut);font-size:14px}
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
table{border-collapse:collapse;width:100%;min-width:660px}
th{font-size:13px;color:var(--mut);font-weight:600;text-align:right;padding:9px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid #edf0f4;text-align:right;white-space:nowrap}
tr:last-child td{border-bottom:0}
th:first-child,td:first-child,#t2 th:nth-child(2),td.l{text-align:left}
td small{color:var(--mut);margin-left:4px}
.g{color:var(--g)}.r{color:var(--r)}.a{color:var(--a)}
.b{display:inline-block;padding:1px 9px;border-radius:99px;font-size:12px;font-weight:600;border:1px solid currentColor}
</style>
<div class=wrap>
<header><h1>Strategy research</h1>
<div class=sub>15-minute candles from MT4, ATR stop and target, spread included. Tests run automatically when new data arrives.</div></header>
<div class=bar><div id=br></div><span class=pill id=st>Waiting for data</span></div>
<div class=strip id=sp></div><div id=nt></div>
<h2>Strategies across all pairs</h2>
<p class=cap>Results on data the strategy had not seen (last third of the period).</p>
<div class=tb><table id=t1></table></div>
<h2>Each pair</h2>
<p class=cap>The range next to win rate is a 95% range. A wide range means too few trades to trust it.</p>
<div class=tb><table id=t2></table></div>
</div>
<script>
const $=i=>document.getElementById(i);
function ago(s){return s<60?s+' sec':s<3600?Math.floor(s/60)+' min':Math.floor(s/3600)+' h'}
function bridge(b){let c='',t='Waiting for MT4. No data received yet.';
 if(b.ago!=null){const i=' · '+b.pairs+' pairs · '+b.rows.toLocaleString()+' candles';
  if(b.ago<20){c='on pulse';t='Receiving data from MT4 now'+i}
  else if(b.ago<1200){c='on';t='MT4 connected · last data '+ago(b.ago)+' ago'+i}
  else{c='warn';t='No data from MT4 for '+ago(b.ago)+'. Check the EA in MT4.'+i}}
 $('br').innerHTML='<span class="dot '+c+'"></span>'+t}
function ci(w,n){const z=1.96,p=w/n,d=1+z*z/n,c=p+z*z/(2*n),m=z*Math.sqrt(p*(1-p)/n+z*z/(4*n*n));return[Math.round((c-m)/d*100),Math.round((c+m)/d*100)]}
const cls=v=>v==null?'':v>=1.1?'g':v<1?'r':'a';
const pf=v=>v==null?'-':v.toFixed(2);
function wr(w,n){if(!n)return'-';const a=ci(w,n);return Math.round(w/n*100)+'%<small>('+a[0]+'-'+a[1]+')</small>'}
const badge=(t,c)=>'<span class="b '+c+'">'+t+'</span>';
function summary(R){const m={};
 for(const r of R){const o=r.oos,i=r.is;if(!o.n)continue;
  const a=m[r.strat]=m[r.strat]||{n:0,w:0,gw:0,gl:0,igw:0,igl:0,tot:0,pairs:0};
  a.n+=o.n;a.w+=Math.round(o.wr*o.n/100);a.gw+=o.gw||0;a.gl+=o.gl||0;a.tot+=o.tot;a.pairs++;
  a.igw+=i.gw||0;a.igl+=i.gl||0}
 return Object.keys(m).map(k=>{const a=m[k];a.k=k;a.pf=a.gl?a.gw/a.gl:null;a.ipf=a.igl?a.igw/a.igl:null;return a}).sort((x,y)=>(y.pf||0)-(x.pf||0))}
async function load(){
 let d;try{d=await (await fetch('/state')).json()}catch(e){$('br').innerHTML='<span class="dot warn"></span>Server is waking up, please wait up to a minute...';return}
 const R=d.results;bridge(d.bridge);
 $('st').textContent=d.status=='running'?'Running backtest...':d.status=='error'?'Error: '+d.error:R.length?'Results ready':'Waiting for data';
 const T=R.reduce((s,r)=>s+(r.oos.n||0),0),P=R.filter(r=>r.ok).length;
 let fr='',to='',days=0;
 if(R.length){fr=R.map(r=>r.from).sort()[0];to=R.map(r=>r.to).sort().pop();days=Math.round((new Date(to)-new Date(fr))/864e5)}
 const pairs=new Set(R.map(r=>r.sym)).size;
 $('sp').innerHTML='<div><b>'+pairs+'</b><span>pairs tested</span></div><div><b>'+days+' days</b><span>'+(fr?fr+' to '+to:'no data yet')+'</span></div><div><b>'+T+'</b><span>trades on unseen data</span></div><div><b>'+P+' of '+R.length+'</b><span>pair and strategy tests passed</span></div>';
 $('nt').innerHTML=R.length&&days<180?'<div class=note>Only '+days+' days of data. Treat these results as early signs, not proof. Aim for 6 to 12 months of data and 100 or more trades per strategy.</div>':'';
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
