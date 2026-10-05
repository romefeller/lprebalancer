"""Stability and arithmetic checks on the saved screening results."""
import datetime as dt
import gzip
import json
import math
import pathlib
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent
rows = json.loads((ROOT/'intervals.json').read_text())
result = json.loads((ROOT/'results.json').read_text())
# Fees and rewards are alternatives on Aerodrome, never add them.
for name, summaries in result['summaries'].items():
    if name.startswith('aero_'):
        for row in summaries:
            row['total_ratio'] = None
(ROOT/'results.json').write_text(json.dumps(result,indent=2))

def selected(r, policy):
    d = dt.datetime.fromtimestamp(r['start'],dt.UTC)
    return (policy=='notHOT' and r['mode'] in ['CALM','WARM'] or
            policy=='CALM' and r['mode']=='CALM' or
            policy=='weekend' and d.weekday()>=5)

rng = np.random.default_rng(6970)
out=[]
for name,policy,income in [('cetus_sui_5bp','notHOT','both'),('cetus_sui_25bp','notHOT','both'),
                           ('cetus_eth_25bp','notHOT','fee'),('bluefin_btc_20bp','weekend','fee'),
                           ('aero_v3','weekend','reward')]:
    allrows=rows[name];end=allrows[-1]['end'];start=end-30*86400;mid=end-15*86400
    rs=[r for r in allrows if r['start']>=start]
    Y=lambda r:r['fee'] if income=='fee' else r['reward'] if income=='reward' else r['fee']+r['reward']
    def ratio(a):
        a=[r for r in a if selected(r,policy)]
        return sum(Y(r) for r in a)/sum(r['g'] for r in a)
    # Resample contiguous three-day blocks, preserving within-block dependence.
    daily=np.zeros((30,2))
    selected_hours=0;weighted_margin=0
    for r in rs:
        if not selected(r,policy):continue
        day=min(29,int((r['start']-start)/86400))
        daily[day]+=[Y(r),r['g']]
        selected_hours+=(r['end']-r['start'])/3600
        width=r['width'] or 1.01
        weighted_margin+=(Y(r)-r['g'])/(1-width**-0.5)
    samples=[]
    for _ in range(3000):
        ix=np.concatenate([(np.arange(3)+rng.integers(0,30))%30 for _ in range(10)])
        y,g=daily[ix].sum(axis=0)
        if g>0:samples.append(y/g)
    o=dict(pool=name,policy=policy,income=income,ratio=ratio(rs),first_half=ratio([r for r in rs if r['start']<mid]),
           second_half=ratio([r for r in rs if r['start']>=mid]),block_bootstrap_95pct=list(np.quantile(samples,[.025,.975])),
           selected_hours=selected_hours,
           proxy_margin_usd_per_calendar_day_230=230*weighted_margin/30,
           proxy_margin_usd_per_calendar_day_2000=2000*weighted_margin/30)
    out.append(o)
    print(json.dumps(o))
(ROOT/'stability.json').write_text(json.dumps(out,indent=2))

# Check archived prices against the new independent retrieval.
old=np.array(json.loads(gzip.decompress((ROOT/'data/claude_panoptic_eth5m.json.gz').read_bytes())))
fresh=np.array(json.loads((ROOT/'data/ETHUSDT_5m.json').read_text()))
lookup={int(r[0]/1000):r[1] for r in old[:-1]}  # Original last candle was unfinished.
differences=[abs(r[4]/lookup[int(r[0])]-1) for r in fresh if int(r[0]) in lookup]
print('ETH matching candles',len(differences),'max relative price difference',max(differences))
assert max(differences)<1e-12
# Exact CL value at the center confirms the 201.4988 concentration factor.
p=100.;k=1.01;lo=p/k;hi=p*k
x=1/math.sqrt(p)-1/math.sqrt(hi);y=math.sqrt(p)-math.sqrt(lo)
lev=2*math.sqrt(p)/(x*p+y)
assert abs(lev-1/(1-k**-.5))<1e-8
print('Concentration multiplier',lev)
