"""deploy.sh rehearsed on scratch copies: a pre-020 database, a copy of this
tree as the live tree after the merge, with the old bot's runtime files in
it, and fakes for git and the service manager. Never the live paths, never
the live database, never git itself.

The dry run changes nothing; --apply does every step of the contract in one
restart; a second --apply changes nothing; rollback (after the merge is
reverted) puts the single service, its files and its keys back."""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import _fixtures  # noqa: F401  (first: the _test guard)

import psycopg2
import psycopg2.extras

ROOT = _fixtures.ROOT
DBNAME = 'rebalancer_deploy_test'
DSN = f'dbname={DBNAME}'
SOL = 'So11111111111111111111111111111111111111112'
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
BUSDC = '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913'
WETH = '0x4200000000000000000000000000000000000006'
MU, DJT, MSFTX = 'MUxEsUKSMACyw5fZf68wxf5FLnZVhtU9CwH8uNNGay1', 'DJTu7vi8norVzdVAffgvb39VP7wjKeTsgaMBJrzfxvoF', \
    'XspzcW1PRtgf6Wj92HCiZdjzKCyFekVD8P5Ueh3dRMX'
BASE_ADDRESS = '0x8815F16a662341b345894477eA818a65617f6021'
RECORDS = {
    '13MEx6gjRadJNUdmToaGSzgeWHLH7FzScUQS9Mc5nYF5': {'token_a': {'address': MU, 'symbol': 'MU'},
                                                     'token_b': {'address': USDC, 'symbol': 'USDC'}},
    '7gkB2D1SqhUYgKrSpDU5cma4tK9efijHouYituABdJcG': {'token_a': {'address': DJT, 'symbol': 'DJT'},
                                                     'token_b': {'address': USDC, 'symbol': 'USDC'}},
    'D6bRhQUcR9B7bPbbqgxpE17MjyUjBtr8hHQCcJoHrrv1': {'token_a': {'address': USDC, 'symbol': 'USDC'},
                                                     'token_b': {'address': MSFTX, 'symbol': 'MSFTx'}},
    '0x3fe04a59ebd38cf06080a6f60a98d124eb59392a': {'token_a': {'address': WETH, 'symbol': 'WETH'},
                                                   'token_b': {'address': BUSDC.upper().replace('0X', '0x'),
                                                               'symbol': 'USDC'}},
}
FAKE_SYSTEMCTL = """#!/usr/bin/env bash
# A service manager in files: enabled.txt and active.txt, one unit a line.
# fail.txt: commands that fail (exact text); on_stop.sh runs at the stop of
# lp-bot.service (to break the next step, after the stop).
d="$(dirname "$0")"
echo "$*" >> "$d/systemctl.log"
touch "$d/enabled.txt" "$d/active.txt"
if [ -f "$d/fail.txt" ] && grep -qxF -- "$*" "$d/fail.txt"; then exit 1; fi
add() { grep -qx "$2" "$d/$1" || echo "$2" >> "$d/$1"; }
del() { grep -vx "$2" "$d/$1" > "$d/x.tmp" || true; mv "$d/x.tmp" "$d/$1"; }
u="${@: -1}"
case "$1" in
  is-enabled) grep -qx "$u" "$d/enabled.txt"; exit $? ;;
  is-active) grep -qx "$u" "$d/active.txt"; exit $? ;;
  enable) add enabled.txt "$u"; if [ "$2" = --now ]; then add active.txt "$u"; fi ;;
  disable) del enabled.txt "$u"; if [ "$2" = --now ]; then del active.txt "$u"; fi ;;
  stop) del active.txt "$u"; if [ "$u" = lp-bot.service ] && [ -f "$d/on_stop.sh" ]; then bash "$d/on_stop.sh"; fi ;;
  start|restart) add active.txt "$u" ;;
  list-units) sed -n 's/^\\(lp-bot@.*\\)$/\\1.service loaded active running x/p' "$d/enabled.txt" ;;
esac
exit 0
"""
FAKE_POSITIONS = "console.log(process.env.FAKE_POSITIONS ?? '[]');\n"
# git status --porcelain: the lines in dirty.txt (none: a clean tree)
FAKE_GIT = """#!/usr/bin/env bash
d="$(dirname "$0")"
echo "$*" >> "$d/git.log"
[ "$3" = status ] || exit 1
cat "$d/dirty.txt" 2>/dev/null
exit 0
"""


def q(sql, args=()):
    con = psycopg2.connect(DSN)
    try:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('set search_path to rebalancer, public')
            cur.execute(sql, args)
            rows = cur.fetchall() if cur.description else None
        con.commit()
        return rows
    finally:
        con.close()


def snapshot_db():
    """Everything the deploy may touch, as data."""
    out = {}
    for t, order in (('config', 'name'), ('audit_state', 'key'), ('health', 'key'), ('events', 'id')):
        out[t] = [dict(r) for r in q(f'select * from {t} order by {order}')]
    for t in ('wallets', 'wallet_claims'):
        if q("select 1 from information_schema.tables where table_schema = 'rebalancer' and table_name = %s", (t,)):
            out[t] = [dict(r) for r in q(f'select * from {t}')]
    for r in out['config']:
        r.pop('updated_at', None)
    return json.loads(json.dumps(out, default=str))


def tree_listing(p):
    return sorted(str(x.relative_to(p)) for x in p.rglob('*') if '__pycache__' not in x.parts) if p.exists() else None


class Fixture(unittest.TestCase):
    """A pre-020 database, the merged tree with the old bot's files, the fakes."""

    @classmethod
    def setUpClass(cls):
        subprocess.run(['dropdb', '--if-exists', DBNAME], check=True, capture_output=True)
        subprocess.run(['createdb', DBNAME], check=True, capture_output=True)
        for f in sorted((ROOT / 'sql').glob('0[01]*.sql')):            # 001..019: the live schema today
            subprocess.run(['psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1', '-d', DBNAME, '-f', str(f)],
                           check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(['dropdb', '--if-exists', DBNAME], capture_output=True)

    def setUp(self):
        q('truncate config, audit_state, health, events, capital_flows, audits cascade')
        if q("select 1 from information_schema.tables where table_schema = 'rebalancer' and table_name = 'wallets'"):
            q('truncate wallets cascade')                                # an earlier test's deploy
        q("""insert into config (name, active, pool, pair_label, token_a, token_b, capital_usd, max_usd, dex,
                                 bands, regime_enabled, calm_enabled, calm_max_moves_per_day, payout_enabled,
                                 profit_wallet, payout_mint, execute_dexes)
             values ('sol-usdc', true, '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj', 'SOL/USDC', 'SOL', 'USDC',
                     190, 260, 'raydium-clmm', '{1.05,1.10}', true, false, 24, true,
                     '8funmDkPNBtjqfNkBEoBF16eBfyQ4vMAFrs4Nyys5D1h', %s, '{orca,raydium-clmm}')""", (USDC,))
        q("insert into audit_state (key, value, ts) values ('flows_cursor', 'SIG', now()), ('status:gas', 'ok', now())")
        q("insert into health (key, fails, trips, updated) values ('swap', 1, 0, now()), ('venue:orca', 0, 0, now())")
        q("insert into events (ts, kind, detail) values (now(), 'OPEN', 'old row')")
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='lp_bot_deploy_'))
        self.live = self.tmp / 'lp_bot'
        # the live tree after the merge: this tree's code, the old bot's runtime files
        shutil.copytree(ROOT, self.live, ignore=shutil.ignore_patterns(
            'node_modules', '.git', '__pycache__', 'research', '.archive', 'tests', 'run', 'HALT',
            'events.jsonl', 'runtime.json', 'telegram_bridge_state.json'))
        (self.live / 'runtime.json').write_text('{"failures": 0, "last_rebalance": 123}')
        (self.live / 'events.jsonl').write_text('{"event": "OPEN"}\n')
        (self.live / 'telegram_bridge_state.json').write_text('{"pos": 17}')
        (self.live / 'REBALANCE').write_text('')
        bin_ = self.bin = self.tmp / 'bin'; bin_.mkdir()
        for name, body in (('systemctl', FAKE_SYSTEMCTL), ('git', FAKE_GIT)):
            (bin_ / name).write_text(body); (bin_ / name).chmod(0o755)
        (bin_ / 'enabled.txt').write_text('lp-bot.service\nlp-telegram\n')
        (bin_ / 'active.txt').write_text('lp-bot.service\nlp-telegram\n')
        self.units = self.tmp / 'units'; self.units.mkdir()
        keys = self.tmp / 'keys'; keys.mkdir()
        (keys / 'sol.secret').write_text('not a key'); (keys / 'evm.secret').write_text('not a key')
        (keys / 'evm.secret').chmod(0o600)
        (self.tmp / 'records.json').write_text(json.dumps(RECORDS))
        self.env = dict(os.environ, LPBOT_LIVE_DIR=str(self.live), LPBOT_DEPLOY_DSN=DSN,
                        LPBOT_UNIT_DIR=str(self.units), LPBOT_SYSTEMCTL=str(bin_ / 'systemctl'),
                        LPBOT_GIT=str(bin_ / 'git'), LPBOT_INSTALL='install',
                        LPBOT_SOL_KEY_PATH=str(keys / 'sol.secret'), LPBOT_EVM_KEY_PATH=str(keys / 'evm.secret'),
                        LPBOT_BASE_ADDRESS=BASE_ADDRESS, LPBOT_POOL_RECORDS=str(self.tmp / 'records.json'),
                        LPBOT_SETTLE_S='0')
        self.log = bin_ / 'systemctl.log'

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def deploy(self, *args, ok=True):
        r = subprocess.run(['bash', str(ROOT / 'deploy.sh'), *args], env=self.env, capture_output=True, text=True,
                           timeout=300)
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def systemctl(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def running(self):
        return (self.bin / 'active.txt').read_text().split()

    def revert(self):
        """The operator's `git revert` of the merge, as far as the script sees
        it: the multi-pool code is gone, the venue signers answer `positions`
        from FAKE_POSITIONS."""
        (self.live / 'wallets.py').unlink()
        for f in ('signer2.mjs', 'signer_dlmm.mjs', 'signer_raydium.mjs'):
            (self.live / f).write_text(FAKE_POSITIONS)

    def changes(self):
        """What the service manager was told to change (queries left out)."""
        return [x for x in self.systemctl() if not x.startswith(('is-', 'list-'))]


class Rehearsal(Fixture):
    def test_the_dry_run_changes_nothing(self):
        before = (snapshot_db(), tree_listing(self.live))
        r = self.deploy()
        self.assertIn('[dry]', r.stdout)
        self.assertIn('profile mu-usdc: meteora-dlmm', r.stdout)
        self.assertEqual((snapshot_db(), tree_listing(self.live)), before)
        self.assertEqual(self.changes(), [])
        self.assertEqual(list(self.units.iterdir()), [])

    def test_apply_then_apply_again_then_rollback(self):
        self.deploy('--apply')
        # the database
        w = {r['id']: r for r in q('select * from wallets')}
        self.assertEqual((w['sol-lp']['address'], w['base-lp']['address']),
                         ('83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f', BASE_ADDRESS))
        cfg = {r['name']: r for r in q('select * from config')}
        sol = cfg['sol-usdc']
        self.assertEqual((sol['wallet_id'], sol['enabled'], sol['residual_owner'], sol['deposit_mint'], sol['active']),
                         ('sol-lp', True, True, SOL, True))
        mu = cfg['mu-usdc']
        self.assertEqual((mu['dex'], mu['pool'], mu['pair_label'], mu['deposit_mint'], mu['wallet_id']),
                         ('meteora-dlmm', '13MEx6gjRadJNUdmToaGSzgeWHLH7FzScUQS9Mc5nYF5', 'MU/USDC', MU, 'sol-lp'))
        self.assertEqual((mu['pool_pinned'], mu['regime_enabled'], mu['rebalance_swap'], mu['payout_enabled'],
                          mu['enabled'], mu['active'], mu['residual_owner']), (True, True, True, True, True, False, False))
        self.assertEqual((mu['profit_wallet'], mu['payout_mint'], mu['execute_dexes'], mu['mints']),
                         (sol['profit_wallet'], USDC, ['meteora-dlmm'], [MU, USDC]))
        for col in ('capital_usd', 'max_usd', 'bands', 'calm_max_moves_per_day', 'regime_widths', 'slippage_bps'):
            self.assertEqual(mu[col], sol[col], col)                     # every tuning column copied
        self.assertEqual(cfg['msftx-usdc']['deposit_mint'], MSFTX)        # the non-USDC side, whichever it is
        self.assertEqual(cfg['djt-usdc']['signer_env'], {'LPBOT_ORCA_ADAPTIVE': '1'})   # an adaptive-fee Orca pool
        self.assertEqual([n for n, r in cfg.items() if r['signer_env']], ['djt-usdc'])
        for n in ('mu-usdc', 'djt-usdc', 'msftx-usdc', 'base-weth-usdc'):
            self.assertEqual(cfg[n]['execute_dexes'], [cfg[n]['dex']], n)  # its own venue only
            self.assertTrue(cfg[n]['pool_pinned'], n)                       # no failover, no migration
        base = cfg['base-weth-usdc']
        self.assertEqual((base['wallet_id'], base['deposit_mint'], base['payout_mint'], base['profit_wallet'],
                          base['residual_owner'], base['mints']),
                         ('base-lp', WETH, BUSDC, '0x2b35948898e1b4897E7FC5a70e39b213dcfd0142', True, [WETH, BUSDC]))
        self.assertLess(float(base['gas_reserve_sol']), 0.01)
        self.assertEqual(sorted(r['key'] for r in q('select key from audit_state')),
                         ['sol-lp|flows_cursor', 'sol-lp|status:gas'])
        self.assertEqual(sorted(r['key'] for r in q('select key from health')), ['sol-usdc|swap', 'sol-usdc|venue:orca'])
        self.assertEqual([r['profile'] for r in q('select profile from events')], ['sol-usdc'])
        # the runtime files moved in place; no tree swapped
        run = self.live / 'run' / 'sol-usdc'
        self.assertEqual(json.loads((run / 'runtime.json').read_text())['last_rebalance'], 123)
        self.assertEqual((run / 'events.jsonl').read_text(), '{"event": "OPEN"}\n')
        self.assertTrue((run / 'REBALANCE').exists())
        self.assertFalse((self.live / 'events.jsonl').exists())
        self.assertFalse((self.live / 'runtime.json').exists())
        self.assertEqual((self.live / 'telegram_bridge_state.json').read_text(), '{"pos": 17}')
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ['bin', 'keys', 'lp_bot', 'records.json', 'units'])
        self.assertTrue((self.units / 'lp-bot@.service').exists())
        # one restart: stop, then (files moved) start, in that order
        self.assertEqual(self.changes(), ['daemon-reload', 'stop lp-bot.service', 'disable lp-bot.service',
                                          'enable --now lp-bot@sol-usdc', 'enable --now lp-bot@base-weth-usdc',
                                          'enable --now lp-bot@djt-usdc', 'enable --now lp-bot@msftx-usdc',
                                          'enable --now lp-bot@mu-usdc', 'restart lp-telegram'])
        self.assertTrue(all(' status ' in x for x in (self.bin / 'git.log').read_text().splitlines()))  # reads only
        # a second run changes nothing and restarts nothing
        state = (snapshot_db(), tree_listing(self.live))
        self.log.unlink()
        r = self.deploy('--apply')
        self.assertIn('no restart', r.stdout)
        self.assertEqual((snapshot_db(), tree_listing(self.live)), state)
        self.assertNotIn('stop lp-bot.service', self.changes())
        # rollback: refused while the merged code is live, done after the revert
        r = self.deploy('rollback', '--apply', ok=False)
        self.assertIn('git revert the merge first', r.stderr)
        self.revert()                                                    # the operator reverted the merge
        self.log.unlink()
        (run / 'runtime.json').write_text('{"failures": 1}')
        self.deploy('rollback', '--apply')
        self.assertEqual(json.loads((self.live / 'runtime.json').read_text()), {'failures': 1})
        self.assertEqual((self.live / 'events.jsonl').read_text(), '{"event": "OPEN"}\n')
        self.assertEqual(sorted(r['key'] for r in q('select key from audit_state')), ['flows_cursor', 'status:gas'])
        self.assertEqual(sorted(r['key'] for r in q('select key from health')), ['swap', 'venue:orca'])
        self.assertEqual({r['name']: r['enabled'] for r in q('select name, enabled from config')},
                         {'sol-usdc': True, 'mu-usdc': False, 'djt-usdc': False, 'msftx-usdc': False,
                          'base-weth-usdc': False})
        log = self.changes()
        self.assertIn('disable --now lp-bot@sol-usdc.service', log)
        self.assertEqual(log[-2:], ['enable --now lp-bot.service', 'restart lp-telegram'])

    def test_a_dirty_or_unmerged_live_tree_is_not_deployed(self):
        (self.bin / 'dirty.txt').write_text(' M rebalancer.py\n')
        r = self.deploy('--apply', ok=False)
        self.assertIn('uncommitted changes', r.stderr)
        (self.bin / 'dirty.txt').unlink()
        (self.live / 'wallets.py').unlink()                              # main without the branch
        r = self.deploy('--apply', ok=False)
        self.assertIn('merge the multi-pool branch', r.stderr)
        self.assertEqual(self.changes(), [])
        self.assertTrue((self.live / 'runtime.json').exists())

    def test_a_halted_bot_is_not_deployed(self):
        (self.live / 'HALT').write_text('operator')
        r = self.deploy('--apply', ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('HALT exists', r.stderr)
        self.assertEqual(self.systemctl(), [])
        self.assertTrue((self.live / 'runtime.json').exists())

    def test_without_an_evm_key_base_is_not_created(self):
        os.unlink(self.env['LPBOT_EVM_KEY_PATH'])
        self.env.pop('LPBOT_BASE_ADDRESS')
        self.deploy('--apply')
        self.assertEqual([r['id'] for r in q('select id from wallets')], ['sol-lp'])
        self.assertNotIn('base-weth-usdc', [r['name'] for r in q('select name from config')])
        self.assertNotIn('enable --now lp-bot@base-weth-usdc', self.systemctl())

    def test_an_evm_key_readable_by_others_stops_the_deploy(self):
        os.chmod(self.env['LPBOT_EVM_KEY_PATH'], 0o644)
        r = self.deploy('--apply', ok=False)
        self.assertIn('not 600', r.stderr)
        self.assertEqual(self.systemctl(), [])


if __name__ == '__main__':
    unittest.main()


class RestartFailures(Fixture):
    """Whatever fails after the stop, a bot is running at the end."""

    def broken_after_stop(self, how):
        if how == 'backfill':                                         # the SQL breaks once the bot is stopped
            (self.bin / 'on_stop.sh').write_text(
                f"psql -q -d {DBNAME} -c \"create or replace function rebalancer.nope() returns trigger language "
                f"plpgsql as 'begin raise exception ''injected''; end'; create trigger nope before update on "
                f"rebalancer.health for each row execute function rebalancer.nope()\"\n")
        elif how == 'rename':                                          # the tree turns read-only
            (self.bin / 'on_stop.sh').write_text(f'chmod 555 {self.live}\n')
            self.addCleanup(lambda: os.chmod(self.live, 0o755))
        else:
            (self.bin / 'fail.txt').write_text(how + '\n')
        r = self.deploy('--apply', ok=False)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertTrue({'lp-bot@sol-usdc', 'lp-bot.service'} & set(self.running()), (how, self.running(), r.stderr))
        self.assertIn('lp-telegram', self.running())
        return r

    def tearDown(self):
        q('drop trigger if exists nope on health; drop function if exists nope()')
        super().tearDown()

    def test_the_backfill_fails(self):
        r = self.broken_after_stop('backfill')
        self.assertIn("stage 'stopped'", r.stderr)
        self.assertIn('lp-bot.service', self.running())

    def test_a_rename_fails(self):
        r = self.broken_after_stop('rename')
        self.assertIn('lp-bot.service', self.running())

    def test_the_profile_unit_does_not_start(self):
        r = self.broken_after_stop('enable --now lp-bot@sol-usdc')
        self.assertIn("stage 'moved'", r.stderr)
        self.assertEqual(self.running().count('lp-bot.service'), 1)
        self.assertTrue((self.live / 'run' / 'sol-usdc' / 'runtime.json').exists())     # where the code reads it

    def test_another_profile_does_not_start(self):
        r = self.broken_after_stop('enable --now lp-bot@mu-usdc')
        self.assertIn('lp-bot@sol-usdc', self.running())
        self.assertNotIn('lp-bot.service', self.running())
        self.assertIn('lp-bot@mu-usdc did not start', r.stderr)

    def test_the_bridge_does_not_restart(self):
        (self.bin / 'fail.txt').write_text('restart lp-telegram\n')
        r = self.deploy('--apply', ok=False)
        self.assertIn('lp-bot@sol-usdc', self.running())

    def test_nothing_is_stopped_when_a_check_before_the_stop_fails(self):
        (self.live / 'run' / 'sol-usdc').mkdir(parents=True)
        (self.live / 'run' / 'sol-usdc' / 'runtime.json').write_text('{}')            # a rename would overwrite it
        r = self.deploy('--apply', ok=False)
        self.assertIn('exists already', r.stderr)
        self.assertNotIn('stop lp-bot.service', self.systemctl())
        self.assertIn('lp-bot.service', self.running())


class RollbackRefusals(Fixture):
    """No rollback while another profile holds anything in sol-usdc's wallet."""

    def deployed_then_reverted(self):
        self.deploy('--apply')
        self.revert()

    def test_an_open_position_in_the_ledger(self):
        self.deployed_then_reverted()
        q("insert into positions (mint, config_name, pool, pair_label, opened_at, lower_price, upper_price, band_pct, "
          "deposit_usd, dex) values ('MUPOS', 'mu-usdc', '13MEx6gjRadJNUdmToaGSzgeWHLH7FzScUQS9Mc5nYF5', 'MU/USDC', "
          "now(), 9, 11, 5, 100, 'meteora-dlmm')")
        self.addCleanup(lambda: q("delete from positions where mint = 'MUPOS'"))
        r = self.deploy('rollback', '--apply', ok=False)
        self.assertIn('mu-usdc: open position MUPOS in the ledger', r.stdout)
        self.assertIn('lp-bot@sol-usdc', self.running())                  # nothing stopped

    def test_a_claim(self):
        self.deployed_then_reverted()
        q("insert into wallet_claims (wallet_id, profile, mint, amount) values ('sol-lp', 'djt-usdc', %s, 12.5)", (USDC,))
        r = self.deploy('rollback', '--apply', ok=False)
        self.assertIn('djt-usdc: claims 12.5', r.stdout)

    def test_a_position_on_chain(self):
        self.deployed_then_reverted()
        self.env['FAKE_POSITIONS'] = json.dumps([{'positionMint': 'X', 'pool': 'D6bRhQUcR9B7bPbbqgxpE17MjyUjBtr8hHQCcJoHrrv1'}])
        r = self.deploy('rollback', '--apply', ok=False)
        self.assertIn('msftx-usdc: open position on chain', r.stdout)

    def test_a_venue_that_cannot_be_read(self):
        self.deployed_then_reverted()
        (self.live / 'signer_dlmm.mjs').write_text("process.exit(1);\n")
        r = self.deploy('rollback', '--apply', ok=False)
        self.assertIn('mu-usdc: positions on meteora-dlmm unreadable', r.stdout)
        self.assertNotIn('enable --now lp-bot.service', self.systemctl())

    def test_an_empty_wallet_rolls_back(self):
        self.deployed_then_reverted()
        self.deploy('rollback', '--apply')
        self.assertIn('lp-bot.service', self.running())
