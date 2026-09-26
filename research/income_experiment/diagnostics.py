"""Post-result diagnostics. No policy parameters are fitted or changed here."""
import json,math,csv
import numpy as np
from run_experiments import setup,run,OUT,table,bootstrap_blocks

b,e,f,m,train,start=setup()
selection=json.loads((OUT/'selection.json').read_text())
p=next(x for x in selection['policies'] if x['name']==selection['choices']['10000']['adaptive_selected'])
baseline={'name':'fixed_3','mode':'fixed','k':1.03}
rows=[]
for cap in (190.,500.,1000.,2000.,5000.,10000.):
    for label,bps,fixed in [('cheap',5.,.02),('base',10.,.10),('expensive',20.,.50)]:
        a=run(b,f,m,start,len(b),p,capital=cap,bps=bps,fixed=fixed,**e)
        z=run(b,f,m,start,len(b),baseline,capital=cap,bps=bps,fixed=fixed,**e)
        rows.append({'capital':cap,'case':label,'bps':bps,'fixed':fixed,'policy':p['name'],
                     'adaptive_paid':a['paid'],'fixed3_paid':z['paid'],'extra_paid':a['paid']-z['paid'],
                     'adaptive_ending_capital':a['ending_capital'],'fixed3_ending_capital':z['ending_capital']})
table('exploratory_capital_transfer.csv',rows)

ids=np.arange(start,len(b)-24)
calm=np.array([m.calm(i,True) for i in ids])
low=np.array([m.calm(i,False) for i in ids])
changes=np.diff(np.r_[False,calm,False].astype(int))
starts=np.flatnonzero(changes==1);ends=np.flatnonzero(changes==-1)
durations=(ends-starts)*5
hour=(b[ids,0].astype(int)//3600)%24
hours=[]
for h in range(24):
    ix=ids[hour==h]
    hours.append({'hour_utc':h,'training_quiet':h in m.quiet_hours,
                  'mean_sigma_pct':float(f['sigma'][ix].mean()*100),
                  'mean_hourly_volume':float(f['volume_hour'][ix].mean()),
                  'exit_30m':float(m.labels[6][ix].mean())})
table('test_hour_of_day.csv',hours)
diagnostics={'calm_origins':int(calm.sum()),'total_origins':len(ids),'calm_spells':len(starts),
             'median_calm_spell_minutes':float(np.median(durations)),
             'max_calm_spell_minutes':float(durations.max()),
             'calm_days':len(np.unique(b[ids[calm],0].astype(int)//86400)),
             'crossing_comparisons':[]}
rng=np.random.default_rng(993)
days=b[ids,0].astype(int)//86400
for H in (3,6,12,24):
    y=m.labels[H][ids]
    for label,mask in [('calm_vs_other',calm),('low_vol_vs_other',low)]:
        units=[]
        for day in np.unique(days):
            sel=days==day
            units.append([int((sel&mask).sum()),float(y[sel&mask].sum()),
                          int((sel&~mask).sum()),float(y[sel&~mask].sum())])
        a=np.asarray(units)
        sampled=a[rng.integers(0,len(a),(4000,len(a)))].sum(1)
        keep=(sampled[:,0]>0)&(sampled[:,2]>0)
        delta=sampled[keep,1]/sampled[keep,0]-sampled[keep,3]/sampled[keep,2]
        diagnostics['crossing_comparisons'].append({
            'horizon_minutes':H*5,'comparison':label,
            'exit_difference':float(y[mask].mean()-y[~mask].mean()),
            'daily_block_ci':np.quantile(delta,[.025,.975]).tolist()})
(OUT/'diagnostics.json').write_text(json.dumps(diagnostics,indent=2))
print(json.dumps(diagnostics,indent=2))
print('CAPITAL TRANSFER')
for x in rows:print(x['capital'],x['case'],round(x['extra_paid'],4))
