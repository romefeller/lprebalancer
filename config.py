"""Configuration, read from Postgres at startup.

Every tunable lives in `rebalancer.config`, one row per named profile, with
exactly one row marked active. Nothing here is specific to a pair. The only
essential fact about what to trade is the pool address; the signer reads the
tokens, their decimals and the price from the pool itself, and `db.py add`
fills the display symbols from the same source.

    python3 db.py add wif-usdc <pool> capital_usd=200   # describe a new pool
    python3 db.py activate wif-usdc                     # switch to it
    python3 db.py set wif-usdc capital_usd=250          # retune, then restart
    python3 db.py config                                # the active profile

Two values stay out of the table on purpose:

    LPBOT_WALLET   path to the signing key. Deployment-specific, and a bot that
                   guesses where your key lives is a bot that might find the
                   wrong one. No default: startup fails loudly without it.
    LPBOT_RPC      endpoint. Often carries an API key in the URL, so it belongs
                   in the service environment, not in a table anyone can select.

Any column may still be overridden for one run by the matching LPBOT_ variable
(`LPBOT_POOL`, `LPBOT_CAPITAL_USD`, ...). The table is the source of truth; the
environment is the escape hatch.
"""
import os

import db


def _env(name, cast, default):
    v = os.environ.get(name)
    if v in (None, ''):
        return default
    return cast(v)


_CFG = db.load_config(os.environ.get('LPBOT_PROFILE'))

PROFILE = _CFG['name']

# --- what to trade -----------------------------------------------------------
DEX = _env('LPBOT_DEX', str, _CFG.get('dex') or 'orca')
POOL = _env('LPBOT_POOL', str, _CFG['pool'])
PAIR_LABEL = _env('LPBOT_PAIR', str, _CFG['pair_label'])
TOKEN_A = _env('LPBOT_TOKEN_A', str, _CFG['token_a'])
TOKEN_B = _env('LPBOT_TOKEN_B', str, _CFG['token_b'])

# --- size --------------------------------------------------------------------
CAPITAL_USD = _env('LPBOT_CAPITAL_USD', float, float(_CFG['capital_usd']))
MAX_USD = _env('LPBOT_MAX_USD', float, float(_CFG['max_usd']))
# Native SOL never spent, whatever the pool holds. A wallet that cannot pay a
# fee cannot close its own position.
GAS_RESERVE_SOL = _env('LPBOT_GAS_RESERVE_SOL', float, float(_CFG['gas_reserve_sol']))
# Each token's deposit cap as a fraction of capital. A centred band takes about
# half of each; the margin over 0.5 absorbs price movement between quote and fill.
SIDE_CAP_FRACTION = _env('LPBOT_SIDE_CAP', float, float(_CFG['side_cap_fraction']))

# --- band selection ----------------------------------------------------------
# Candidate half-widths as multipliers: 1.05 means the band runs from price/1.05
# to price*1.05. The simulator scores each on the pool's own recent price and
# volume and keeps the best.
BANDS = tuple(_env('LPBOT_BANDS', lambda s: [float(x) for x in s.split(',')],
                   [float(b) for b in _CFG['bands']]))

# A narrower band earns more per dollar and dies more often. Every rebalance is
# a chance for a transaction to fail, so the bot refuses a band whose simulated
# rebalance rate exceeds this, however good its raw yield looks.
MAX_REBALANCES_PER_DAY_MODELLED = _env(
    'LPBOT_MAX_MODELLED_REBAL', float, float(_CFG['max_modelled_rebal_per_day']))
# Round-trip swap and slippage the simulator charges per modelled rebalance.
SWAP_COST = _env('LPBOT_SWAP_COST_BPS', int, _CFG['swap_cost_bps']) / 1e4

# --- rebalancing -------------------------------------------------------------
POLL_SECONDS = _env('LPBOT_POLL_SECONDS', int, _CFG['poll_seconds'])
MIN_REBALANCE_GAP = _env('LPBOT_MIN_GAP', int, _CFG['min_rebalance_gap_seconds'])
MAX_REBALANCES_PER_DAY = _env('LPBOT_MAX_REBAL', int, _CFG['max_rebalances_per_day'])
REOPT_INTERVAL = _env('LPBOT_REOPT_INTERVAL', int, _CFG['reopt_interval_seconds'])
REOPT_MIN_GAIN = _env('LPBOT_REOPT_MIN_GAIN', float, float(_CFG['reopt_min_gain']))

# --- safety ------------------------------------------------------------------
MAX_CONSECUTIVE_FAILURES = _env('LPBOT_MAX_FAILURES', int,
                                _CFG['max_consecutive_failures'])
MAX_UNREADABLE_POLLS = _env('LPBOT_MAX_UNREADABLE', int, _CFG['max_unreadable_polls'])
SLIPPAGE_BPS = _env('LPBOT_SLIPPAGE_BPS', int, _CFG['slippage_bps'])

# --- pool screening (scanner only) ------------------------------------------
# Consulted only when the scanner picks the pool. Token quality is a rule on
# Jupiter's token facts (engine.screen_token), not a parameter here.
MIN_NET_DAY_PCT = _env('LPBOT_MIN_NET_DAY', float, float(_CFG['min_net_day_pct']))
MIN_TVL_USD = _env('LPBOT_MIN_TVL', float, float(_CFG['min_tvl_usd']))

# --- the board: scanning other pools and other DEXes --------------------------
_list = lambda s: [x.strip() for x in s.split(',') if x.strip()]
DEXES = tuple(_env('LPBOT_DEXES', _list, list(_CFG.get('dexes') or ['orca'])))
SCAN_LIMIT = _env('LPBOT_SCAN_LIMIT', int, _CFG.get('scan_limit') or 30)
SCAN_INTERVAL = _env('LPBOT_SCAN_INTERVAL', int, _CFG.get('scan_interval_seconds') or 21600)
MIN_VOLUME_24H_USD = _env('LPBOT_MIN_VOLUME', float, float(_CFG.get('min_volume_24h_usd') or 0))
# The board's best must beat the held pool's best by this much (modelled,
# relative) before the bot moves pools. Higher than reopt_min_gain: a pool
# move is a close, possibly a swap, and an open on a venue the bot has not
# been watching.
MIGRATE_MIN_GAIN = _env('LPBOT_MIGRATE_MIN_GAIN', float, float(_CFG.get('migrate_min_gain') or 0.5))
# DEXes the bot may actually open positions on: the ones with a signer.
EXECUTE_DEXES = tuple(_env('LPBOT_EXECUTE_DEXES', _list, list(_CFG.get('execute_dexes') or ['orca'])))
# Stay on `pool` whatever the board says.
POOL_PINNED = _env('LPBOT_POOL_PINNED', lambda s: s.lower() in ('1', 'true', 'yes'),
                   bool(_CFG.get('pool_pinned')))
# Whether the bot may swap tokens to enter a pool of a different pair. Until
# it may, migration is limited to pools of the pair the wallet already holds.
ALLOW_SWAP = _env('LPBOT_ALLOW_SWAP', lambda s: s.lower() in ('1', 'true', 'yes'),
                  bool(_CFG.get('allow_swap')))

# A voluntary move (reband, pool move) waits for a quiet hour, one whose
# volume multiplier is at or under 1.0, when ten minutes out of market costs
# least. An out-of-band rebalance never waits.
DEFER_MOVES_TO_QUIET_HOURS = _env('LPBOT_DEFER_QUIET', lambda s: s.lower() in ('1', 'true', 'yes'),
                                  bool(_CFG.get('defer_moves_to_quiet_hours', True)))

# --- plumbing ----------------------------------------------------------------
RPC = _env('LPBOT_RPC', str,
           os.environ.get('SOLANA_RPC_URL') or 'https://api.mainnet-beta.solana.com')
WALLET = _env('LPBOT_WALLET', str, os.environ.get('WALLET_SECRET_PATH', ''))


def require_wallet():
    """Fail at startup rather than mysteriously at the first signature."""
    if not WALLET:
        raise SystemExit(
            'No signing key configured. Set LPBOT_WALLET (or WALLET_SECRET_PATH) '
            'to the path of your Solana keypair. Nothing was started.')
    return WALLET


def reload():
    """Re-read the active profile in place. The bot calls this after it moves
    pools, so every `config.X` the loop reads reflects the new pool."""
    import importlib, sys
    importlib.reload(sys.modules[__name__])


def summary():
    return {
        'profile': PROFILE,
        'dex': DEX,
        'pool': POOL,
        'pair': PAIR_LABEL,
        'capital_usd': CAPITAL_USD,
        'max_usd': MAX_USD,
        'gas_reserve_sol': GAS_RESERVE_SOL,
        'bands': [f'+/-{(b - 1) * 100:.0f}%' for b in BANDS],
        'max_modelled_rebalances_per_day': MAX_REBALANCES_PER_DAY_MODELLED,
        'poll_seconds': POLL_SECONDS,
        'min_gap_seconds': MIN_REBALANCE_GAP,
        'max_rebalances_per_day': MAX_REBALANCES_PER_DAY,
        'reopt_every_hours': REOPT_INTERVAL / 3600,
        'reopt_min_gain': REOPT_MIN_GAIN,
        'scan_dexes': list(DEXES),
        'scan_every_hours': SCAN_INTERVAL / 3600,
        'execute_dexes': list(EXECUTE_DEXES),
        'migrate_min_gain': MIGRATE_MIN_GAIN,
        'pool_pinned': POOL_PINNED,
        'allow_swap': ALLOW_SWAP,
    }


if __name__ == '__main__':
    import json
    print(json.dumps(summary(), indent=1))
