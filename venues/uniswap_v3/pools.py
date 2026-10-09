"""Uniswap v3 on Unichain and Polygon: one pool at a time, by address."""

import math
import os

from venues import api as venue_api
from venues import evm
from venues.api import _f, _token
from venues.evm import _abi_string, _evm_addr, _signed, _words, checksum_address
from venues.solana_state import U128


# --- Unichain and Polygon: Uniswap v3 (EVM) ----------------------------------
# One pool at a time, by address: `pool('uniswap-v3-unichain', addr)` or
# `pool('uniswap-v3-polygon', addr)`. Never on the Solana board. The pool's
# state comes from the chain (one eth_call batch); volume and TVL from the
# chain's GeckoTerminal network. A pool is accepted only when its factory() is
# Uniswap's v3 factory on that chain and that factory maps (token0, token1,
# fee) back to the pool. fee() is fixed per pool (no dynamic fee, no staking
# cut): the LP keeps fee/1e6 of every swap.

GECKO_UNICHAIN = 'https://api.geckoterminal.com/api/v2/networks/unichain'


UNICHAIN_RPCS = ('https://mainnet.unichain.org', 'https://unichain-rpc.publicnode.com')


UNICHAIN_V3_FACTORY = '0x1f98400000000000000000000000000000000003'


evm._SEL['getPool_v3'] = '0x1698ee82'          # getPool(address,address,uint24)


# Per dex: the chain, its name in errors, the v3 factory, the GeckoTerminal
# network, the public endpoints and the environment variable put first.
UNISWAP_V3 = {
    'uniswap-v3-unichain': {'chain': 'unichain', 'name': 'Unichain', 'factory': UNICHAIN_V3_FACTORY,
                            'gecko': GECKO_UNICHAIN, 'rpcs': UNICHAIN_RPCS, 'rpc_env': 'LPBOT_UNICHAIN_RPC'},
    'uniswap-v3-polygon': {'chain': 'polygon', 'name': 'Polygon',
                           'factory': '0x1f98431c8ad98523631ae4a59f267346ea31f984',
                           'gecko': 'https://api.geckoterminal.com/api/v2/networks/polygon_pos',
                           'rpcs': ('https://polygon-bor-rpc.publicnode.com', 'https://polygon.drpc.org'),
                           'rpc_env': 'LPBOT_POLYGON_RPC'},
}


def _uniswap_rpcs(dex):
    v = UNISWAP_V3[dex]
    own = os.environ.get(v['rpc_env'])
    return ((own,) if own else ()) + v['rpcs']


def uniswap_v3_state(address, urls=None, dex='uniswap-v3-unichain'):
    """The pool's own facts, from the chain. Refuses a pool whose factory is
    not Uniswap's v3 factory on the dex's chain, or that the factory does not
    map back to this address."""
    v = UNISWAP_V3[dex]
    urls = urls or _uniswap_rpcs(dex)
    names = ['factory', 'token0', 'token1', 'fee', 'tickSpacing', 'liquidity', 'slot0']
    res = evm.evm_calls([(address, evm._SEL[n]) for n in names], urls, chain=v['name'])
    st = {n: _words(r) for n, r in zip(names, res)}
    factory = f"0x{st['factory'][0]:040x}"
    if factory != v['factory']:
        raise ValueError(f"{address} is not a pool of the Uniswap v3 factory on {v['name']} (factory {factory})")
    t0, t1 = _evm_addr(st['token0'][0]), _evm_addr(st['token1'][0])
    fee = st['fee'][0]
    arg = lambda a: f'{int(a, 16):064x}'
    tok = evm.evm_calls([(t0, evm._SEL['decimals']), (t1, evm._SEL['decimals']),
                         (t0, evm._SEL['symbol']), (t1, evm._SEL['symbol']),
                         (factory, evm._SEL['getPool_v3'] + arg(t0) + arg(t1) + f'{fee:064x}')], urls, chain=v['name'])
    if f"0x{_words(tok[4])[0]:040x}" != address.lower():
        raise ValueError(f'factory {factory} does not map ({t0}, {t1}, {fee}) to {address}')
    da, db_ = _words(tok[0])[0], _words(tok[1])[0]
    sqrt_p = st['slot0'][0]
    return {
        'token0': t0, 'token1': t1, 'decimals0': da, 'decimals1': db_,
        'symbol0': _abi_string(tok[2]), 'symbol1': _abi_string(tok[3]),
        'tick_spacing': _signed(st['tickSpacing'][0], 24), 'tick': _signed(st['slot0'][1], 24),
        'fee_pips': fee, 'liquidity_raw': st['liquidity'][0],
        'price': (sqrt_p / 2 ** 96) ** 2 * 10 ** (da - db_),
    }


def from_uniswap_v3(address, st, gecko, dex='uniswap-v3-unichain'):
    """The usual record from the chain state and GeckoTerminal's attributes."""
    at = (gecko or {}).get('attributes') or {}
    fee = st['fee_pips'] / 1e6
    volume = _f((at.get('volume_usd') or {}).get('h24'))
    da, db_ = st['decimals0'], st['decimals1']
    return {
        'dex': dex, 'kind': 'clmm', 'chain': UNISWAP_V3[dex]['chain'],
        'address': checksum_address(address), 'pair': f"{st['symbol0']}/{st['symbol1']}",
        'token_a': _token(st['token0'], st['symbol0'], st['symbol0'], da),
        'token_b': _token(st['token1'], st['symbol1'], st['symbol1'], db_),
        'price': st['price'], 'chain_price': st['price'],
        'fee': fee, 'fee_source': 'nominal', 'fee_nominal': fee,
        'tvl_usd': _f(at.get('reserve_in_usd')), 'volume_24h_usd': volume,
        'fees_24h_usd': volume * fee,
        'liquidity': st['liquidity_raw'] / math.sqrt(10 ** da * 10 ** db_) or None,
        'adaptive_fee': False, 'tick_spacing': st['tick_spacing'], 'tick': st['tick'],
        'reward_usd_day': 0.0, 'reward_mints': [],
    }


def uniswap_v3_pool(address, dex='uniswap-v3-unichain'):
    """One Uniswap v3 pool on the dex's chain (Unichain by default). The chain
    is required; GeckoTerminal only adds volume and TVL, and its absence leaves
    them at 0, not the record."""
    try:
        st = uniswap_v3_state(address, dex=dex)
    except Exception:
        return None
    try:
        d = venue_api._get(f"{UNISWAP_V3[dex]['gecko']}/pools/{address.lower()}", accept='application/json;version=20230203')
        gecko = d.get('data') if isinstance(d, dict) else None
    except Exception:
        gecko = None
    return from_uniswap_v3(address, st, gecko, dex)


evm._SEL['feeGrowthGlobal0X128'] = '0xf3058399'


evm._SEL['feeGrowthGlobal1X128'] = '0x46141319'


def v3_fee_state(raw):
    """A fee_growth sample from a Uniswap v3 pool's own counters, in the Q64
    units of the Solana layouts, so dexes.fee_yield and the hot pause's
    fee / in-band-loss ratio read it unchanged. `raw` holds the pool's
    feeGrowthGlobal0X128 / 1X128 (Q128, mod 2^256) and slot0's sqrtPriceX96
    (Q96), with the pool's tokens and decimals. The counters are cut to 128
    bits after the shift, as fee_yield takes differences mod 2^128. Pure."""
    return {'sqrt_price': raw['sqrt_price_x96'] >> 32,
            'g0': (raw['g0_x128'] >> 64) % U128, 'g1': (raw['g1_x128'] >> 64) % U128,
            'rewards': [], 'dec_a': raw['dec_a'], 'dec_b': raw['dec_b'],
            'mint_a': raw['mint_a'], 'mint_b': raw['mint_b']}


def uniswap_v3_fee_state(address, dex='uniswap-v3-unichain', urls=None):
    """One fee_growth sample of a Uniswap v3 pool, from one eth_call batch of
    the pool and its tokens. The hot pause needs these on EVM chains: with no
    sample it reads "no data" and never pauses (2026-10-08: Polygon had none).
    Raises when the chain cannot be read."""
    v = UNISWAP_V3[dex]
    urls = urls or _uniswap_rpcs(dex)
    names = ['feeGrowthGlobal0X128', 'feeGrowthGlobal1X128', 'slot0', 'token0', 'token1']
    res = evm.evm_calls([(address, evm._SEL[n]) for n in names], urls, chain=v['name'])
    w = {n: _words(r) for n, r in zip(names, res)}
    t0, t1 = _evm_addr(w['token0'][0]), _evm_addr(w['token1'][0])
    dec = evm.evm_calls([(t0, evm._SEL['decimals']), (t1, evm._SEL['decimals'])], urls, chain=v['name'])
    return v3_fee_state({'g0_x128': w['feeGrowthGlobal0X128'][0], 'g1_x128': w['feeGrowthGlobal1X128'][0],
                         'sqrt_price_x96': w['slot0'][0], 'dec_a': _words(dec[0])[0], 'dec_b': _words(dec[1])[0],
                         'mint_a': t0.lower(), 'mint_b': t1.lower()})


def uniswap_v3_polygon_pool(address):
    """One Uniswap v3 pool on Polygon (uniswap_v3_pool for 'uniswap-v3-polygon')."""
    return uniswap_v3_pool(address, dex='uniswap-v3-polygon')
