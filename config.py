"""Every tunable in one place.

Nothing in this bot is specific to SOL/USDC. The pool is discovered by the
scanner or pinned here; the band is chosen by simulation; token decimals and
prices come from the chain. To run it on a different pair, change POOL (or
leave it None and let it scan), and change nothing else.

Environment variables override the file, so a service unit can retune without
editing code:

    LPBOT_POOL              pin to one pool address; unset = scan and choose
    LPBOT_CAPITAL_USD       target position size
    LPBOT_MAX_USD           hard cap, refuses anything larger
    LPBOT_POLL_SECONDS      how often to check the band
    LPBOT_RPC               Solana RPC endpoint
    LPBOT_WALLET            path to the signing key (REQUIRED, no default)
"""
import os

def _f(name, default):
    v = os.environ.get(name)
    return float(v) if v not in (None, '') else default

def _i(name, default):
    v = os.environ.get(name)
    return int(v) if v not in (None, '') else default

def _s(name, default):
    v = os.environ.get(name)
    return v if v not in (None, '') else default


# --- what to trade -----------------------------------------------------------
# Pin a pool, or leave None to let the scanner rank every eligible pool and
# choose. Pinning is the safer default once you know what you want to hold.
POOL = _s('LPBOT_POOL', 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE')
PAIR_LABEL = _s('LPBOT_PAIR', 'SOL/USDC')

# --- size --------------------------------------------------------------------
CAPITAL_USD = _f('LPBOT_CAPITAL_USD', 190.0)
MAX_USD = _f('LPBOT_MAX_USD', 260.0)
SOL_RESERVE = _f('LPBOT_SOL_RESERVE', 0.05)     # never spend below this, ever

# --- band selection ----------------------------------------------------------
# Candidate half-widths, as a multiplier: 1.05 means the band runs from
# price/1.05 to price*1.05. The simulator scores each on the pool's own recent
# price and volume and keeps the best.
BANDS = tuple(float(x) for x in
              _s('LPBOT_BANDS', '1.03,1.05,1.08,1.12,1.18,1.25,1.40').split(','))

# A narrower band earns more per dollar and dies more often. Every rebalance is
# a chance for a transaction to fail, so the bot will not choose a band whose
# simulated rebalance rate exceeds this, however good its raw yield looks.
MAX_REBALANCES_PER_DAY_MODELLED = _f('LPBOT_MAX_MODELLED_REBAL', 0.50)

# --- rebalancing -------------------------------------------------------------
POLL_SECONDS = _i('LPBOT_POLL_SECONDS', 300)
MIN_REBALANCE_GAP = _i('LPBOT_MIN_GAP', 3600)       # no churn on a band edge
MAX_REBALANCES_PER_DAY = _i('LPBOT_MAX_REBAL', 6)   # hard daily ceiling
REOPT_INTERVAL = _i('LPBOT_REOPT_INTERVAL', 6 * 3600)
REOPT_MIN_GAIN = _f('LPBOT_REOPT_MIN_GAIN', 0.25)   # only reband for a clear win

# --- safety ------------------------------------------------------------------
MAX_CONSECUTIVE_FAILURES = _i('LPBOT_MAX_FAILURES', 3)
MAX_UNREADABLE_POLLS = _i('LPBOT_MAX_UNREADABLE', 12)
SLIPPAGE_BPS = _i('LPBOT_SLIPPAGE_BPS', 100)

# --- token quality gates (Jev) ----------------------------------------------
# Only consulted when the scanner picks the pool. A leveraged token in an LP
# pays a high headline yield for taking the other side of something engineered
# to decay: SOL/xSOL advertised 243%/yr and xSOL is 3x leveraged SOL.
MAX_LEVERAGED = _f('LPBOT_MAX_LEVERAGED', 0.35)
MIN_ESTABLISHED = _f('LPBOT_MIN_ESTABLISHED', 0.30)
MIN_NET_DAY_PCT = _f('LPBOT_MIN_NET_DAY', 0.05)
MIN_TVL_USD = _f('LPBOT_MIN_TVL', 250_000.0)

# --- plumbing ----------------------------------------------------------------
RPC = _s('LPBOT_RPC', _s('SOLANA_RPC_URL', 'https://api.mainnet-beta.solana.com'))
# No default. A signing key location is deployment-specific and does not belong
# in source control, and a bot that guesses where your key lives is a bot that
# might find the wrong one.
WALLET = _s('LPBOT_WALLET', _s('WALLET_SECRET_PATH', ''))


def require_wallet():
    """Fail loudly at startup rather than mysteriously at the first signature."""
    if not WALLET:
        raise SystemExit(
            'No signing key configured. Set LPBOT_WALLET (or WALLET_SECRET_PATH) '
            'to the path of your Solana keypair. Nothing was started.')
    return WALLET


def summary():
    return {
        'pool': POOL or 'scan and choose',
        'pair': PAIR_LABEL,
        'capital_usd': CAPITAL_USD,
        'max_usd': MAX_USD,
        'bands': [f'+/-{(b - 1) * 100:.0f}%' for b in BANDS],
        'max_modelled_rebalances_per_day': MAX_REBALANCES_PER_DAY_MODELLED,
        'poll_seconds': POLL_SECONDS,
        'min_gap_seconds': MIN_REBALANCE_GAP,
        'max_rebalances_per_day': MAX_REBALANCES_PER_DAY,
        'reopt_every_hours': REOPT_INTERVAL / 3600,
        'reopt_min_gain': REOPT_MIN_GAIN,
    }


if __name__ == '__main__':
    import json
    print(json.dumps(summary(), indent=1))
