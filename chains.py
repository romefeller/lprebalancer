"""What the chain can do, as data. The loop asks `caps(config.CHAIN)` before
a step that exists on one chain only. Solana only since 2026-10-19 (the EVM
chains were removed with their profiles: nothing earned there).
"""
import json
import pathlib
import re

# Solana addresses: mints, stablecoins and program ids, one file for Python and Node.
SOLANA = json.loads((pathlib.Path(__file__).parent / 'chains' / 'solana' / 'solana.json').read_text())

CHAINS = {
    'solana': {
        'native_symbol': 'SOL', 'native_decimals': 9, 'gecko_network': 'solana',
        # the mint the signers report native SOL as (balanceA/balanceB)
        'native_mint': SOLANA['native_mint'],
        'stable_mints': tuple(SOLANA['stable_mints'].values()),
        # wallet-wide housekeeping, run by the wallet's residual owner only
        'sweep': True, 'janitor': True, 'audit': True,
        # the board: other pools and venues on this chain
        'scanner': True,
        'payout': True,
        # where the pre-open swap and the payout transfers go: a script of
        # their own (SIGNERS key), or 'venue', the profile's own signer
        'swap_via': 'jupiter', 'payout_via': 'payout',
        # the environment variable that pins the payout destination outside
        # the database (rebalancer.profit_wallet_pinned)
        'pin_env': 'LPBOT_PROFIT_WALLET_PIN',
        # reward tokens swapped and paid out (needs a swap command)
        'rewards': True,
        # harvests measured from their transactions (txfees.py reads Solana)
        'txfees': True,
        # on-chain venue ranking (solana_state.fee_states)
        'venues': True,
        # the JSON-RPC method that proves an endpoint answers (rebalancer.probe_rpc)
        'probe': 'getSlot',
        # the shortest poll the chain's public RPC tolerates (config.POLL_SECONDS)
        'min_poll_seconds': 0,
    },
}

# Address shapes per chain: base58 on Solana. The same rules as the wallets
# and config checks in sql/020.
ADDRESS = {
    'solana': re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}'),
}


def caps(chain):
    """The capability row of `chain`. Unknown chains raise: a profile on a
    chain the code does not know must not start."""
    try:
        return CHAINS[chain]
    except KeyError:
        raise ValueError(f'unknown chain {chain!r}; known: {sorted(CHAINS)}') from None


def is_address(chain, s):
    """Whether `s` is an address on `chain`. fullmatch: a trailing newline is
    not part of an address. Pure."""
    rx = ADDRESS.get(chain)
    return bool(rx) and isinstance(s, str) and bool(rx.fullmatch(s))
