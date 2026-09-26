"""Run the frozen train/validation/test research suite. No trading or database I/O."""
import csv
import datetime as dt
import hashlib
import json
import math
import pathlib
import sys
import numpy as np
from models import Model,features
from simulator import run

ROOT=pathlib.Path(__file__).resolve().parent
OUT=ROOT/'results'
OUT.mkdir(exist_ok=True)
iso=lambda ts:dt.datetime.fromtimestamp(float(ts),dt.timezone.utc).isoformat()

def dump(name,obj):
    (OUT/name).write_text(json.dumps(obj,indent=2,allow_nan=False))

def table(name,rows):
    if not rows:return
    keys=list(rows[0])
    with (OUT/name).open('w') as f:
        w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(rows)

def bootstrap_blocks(values,block=1,reps=2000):
    values=np.asarray(values); n=len(values)
    if n<4:return [None,None]
    rng=np.random.default_rng(61025)
    blocks=np.array([values[(i+np.arange(block))%n] for i in range(n)])
    draws=rng.integers(0,n,(reps,math.ceil(n/block)))
    means=blocks[draws].reshape(reps,-1)[:,:n].mean(1)
    return np.quantile(means,[.025,.975]).tolist()

def daily_means(ts,values):
    days=ts.astype(int)//86400
    return np.array([np.mean(values[days==d]) for d in np.unique(days)])

def load():
    bars=np.asarray(json.loads((ROOT/'data/candles_5m.json').read_text()),dtype=float)
    if bars.ndim!=2 or bars.shape[1]!=6 or len(bars)<5000:raise ValueError('Insufficient or malformed candles.')
    if not np.isfinite(bars).all() or (bars[:,1:5]<=0).any() or (bars[:,5]<0).any():raise ValueError('Invalid candle values.')
    gaps=np.where(np.diff(bars[:,0])!=300)[0]
    if len(gaps):raise ValueError(f'Nonconsecutive data: {len(gaps)} gaps. Do not manufacture missing bars.')
    if (bars[:,2]<bars[:,1:5].max(1)).any() or (bars[:,3]>bars[:,1:5].min(1)).any():raise ValueError('Invalid OHLC relationships.')
    snapshot=json.loads((ROOT/'data/orca_pool.json').read_text())
    d=snapshot.get('data',snapshot)
    L=float(d['liquidity'])/10**7.5
    tvl=float(d['tvlUsdc']); p=float(d['price'])
    economics={'tvl':tvl,'concentration':2*L*math.sqrt(p)/tvl,
               'fee_rate':float(d['feeRate'])/1e6,'lp_fraction':1-float(d['protocolFeeRate'])/1e4,
               'tick_spacing':int(d['tickSpacing'])}
    return bars,economics

def policies():
    out=[{'name':f'fixed_{w}','mode':'fixed','k':1+w/100} for w in (1,3,5,8)]
    for mode in ('clock','vol','profile'):
        for wide in (3,5):
            out.append({'name':f'{mode}_{wide}','mode':mode,'wide':1+wide/100})
    for mode in ('survival','economic'):
        for wide in (3,5):
            for H in (6,12):
                for risk in (.05,.15):
                    out.append({'name':f'{mode}_{wide}_{H*5}m_{int(risk*100)}',
                                'mode':mode,'wide':1+wide/100,'H':H,'risk':risk})
    return out

def setup():
    b,e=load(); f=features(b); n=len(b); train=int(n*.6); valid=int(n*.8)
    m=Model(b,f,train)
    return b,e,f,m,train,valid

def select():
    b,e,f,m,train,valid=setup()
    stats={'candles':len(b),'days':(b[-1,0]-b[0,0])/86400,'first':iso(b[0,0]),
           'last':iso(b[-1,0]),'validation_start':iso(b[train,0]),'test_start':iso(b[valid,0]),
           'economics':e,'feature_thresholds':m.config(),
           'dataset_sha256':hashlib.sha256((ROOT/'data/candles_5m.json').read_bytes()).hexdigest(),
           'implementation_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [ROOT/'models.py',ROOT/'simulator.py']}}
    dump('metadata.json',stats)
    choices={}; records=[]
    for cap in (190.,10000.):
        allrows=[]
        for p in policies():
            r=run(b,f,m,train,valid,p,capital=cap,**e)
            r['eligible']=r['ending_capital']>=cap*.95 and r['max_drawdown']<=.15
            allrows.append(r);records.append(r)
        byname={p['name']:p for p in policies()}
        eligible=[r for r in allrows if r['eligible']]
        adaptive=[r for r in eligible if byname[r['policy']]['mode']!='fixed']
        choices[str(int(cap))]={
            'selected':max(eligible,key=lambda r:r['paid'])['policy'] if eligible else None,
            'adaptive_selected':max(adaptive,key=lambda r:r['paid'])['policy'] if adaptive else None,
            'unconstrained_income_leader':max(allrows,key=lambda r:r['paid'])['policy'],
            'eligible_count':len(eligible)}
        print('VALIDATION',cap,choices[str(int(cap))],flush=True)
    dump('selection.json',{'choices':choices,'policies':policies(),'metadata':stats})
    table('validation.csv',records)
    print('Selection frozen before test evaluation.',flush=True)

def prediction_test(b,f,m,start):
    output=[];cal=[];groups=[];n=len(b)
    for H in (3,6,12,24):
        ids=np.arange(start,n-H);y=m.labels[H][ids]
        pred={'unconditional':np.full(len(ids),m.base[H]),
              'volatility':np.array([m.probability(i,H,math.log(1.01),math.log(1.01),False) for i in ids]),
              'volatility_velocity':np.array([m.probability(i,H,math.log(1.01),math.log(1.01),True) for i in ids])}
        baseerr=(pred['unconditional']-y)**2
        volerr=(pred['volatility']-y)**2
        for name,p in pred.items():
            err=(p-y)**2
            ci=bootstrap_blocks(daily_means(b[ids,0],err-baseerr))
            vci=bootstrap_blocks(daily_means(b[ids,0],err-volerr))
            output.append({'horizon_minutes':H*5,'model':name,'n':len(ids),
                           'observed_exit_rate':float(y.mean()),'mean_prediction':float(p.mean()),
                           'brier':float(err.mean()),'brier_delta_vs_unconditional':float((err-baseerr).mean()),
                           'daily_bootstrap_delta_low':ci[0],'daily_bootstrap_delta_high':ci[1],
                           'brier_delta_vs_volatility':float((err-volerr).mean()),
                           'velocity_delta_low':vci[0],'velocity_delta_high':vci[1]})
            for lo,hi in ((0,.05),(.05,.15),(.15,.30),(.30,.60),(.60,1.000001)):
                mask=(p>=lo)&(p<hi)
                if mask.sum():
                    cal.append({'horizon_minutes':H*5,'model':name,'bin_low':lo,'bin_high':hi,
                                'n':int(mask.sum()),'prediction':float(p[mask].mean()),'observed':float(y[mask].mean())})
        vol=np.array([m.calm(i,False) for i in ids]);full=np.array([m.calm(i,True) for i in ids])
        clock=np.isin((b[ids,0].astype(int)//3600)%24,m.quiet_hours)
        for name,mask in [('all',np.ones(len(ids),dtype=bool)),('low_volatility',vol),
                          ('calm_profile',full),('training_quiet_hours',clock),
                          ('outside_calm_profile',~full)]:
            if mask.sum():
                groups.append({'horizon_minutes':H*5,'group':name,'n':int(mask.sum()),
                               'exit_rate':float(y[mask].mean()),
                               'mean_hourly_volume_usd':float(f['volume_hour'][ids][mask].mean())})
    table('prediction.csv',output);table('calibration.csv',cal);table('conditional_survival.csv',groups)

def daily_payouts(path):
    totals={}
    for row in path: totals[int(row[0]//86400)]=row[2]
    prev=0;result={}
    for d,total in totals.items():
        result[d]=total-prev;prev=total
    return result

def evaluate():
    b,e,f,m,train,start=setup()
    frozen=json.loads((OUT/'selection.json').read_text())
    checksum=hashlib.sha256((ROOT/'data/candles_5m.json').read_bytes()).hexdigest()
    if checksum!=frozen['metadata']['dataset_sha256']:raise ValueError('Data changed after selection.')
    for p in (ROOT/'models.py',ROOT/'simulator.py'):
        if hashlib.sha256(p.read_bytes()).hexdigest()!=frozen['metadata']['implementation_sha256'][p.name]:
            raise ValueError('Model or simulator changed after selection.')
    prediction_test(b,f,m,start)
    rows=[];sensitivity=[];comparisons=[];allpaths={}
    for cap in (190.,10000.):
        paths={}
        for p in frozen['policies']:
            r=run(b,f,m,start,len(b),p,capital=cap,keep_path=True,**e)
            paths[p['name']]=r.pop('path');rows.append(r)
        selected=frozen['choices'][str(int(cap))]['adaptive_selected']
        wanted=list(dict.fromkeys(['fixed_1','fixed_3','fixed_5']+([selected] if selected else [])))
        byname={p['name']:p for p in frozen['policies']}
        cases=[]
        for bps in (5.,10.,20.):
            for fixed in (.02,.10,.50):
                for fee_mult in (.5,1.,2.):
                    cases.append({'bps':bps,'fixed':fixed,'fee_mult':fee_mult,'downtime':1,'fee_mode':'strict'})
        cases += [{'bps':10.,'fixed':.1,'fee_mult':1.,'downtime':3,'fee_mode':'strict'},
                  {'bps':10.,'fixed':.1,'fee_mult':1.,'downtime':1,'fee_mode':'close'}]
        for ci,case in enumerate(cases):
            for name in wanted:
                r=run(b,f,m,start,len(b),byname[name],capital=cap,**case,**e)
                sensitivity.append({**case,**r})
            if ci%10==0:print('TEST sensitivity',cap,ci+1,'/',len(cases),flush=True)
        if selected:
            a=daily_payouts(paths[selected])
            for baseline in ('fixed_1','fixed_3','fixed_5'):
                bb=daily_payouts(paths[baseline]);days=sorted(set(a)&set(bb))
                # Exclude partial boundary days from uncertainty calculations.
                days=days[1:-1]
                diff=np.array([a[d]-bb[d] for d in days])
                ci=bootstrap_blocks(diff,block=2)
                comparisons.append({'capital':cap,'adaptive':selected,'baseline':baseline,
                                    'full_days':len(days),'mean_daily_extra_paid':float(diff.mean()),
                                    'paired_two_day_bootstrap_low':ci[0],'paired_two_day_bootstrap_high':ci[1]})
        allpaths[str(int(cap))]={name:paths[name] for name in wanted}
    table('test.csv',rows);table('sensitivity.csv',sensitivity);table('paired_income.csv',comparisons)
    dump('paths.json',allpaths)
    print('Test evaluation complete.',flush=True)

if __name__=='__main__':
    if sys.argv[1]=='select':select()
    elif sys.argv[1]=='evaluate':evaluate()
    else:raise SystemExit('Use select or evaluate.')
