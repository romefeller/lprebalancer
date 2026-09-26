"""CLMM cash-flow scenarios. This module cannot sign or send transactions."""
import math
import numpy as np

def amounts(L,p,lo,hi):
    if p<=lo: return L*(1/math.sqrt(lo)-1/math.sqrt(hi)),0.
    if p>=hi: return 0.,L*(math.sqrt(hi)-math.sqrt(lo))
    return L*(1/math.sqrt(p)-1/math.sqrt(hi)),L*(math.sqrt(p)-math.sqrt(lo))

def unit_value(p,lo,hi):
    x,y=amounts(1.,p,lo,hi)
    return x*p+y

def bounds(p,k,spacing=4):
    # SOL 9 decimals / USDC 6 decimals. Outward rounding to initialized ticks.
    scale=1e-3; step=math.log(1.0001)*spacing
    lo=math.exp(math.floor(math.log(p/k*scale)/step)*step)/scale
    hi=math.exp(math.ceil(math.log(p*k*scale)/step)*step)/scale
    return lo,hi

def run(bars,features,model,start,end,policy,capital=10000.,bps=10.,fixed=.10,
        downtime=1,latency=1,fee_mult=1.,fee_mode='strict',tvl=25e6,concentration=20.,
        fee_rate=.0004,lp_fraction=.8,tick_spacing=4,keep_path=False):
    rate=bps/1e4
    L=lo=hi=0.; width=policy.get('wide',policy.get('k',1.05))
    wallet_x=capital/(2*bars[start,4]); wallet_y=capital/2
    cash=debt=paid=retained=gross=costs=0.
    rebals=settlements=inside=crossings=narrow_bars=0
    last_action=start-10000; pending=None; reopen_at=None
    rows=[]; peak=capital; maxdd=0.; last_day=int(bars[start,0]//86400)

    def wealth(p):
        if L>0:
            x,y=amounts(L,p,lo,hi)
            return x*p+y+cash
        return wallet_x*p+wallet_y+cash

    def open_position(i,k,initial=False):
        nonlocal L,lo,hi,wallet_x,wallet_y,costs,debt,rebals,last_action,width
        p=bars[i,4]; value=wallet_x*p+wallet_y
        lo,hi=bounds(p,k,tick_spacing)
        uv=unit_value(p,lo,hi)
        target_x,_=amounts(max(value-fixed,0)/uv,p,lo,hi)
        swap=abs(target_x-wallet_x)*p
        charge=fixed+rate*swap
        if value<=charge:
            return False
        target_x,_=amounts((value-charge)/uv,p,lo,hi)
        charge=fixed+rate*abs(target_x-wallet_x)*p
        L=max(value-charge,0)/uv; wallet_x=wallet_y=0.
        costs+=charge; debt+=charge; width=k; last_action=i
        if not initial: rebals+=1
        return True

    def settle(i):
        nonlocal L,cash,debt,paid,retained,costs,settlements
        if L<=0 or cash<=fixed:
            return
        p=bars[i,4]; uv=unit_value(p,lo,hi)
        x,_=amounts(1.,p,lo,hi); a_fraction=x*p/uv
        available=cash-fixed; q=rate*a_fraction
        if available<=debt*(1+q):
            invest=available/(1+q); distribution=0.; repayment=invest
        else:
            invest=(available+debt)/(2+q)
            distribution=available-invest*(1+q); repayment=debt
        costs+=fixed+q*invest
        debt=max(0.,debt-repayment)
        retained+=invest-repayment; paid+=distribution
        L+=invest/uv; cash=0.; settlements+=1

    if not open_position(start,width,True):
        raise ValueError('Capital cannot pay initial opening cost.')
    for i in range(start+1,end):
        p=bars[i,4]
        if L>0:
            fully=bars[i,3]>=lo and bars[i,2]<=hi
            close_inside=lo<=p<=hi
            inside+=int(close_inside); crossings+=int(not fully)
            narrow_bars+=int(width<1.02)
            earn=fully if fee_mode=='strict' else close_inside
            if earn:
                pool_L=concentration*tvl/(2*math.sqrt(p))
                fee=bars[i,5]*fee_rate*lp_fraction*L/(pool_L+L)*fee_mult
                cash+=fee; gross+=fee
        if pending is not None and i>=pending[0]:
            wallet_x,wallet_y=amounts(L,p,lo,hi); L=0.
            width=pending[1]; reopen_at=i+downtime; pending=None
        if reopen_at is not None and i>=reopen_at:
            open_position(i,width)
            reopen_at=None
        day=int(bars[i,0]//86400)
        if day!=last_day:
            settle(i); last_day=day
        if i==end-1:
            settle(i)
        eq=wealth(p); peak=max(peak,eq); maxdd=max(maxdd,1-eq/peak)
        rows.append((int(bars[i,0]),eq,paid,gross,costs,rebals,width,retained))
        if L<=0 or pending is not None or reopen_at is not None or i==end-1:
            continue
        mode=policy['mode']; wide=policy.get('wide',policy.get('k',1.05))
        if mode=='fixed':
            target=policy['k']
        elif mode=='clock':
            target=1.01 if int(bars[i,0]//3600)%24 in model.quiet_hours else wide
        elif mode=='vol':
            target=1.01 if model.calm(i,False) else wide
        elif mode=='profile':
            target=1.01 if model.calm(i,True) else wide
        else:
            H=policy['H']; risk=policy['risk']; calm=model.calm(i,True)
            candidate=model.probability(i,H,math.log(1.01),math.log(1.01),True)
            held=model.probability(i,H,math.log(hi/p),math.log(p/lo),True)
            target=wide
            if width<1.02:
                if calm and held<=risk+.10: target=1.01
            elif calm and candidate<=risk:
                target=1.01
                if mode=='economic':
                    v=unit_value(p,*bounds(p,1.01,tick_spacing))
                    narrow_L=(eq-cash)/v
                    pool_L=concentration*tvl/(2*math.sqrt(p))
                    share_gain=max(0.,narrow_L/(pool_L+narrow_L)-L/(pool_L+L))
                    expected_extra=features['volume_hour'][i]*H/12*fee_rate*lp_fraction*fee_mult*share_gain
                    transition=2*fixed+rate*(eq-cash)
                    if expected_extra<=transition: target=wide
        outside=not(lo<=p<=hi)
        change=abs(target-width)>1e-8
        if (outside or change) and (outside or i-last_action>=6):
            pending=(i+latency,target)
    final=wealth(bars[min(i,end-1),4]); days=(bars[end-1,0]-bars[start,0])/86400
    result={'policy':policy['name'],'capital':capital,'days':days,'paid':paid,
            'retained_fees':retained,'ending_capital':final,'total_wealth':final+paid,
            'gross_fees':gross,'costs':costs,'unrecovered_costs':debt,'fee_reserve':cash,
            'rebalances':rebals,'settlements':settlements,'max_drawdown':maxdd,
            'close_in_range_fraction':inside/max(1,end-start-1),
            'crossing_bar_fraction':crossings/max(1,end-start-1),
            'narrow_fraction':narrow_bars/max(1,end-start-1),
            'gross_fee_apr_pct':gross/capital/days*36500,
            'spendable_apr_pct':paid/capital/days*36500}
    if keep_path: result['path']=rows
    return result
