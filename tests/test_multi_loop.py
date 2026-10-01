"""The loop with many profiles on one wallet, end to end, with a fake chain.

Each profile is a process in production; here one process plays them in
turn (as_profile switches config, the run directory and the db context).
The signers are stubbed at rebalancer._chain: the fake below keeps one
wallet's balances and positions and answers balance, status, open,
rebalance (the swap), send and close-empty like the real scripts. Everything
above it is the real code: chain() with the wallet lock and the claims,
wallet() and its sleeve, the dormant rule, reopen, balance_wallet, guards.

Covered: a SOL deposit goes to SOL/USDC; a MU deposit wakes mu-usdc, which
swaps half to USDC (claimed by it) and opens, while sol-usdc ignores the MU
and the claimed USDC; a USDC deposit goes to the residual owner; a profile
with nothing stays quiet over many polls; the sweep never sells another
profile's token; HALT global and per profile; a failed balance read and a
busy lock; Base routes the swap and the payout to its venue signer; a
profile alone on its wallet does what a pre-020 profile does."""
import contextlib
import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import audit
import chains
import config
import db
import health
import rebalancer
import wallets

REAL_CHAIN = rebalancer._chain                  # the node layer, before any test stubs it

SOL = 'So11111111111111111111111111111111111111112'
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
MU = 'MUmintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
JITO = 'J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn'
SOL_POOL = '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj'
MU_POOL = '13MEx6gjRadJNUdmToaGSzgeWHLH7FzScUQS9Mc5nYF5'
WETH = '0x4200000000000000000000000000000000000006'
BUSDC = '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913'
BASE_POOL = '0xb2cc224c1c9fee385f8ad6a55b4d94e92359dc59'
EVM_PROFIT = '0x2b35948898e1b4897E7FC5a70e39b213dcfd0142'
WALLET, ADDRESS = 'e2e-lp', '83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f'
BASE_WALLET, BASE_ADDRESS = 'e2e-base', '0x' + 'ab' * 20

POOLS = {
    SOL_POOL: {'dex': 'raydium-clmm', 'a': SOL, 'b': USDC, 'sa': 'SOL', 'sb': 'USDC', 'price': 150.0, 'native': 'A'},
    MU_POOL: {'dex': 'meteora-dlmm', 'a': MU, 'b': USDC, 'sa': 'MU', 'sb': 'USDC', 'price': 10.0, 'native': None},
    BASE_POOL: {'dex': 'aerodrome-slipstream', 'a': WETH, 'b': BUSDC, 'sa': 'WETH', 'sb': 'USDC', 'price': 2500.0,
                'native': 'A'},
}
PROFILES = {
    'e2e-sol': dict(pool=SOL_POOL, wallet=WALLET, deposit=SOL, residual=True, chain='solana'),
    'e2e-mu': dict(pool=MU_POOL, wallet=WALLET, deposit=MU, residual=False, chain='solana'),
    'e2e-base': dict(pool=BASE_POOL, wallet=BASE_WALLET, deposit=WETH, residual=True, chain='base'),
}
USD = {SOL: 150.0, USDC: 1.0, MU: 10.0, WETH: 2500.0, BUSDC: 1.0, JITO: 160.0}
NATIVE = {SOL, WETH}


class StopPoll(Exception):
    """A poll's closing sleep: the end of one pass of the loop."""


class FakeChain:
    """One wallet (or two: Solana and Base share nothing but this object)
    and the positions on its pools, answering like the signer scripts."""

    def __init__(self, **held):
        self.wallet = {SOL: 0.0, USDC: 0.0, MU: 0.0, JITO: 0.0, WETH: 0.0, BUSDC: 0.0}
        self.wallet.update(held)
        self.positions = {}
        self.calls = []
        self.n = 0
        self.refuse = None                     # a signer refusal for every write ('refused: mint paused')
        self.slot = 1000                       # the chain's slot: every landed write moves it on
        self.sig_slot = {}                     # signature -> the slot it landed in
        self.lags = []                         # slots behind, per confirmed read, for a lagging node
        self.rpc_down = False
        self.rpc_calls = []

    def rpc(self, url, method, params, timeout):
        """The JSON-RPC answers wallets._rpc gets: the node is at self.slot,
        less a lag per read, and answers at the commitment it is asked."""
        self.rpc_calls.append((method, params))
        if self.rpc_down:
            raise OSError('rpc down')
        if method in ('getBalance', 'getTokenAccountsByOwner'):
            assert params[-1].get('commitment') == 'confirmed', params
            at = self.slot - (self.lags.pop(0) if self.lags else 0)
            if method == 'getBalance':
                return {'context': {'slot': at}, 'value': int(round(self.wallet[SOL] * 1e9))}
            amt = self.wallet[params[1]['mint']]
            return {'context': {'slot': at}, 'value': [{'account': {'data': {'parsed': {'info': {
                'tokenAmount': {'uiAmountString': repr(amt)}}}}}}]}
        if method == 'getSignatureStatuses':
            return {'value': [{'slot': self.sig_slot[x], 'confirmationStatus': 'confirmed', 'err': None}
                              if x in self.sig_slot else None for x in params[0]]}
        raise AssertionError(f'unexpected rpc {method}')

    def balance(self, pool):
        p = POOLS[pool]
        a, b = self.wallet[p['a']], self.wallet[p['b']]
        native = self.wallet[SOL if p['dex'] != 'aerodrome-slipstream' else WETH]
        gas_usd = 0.0 if p['native'] else native * 150.0
        return {'owner': ADDRESS if p['dex'] != 'aerodrome-slipstream' else BASE_ADDRESS, 'sol': native,
                'pool': pool, 'dex': p['dex'], 'tokenA': p['sa'], 'tokenB': p['sb'], 'price': p['price'],
                'quoteUsd': 1.0, 'balanceA': a, 'balanceB': b, 'nativeSide': p['native'],
                'walletUsd': round(a * p['price'] + b + gas_usd, 4)}

    def status(self, pool, mint=None):
        pos = self.positions.get(pool)
        if not pos or (mint and pos['mint'] != mint):
            return {'positions': 0, 'positionMint': None, 'pool': pool}
        p = POOLS[pool]
        return {'positionMint': pos['mint'], 'whirlpool': pool, 'pool': pool, 'dex': p['dex'], 'price': p['price'],
                'lowerPrice': pos['lower'], 'upperPrice': pos['upper'], 'inRange': True, 'quoteUsd': 1.0,
                'liquidity': '1', 'closeEstA': pos['a'], 'closeEstB': pos['b'],
                'positionUsd': pos['a'] * p['price'] + pos['b'], 'rentUsd': 0.0,
                'feesAccruedA': 0.0, 'feesAccruedB': 0.0, 'feesAccrued_USD': 0.0}

    def sellable(self, mint, sleeve):
        have = self.wallet[mint]
        if sleeve is not None:
            have = min(have, sleeve.get(mint, 0.0))
        return max(have - (0.05 if mint in NATIVE else 0.0), 0.0)

    def rebalance(self, ma, mb, ta, tb, env):
        """swap_jupiter.mjs planRebalance on balances capped by LPBOT_SLEEVE."""
        sleeve = json.loads(env['LPBOT_SLEEVE']) if env and 'LPBOT_SLEEVE' in env else None
        ua, ub = self.sellable(ma, sleeve) * USD[ma], self.sellable(mb, sleeve) * USD[mb]
        ta, tb = float(ta), float(tb)
        total, want = ua + ub, ta + tb
        da, db_ = (ta, tb) if total >= want else (total * ta / want, total * tb / want)
        if ua < da:
            buy, sell, deficit, usd = ma, mb, da - ua, min(da - ua, max(0.0, ub - db_))
            ref = da
        elif ub < db_:
            buy, sell, deficit, usd = mb, ma, db_ - ub, min(db_ - ub, max(0.0, ua - da))
            ref = db_
        else:
            return {'noop': True}, None
        if deficit <= 0.02 * ref or usd <= 0:
            return {'noop': True}, None
        self.wallet[sell] -= usd / USD[sell]
        self.wallet[buy] += usd / USD[buy]
        self.n += 1
        return {'sent': True, 'signature': f'swap{self.n}', 'swapUsdValue': usd,
                'sold': {'mint': sell, 'amount': usd / USD[sell]}, 'bought': {'mint': buy, 'amount': usd / USD[buy]}}, None

    def open(self, pool, lower, upper, max_a, max_b):
        p = POOLS[pool]
        a = min(float(max_a), self.wallet[p['a']])
        b = min(float(max_b), self.wallet[p['b']])
        v = min(a * p['price'], b)
        if v <= 0:
            return None, 'refused: nothing to deposit'
        self.wallet[p['a']] -= v / p['price']
        self.wallet[p['b']] -= v
        self.n += 1
        mint = f'POS{self.n}x{pool[:6]}'
        self.positions[pool] = {'mint': mint, 'a': v / p['price'], 'b': v, 'lower': float(lower), 'upper': float(upper)}
        return {'positionMint': mint, 'signature': f'open{self.n}', 'depositUsd': 2 * v, 'sent': True}, None

    def __call__(self, *args, dex=None, timeout=420, extra_env=None):
        out, err = self.answer(*args, dex=dex, extra_env=extra_env)
        if out and out.get('signature'):
            self.slot += 1
            self.sig_slot[out['signature']] = self.slot
        return out, err

    def answer(self, *args, dex=None, extra_env=None):
        self.calls.append({'profile': config.PROFILE, 'dex': dex, 'args': args, 'env': dict(extra_env or {})})
        cmd = args[0]
        if self.refuse and cmd in ('open', 'rebalance', 'harvest', 'close'):
            return None, self.refuse
        if cmd == 'balance' and dex not in ('payout',):
            return self.balance(args[1]), None
        if cmd == 'status':
            return self.status(config.POOL, args[1] if len(args) > 1 else None), None
        if cmd == 'rebalance':
            return self.rebalance(*args[1:5], extra_env)
        if cmd == 'open':
            return self.open(*args[1:6])
        if cmd == 'send':
            mint, amt = args[1], float(args[2])
            self.wallet[mint] -= amt
            self.n += 1
            return {'signature': f'send{self.n}', 'sent': True}, None
        if cmd == 'close-empty':
            return {'closable': [], 'reclaimSol': 0.0}, None
        if cmd in ('harvest', 'close'):
            pool = next((k for k, v in self.positions.items() if v['mint'] == args[1]), None)
            if pool is None:
                return None, f'no position {args[1]}'
            self.n += 1
            if cmd == 'close':
                pos = self.positions.pop(pool)
                self.wallet[POOLS[pool]['a']] += pos['a']
                self.wallet[POOLS[pool]['b']] += pos['b']
                return {'closed': args[1], 'signature': f'close{self.n}'}, None
            return {'harvested': args[1], 'signature': f'harv{self.n}'}, None
        return None, f'fake chain: unexpected {args}'

    def of(self, profile, cmd):
        return [c for c in self.calls if c['profile'] == profile and c['args'][0] == cmd]


def pool_record_for(pool):
    p = POOLS[pool]
    return {'dex': p['dex'], 'address': pool, 'pair': f"{p['sa']}/{p['sb']}", 'reward_mints': [],
            'token_a': {'address': p['a'], 'symbol': p['sa'], 'decimals': 9},
            'token_b': {'address': p['b'], 'symbol': p['sb'], 'decimals': 6}}


def setup_db():
    teardown_db()
    with db.cursor(commit=True) as cur:
        cur.execute('insert into wallets (id, chain, address, secret_env) values (%s,%s,%s,%s)',
                    (WALLET, 'solana', ADDRESS, 'WALLET_SECRET_PATH'))
        cur.execute('insert into wallets (id, chain, address, secret_env) values (%s,%s,%s,%s)',
                    (BASE_WALLET, 'base', BASE_ADDRESS, 'LPBOT_EVM_KEY_PATH'))
        for name, p in PROFILES.items():
            cur.execute("insert into config (name, pool, pair_label, capital_usd, max_usd, wallet_id, enabled, "
                        "deposit_mint, residual_owner, dex, pool_pinned) "
                        "values (%s,%s,%s,100,10000,%s,true,%s,%s,%s,true)",
                        (name, p['pool'], 'X/USDC', p['wallet'], p['deposit'], p['residual'], POOLS[p['pool']]['dex']))


def teardown_db():
    names = list(PROFILES)
    with db.cursor(commit=True) as cur:
        cur.execute('delete from wallet_claims where wallet_id in (%s, %s)', (WALLET, BASE_WALLET))
        cur.execute('delete from wallet_settle where wallet_id in (%s, %s)', (WALLET, BASE_WALLET))
        cur.execute('delete from snapshots where mint in (select mint from positions where config_name = any(%s))', (names,))
        cur.execute('delete from band_profile where mint in (select mint from positions where config_name = any(%s))', (names,))
        cur.execute('delete from positions where config_name = any(%s)', (names,))
        cur.execute('delete from payouts where config_name = any(%s)', (names,))
        cur.execute('delete from capital_flows where profile = any(%s)', (names,))
        cur.execute('delete from events where profile = any(%s)', (names,))
        cur.execute("delete from health where key like 'e2e-%%'")
        cur.execute('delete from config where name = any(%s)', (names,))
        cur.execute('delete from wallets where id in (%s, %s)', (WALLET, BASE_WALLET))


class Fixture(unittest.TestCase):
    """One process playing the profiles of the e2e wallets over a FakeChain."""

    def setUp(self):
        setup_db(); self.addCleanup(teardown_db)
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='lp_bot_multi_'))
        self.halt_all = self.tmp / 'HALT'
        self.sleeps = []
        self.chain = FakeChain()
        saved = dict(db.CONTEXT)
        self.addCleanup(lambda: db.CONTEXT.update(saved))
        patches = [
            mock.patch.object(rebalancer, '_chain', self.chain),
            mock.patch.object(rebalancer, 'HALT_ALL', self.halt_all),
            mock.patch.object(rebalancer, 'probe_rpc', lambda *a, **k: None),
            mock.patch.object(rebalancer, 'notify_book', lambda ev, **kw: rebalancer.notify(ev, **kw)),
            mock.patch.object(rebalancer, 'pool_record', lambda: pool_record_for(config.POOL)),
            mock.patch.object(rebalancer, 'best_band_for', lambda pool, dex=None: {
                'band': 1.05, 'price': POOLS[pool]['price'], 'net_day_pct': 0.1, 'rebal_per_day': 0.1,
                'record': pool_record_for(pool)}),
            mock.patch.object(rebalancer.time, 'sleep', self.sleep),
            mock.patch.object(wallets, '_rpc', lambda *a: self.chain.rpc(*a)),
            mock.patch.object(config, 'require_wallet', lambda: 'key'),
            mock.patch.dict(rebalancer.MINT_HOLD, {'why': None}),
        ]
        for p in patches:
            p.start(); self.addCleanup(p.stop)

    def sleep(self, s):
        self.sleeps.append(s)
        if s >= 60:
            raise StopPoll()

    @contextlib.contextmanager
    def as_profile(self, name):
        p = PROFILES[name]
        pool = POOLS[p['pool']]
        run = self.tmp / name
        run.mkdir(exist_ok=True)
        values = dict(PROFILE=name, WALLET_ID=p['wallet'], CHAIN=p['chain'], CAPS=chains.caps(p['chain']),
                      WALLET_ADDRESS=ADDRESS if p['chain'] == 'solana' else BASE_ADDRESS, ENABLED=True,
                      DEPOSIT_MINT=p['deposit'], RESIDUAL_OWNER=p['residual'], MIN_DEPLOY_USD=5.0,
                      DEX=pool['dex'], POOL=p['pool'], PAIR_LABEL=f"{pool['sa']}/{pool['sb']}", POOL_PINNED=True,
                      REGIME_ENABLED=False, CALM_ENABLED=False, REBALANCE_SWAP=True, DEPLOY_ALL=True,
                      EXECUTE_DEXES=(pool['dex'],), MAX_USD=10000.0, CAPITAL_USD=100.0, SIDE_CAP_FRACTION=0.55,
                      GAS_RESERVE_SOL=0.05, PAYOUT_ENABLED=False, HARVEST_INTERVAL=0, PROACTIVE_THRESHOLD=0.0,
                      MAX_CONSECUTIVE_FAILURES=3, POLL_SECONDS=300, CALM_POLL_SECONDS=120,
                      MAX_UNREADABLE_POLLS=12)
        with contextlib.ExitStack() as st:
            for k, v in values.items():
                st.enter_context(mock.patch.object(config, k, v))
            for k, f in (('STATE', 'runtime.json'), ('FEED', 'events.jsonl'), ('HALT', 'HALT'),
                         ('REBALANCE', 'REBALANCE'), ('REOPT', 'REOPT'), ('MIGRATE', 'MIGRATE'), ('CLOSE', 'CLOSE')):
                st.enter_context(mock.patch.object(rebalancer, k, run / f, create=(k == 'CLOSE')))
            st.enter_context(mock.patch.dict(rebalancer._MINTS_SEEN, clear=True))
            db.set_context(name, p['wallet'])
            yield run

    def poll(self, name, n=1):
        """n passes of the loop as profile `name`; main()'s return when it stops."""
        out = None
        with self.as_profile(name):
            for _ in range(n):
                try:
                    out = rebalancer.main()
                except StopPoll:
                    out = None
                if out is not None:
                    break
        return out

    def feed(self, name):
        f = self.tmp / name / 'events.jsonl'
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def events(self, name):
        return [r['event'] for r in self.feed(name)]

    def claim(self, profile, mint=USDC):
        return (wallets.claims(WALLET).get(profile) or {}).get(mint, 0.0)


class Loop(Fixture):
    # --- deposits route by token ------------------------------------------------------

    def test_a_sol_deposit_opens_sol_usdc_and_mu_usdc_stays_dormant(self):
        self.chain.wallet[SOL] = 2.0
        self.poll('e2e-mu')
        self.poll('e2e-sol')
        self.assertIn(SOL_POOL, self.chain.positions)
        self.assertNotIn(MU_POOL, self.chain.positions)
        swap = self.chain.of('e2e-sol', 'rebalance')
        self.assertEqual(len(swap), 1)
        self.assertEqual(swap[0]['dex'], 'jupiter')
        self.assertEqual(json.loads(swap[0]['env']['LPBOT_SLEEVE']), {SOL: 2.0, USDC: 0.0})
        self.assertEqual(self.chain.of('e2e-mu', 'open') + self.chain.of('e2e-mu', 'rebalance'), [])
        self.assertEqual(self.events('e2e-mu').count('dormant'), 1)
        self.assertEqual(wallets.claims(WALLET), {})                 # the holder never claims
        row = self.feed('e2e-sol')[-1]
        self.assertEqual((row['profile'], row['wallet_id'], row['chain'], row['pair']),
                         ('e2e-sol', WALLET, 'solana', 'SOL/USDC'))
        with db.cursor() as cur:
            cur.execute('select config_name from positions where pool = %s', (SOL_POOL,))
            self.assertEqual([r['config_name'] for r in cur.fetchall()], ['e2e-sol'])

    def test_a_mu_deposit_wakes_mu_usdc_whose_usdc_is_claimed_and_invisible_to_sol_usdc(self):
        self.chain.wallet.update({SOL: 2.0, USDC: 40.0})
        self.poll('e2e-mu')                                          # nothing of its own: dormant
        self.assertEqual(self.events('e2e-mu'), ['startup', 'dormant'])
        self.chain.wallet[MU] = 10.0                                 # $100 of MU arrives
        self.poll('e2e-mu')
        ev = self.events('e2e-mu')
        self.assertEqual(ev.count('deposit_seen'), 1)
        self.assertIn('SWAP', ev); self.assertIn('OPEN', ev)
        swap = self.chain.of('e2e-mu', 'rebalance')[0]
        self.assertEqual(json.loads(swap['env']['LPBOT_SLEEVE']), {MU: 10.0, USDC: 0.0})   # not sol-usdc's 40 USDC
        self.assertAlmostEqual(self.chain.wallet[MU] * 10 + (self.chain.wallet[USDC] - 40.0), 0.0, places=6)
        pos = self.chain.positions[MU_POOL]
        self.assertAlmostEqual(pos['a'] * 10 + pos['b'], 100.0, places=6)
        # the swap's USDC went to mu-usdc's claim, the open took it back out
        self.assertAlmostEqual(self.claim('e2e-mu'), self.chain.wallet[USDC] - 40.0, places=9)
        # sol-usdc: the MU is not in its pool, the 40 USDC are all its own
        with self.as_profile('e2e-sol'):
            v = rebalancer.wallet(SOL_POOL)
        self.assertAlmostEqual(v['balanceB'], 40.0, places=9)
        self.assertEqual(v['rawBalanceB'], self.chain.wallet[USDC])
        before, claimed = self.chain.wallet[USDC], self.claim('e2e-mu')
        self.poll('e2e-sol')
        self.assertIn(SOL_POOL, self.chain.positions)
        # sol-usdc swapped and deposited from its own 40 USDC and its SOL only:
        # mu-usdc's claimed USDC is still in the wallet, and still claimed
        self.assertGreaterEqual(self.chain.wallet[USDC] + 1e-9, self.claim('e2e-mu'))
        self.assertLessEqual(before - self.chain.wallet[USDC], 40.0 + 1e-9)
        sleeve_b = json.loads(self.chain.of('e2e-sol', 'rebalance')[0]['env']['LPBOT_SLEEVE'])[USDC]
        self.assertAlmostEqual(sleeve_b, 40.0, places=9)
        self.assertEqual(self.claim('e2e-mu'), claimed)            # the holder's writes book no claim

    def test_the_first_open_writes_the_profiles_baseline_once(self):
        self.chain.wallet.update({MU: 10.0, SOL: 0.2})
        self.poll('e2e-mu')
        q = ("select sol, usdc, usd, price, amounts, wallet_id, profile from capital_flows "
             "where kind = 'baseline' and profile = 'e2e-mu'")
        with db.cursor() as cur:
            cur.execute(q)
            rows = cur.fetchall()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((float(r['sol']), float(r['usdc']), float(r['usd']), r['price'], r['wallet_id']),
                         (0.0, 0.0, 100.0, 10.0, WALLET))                  # the sleeve as funded: 10 MU at $10
        self.assertEqual(r['amounts'], {MU: 10.0, USDC: 0.0})
        self.chain.positions.clear()                                     # the position is gone: a reopen
        self.poll('e2e-mu')
        with db.cursor() as cur:
            cur.execute(q)
            self.assertEqual(len(cur.fetchall()), 1)

    def test_a_sol_profiles_baseline_counts_sol_and_a_pre_020_one_writes_none(self):
        self.chain.wallet.update({SOL: 2.0, USDC: 30.0})
        with self.as_profile('e2e-sol'), mock.patch.object(config, 'WALLET_ID', None):
            self.assertIs(rebalancer.record_baseline(self.chain.balance(SOL_POOL)), False)
        self.poll('e2e-sol')
        with db.cursor() as cur:
            cur.execute("select sol, usdc, amounts from capital_flows where kind = 'baseline' and profile = 'e2e-sol'")
            rows = cur.fetchall()
        self.assertEqual([(float(r['sol']), float(r['usdc'])) for r in rows], [(2.0, 30.0)])

    def test_a_usdc_deposit_belongs_to_the_residual_owner(self):
        self.chain.wallet.update({SOL: 0.06, USDC: 100.0})          # gas, and $100 of USDC
        self.poll('e2e-mu')
        self.assertEqual(self.chain.of('e2e-mu', 'rebalance') + self.chain.of('e2e-mu', 'open'), [])
        self.assertIn('dormant', self.events('e2e-mu'))
        self.poll('e2e-sol')
        self.assertIn(SOL_POOL, self.chain.positions)
        self.assertEqual(json.loads(self.chain.of('e2e-sol', 'rebalance')[0]['env']['LPBOT_SLEEVE'])[USDC], 100.0)
        self.assertEqual(self.claim('e2e-mu'), 0.0)

    # --- a profile with nothing --------------------------------------------------------------

    def test_a_dormant_profile_with_zero_balance_stays_quiet_over_many_polls(self):
        self.chain.wallet[SOL] = 0.06                                  # gas only, and it is sol-usdc's
        self.poll('e2e-mu', n=25)
        ev = self.events('e2e-mu')
        self.assertEqual(ev.count('dormant'), 1)
        self.assertEqual([e for e in ev if e not in ('startup', 'dormant')], [])
        writes = [c for c in self.chain.calls if '--execute' in c['args']]
        self.assertEqual(writes, [])
        self.assertTrue(all(s >= rebalancer.DORMANT_POLL_S for s in self.sleeps if s >= 60))
        self.assertEqual([r for r in health.summary() if r['fails']], [])
        with self.as_profile('e2e-mu'):
            self.assertFalse(rebalancer.halted())
            self.assertEqual(rebalancer.load()['failures'], 0)
        # one wallet read and one status read a poll, nothing else
        self.assertEqual({c['args'][0] for c in self.chain.calls}, {'balance', 'status'})

    def test_a_dormant_profile_without_a_readable_wallet_stays_dormant_and_silent(self):
        self.poll('e2e-mu')
        self.assertIn('dormant', self.events('e2e-mu'))
        real = self.chain.__call__

        def no_balance(*a, **k):
            if a[0] == 'balance':
                return None, 'RPC rate limited'
            return real(*a, **k)
        with mock.patch.object(rebalancer, '_chain', no_balance):
            self.poll('e2e-mu', n=5)
        self.assertEqual([e for e in self.events('e2e-mu') if e not in ('startup', 'dormant')], [])

    def test_a_dormant_profile_with_unreadable_status_neither_speaks_nor_halts(self):
        self.poll('e2e-mu')
        real = self.chain.__call__

        def no_status(*a, **k):
            return (None, 'RPC rate limited') if a[0] == 'status' else real(*a, **k)
        with mock.patch.object(rebalancer, '_chain', no_status):
            self.assertIsNone(self.poll('e2e-mu', n=30))             # 30 > max_unreadable_polls (12)
        self.assertEqual([e for e in self.events('e2e-mu') if e not in ('startup', 'dormant')], [])
        with self.as_profile('e2e-mu'):
            self.assertIsNone(rebalancer.halted())

    # --- tokenized stocks: a paused mint, the gas for a DLMM open ------------------------------

    def test_a_paused_mint_holds_quietly_and_counts_no_failure(self):
        self.chain.wallet.update({MU: 10.0, SOL: 0.2})
        self.chain.refuse = 'refused: mint paused'
        self.poll('e2e-mu', n=6)
        ev = self.events('e2e-mu')
        self.assertEqual(ev.count('mint_paused'), 1)
        for bad in ('open_failed', 'swap_failed', 'swap_skipped', 'BREAKER', 'open_refused', 'idle'):
            self.assertNotIn(bad, ev)
        with self.as_profile('e2e-mu'):
            self.assertEqual(rebalancer.load()['failures'], 0)
            self.assertIsNone(rebalancer.halted())
        self.assertEqual([r for r in health.summary() if r['fails']], [])
        self.chain.refuse = None                                     # the issuer lifts the pause
        self.poll('e2e-mu')
        self.assertIn('mint_resumed', self.events('e2e-mu'))
        self.assertIn(MU_POOL, self.chain.positions)

    def test_a_paused_mint_holds_an_open_position_without_closing_it(self):
        self.chain.wallet.update({MU: 10.0, SOL: 0.2})
        self.poll('e2e-mu')
        mint = self.chain.positions[MU_POOL]['mint']
        self.chain.refuse = 'refused: transfer hook'
        with self.as_profile('e2e-mu'):
            state = rebalancer.load()
            status, _ = rebalancer.read_status()
            rebalancer.rebalance(state, status, 'price went above')
            rebalancer.rebalance(state, status, 'price went above')
            self.assertEqual(state['failures'], 0)
        self.assertEqual(self.chain.positions[MU_POOL]['mint'], mint)
        self.assertEqual(self.events('e2e-mu').count('mint_paused'), 1)
        self.assertNotIn('close_failed', self.events('e2e-mu'))

    def test_a_dlmm_open_waits_for_gas_it_would_take_from_the_others(self):
        self.chain.wallet.update({MU: 10.0, SOL: 0.08})              # under 0.05 reserve + 0.05 bin-array rent
        self.poll('e2e-mu', n=4)
        self.assertEqual(self.events('e2e-mu').count('gas_short'), 1)
        self.assertEqual(self.chain.of('e2e-mu', 'rebalance') + self.chain.of('e2e-mu', 'open'), [])
        with self.as_profile('e2e-mu'):
            self.assertEqual(rebalancer.load()['failures'], 0)
        self.chain.wallet[SOL] = 0.2
        self.poll('e2e-mu')
        self.assertIn(MU_POOL, self.chain.positions)

    def test_the_native_owner_keeps_back_the_other_profiles_open_rent(self):
        self.chain.wallet.update({SOL: 2.0, USDC: 300.0})
        self.poll('e2e-sol')
        need = 0.05 + rebalancer.open_headroom('raydium-clmm') + rebalancer.open_headroom('meteora-dlmm')
        self.assertGreaterEqual(self.chain.wallet[SOL] + 1e-9, need)
        with self.as_profile('e2e-sol'):
            v = rebalancer.wallet(SOL_POOL)
        self.assertAlmostEqual(v['nativeReserve'], need)

    # --- a disabled profile -------------------------------------------------------------------

    def disable(self, name):
        with db.cursor(commit=True) as cur:
            cur.execute('update config set enabled = false where name = %s', (name,))

    def test_a_disabled_profile_without_a_position_stops(self):
        self.chain.wallet.update({MU: 10.0, SOL: 0.2})
        self.disable('e2e-mu')
        self.assertEqual(self.poll('e2e-mu', n=3), 0)                    # main() returns 0: systemd leaves it
        self.assertEqual([c for c in self.chain.calls if '--execute' in c['args']], [])
        self.assertEqual(self.events('e2e-mu'), ['startup', 'disabled'])

    def test_a_disabled_profile_holds_its_position_and_closes_only_on_request(self):
        self.chain.wallet.update({MU: 10.0, SOL: 0.2})
        self.poll('e2e-mu')
        mint = self.chain.positions[MU_POOL]['mint']
        self.chain.calls.clear()
        self.disable('e2e-mu')
        self.assertIsNone(self.poll('e2e-mu', n=4))
        self.assertEqual([c for c in self.chain.calls if '--execute' in c['args']], [])
        self.assertEqual(self.events('e2e-mu').count('disabled'), 1)          # said once
        self.assertEqual(self.chain.positions[MU_POOL]['mint'], mint)
        (self.tmp / 'e2e-mu' / 'CLOSE').write_text('')
        self.poll('e2e-mu')
        writes = [c['args'][0] for c in self.chain.calls if '--execute' in c['args']]
        self.assertEqual(writes, ['harvest', 'close'])                    # no swap, no open
        self.assertNotIn(MU_POOL, self.chain.positions)
        self.assertFalse((self.tmp / 'e2e-mu' / 'CLOSE').exists())
        self.assertEqual(self.poll('e2e-mu'), 0)                          # now empty: it stops

    def test_a_disabled_profiles_claim_and_mints_stay_its_own(self):
        self.chain.wallet.update({USDC: 100.0, SOL: 1.0})
        wallets.adjust(WALLET, 'e2e-mu', {USDC: 30.0})
        wallets.register_mints('e2e-mu', [MU, USDC])
        self.disable('e2e-mu')
        with self.as_profile('e2e-sol'):
            v = rebalancer.wallet(SOL_POOL)
            self.assertEqual(v['balanceB'], 70.0)                         # the residual owner leaves the claim
            self.assertIn(MU, rebalancer.wallet_mints())                  # the sweep and the janitor keep MU
            self.assertIn(MU, rebalancer.wallet_book()['mints'])

    # --- wallet-wide chores ---------------------------------------------------------------------

    def test_the_sweep_never_sells_another_profiles_token(self):
        wallets.register_mints('e2e-mu', [MU, USDC])
        accounts = [{'mint': MU, 'amount': 5 * 10 ** 9, 'decimals': 9, 'lamports': 2039280},
                    {'mint': JITO, 'amount': 10 ** 9, 'decimals': 9, 'lamports': 2039280},
                    {'mint': USDC, 'amount': 10 ** 7, 'decimals': 6, 'lamports': 2039280}]
        swaps = []
        with mock.patch.object(audit, 'token_accounts', lambda url, owner: accounts), \
                mock.patch.object(rebalancer.dexes, 'jupiter_prices', lambda ms: {m: USD.get(m, 0.0) for m in ms}), \
                mock.patch.object(rebalancer.dexes, 'jupiter_token', lambda m: {'verified': True, 'symbol': m[:4]}), \
                mock.patch.object(rebalancer, 'chain', lambda *a, **k: (swaps.append(a), ({'signature': 'sw'}, None))[1]):
            with self.as_profile('e2e-mu'):
                self.assertEqual(rebalancer.sweep_foreign({}, {'owner': ADDRESS}), [])     # not the residual owner
            self.assertEqual(swaps, [])
            with self.as_profile('e2e-sol'):
                done = rebalancer.sweep_foreign({}, {'owner': ADDRESS})
        self.assertEqual([d['mint'] for d in done], [JITO])
        self.assertEqual([a[1] for a in swaps], [JITO])
        self.assertNotIn(MU, [a[1] for a in swaps])

    def test_the_janitor_keeps_every_profiles_mints_and_only_the_owner_runs_it(self):
        wallets.register_mints('e2e-mu', [MU, USDC])
        with self.as_profile('e2e-mu'):
            self.assertIsNone(rebalancer.janitor({}))
        self.assertEqual(self.chain.of('e2e-mu', 'close-empty'), [])
        with self.as_profile('e2e-sol'):
            rebalancer.janitor({})
        keep = set(self.chain.of('e2e-sol', 'close-empty')[0]['args'][1:])
        self.assertLessEqual({SOL, USDC, MU}, keep)

    def test_audits_run_in_the_residual_owner_only_with_the_wallet_book(self):
        runs = []
        with mock.patch.object(rebalancer.audit, 'run', lambda *a, **k: runs.append(k['wallet']) or {}):
            with self.as_profile('e2e-mu'):
                self.assertIsNone(rebalancer.run_audits({}))
            with self.as_profile('e2e-sol'):
                rebalancer.run_audits({})
        self.assertEqual(len(runs), 1)
        self.assertEqual(sorted(p['name'] for p in runs[0]['profiles']), ['e2e-mu', 'e2e-sol'])
        self.assertLessEqual({MU, SOL}, runs[0]['mints'])

    # --- HALT ------------------------------------------------------------------------------------

    def test_a_global_halt_stops_every_profile_a_profiles_halt_only_that_one(self):
        self.chain.wallet.update({SOL: 2.0, MU: 10.0})
        self.halt_all.write_text('operator')
        self.assertEqual(self.poll('e2e-mu'), 2)
        self.assertEqual(self.poll('e2e-sol'), 2)
        self.assertEqual(self.chain.calls, [])
        self.halt_all.unlink()
        (self.tmp / 'e2e-mu' / 'HALT').write_text('mu only')
        self.assertEqual(self.poll('e2e-mu'), 2)
        self.assertEqual(self.chain.of('e2e-mu', 'status'), [])
        self.assertIsNone(self.poll('e2e-sol'))
        self.assertIn(SOL_POOL, self.chain.positions)

    def test_no_write_is_spawned_while_halted_global_or_this_profiles(self):
        spawned = []
        with self.as_profile('e2e-mu') as run, mock.patch.object(rebalancer.subprocess, 'run',
                                                                 lambda *a, **k: spawned.append(a)):
            (run / 'HALT').write_text('mu only')
            self.assertEqual(REAL_CHAIN('open', MU_POOL, '9', '11', '1', '1', '--execute', dex='meteora-dlmm'),
                             (None, 'refused: halted (mu only)'))
            (run / 'HALT').unlink()
            self.halt_all.write_text('everyone')
            out, err = REAL_CHAIN('send', USDC, '1', 'x', '--execute', dex='payout')
            self.assertEqual(err, 'refused: halted (everyone)')
            self.assertTrue(rebalancer.held(err))
        self.assertEqual(spawned, [])                                     # not one signer process

    def test_a_breaker_halt_writes_the_profiles_halt_not_the_global_one(self):
        with self.as_profile('e2e-mu') as run:
            rebalancer.halt('3 consecutive failures')
            self.assertTrue((run / 'HALT').exists())
        self.assertFalse(self.halt_all.exists())
        self.assertIsNone(self.poll('e2e-sol'))

    # --- the claims under failure -------------------------------------------------------------

    def test_a_write_whose_claims_cannot_be_measured_is_not_sent(self):
        self.chain.wallet[MU] = 10.0
        self.chain.rpc_down = True                                         # the before-read fails
        with self.as_profile('e2e-mu'):
            for _ in range(3):
                out, err = rebalancer.chain('rebalance', MU, USDC, '50', '50', '--execute', dex='jupiter')
                self.assertIsNone(out)
                self.assertRegex(err, r'^refused: claims unmeasurable \(balance before the write unreadable\)')
            self.assertTrue(rebalancer.held(err))
        self.assertEqual(self.chain.of('e2e-mu', 'rebalance'), [])        # nothing sent
        self.assertEqual(self.events('e2e-mu'), ['claim_unmeasured'])      # said once
        self.chain.rpc_down = False
        with self.as_profile('e2e-mu'), mock.patch.object(rebalancer, 'pool_record', side_effect=RuntimeError('api')):
            out, err = rebalancer.chain('rebalance', MU, USDC, '50', '50', '--execute', dex='jupiter')
        self.assertRegex(err, r'^refused: claims unmeasurable \(mints unknown')
        self.assertEqual(self.chain.of('e2e-mu', 'rebalance'), [])

    def test_a_write_measured_on_a_lagging_node_waits_for_its_slot(self):
        # The old reads asked finalized commitment and re-read once after 3 s:
        # a node behind the write's slot booked nothing and the USDC went to
        # the residual owner.
        self.chain.wallet[MU] = 10.0
        self.chain.lags = [0] + [3, 2, 1]                                  # before; then a node catching up
        with self.as_profile('e2e-mu'):
            out, err = rebalancer.chain('rebalance', MU, USDC, '50', '50', '--execute', dex='jupiter',
                                        extra_env={'LPBOT_SLEEVE': json.dumps({MU: 10.0, USDC: 0.0})})
        self.assertTrue(out['sent']); self.assertIsNone(err)
        self.assertAlmostEqual(self.claim('e2e-mu'), self.chain.wallet[USDC])
        self.assertGreater(self.claim('e2e-mu'), 0)
        self.assertEqual(wallets.settle_state(WALLET), (self.chain.slot, None))

    def test_an_unmeasured_write_stays_pending_and_is_booked_before_the_next_write(self):
        self.chain.wallet.update({MU: 10.0, SOL: 1.0})
        self.chain.lags = [0] + [1] * 200                                  # no read reaches the write's slot
        with self.as_profile('e2e-mu'):
            out, err = rebalancer.chain('rebalance', MU, USDC, '50', '50', '--execute', dex='jupiter',
                                        extra_env={'LPBOT_SLEEVE': json.dumps({MU: 10.0, USDC: 0.0})})
        self.assertTrue(out['sent'])
        self.assertEqual(self.claim('e2e-mu'), 0.0)
        self.assertIn('claim_unsettled', self.events('e2e-mu'))
        self.assertEqual(wallets.settle_state(WALLET)[1]['signatures'], [out['signature']])
        gained = self.chain.wallet[USDC]
        self.chain.lags = []
        with self.as_profile('e2e-sol'):                                    # any next write books it first
            rebalancer.chain('send', USDC, '0.000001', 'x', '--execute', dex='payout')
        self.assertAlmostEqual(self.claim('e2e-mu'), gained)
        self.assertIsNone(wallets.settle_state(WALLET)[1])

    def test_a_pending_write_blocks_every_write_of_the_wallet_until_booked(self):
        wallets.set_pending(WALLET, {'profile': 'e2e-mu', 'command': 'open', 'mints': [USDC], 'before': {USDC: 0.0},
                                     'before_slot': self.chain.slot + 50, 'signatures': [], 'sent_at': time.time()})
        with self.as_profile('e2e-sol'):
            out, err = rebalancer.chain('send', USDC, '1', 'x', '--execute', dex='payout')
        self.assertIsNone(out)
        self.assertRegex(err, r'^refused: claims unmeasurable \(an earlier write of the wallet is not booked yet\)')
        self.assertEqual(self.chain.of('e2e-sol', 'send'), [])

    def test_a_busy_wallet_lock_sends_nothing_and_trips_no_breaker(self):
        with wallets.wallet_lock(WALLET, wait_s=1), mock.patch.object(wallets, 'LOCK_WAIT_S', 0.3), \
                mock.patch.object(wallets, 'LOCK_POLL_S', 0.05):
            with self.as_profile('e2e-mu'):
                out, err = rebalancer.chain('open', MU_POOL, '9', '11', '1', '1', '--execute')
        self.assertIsNone(out)
        self.assertRegex(err, r'^refused: wallet e2e-lp lock busy')
        self.assertEqual(self.chain.calls, [])
        self.assertIn('wallet_lock_timeout', self.events('e2e-mu'))
        self.assertEqual(health.load('venue:meteora-dlmm')['fails'], 0)

    def test_a_busy_lock_holds_opens_and_closes_without_counting_failures(self):
        self.chain.wallet.update({MU: 10.0, SOL: 0.2})
        with wallets.wallet_lock(WALLET, wait_s=1), mock.patch.object(wallets, 'LOCK_WAIT_S', 0.2), \
                mock.patch.object(wallets, 'LOCK_POLL_S', 0.05):
            self.poll('e2e-mu', n=5)                                      # five polls of a busy wallet
            with self.as_profile('e2e-mu'):
                self.assertEqual(rebalancer.load()['failures'], 0)
                self.assertIsNone(rebalancer.halted())
        self.assertEqual([c for c in self.chain.calls if '--execute' in c['args']], [])
        self.poll('e2e-mu')                                               # free again: it opens
        mint = self.chain.positions[MU_POOL]['mint']
        with wallets.wallet_lock(WALLET, wait_s=1), mock.patch.object(wallets, 'LOCK_WAIT_S', 0.2), \
                mock.patch.object(wallets, 'LOCK_POLL_S', 0.05):
            with self.as_profile('e2e-mu'):
                state = rebalancer.load()
                status, _ = rebalancer.read_status()
                for _ in range(4):
                    rebalancer.rebalance(state, status, 'price went above')
                self.assertEqual(state['failures'], 0)
                self.assertIsNone(rebalancer.halted())
        self.assertEqual(self.chain.positions[MU_POOL]['mint'], mint)

    def test_an_overdraw_is_reported_and_floored(self):
        self.chain.wallet[USDC] = 30.0
        with self.as_profile('e2e-mu'):
            rebalancer.chain('send', USDC, '20', 'x', '--execute', dex='payout')   # spends USDC it never claimed
        self.assertEqual(self.claim('e2e-mu'), 0.0)
        row = [r for r in self.feed('e2e-mu') if r['event'] == 'claim_overdraw'][0]
        self.assertEqual((row['mint'], row['claim'], row['delta'], row['overdraw']), (USDC, 0.0, -20.0, 20.0))

    def test_one_portfolio_a_day_from_the_first_wallets_residual_owner(self):
        sent = []
        with mock.patch.object(rebalancer.stats, 'portfolio', lambda: sent.append(1) or {'total': {'equity_usd': 1.0}}), \
                mock.patch.object(rebalancer.db, 'wallets', lambda: [{'id': WALLET}, {'id': BASE_WALLET}]):
            for name in ('e2e-sol', 'e2e-mu'):               # e2e-lp sorts after e2e-base
                with self.as_profile(name):
                    self.assertIsNone(rebalancer.portfolio_report({}))
            with self.as_profile('e2e-base'):
                state = {}
                self.assertEqual(rebalancer.portfolio_report(state), {'total': {'equity_usd': 1.0}})
                self.assertIsNone(rebalancer.portfolio_report(state))     # once a day
        self.assertEqual(sent, [1])
        self.assertEqual(self.events('e2e-base'), ['PORTFOLIO'])

    def test_reads_are_not_locked_and_book_nothing(self):
        with wallets.wallet_lock(WALLET, wait_s=1):
            with self.as_profile('e2e-mu'):
                out, err = rebalancer.chain('status')
        self.assertEqual(out['positionMint'], None)
        self.assertEqual(wallets.claims(WALLET), {})

    # --- Base -------------------------------------------------------------------------------------

    def test_base_swaps_and_pays_through_the_venue_signer(self):
        self.chain.wallet[WETH] = 0.1                                   # $250 of ETH, gas kept
        self.poll('e2e-base')
        swap = self.chain.of('e2e-base', 'rebalance')
        self.assertEqual(len(swap), 1)
        self.assertEqual(swap[0]['dex'], 'aerodrome-slipstream')
        self.assertEqual(json.loads(swap[0]['env']['LPBOT_SLEEVE']), {WETH: 0.1, BUSDC: 0.0})
        self.assertEqual(self.chain.of('e2e-base', 'open')[0]['dex'], 'aerodrome-slipstream')
        self.assertIn(BASE_POOL, self.chain.positions)
        self.chain.wallet[BUSDC] += 3.0                                 # a harvest's USDC fees
        with self.as_profile('e2e-base'), \
                mock.patch.object(config, 'PAYOUT_ENABLED', True), mock.patch.object(config, 'PROFIT_WALLET', EVM_PROFIT), \
                mock.patch.object(config, 'PAYOUT_MINT', BUSDC), \
                mock.patch.dict('os.environ', {'LPBOT_EVM_PROFIT_WALLET_PIN': EVM_PROFIT, 'LPBOT_PROFIT_WALLET_PIN': ''}):
            state = rebalancer.load()
            rebalancer.distribute(state, 'POSB', 0.0, 3.0)
        send = self.chain.of('e2e-base', 'send')
        self.assertEqual(len(send), 1)
        self.assertEqual(send[0]['dex'], 'aerodrome-slipstream')
        self.assertEqual(send[0]['args'][1:4], (BUSDC, '3.000000000', EVM_PROFIT))

    def test_base_compares_addresses_in_any_letter_case(self):
        # The pool record and the pin in EIP-55 case, the database in lower case:
        # the same token and the same wallet, so the USDC fees are paid.
        self.chain.wallet.update({BUSDC: 3.0, WETH: 0.06})
        rec = pool_record_for(BASE_POOL)
        rec['token_b'] = dict(rec['token_b'], address='0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913')
        with self.as_profile('e2e-base'), mock.patch.object(rebalancer, 'pool_record', lambda: rec), \
                mock.patch.object(config, 'PAYOUT_ENABLED', True), \
                mock.patch.object(config, 'PROFIT_WALLET', EVM_PROFIT.lower()), \
                mock.patch.object(config, 'PAYOUT_MINT', BUSDC), \
                mock.patch.dict('os.environ', {'LPBOT_EVM_PROFIT_WALLET_PIN': EVM_PROFIT}):
            self.assertTrue(rebalancer.profit_wallet_pinned())
            self.assertEqual(rebalancer.pool_tokens()[1][0], BUSDC)
            parts = rebalancer.distribute(rebalancer.load(), 'POSB', 0.0, 3.0)
        self.assertEqual([p['kind'] for p in parts], ['paid'])
        self.assertEqual(self.chain.of('e2e-base', 'send')[0]['args'][1], BUSDC)

    def test_base_payout_refuses_a_destination_off_the_evm_pin(self):
        self.chain.wallet.update({BUSDC: 3.0, WETH: 0.06})             # gas above the reserve
        with self.as_profile('e2e-base'), \
                mock.patch.object(config, 'PAYOUT_ENABLED', True), mock.patch.object(config, 'PROFIT_WALLET', EVM_PROFIT), \
                mock.patch.object(config, 'PAYOUT_MINT', BUSDC), \
                mock.patch.dict('os.environ', {'LPBOT_EVM_PROFIT_WALLET_PIN': '0x' + '11' * 20,
                                               'LPBOT_PROFIT_WALLET_PIN': EVM_PROFIT}):
            state = rebalancer.load()
            rebalancer.distribute(state, 'POSB', 0.0, 3.0)
            self.assertAlmostEqual(state['payout_owed'][BUSDC], 3.0)
        self.assertEqual(self.chain.of('e2e-base', 'send'), [])
        self.assertIn('payout_refused', self.events('e2e-base'))

    def test_without_its_script_the_venue_signer_is_no_signer(self):
        with self.as_profile('e2e-base'), \
                mock.patch.dict(rebalancer.SIGNERS, {'aerodrome-slipstream': str(self.tmp / 'signer_aerodrome.mjs')}), \
                mock.patch.object(rebalancer, '_chain', REAL_CHAIN):
            out, err = rebalancer.chain('balance', BASE_POOL)
        self.assertEqual((out, err), (None, 'no signer for aerodrome-slipstream'))

    def test_the_sleeve_reaches_the_orca_fallback_swap(self):
        self.chain.wallet.update({SOL: 2.0, USDC: 40.0})
        real = self.chain.__call__

        def jupiter_down(*a, **k):
            if k.get('dex') == 'jupiter' and a[0] == 'rebalance':
                self.chain.calls.append({'profile': config.PROFILE, 'dex': 'jupiter', 'args': a,
                                         'env': dict(k.get('extra_env') or {})})
                return None, 'Jupiter 500 on /swap/v1/quote: upstream error'
            return real(*a, **k)
        with mock.patch.object(rebalancer, '_chain', jupiter_down):
            self.poll('e2e-sol')
        jup = [c for c in self.chain.calls if c['dex'] == 'jupiter']
        orca = [c for c in self.chain.calls if c['dex'] == rebalancer.SWAP_FALLBACK]
        self.assertEqual((len(jup), len(orca)), (1, 1))
        self.assertEqual(orca[0]['args'], jup[0]['args'])
        self.assertEqual(json.loads(orca[0]['env']['LPBOT_SLEEVE']), {SOL: 2.0, USDC: 40.0})
        self.assertIn('swap_fallback', self.events('e2e-sol'))

    # --- backward compatibility ---------------------------------------------------------------

    def test_alone_on_its_wallet_a_profile_moves_as_a_pre_020_profile(self):
        def run(wallet_id):
            self.chain = FakeChain(**{SOL: 2.0, USDC: 30.0})
            with mock.patch.object(rebalancer, '_chain', self.chain):
                with db.cursor(commit=True) as cur:
                    # alone: no other profile on the wallet (a disabled one would still count)
                    cur.execute("update config set wallet_id = null where name = 'e2e-mu'")
                with self.as_profile('e2e-sol'), mock.patch.object(config, 'WALLET_ID', wallet_id), \
                        mock.patch.object(config, 'RESIDUAL_OWNER', True):
                    for f in ('runtime.json', 'events.jsonl'):
                        (self.tmp / 'e2e-sol' / f).unlink(missing_ok=True)
                    try:
                        rebalancer.main()
                    except StopPoll:
                        pass
            return ([(c['dex'], c['args']) for c in self.chain.calls],
                    [c['env'] for c in self.chain.calls], dict(self.chain.wallet), self.events('e2e-sol'))
        legacy, legacy_env, legacy_wallet, legacy_ev = run(None)
        multi, multi_env, multi_wallet, multi_ev = run(WALLET)
        self.assertEqual(multi, legacy)                         # the same commands, the same amounts
        self.assertEqual(multi_wallet, legacy_wallet)
        self.assertEqual(multi_ev, legacy_ev)
        # the one difference: the swap is told its sleeve, which is the whole wallet
        swap_env = [e for e, (d, a) in zip(multi_env, multi) if a[0] == 'rebalance'][0]
        self.assertEqual(json.loads(swap_env['LPBOT_SLEEVE']), {SOL: 2.0, USDC: 30.0})
        self.assertEqual([e for e in legacy_env if 'LPBOT_SLEEVE' in e], [])



class Parts(Fixture):
    """The pieces of the loop above, one at a time, at their edges."""

    def test_halted_names_the_file_when_it_is_empty(self):
        with self.as_profile('e2e-mu') as run:
            self.assertIsNone(rebalancer.halted())
            (run / 'HALT').write_text('')
            self.assertEqual(rebalancer.halted(), str(run / 'HALT'))
            (run / 'HALT').write_text(' mu only \n')
            self.assertEqual(rebalancer.halted(), 'mu only')
            self.halt_all.write_text('everyone')
            self.assertEqual(rebalancer.halted(), 'everyone')               # the global one first

    def test_chores_need_the_residual_owner_and_the_chain(self):
        with self.as_profile('e2e-base'):
            self.assertEqual([rebalancer.housekeeper(k) for k in ('sweep', 'janitor', 'audit', 'scanner')],
                             [False] * 4)                                    # the owner, on a chain without them
        with self.as_profile('e2e-mu'):
            self.assertFalse(rebalancer.housekeeper('sweep'))                # not the owner
        with self.as_profile('e2e-sol'):
            self.assertTrue(rebalancer.housekeeper('sweep'))

    def test_stable_mints_by_address_in_any_case(self):
        self.assertTrue(rebalancer.is_stable_mint(USDC))
        self.assertTrue(rebalancer.is_stable_mint('0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'))
        self.assertFalse(rebalancer.is_stable_mint(MU))
        self.assertIs(rebalancer.is_stable_mint(SOL), False)

    # settle_pending: a write's claims, booked from reads at or past its slot
    def pend(self, before, signatures=(), sent_at=None, mints=(USDC,), before_slot=None, profile='e2e-mu'):
        wallets.set_pending(WALLET, {'profile': profile, 'command': 'rebalance', 'mints': list(mints),
                                     'before': before, 'before_slot': before_slot or self.chain.slot,
                                     'signatures': list(signatures), 'sent_at': sent_at or time.time()})

    def land(self, sig):
        self.chain.slot += 1
        self.chain.sig_slot[sig] = self.chain.slot

    def test_nothing_pending_reads_nothing(self):
        with self.as_profile('e2e-mu'):
            self.assertIs(rebalancer.settle_pending(60), True)
        self.assertEqual(self.chain.rpc_calls, [])

    def test_a_pending_write_is_booked_from_a_read_at_its_slot(self):
        self.chain.wallet[USDC] = 10.0
        self.pend({USDC: 10.0}, ['S'])
        self.chain.wallet[USDC] = 12.5
        self.land('S')
        self.chain.lags = [2, 1]                                  # a node two slots behind, then one
        with self.as_profile('e2e-mu'):
            self.assertIs(rebalancer.settle_pending(60), True)
        self.assertEqual(self.claim('e2e-mu'), 2.5)
        self.assertEqual(wallets.settle_state(WALLET), (self.chain.slot, None))
        self.assertEqual(self.sleeps.count(rebalancer.CLAIM_POLL_S), 2)
        self.pend({USDC: 12.5}, ['T'])
        self.chain.wallet[USDC] = 11.5
        self.land('T')
        with self.as_profile('e2e-mu'):
            rebalancer.settle_pending(60)
        self.assertEqual(self.claim('e2e-mu'), 1.5)

    def test_a_node_that_never_reaches_the_slot_books_nothing(self):
        self.pend({USDC: 10.0}, ['S'])
        self.chain.wallet[USDC] = 99.0
        self.land('S')
        self.chain.lags = [1] * 1000
        with self.as_profile('e2e-mu'):
            self.assertIs(rebalancer.settle_pending(60), False)
        self.assertEqual(wallets.claims(WALLET), {})
        self.assertIsNotNone(wallets.settle_state(WALLET)[1])                    # still pending

    def test_an_unconfirmed_signature_waits_then_expires_to_the_slot_before(self):
        self.chain.wallet[USDC] = 10.0
        self.pend({USDC: 10.0}, ['NEVER'])
        with self.as_profile('e2e-mu'):
            self.assertIs(rebalancer.settle_pending(4), False)                   # recent: it may still land
        self.pend({USDC: 10.0}, ['NEVER'], sent_at=time.time() - rebalancer.PENDING_EXPIRE_S - 1)
        with self.as_profile('e2e-mu'):
            self.assertIs(rebalancer.settle_pending(4), True)                    # expired: it never landed
        self.assertEqual(wallets.claims(WALLET), {})
        self.assertIsNone(wallets.settle_state(WALLET)[1])

    def test_a_write_that_books_no_claim_still_moves_the_slot(self):
        self.pend({}, ['S'], mints=(), profile='e2e-sol')
        self.land('S')
        with self.as_profile('e2e-sol'):
            self.assertIs(rebalancer.settle_pending(60), True)
        self.assertEqual(wallets.settle_state(WALLET), (self.chain.slot, None))
        self.assertEqual([m for m, _ in self.chain.rpc_calls], ['getSignatureStatuses'])

    def test_an_overdraw_is_said_with_its_figures(self):
        self.pend({USDC: 30.0}, ['S'])
        self.chain.wallet[USDC] = 10.0
        self.land('S')
        with self.as_profile('e2e-mu'):
            rebalancer.settle_pending(60)
        row = [r for r in self.feed('e2e-mu') if r['event'] == 'claim_overdraw'][0]
        self.assertEqual((row['claim'], row['delta'], row['overdraw']), (0.0, -20.0, 20.0))

    def test_the_command_is_named_in_the_refusal(self):
        self.chain.rpc_down = True
        with self.as_profile('e2e-mu'):
            out, err = rebalancer.chain('open', MU_POOL, '9', '11', '1', '1', '--execute')
        self.assertEqual(out, None)
        self.assertEqual(err, 'refused: claims unmeasurable (balance before the write unreadable); open not sent')
        row = [r for r in self.feed('e2e-mu') if r['event'] == 'claim_unmeasured'][0]
        self.assertEqual(row['command'], 'open')

    # sleeve_of: the wallet read a profile sees
    def test_a_pre_020_priced_read_is_returned_as_it_is(self):
        bal = self.chain.balance(SOL_POOL)
        asked = []
        with self.as_profile('e2e-sol'), mock.patch.object(config, 'WALLET_ID', None), \
                mock.patch.object(rebalancer, 'pool_tokens', lambda: asked.append(1) or ((SOL, 'SOL'), (USDC, 'USDC'))):
            self.assertIs(rebalancer.sleeve_of(bal), bal)
        self.assertEqual(asked, [])                                          # not even the pool record

    def test_a_pre_020_read_never_becomes_a_sleeve(self):
        written = []
        bal = dict(self.chain.balance(SOL_POOL), quoteUsd=None)
        with self.as_profile('e2e-sol'), mock.patch.object(config, 'WALLET_ID', None), \
                mock.patch.object(wallets, 'register_mints', lambda *a: written.append(a)):
            v = rebalancer.sleeve_of(bal)
        self.assertEqual((v['quoteUsd'], written), (1.0, []))
        self.assertNotIn('rawBalanceA', v)

    def test_a_priced_quote_is_never_replaced_by_a_dollar(self):
        bal = dict(self.chain.balance(MU_POOL), quoteUsd=1.0001)
        with self.as_profile('e2e-mu'):
            self.assertEqual(rebalancer.sleeve_of(bal)['quoteUsd'], 1.0001)

    def test_a_stable_quote_without_a_price_is_a_dollar_and_any_other_is_unknown(self):
        bal = dict(self.chain.balance(SOL_POOL), quoteUsd=None)
        with self.as_profile('e2e-sol'), mock.patch.object(config, 'WALLET_ID', None):
            v = rebalancer.sleeve_of(bal)
            self.assertEqual((v['quoteUsd'], v['quoteUsdSource']), (1.0, 'stable mint'))
            with mock.patch.object(rebalancer, 'pool_record', lambda: dict(pool_record_for(SOL_POOL),
                                                                           token_b={'address': MU, 'symbol': 'MU'})):
                self.assertIsNone(rebalancer.sleeve_of(bal)['quoteUsd'])
            with mock.patch.object(rebalancer, 'pool_record', side_effect=RuntimeError('no record')):
                self.assertIsNone(rebalancer.sleeve_of(bal)['quoteUsd'])
        self.assertIsNone(bal['quoteUsd'])                                   # the read itself is not changed

    def test_a_shared_wallet_without_pool_tokens_is_unreadable(self):
        bal = self.chain.balance(MU_POOL)
        with self.as_profile('e2e-mu'), mock.patch.object(rebalancer, 'pool_record', side_effect=RuntimeError('x')):
            self.assertEqual(rebalancer.sleeve_of(bal), {})
        self.assertEqual(self.events('e2e-mu'), ['sleeve_unreadable'])
        with db.cursor(commit=True) as cur:
            cur.execute("update config set wallet_id = null where name = 'e2e-sol'")    # alone on the wallet
        with self.as_profile('e2e-mu'), mock.patch.object(rebalancer, 'pool_record', side_effect=RuntimeError('x')):
            self.assertIs(rebalancer.sleeve_of(bal), bal)                    # alone: the read as it is

    def test_a_database_failure_is_an_unreadable_wallet(self):
        bal = self.chain.balance(MU_POOL)
        with self.as_profile('e2e-mu'), mock.patch.object(wallets, 'wallet_profiles', side_effect=RuntimeError('db')):
            self.assertEqual(rebalancer.sleeve_of(bal), {})
        self.assertEqual(self.events('e2e-mu'), ['sleeve_unreadable'])

    def test_the_pool_mints_are_registered_once(self):
        written = []
        with self.as_profile('e2e-mu'), mock.patch.object(wallets, 'register_mints', lambda p, m: written.append((p, m))):
            rebalancer.sleeve_of(self.chain.balance(MU_POOL))
            rebalancer.sleeve_of(self.chain.balance(MU_POOL))
        self.assertEqual(written, [('e2e-mu', [MU, USDC])])

    # quote_known: said once an episode
    def test_an_unknown_quote_is_said_once_until_it_is_known_again(self):
        with self.as_profile('e2e-mu'):
            state = {}
            self.assertIs(rebalancer.quote_known(state, {'quoteUsd': None}, 'the open'), False)
            self.assertIs(rebalancer.quote_known(state, {}, 'the open'), False)
            self.assertIs(rebalancer.quote_known(state, {'quoteUsd': 1.0}, 'the open'), True)
            self.assertNotIn('quote_unknown_told', rebalancer.load())          # saved cleared
            self.assertIs(rebalancer.quote_known(state, {'quoteUsd': None}, 'the open'), False)
        self.assertEqual(self.events('e2e-mu'), ['quote_unknown', 'quote_unknown'])

    # the wallet's mints and book
    def test_wallet_mints_include_deposit_mints_and_unknown_profiles(self):
        with db.cursor(commit=True) as cur:
            cur.execute("update config set mints = null where name = 'e2e-mu'")
        wallets.register_mints('e2e-sol', [SOL, USDC])
        with self.as_profile('e2e-sol'):
            self.assertEqual(rebalancer.wallet_mints(), {SOL, USDC, MU})
            with mock.patch.object(config, 'WALLET_ID', None):
                self.assertEqual(rebalancer.wallet_mints(), set())

    def test_the_wallet_book_counts_disabled_profiles_and_their_mints(self):
        wallets.register_mints('e2e-sol', [SOL, USDC])
        with db.cursor(commit=True) as cur:
            cur.execute("update config set enabled = false, mints = null where name = 'e2e-mu'")
        with self.as_profile('e2e-sol'):
            book = rebalancer.wallet_book()
            with mock.patch.object(config, 'WALLET_ID', None):
                self.assertIsNone(rebalancer.wallet_book())
        self.assertEqual(sorted(p['name'] for p in book['profiles']), ['e2e-mu', 'e2e-sol'])
        self.assertEqual(book['mints'], {SOL, USDC, MU})                      # disabled: its MU is still its own
        self.assertEqual(book['mints_of'], {'e2e-sol': [SOL, USDC]})

    def test_no_portfolio_without_a_wallet_or_from_another_owner(self):
        with mock.patch.object(rebalancer.stats, 'portfolio', side_effect=AssertionError):
            with self.as_profile('e2e-base'):
                with mock.patch.object(config, 'WALLET_ID', None):
                    self.assertIsNone(rebalancer.portfolio_report({}))
                with mock.patch.object(config, 'RESIDUAL_OWNER', False):
                    self.assertIsNone(rebalancer.portfolio_report({}))
                with mock.patch.object(rebalancer.db, 'wallets', lambda: []):
                    self.assertIsNone(rebalancer.portfolio_report({}))
        self.assertEqual(self.events('e2e-base'), [])                        # no portfolio_failed either

    # the baseline: once, in the units of the book
    def baseline_row(self, profile):
        with db.cursor() as cur:
            cur.execute("select sol, usdc, usd, price, amounts from capital_flows "
                        "where kind = 'baseline' and profile = %s", (profile,))
            return cur.fetchall()

    def test_record_baseline_values_the_sleeve_and_writes_once(self):
        bal = {'balanceA': 2.0, 'balanceB': 30.0, 'price': 9.0, 'uiPrice': 10.0, 'quoteUsd': 2.0}
        with self.as_profile('e2e-mu'):
            self.assertIs(rebalancer.record_baseline(bal), True)
            self.assertIs(rebalancer.record_baseline(dict(bal, balanceA=5.0)), False)      # one per profile
        [r] = self.baseline_row('e2e-mu')
        self.assertEqual((float(r['sol']), float(r['usdc']), float(r['usd']), r['price']),
                         (0.0, 30.0, (2.0 * 10.0 + 30.0) * 2.0, 10.0))                  # UI price, quote in dollars
        self.assertEqual(r['amounts'], {MU: 2.0, USDC: 30.0})

    def test_record_baseline_with_the_stablecoin_as_token_a_and_missing_balances(self):
        rec = dict(pool_record_for(MU_POOL), token_a={'address': USDC, 'symbol': 'USDC', 'decimals': 6},
                   token_b={'address': MU, 'symbol': 'MU', 'decimals': 9})
        bal = {'balanceA': 40.0, 'balanceB': None, 'price': 0.1, 'quoteUsd': 10.0}
        with self.as_profile('e2e-mu'), mock.patch.object(rebalancer, 'pool_record', lambda: rec):
            self.assertIs(rebalancer.record_baseline(bal), True)
        [r] = self.baseline_row('e2e-mu')
        self.assertEqual((float(r['usdc']), float(r['usd'])), (40.0, 40.0 * 0.1 * 10.0))     # USDC is side A here
        self.assertEqual(r['amounts'], {USDC: 40.0, MU: 0.0})

    def test_record_baseline_puts_side_b_in_usdc_unless_only_side_a_is_a_stablecoin(self):
        usdt = 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB'
        bal = {'balanceA': None, 'balanceB': 7.0, 'price': 1.0, 'quoteUsd': 1.0}
        for a_mint, b_mint, want in ((SOL, MU, 7.0), (USDC, usdt, 7.0)):            # neither, both: side B
            rec = dict(pool_record_for(MU_POOL), token_a={'address': a_mint, 'symbol': 'A', 'decimals': 6},
                       token_b={'address': b_mint, 'symbol': 'B', 'decimals': 6})
            with db.cursor(commit=True) as cur:
                cur.execute("delete from capital_flows where profile = 'e2e-mu'")
            with self.as_profile('e2e-mu'), mock.patch.object(rebalancer, 'pool_record', lambda: rec):
                self.assertIs(rebalancer.record_baseline(bal), True)
            [r] = self.baseline_row('e2e-mu')
            self.assertEqual((float(r['usdc']), float(r['usd'])), (want, 7.0), (a_mint, b_mint))
            self.assertEqual(r['amounts'], {a_mint: 0.0, b_mint: 7.0})                  # a missing side A is 0

    def test_record_baseline_never_blocks_the_open(self):
        with self.as_profile('e2e-mu'), mock.patch.object(rebalancer, 'pool_record', side_effect=RuntimeError('x')):
            self.assertIs(rebalancer.record_baseline({'balanceA': 1.0, 'balanceB': 1.0, 'price': 1.0, 'quoteUsd': 1.0}),
                          False)
        self.assertEqual(self.events('e2e-mu'), ['baseline_failed'])
        self.assertEqual(self.baseline_row('e2e-mu'), [])

    # dormant: the edges
    def test_dormant_edges(self):
        with self.as_profile('e2e-mu'):
            bal = self.chain.balance(MU_POOL)
            self.assertIs(rebalancer.dormant({'pending_reopen': {'x': 1}}, bal), False)
            self.assertIs(rebalancer.dormant({'dormant': True, 'pending_reopen': {'x': 1}}, bal), False)
            self.assertIs(rebalancer.dormant({'dormant': True}, {}), True)          # unreadable: as it was
            self.assertIs(rebalancer.dormant({}, {}), False)
            self.assertIs(rebalancer.dormant({'dormant': True}, dict(bal, quoteUsd=None)), True)
            self.assertIs(rebalancer.dormant({}, dict(bal, quoteUsd=None)), False)
            at = dict(bal, balanceA=0.5, balanceB=0.0)                              # exactly $5: deployable
            self.assertIs(rebalancer.dormant({}, at), False)
            state = {'dormant': True}
            self.assertIs(rebalancer.dormant(state, dict(bal, balanceA=1.0)), False)
            self.assertIs(state['dormant'], False)
            self.assertIs(rebalancer.load()['dormant'], False)


if __name__ == '__main__':
    unittest.main()
