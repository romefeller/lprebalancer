"""Render archived results. Install matplotlib separately if unavailable."""
import sys,pathlib,json,csv,datetime
try:
    import matplotlib
except ImportError:
    sys.path.insert(0,'/tmp/lp-research-plot')
    import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
ROOT=pathlib.Path(__file__).resolve().parent
R=ROOT/'results'
def read(name):return list(csv.DictReader((R/name).open()))
paths=json.loads((R/'paths.json').read_text())['10000']
surv=read('conditional_survival.csv')
colors={'fixed_1':'#bd5636','fixed_3':'#365d86','vol_3':'#008873'}
labels={'fixed_1':'Fixed 1%','fixed_3':'Fixed 3%','vol_3':'Adaptive 1% / 3%'}
plt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False,
                     'axes.titleweight':'bold','figure.facecolor':'white'})
fig,axs=plt.subplots(2,2,figsize=(13,9),layout='constrained')
for name,label,color in [('all','All observations','#6f7680'),('low_volatility','Low volatility','#365d86'),('calm_profile','Calm profile','#008873')]:
    rows=[r for r in surv if r['group']==name]
    axs[0,0].plot([int(r['horizon_minutes']) for r in rows],
                  [100*(1-float(r['exit_rate'])) for r in rows],marker='o',label=label,color=color)
axs[0,0].set(title='Observed survival of a new 1% band',xlabel='Horizon (minutes)',ylabel='No crossing (%)',xticks=[15,30,60,120],ylim=(25,102))
axs[0,0].legend(frameon=False)
test=read('test.csv')
names=['fixed_1','fixed_3','vol_3']
rr=[next(r for r in test if r['capital']=='10000.0' and r['policy']==name) for name in names]
xx=np.arange(3)
axs[0,1].bar(xx-.2,[float(r['gross_fees']) for r in rr],width=.4,label='Fees before operating costs',color='#aac0ce')
axs[0,1].bar(xx+.2,[float(r['paid']) for r in rr],width=.4,label='Spendable dividend',color='#008873')
axs[0,1].set(title='Modeled fees and dividends on $10,000',ylabel='USD over 18 days',xticks=xx,xticklabels=[labels[n] for n in names])
axs[0,1].legend(frameon=False)
for name in names:
    a=paths[name][::12]
    if a[-1]!=paths[name][-1]:a.append(paths[name][-1])
    dates=[datetime.datetime.fromtimestamp(r[0],datetime.timezone.utc) for r in a]
    axs[1,0].plot(dates,[r[2] for r in a],label=labels[name],color=colors[name])
    axs[1,1].plot(dates,[r[1] for r in a],label=labels[name],color=colors[name])
axs[1,0].set(title='Modeled cumulative dividends',ylabel='Spendable USD')
axs[1,1].set(title='Modeled remaining capital after payouts',ylabel='USD')
axs[1,1].axhline(10000,color='#aaa',linestyle='--',linewidth=1)
for ax in axs[1]:
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter('%b %d'))
    ax.tick_params(axis='x',rotation=25)
    ax.legend(frameon=False)
for ax in axs.flat:ax.grid(axis='y',alpha=.18)
fig.suptitle('Calm-period LP experiments | Held-out period: September 7–25, 2026\nMeasured survival; cash flows depend on fee-density and execution assumptions',fontsize=14)
for ext in ('png','svg','pdf'):fig.savefig(R/f'experiment_summary.{ext}',dpi=170)
plt.close(fig)
fig,ax=plt.subplots(figsize=(9,5),layout='constrained')
rows=read('exploratory_capital_transfer.csv')
for case,color in [('cheap','#008873'),('base','#365d86'),('expensive','#bd5636')]:
    rr=[r for r in rows if r['case']==case]
    ax.plot([float(r['capital']) for r in rr],[float(r['extra_paid']) for r in rr],marker='o',label=case,color=color)
ax.axhline(0,color='#777',linewidth=1)
ax.set_xscale('log')
ax.set(title='Exploratory transfer: adaptive 1% / 3% versus fixed 3%',xlabel='Initial capital (USD, log scale)',ylabel='Extra modeled spendable income over 18 days (USD)')
ax.grid(alpha=.2);ax.legend(frameon=False,title='Execution costs')
fig.text(.1,-.04,'Cheap: 5 bps + $0.02. Base: 10 bps + $0.10. Expensive: 20 bps + $0.50 per action.',fontsize=9)
for ext in ('png','svg','pdf'):fig.savefig(R/f'capital_sensitivity.{ext}',dpi=170,bbox_inches='tight')
print('Figures saved.')
