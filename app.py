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
STATE = {"status": "idle", "results": [], "error": "", "src": "", "k": SL_MULT, "m": TP_MULT}
CORR = {"names": [], "m": []}
BR = {"ing": 0.0, "dirty": False, "rows": {}}
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
    folder = HIST if has_hist() else DATA
    if not glob.glob(folder + "/*.csv"):
        return False
    with LOCK:
        if STATE["status"] == "running":
            return False
        STATE.update(status="running", error="", src="Uploaded history" if folder == HIST else "MT4 data")
    BR["dirty"] = False

    def job():
        try:
            try:
                calc_corr(folder)
            except Exception:
                pass
            STATE["results"] = run_all(folder, SL_MULT, TP_MULT)
            STATE["status"] = "done"
        except Exception as e:
            STATE.update(status="error", error=str(e))

    threading.Thread(target=job, daemon=True).start()
    return True


def watcher():  # run 25s after MT4 candles stop arriving (only when no uploaded history)
    while True:
        time.sleep(5)
        if BR["dirty"] and BR["ing"] and time.time() - BR["ing"] > 25 and not has_hist():
            start_run()


for p in glob.glob(DATA + "/*_M15.csv"):
    with open(p) as fh:
        BR["rows"][os.path.basename(p).split("_")[0]] = max(sum(1 for _ in fh) - 1, 0)
threading.Thread(target=watcher, daemon=True).start()
if has_hist():
    start_run()


@app.post("/ping")
def ping():
    return ("pong", 200) if authed() else ("forbidden", 403)


@app.post("/need")  # EA asks: which of my pairs do you lack?
def need():
    if not authed():
        return "forbidden", 403
    names = [clean(x) for x in request.get_data(as_text=True).split(",")]
    return ",".join(s for s in names if s and BR["rows"].get(s, 0) < 500)


@app.post("/ingest")  # candles from MT4
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
    return jsonify({**STATE, "corr": CORR,
                    "bridge": {"ago": int(now - last) if last else None,
                               "pairs": len(BR["rows"]), "rows": sum(BR["rows"].values())},
                    "live": {"acc": LIVE["acc"], "q": LIVE["q"], "pos": LIVE["pos"],
                             "ago": int(now - LIVE["t"]) if LIVE["t"] else None}})


PAGE = r"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>কৌশল গবেষণা</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;600;700&family=Source+Sans+3:wght@400;600;700&display=swap" rel=stylesheet>
<style>
:root{--bg:#eef1f5;--ink:#14213d;--mut:#5b6678;--line:#d9dee7;--g:#1a7f4b;--r:#b4322b;--a:#a8650c;--b:#0b5cad}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 "Noto Sans Bengali","Source Sans 3","Vrinda",system-ui,sans-serif;font-variant-numeric:tabular-nums}
.wrap{max-width:1100px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:24px;margin:0;font-weight:700}
.sub{color:var(--mut);font-size:14px;margin-bottom:14px}
.bar{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:8px;background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:14px;font-size:14px}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:8px;background:var(--r)}
.dot.on{background:var(--g)}.dot.warn{background:var(--a)}
.dot.pulse{animation:p 1.2s ease-in-out infinite}
@keyframes p{0%,100%{box-shadow:0 0 0 0 rgba(26,127,75,.5)}50%{box-shadow:0 0 0 6px rgba(26,127,75,0)}}
.pill{border:1px solid var(--line);border-radius:99px;padding:2px 12px;font-size:13px;color:var(--mut)}
.hero{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:14px}
.hh{font-size:21px;font-weight:700;line-height:1.35}
.hero p{margin:6px 0 12px;color:var(--mut)}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.facts div{border-top:1px solid var(--line);padding-top:8px}
.facts b{display:block;font-size:19px}.facts span{color:var(--mut);font-size:13px}
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));background:#fff;border:1px solid var(--line);border-radius:10px;margin-bottom:14px}
.strip div{padding:12px 16px;border-right:1px solid var(--line)}
.strip div:last-child{border-right:0}
.strip b{display:block;font-size:22px;line-height:1.3}
.strip span{color:var(--mut);font-size:13px}
.note{border-left:4px solid var(--a);background:#fff7e8;padding:10px 14px;margin-bottom:14px;border-radius:0 6px 6px 0;font-size:14px}
h2{font-size:18px;margin:26px 0 4px}
.cap{color:var(--mut);font-size:13.5px;margin:0 0 10px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.ch{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px}
.big{font-size:34px;font-weight:700;line-height:1.2}
.big small{font-size:13px;color:var(--mut);font-weight:400;margin-left:6px}
.barw{position:relative;height:9px;background:#e3e6ec;border-radius:5px;margin:10px 0 3px}
.fill{height:9px;border-radius:5px;background:var(--b)}
.mark{position:absolute;top:-4px;width:3px;height:17px;background:var(--ink);border-radius:2px}
.bl{display:flex;justify-content:space-between;color:var(--mut);font-size:12px;margin-bottom:10px}
.row{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-top:1px solid #edf0f4;font-size:14px}
.row span{color:var(--mut)}
.tb{overflow-x:auto;background:#fff;border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;min-width:420px}
th{font-size:13px;color:var(--mut);font-weight:600;text-align:right;padding:9px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid #edf0f4;text-align:right;white-space:nowrap}
tr:last-child td{border-bottom:0}
th:first-child,td:first-child{text-align:left}
td small{color:var(--mut);margin-left:6px}
.m{color:#9aa3b2}
.g{color:var(--g)}.r{color:var(--r)}.a{color:var(--a)}
td.pos{color:var(--b);font-weight:700}td.neg{color:var(--a);font-weight:700}
.b{display:inline-block;padding:1px 10px;border-radius:99px;font-size:12px;font-weight:600;border:1px solid currentColor;white-space:nowrap}
</style>
<div class=wrap>
<h1>কৌশল গবেষণা</h1>
<div class=sub>MT4 থেকে লাইভ তথ্য আসে, নতুন ডেটা এলে ব্যাকটেস্ট নিজে চলে।</div>
<div class=bar><div id=br></div><span class=pill id=st>ডেটার অপেক্ষা</span></div>
<div id=hero></div>
<div id=nt></div>
<h2>কৌশলের ফল</h2>
<p class=cap id=rule></p>
<div class=cards id=cards></div>
<h2>অ্যাকাউন্ট</h2>
<div class=strip id=ac></div>
<h2>খোলা ট্রেড</h2><div class=tb><table id=t3></table></div>
<h2>লাইভ দাম</h2><p class=cap id=mk></p>
<div class=tb><table id=t4></table></div>
<h2>কোন বাজার একসাথে চলে</h2>
<p class=cap>ঘণ্টাভিত্তিক দামের ওঠানামা মিলিয়ে। নীল = একসাথে চলে, কমলা = উল্টো চলে (+১ এর কাছে একসাথে, −১ এর কাছে উল্টো)। Buy আর Sell ট্রেড আলাদা গ্রুপ থেকে নিন, উল্টো চলা জোড়া থেকে নয়, তাহলে একই বাজি দ্বিগুণ হয় না।</p>
<div class=tb><table id=t5></table></div>
</div>
<script>
const $=i=>document.getElementById(i);
const N=v=>Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
function ago(s){return s<60?s+' সেকেন্ড':s<3600?Math.floor(s/60)+' মিনিট':s<86400?Math.floor(s/3600)+' ঘণ্টা':Math.floor(s/86400)+' দিন'}
const badge=(t,c)=>'<span class="b '+c+'">'+t+'</span>';
function bridge(b){let c='',t='MT4 থেকে এখনো কোনো ডেটা আসেনি।';
 if(b.ago!=null){const i=' · '+b.pairs+'টা বাজার · '+b.rows.toLocaleString()+' ক্যান্ডেল';
  if(b.ago<20){c='on pulse';t='MT4 থেকে এখন ডেটা আসছে'+i}
  else if(b.ago<1200){c='on';t='MT4 সংযুক্ত · শেষ ডেটা '+ago(b.ago)+' আগে'+i}
  else{c='warn';t='MT4 থেকে '+ago(b.ago)+' ধরে ডেটা আসছে না। MT4-এ EA চেক করুন।'+i}}
 $('br').innerHTML='<span class="dot '+c+'"></span>'+t}
function live(l){const a=l.acc;
 if(!a||l.ago==null||l.ago>60){$('ac').innerHTML='<div><b>-</b><span>MT4 থেকে অ্যাকাউন্টের তথ্যের অপেক্ষায়</span></div>';$('t3').innerHTML='';$('t4').innerHTML='';$('mk').innerHTML='';return}
 const bal=+a.bal,eq=+a.eq,f=eq-bal;
 $('ac').innerHTML='<div><b>'+N(bal)+'</b><span>ব্যালেন্স ('+a.cur+')</span></div><div><b>'+N(eq)+'</b><span>ইক্যুইটি</span></div><div><b class="'+(f>=0?'g':'r')+'">'+(f>=0?'+':'')+N(f)+'</b><span>চলমান লাভ/লস</span></div><div><b>'+(+a.lvl>0?(+a.lvl).toFixed(0)+'%':'-')+'</b><span>মার্জিন লেভেল</span></div><div><b>'+N(a.free)+'</b><span>ফ্রি মার্জিন</span></div>';
 let p='<tr><th>বাজার<th>দিক<th>লট<th>লাভ/লস</tr>';
 p+=l.pos.length?l.pos.map(x=>'<tr><td>'+x.s+'<td>'+x.d+'<td>'+x.lot+'<td class="'+(+x.pl>=0?'g':'r')+'">'+N(x.pl)+'</tr>').join(''):'<tr><td colspan=4>কোনো ট্রেড খোলা নেই</tr>';
 $('t3').innerHTML=p;
 const keys=Object.keys(l.q),op=keys.filter(s=>l.q[s].age!=null&&l.q[s].age<300),cl=keys.filter(s=>!op.includes(s));
 $('mk').innerHTML='<b class=g>খোলা ('+op.length+'):</b> '+(op.join(', ')||'নেই')+'<br><b class=a>বন্ধ ('+cl.length+'):</b> '+(cl.join(', ')||'নেই');
 let q='<tr><th>বাজার<th>অবস্থা<th>Bid<th>Ask<th>Spread (পয়েন্ট)</tr>';
 for(const s of keys){const x=l.q[s],o=op.includes(s),dec=(x.bid.split('.')[1]||'').length,m=o?'':' class=m';
  q+='<tr><td>'+s+'<td>'+(o?badge('খোলা','g'):badge('বন্ধ','a')+'<small>শেষ টিক '+(x.age!=null&&x.age<99999?ago(x.age)+' আগে':'অজানা')+'</small>')+'<td'+m+'>'+x.bid+'<td'+m+'>'+x.ask+'<td'+m+'>'+(o?Math.round((x.ask-x.bid)*Math.pow(10,dec)):'-')+'</tr>'}
 $('t4').innerHTML=q}
function corr(c){
 if(!c||!c.names.length){$('t5').innerHTML='<tr><td>ব্যাকটেস্ট শুরু হলে হিসাব হবে</tr>';return}
 let h='<tr><th><th>'+c.names.join('<th>')+'</tr>';
 c.names.forEach((n,i)=>{h+='<tr><td>'+n+c.m[i].map((v,j)=>'<td class="'+(i==j?'m':v>=.5?'pos':v<=-.5?'neg':'')+'">'+(i==j?'-':v.toFixed(2))).join('')+'</tr>'});
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
 if(!R.length){$('hero').innerHTML='<div class=hero><div class=hh>ফলের অপেক্ষা</div><p>ডেটা এলে ব্যাকটেস্ট নিজে চলবে, ফল এখানে সবার উপরে আসবে।</p></div>';return}
 const S=summary(R),good=S.filter(a=>a.n>=100&&a.pf>1.1&&a.ipf>1);
 const fr=R.map(r=>r.from).sort()[0],to=R.map(r=>r.to).sort().pop(),days=Math.round((new Date(to)-new Date(fr))/864e5);
 const T=R.reduce((s,r)=>s+(r.oos.n||0),0),pairs=[...new Set(R.map(r=>r.sym))];
 const src=d.src=='Uploaded history'?'আপলোড করা ইতিহাস':'MT4-এর ডেটা';
 $('hero').innerHTML='<div class=hero><div class="hh '+(good.length?'g':'r')+'">'+(good.length?'সম্ভাবনাময়: '+good.map(a=>a.k).join(', '):'এখনো কোনো কৌশল প্রমাণিত নয়')+'</div><p>'
  +(good.length?'এগুলো আগের ও শেষের দুই অংশেই লাভজনক ছিল। তবু ডেমোতে চালিয়ে মিলিয়ে দেখার আগে টাকা লাগাবেন না।':'কোনো কৌশল আগের ও শেষের দুই অংশেই স্থিরভাবে লাভ দেয়নি। দুই অংশেই লাভ হলে এখানে সবুজ হয়ে দেখা যাবে।')
  +'</p><div class=facts><div><b>'+days+' দিন</b><span>'+fr+' থেকে '+to+' · '+src+'</span></div><div><b>'+pairs.length+'টা বাজার</b><span>'+pairs.join(', ')+'</span></div><div><b>'+T+'</b><span>ট্রেড (অদেখা অংশে)</span></div></div></div>';
 $('nt').innerHTML=days<180?'<div class=note>মাত্র '+days+' দিনের ডেটা। এই ফল শুধু আভাস, প্রমাণ নয়। ১ বছরের ইতিহাস আপলোড করুন।</div>':''}
function cards(R,d){
 const sl=d.k||1.5,tp=d.m||3,rr=tp/sl,be=100/(1+rr),S=summary(R);
 $('rule').innerHTML='প্রতি ট্রেডে লস সীমা (SL) = '+sl+' × ATR, লাভের লক্ষ্য (TP) = '+tp+' × ATR, তাই <b>Risk : Reward = 1 : '+rr+'</b>। এই অনুপাতে খরচ ছাড়া প্রায় <b>'+be.toFixed(0)+'%</b> জিতলে লাভ-লোকসান সমান, spread ধরলে আরও একটু বেশি লাগে (কার্ডের কালো দাগ)। R মানে এক ট্রেডে যতটা ঝুঁকি নেন। সব সংখ্যা অদেখা অংশের (শেষ এক-তৃতীয়াংশ)।';
 $('cards').innerHTML=S.map(a=>{
  const win=a.w/a.n*100,r=ci(a.w,a.n),avg=a.tot/a.n;
  const v=a.n<100?['ট্রেড কম','a']:(a.pf>1.1&&a.ipf>1)?['সম্ভাবনাময়','g']:a.pf<1?['লোকসানি','r']:['অস্থির, প্রমাণ নেই','a'];
  return '<div class=card><div class=ch><b>'+a.k+'</b>'+badge(v[0],v[1])+'</div>'
   +'<div class=big>'+win.toFixed(0)+'%<small>জিতেছে ('+r[0]+'-'+r[1]+'%)</small></div>'
   +'<div class=barw><div class=fill style="width:'+Math.min(win/60*100,100)+'%"></div><div class=mark style="left:'+(be/60*100)+'%"></div></div>'
   +'<div class=bl><span>0%</span><span>লাভের সীমা '+be.toFixed(0)+'%</span><span>60%</span></div>'
   +'<div class=row><span>ট্রেড সংখ্যা</span><b>'+a.n+'</b></div>'
   +'<div class=row><span>প্রতি $1 হারালে জিতেছেন</span><b class="'+cls(a.pf)+'">$'+pf(a.pf)+'</b></div>'
   +'<div class=row><span>আগের অংশে ওই হিসাব</span><b class="'+cls(a.ipf)+'">$'+pf(a.ipf)+'</b></div>'
   +'<div class=row><span>প্রতি ট্রেডে গড়</span><b class="'+(avg>=0?'g':'r')+'">'+(avg>=0?'+':'')+avg.toFixed(2)+'R</b></div>'
   +'<div class=row><span>মোট</span><b class="'+(a.tot>=0?'g':'r')+'">'+(a.tot>=0?'+':'')+a.tot.toFixed(1)+'R</b></div></div>'}).join('')}
async function load(){
 let d;try{d=await (await fetch('/state')).json()}catch(e){$('br').innerHTML='<span class="dot warn"></span>সার্ভার জাগছে, ১ মিনিট পর্যন্ত অপেক্ষা করুন...';return}
 const R=d.results;bridge(d.bridge);live(d.live);corr(d.corr);hero(R,d);cards(R,d);
 $('st').textContent=d.status=='running'?'ব্যাকটেস্ট চলছে...':d.status=='error'?'সমস্যা: '+d.error:R.length?'ফল তৈরি':'ডেটার অপেক্ষা';
}
setInterval(load,3000);load();
</script>"""


@app.get("/")
def home():
    return PAGE
