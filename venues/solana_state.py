"""Pool state read from Solana accounts: the Raydium layout (Raydium,
Byreal, PancakeSwap) and Orca's fee counters, rewards and liquidity."""

import base64
import json
import math
import os
import urllib.request

from venues.api import NULL_MINT
from venues.jupiter import prices as jupiter_api


# --- on-chain CLMM state (Raydium layout, also Byreal's) ----------------------

def _rpc_url():
    return (os.environ.get('LPBOT_RPC') or os.environ.get('SOLANA_RPC_URL')
            or 'https://api.mainnet-beta.solana.com')


def pool_accounts(addresses):
    """Raw account bytes for up to 100 pools in one RPC call."""
    out = {}
    for i in range(0, len(addresses), 100):
        chunk = addresses[i:i + 100]
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'getMultipleAccounts',
                           'params': [chunk, {'encoding': 'base64'}]}).encode()
        # In-process, not through curl: the RPC URL can carry an API key, and a
        # curl argv shows it to every `ps` (security review, 2026-09-26).
        try:
            req = urllib.request.Request(_rpc_url(), data=body, headers={'content-type': 'application/json'})
            with urllib.request.urlopen(req, timeout=40) as resp:
                vals = json.loads(resp.read())['result']['value']
        except Exception:
            continue
        for addr, v in zip(chunk, vals):
            if v and v.get('data'):
                out[addr] = base64.b64decode(v['data'][0])
    return out


_B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def b58(b):
    n = int.from_bytes(b, 'big')
    out = ''
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return '1' * (len(b) - len(b.lstrip(b'\0'))) + out


def decode_clmm_state(raw):
    """Raydium's PoolState, which Byreal and PancakeSwap share: after the
    8-byte discriminator come a bump, then amm_config, owner, the two mints,
    the two vaults and the observation key, then the fields the fee model
    needs. Verified against the API price on all three DEXes."""
    o = 8 + 1 + 32 * 7
    if len(raw) < o + 40:
        return None
    dec0, dec1 = raw[o], raw[o + 1]
    tick_spacing = int.from_bytes(raw[o + 2:o + 4], 'little')
    liquidity = int.from_bytes(raw[o + 4:o + 20], 'little')
    sqrt_price = int.from_bytes(raw[o + 20:o + 36], 'little')
    tick = int.from_bytes(raw[o + 36:o + 40], 'little', signed=True)
    price = (sqrt_price / 2 ** 64) ** 2 * 10 ** (dec0 - dec1)
    return {'decimals_a': dec0, 'decimals_b': dec1, 'tick_spacing': tick_spacing,
            'liquidity_raw': liquidity, 'tick': tick, 'price': price,
            'liquidity': liquidity / math.sqrt(10 ** dec0 * 10 ** dec1),
            'amm_config': b58(raw[9:41]),
            'mint_a': b58(raw[73:105]), 'mint_b': b58(raw[105:137])}


REWARD_INFOS_OFFSET = 8 + 1 + 32 * 7 + 40 + 2 + 2 + 16 * 2 + 8 * 2 + 16 * 4 + 1 + 7   # 397


REWARD_INFO_LEN = 169


def decode_rewards(raw, now=None):
    """The live reward programs of a Raydium-layout pool (Raydium, Byreal,
    PancakeSwap all share it): up to three RewardInfo slots after the swap
    counters. A slot is live while open_time <= now < end_time and it emits.
    Returns [(mint, emissions per second in RAW units)]. Verified on chain:
    PancakeSwap SOL/USDC pays CAKE, Raydium SOL/USDC's RAY program ended."""
    import time as _t
    now = now or _t.time()
    out = []
    for i in range(3):
        b = raw[REWARD_INFOS_OFFSET + i * REWARD_INFO_LEN: REWARD_INFOS_OFFSET + (i + 1) * REWARD_INFO_LEN]
        if len(b) < REWARD_INFO_LEN or b[0] == 0:
            continue
        open_t = int.from_bytes(b[1:9], 'little'); end_t = int.from_bytes(b[9:17], 'little')
        eps = int.from_bytes(b[25:41], 'little') / 2 ** 64
        mint = b58(b[57:89])
        if eps > 0 and open_t <= now < end_t and mint != NULL_MINT:
            out.append((mint, eps))
    return out


# --- fee counters: what liquidity at the active price actually earned -----------
#
# Both layouts keep the pool's cumulative fees per unit of liquidity, per
# token, as a Q64 fixed-point number that only grows (it wraps at 2^128).
# Raydium's PoolState (Raydium, Byreal, PancakeSwap): after tick_current and
# two u16 paddings. Orca's Whirlpool: fee_growth_global_a after vault_a,
# fee_growth_global_b after vault_b. Verified on chain 2026-09-26: both grow,
# and a 75-second delta gives a +/-1% position 1.14%/day on Raydium.
FEE_LAYOUT_DEXES = {'raydium-clmm': 'raydium', 'byreal': 'raydium',
                    'pancakeswap-v3-solana': 'raydium', 'orca': 'orca'}


def decode_fee_state(raw, layout):
    """sqrt price (Q64), fee growth per token (Q64), mints, and for the
    Raydium layout the decimals and reward growth per reward mint."""
    u = lambda a, b: int.from_bytes(raw[a:b], 'little')
    if layout == 'raydium':
        o = 8 + 1 + 32 * 7
        if len(raw) < REWARD_INFOS_OFFSET + 3 * REWARD_INFO_LEN:
            return None
        rewards = []
        for i in range(3):
            b = raw[REWARD_INFOS_OFFSET + i * REWARD_INFO_LEN: REWARD_INFOS_OFFSET + (i + 1) * REWARD_INFO_LEN]
            mint = b58(b[57:89])
            if b[0] != 0 and mint != NULL_MINT:
                rewards.append([mint, str(int.from_bytes(b[153:169], 'little'))])
        return {'sqrt_price': u(o + 20, o + 36), 'g0': u(o + 44, o + 60), 'g1': u(o + 60, o + 76),
                'dec_a': raw[o], 'dec_b': raw[o + 1], 'mint_a': b58(raw[73:105]), 'mint_b': b58(raw[105:137]),
                'rewards': rewards}
    if layout == 'orca':
        if len(raw) < 261:
            return None
        return {'sqrt_price': u(65, 81), 'g0': u(165, 181), 'g1': u(245, 261),
                'mint_a': b58(raw[101:133]), 'mint_b': b58(raw[181:213]), 'dec_a': None, 'dec_b': None,
                'rewards': []}
    return None


_MINT_DECIMALS = {}


def mint_decimals(mints):
    need = [m for m in mints if m not in _MINT_DECIMALS]
    if need:
        for m, raw in pool_accounts(need).items():
            if len(raw) > 44:
                _MINT_DECIMALS[m] = raw[44]
    return {m: _MINT_DECIMALS.get(m) for m in mints}


def fee_states(pools):
    """{address: state} for [(dex, address)] of the known layouts, one RPC
    call for the pools and one for any mint decimals Orca needs."""
    known = [(d, a) for d, a in pools if d in FEE_LAYOUT_DEXES]
    raws = pool_accounts([a for _, a in known])
    out = {}
    for d, a in known:
        st = decode_fee_state(raws.get(a, b''), FEE_LAYOUT_DEXES[d])
        if st:
            out[a] = st
    need = sorted({m for st in out.values() if st['dec_a'] is None for m in (st['mint_a'], st['mint_b'])})
    if need:
        decs = mint_decimals(need)
        for st in out.values():
            if st['dec_a'] is None:
                st['dec_a'], st['dec_b'] = decs.get(st['mint_a']), decs.get(st['mint_b'])
    return {a: st for a, st in out.items() if st['dec_a'] is not None and st['dec_b'] is not None}


Q64 = 2 ** 64


U128 = 2 ** 128


def band_income(first, last, seconds, band, usd_a, usd_b, reward_usd=None):
    """Fee (and reward) income of a position centred at the LAST price with
    half-width `band` (1.01 = +/-1%), in % of its value per day, from two
    samples of a pool's counters `seconds` apart. None if unusable."""
    if seconds <= 0 or not usd_a or not usd_b:
        return None
    dg0 = (int(last['g0']) - int(first['g0'])) % U128
    dg1 = (int(last['g1']) - int(first['g1'])) % U128
    fee_usd = (dg0 / Q64 / 10 ** last['dec_a'] * usd_a + dg1 / Q64 / 10 ** last['dec_b'] * usd_b)
    rw_usd = 0.0
    if reward_usd:
        before = {m: int(g) for m, g in (first.get('rewards') or [])}
        for m, g in (last.get('rewards') or []):
            if m in before and reward_usd.get(m) and m in _MINT_DECIMALS:
                rw_usd += ((int(g) - before[m]) % U128) / Q64 / 10 ** _MINT_DECIMALS[m] * reward_usd[m]
    sp = int(last['sqrt_price']) / Q64
    sa, sb = sp / math.sqrt(band), sp * math.sqrt(band)
    value = (1 / sp - 1 / sb) / 10 ** last['dec_a'] * usd_a + (sp - sa) / 10 ** last['dec_b'] * usd_b
    if value <= 0:
        return None
    return {'fee_pct_day': (fee_usd / value) / seconds * 86400 * 100,
            'reward_pct_day': (rw_usd / value) / seconds * 86400 * 100}


def fee_yield(first, last, usd_a, usd_b):
    """The fees a unit of in-range liquidity earned between two samples of a
    pool's counters, per dollar of its full-range equivalent (2 L sqrt p):
    the fee side of the fee / in-band-loss ratio (calm.fee_loss_ratio).
    None if unusable."""
    if not usd_a or not usd_b:
        return None
    dg0 = (int(last['g0']) - int(first['g0'])) % U128
    dg1 = (int(last['g1']) - int(first['g1'])) % U128
    fee_usd = dg0 / Q64 / 10 ** last['dec_a'] * usd_a + dg1 / Q64 / 10 ** last['dec_b'] * usd_b
    full = 2 * int(last['sqrt_price']) / Q64 / 10 ** last['dec_b'] * usd_b
    if full <= 0:
        return None
    return fee_usd / full


def attach_chain_rewards(records, accounts):
    """reward_usd_day and reward_mints from the pool accounts, for records
    whose API reports none. The chain is the authority: PancakeSwap has no
    reward API at all, and it was paying CAKE the board could not see."""
    live = {}
    for r in records:
        progs = decode_rewards(accounts.get(r['address'], b''))
        if progs:
            live[r['address']] = progs
    if not live:
        return records
    mints = sorted({m for progs in live.values() for m, _ in progs})
    prices = jupiter_api.jupiter_prices(mints)
    decs = {}
    for m, raw in pool_accounts(mints).items():
        # SPL mint layout (both programs): decimals at byte 44
        if len(raw) > 44:
            decs[m] = raw[44]
    for r in records:
        progs = live.get(r['address'])
        if not progs or (r.get('reward_usd_day') or 0) > 0:
            continue
        usd = sum(eps / 10 ** decs.get(m, 9) * 86400 * prices.get(m, 0.0) for m, eps in progs)
        r['reward_usd_day'] = usd
        r['reward_mints'] = list(dict.fromkeys((r.get('reward_mints') or []) + [m for m, _ in progs]))
    return records


def decode_amm_config(raw):
    """Raydium's AmmConfig: bump, index, owner, then the fee rates in ppm."""
    o = 8
    if len(raw) < o + 45:
        return None
    return {'protocol_fee_rate': int.from_bytes(raw[o + 35:o + 39], 'little'),
            'trade_fee_rate': int.from_bytes(raw[o + 39:o + 43], 'little'),
            'tick_spacing': int.from_bytes(raw[o + 43:o + 45], 'little')}


def attach_chain_state(records):
    """Fill `liquidity` (and check the price) from the pool accounts themselves.
    A record whose account cannot be read or decoded keeps liquidity=None and
    the engine declines to score it rather than guess."""
    accounts = pool_accounts([r['address'] for r in records])
    for r in records:
        st = decode_clmm_state(accounts.get(r['address'], b''))
        if not st:
            continue
        r['liquidity'] = st['liquidity']
        r['tick_spacing'] = st['tick_spacing']
        r['chain_price'] = st['price']
        # The API price and the chain price must agree, or the pool the API
        # describes is not the account that was read.
        if r.get('price') and abs(st['price'] / r['price'] - 1) > 0.05:
            r['liquidity'] = None
            r['note'] = f'chain price {st["price"]:.6g} disagrees with API price {r["price"]:.6g}'
    try:
        attach_chain_rewards(records, accounts)
    except Exception:
        pass                     # rewards are extra; a failed read leaves the API's figure
    return records
