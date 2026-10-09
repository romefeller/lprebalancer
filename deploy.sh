#!/usr/bin/env bash
# Deploy the multi-wallet, multi-pool bot (MULTI_DESIGN.md, "Deploy").
# Run by the manager only, AFTER the multi-pool branch is merged into the
# live repository's main (lp_bot is a git repository; this script runs no git
# command that changes it). A dry run by default: it checks, reads and prints
# every step, and changes nothing (the database is only SELECTed).
#
#   ./deploy.sh                    dry run of the deploy
#   ./deploy.sh --apply            the deploy
#   ./deploy.sh rollback           dry run of the rollback
#   ./deploy.sh rollback --apply   back to the single lp-bot.service, after `git revert` of the merge
#
# The deploy, in order (each step is skipped when it is already done, so a
# second run changes nothing):
#   1. preflight: the live tree is clean (git status) and holds the merged
#      code; it compiles; no HALT; the keys exist (never read here, except
#      the EVM key's address, through chains/evm/wallet.mjs, which prints no key)
#   2. schema: sql/020 and sql/021 (additive: the running bot keeps working)
#   3. wallets sol-lp and base-lp; sol-usdc joins sol-lp as its residual owner
#      with deposit mint SOL; the new profiles get every tuning column of
#      sol-usdc, then their pool, dex, pair, deposit mint, wallet, pinned,
#      regime, swap, payout and signer opt-in settings (an existing row is
#      left as it is)
#   4. lp-bot@.service installed (the running bot is not touched by it)
#   5. checks of everything the restart does: the backfill SQL runs in a
#      transaction that is rolled back; every rename can be done
#   6. the one restart, kept short: stop lp-bot.service; then, with nothing
#      writing them, pre-020 rows become sol-usdc / sol-lp's, old audit_state
#      keys get 'sol-lp|' and health keys 'sol-usdc|', runtime.json,
#      events.jsonl and the operator triggers are RENAMED into run/sol-usdc/
#      (the bridge keeps its cursor file and migrates it); lp-bot@sol-usdc and
#      every other enabled profile start; the Telegram bridge restarts. If any
#      step after the stop fails, a trap brings a bot back: lp-bot@sol-usdc
#      when the files are moved, else the old lp-bot.service on the same code.
#
# Run it as the NEXT step after the merge, within minutes. Until the restart
# the old process keeps running with the old Python in memory while every
# signer call spawns the merged .mjs, and a pool move (config.reload) would
# import the merged config.py onto the old db module. The script refuses to
# run unless the tree it runs is the merged code.
#
# Rollback (020 and 021 are additive: the schema stays): first `git revert`
# the merge in the live repository (the script checks the old code is back,
# and does not do it itself). It refuses while a profile other than sol-usdc
# holds an open position (ledger or chain) or a claim in sol-usdc's wallet:
# the old code would sweep its tokens and adopt its position. Then: stop
# every lp-bot@, strip the key prefixes, move the runtime files back,
# re-enable lp-bot.service, restart the bridge. The new profiles are disabled.
#
# Every path, the database, git and the service manager can be pointed
# elsewhere for a rehearsal (tests/test_deploy_script.py runs it on scratch
# copies with fakes):
#   LPBOT_LIVE_DIR LPBOT_DEPLOY_DSN LPBOT_UNIT_DIR LPBOT_SYSTEMCTL LPBOT_INSTALL
#   LPBOT_GIT LPBOT_SOL_KEY_PATH LPBOT_EVM_KEY_PATH LPBOT_BASE_ADDRESS
#   LPBOT_POOL_RECORDS LPBOT_SETTLE_S
set -euo pipefail

APPLY=0
MODE=deploy
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    --dry-run) APPLY=0 ;;
    rollback) MODE=rollback ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    *) echo "ERROR: unknown argument $a" >&2; exit 2 ;;
  esac
done

LIVE=${LPBOT_LIVE_DIR:-/home/ubuntu/agent-fin/lp_bot}
DSN=${LPBOT_DEPLOY_DSN:-dbname=rebalancer}
UNIT_DIR=${LPBOT_UNIT_DIR:-/etc/systemd/system}
SYSTEMCTL=${LPBOT_SYSTEMCTL:-sudo systemctl}
SYSTEMCTL_READ=${LPBOT_SYSTEMCTL:-systemctl}        # queries need no sudo
INSTALL=${LPBOT_INSTALL:-sudo install}
GIT=${LPBOT_GIT:-git}
SOL_KEY=${LPBOT_SOL_KEY_PATH:-/home/ubuntu/.kamino-keys/bot-wallet.secret}
EVM_KEY=${LPBOT_EVM_KEY_PATH:-/home/ubuntu/.kamino-keys/evm-wallet.secret}
SETTLE_S=${LPBOT_SETTLE_S:-20}
RUNTIME_FILES="runtime.json events.jsonl REBALANCE REOPT MIGRATE"
NEW_PROFILES="mu-usdc djt-usdc msftx-usdc base-weth-usdc"

say() { printf '%s %s\n' "$([ "$APPLY" = 1 ] && echo '[apply]' || echo '[dry]  ')" "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
run() {  # a command that changes something: printed always, run only with --apply
  say "\$ $*"
  if [ "$APPLY" = 1 ]; then "$@"; fi
}
# The multi-profile units are the ones in service: the restart already happened.
deployed() { $SYSTEMCTL_READ is-enabled --quiet lp-bot@sol-usdc 2>/dev/null && ! $SYSTEMCTL_READ is-enabled --quiet lp-bot.service 2>/dev/null; }

# --- the database part, in Python (psycopg2), dry unless DEPLOY_APPLY=1 --------------------
db() {  # db <phase>
  DEPLOY_APPLY=$APPLY DEPLOY_DSN=$DSN DEPLOY_CODE=$LIVE DEPLOY_EVM_KEY=$EVM_KEY DEPLOY_SOL_KEY=$SOL_KEY \
    DEPLOY_NEW_PROFILES=$NEW_PROFILES python3 - "$1" <<'PY'
import json, os, subprocess, sys
import psycopg2, psycopg2.extras

phase = sys.argv[1]
APPLY = os.environ['DEPLOY_APPLY'] == '1'
# backfill_check runs the backfill for real inside a transaction and rolls it
# back: a proof, before the bot stops, that the SQL runs on these rows.
EXECUTE = APPLY or phase == 'backfill_check'
CODE = os.environ['DEPLOY_CODE']
SOL = 'So11111111111111111111111111111111111111112'
USDC = {'solana': 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'base': '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913'}
PIN = {'solana': '8funmDkPNBtjqfNkBEoBF16eBfyQ4vMAFrs4Nyys5D1h', 'base': '0x2b35948898e1b4897E7FC5a70e39b213dcfd0142'}
SOL_LP = '83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f'
WETH = '0x4200000000000000000000000000000000000006'
NEW = {
    'mu-usdc': ('sol-lp', 'meteora-dlmm', '13MEx6gjRadJNUdmToaGSzgeWHLH7FzScUQS9Mc5nYF5'),
    'djt-usdc': ('sol-lp', 'orca', '7gkB2D1SqhUYgKrSpDU5cma4tK9efijHouYituABdJcG'),
    'msftx-usdc': ('sol-lp', 'raydium-clmm', 'D6bRhQUcR9B7bPbbqgxpE17MjyUjBtr8hHQCcJoHrrv1'),
    'base-weth-usdc': ('base-lp', 'aerodrome-slipstream', '0x3fe04a59ebd38cf06080a6f60a98d124eb59392a'),
}
assert sorted(NEW) == sorted(os.environ['DEPLOY_NEW_PROFILES'].split())
# Never copied from sol-usdc: identity, the pool, the wallet and the settings
# set per profile below.
SKIP = {'id', 'name', 'active', 'pool', 'pair_label', 'token_a', 'token_b', 'dex', 'dexes', 'updated_at',
        'wallet_id', 'enabled', 'deposit_mint', 'residual_owner', 'mints', 'pool_pinned', 'regime_enabled',
        'rebalance_swap', 'payout_enabled', 'profit_wallet', 'payout_mint', 'execute_dexes', 'gas_reserve_sol',
        'signer_env'}
# Opt-ins a pool's signer needs (config.SIGNER_ENV_ALLOWED): the DJT/USDC Orca
# pool is adaptive-fee, which venues/orca/signer.mjs opens only with LPBOT_ORCA_ADAPTIVE=1.
SIGNER_ENV = {'djt-usdc': {'LPBOT_ORCA_ADAPTIVE': '1'}}
# Native gas kept back, per chain: SOL as sol-usdc keeps it; on Base 0.003 ETH
# (~$8) pays hundreds of L2 transactions.
GAS = {'base': 0.003}

con = psycopg2.connect(os.environ['DEPLOY_DSN'])
cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute('set search_path to rebalancer, public')


def tag():
    return '[apply]' if APPLY else '[dry]  '


def write(sql, args=(), what=''):
    """A change: printed, and executed only with --apply (dry: nothing runs)."""
    print(f'{tag()}   sql: {what or sql.split()[0]}', flush=True)
    if EXECUTE:
        cur.execute(sql, args)
        return cur.rowcount
    return None


def one(sql, args=()):
    cur.execute(sql, args)
    return cur.fetchone()


def has_column(table, col):
    return bool(one("select 1 from information_schema.columns where table_schema = 'rebalancer' "
                    "and table_name = %s and column_name = %s", (table, col)))


def records():
    """Pool records of the new profiles: from LPBOT_POOL_RECORDS (a rehearsal)
    or from the pools themselves (dexes.pool, network reads only)."""
    if os.environ.get('LPBOT_POOL_RECORDS'):
        return json.load(open(os.environ['LPBOT_POOL_RECORDS']))
    sys.path.insert(0, CODE)
    import dexes
    out = {}
    for name, (_w, dex, pool) in NEW.items():
        try:
            out[pool] = dexes.pool(dex, pool)
        except Exception as e:
            print(f'{tag()}   {name}: pool record unreadable ({type(e).__name__}: {str(e)[:120]})')
            out[pool] = None
        if out[pool] is None and dex == 'aerodrome-slipstream':
            out[pool] = {'token_a': {'address': WETH, 'symbol': 'WETH'}, 'token_b': {'address': USDC['base'], 'symbol': 'USDC'}}
    return out


def base_address():
    a = os.environ.get('LPBOT_BASE_ADDRESS')
    if a:
        return a
    key = os.environ['DEPLOY_EVM_KEY']
    if not os.path.exists(key):
        return None
    r = subprocess.run(['node', os.path.join(CODE, 'chains/evm/wallet.mjs'), 'address', '--path', key],
                       capture_output=True, text=True, timeout=60)
    out = r.stdout.strip()
    return out if r.returncode == 0 and out.startswith('0x') and len(out) == 42 else None


def register():
    if not has_column('config', 'wallet_id') or not has_column('config', 'mints'):
        print(f'{tag()}   schema 020/021 not applied yet: the wallets and profiles below are what --apply writes')
    base = base_address()
    print(f'{tag()} wallets: sol-lp {SOL_LP}; base-lp {base or "(no EVM key: base-weth-usdc stays disabled)"}')
    write('insert into wallets (id, chain, address, secret_env, label) values (%s,%s,%s,%s,%s) on conflict (id) do nothing',
          ('sol-lp', 'solana', SOL_LP, 'WALLET_SECRET_PATH', 'SOL LP wallet'), 'wallet sol-lp')
    if base:
        write('insert into wallets (id, chain, address, secret_env, label) values (%s,%s,%s,%s,%s) on conflict (id) do nothing',
              ('base-lp', 'base', base, 'LPBOT_EVM_KEY_PATH', 'Base LP wallet'), 'wallet base-lp')
    sol = one("select * from config where name = 'sol-usdc'")
    if not sol:
        raise SystemExit('ERROR: profile sol-usdc is missing; nothing to copy from')
    if (sol.get('wallet_id'), sol.get('enabled'), sol.get('residual_owner'), sol.get('deposit_mint')) != \
            ('sol-lp', True, True, SOL):
        write("update config set wallet_id = 'sol-lp', enabled = true, residual_owner = true, deposit_mint = %s, "
              "updated_at = now() where name = 'sol-usdc'", (SOL,), 'sol-usdc joins sol-lp (residual owner, deposit SOL)')
    else:
        print(f'{tag()}   sol-usdc already on sol-lp')
    recs = records()
    cols = [c for c in sol if c not in SKIP]
    for name, (wallet, dex, pool) in NEW.items():
        if one('select 1 from config where name = %s', (name,)):
            print(f'{tag()}   {name}: exists, left as it is')
            continue
        chain = 'base' if wallet == 'base-lp' else 'solana'
        rec = recs.get(pool)
        if not rec:
            print(f'{tag()}   {name}: SKIPPED, no pool record (rerun the deploy to add it)')
            continue
        a, b = rec['token_a'], rec['token_b']
        norm = (lambda m: m.lower()) if chain == 'base' else (lambda m: m)
        mints = [norm(a['address']), norm(b['address'])]
        if USDC[chain] not in mints:
            print(f'{tag()}   {name}: SKIPPED, the pool does not quote USDC ({a["symbol"]}/{b["symbol"]})')
            continue
        deposit = mints[0] if mints[1] == USDC[chain] else mints[1]
        enabled = chain == 'solana' or bool(base)
        row = {c: sol[c] for c in cols}
        row.update(name=name, active=False, pool=pool, dex=dex, dexes=[dex], execute_dexes=[dex],
                   pair_label=f"{a['symbol']}/{b['symbol']}", token_a=a['symbol'], token_b=b['symbol'],
                   wallet_id=wallet, enabled=enabled, deposit_mint=deposit, residual_owner=(chain == 'base'),
                   mints=mints, pool_pinned=True, regime_enabled=True, rebalance_swap=True, payout_enabled=True,
                   profit_wallet=PIN[chain], payout_mint=USDC[chain],
                   gas_reserve_sol=GAS.get(chain, sol['gas_reserve_sol']),
                   signer_env=psycopg2.extras.Json(SIGNER_ENV[name]) if name in SIGNER_ENV else None)
        if wallet == 'base-lp' and not base:
            print(f'{tag()}   {name}: SKIPPED, no base-lp wallet')
            continue
        keys = list(row)
        write(f'insert into config ({", ".join(keys)}) values ({", ".join(["%s"] * len(keys))}) on conflict (name) do nothing',
              [row[k] for k in keys], f'profile {name}: {dex} {pool} {row["pair_label"]} deposit {deposit} '
                                      f'enabled={enabled} wallet={wallet} signer_env={SIGNER_ENV.get(name)}')


def backfill():
    rows = has_column('events', 'profile') or APPLY
    if not rows:
        print(f'{tag()}   schema 020 not applied yet: every pre-020 row of events, audits and capital_flows '
              f'gets sol-usdc / sol-lp after it')
    for table, sets, where in () if not rows else (
            ('events', "profile = 'sol-usdc'", 'profile is null'),
            ('audits', "profile = 'sol-usdc', wallet_id = 'sol-lp'", 'profile is null'),
            ('capital_flows', "wallet_id = 'sol-lp', profile = 'sol-usdc'", 'wallet_id is null')):
        n = one(f'select count(*) n from {table} where {where}')['n']
        print(f'{tag()}   {table}: {n} pre-020 rows -> sol-usdc / sol-lp')
        if n:
            write(f'update {table} set {sets} where {where}', (), f'backfill {table}')
    for table, prefix in (('audit_state', 'sol-lp|'), ('health', 'sol-usdc|')):
        n = one(f"select count(*) n from {table} where key not like '%%|%%'")['n']
        print(f'{tag()}   {table}: {n} keys get the prefix {prefix!r}')
        if n:
            # a prefixed key already there (an earlier, interrupted deploy) wins
            write(f"delete from {table} t where key not like '%%|%%' and exists "
                  f"(select 1 from {table} u where u.key = %s || t.key)", (prefix,), f'{table}: drop shadowed keys')
            write(f"update {table} set key = %s || key where key not like '%%|%%'", (prefix,), f'{table}: prefix keys')


def unbackfill():
    for table, prefix in (('audit_state', 'sol-lp|'), ('health', 'sol-usdc|')):
        n = one(f'select count(*) n from {table} where key like %s', (prefix + '%',))['n']
        print(f'{tag()}   {table}: {n} keys lose the prefix {prefix!r}')
        if n:
            write(f'delete from {table} t where key not like %s and exists (select 1 from {table} u where u.key = %s || t.key)',
                  (prefix + '%', prefix), f'{table}: drop plain keys the prefixed ones replace')
            write(f'update {table} set key = substr(key, %s) where key like %s', (len(prefix) + 1, prefix + '%'),
                  f'{table}: strip prefix')
    if has_column('config', 'enabled'):
        write('update config set enabled = false where name = any(%s)', (list(NEW),), 'disable the new profiles')


def enabled():
    """The profiles to run: enabled ones of a known wallet (one name a line)."""
    if not has_column('config', 'enabled'):
        print('sol-usdc')
        return
    cur.execute('select c.name from config c join wallets w on w.id = c.wallet_id where c.enabled order by c.name')
    for r in cur.fetchall():
        print(r['name'])


# The rollback reads positions with the code `git revert` brought back, which
# kept every signer at the top of the tree.
SCRIPTS = {'orca': 'signer2.mjs', 'meteora-dlmm': 'signer_dlmm.mjs', 'raydium-clmm': 'signer_raydium.mjs',
           'byreal': 'signer_byreal.mjs', 'pancakeswap-v3-solana': 'signer_pancake.mjs'}


def rollback_check():
    """Refuse the rollback while a profile other than sol-usdc holds anything
    in sol-usdc's wallet: an open position (in the ledger, or on chain by the
    venue signer's `positions`) or a claim. The old code would sweep its
    tokens, deploy its USDC and adopt its position. Base is another wallet the
    old code never touches."""
    problems = []
    if not has_column('config', 'wallet_id'):
        return
    cur.execute("select name, dex, pool from config where wallet_id = (select wallet_id from config "
                "where name = 'sol-usdc') and name <> 'sol-usdc' order by name")
    others = cur.fetchall()
    names = [r['name'] for r in others]
    cur.execute('select config_name, mint from positions where closed_at is null and config_name = any(%s)', (names,))
    problems += [f"{r['config_name']}: open position {r['mint']} in the ledger" for r in cur.fetchall()]
    cur.execute("select profile, mint, amount from wallet_claims where amount > 0 and profile = any(%s)", (names,))
    problems += [f"{r['profile']}: claims {r['amount']} of {r['mint']}" for r in cur.fetchall()]
    for r in others:
        script = os.path.join(CODE, SCRIPTS.get(r['dex'], ''))
        env = dict(os.environ, WALLET_SECRET_PATH=os.environ['DEPLOY_SOL_KEY'], LPBOT_POOL=r['pool'])
        try:
            res = subprocess.run(['node', script, 'positions'], capture_output=True, text=True, timeout=180, env=env)
            ok = res.returncode == 0 and res.stdout.strip()[:1] in '[{'
        except Exception as e:                      # no script for the venue, a timeout: not verified
            res, ok = None, False
        if not ok:
            problems.append(f"{r['name']}: positions on {r['dex']} unreadable, cannot prove the pool is empty")
        elif r['pool'].lower() in res.stdout.lower():
            problems.append(f"{r['name']}: open position on chain in {r['pool']}")
    for x in problems:
        print(f'{tag()}   REFUSED: {x}')
    if problems:
        raise SystemExit('ERROR: close these positions and pay out these claims first (touch run/<profile>/CLOSE '
                         'on a disabled profile), then rerun the rollback')
    print(f'{tag()}   no other profile holds anything in the sol-usdc wallet')


{'register': register, 'backfill': backfill, 'backfill_check': backfill, 'unbackfill': unbackfill,
 'enabled': enabled, 'rollback_check': rollback_check}[phase]()
if APPLY and phase != 'backfill_check':
    con.commit()
else:
    con.rollback()
con.close()
PY
}

preflight() {
  say "preflight: $MODE  live=$LIVE db='$DSN'"
  command -v psql >/dev/null || die 'psql not found'
  psql -X -q -d "$DSN" -c 'select 1' >/dev/null || die "database $DSN unreachable"
  [ -f "$SOL_KEY" ] || die "Solana key file $SOL_KEY missing"
  [ -d "$LIVE" ] || die "$LIVE missing"
  local dirty
  dirty=$($GIT -C "$LIVE" status --porcelain --untracked-files=no) || die "$LIVE is not a git work tree"
  [ -z "$dirty" ] || die "$LIVE has uncommitted changes: $(echo "$dirty" | head -5 | tr '\n' ' ')"
  if [ "$MODE" = rollback ]; then
    [ ! -e "$LIVE/wallets.py" ] || die "$LIVE still holds the multi-pool code: git revert the merge first, then rerun"
    return
  fi
  for f in rebalancer.py wallets.py chains.py sql/020_multi_wallet.sql sql/021_profile_mints.sql ops/lp-bot@.service; do
    [ -e "$LIVE/$f" ] || die "$LIVE/$f missing: merge the multi-pool branch into main first"
  done
  [ ! -e "$LIVE/HALT" ] || die "$LIVE/HALT exists: the operator halted the bot; deploy after it is lifted"
  python3 -m py_compile "$LIVE"/*.py || die 'python does not compile'
  # every script of the bot, in its venue, chain or shared folder too
  while IFS= read -r f; do node --check "$f" || die "$f does not parse"; done \
    < <(find "$LIVE" -name '*.mjs' -not -path '*/node_modules/*' -not -path '*/tests/*' -not -path '*/.claude/*')
  if [ -e "$EVM_KEY" ]; then
    mode=$(stat -c %a "$EVM_KEY")
    [ "$mode" = 600 ] || die "$EVM_KEY has mode $mode, not 600"
  else
    say "no EVM key at $EVM_KEY: base-weth-usdc is not created"
  fi
}

schema() {
  for m in 020_multi_wallet 021_profile_mints; do
    run psql -X -q -v ON_ERROR_STOP=1 -d "$DSN" -f "$LIVE/sql/$m.sql"
  done
}

move_check() {  # before the stop: every rename move_runtime will do can be done
  [ -w "$LIVE" ] || die "$LIVE is not writable"
  if [ -e "$LIVE/run/sol-usdc" ]; then [ -w "$LIVE/run/sol-usdc" ] || die "$LIVE/run/sol-usdc is not writable"; fi
  for f in $RUNTIME_FILES; do
    if [ -e "$LIVE/$f" ] && [ -e "$LIVE/run/sol-usdc/$f" ]; then
      die "$LIVE/run/sol-usdc/$f exists already: not overwriting it"
    fi
  done
}

move_runtime() {  # the old bot is stopped: its state and feed become run/sol-usdc's (renames: same inode)
  run mkdir -p "$LIVE/run/sol-usdc"
  for f in $RUNTIME_FILES; do
    if [ -e "$LIVE/$f" ]; then
      [ ! -e "$LIVE/run/sol-usdc/$f" ] || die "$LIVE/run/sol-usdc/$f exists already: not overwriting it"
      run mv "$LIVE/$f" "$LIVE/run/sol-usdc/$f"
    fi
  done
}

install_unit() {
  run $INSTALL -m 644 "$LIVE/ops/lp-bot@.service" "$UNIT_DIR/lp-bot@.service"
  run $SYSTEMCTL daemon-reload
}

STAGE=none                       # how far the restart got: restore() starts from there

restore() {  # a step after the stop failed: never leave nothing running
  local rc=$?
  trap - EXIT
  [ "$STAGE" = done ] && exit $rc
  echo "ERROR: the restart failed at stage '$STAGE'; bringing a bot back" >&2
  set +e
  if [ "$STAGE" = moved ] || [ "$STAGE" = started ]; then
    $SYSTEMCTL enable --now lp-bot@sol-usdc
  fi
  if ! $SYSTEMCTL_READ is-active --quiet lp-bot@sol-usdc; then
    # The code on disk is the merged code (preflight): it reads its state in
    # run/sol-usdc whichever unit starts it, so the renames are completed,
    # not undone, and the old unit, which needs nothing new from systemd,
    # runs it.
    $SYSTEMCTL disable --now lp-bot@sol-usdc
    mkdir -p "$LIVE/run/sol-usdc"
    for f in $RUNTIME_FILES; do
      if [ -e "$LIVE/$f" ] && [ ! -e "$LIVE/run/sol-usdc/$f" ]; then mv "$LIVE/$f" "$LIVE/run/sol-usdc/$f"; fi
    done
    $SYSTEMCTL enable --now lp-bot.service
  fi
  $SYSTEMCTL restart lp-telegram
  if $SYSTEMCTL_READ is-active --quiet lp-bot@sol-usdc; then echo "running: lp-bot@sol-usdc" >&2
  elif $SYSTEMCTL_READ is-active --quiet lp-bot.service; then echo "running: lp-bot.service" >&2
  else echo "NOTHING RUNS: start lp-bot.service by hand" >&2; fi
  exit 1
}

restart_profiles() {  # the one restart: the stopped window is stop -> SQL -> renames -> start
  local names others=0
  names=$(db enabled)                       # read before the stop: nothing that can fail waits inside
  trap restore EXIT
  STAGE=stopping
  run $SYSTEMCTL stop lp-bot.service
  run $SYSTEMCTL disable lp-bot.service
  STAGE=stopped
  db backfill
  STAGE=backfilled
  move_runtime
  STAGE=moved
  run $SYSTEMCTL enable --now lp-bot@sol-usdc
  STAGE=started
  for p in $names; do
    [ "$p" = sol-usdc ] && continue
    $SYSTEMCTL enable --now "lp-bot@$p" || { echo "ERROR: lp-bot@$p did not start" >&2; others=1; }
  done
  run $SYSTEMCTL restart lp-telegram
  STAGE=done
  trap - EXIT
  [ "$others" = 0 ] || die 'sol-usdc runs; a profile above did not start: look at its journal, then rerun'
}

start_profiles() {  # already restarted once: start what is enabled and not running
  local names
  if [ "$APPLY" = 1 ]; then names=$(db enabled); else names="sol-usdc $NEW_PROFILES (those enabled after --apply)"; fi
  say "profiles to run: $(echo $names)"
  run $SYSTEMCTL enable --now lp-bot@sol-usdc
  if [ "$APPLY" = 1 ]; then
    for p in $names; do
      [ "$p" = sol-usdc ] && continue
      run $SYSTEMCTL enable --now "lp-bot@$p"
    done
  fi
}

verify() {
  [ "$APPLY" = 1 ] || return 0
  sleep "$SETTLE_S"
  if $SYSTEMCTL_READ is-active --quiet lp-bot@sol-usdc; then
    say 'lp-bot@sol-usdc is active'
  else
    echo "ERROR: lp-bot@sol-usdc is not active. Look: journalctl -u lp-bot@sol-usdc -n 50; back out: git revert the merge, then $0 rollback --apply" >&2
    exit 1
  fi
}

deploy() {
  preflight
  schema
  db register
  install_unit
  if deployed; then
    say 'the profiles already run as lp-bot@<profile>: no restart'
    start_profiles
  elif [ "$APPLY" = 1 ]; then
    # Everything that can fail runs before the stop: the backfill SQL in a
    # transaction rolled back, the rename checks.
    db backfill_check
    move_check
    restart_profiles
  else
    say "\$ $SYSTEMCTL stop lp-bot.service; backfill; rename the runtime files; enable --now lp-bot@<profile>...; restart lp-telegram"
    db backfill
    move_check
  fi
  verify
  say "done. Rollback: git revert the merge in $LIVE, then $0 rollback --apply"
}

rollback() {
  preflight
  db rollback_check
  local units
  units=$($SYSTEMCTL_READ list-units --all --plain --no-legend 'lp-bot@*' 2>/dev/null | awk '{print $1}' || true)
  for u in $units; do run $SYSTEMCTL disable --now "$u"; done
  db unbackfill
  for f in $RUNTIME_FILES; do
    if [ -e "$LIVE/run/sol-usdc/$f" ]; then
      [ ! -e "$LIVE/$f" ] || die "$LIVE/$f exists: not overwriting it with run/sol-usdc/$f"
      run mv "$LIVE/run/sol-usdc/$f" "$LIVE/$f"
    fi
  done
  run $SYSTEMCTL enable --now lp-bot.service
  run $SYSTEMCTL restart lp-telegram
  say 'rolled back to lp-bot.service; the schema, the wallets and run/ stay (additive)'
}

if [ "$MODE" = rollback ]; then rollback; else deploy; fi
