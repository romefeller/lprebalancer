"""One more venue on a vault that exists (owner, 2026-10-12: "move test0 to
raydium"), on the loop side: the loop holds while the vault awaits its owners'
approval, an operator move to the staged venue waits for it, then runs as
any move does; the staged venue's row (ops/add_vault.py venue); and the
migration that stages Raydium for test0 (sql/039)."""
import contextlib
import json
import pathlib
import unittest
from unittest import mock

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import config
import db
import dexes
import lp.board
import lp.books
import lp.loop
import lp.paths
import lp.signers
from venues.jupiter import prices as jupiter_api
import test_multi_loop as tml

import importlib.util
_spec = importlib.util.spec_from_file_location('add_vault', pathlib.Path(__file__).resolve().parent.parent / 'ops' / 'add_vault.py')
add_vault = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(add_vault)

ROOT = pathlib.Path(__file__).resolve().parent.parent
DLMM_POOL = '5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6'
RAY_POOL = tml.SOL_POOL                                   # 8sLb…, raydium-clmm in the fake chain
# what a signer prints when the vault's policies are not the configured ones (account.mjs AWAITS_OWNERS)
AWAITING = ("ERROR: vault awaits the owners' approval: policy 86MJFjsqMcuQZqWXxydjeRaRjwFE8oyeWoLBygS9dPL2 differs "
            "from the meteora-dlmm rules and approved pools in the config; the owners must approve the change (Approve bot rules)")
VAULT = {'wallet_id': 'e2e-vw', 'kind': 'squads', 'settings': '5wcMGHQsaMe4hkTwoXVRBmt4at4VGCUk3A4NB9Yu4hcS', 'owners': ['x'],
         'threshold': 1, 'policy_key_env': 'LPBOT_VAULT_SOL_POLICY_PATH', 'lookup': None,
         'venues': {'meteora-dlmm': {'policies': ['p1'], 'pools': [DLMM_POOL], 'payouts': []},
                    'raydium-clmm': {'policies': ['p4'], 'pools': [RAY_POOL], 'payouts': []}}}


class Pure(unittest.TestCase):
    def test_the_signers_refusal_survives_the_tidy_and_nothing_else_matches(self):
        noisy = f'bigint: Failed to load bindings, pure JS will be used (try npm run rebuild?)\n{AWAITING}\n    at verifyPolicy (x.mjs:1)'
        self.assertTrue(lp.signers.awaits_owners(lp.books.tidy(noisy)))
        self.assertTrue(lp.signers.awaits_owners(AWAITING))
        for other in (None, '', 'refused: halted (HALT)', 'policy X belongs to another vault', 'X is not a Squads account',
                      'all RPC endpoints failed: 429', "vault awaits the owner's approval"):
            self.assertFalse(lp.signers.awaits_owners(other), other)

    def test_a_staged_venue_row_is_the_vaults_own_and_new(self):
        v = {'wallet_id': 'w', 'kind': 'squads', 'settings': VAULT['settings'], 'venues': {'meteora-dlmm': {}}}
        staged = {'settings': VAULT['settings'], 'venue': 'raydium-clmm', 'sent': True, 'lookupAdds': 12,
                  'policies': ['G5ExKR7yfzj3WpDNaHpwhDscceN7sZtiTNfj4ZJvxejx', 'F2u7B8GDtQSdWjj3XZutJVSZRZ61jWBL4p5f9R79ERcD'],
                  'pools': [RAY_POOL], 'payouts': ['CvkYX7aRberphM2wx2Mf2f7BomxtikEqPvsM4USGYG9z']}
        self.assertEqual(add_vault.venue_row(v, staged), ('w', 'raydium-clmm', staged['policies'], staged['pools'], staged['payouts']))
        # a dry run is fine once the table has every account
        self.assertEqual(add_vault.venue_row(v, dict(staged, sent=False, lookupAdds=0))[1], 'raydium-clmm')
        for bad, why in ((dict(staged, settings='9Z8KJb4qTn6SDd4xvRjXkJ4o3V5y8sLtqiHqyZ2y3Bz1'), 'not '),
                         (dict(staged, venue='meteora-dlmm'), 'already'),
                         (dict(staged, venue='uniswap-v3-polygon'), 'no Squads vault rules'),
                         (dict(staged, venue='nowhere'), 'no Squads vault rules'),
                         (dict(staged, sent=False), 'lookup table lacks 12'),
                         (dict(staged, policies=[]), 'no policies'),
                         (dict(staged, pools=[]), 'no policies or no pools'),
                         (dict(staged, policies=['not-an-address']), 'not addresses'),
                         (dict(staged, payouts=['0x2b35948898e1b4897E7FC5a70e39b213dcfd0142']), 'not addresses')):
            with self.assertRaises(add_vault.Refused, msg=str(bad)) as e:
                add_vault.venue_row(v, bad)
            self.assertIn(why, str(e.exception))
        with self.assertRaises(add_vault.Refused):
            add_vault.venue_row(dict(v, kind='safe'), staged)
        with self.assertRaises(add_vault.Refused):                         # a Safe's own venue is no Squads venue either
            add_vault.venue_row(dict(v, kind='safe'), dict(staged, venue='uniswap-v3-polygon'))


class Migration(unittest.TestCase):
    """sql/039 on the test database: test0's rows as connected, then as staged."""
    SQL = (ROOT / 'sql' / '039_test0_raydium.sql').read_text()
    METEORA = ['86MJFjsqMcuQZqWXxydjeRaRjwFE8oyeWoLBygS9dPL2', 'A6ohPdAvs2YUc4BAHP5Vv6qHWHNex95A3ojCQDHJmd2R',
               'A9zDtyaW5XgvoR3H2SKBz7wHmTcShsGAq5dsMy5jezsj']

    def setUp(self):
        self.drop(); self.addCleanup(self.drop)

    def drop(self):
        with db.cursor(commit=True) as cur:
            cur.execute("delete from vault_policies where wallet_id = 'test0-vault'")
            cur.execute("delete from vaults where wallet_id = 'test0-vault'")
            cur.execute("delete from config where name = 'test0' and wallet_id = 'test0-vault'")
            cur.execute("delete from wallets where id = 'test0-vault'")

    def connected(self, settings='5wcMGHQsaMe4hkTwoXVRBmt4at4VGCUk3A4NB9Yu4hcS', policies=None):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into wallets (id, chain, address, secret_env) values ('test0-vault', 'solana', "
                        "'37mZwrAUAfTw8nXHAeuJFaMCVzrRpTpyVzjdMsurE3yv', 'LPBOT_VAULT_SOL_PAYER_PATH')")
            cur.execute("insert into vaults (wallet_id, kind, settings, owners, threshold, policy_key_env) values "
                        "('test0-vault', 'squads', %s, '{7n68tK3N7M2dim1hARhsZ376eSw4gGFys4CaZQJPomRN}', 1, 'LPBOT_VAULT_SOL_POLICY_PATH')",
                        (settings,))
            cur.execute("insert into vault_policies (wallet_id, venue, policies, pools, payouts) values ('test0-vault', 'meteora-dlmm', "
                        "%s, '{5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6}', '{CvkYX7aRberphM2wx2Mf2f7BomxtikEqPvsM4USGYG9z}')",
                        (policies or self.METEORA,))
            cur.execute("insert into config (name, pool, pair_label, capital_usd, max_usd, wallet_id, enabled, dex, dexes, "
                        "execute_dexes, pool_pinned) values ('test0', %s, 'SOL/USDC', 216, 600, 'test0-vault', true, 'meteora-dlmm', "
                        "'{meteora-dlmm}', '{meteora-dlmm}', true)", (DLMM_POOL,))

    def migrate(self):
        with db.cursor(commit=True) as cur:
            cur.execute(self.SQL)

    def rows(self):
        with db.cursor() as cur:
            cur.execute("select venue, policies, pools, payouts from vault_policies where wallet_id = 'test0-vault' order by venue")
            pol = [dict(r) for r in cur.fetchall()]
            cur.execute("select dex, pool, dexes, execute_dexes, pool_pinned from config where name = 'test0'")
            cfg = cur.fetchone()
        return pol, cfg and dict(cfg)

    def test_test0_gets_raydium_staged_on_its_vault_and_may_open_there(self):
        self.connected()
        self.migrate()
        pol, cfg = self.rows()
        self.assertEqual([p['venue'] for p in pol], ['meteora-dlmm', 'raydium-clmm'])
        ray = pol[1]
        self.assertEqual(ray['policies'], ['G5ExKR7yfzj3WpDNaHpwhDscceN7sZtiTNfj4ZJvxejx', 'F2u7B8GDtQSdWjj3XZutJVSZRZ61jWBL4p5f9R79ERcD'])
        self.assertEqual(ray['pools'], [RAY_POOL])
        self.assertEqual(ray['payouts'], pol[0]['payouts'])
        self.assertEqual(pol[0]['policies'], self.METEORA, 'the Meteora policies keep their addresses')
        self.assertEqual(cfg, {'dex': 'meteora-dlmm', 'pool': DLMM_POOL, 'dexes': ['meteora-dlmm', 'raydium-clmm'],
                               'execute_dexes': ['meteora-dlmm', 'raydium-clmm'], 'pool_pinned': True})
        self.migrate()                                                    # idempotent
        self.assertEqual(self.rows(), (pol, cfg))
        self.assertEqual(db.vault_row('test0-vault')['venues']['raydium-clmm']['pools'], [RAY_POOL])

    def test_anywhere_else_it_does_nothing(self):
        self.migrate()                                                    # no test0 at all
        self.assertEqual(self.rows(), ([], None))
        for over in ({'settings': '9Z8KJb4qTn6SDd4xvRjXkJ4o3V5y8sLtqiHqyZ2y3Bz1'}, {'policies': self.METEORA[:2]}):
            self.drop()
            self.connected(**over)                                        # another vault, or other policies: not as connected
            self.migrate()
            pol, cfg = self.rows()
            self.assertEqual([p['venue'] for p in pol], ['meteora-dlmm'], over)
            self.assertEqual(cfg['execute_dexes'], ['meteora-dlmm'], over)


POOLS = dict(tml.POOLS, **{DLMM_POOL: {'dex': 'meteora-dlmm', 'a': tml.SOL, 'b': tml.USDC, 'sa': 'SOL', 'sb': 'USDC',
                                       'price': 150.0, 'native': 'A'}})
VAULTED = {'e2e-vault': dict(pool=DLMM_POOL, wallet='e2e-vw', deposit=tml.USDC, residual=True, chain='solana')}


class Loop(tml.Fixture):
    """A vault profile over the fake chain: Meteora today, Raydium staged."""

    def setUp(self):
        for p in (mock.patch.dict(tml.POOLS, POOLS), mock.patch.dict(tml.PROFILES, VAULTED)):
            p.start(); self.addCleanup(p.stop)
        self.drop_wallet()
        with db.cursor(commit=True) as cur:
            cur.execute("insert into wallets (id, chain, address, secret_env) values ('e2e-vw', 'solana', %s, "
                        "'LPBOT_VAULT_SOL_PAYER_PATH')", ('37mZwrAUAfTw8nXHAeuJFaMCVzrRpTpyVzjdMsurE3yv',))
        super().setUp()
        self.addCleanup(self.drop_wallet)
        self.pending = set()                       # the venues whose policies are not approved on chain
        fake = self.chain

        def chain(*args, dex=None, **kw):
            if (dex or config.DEX) in self.pending:
                fake.calls.append({'profile': config.PROFILE, 'dex': dex, 'args': args, 'env': {}})
                return None, AWAITING
            out, err = fake(*args, dex=dex, **kw)
            if out and args[0] == 'balance':
                out = dict(out, custody='vault', gasSol=0.5)          # the bot's payer key pays the fees
            return out, err
        band = {'band': 1.05, 'net_day_pct': 0.1, 'rebal_per_day': 0.1}
        for p in (mock.patch.object(lp.signers, '_chain', chain),
                  mock.patch.object(lp.board, 'best_band_for', lambda pool, dex=None: dict(
                      band, price=POOLS[pool]['price'], record=tml.pool_record_for(pool), all_runs=[band])),
                  mock.patch.object(dexes, 'pool', lambda dex, pool: tml.pool_record_for(pool)),
                  mock.patch.object(jupiter_api, 'jupiter_prices', lambda ms: {m: tml.USD.get(m, 0.0) for m in ms}),
                  mock.patch.object(config, 'reload', self.reload)):
            p.start(); self.addCleanup(p.stop)

    @staticmethod
    def reload():
        with db.cursor() as cur:
            cur.execute('select dex, pool, pair_label from config where name = %s', (config.PROFILE,))
            r = cur.fetchone()
        config.DEX, config.POOL, config.PAIR_LABEL = r['dex'], r['pool'], r['pair_label']

    def drop_wallet(self):
        with db.cursor(commit=True) as cur:
            for t in ('wallet_settle', 'wallet_claims'):
                cur.execute(f"delete from {t} where wallet_id = 'e2e-vw'")
            cur.execute("delete from snapshots where mint in (select mint from positions where config_name = 'e2e-vault')")
            cur.execute("delete from band_profile where mint in (select mint from positions where config_name = 'e2e-vault')")
            for t, col in (('positions', 'config_name'), ('capital_flows', 'profile'), ('events', 'profile'), ('payouts', 'config_name'),
                           ('config', 'name')):
                cur.execute(f"delete from {t} where {col} = 'e2e-vault'")
            cur.execute("delete from wallets where id = 'e2e-vw'")

    @contextlib.contextmanager
    def as_profile(self, name):
        with super().as_profile(name) as run:
            with db.cursor() as cur:
                cur.execute("select dex, pool, pair_label from config where name = %s", (name,))
                r = cur.fetchone()
            with mock.patch.multiple(config, VAULT=VAULT, EXECUTE_DEXES=('meteora-dlmm', 'raydium-clmm'),
                                     DEX=r['dex'], POOL=r['pool'], PAIR_LABEL=r['pair_label']):
                yield run

    def writes(self):
        return [c for c in self.chain.calls if c['args'][0] in ('open', 'close', 'harvest', 'rebalance', 'increase')]

    def state(self):
        return json.loads((self.tmp / 'e2e-vault' / 'runtime.json').read_text())

    def test_the_loop_holds_until_the_owners_approve_then_the_move_waits_for_raydium_then_runs(self):
        self.chain.wallet.update({tml.USDC: 150.0, tml.SOL: 1.1})
        self.poll('e2e-vault')                                             # the position test0 holds on Meteora
        self.assertIn(DLMM_POOL, self.chain.positions)
        held = self.chain.positions[DLMM_POOL]['mint']
        # this release: the Meteora policies differ, Raydium is staged; the operator asks for the move
        self.pending = {'meteora-dlmm', 'raydium-clmm'}
        (self.tmp / 'e2e-vault' / 'MIGRATE').write_text(f'raydium-clmm {RAY_POOL}\n')
        before = len(self.writes())
        for _ in range(30):                                                # longer than max_unreadable_polls
            self.assertIsNone(self.poll('e2e-vault'), 'never halted')
        self.assertEqual(len(self.writes()), before, 'nothing moves while the vault awaits its owners')
        self.assertFalse((self.tmp / 'e2e-vault' / 'HALT').exists())
        self.assertFalse(self.halt_all.exists())
        self.assertEqual(self.events('e2e-vault').count('vault_awaits_owners'), 1, 'said once')
        self.assertEqual(self.state()['read_failures'], 0)
        self.assertTrue((self.tmp / 'e2e-vault' / 'MIGRATE').exists())
        # the owner signs the Meteora updates first: the bot acts on Meteora again, the move waits for Raydium
        self.pending = {'raydium-clmm'}
        for _ in range(3):
            self.poll('e2e-vault')
        self.assertEqual(self.events('e2e-vault').count('vault_approved'), 1)
        self.assertEqual(self.events('e2e-vault').count('migrate_waits'), 1, 'said once')
        self.assertEqual(self.chain.positions.get(DLMM_POOL, {}).get('mint'), held, 'the Meteora position stays')
        self.assertNotIn(RAY_POOL, self.chain.positions)
        self.assertTrue((self.tmp / 'e2e-vault' / 'MIGRATE').exists())
        self.assertNotIn('awaits_owners', self.state())
        # the last signature lands: the move closes on Meteora and opens on Raydium
        self.pending = set()
        self.poll('e2e-vault')
        self.assertNotIn(DLMM_POOL, self.chain.positions)
        self.assertIn(RAY_POOL, self.chain.positions)
        self.assertFalse((self.tmp / 'e2e-vault' / 'MIGRATE').exists())
        self.assertNotIn('move_waits', self.state())
        with db.cursor() as cur:
            cur.execute("select dex, pool from config where name = 'e2e-vault'")
            self.assertEqual(dict(cur.fetchone()), {'dex': 'raydium-clmm', 'pool': RAY_POOL})
            cur.execute("select pool from positions where config_name = 'e2e-vault' order by opened_at")
            self.assertEqual([r['pool'] for r in cur.fetchall()], [DLMM_POOL, RAY_POOL])
        self.assertEqual(self.events('e2e-vault')[-3:], ['CLOSE', 'REPOINTED', 'OPEN'])
        self.assertEqual(self.events('e2e-vault').count('OPEN'), 2)
        with db.cursor() as cur:
            cur.execute("select count(*) as n from events where profile = 'e2e-vault' and kind = 'MIGRATE_REQUESTED'")
            self.assertEqual(cur.fetchone()['n'], 1)

    def test_with_nothing_held_the_move_waits_for_raydium_too(self):
        self.chain.wallet.update({tml.USDC: 0.0, tml.SOL: 0.0})
        self.pending = {'raydium-clmm'}
        (self.tmp / 'e2e-vault').mkdir(exist_ok=True)
        (self.tmp / 'e2e-vault' / 'MIGRATE').write_text(f'raydium-clmm {RAY_POOL}\n')
        for _ in range(3):
            self.poll('e2e-vault')
        self.assertTrue((self.tmp / 'e2e-vault' / 'MIGRATE').exists())
        self.assertEqual(self.events('e2e-vault').count('migrate_waits'), 1)
        with db.cursor() as cur:
            cur.execute("select dex from config where name = 'e2e-vault'")
            self.assertEqual(cur.fetchone()['dex'], 'meteora-dlmm')
        self.pending = set()
        self.poll('e2e-vault')
        self.assertFalse((self.tmp / 'e2e-vault' / 'MIGRATE').exists())
        with db.cursor() as cur:
            cur.execute("select dex from config where name = 'e2e-vault'")
            self.assertEqual(cur.fetchone()['dex'], 'raydium-clmm')

    def test_a_refused_target_is_consumed_and_never_waits(self):
        self.chain.wallet.update({tml.USDC: 150.0, tml.SOL: 1.1})
        self.poll('e2e-vault')
        self.pending = {'raydium-clmm'}
        (self.tmp / 'e2e-vault' / 'MIGRATE').write_text('raydium-clmm not-a-pool\n')
        self.poll('e2e-vault')
        self.assertFalse((self.tmp / 'e2e-vault' / 'MIGRATE').exists())
        self.assertIn('migrate_refused', self.events('e2e-vault'))
        self.assertNotIn('migrate_waits', self.events('e2e-vault'))


class AwaitingOwners(unittest.TestCase):
    """The hold itself: only an unreadable status that awaits the owners holds."""

    def run_(self, state, status, err):
        said, saved = [], []
        with mock.patch.object(lp.books, 'notify', lambda ev, **kw: said.append(ev)), \
                mock.patch.object(lp.paths, 'save', lambda s: saved.append(dict(s))), \
                mock.patch.object(db, 'event', lambda *a: None):
            held = lp.loop.awaiting_owners(state, status, err)
        return held, said, saved

    def test_it_holds_once_said_and_ends_once_said(self):
        state = {}
        self.assertEqual(self.run_(state, None, AWAITING), (True, ['vault_awaits_owners'], [{'awaits_owners': AWAITING}]))
        self.assertEqual(self.run_(state, None, AWAITING)[:2], (True, []))
        self.assertEqual(self.run_(state, {'positionMint': None}, None), (False, ['vault_approved'], [{}]))
        self.assertEqual(self.run_(state, {'positionMint': None}, None), (False, [], []))

    def test_any_other_unreadable_status_is_a_read_failure_as_before(self):
        state = {'awaits_owners': AWAITING}
        self.assertEqual(self.run_(state, None, 'all RPC endpoints failed: 429'), (False, [], []))
        self.assertEqual(state, {'awaits_owners': AWAITING}, 'not approved: only unreadable')
        self.assertEqual(self.run_({}, None, None), (False, [], []))
        # a status read is an answer, whatever error text came with it
        self.assertEqual(self.run_({}, {'positionMint': 'm'}, AWAITING), (False, [], []))


class OperatorMove(unittest.TestCase):
    """The MIGRATE file: consumed when the move runs or is refused, kept while it waits."""

    def setUp(self):
        import tempfile
        self.file = pathlib.Path(tempfile.mkdtemp()) / 'MIGRATE'
        self.file.write_text(f'raydium-clmm {RAY_POOL}\n')
        self.saved, self.said = [], []
        for p in (mock.patch.object(lp.paths, 'MIGRATE', self.file),
                  mock.patch.object(lp.paths, 'save', lambda s: self.saved.append(dict(s))),
                  mock.patch.object(lp.books, 'notify', lambda ev, **kw: self.said.append(ev))):
            p.start(); self.addCleanup(p.stop)

    def move(self, state, target, why=None):
        with mock.patch.object(lp.board, 'operator_target', target), mock.patch.object(lp.board, 'move_waits', lambda t: why):
            return lp.board.operator_move(state)

    def test_a_move_that_runs_consumes_the_file_and_forgets_the_wait(self):
        t = {'dex': 'raydium-clmm', 'address': RAY_POOL}
        state = {'move_waits': 'x'}
        self.assertEqual(self.move(state, lambda spec: t), t)
        self.assertFalse(self.file.exists())
        self.assertEqual((state, self.saved), ({}, [{}]))

    def test_without_a_wait_to_forget_nothing_is_saved(self):
        self.move({}, lambda spec: {'dex': 'raydium-clmm', 'address': RAY_POOL})
        self.assertEqual(self.saved, [])

    def test_a_wait_keeps_the_file_and_is_said_once_per_reason(self):
        t = {'dex': 'raydium-clmm', 'address': RAY_POOL}
        state = {}
        for why in (AWAITING, AWAITING, 'other'):
            self.assertIsNone(self.move(state, lambda spec: t, why))
        self.assertTrue(self.file.exists())
        self.assertEqual(self.said, ['migrate_waits', 'migrate_waits'])
        self.assertEqual(state, {'move_waits': 'other'})

    def test_a_refused_target_is_consumed_even_if_the_file_is_gone_already(self):
        def refused(spec):
            self.file.unlink()                         # an operator took it back meanwhile
            return None
        self.assertIsNone(self.move({}, refused))
        self.assertFalse(self.file.exists())


class MoveWaits(unittest.TestCase):
    """Who is asked before an operator move, and who is not."""

    def ask(self, vault, dex='meteora-dlmm', target='raydium-clmm', answer=(None, None), pool='HELD', address=RAY_POOL):
        calls = []
        with mock.patch.multiple(config, VAULT=vault, DEX=dex, POOL=pool), \
                mock.patch.object(lp.signers, 'chain', lambda *a, **k: calls.append((a, k)) or answer):
            why = lp.board.move_waits({'dex': target, 'address': address})
        return why, calls

    def test_a_squads_vault_reads_the_target_venue_first(self):
        why, calls = self.ask(VAULT, answer=({'owner': 'v'}, None))
        self.assertIsNone(why)
        self.assertEqual(calls, [(('balance', RAY_POOL), {'dex': 'raydium-clmm'})])
        self.assertEqual(self.ask(VAULT, answer=(None, AWAITING))[0], AWAITING)
        self.assertEqual(self.ask(VAULT, answer=(None, 'all RPC endpoints failed'))[0], 'all RPC endpoints failed')

    def test_no_vault_a_safe_or_the_pool_it_is_on_asks_nothing(self):
        for vault, dex, pool in ((None, 'meteora-dlmm', 'HELD'), (dict(VAULT, kind='safe'), 'meteora-dlmm', 'HELD'),
                                 (VAULT, 'raydium-clmm', RAY_POOL)):
            why, calls = self.ask(vault, dex=dex, pool=pool, answer=(None, AWAITING))
            self.assertEqual((why, calls), (None, []), (vault and vault['kind'], dex))

    # 2026-10-17: tang0 to 3ucNos, another pool of the venue it is on. Its policy
    # holds the pool only once the owners approve it (cli.mjs add-pool).
    def test_another_pool_of_the_same_venue_waits_for_the_owners(self):
        why, calls = self.ask(VAULT, dex='raydium-clmm', pool=RAY_POOL, address='3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv')
        self.assertIn('does not hold pool 3ucNos', why)
        self.assertEqual(calls, [], 'a pool the policy does not list is refused before any read')
        staged = dict(VAULT, venues=dict(VAULT['venues'], **{'raydium-clmm': {
            'policies': ['p4', 'p5'], 'pools': [RAY_POOL, '3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv'], 'payouts': []}}))
        why, calls = self.ask(staged, dex='raydium-clmm', pool=RAY_POOL, address='3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv',
                              answer=(None, AWAITING))
        self.assertEqual(why, AWAITING, 'listed but not approved yet: the signer\'s read refuses, the move waits')
        self.assertEqual(calls, [(('balance', '3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv'), {'dex': 'raydium-clmm'})])
        why, _ = self.ask(staged, dex='raydium-clmm', pool=RAY_POOL, address='3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv',
                          answer=({'owner': 'v'}, None))
        self.assertIsNone(why, 'approved: the move runs')

    def test_the_pool_list_compares_addresses_not_spellings(self):
        lower = dict(VAULT, venues=dict(VAULT['venues'], **{'raydium-clmm': {'policies': ['p4'], 'pools': [RAY_POOL], 'payouts': []}}))
        why, calls = self.ask(lower, dex='meteora-dlmm', address=RAY_POOL, answer=({'owner': 'v'}, None))
        self.assertIsNone(why)
        self.assertEqual(len(calls), 1)


class PoolRow(unittest.TestCase):
    """ops/add_vault.py pool: more pools on a venue the vault has (cli.mjs add-pool)."""
    V = {'wallet_id': 'w', 'kind': 'squads', 'settings': 'S', 'venues': {
        'raydium-clmm': {'policies': ['EbfaT1qboAz4cZ6JAUYBPuP9wCHkW83LfZvSASiTNgMC', '3MPJ3vLeaHqNLrMH5DAmBzLByRrZJE5M5tN135ueWzpX'],
                         'pools': ['8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj'],
                         'payouts': ['CvkYX7aRberphM2wx2Mf2f7BomxtikEqPvsM4USGYG9z']}}}
    NEW_POLICY = '7yw9FuFRB7mKhJsdcqyrZGq2iEnQjxpL9e8jB2ZV4CGz'
    NEW_POOL = '3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv'

    def staged(self, **kw):
        have = self.V['venues']['raydium-clmm']
        return dict({'settings': 'S', 'venue': 'raydium-clmm', 'policies': have['policies'] + [self.NEW_POLICY],
                     'pools': have['pools'] + [self.NEW_POOL], 'payouts': have['payouts'], 'sent': True, 'lookupAdds': 3}, **kw)

    def test_a_staged_pool_keeps_every_approved_policy_and_pool_and_adds(self):
        policies, pools = add_vault.pool_row(self.V, self.staged())
        have = self.V['venues']['raydium-clmm']
        self.assertEqual(policies, have['policies'] + [self.NEW_POLICY])
        self.assertEqual(pools, have['pools'] + [self.NEW_POOL])
        # the same number of policies is fine: the old ones hold the new pool after the update
        self.assertEqual(add_vault.pool_row(self.V, self.staged(policies=have['policies']))[0], have['policies'])
        self.assertEqual(add_vault.pool_row(self.V, self.staged(sent=False, lookupAdds=0))[1][-1], self.NEW_POOL)

    def test_the_pool_command_stores_the_size_with_the_rows(self):
        # tang0 (2026-10-17): the staging compiled at 600 bytes; the rows and the size go in one write
        import json, tempfile, types
        v = dict(self.V, policy_bytes=None)
        writes = []

        class Cur:
            def execute(self, sql, args):
                writes.append((sql.split()[1], args))

        class Ctx:
            def __enter__(self): return Cur()
            def __exit__(self, *a): return False
        for size, want in ((600, 2), (None, 1)):
            writes.clear()
            with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
                json.dump(self.staged(**({'policyBytes': size} if size else {})), f)
            with mock.patch.object(add_vault.db, 'vault_row', lambda w: v), \
                    mock.patch.object(add_vault.db, 'cursor', lambda commit=False: Ctx()):
                add_vault.pool(types.SimpleNamespace(wallet='w', json=f.name, apply=True))
            self.assertEqual(len(writes), want, size)
            if size:
                self.assertEqual(writes[1], ('vaults', (600, 'w')))
        for bad in (100, 900, 600.5, '600'):
            with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
                json.dump(self.staged(policyBytes=bad), f)
            with mock.patch.object(add_vault.db, 'vault_row', lambda w: v), self.assertRaises(add_vault.Refused):
                add_vault.pool(types.SimpleNamespace(wallet='w', json=f.name, apply=True))

    def test_every_refusal(self):
        have = self.V['venues']['raydium-clmm']
        bad = [self.staged(settings='OTHER'), self.staged(venue='orca'), self.staged(sent=False),
               self.staged(policies=[self.NEW_POLICY] + have['policies']),                # reordered
               self.staged(policies=have['policies'][:1]),                               # dropped
               self.staged(pools=[self.NEW_POOL] + have['pools']),                       # reordered
               self.staged(pools=have['pools']),                                         # nothing added
               self.staged(payouts=['CvkYX7aRberphM2wx2Mf2f7BomxtikEqPvsM4USGYG9z', self.NEW_POOL]),
               self.staged(policies=have['policies'] + ['not an address'])]
        for b in bad:
            with self.assertRaises(add_vault.Refused, msg=str(b)[:120]):
                add_vault.pool_row(self.V, b)
        with self.assertRaises(add_vault.Refused):
            add_vault.pool_row(dict(self.V, kind='safe'), self.staged())


if __name__ == '__main__':
    unittest.main()
