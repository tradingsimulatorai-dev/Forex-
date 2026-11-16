import glob, hmac, os, threading, time
import pandas as pd
from flask import Flask, jsonify, request
from backtest import run_all

app = Flask(__name__)
DATA = os.environ.get("DATA_DIR", "data")
HIST = DATA + "/hist"
KEY = os.environ.get("API_KEY", "")
os.makedirs(HIST, exist_ok=True)
SL_MULT, TP_MULT = 1.5, 3.0  # stop = 1.5 x ATR, target = 3 x ATR (Risk:Reward 1:2)
STATE = {"status": "idle", "results": [], "error": "", "src": "Colab history", "k": SL_MULT, "m": TP_MULT}
CORR = {"names": [], "m": []}
LIVE = {"acc": None, "q": {}, "pos": [], "t": 0.0}
LOCK = threading.Lock()


def authed():
    sent = request.headers.get("X-Key") or request.form.get("key") or ""
    return bool(KEY) and hmac.compare_digest(sent, KEY)


def clean(s):
    return "".join(ch for ch in s if ch.isalnum())


def has_hist():
    return bool(glob.glob(HIST + "/*.csv"))


def calc_corr(folder):  # how markets move together, from hourly moves
    cols = {}
    for p in sorted(glob.glob(folder + "/*.csv")):
        s = pd.read_csv(p, usecols=["time", "close"])
        s["time"] = pd.to_datetime(s["time"].astype(str).str.replace(".", "-", regex=False))
        s = s.drop_duplicates("time").sort_values("time")
        cols[os.path.basename(p).split("_")[0]] = s.set_index("time")["close"].resample("1h").last().pct_change()
    if len(cols) > 1:
        c = pd.DataFrame(cols).corr().round(2).fillna(0)
        CORR["names"] = list(c.columns)
        CORR["m"] = c.values.tolist()


def start_run():
    if not has_hist():
        return False
    with LOCK:
        if STATE["status"] == "running":
            return False
        STATE.update(status="running", error="")

    def job():
        try:
            try:
                calc_corr(HIST)
            except Exception:
                pass
            STATE["results"] = run_all(HIST, SL_MULT, TP_MULT)
            STATE["status"] = "done"
        except Exception as e:
            STATE.update(status="error", error=str(e))

    threading.Thread(target=job, daemon=True).start()
    return True


if has_hist():
    start_run()


@app.post("/ping")
def ping():
    return ("pong", 200) if authed() else ("forbidden", 403)


@app.post("/need")  # MT4 candles are no longer used for backtesting
def need():
    return ("", 200) if authed() else ("forbidden", 403)


@app.post("/ingest")  # accepted but ignored: backtests use only the Colab history
def ingest():
    return ("ok", 200) if authed() else ("forbidden", 403)


@app.post("/hist")  # one-year history uploaded from Colab
def hist():
    if not authed():
        return "forbidden", 403
    sym = clean(request.args.get("symbol", ""))
    if not sym:
        return "no symbol", 400
    p = f"{HIST}/{sym}_M15.csv"
    new = request.args.get("reset") == "1" or not os.path.exists(p)
    with open(p, "w" if new else "a") as f:
        if new:
            f.write("time,open,high,low,close,volume\n")
        f.write(request.get_data(as_text=True).strip() + "\n")
    return "ok"


@app.post("/clear")  # Colab clears old history before uploading a new set
def clear():
    if not authed():
        return "forbidden", 403
    for p in glob.glob(HIST + "/*.csv"):
        os.remove(p)
    STATE.update(results=[], status="idle", error="")
    CORR.update(names=[], m=[])
    return "cleared"


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
    return jsonify({**STATE, "corr": CORR,
                    "bridge": {"ago": int(now - LIVE["t"]) if LIVE["t"] else None, "pairs": len(LIVE["q"])},
                    "live": {"acc": LIVE["acc"], "q": LIVE["q"], "pos": LIVE["pos"],
                             "ago": int(now - LIVE["t"]) if LIVE["t"] else None}})


PAGE = r"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Strategy research</title>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;600;700;800&display=swap" rel=stylesheet>
<style>
:root{--bg:#f3f5f9;--ink:#0f1b2d;--mut:#64708a;--line:#e1e6ef;--g:#12805c;--r:#c0392b;--a:#b7791f;--b:#0b5cad}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 "Plus Jakarta Sans",system-ui,sans-serif;font-variant-numeric:tabular-nums}
.top{background:linear-gradient(135deg,#0f2742 0%,#16486f 100%);color:#fff;padding:26px 16px 82px}
.in,.wrap{max-width:1100px;margin:0 auto}
.wrap{padding:0 16px 48px;margin-top:-58px}
.brand{font-size:12px;letter-spacing:.14em;text-transform:uppercase;opacity:.7;font-weight:700}
h1{font-size:28px;margin:4px 0 2px;font-weight:800;letter-spacing:-.02em}
.sub{opacity:.8;font-size:14px;margin-bottom:14px;max-width:640px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-flex;align-items:center;background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.22);border-radius:99px;padding:5px 14px;font-size:13px}
.dot{width:9px;height:9px;border-radius:50%;background:#f87171;margin-right:8px;flex:none}
.dot.on{background:#34d399}.dot.warn{background:#fbbf24}
.dot.pulse{animation:p 1.2s ease-in-out infinite}
@keyframes p{0%,100%{box-shadow:0 0 0 0 rgba(52,211,153,.6)}50%{box-shadow:0 0 0 6px rgba(52,211,153,0)}}
.card,.hero,.tiles div,.tb{background:#fff;border:1px solid var(--line);border-radius:14px;box-shadow:0 1px 2px rgba(15,27,45,.04),0 6px 18px rgba(15,27,45,.05)}
.hero{padding:20px 22px;margin-bottom:16px;border-top:4px solid var(--r)}
.hero.good{border-top-color:var(--g)}
.eyebrow{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--mut);font-weight:700}
.hh{font-size:25px;font-weight:800;line-height:1.3;letter-spacing:-.01em;margin:4px 0}
.hero p{margin:4px 0 14px;color:var(--mut);max-width:720px}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
.facts div{background:#f6f8fb;border-radius:10px;padding:10px 14px}
.facts b{display:block;font-size:18px}.facts span{color:var(--mut);font-size:12.5px}
.note{border-left:4px solid var(--a);background:#fff7e8;padding:10px 14px;margin-bottom:16px;border-radius:0 8px 8px 0;font-size:14px}
h2{font-size:19px;font-weight:800;margin:30px 0 2px;letter-spacing:-.01em}
.cap{color:var(--mut);font-size:13.5px;margin:0 0 12px;max-width:820px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(255px,1fr));gap:14px}
.card{padding:16px 18px}
.ch{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:4px}
.ch b{font-size:16px}
.big{font-size:36px;font-weight:800;line-height:1.2;letter-spacing:-.02em}
.big small{font-size:13px;color:var(--mut);font-weight:600;margin-left:8px;letter-spacing:0}
.barw{position:relative;height:10px;background:#e8ecf3;border-radius:6px;margin:12px 0 4px}
.fill{height:10px;border-radius:6px}
.mark{position:absolute;top:-4px;width:3px;height:18px;background:var(--ink);border-radius:2px}
.bl{display:flex;justify-content:space-between;color:var(--mut);font-size:12px;margin-bottom:10px}
.row{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-top:1px solid #edf0f5;font-size:14px}
.row span{color:var(--mut)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.tiles div{padding:14px 16px}
.tiles span{display:block;color:var(--mut);font-size:12.5px;font-weight:600}
.tiles b{font-size:23px;font-weight:800;letter-spacing:-.01em}
.tb{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:420px}
th{font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);font-weight:700;text-align:right;padding:10px 14px;border-bottom:1px solid var(--line);background:#f8fafc;white-space:nowrap}
td{padding:10px 14px;border-bottom:1px solid #edf0f5;text-align:right;white-space:nowrap}
tr:last-child td{border-bottom:0}
th:first-child,td:first-child{text-align:left}
td small{color:var(--mut);margin-left:6px}
#t6 th,#t6 td{text-align:center}#t6 th:first-child,#t6 td:first-child{text-align:left}
#t6 td{line-height:1.3;padding:9px 12px}#t6 td b{font-size:17px}#t6 td small{display:block;margin:2px 0 0}
.m{color:#9aa3b2}
.g{color:var(--g)}.r{color:var(--r)}.a{color:var(--a)}
.b{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;font-weight:700;border:1px solid currentColor;white-space:nowrap}
</style>
<div class=top><div class=in>
<div class=brand>Strategy Lab</div>
<h1>Strategy research</h1>
<div class=sub>Live account data from your MT4. Backtests use one year of history uploaded from Colab and run automatically.</div>
<div class=chips><span class=chip id=br></span><span class=chip id=st>Waiting for history</span></div>
</div></div>
<div class=wrap>
<div id=hero></div>
<div id=nt></div>
<h2>Strategy results</h2>
<p class=cap id=rule></p>
<div class=cards id=cards></div>
<h2>By market</h2>
<p class=cap>Every market tested separately on the unseen last third of the year. Bold = win rate. Under it: number of trades, and dollars won for every $1 lost. Green = passed the checks, red = losing, amber = unstable, grey = fewer than 30 trades.</p>
<div class=tb><table id=t6></table></div>
<h2>Account</h2>
<div class=tiles id=ac></div>
<h2>Open trades</h2><div class=tb><table id=t3></table></div>
<h2>Live prices</h2><p class=cap id=mk></p>
<div class=tb><table id=t4></table></div>
<h2>Which markets move together</h2>
<p class=cap>Based on hourly price moves over the year. Blue = move together, amber = move opposite (darker means stronger). Take your Buy and Sell trades from markets that are not strongly linked, otherwise you double the same bet.</p>
<div class=tb><table id=t5></table></div>
</div>
<script>
const $=i=>document.getElementById(i);
const N=v=>Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
function ago(s){return s<60?s+' sec':s<3600?Math.floor(s/60)+' min':s<86400?Math.floor(s/3600)+' h':Math.floor(s/86400)+' d'}
const badge=(t,c)=>'<span class="b '+c+'">'+t+'</span>';
function bridge(b){let c='',t='No data received from MT4 yet';
 if(b.ago!=null){const i=' · '+b.pairs+' live prices';
  if(b.ago<20){c='on pulse';t='MT4 connected, live now'+i}
  else if(b.ago<300){c='on';t='MT4 connected · last data '+ago(b.ago)+' ago'+i}
  else{c='warn';t='No data from MT4 for '+ago(b.ago)+'. Check the EA in MT4'+i}}
 $('br').innerHTML='<span class="dot '+c+'"></span>'+t}
function live(l){const a=l.acc;
 if(!a||l.ago==null||l.ago>60){$('ac').innerHTML='<div><span>Account</span><b>-</b><span>Waiting for account data from MT4</span></div>';$('t3').innerHTML='';$('t4').innerHTML='';$('mk').innerHTML='';return}
 const bal=+a.bal,eq=+a.eq,f=eq-bal;
 $('ac').innerHTML='<div><span>Balance ('+a.cur+')</span><b>'+N(bal)+'</b></div><div><span>Equity</span><b>'+N(eq)+'</b></div><div><span>Floating P/L</span><b class="'+(f>=0?'g':'r')+'">'+(f>=0?'+':'')+N(f)+'</b></div><div><span>Margin level</span><b>'+(+a.lvl>0?(+a.lvl).toFixed(0)+'%':'-')+'</b></div><div><span>Free margin</span><b>'+N(a.free)+'</b></div>';
 let p='<tr><th>Market<th>Side<th>Lots<th>Profit</tr>';
 p+=l.pos.length?l.pos.map(x=>'<tr><td>'+x.s+'<td>'+x.d+'<td>'+x.lot+'<td class="'+(+x.pl>=0?'g':'r')+'">'+N(x.pl)+'</tr>').join(''):'<tr><td colspan=4>No open trades</tr>';
 $('t3').innerHTML=p;
 const keys=Object.keys(l.q),op=keys.filter(s=>l.q[s].age!=null&&l.q[s].age<300),cl=keys.filter(s=>!op.includes(s));
 $('mk').innerHTML='<b class=g>Open now ('+op.length+'):</b> '+(op.join(', ')||'none')+'<br><b class=a>Closed ('+cl.length+'):</b> '+(cl.join(', ')||'none');
 let q='<tr><th>Market<th>Status<th>Bid<th>Ask<th>Spread (points)</tr>';
 for(const s of keys){const x=l.q[s],o=op.includes(s),dec=(x.bid.split('.')[1]||'').length,m=o?'':' class=m';
  q+='<tr><td>'+s+'<td>'+(o?badge('Open','g'):badge('Closed','a')+'<small>last tick '+(x.age!=null&&x.age<99999?ago(x.age)+' ago':'unknown')+'</small>')+'<td'+m+'>'+x.bid+'<td'+m+'>'+x.ask+'<td'+m+'>'+(o?Math.round((x.ask-x.bid)*Math.pow(10,dec)):'-')+'</tr>'}
 $('t4').innerHTML=q}
function corr(c){
 if(!c||!c.names.length){$('t5').innerHTML='<tr><td>Calculated when the backtest starts</tr>';return}
 let h='<tr><th><th>'+c.names.join('<th>')+'</tr>';
 c.names.forEach((n,i)=>{h+='<tr><td>'+n+c.m[i].map((v,j)=>{if(i==j)return'<td class=m>-';const a=Math.min(Math.abs(v),1)*.6;return'<td style="background:'+(v>=0?'rgba(11,92,173,'+a+')':'rgba(217,119,6,'+a+')')+'">'+v.toFixed(2)}).join('')+'</tr>'});
 $('t5').innerHTML=h}
function ci(w,n){const z=1.96,p=w/n,d=1+z*z/n,c=p+z*z/(2*n),m=z*Math.sqrt(p*(1-p)/n+z*z/(4*n*n));return[Math.round((c-m)/d*100),Math.round((c+m)/d*100)]}
const cls=v=>v==null?'':v>=1.1?'g':v<1?'r':'a';
const pf=v=>v==null?'-':v.toFixed(2);
function summary(R){const m={};
 for(const r of R){const o=r.oos,i=r.is;if(!o.n)continue;
  const a=m[r.strat]=m[r.strat]||{n:0,w:0,gw:0,gl:0,igw:0,igl:0,tot:0,pairs:0};
  a.n+=o.n;a.w+=Math.round(o.wr*o.n/100);a.gw+=o.gw||0;a.gl+=o.gl||0;a.tot+=o.tot;a.pairs++;
  a.igw+=i.gw||0;a.igl+=i.gl||0}
 return Object.keys(m).map(k=>{const a=m[k];a.k=k;a.pf=a.gl?a.gw/a.gl:null;a.ipf=a.igl?a.igw/a.igl:null;return a}).sort((x,y)=>(y.pf||0)-(x.pf||0))}
function hero(R,d){
 if(!R.length){$('hero').innerHTML='<div class=hero><div class=eyebrow>Verdict</div><div class=hh>Waiting for history</div><p>Run the Colab notebook to upload one year of data. The backtest then starts by itself and the verdict appears here.</p></div>';$('nt').innerHTML='';return}
 const S=summary(R),good=S.filter(a=>a.n>=100&&a.pf>1.1&&a.ipf>1);
 const fr=R.map(r=>r.from).sort()[0],to=R.map(r=>r.to).sort().pop(),days=Math.round((new Date(to)-new Date(fr))/864e5);
 const T=R.reduce((s,r)=>s+(r.oos.n||0),0),pairs=[...new Set(R.map(r=>r.sym))];
 $('hero').innerHTML='<div class="hero'+(good.length?' good':'')+'"><div class=eyebrow>Verdict</div><div class=hh>'+(good.length?'Promising: '+good.map(a=>a.k).join(', '):'No strategy is proven yet')+'</div><p>'
  +(good.length?'These made money in both the earlier and the recent part of the data. Still run them on a demo account before using real money.':'No strategy made money steadily in both the earlier and the recent part of the data. If one does, it will show here in green.')
  +'</p><div class=facts><div><b>'+days+' days</b><span>'+fr+' to '+to+' · Colab history</span></div><div><b>'+pairs.length+' markets</b><span>'+pairs.join(', ')+'</span></div><div><b>'+T.toLocaleString()+'</b><span>trades on unseen data</span></div></div></div>';
 $('nt').innerHTML=days<300?'<div class=note>Only '+days+' days of data. Upload a full year for a fair test.</div>':''}
function cards(R,d){
 const sl=d.k||1.5,tp=d.m||3,rr=tp/sl,be=100/(1+rr),S=summary(R);
 $('rule').innerHTML='Every trade risks 1 unit, called <b>1R</b> (what you lose if the stop loss is hit). Stop loss = '+sl+' × ATR and take profit = '+tp+' × ATR, so a win pays '+rr+'R: <b>Risk : Reward = 1 : '+rr+'</b>. At this ratio you break even at about <b>'+be.toFixed(0)+'% wins</b> before costs (the black mark), a little more after spread. All markets combined, unseen last third of the data.';
 $('cards').innerHTML=S.map(a=>{
  const win=a.w/a.n*100,r=ci(a.w,a.n),avg=a.tot/a.n,col=win>=be+3?'var(--g)':win<be?'var(--r)':'var(--a)';
  const v=a.n<100?['Too few trades','a']:(a.pf>1.1&&a.ipf>1)?['Promising','g']:a.pf<1?['Losing','r']:['Unstable, not proven','a'];
  return '<div class=card><div class=ch><b>'+a.k+'</b>'+badge(v[0],v[1])+'</div>'
   +'<div class=big>'+win.toFixed(0)+'%<small>win rate ('+r[0]+'-'+r[1]+'%)</small></div>'
   +'<div class=barw><div class=fill style="width:'+Math.min(win/60*100,100)+'%;background:'+col+'"></div><div class=mark style="left:'+(be/60*100)+'%"></div></div>'
   +'<div class=bl><span>0%</span><span>break-even '+be.toFixed(0)+'%</span><span>60%</span></div>'
   +'<div class=row><span>Trades tested</span><b>'+a.n+'</b></div>'
   +'<div class=row><span>Won per $1 lost</span><b class="'+cls(a.pf)+'">$'+pf(a.pf)+'</b></div>'
   +'<div class=row><span>Same, earlier part</span><b class="'+cls(a.ipf)+'">$'+pf(a.ipf)+'</b></div>'
   +'<div class=row><span>Average per trade</span><b class="'+(avg>=0?'g':'r')+'">'+(avg>=0?'+':'')+avg.toFixed(2)+'R</b></div>'
   +'<div class=row><span>Total result</span><b class="'+(a.tot>=0?'g':'r')+'">'+(a.tot>=0?'+':'')+a.tot.toFixed(1)+'R</b></div></div>'}).join('')}
function matrix(R){
 const syms=[...new Set(R.map(r=>r.sym))],strats=[...new Set(R.map(r=>r.strat))];
 if(!syms.length){$('t6').innerHTML='<tr><td>Waiting for results</tr>';return}
 let h='<tr><th>Market'+strats.map(s=>'<th>'+s).join('')+'</tr>';
 for(const m of syms){h+='<tr><td><b>'+m+'</b>';
  for(const s of strats){const r=R.find(x=>x.sym==m&&x.strat==s);
   if(!r||!r.oos.n){h+='<td class=m>-';continue}
   const o=r.oos,bg=o.n<30?'#f1f3f7':r.ok?'rgba(18,128,92,.16)':o.pf<1?'rgba(192,57,43,.13)':'rgba(183,121,31,.15)';
   h+='<td style="background:'+bg+'"><b>'+Math.round(o.wr)+'%</b><small>'+o.n+' trades · $'+pf(o.pf)+'</small>'}
  h+='</tr>'}
 $('t6').innerHTML=h}
async function load(){
 let d;try{d=await (await fetch('/state')).json()}catch(e){$('br').innerHTML='<span class="dot warn"></span>Server is waking up, please wait up to a minute...';return}
 const R=d.results;bridge(d.bridge);live(d.live);corr(d.corr);hero(R,d);cards(R,d);matrix(R);
 $('st').textContent=d.status=='running'?'Running backtest...':d.status=='error'?'Error: '+d.error:R.length?'Results ready':'Waiting for history';
}
setInterval(load,3000);load();
</script>"""


@app.get("/")
def home():
    return PAGE
