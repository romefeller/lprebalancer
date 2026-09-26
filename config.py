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

# --- the proactive policy ------------------------------------------------------
# The bot acts BEFORE the price leaves. Every poll it estimates, from the
# pool's own tape, the probability that the price is outside the band within
# `proactive_horizon_hours`; at or above `proactive_threshold` it re-centres.
# A threshold of 0 disables the rule (rebalance only once outside).
PROACTIVE_HORIZON = _env('LPBOT_PROACTIVE_HORIZON', int, int(_CFG.get('proactive_horizon_hours') or 6))
PROACTIVE_THRESHOLD = _env('LPBOT_PROACTIVE_THRESHOLD', float, float(_CFG.get('proactive_threshold') or 0))

# --- the dividend -------------------------------------------------------------
# Accrued fees are harvested into the wallet every `harvest_interval_hours`
# (0: only when a position closes), once at least `min_harvest_usd` accrued.
HARVEST_INTERVAL = _env('LPBOT_HARVEST_INTERVAL_HOURS', int, int(_CFG.get('harvest_interval_hours') or 0)) * 3600
MIN_HARVEST_USD = _env('LPBOT_MIN_HARVEST_USD', float, float(_CFG.get('min_harvest_usd') or 0))


# --- calm mode ----------------------------------------------------------------
# A tight band while five-minute volatility is low (calm.py). Off by default:
# with calm_enabled false the loop never calls calm.py and nothing changes.
CALM_ENABLED = _env('LPBOT_CALM', lambda s: s.lower() in ('1', 'true', 'yes'), bool(_CFG.get('calm_enabled')))
CALM_BAND = _env('LPBOT_CALM_BAND', float, float(_CFG.get('calm_band') or 1.01))
CALM_SIGMA_CUT = _env('LPBOT_CALM_CUT', float, float(_CFG.get('calm_sigma_cut') or 0.0015272))
CALM_EXIT_MULT = _env('LPBOT_CALM_EXIT_MULT', float, float(_CFG.get('calm_exit_mult') or 1.25))
CALM_HORIZON_MINUTES = _env('LPBOT_CALM_HORIZON', int, int(_CFG.get('calm_horizon_minutes') or 30))
CALM_THRESHOLD = _env('LPBOT_CALM_THRESHOLD', float, float(_CFG.get('calm_threshold') or 0.25))
CALM_MIN_GAP = _env('LPBOT_CALM_MIN_GAP', int, int(_CFG.get('calm_min_gap_seconds') or 600))
CALM_MAX_MOVES = _env('LPBOT_CALM_MAX_MOVES', int, int(_CFG.get('calm_max_moves_per_day') or 0))
CALM_POLL_SECONDS = _env('LPBOT_CALM_POLL', int, int(_CFG.get('calm_poll_seconds') or 120))
# Regime mode (calm.regime_view): the narrowest width in REGIME_WIDTHS whose
# P(touch within REGIME_HORIZON) <= REGIME_THRESHOLD, chosen every poll. When
# on, it holds every band; calm mode's two-width switch and the hourly rule
# stand aside. CALM_MAX_MOVES remains as the runaway guard.
REGIME_ENABLED = _env('LPBOT_REGIME', lambda s: s.lower() in ('1', 'true', 'yes'), bool(_CFG.get('regime_enabled')))
REGIME_WIDTHS = tuple(float(x) for x in (_CFG.get('regime_widths') or [1.01, 1.0125, 1.015, 1.02, 1.025, 1.03, 1.04, 1.05]))
REGIME_HORIZON = _env('LPBOT_REGIME_HORIZON', int, int(_CFG.get('regime_horizon_minutes') or 120))
REGIME_THRESHOLD = _env('LPBOT_REGIME_THRESHOLD', float, float(_CFG.get('regime_threshold') or 0.25))
REGIME_STEPS = _env('LPBOT_REGIME_STEPS', int, int(_CFG.get('regime_steps') or 2))
REGIME_TAPE_DAYS = _env('LPBOT_REGIME_TAPE_DAYS', int, int(_CFG.get('regime_tape_days') or 30))
# Fee density scales the touch threshold: liquidity flooding in (our share of
# fees falls, the risk of a touch does not) tightens it; volume up or
# liquidity leaving loosens it. Bounded, because no history exists to fit it.
REGIME_LIQ_MIN = _env('LPBOT_REGIME_LIQ_MIN', float, float(_CFG.get('regime_liq_min') or 0.6))
REGIME_LIQ_MAX = _env('LPBOT_REGIME_LIQ_MAX', float, float(_CFG.get('regime_liq_max') or 1.25))
# Venues are ranked by what their on-chain fee counters say liquidity at the
# active price earned: sampled every VENUE_SAMPLE_S, and a move needs at least
# VENUE_MIN_HOURS of evidence on both the held pool and the target.
VENUE_SAMPLE_S = _env('LPBOT_VENUE_SAMPLE_S', int, int(_CFG.get('venue_sample_seconds') or 600))
VENUE_MIN_HOURS = _env('LPBOT_VENUE_MIN_HOURS', int, int(_CFG.get('venue_min_hours') or 6))
# Swap to 50/50 before an open when the wallet is lopsided. Without it, an
# open after an exit is limited by the scarcer token and most capital idles.
REBALANCE_SWAP = _env('LPBOT_REBALANCE_SWAP', lambda s: s.lower() in ('1', 'true', 'yes'),
                      bool(_CFG.get('rebalance_swap')))


# --- the fee split ---------------------------------------------------------------
# Fees in `payout_mint` go to `profit_wallet` on every harvest; the rest are
# reinvested. Native-SOL fees refill gas first when it is under the reserve
# (fees.py). Off unless `payout_enabled`; nothing here names a token or address.
PAYOUT_ENABLED = _env('LPBOT_PAYOUT', lambda s: s.lower() in ('1', 'true', 'yes'), bool(_CFG.get('payout_enabled')))
PROFIT_WALLET = _env('LPBOT_PROFIT_WALLET', str, _CFG.get('profit_wallet') or '')
PAYOUT_MINT = _env('LPBOT_PAYOUT_MINT', str, _CFG.get('payout_mint') or '')
# Reward tokens that are not one of the pool's own: 'payout' swaps them to
# payout_mint and sends them (to native SOL for gas while gas is low); 'hold'
# leaves them in the LP wallet. Balances under reward_min_usd wait.
REWARD_POLICY = _env('LPBOT_REWARD_POLICY', str, _CFG.get('reward_policy') or 'payout')
REWARD_MIN_USD = _env('LPBOT_REWARD_MIN_USD', float, float(_CFG.get('reward_min_usd') or 1.0))
REWARD_MAX_USD = _env('LPBOT_REWARD_MAX_USD', float, float(_CFG.get('reward_max_usd') or 25.0))


def policy():
    """The proactive rule as the engine takes it."""
    return {'horizon': PROACTIVE_HORIZON, 'threshold': PROACTIVE_THRESHOLD}


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
        'proactive_horizon_hours': PROACTIVE_HORIZON,
        'proactive_threshold': PROACTIVE_THRESHOLD,
        'harvest_every_hours': HARVEST_INTERVAL / 3600,
        'min_harvest_usd': MIN_HARVEST_USD,
        'calm_enabled': CALM_ENABLED,
        'calm_band_pct': round((CALM_BAND - 1) * 100, 2),
        'calm_sigma_cut_pct': round(CALM_SIGMA_CUT * 100, 4),
        'calm_max_moves_per_day': CALM_MAX_MOVES,
        'rebalance_swap': REBALANCE_SWAP,
        'regime_enabled': REGIME_ENABLED,
        'regime_widths_pct': [round((k - 1) * 100, 2) for k in REGIME_WIDTHS],
        'regime_horizon_minutes': REGIME_HORIZON,
        'regime_threshold': REGIME_THRESHOLD,
        'payout_enabled': PAYOUT_ENABLED,
        'profit_wallet': PROFIT_WALLET or None,
        'payout_mint': PAYOUT_MINT or None,
        'reward_policy': REWARD_POLICY,
        'reward_min_usd': REWARD_MIN_USD,
    }


if __name__ == '__main__':
    import json
    print(json.dumps(summary(), indent=1))
