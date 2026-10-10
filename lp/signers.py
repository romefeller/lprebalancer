"""Calls to the Node signers: the venue map, the wallet lock, breakers, claims, status reads."""

import json
import os
import pathlib
import re
import subprocess
import time

import chains
import config
import db
import fees
import guards
import health
import wallets
from venues import api as venue_api
from venues.jupiter import prices as jupiter_api
from lp import books, capital, paths, tuning

# One signer per DEX (venues/<venue>/), plus the chain-wide scripts
# (chains/<chain>/). A DEX without an entry can be scanned and recommended
# but never opened; `execute_dexes` must not name it.
SIGNERS = {'orca': str(paths.ROOT / 'venues/orca/signer.mjs'),
           'meteora-dlmm': str(paths.ROOT / 'venues/meteora_dlmm/signer.mjs'),
           'raydium-clmm': str(paths.ROOT / 'venues/raydium_clmm/signer.mjs'),
           'byreal': str(paths.ROOT / 'venues/byreal/signer.mjs'),
           'pancakeswap-v3-solana': str(paths.ROOT / 'venues/pancakeswap_v3/signer.mjs'),
           'aerodrome-slipstream': str(paths.ROOT / 'venues/aerodrome/signer.mjs'),   # Base
           'uniswap-v3-unichain': str(paths.ROOT / 'venues/uniswap_v3/signer.mjs'),   # Unichain
           'uniswap-v3-polygon': str(paths.ROOT / 'venues/uniswap_v3/signer.mjs'),    # Polygon, LPBOT_CHAIN=polygon
           'jupiter': str(paths.ROOT / 'venues/jupiter/swap.mjs'),                    # swaps, not positions
           'orca-swap': str(paths.ROOT / 'venues/orca/swap.mjs'),                     # the fallback swap
           'payout': str(paths.ROOT / 'chains/solana/payout.mjs'),                    # to the profit wallet only
           'janitor': str(paths.ROOT / 'chains/solana/janitor.mjs')}                  # closes empty token accounts
LAST_CHAIN_ERROR = {}             # {'args', 'text'}: the last signer failure in full, for the events table
SIGNER_ENV_WITHHELD = ('TELEGRAM_',)     # environment prefixes no signer gets (_chain)


def route(kind):
    """The SIGNERS key that does `kind` ('swap' or 'payout') on this chain:
    its own script on Solana (Jupiter, chains/solana/payout.mjs), the venue signer where the
    chain row says 'venue' (Base)."""
    via = config.CAPS[f'{kind}_via']
    return config.DEX if via == 'venue' else via


def housekeeper(kind):
    """Whether this process runs the wallet-wide chore `kind` ('sweep',
    'janitor', 'audit', 'scanner'): only the wallet's residual owner, and
    only where the chain allows it."""
    return bool(config.RESIDUAL_OWNER and config.CAPS.get(kind))


def rpc_host(url):
    """The host of an RPC URL, without a user:password@ or anything after
    the host: what a log line may show of an endpoint. Pure."""
    return re.sub(r'^https?://(?:[^@/?]*@)?([^/?]+).*$', r'\1', url)


def probe_rpc(url=None, fallback=None, timeout=10):
    """Whether the configured RPC answers the chain's probe (getSlot on
    Solana, eth_blockNumber on Base). At startup: a keyed endpoint that does
    not answer (a bad or expired key) is replaced by the public one, and the
    book says so, host only. Returns the URL in use."""
    import urllib.request
    url, fallback = url or config.RPC, fallback or config.PUBLIC_RPC
    if url == fallback:
        return url
    method = config.CAPS['probe']
    try:
        req = urllib.request.Request(url, data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method}).encode(),
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.load(r).get('result')
            ok = isinstance(res, int) or (isinstance(res, str) and res.startswith('0x'))
    except Exception as e:
        ok, why = False, f'{type(e).__name__}: {books.redact(e)}'
    else:
        why = 'no slot in the answer'
    host = rpc_host(url)
    if ok:
        print(f'🔑 RPC {host} answers', flush=True)
        return url
    config.RPC = fallback
    books.notify('rpc_fallback', host=host, reason=why[:200], using=rpc_host(fallback))
    return fallback


WRITE_COMMANDS = {'open', 'close', 'harvest', 'increase'}      # a venue's own transactions


def health_key(args, dex):
    """The breaker a signer call answers to, or None for calls that do not
    feed one (reads have their own counter, read_failures). Pure."""
    cmd = str(args[0]) if args else ''
    if dex == 'jupiter':
        return 'swap' if cmd == 'rebalance' else None
    return f'venue:{dex}' if cmd in WRITE_COMMANDS else None


def counts_as_failure(err):
    """Whether an error is the dependency's fault. Our own refusals, HALT and
    a missing signer are not: they say nothing about the venue. Pure."""
    e = str(err or '')
    return bool(e) and not re.search(r'^refused:|HALT present|^no signer for', e)


def record_health(key, out, err):
    """Feeds one operation's outcome to its breaker (health.py): a failure
    when nothing was sent and the error is the dependency's, a success when
    the call answered or sent. One call per operation, after its retries."""
    if not key:
        return
    failed = (err or not out) and not (out or {}).get('signature') and not (out or {}).get('noop')
    if failed and counts_as_failure(err or 'no result'):
        rec = health.record_failure(key, err or 'no result')
        state, _ok, wait = health.verdict(rec, time.time())
        print(f"{health.EMOJI[state]} health {key}: {state} after {rec['fails']} failure(s); "
              f"retry in {wait / 60:.0f} min: {err}", flush=True)
    elif not failed:
        health.record_success(key)


USDC_MINT = chains.SOLANA['usdc_mint']
PROBE_QUOTE = (f'{jupiter_api.JUPITER}/swap/v1/quote?inputMint={fees.NATIVE_MINT}&outputMint={USDC_MINT}'
               '&amount=10000000&slippageBps=50')                  # 0.01 SOL: a quote, never a swap


def jupiter_answers():
    """(ok, why): whether Jupiter quotes a small SOL to USDC swap now. Read-only,
    one gated request (jupgate). Never raises."""
    try:
        d = venue_api._get(PROBE_QUOTE, timeout=15)
    except Exception as e:
        return False, f'{type(e).__name__}: {books.tidy(e)}'
    try:
        ok = int((d or {}).get('outAmount') or 0) > 0
    except (TypeError, ValueError, AttributeError):
        ok = False
    return ok, None if ok else books.tidy(json.dumps(d)[:300] if d is not None else 'no answer')


def probe_breakers(now=None):
    """Closes the 'jupiter' and 'swap' breakers once their cooldown is over and
    Jupiter quotes again, so the light shows what the bot can do now, not the
    last failure (2026-10-01: red for hours after the cooldown, with nothing to
    clear it until a real swap). One read-only quote, only when a breaker is
    PROBING and Jupiter's own breaker allows a request. A failed probe counts
    for 'jupiter' only: the Orca fallback may still swap. Returns True or False
    after a probe, None when none was due. Never raises."""
    try:
        now = time.time() if now is None else now
        states = {k: health.allowed(k, now) for k in ('jupiter', 'swap')}
        due = [k for k, (_ok, st, _w, _r) in states.items() if st == health.PROBING]
        if not due or not states['jupiter'][0]:
            return None
        ok, why = jupiter_answers()
        if ok:
            for k in due:
                health.record_success(k, now)
            print(f"🟢 health probe: Jupiter quotes again; {', '.join(due)} closed", flush=True)
        else:
            if 'jupiter' in due:
                health.record_failure('jupiter', f'probe: {why}', now)
            print(f'🟡 health probe: Jupiter still failing ({why})', flush=True)
        return ok
    except Exception as e:
        print(f'health probe failed: {type(e).__name__}: {e}', flush=True)
        return None


def chain(*args, dex=None, timeout=tuning.SIGNER_TIMEOUT_S, extra_env=None, record=True):
    """Call the signer for `dex` (the active pool's by default).
    Returns (parsed_json, tidy_error). Every write feeds its breaker
    (health.py): the venue's for open/close/harvest, 'swap' for the swap.
    A caller that retries passes record=False for each attempt and records
    the operation once with record_health (2026-10-01: three attempts of one
    rate-limited swap counted as three failures and tripped the breaker).
    A write (--execute) of a profile with a wallet runs under the wallet's
    lock and books what it moved of shared tokens to this profile's claims
    (locked_chain)."""
    d = dex or config.DEX
    if '--execute' in args and config.WALLET_ID:
        out, err = locked_chain(*args, dex=d, timeout=timeout, extra_env=extra_env)
    else:
        out, err = _chain(*args, dex=d, timeout=timeout, extra_env=extra_env)
    if '--execute' in args:
        note_mint_refusal(str(args[0]), err, out)
    if record:
        record_health(health_key(args, d), out, err)
    return out, err


# A write's balances are read at `confirmed` commitment from a node at or past
# the write's own slot (wallets.read_balances, wallets.write_slot): a node a
# few slots behind is asked again every CLAIM_POLL_S, for up to CLAIM_SETTLE_S.
CLAIM_SETTLE_S = 60
CLAIM_POLL_S = 2
# A signature no node knows this long after the send never landed: its
# blockhash expired (~90 s). The pending write is then booked from the slot
# before it (its balances unchanged but the fee).
PENDING_EXPIRE_S = 300


def me_row(mint_a, mint_b):
    """This profile as wallets.py describes profiles, with its pool's live mints."""
    return {'name': config.PROFILE, 'mints': [mint_a, mint_b], 'deposit_mint': config.DEPOSIT_MINT,
            'residual_owner': config.RESIDUAL_OWNER, 'enabled': config.ENABLED}


def claim_problem(kind, why, **detail):
    """A claim left as it was, said in the feed and the events table."""
    books.notify(kind, reason=why, **detail)
    try:
        db.event(kind, f'{why} {json.dumps(detail, default=str)}')
    except Exception:
        pass


def claim_mints():
    """(mints, why, shared): the shared mints whose movements go to this
    profile's claims (wallets.claimed_mints), or None with `why` when they
    cannot be known; `shared` whether another profile signs with this wallet
    (then every write's slot is tracked). Alone on its wallet: no read."""
    try:
        profiles = wallets.wallet_profiles(config.WALLET_ID)
        if not any(p['name'] != config.PROFILE for p in profiles):
            return [], None, False
        (ma, _), (mb, _) = capital.pool_tokens()
        return (wallets.claimed_mints(config.PROFILE, [ma, mb], wallets.with_self(profiles, me_row(ma, mb))),
                None, True)
    except Exception as e:
        return None, f'mints unknown: {type(e).__name__}: {books.tidy(e)}', True


def native_giver():
    """The profile whose native token this profile's writes spend
    (wallets.native_giver), or None: the owner itself, a pool that holds the
    native token, a wallet nobody shares. An unknown answer is None: the
    write's claims are still measured, only its native flows are not."""
    try:
        profiles = wallets.wallet_profiles(config.WALLET_ID)
        (ma, _), (mb, _) = capital.pool_tokens()
        return wallets.native_giver(config.PROFILE, [ma, mb], wallets.with_self(profiles, me_row(ma, mb)),
                                    config.CAPS['native_mint'])
    except Exception:
        return None


def tries_in(wait_s):
    """How many reads fit in `wait_s` at one every CLAIM_POLL_S. Pure."""
    return 1 + max(int(wait_s // CLAIM_POLL_S), 0)


def measure(mints, min_slot=0, wait_s=0.0):
    """(balances of `mints`, slot) read at or past `min_slot`, asking again
    for up to `wait_s`; None when no such read came."""
    for k in range(tries_in(wait_s)):
        if k:
            time.sleep(CLAIM_POLL_S)
        got = wallets.read_balances(config.CHAIN, config.RPC, config.WALLET_ADDRESS, mints,
                                    config.CAPS['native_mint'])
        if got is not None and got[1] >= min_slot:
            return got
    return None


def signatures_of(out):
    """Every transaction signature a signer's answer names."""
    out = out or {}
    sigs = out.get('signatures') or [out.get('signature')]
    return [x for x in sigs if isinstance(x, str) and x]


def settle_pending(wait_s=0.0):
    """Book the wallet's pending write (wallet_settle), if one is there: its
    balances after it, read at or past the slot its signatures confirmed at,
    less its balances before, to the claims of the profile that sent it. No
    other write of the wallet goes out while one is pending, so the
    difference is that write's, after a crash too. True when nothing is left
    pending; False when it cannot be booked yet (nothing is guessed)."""
    settled, p = wallets.settle_state(config.WALLET_ID)
    if not p:
        return True
    need = max(int(p['before_slot']), settled)
    if p.get('signatures'):
        at = None
        for k in range(tries_in(wait_s)):
            if k:
                time.sleep(CLAIM_POLL_S)
            at = wallets.write_slot(config.CHAIN, config.RPC, p['signatures'])
            if at is not None:
                break
        if at is None and time.time() - float(p.get('sent_at') or 0) <= PENDING_EXPIRE_S:
            return False
        need = max(need, at or 0)
    nat = p.get('native')
    read = list(p['mints']) + ([nat['mint']] if nat else [])     # never a claimed mint (native_giver)
    if not read:
        wallets.book(config.WALLET_ID, p['profile'], {}, need)        # nothing claimed: the slot moves on
        return True
    got = measure(read, need, wait_s)
    if got is None:
        return False
    after, slot = got
    deltas = {m: after[m] - p['before'][m] for m in p['mints'] if abs(after[m] - p['before'][m]) > wallets.DUST}
    flows = native_flows(p, after[nat['mint']] - nat['before']) if nat else []
    res = wallets.book(config.WALLET_ID, p['profile'], deltas, slot, flows)
    for m, (new, over) in res.items():
        if over > wallets.DUST:
            # new - over is claim + delta (wallets.claim_after)
            claim_problem('claim_overdraw', 'a write moved more of a shared token than this profile claimed',
                          command=p['command'], mint=m, claim=round(new - over - deltas[m], 9),
                          delta=round(deltas[m], 9), overdraw=round(over, 9))
    return True


def native_flows(p, delta):
    """The internal flows (wallets.internal_flows) of pending write `p`,
    whose native balance moved by `delta`: its rent and fees out of the
    giver's sleeve, or a refund back into it. Priced by native_usd()."""
    try:
        profiles = wallets.wallet_profiles(config.WALLET_ID)
    except Exception:
        profiles = []
    sigs = ' '.join(p.get('signatures') or []) or 'no signature'
    return wallets.internal_flows(p['profile'], p['native']['giver'], delta, capital.native_usd(), profiles,
                                  p['native']['mint'], f"{p['command']} by {p['profile']}: {sigs}")


UNMEASURABLE = {}                 # the last 'claims unmeasurable' reason said, so it is said once


def unmeasurable(command, why):
    """The refusal of a write whose claims cannot be measured: nothing is
    sent, the next poll tries again. Said once per reason."""
    if UNMEASURABLE.get('why') != why:
        UNMEASURABLE['why'] = why
        claim_problem('claim_unmeasured', f'{why}; {command} not sent', command=command)
    return None, f'refused: claims unmeasurable ({why}); {command} not sent'


def locked_chain(*args, dex, timeout, extra_env):
    """_chain() under the wallet's advisory lock. On a wallet other profiles
    share, the claimed mints are read before (at or past the wallet's
    settled slot) and the write is recorded as pending before it is sent;
    after it, settle_pending books it. A write is refused, not sent, while an
    earlier write is unbooked, the mints are unknown or the before-read
    fails. A lock that cannot be taken sends nothing either. Every refusal
    starts 'refused:' (no breaker counts it)."""
    command = str(args[0])
    try:
        with wallets.wallet_lock(config.WALLET_ID):
            if not settle_pending(CLAIM_SETTLE_S):
                return unmeasurable(command, 'an earlier write of the wallet is not booked yet')
            mints, why, shared = claim_mints()
            if mints is None:
                return unmeasurable(command, why)
            if shared:
                # Even a write that books no claim (the holder's) moves the
                # wallet's settled slot: the next before-read starts after it.
                settled, _ = wallets.settle_state(config.WALLET_ID)
                giver = native_giver()
                nat = wallets.norm(config.CAPS['native_mint']) if giver else None
                read = list(mints) + ([nat] if nat else [])      # never a claimed mint (native_giver)
                got = measure(read, settled, CLAIM_SETTLE_S) if read else ({}, settled)
                if got is None:
                    return unmeasurable(command, 'balance before the write unreadable')
                pending = {'profile': config.PROFILE, 'command': command, 'mints': mints,
                           'before': {m: got[0][m] for m in mints}, 'before_slot': got[1], 'signatures': []}
                if giver:
                    pending['native'] = {'mint': nat, 'giver': giver, 'before': got[0][nat]}
                wallets.set_pending(config.WALLET_ID, pending)
            UNMEASURABLE.pop('why', None)
            out, err = _chain(*args, dex=dex, timeout=timeout, extra_env=extra_env)
            if shared:
                if signatures_of(out):
                    wallets.set_pending(config.WALLET_ID, dict(pending, signatures=signatures_of(out),
                                                               sent_at=time.time()))
                if not settle_pending(CLAIM_SETTLE_S):
                    # Never guessed: the write stays pending, every write of
                    # the wallet waits until a measurement books it.
                    claim_problem('claim_unsettled', 'the write is sent and not yet measured; the wallet '
                                                     'sends nothing until it is booked',
                                  command=command, signatures=signatures_of(out))
            return out, err
    except wallets.LockError as e:
        why = f'refused: {e}; {args[0]} not sent'
        books.notify('wallet_lock_timeout', reason=str(e), command=str(args[0]), waited_s=wallets.LOCK_WAIT_S,
                     action=f'{args[0]} not sent; retried at the next poll')
        try:
            db.event('wallet_lock_timeout', why)
        except Exception:
            pass
        return None, why


def _chain(*args, dex=None, timeout=tuning.SIGNER_TIMEOUT_S, extra_env=None):
    script = SIGNERS.get(dex or config.DEX)
    if not script or not pathlib.Path(script).exists():
        return None, f'no signer for {dex or config.DEX}'
    # A HALT (global or this profile's) stops every write here too, not only
    # in the loop and the signers: a write already on its way when the
    # operator halts is not spawned.
    stop = paths.halted() if '--execute' in args else None
    if stop:
        return None, f'refused: halted ({stop})'
    # Arguments reach node's argv, never a shell, but an address with a
    # newline in it is still not an address. Refuse before spawning.
    try:
        guards.signer_args(args)
        guards.inside(pathlib.Path(script), paths.ROOT)
    except guards.Refused as e:
        return None, f'refused: {e}'
    # The whole service environment passes through (LPBOT_PROFIT_WALLET_PIN,
    # LPBOT_EVM_PROFIT_WALLET_PIN: the signers check the pin themselves).
    # The gas reserve is in the chain's native token: _SOL for the Solana
    # scripts, _NATIVE for every signer that is not Solana's. LPBOT_RUN_DIR:
    # a signer refuses writes on this profile's HALT as on the global one.
    # LPBOT_CHAIN: the EVM Uniswap signer serves Unichain and Polygon and picks
    # its chain module by it (chains/evm/chains.mjs).
    # The profile's own signer gets its opt-ins (config.SIGNER_ENV); no other
    # script does.
    own = config.SIGNER_ENV if (dex or config.DEX) == config.DEX else {}
    # The bridge's Telegram token shares the EnvironmentFile; no signer needs it.
    base = {k: v for k, v in os.environ.items() if not k.startswith(SIGNER_ENV_WITHHELD)}
    env = dict(base, **own,
               WALLET_SECRET_PATH=config.WALLET,
               SOLANA_RPC_URL=config.RPC,
               LPBOT_RPC=config.RPC,
               LPBOT_POOL=config.POOL,
               LPBOT_MAX_USD=str(config.MAX_USD),
               LPBOT_SLIPPAGE_BPS=str(config.SLIPPAGE_BPS),
               LPBOT_GAS_RESERVE_SOL=str(config.GAS_RESERVE_SOL),
               LPBOT_GAS_RESERVE_NATIVE=str(config.GAS_RESERVE_SOL),
               LPBOT_RUN_DIR=str(paths.RUN),
               LPBOT_CHAIN=config.CHAIN,
               LPBOT_PROFIT_WALLET=config.PROFIT_WALLET, **(extra_env or {}))
    try:
        r = subprocess.run(['node', script, *args], capture_output=True,
                           text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, 'signer timed out'
    text = (r.stdout or '') + (r.stderr or '')
    # Only stdout carries signer results. Decode one object despite trailing logs.
    # An error/partial JSON result is never silently turned into success.
    decoder = json.JSONDecoder()
    for match in re.finditer(r'(?m)^\s*\{', r.stdout or ''):
        try:
            out, _ = decoder.raw_decode(r.stdout[match.start():].lstrip())
        except ValueError:
            continue
        if not isinstance(out, dict):
            continue
        err = out.get('error') or ('partial transaction execution' if out.get('partial') else None)
        if r.returncode or err:
            LAST_CHAIN_ERROR.update(args=' '.join(map(str, args[:1])), text=str(err or r.stderr or text)[:2000])
            return out, books.tidy(err or r.stderr or text) or f'signer exited {r.returncode}'
        return out, None
    LAST_CHAIN_ERROR.update(args=' '.join(map(str, args[:1])), text=text[:2000])
    return None, books.tidy(text) or (f'signer exited {r.returncode}' if r.returncode else 'no signer result')


def read_status(mint=None):
    """The open position, from the chain. Every signer gets the pool through
    LPBOT_POOL in its environment (see chain); a positional argument is a
    POSITION filter, never the pool. Passing the pool there once made the
    Meteora signer answer "no position" for a position it held, and the loop
    went to open a second one.

    Every answer with a position passes guards.fee_read_problem. An
    impossible fee figure is read once more; if it is still impossible, the
    fees of the last snapshot stand in (a lower bound: never more than was
    earned), `feesSuspect` says why, and nothing downstream (the harvest
    record, the split, the gas refill) sees the bad number."""
    out, err = chain('status', *([mint] if mint else []))
    capital.note_scale(out)
    if not (out and out.get('positionMint')):
        return out, err
    why = fee_problem(out)
    if not why:
        return out, err
    time.sleep(5)
    again, err2 = chain('status', *([mint] if mint else []))
    if again and again.get('positionMint') == out['positionMint'] and not fee_problem(again):
        return again, err2
    return sanitised(out, why), err


def fee_problem(status):
    try:
        prev = db.last_fees(status['positionMint'])
    except Exception:
        prev = None
    return guards.fee_read_problem(status, (prev or {}).get('accrued_usd'), (prev or {}).get('hours'))


def sanitised(status, why):
    """`status` with its fee figures replaced by the last snapshot's."""
    try:
        prev = db.last_fees(status['positionMint']) or {}
    except Exception:
        prev = {}
    out = dict(status)
    out['feesAccruedA'] = prev.get('accrued_a') or 0.0
    out['feesAccruedB'] = prev.get('accrued_b') or 0.0
    out['feesAccrued_USD'] = prev.get('accrued_usd') or 0.0
    q = capital.quote_price(out)
    out['feesAccrued_quote'] = out['feesAccrued_USD'] / q if q else None      # unknown quote price: no figure
    out['feesSuspect'] = why
    out['feesRejected'] = {k: status.get(k) for k in ('feesAccruedA', 'feesAccruedB', 'feesAccrued_USD')}
    try:
        db.event('fee_read_rejected', f'{status["positionMint"]}: {why}; read {out["feesRejected"]}')
    except Exception:
        pass
    books.notify('fee_read_rejected', reason=why, rejected=out['feesRejected'])
    return out


REFUSED_MINT = re.compile(r'^refused: (mint paused|transfer hook)')
MINT_HOLD = {'why': None}       # the mint refusal in force, said once per change


def mint_refusal(err):
    """Whether `err` is a signer's refusal to write on a pool whose mint is
    paused or carries a transfer hook. Not the venue's fault: no breaker
    counts it (counts_as_failure), no failure is counted, and the profile
    holds where it is (no failover: another venue has the same mint)."""
    return bool(err) and bool(REFUSED_MINT.match(str(err)))


# Refusals that say nothing about the venue and need only time: the wallet's
# lock was busy or unreachable, the claims could not be measured, a HALT.
# A gas price over LPBOT_EVM_MAX_GWEI holds too: the EVM signer sent nothing, and three
# refused reopens in a spike must not write HALT (edge test, 2026-10-10).
WAIT_REFUSAL = re.compile(r'^refused: (wallet \S+ lock|claims unmeasurable|halted|max fee \d+ wei/gas exceeds)')


def held(err):
    """Whether a write was refused for a reason that holds the profile where
    it is (mint_refusal, or WAIT_REFUSAL): nothing was sent, no failure is
    counted, no HALT is written, the next poll tries again."""
    return mint_refusal(err) or (bool(err) and bool(WAIT_REFUSAL.match(str(err))))


def note_mint_refusal(command, err, out):
    """The mint hold as chain() sees each write: 'mint_paused' once when a
    refusal starts or changes, 'mint_resumed' once when a write goes through
    again."""
    if mint_refusal(err):
        if MINT_HOLD['why'] != err:
            MINT_HOLD['why'] = err
            books.notify('mint_paused', reason=err, command=command,
                         action='holding: no open, no close, no failover until the mint is writable again')
            try:
                db.event('mint_paused', f'{command}: {err}')
            except Exception:
                pass
    elif MINT_HOLD['why'] and (out or {}).get('signature') and not err:
        MINT_HOLD['why'] = None
        books.notify('mint_resumed', command=command)
