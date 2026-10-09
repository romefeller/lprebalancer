"""Aerodrome Slipstream on Base: one pool at a time, by address."""

import math
import os

from venues import api as venue_api
from venues import evm
from venues.api import _f, _token
from venues.evm import _abi_string, _evm_addr, _signed, _words, checksum_address


# --- Base: Aerodrome Slipstream (EVM) ------------------------------------------
# One pool at a time, by address: `pool('aerodrome-slipstream', addr)`. Not on
# the Solana board (KNOWN, ADAPTERS): the scanner never lists Base pools.
# The pool's state comes from the chain (eth_call, one JSON-RPC batch); volume
# and TVL from GeckoTerminal's 'base' network. The fee is what an UNSTAKED
# position keeps: the pool's current fee() less the unstakedFee share the gauge
# takes (5% on 2026-10-01). fee() is dynamic (base + tick-volatility term), and
# the first swap of each block pays a cheaper initial fee (150 pips on this
# pool), so the realised fee runs below fee().

GECKO_BASE = 'https://api.geckoterminal.com/api/v2/networks/base'


BASE_RPCS = ('https://mainnet.base.org', 'https://base-rpc.publicnode.com')


# Known Slipstream deployments: factory -> its NonfungiblePositionManager (lower
# case). Same table as chains/evm/base_addresses.mjs DEPLOYMENTS; evidence in chains/evm/BASE_ADDRESSES.md.
SLIPSTREAM_DEPLOYMENTS = {
    '0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a': '0x827922686190790b37229fd06084350e74485b72',   # initial
    '0xf8f2eb4940cfe7d13603dddd87f123820fc061ef': '0xe1f8cd9ac4e4a65f54f38a5cdafca44f6dd68b53',   # gauges-v3
}


def _base_rpcs():
    own = os.environ.get('LPBOT_BASE_RPC')
    return ((own,) if own else ()) + BASE_RPCS


def _get_pool_call(factory, t0, t1, tick_spacing):
    """(to, data) of factory.getPool(t0, t1, tickSpacing)."""
    arg = lambda a: f'{int(a, 16):064x}'
    return factory, evm._SEL['getPool'] + arg(t0) + arg(t1) + f'{tick_spacing % (1 << 256):064x}'


def slipstream_state(address, urls=None):
    """The pool's own facts, from the chain. Refuses a pool whose factory and
    position manager are not one known deployment, or that the factory does
    not map back to this address."""
    names = ['factory', 'nft', 'token0', 'token1', 'tickSpacing', 'fee', 'unstakedFee',
             'liquidity', 'stakedLiquidity', 'slot0']
    urls = urls or _base_rpcs()
    res = evm.evm_calls([(address, evm._SEL[n]) for n in names], urls)
    st = {n: _words(r) for n, r in zip(names, res)}
    factory, nft = f"0x{st['factory'][0]:040x}", f"0x{st['nft'][0]:040x}"
    if factory not in SLIPSTREAM_DEPLOYMENTS:
        raise ValueError(f'{address} is not a pool of a known Slipstream factory (factory {factory})')
    if nft != SLIPSTREAM_DEPLOYMENTS[factory]:
        raise ValueError(f'{address} names position manager {nft}, not {SLIPSTREAM_DEPLOYMENTS[factory]} of factory {factory}')
    t0, t1 = _evm_addr(st['token0'][0]), _evm_addr(st['token1'][0])
    spacing = _signed(st['tickSpacing'][0], 24)
    tok = evm.evm_calls([(t0, evm._SEL['decimals']), (t1, evm._SEL['decimals']),
                         (t0, evm._SEL['symbol']), (t1, evm._SEL['symbol']),
                         _get_pool_call(factory, t0, t1, spacing)], urls)
    if f"0x{_words(tok[4])[0]:040x}" != address.lower():
        raise ValueError(f'factory {factory} does not map ({t0}, {t1}, {spacing}) to {address}')
    da, db_ = _words(tok[0])[0], _words(tok[1])[0]
    sqrt_p = st['slot0'][0]
    return {
        'token0': t0, 'token1': t1, 'decimals0': da, 'decimals1': db_,
        'symbol0': _abi_string(tok[2]), 'symbol1': _abi_string(tok[3]),
        'tick_spacing': spacing, 'tick': _signed(st['slot0'][1], 24),
        'fee_pips': st['fee'][0], 'unstaked_fee_pips': st['unstakedFee'][0],
        'liquidity_raw': st['liquidity'][0], 'staked_liquidity_raw': st['stakedLiquidity'][0],
        'price': (sqrt_p / 2 ** 96) ** 2 * 10 ** (da - db_),
    }


def from_slipstream(address, st, gecko):
    """The usual record from the chain state and GeckoTerminal's attributes."""
    at = (gecko or {}).get('attributes') or {}
    nominal = st['fee_pips'] / 1e6
    lp_fee = nominal * (1 - st['unstaked_fee_pips'] / 1e6)
    volume = _f((at.get('volume_usd') or {}).get('h24'))
    da, db_ = st['decimals0'], st['decimals1']
    return {
        'dex': 'aerodrome-slipstream', 'kind': 'clmm', 'chain': 'base',
        'address': checksum_address(address), 'pair': f"{st['symbol0']}/{st['symbol1']}",
        'token_a': _token(st['token0'], st['symbol0'], st['symbol0'], da),
        'token_b': _token(st['token1'], st['symbol1'], st['symbol1'], db_),
        'price': st['price'], 'chain_price': st['price'],
        'fee': lp_fee, 'fee_source': 'nominal', 'fee_nominal': nominal,
        'unstaked_fee': st['unstaked_fee_pips'] / 1e6,
        'tvl_usd': _f(at.get('reserve_in_usd')), 'volume_24h_usd': volume,
        'fees_24h_usd': volume * nominal,
        'liquidity': st['liquidity_raw'] / math.sqrt(10 ** da * 10 ** db_) or None,
        # Unstaked positions share fees with the unstaked liquidity only; the
        # gauge's stakers earn AERO instead of fees.
        'staked_share': (st['staked_liquidity_raw'] / st['liquidity_raw']) if st['liquidity_raw'] else None,
        'adaptive_fee': True, 'tick_spacing': st['tick_spacing'], 'tick': st['tick'],
        'reward_usd_day': 0.0, 'reward_mints': [],
    }


def slipstream_pool(address):
    """One Slipstream pool on Base. The chain is required; GeckoTerminal only
    adds volume and TVL, and its absence leaves them at 0, not the record."""
    try:
        st = slipstream_state(address)
    except Exception:
        return None
    try:
        d = venue_api._get(f'{GECKO_BASE}/pools/{address.lower()}', accept='application/json;version=20230203')
        gecko = d.get('data') if isinstance(d, dict) else None
    except Exception:
        gecko = None
    return from_slipstream(address, st, gecko)
