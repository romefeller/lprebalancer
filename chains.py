"""What each chain can do, as data. The loop asks `caps(config.CHAIN)` before
any step that exists on one chain only (Jupiter, the janitor, the Solana
audits), so a profile on another chain skips the step instead of failing it.

Adding a chain is a row here plus a signer that speaks SIGNER_CONTRACT.md.
"""
import re

CHAINS = {
    'solana': {
        'native_symbol': 'SOL', 'native_decimals': 9, 'gecko_network': 'solana',
        # the mint the signers report native SOL as (balanceA/balanceB)
        'native_mint': 'So11111111111111111111111111111111111111112',
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
        # on-chain venue ranking (dexes.fee_states)
        'venues': True,
        # the JSON-RPC method that proves an endpoint answers (rebalancer.probe_rpc)
        'probe': 'getSlot',
        # the shortest poll the chain's public RPC tolerates (config.POLL_SECONDS)
        'min_poll_seconds': 0,
    },
    'base': {
        'native_symbol': 'ETH', 'native_decimals': 18, 'gecko_network': 'base',
        # native ETH counts as WETH (SIGNER_CONTRACT additions, EVM signer)
        'native_mint': '0x4200000000000000000000000000000000000006',
        'sweep': False, 'janitor': False, 'audit': False,
        'scanner': False,
        'payout': True,
        'swap_via': 'venue', 'payout_via': 'venue',
        'pin_env': 'LPBOT_EVM_PROFIT_WALLET_PIN',
        'rewards': False,
        'txfees': False,
        'venues': False,
        'probe': 'eth_blockNumber',
        # the public Base RPC rate-limits after a few calls: no poll under two minutes
        'min_poll_seconds': 120,
        # the public endpoint, and the environment variable that replaces it
        # (config.PUBLIC_RPC)
        'public_rpc': 'https://mainnet.base.org', 'rpc_env': 'LPBOT_BASE_RPC',
    },
    'unichain': {
        'native_symbol': 'ETH', 'native_decimals': 18, 'gecko_network': 'unichain',
        # native ETH counts as WETH, as on Base (the EVM signers report it so)
        'native_mint': '0x4200000000000000000000000000000000000006',
        'sweep': False, 'janitor': False, 'audit': False,
        'scanner': False,
        'payout': True,
        'swap_via': 'venue', 'payout_via': 'venue',
        # one EVM profit wallet serves every EVM chain: the same pin
        'pin_env': 'LPBOT_EVM_PROFIT_WALLET_PIN',
        'rewards': False,
        'txfees': False,
        'venues': False,
        'probe': 'eth_blockNumber',
        'min_poll_seconds': 60,
        'public_rpc': 'https://mainnet.unichain.org', 'rpc_env': 'LPBOT_UNICHAIN_RPC',
    },
    'polygon': {
        'native_symbol': 'POL', 'native_decimals': 18, 'gecko_network': 'polygon_pos',
        # Polygon's native-token address, NOT WPOL: WPOL is a pool token and the
        # EVM balance reader adds native gas to the native mint. Gas POL stays
        # outside the pool, as ETH does on Unichain.
        'native_mint': '0x0000000000000000000000000000000000001010',
        # the wrapped native token, a pool token here: native POL above the
        # profile's native_keep is wrapped into it (rebalancer.wrap_native)
        'wrapped_native': '0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270',
        'sweep': False, 'janitor': False, 'audit': False,
        'scanner': False,
        'payout': True,
        'swap_via': 'venue', 'payout_via': 'venue',
        'pin_env': 'LPBOT_EVM_PROFIT_WALLET_PIN',
        'rewards': False,
        'txfees': False,
        'venues': False,
        'probe': 'eth_blockNumber',
        'min_poll_seconds': 60,
        # polygon-rpc.com answers 403 (2026-10-08); publicnode answers and simulates
        'public_rpc': 'https://polygon-bor-rpc.publicnode.com', 'rpc_env': 'LPBOT_POLYGON_RPC',
    },
}

# Address shapes per chain: base58 on Solana, 0x-hex on EVM chains. The same
# rules as the wallets and config checks in sql/020.
ADDRESS = {
    'solana': re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}'),
    'base': re.compile(r'0x[0-9a-fA-F]{40}'),
    'unichain': re.compile(r'0x[0-9a-fA-F]{40}'),
    'polygon': re.compile(r'0x[0-9a-fA-F]{40}'),
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
