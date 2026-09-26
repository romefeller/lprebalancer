"""Causal features and empirical first-passage estimates for offline research."""
import math
import numpy as np

def ewma(x, half_life):
    out = np.empty(len(x)); out[0] = x[0]
    decay = 2 ** (-1 / half_life)
    for i in range(1, len(x)):
        out[i] = decay * out[i-1] + (1-decay) * x[i]
    return out

def rolling_mean(x, window):
    return np.convolve(x, np.ones(window)/window, mode='full')[:len(x)]

def features(bars):
    lp = np.log(bars[:,4]); r = np.diff(lp, prepend=lp[0])
    sigma = np.sqrt(np.maximum(ewma(r*r,12),1e-14))
    g = np.zeros(len(bars)); g[6:] = np.log(sigma[6:]/sigma[:-6])/.5
    ds = np.diff(np.log(sigma),prepend=np.log(sigma[0]))
    instability = np.sqrt(np.maximum(rolling_mean(ds*ds,12)-rolling_mean(ds,12)**2,0))
    return {'sigma':sigma,'velocity':g,'instability':instability,
            'volume_hour':rolling_mean(bars[:,5],12)*12}

def future_extrema(bars, H):
    up = np.zeros(len(bars)); down = np.zeros(len(bars))
    close = bars[:,4]
    for h in range(1,H+1):
        up[:-h] = np.maximum(up[:-h], np.log(bars[h:,2]/close[:-h]))
        down[:-h] = np.maximum(down[:-h], np.log(close[:-h]/bars[h:,3]))
    up[-H:] = np.nan; down[-H:] = np.nan
    return up,down

class Model:
    def __init__(self,bars,f,train_end):
        self.f = f
        self.train = np.arange(864,train_end-24)
        if len(self.train)<1000:
            raise ValueError('Insufficient training data after warmup.')
        self.vol_cut = float(np.quantile(f['sigma'][self.train],.4))
        self.vel_cut = float(np.median(np.abs(f['velocity'][self.train])))
        self.inst_cut = float(np.median(f['instability'][self.train]))
        self.vel_edges = np.quantile(f['velocity'][self.train],[1/3,2/3])
        self.group = np.searchsorted(self.vel_edges,f['velocity'])
        hours = (bars[:,0].astype(int)//3600)%24
        quiet = sorted(range(24),key=lambda h:np.mean(f['sigma'][self.train[hours[self.train]==h]]))[:6]
        self.quiet_hours = quiet
        self.edges = np.r_[np.linspace(0,30,301),np.inf]
        self.tables = {}; self.base = {}; self.labels = {}
        for H in (3,6,12,24):
            up,dn = future_extrema(bars,H)
            self.labels[H] = ((up>math.log(1.01))|(dn>math.log(1.01))).astype(float)
            self.labels[H][-H:] = np.nan
            self.base[H] = float(self.labels[H][self.train].mean())
            for group in (-1,0,1,2):
                ids=self.train if group==-1 else self.train[self.group[self.train]==group]
                a=up[ids]/f['sigma'][ids]; b=dn[ids]/f['sigma'][ids]
                hist,_,_=np.histogram2d(a,b,bins=(self.edges,self.edges))
                self.tables[H,group]=hist.cumsum(0).cumsum(1)/len(ids)

    def probability(self,i,H,up,down,velocity=True):
        if up<=0 or down<=0:
            return 1.0
        a=up/self.f['sigma'][i]; b=down/self.f['sigma'][i]
        # Count only cells wholly below the threshold: conservative grid approximation.
        ia=min(np.searchsorted(self.edges,a,side='right')-2,len(self.edges)-2)
        ib=min(np.searchsorted(self.edges,b,side='right')-2,len(self.edges)-2)
        if ia<0 or ib<0:
            return 1.0
        group=int(self.group[i]) if velocity else -1
        return float(1-self.tables[H,group][ia,ib])

    def calm(self,i,with_velocity=True):
        if self.f['sigma'][i]>self.vol_cut:
            return False
        return not with_velocity or (abs(self.f['velocity'][i])<=self.vel_cut and
                                      self.f['instability'][i]<=self.inst_cut)

    def config(self):
        return {'volatility_40pct':self.vol_cut,'absolute_velocity_median':self.vel_cut,
                'instability_median':self.inst_cut,'velocity_terciles':self.vel_edges.tolist(),
                'quiet_hours_utc':self.quiet_hours,'training_origins':len(self.train)}
