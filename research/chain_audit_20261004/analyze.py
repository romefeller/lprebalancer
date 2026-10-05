"""Independent audit of fee/reward arithmetic and lagged regime conditioning.

Ratios are screening statistics against a variance proxy, NOT realized P&L.
Hourly global counters cannot reproduce exact earnings of a finite tick range.
"""
import collections
import datetime as dt
import hashlib
import gzip
import importlib.util
import json
import math
import pathlib
import sys
import types

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / 'data'
def old_text(relative):
    return gzip.decompress((DATA / ('claude_' + relative.replace('/', '_') + '.gz')).read_bytes()).decode()
sys.modules['engine'] = types.ModuleType('engine')
spec = importlib.util.spec_from_file_location('audit_calm', ROOT.parents[1] / 'calm.py')
calm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(calm)
Q128, Q64, Q96 = 2**128, 2**64, 2**96

def iso(t):
    return dt.datetime.fromtimestamp(t, dt.UTC).isoformat()

def timestamp(s):
    return dt.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()

def load_candles(symbol):
    a = np.asarray(json.loads((DATA / f'{symbol}_5m.json').read_text()), float)
    assert np.all(np.diff(a[:, 0]) == 300), symbol
    return a

CANDLES = {s: load_candles(s) for s in ['SUIUSDT', 'ETHUSDT', 'BTCUSDT', 'SOLUSDT']}
oldeth = np.asarray(json.loads(old_text('panoptic/eth5m.json')), float)
ETH = np.c_[oldeth[:, 0] / 1000, oldeth[:, 1], oldeth[:, 2], oldeth[:, 3], oldeth[:, 1], np.zeros(len(oldeth))]
# The old download included its unfinished last candle. Replace the overlap
# with newly retrieved, fully closed candles and extend coverage to 18:00 UTC.
ETH = np.concatenate([ETH[ETH[:,0] < CANDLES['ETHUSDT'][0,0]], CANDLES['ETHUSDT']])
oldkl = json.loads(old_text('sui/kl.json'))
META = json.loads(old_text('sui/meta.json'))

def variance(a, start, end):
    # Candle timestamps are OPEN times; a close is observable 300 seconds later.
    times = a[:, 0] + 300
    if end > times[-1] or start < times[0]:
        return None
    i = np.searchsorted(times, start, side='right')
    j = np.searchsorted(times, end, side='right')
    if i < 1 or j <= i:
        return None
    return float(np.square(np.diff(np.log(a[i-1:j, 4]))).sum() / 8)

def mode(a, start):
    j = np.searchsorted(a[:, 0] + 300, start, side='right')
    b = a[max(0, j-2880):j]
    if len(b) < 2880:
        return None, None
    tape = tuple(b[:, i] for i in range(6))
    p = b[-1, 4]
    v = calm.regime_view(tape, p, p / 1.01, p * 1.01)
    return v['mode'], v['choice']

def summarize(rows, mutually_exclusive=False):
    end = max(r['end'] for r in rows)
    output = []
    for window in [365, 165, 90, 30, 7]:
        available = (end - rows[0]['start']) / 86400
        if window > available + 1 and window != 365:
            continue
        active = [r for r in rows if r['start'] >= end-window*86400]
        for group in ['all', 'CALM', 'WARM', 'HOT', 'notHOT', 'weekend', 'weekday', '00-08UTC']:
            rs = []
            for r in active:
                d = dt.datetime.fromtimestamp(r['start'], dt.UTC)
                include = (group == 'all' or group == r['mode'] or
                           (group == 'notHOT' and r['mode'] in ('CALM', 'WARM')) or
                           (group == 'weekend' and d.weekday() >= 5) or
                           (group == 'weekday' and d.weekday() < 5) or
                           (group == '00-08UTC' and d.hour < 8))
                if include:
                    rs.append(r)
            if not rs:
                continue
            fee, reward, g = [sum(r[k] for r in rs) for k in ['fee', 'reward', 'g']]
            days = sum(r['end']-r['start'] for r in rs) / 86400
            output.append(dict(window=window, group=group, observations=len(rs), exposure_days=days,
                               fee_ratio=fee/g, reward_ratio=reward/g, total_ratio=None if mutually_exclusive else (fee+reward)/g,
                               fee_1pct_pct_per_exposure_day=fee/days/(1-1/math.sqrt(1.01))*100,
                               variance_per_exposure_day=g*8/days))
    return output

def aero():
    raw = sorted([json.loads(line) for line in old_text('aero/samples.jsonl').splitlines()], key=lambda r: r['t'])
    h = json.loads(old_text('panoptic/regime_h.json'))
    ht = np.array([r[0] for r in h])
    out = collections.defaultdict(list)
    def reward_counter(p, t):
        return p[6] + (min(p[4]*(t-p[7]), p[5])*Q128//p[3] if p[3] > 0 and p[5] > 0 and t > p[7] else 0)
    for a, b in zip(raw, raw[1:]):
        start, end = a['t'], b['t']
        if end-start != 3600:
            continue
        g = variance(ETH, start, end)
        if g is None:
            continue
        j = np.searchsorted(ht, start, side='right')-1
        md, width = (h[j][1], 1+h[j][2]/100) if j >= 0 else (None, None)
        for name in ['v3', 'i100']:
            p, q = a[name], b[name]
            if not p or not q or p[2] <= 0:
                continue
            # USD value of one raw liquidity unit: 2 sqrt(raw USDC/WETH) / 1e6.
            sqrtp = q[0] / Q96
            eth_usd = sqrtp**2 * 1e12
            full_usd = 2 * sqrtp / 1e6
            f0 = ((q[8]-p[8]) % (1<<256)) / Q128 / 1e18
            f1 = ((q[9]-p[9]) % (1<<256)) / Q128 / 1e6
            f = (f0*eth_usd+f1)/full_usd
            reward = ((reward_counter(q,end)-reward_counter(p,start)) % (1<<256))/Q128/1e18*b['aero']/full_usd
            out[name].append(dict(start=start,end=end,fee=f,reward=reward,g=g,mode=md,width=width))
    return out

def token_price(sym, t):
    if sym == 'USDC':
        return 1.
    symbol = {'xBTC':'BTC', 'LBTC':'BTC'}.get(sym, sym) + 'USDT'
    v = np.array(oldkl[symbol], float)
    j = np.searchsorted(v[:,0]/1000+300, t, side='right')-1
    return float(v[max(0,j),1])

def sui():
    raw = sorted([json.loads(line) for line in (DATA / 'sui_hourly.jsonl').open()], key=lambda r:r['checkpoint'])
    out = collections.defaultdict(list)
    symbol = {'cetus_sui_5bp':'SUIUSDT','cetus_sui_25bp':'SUIUSDT','cetus_eth_25bp':'ETHUSDT','bluefin_btc_20bp':'BTCUSDT'}
    for ix, (a,b) in enumerate(zip(raw,raw[1:])):
        start,end = timestamp(a['timestamp']),timestamp(b['timestamp'])
        if not 1800 < end-start < 5400:
            continue
        modes = {s:mode(CANDLES[s],start) for s in set(symbol.values())}
        for name,sym in symbol.items():
            p,q = a['pools'][name],b['pools'][name]
            sqrtp = math.sqrt(int(p['current_sqrt_price'])*int(q['current_sqrt_price']))/Q64
            price_raw = sqrtp**2
            suff = '_coin_' if name.startswith('bluefin') else '_'
            f0 = ((int(q['fee_growth_global'+suff+'a'])-int(p['fee_growth_global'+suff+'a'])) % (1<<128))/Q64
            f1 = ((int(q['fee_growth_global'+suff+'b'])-int(p['fee_growth_global'+suff+'b'])) % (1<<128))/Q64
            fee = (f0*price_raw+f1)/(2*sqrtp)
            # Cetus objects here have USDC as A, volatile B. Bluefin BTC has USDC as B.
            b_sym = {'SUIUSDT':'SUI','ETHUSDT':'ETH','BTCUSDT':'USDC'}[sym]
            b_dec = {'SUI':9,'ETH':8,'USDC':6}[b_sym]
            # Read authoritative decimals from the original saved object metadata.
            if name == 'cetus_eth_25bp':
                eth_meta = [m for m in META.values() if m['symbol']=='ETH']
                b_dec = eth_meta[0]['decimals']
            b_usd = token_price(b_sym,end)/10**b_dec
            def rewards(obj):
                if 'rewarder_manager' in obj:
                    return {x['reward_coin']:int(x['growth_global']) for x in obj['rewarder_manager']['rewarders']}
                return {x['reward_coin_type']:int(x['reward_growth_global']) for x in obj['reward_infos']}
            rp,rq = rewards(p),rewards(q)
            reward = 0.
            for token,new in rq.items():
                if token not in rp:
                    continue
                meta = META.get(token if token.startswith('0x') else '0x'+token)
                if not meta or meta['symbol'] not in ['SUI','CETUS','USDC','ETH','xBTC','LBTC']:
                    continue
                growth = ((new-rp[token]) % (1<<128))/Q64
                reward += growth/10**meta['decimals']*token_price(meta['symbol'],end)/b_usd/(2*sqrtp)
            g = variance(CANDLES[sym],start,end)
            md,width = modes[sym]
            out[name].append(dict(start=start,end=end,fee=fee,reward=reward,g=g,mode=md,width=width))
        if ix % 200 == 0:
            print('analyzed Sui hours',ix,flush=True)
    return out

def seasonality():
    result = {}
    start = timestamp('2026-09-04T18:00:00Z')
    end = timestamp('2026-10-04T18:00:00Z')
    for sym,a in CANDLES.items():
        times = a[1:,0]+300
        squared = np.diff(np.log(a[:,4]))**2
        dates = [dt.datetime.fromtimestamp(t,dt.UTC) for t in times]
        base = (times>start)&(times<=end)
        week = np.array([d.weekday()>=5 for d in dates])
        night = np.array([d.hour<8 for d in dates])
        result[sym] = {'weekend_to_weekday_variance':float(squared[base&week].mean()/squared[base&~week].mean()),
                       '00_08_to_08_24_variance':float(squared[base&night].mean()/squared[base&~night].mean())}
    return result

if __name__ == '__main__':
    sui_rows = ({k:v for k,v in json.loads((ROOT/'intervals.json').read_text()).items() if not k.startswith('aero_')}
                if '--reuse-sui' in sys.argv else sui())
    pools = {**{'aero_'+k:v for k,v in aero().items()},**sui_rows}
    (ROOT/'intervals.json').write_text(json.dumps(pools))
    output = {'summaries':{k:summarize(v,k.startswith('aero_')) for k,v in pools.items()},'seasonality':seasonality(),
              'windows':{k:{'start':iso(v[0]['start']),'end':iso(v[-1]['end']),'count':len(v)} for k,v in pools.items()},
              'source_sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [DATA/'claude_aero_samples.jsonl.gz',DATA/'claude_panoptic_eth5m.json.gz',DATA/'sui_hourly.jsonl',ROOT.parents[1]/'calm.py']}}
    (ROOT/'results.json').write_text(json.dumps(output,indent=2))
    for name, summaries in output['summaries'].items():
        print('\n',name,output['windows'][name])
        for r in summaries:
            if r['window']==30:
                print(r['group'],r['observations'],'fee/G',round(r['fee_ratio'],3),'reward/G',round(r['reward_ratio'],3),'total/G',None if r['total_ratio'] is None else round(r['total_ratio'],3))
    print('seasonality',output['seasonality'])
