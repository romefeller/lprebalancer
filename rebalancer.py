"""Rebalancer — a concentrated-liquidity rebalancer across Solana DEXes.

Holds one position, watches the price against its band, and when the price
leaves, collects the fees, closes, re-optimises the band and opens again. Every
number it reports comes from the chain or from its own ledger, never from a
simulation of what it thinks happened.

It also optimises the pool, not only the band. A scanner thread lists the
busiest pools on every DEX in the profile's `dexes`, scores each under the same
replay the band optimiser uses, and writes the board to Postgres. At each
re-optimisation the loop compares the pool it holds with the best pool on the
board; when the board's best beats it by `migrate_min_gain` and its DEX has a
signer (`execute_dexes`), the bot closes here and opens there. When it has no
signer for that DEX it says so, with the command to move by hand.

Every parameter comes from the `rebalancer.config` table and every observation
goes back into Postgres, so the pool, the band ladder and the guards are data,
not code.

    read chain -> in band?  yes -> forecast: P(exit within H hours) from the pool's own tape
                                   high -> harvest, close, re-centre NOW (quiet hour if one is near)
                                   low  -> record a snapshot, wait
                            no  -> harvest, close, re-optimise, reopen

The bot acts before the price leaves, not after. A rebalance at the edge makes
the position's whole loss against holding permanent and leaves it one-sided
and earning nothing until it runs; a re-centre while still inside is done at
a price of the bot's choosing, and the survival figures that drive it are in
every book it sends. Accrued fees are harvested into the wallet on a schedule
(the dividend), so income is realised and permanent rather than a number on
an open position.

Three things it does that a simpler loop gets wrong:

**A failed write is not a known outcome.** A transaction can land and still
report failure, because confirmation runs over the same rate-limited RPC that
just timed out. This happened in production: a close succeeded, reported
`close_failed`, and the bot left the capital idle. So after any write error the
bot re-reads the chain and believes what it finds there, not the error.

**A failed read is not an empty wallet.** Treating an RPC error as "no position"
makes a bot open a second one, and on a flaky endpoint it keeps opening them
until the wallet is empty. Reads that fail hold; only a read that succeeds and
reports nothing may open.

**Fees belong to you, not to the position.** A position's fee counter resets
every time you rebalance. Cumulative earnings live in the ledger, split into
realised (harvested into the wallet) and unrealised (still in the position).

One process runs one profile (LPBOT_PROFILE). Its runtime files and the
operator triggers live in run/<profile>/:

Stop this profile:              touch run/<profile>/HALT
Stop every profile and signer:  touch HALT
Force one rebalance now with:   touch run/<profile>/REBALANCE
Force the pool and band review: touch run/<profile>/REOPT
Move to a pool by hand:         echo "<dex> <pool>" > run/<profile>/MIGRATE

Profiles that share a wallet share its tokens through sleeves (wallets.py):
every write runs under the wallet's lock, and the wallet read the loop sizes
from is the profile's own sleeve, not the whole wallet.

The trigger exists because the rebalance path is the one that runs unattended,
and a path that has only ever run at 3am has never been watched. Touch the file
while you are looking and the next poll runs harvest -> close -> reopen at the
best band, subject to the same gap and daily limits as an automatic one.
"""
import json
import math
import os
import pathlib
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
import datetime as dt

import numpy as np

import calm
import health
import config
import db
import dexes
import engine
import fees
import guards
import scanner
import stats
import txfees
import audit
import chains
import wallets

ROOT = pathlib.Path(__file__).resolve().parent
RUN = config.RUN_DIR            # run/<profile>: this profile's state, feed and triggers
STATE = RUN / 'runtime.json'
FEED = RUN / 'events.jsonl'
HALT = RUN / 'HALT'             # stops this profile
HALT_ALL = ROOT / 'HALT'        # stops every profile, and every signer (they check it themselves)
REBALANCE = RUN / 'REBALANCE'
REOPT = RUN / 'REOPT'           # run the board and band review on the next poll
MIGRATE = RUN / 'MIGRATE'       # "<dex> <pool>": move there on the next poll
CLOSE = RUN / 'CLOSE'           # a disabled profile: harvest and close its position, open nothing
engine.use_network(config.CAPS['gecko_network'])
# One signer per DEX. A DEX without an entry can be scanned and recommended
# but never opened; `execute_dexes` must not name it.
SIGNERS = {'orca': str(ROOT / 'signer2.mjs'),
           'meteora-dlmm': str(ROOT / 'signer_dlmm.mjs'),
           'raydium-clmm': str(ROOT / 'signer_raydium.mjs'),
           'byreal': str(ROOT / 'signer_byreal.mjs'),
           'pancakeswap-v3-solana': str(ROOT / 'signer_pancake.mjs'),
           'aerodrome-slipstream': str(ROOT / 'signer_aerodrome.mjs'),     # Base: positions, swaps, payouts
           'jupiter': str(ROOT / 'swap_jupiter.mjs'),       # swaps, not positions
           'orca-swap': str(ROOT / 'swap_orca.mjs'),        # the fallback swap, direct on an Orca whirlpool
           'payout': str(ROOT / 'payout.mjs'),              # transfers to the profit wallet only
           'janitor': str(ROOT / 'janitor.mjs')}            # closes empty token accounts, rent to the wallet


def band_label(k):
    """'+/-1.5%' for 1.015: two decimals, trailing zeros dropped. The OPEN
    message said '+/-1%' for a +/-1.5% band (rounding) on 2026-09-26."""
    return '+/-' + f'{(float(k) - 1) * 100:.2f}'.rstrip('0').rstrip('.') + '%'


def stamp():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


NOISE = re.compile(r'^\s*bigint: Failed to load bindings|^\s*\(node:\d+\) \[?\w*\]? ?(Experimental|Deprecation)Warning'
                   r'|^\s*\(Use `node --trace-')
LAST_CHAIN_ERROR = {}             # {'args', 'text'}: the last signer failure in full, for the events table


def tidy(err, limit=140):
    """Turn a wall of RPC error text into one readable line.

    Raw provider errors arrive as a nested dump with headers and cookies. Sent
    to Telegram verbatim they read as gibberish with a 429 buried in them, which
    is exactly how a healthy bridge came to look like a broken one.
    """
    if not err:
        return None
    # Known noise lines first: the bigint warning took 76 of 140 characters
    # and cut the 2026-09-30 403 off before its cause.
    s = ' '.join(ln for ln in str(err).splitlines() if not NOISE.search(ln))
    s = ' '.join(s.split())
    if not s:
        return None
    # A program rejection is authoritative even if earlier RPC retries logged 429.
    if re.search(r'PriceSlippageCheck|price slippage check|0x1781\b|Custom["\s:]+6017\b', s, re.I):
        return 'PriceSlippageCheck (6017): price moved beyond the slippage limit'
    # The specific part of a program failure first: an Anchor "Error Message",
    # the custom error code, a JSON-RPC message. A simulation failure's log
    # dump buried them (2026-09-28 18:56: only "Program data: 7XCU..." kept).
    anchor = re.search(r'Error Code: (\w+)\. Error Number: (\d+)\. Error Message: ([^."\]]+)', s)
    if anchor:
        return f'program error {anchor.group(1)} ({anchor.group(2)}): {anchor.group(3).strip()}'[:limit]
    code = re.search(r'custom program error: 0x[0-9a-fA-F]+', s)
    if code and re.search(r'simulation failed|failed on chain|InstructionError', s, re.I):
        head = re.search(r'Error processing Instruction \d+', s)
        return (f'{head.group(0)}: ' if head else '') + code.group(0)
    program = re.search(r'(?:InstructionError|custom program error|failed on chain|simulation failed).*', s, re.I)
    if program:
        return program.group(0)[:limit]
    if re.search(r'Indexed requests|personal token', s, re.I):
        return 'RPC endpoint refuses indexed reads (403: needs a personal token)'
    if re.search(r'^Jupiter \d+|\bJupiter 429\b', s):
        return ('Jupiter rate limited: ' if re.search(r'\bJupiter 429\b', s) else '') + s[:limit]
    for needle, plain in (
            ('Too Many Requests', 'RPC rate limited'),
            ('timeout', 'RPC timeout'),
            ('ECONNRESET', 'RPC connection reset'),
            ('blockhash', 'blockhash expired')):
        if needle.lower() in s.lower():
            return plain
    if re.search(r'\b429\b', s):
        return 'RPC rate limited'
    rpc_msg = re.search(r'"message"\s*:\s*"([^"]{3,})"', s)
    if rpc_msg:
        return rpc_msg.group(1)[:limit]
    return s[:limit]


def _load_emoji():
    try:
        m = json.loads((ROOT / 'event_emoji.json').read_text())
        return {k: v for k, v in m.items() if not k.startswith('_') and isinstance(v, str)}
    except Exception:
        return {}


EVENT_EMOJI = _load_emoji()


def emoji_for(event):
    """The event's emoji (event_emoji.json, shared with the Telegram bridge),
    or by rule: a failure ❌, a deferral ⏳, anything else ▫️. Pure."""
    e = str(event)
    if e in EVENT_EMOJI:
        return EVENT_EMOJI[e]
    if re.search(r'fail|unreadable|refused|rejected|error', e, re.I):
        return '❌'
    if re.search(r'defer|skip|wait|held', e, re.I):
        return '⏳'
    return '▫️'


SECRET_IN_URL = re.compile(r'(api[-_]?key=)[^&\s"\'<>]+', re.I)


def redact(text):
    """Text with every api-key in a URL replaced by ***. The keyed RPC URL
    carries the Helius key; no feed row, log line or event may show it."""
    return SECRET_IN_URL.sub(r'\1***', str(text))


def halted():
    """The text of the HALT that stops this profile (the global one first),
    or None."""
    for f in (HALT_ALL, HALT):
        if f.exists():
            return f.read_text().strip() or str(f)
    return None


def route(kind):
    """The SIGNERS key that does `kind` ('swap' or 'payout') on this chain:
    its own script on Solana (Jupiter, payout.mjs), the venue signer where the
    chain row says 'venue' (Base)."""
    via = config.CAPS[f'{kind}_via']
    return config.DEX if via == 'venue' else via


def housekeeper(kind):
    """Whether this process runs the wallet-wide chore `kind` ('sweep',
    'janitor', 'audit', 'scanner'): only the wallet's residual owner, and
    only where the chain allows it."""
    return bool(config.RESIDUAL_OWNER and config.CAPS.get(kind))


def notify(event, **payload):
    # Every row says whose it is: the bridge tails every profile's feed.
    row = {'t': stamp(), 'event': event, **payload}
    # wallet_tag: the address's first 10 characters, how every report names a wallet
    for k, v in (('profile', config.PROFILE), ('wallet_id', config.WALLET_ID),
                 ('wallet_tag', db.wallet_tag(config.WALLET_ADDRESS)), ('chain', config.CHAIN),
                 ('pair', config.PAIR_LABEL)):
        row.setdefault(k, v)
    line = redact(json.dumps(row, default=str))
    FEED.parent.mkdir(parents=True, exist_ok=True)
    with open(FEED, 'a') as fh:
        fh.write(line + '\n')
    print(redact(f'[{row["t"]}] {emoji_for(event)} {event}: {json.dumps(payload, default=str, sort_keys=True)}'),
          flush=True)


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
        ok, why = False, f'{type(e).__name__}: {redact(e)}'
    else:
        why = 'no slot in the answer'
    host = re.sub(r'^https?://([^/?]+).*$', r'\1', url)
    if ok:
        print(f'🔑 RPC {host} answers', flush=True)
        return url
    config.RPC = fallback
    notify('rpc_fallback', host=host, reason=why[:200], using=re.sub(r'^https?://([^/?]+).*$', r'\1', fallback))
    return fallback


def nearest(runs, pct, tolerance=2):
    """The modelled band closest to the one actually held.

    A band opened at one price and measured at another rarely lands exactly on
    a rung of the ladder, and a held band with no run to compare against means
    no re-optimisation ever happens.
    """
    if not runs:
        return None
    k = min(runs, key=lambda x: abs(x - pct))
    return runs[k] if abs(k - pct) <= tolerance else None


def regime_at_move(view, lower, upper, moves_24h=None):
    """The regime view as it is right after a move. Pure. With a new band
    (lower, upper): held, held_pct, inside and p_held describe it, p_held
    from the view's own probability for that width. Without one (a close):
    nothing is held. moves_24h, when given, replaces the poll's count."""
    v = dict(view)
    if moves_24h is not None:
        v['moves_24h'] = moves_24h
    if lower and upper and upper > lower > 0:
        half = math.sqrt(upper / lower)
        widths = [k for k in config.REGIME_WIDTHS]
        held = min(widths, key=lambda k: abs(k - half))
        v['held'], v['held_pct'], v['inside'] = held, round((half - 1) * 100, 2), True
        pct = round((held - 1) * 100, 2)
        v['p_held'] = next((p for w, p in (v.get('probs') or []) if abs(float(w) - pct) < 1e-6), None)
    else:
        v['held'] = v['held_pct'] = v['p_held'] = None
        v['inside'] = False
    return v


def notify_book(event, **payload):
    """notify() with the cumulative book attached.

    The book is merged last and wins on any shared key, so a caller can pass a
    convenient label without risking a duplicate-keyword error at the one moment
    the bot most needs to report something.
    """
    # Every book carries the latest calm view while calm mode is on, not only
    # the in-band one: the OPEN/CLOSE books after a calm move are exactly the
    # ones where it matters.
    if config.CALM_ENABLED and payload.get('calm') is None and LAST_CALM.get('view'):
        payload = dict(payload, calm=LAST_CALM['view'])
    if config.REGIME_ENABLED and payload.get('regime') is None and LAST_REGIME.get('view'):
        payload = dict(payload, regime=LAST_REGIME['view'])
    if payload.get('venues') is None and LAST_VENUES.get('view'):
        payload = dict(payload, venues=LAST_VENUES['view'][:4])
    # At an OPEN or a CLOSE the caller knows the position's mark before any
    # snapshot of it exists: the LP line is built from that (db.deployment_now),
    # and the regime block describes the band the move left behind it, not
    # the one the last poll saw (audit, 2026-09-30: 15 of 19 OPEN books).
    lp_now = payload.pop('lp_now_usd', None)
    moves_now = payload.pop('moves_24h_now', None)
    if lp_now is not None and isinstance(payload.get('regime'), dict):
        opened = event == 'OPEN'
        payload = dict(payload, regime=regime_at_move(payload['regime'], payload.get('lower') if opened else None,
                                                      payload.get('upper') if opened else None, moves_now))
    if payload.get('health') is None:
        payload = dict(payload, health=health.summary())
    book = db.stats()
    if lp_now is not None:
        book = {**book, **db.deployment_now(book, lp_now)}
    return notify(event, **{**payload, **book})


WRITE_COMMANDS = {'open', 'close', 'harvest'}      # a venue's own transactions


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


USDC_MINT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
PROBE_QUOTE = (f'{dexes.JUPITER}/swap/v1/quote?inputMint={fees.NATIVE_MINT}&outputMint={USDC_MINT}'
               '&amount=10000000&slippageBps=50')                  # 0.01 SOL: a quote, never a swap


def jupiter_answers():
    """(ok, why): whether Jupiter quotes a small SOL to USDC swap now. Read-only,
    one gated request (jupgate). Never raises."""
    try:
        d = dexes._get(PROBE_QUOTE, timeout=15)
    except Exception as e:
        return False, f'{type(e).__name__}: {tidy(e)}'
    try:
        ok = int((d or {}).get('outAmount') or 0) > 0
    except (TypeError, ValueError, AttributeError):
        ok = False
    return ok, None if ok else tidy(json.dumps(d)[:300] if d is not None else 'no answer')


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


def chain(*args, dex=None, timeout=420, extra_env=None, record=True):
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
    notify(kind, reason=why, **detail)
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
        (ma, _), (mb, _) = pool_tokens()
        return (wallets.claimed_mints(config.PROFILE, [ma, mb], wallets.with_self(profiles, me_row(ma, mb))),
                None, True)
    except Exception as e:
        return None, f'mints unknown: {type(e).__name__}: {tidy(e)}', True


def native_giver():
    """The profile whose native token this profile's writes spend
    (wallets.native_giver), or None: the owner itself, a pool that holds the
    native token, a wallet nobody shares. An unknown answer is None: the
    write's claims are still measured, only its native flows are not."""
    try:
        profiles = wallets.wallet_profiles(config.WALLET_ID)
        (ma, _), (mb, _) = pool_tokens()
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
    return wallets.internal_flows(p['profile'], p['native']['giver'], delta, native_usd(), profiles,
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
        notify('wallet_lock_timeout', reason=str(e), command=str(args[0]), waited_s=wallets.LOCK_WAIT_S,
               action=f'{args[0]} not sent; retried at the next poll')
        try:
            db.event('wallet_lock_timeout', why)
        except Exception:
            pass
        return None, why


def _chain(*args, dex=None, timeout=420, extra_env=None):
    script = SIGNERS.get(dex or config.DEX)
    if not script or not pathlib.Path(script).exists():
        return None, f'no signer for {dex or config.DEX}'
    # A HALT (global or this profile's) stops every write here too, not only
    # in the loop and the signers: a write already on its way when the
    # operator halts is not spawned.
    stop = halted() if '--execute' in args else None
    if stop:
        return None, f'refused: halted ({stop})'
    # Arguments reach node's argv, never a shell, but an address with a
    # newline in it is still not an address. Refuse before spawning.
    try:
        guards.signer_args(args)
        guards.inside(pathlib.Path(script), ROOT)
    except guards.Refused as e:
        return None, f'refused: {e}'
    # The whole service environment passes through (LPBOT_PROFIT_WALLET_PIN,
    # LPBOT_EVM_PROFIT_WALLET_PIN: the signers check the pin themselves).
    # The gas reserve is in the chain's native token: _SOL for the Solana
    # scripts, _NATIVE for every signer that is not Solana's. LPBOT_RUN_DIR:
    # a signer refuses writes on this profile's HALT as on the global one.
    # The profile's own signer gets its opt-ins (config.SIGNER_ENV); no other
    # script does.
    own = config.SIGNER_ENV if (dex or config.DEX) == config.DEX else {}
    env = dict(os.environ, **own,
               WALLET_SECRET_PATH=config.WALLET,
               SOLANA_RPC_URL=config.RPC,
               LPBOT_RPC=config.RPC,
               LPBOT_POOL=config.POOL,
               LPBOT_MAX_USD=str(config.MAX_USD),
               LPBOT_SLIPPAGE_BPS=str(config.SLIPPAGE_BPS),
               LPBOT_GAS_RESERVE_SOL=str(config.GAS_RESERVE_SOL),
               LPBOT_GAS_RESERVE_NATIVE=str(config.GAS_RESERVE_SOL),
               LPBOT_RUN_DIR=str(RUN),
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
            return out, tidy(err or r.stderr or text) or f'signer exited {r.returncode}'
        return out, None
    LAST_CHAIN_ERROR.update(args=' '.join(map(str, args[:1])), text=text[:2000])
    return None, tidy(text) or (f'signer exited {r.returncode}' if r.returncode else 'no signer result')


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
    note_scale(out)
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
    q = quote_price(out)
    out['feesAccrued_quote'] = out['feesAccrued_USD'] / q if q else None      # unknown quote price: no figure
    out['feesSuspect'] = why
    out['feesRejected'] = {k: status.get(k) for k in ('feesAccruedA', 'feesAccruedB', 'feesAccrued_USD')}
    try:
        db.event('fee_read_rejected', f'{status["positionMint"]}: {why}; read {out["feesRejected"]}')
    except Exception:
        pass
    notify('fee_read_rejected', reason=why, rejected=out['feesRejected'])
    return out


STATE_DEFAULTS = {'last_rebalance': 0, 'rebalance_times': [], 'failures': 0,
                  'read_failures': 0, 'last_reopt': 0, 'last_harvest': 0,
                  'calm_times': [], 'calm': False}


def load():
    """runtime.json, with every key the loop indexes present. A corrupt file
    is set aside (runtime.json.corrupt) and the loop starts from defaults: a
    crash loop on a bad file is worse than forgetting the rebalance clock."""
    if STATE.exists():
        try:
            s = json.loads(STATE.read_text())
            if not isinstance(s, dict):
                raise ValueError('not an object')
            return {**STATE_DEFAULTS, **s}
        except Exception:
            try:
                STATE.replace(STATE.with_suffix('.json.corrupt'))
            except Exception:
                pass
    return dict(STATE_DEFAULTS)


def save(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(s, indent=1, default=str))
    tmp.replace(STATE)


def halt(reason):
    """Stop this profile (its own HALT; the others run on)."""
    HALT.parent.mkdir(parents=True, exist_ok=True)
    HALT.write_text(f'{stamp()} {reason}')
    db.event('BREAKER', reason)
    notify('BREAKER', reason=reason, action='HALT written; will not restart')


def wallet(pool):
    """What this profile holds of this pool's two tokens, and its dollar
    value: its sleeve of the wallet (sleeve_of).

    Both tokens, not just SOL. After a close the withdrawn quote token sits in
    the wallet; counting SOL alone would drop it from equity and report a loss
    on every rebalance that never happened.
    """
    before = settle_mark()
    out, _ = chain('balance', pool)
    note_scale(out)
    if not out or 'balanceA' not in out:
        # One retry. The read that follows a close lands on an endpoint that
        # has just confirmed a transaction for us and is quick to rate-limit;
        # a second try ten seconds later has read cleanly every time so far.
        time.sleep(10)
        out, _ = chain('balance', pool)
        note_scale(out)
    return unsettled_guard(sleeve_of(out or {}), before, settle_mark())


def settle_mark():
    """The wallet's (settled slot, pending write) now, to bracket a balance
    read and the claims read after it; None without a wallet, UNREADABLE
    (a pending write that is never booked) when the database cannot say."""
    if not config.WALLET_ID:
        return None
    try:
        return wallets.settle_state(config.WALLET_ID)
    except Exception:
        return UNREADABLE


UNREADABLE = (None, 'settle state unreadable')


def unsettled_guard(view, before, after):
    """`view` with walletUsd None when a write of the wallet was pending, or
    was booked, between the balance read (`before`) and the claims read
    (`after`): the balances and the claims then describe two different
    moments, and the residual holder's sleeve is off by the write (2026-10-02
    20:15Z: djt-usdc's open landed, its claim was not yet cut, and sol-usdc
    read $0 of USDC: equity -$5.84 for one snapshot). The snapshot then
    records equity unknown, not a loss. Pure."""
    if before is None or 'walletUsd' not in view:
        return view
    if before == after and not before[1]:
        return view
    return dict(view, walletUsd=None, unsettled=True)


_MINTS_SEEN = {}                  # profile -> the mints last written to config.mints


def is_stable_mint(mint):
    return wallets.norm(mint) in engine.STABLE_MINTS


def sleeve_of(bal):
    """`bal` as this profile sees it (wallets.sleeve): the wallet's figures
    stay under rawBalanceA/rawBalanceB/rawWalletUsd. A quote price the signer
    could not give is unknown, except for a stablecoin by mint, which is a
    dollar. Without a wallet (a pre-020 profile), or when this profile is its
    wallet's only enabled one, the read as it is. When the sleeve cannot be
    worked out the answer is {}: an unreadable wallet, never the whole one."""
    if 'balanceA' not in bal or (config.WALLET_ID is None and bal.get('quoteUsd') is not None):
        return bal                                        # a pre-020 profile with a priced read: as it is
    try:
        (ma, _), (mb, _) = pool_tokens()
    except Exception:
        ma = mb = None
    if bal.get('quoteUsd') is None and mb and is_stable_mint(mb):
        bal = dict(bal, quoteUsd=1.0, quoteUsdSource='stable mint')
    if not config.WALLET_ID:
        return bal
    try:
        profiles = wallets.wallet_profiles(config.WALLET_ID)
        if ma is None:
            if any(p['name'] != config.PROFILE for p in profiles):
                notify('sleeve_unreadable', reason='pool tokens unknown; the wallet is shared, so no figure')
                return {}
            return bal
        if _MINTS_SEEN.get(config.PROFILE) != [ma, mb]:
            wallets.register_mints(config.PROFILE, [ma, mb])
            _MINTS_SEEN[config.PROFILE] = [ma, mb]
        view = wallets.sleeve(bal, config.PROFILE, ma, mb, wallets.with_self(profiles, me_row(ma, mb)),
                              wallets.claims(config.WALLET_ID), config.CAPS['native_mint'])
        if view.get('nativeSide') is not None:
            # This profile's pool holds the native token: every other profile
            # of the wallet pays its opens' rent from it, so it keeps that back.
            view['nativeReserve'] = config.GAS_RESERVE_SOL + open_headroom(config.DEX) + sum(
                open_headroom(p.get('dex')) for p in profiles if p['name'] != config.PROFILE)
        return view
    except Exception as e:
        notify('sleeve_unreadable', reason=f'{type(e).__name__}: {tidy(e)}')
        return {}


def ui_price(rec):
    """B per A in UI units, for valuing the UI amounts a signer reports
    (balances, caps, close estimates). The signer's `uiPrice`; its pool-native
    `price` when it has none (a plain mint: the same number). Bands and the
    open's lower/upper stay pool-native. Pure."""
    v = rec.get('uiPrice')
    return float(v if v is not None else rec['price'])


# The rent an open takes and does not give back at once, by venue, in SOL: a
# position's accounts and the tick or bin arrays its range is first to use.
# Raydium layout: 0.0053 + two tick arrays of 0.0018. Meteora DLMM: a new bin
# array is 0.0435 (the MU open, simulated 2026-10-01).
OPEN_RENT_HEADROOM_SOL = 0.009       # every venue not listed below
# Aerodrome (Base) keeps no rent: an NFT mint costs gas only, inside the gas
# reserve; 0.009 of ETH there would leave ~$25 never deployed.
OPEN_RENT_HEADROOM = {'meteora-dlmm': 0.05, 'aerodrome-slipstream': 0.0}


def open_headroom(dex):
    return OPEN_RENT_HEADROOM.get(dex, OPEN_RENT_HEADROOM_SOL)


def native_reserve(bal):
    """Native SOL never deployed: the gas reserve and this venue's open rent,
    plus (sleeve_of puts it in `nativeReserve`) the open rent of every other
    profile of the wallet, which pays its rent and fees from this SOL too."""
    v = bal.get('nativeReserve')
    return float(v) if v is not None else config.GAS_RESERVE_SOL + open_headroom(config.DEX)


def deployable_usd(bal):
    """What the wallet can put into a position, in dollars: every unit of the
    pool's two tokens, less the native reserve on the native side. The only
    money that stays out is the gas the bot needs. None when the quote
    token's price is unknown: never valued as a dollar."""
    q = bal.get('quoteUsd')
    if q is None:
        return None
    res = native_reserve(bal)
    a = max(float(bal.get('balanceA') or 0.0) - (res if bal.get('nativeSide') == 'A' else 0.0), 0.0)
    b = max(float(bal.get('balanceB') or 0.0) - (res if bal.get('nativeSide') == 'B' else 0.0), 0.0)
    return (a * ui_price(bal) + b) * q


def capital(bal=None):
    """The sizing base, held under the ceiling a deposit must respect (each
    side is at most side_cap_fraction of it, and the signer refuses an open
    worth more than max_usd). With deploy_all and a wallet read, it is
    everything the wallet can deploy (deployable_usd); otherwise the
    configured capital plus every fee reinvested under the split. Callers
    with a wallet read check its quote price first (quote_known)."""
    if config.DEPLOY_ALL and bal and 'balanceA' in bal and bal.get('price'):
        base = deployable_usd(bal)
        if base is None:
            raise ValueError('quote price unknown: no capital figure')
    else:
        base = config.CAPITAL_USD
        if config.PAYOUT_ENABLED:
            try:
                base += db.reinvested_usd(config.PROFILE)
            except Exception:
                pass
    return min(base, config.MAX_USD / (2 * config.SIDE_CAP_FRACTION))


def side_target_fraction():
    """The share of the capital each side should hold before an open. A
    centred band takes exactly half in value (both sides are L*sqrt(P)*(1 -
    1/sqrt(k))), so when the capital is the whole wallet each side aims at
    one half; when it is a slice, each side may hold up to the side cap."""
    return 0.5 if config.DEPLOY_ALL else config.SIDE_CAP_FRACTION


def deposit_caps(bal):
    """Per-token deposit caps for an open, from what the wallet actually holds.

    Each side is capped at `side_cap_fraction` of the capital, in that token's
    own units, and at the wallet's balance of it less the gas reserve when the
    token is native SOL. The SDK's quote picks the liquidity both caps allow, so
    a wallet that is short one side opens a smaller position rather than failing.
    Caps are UI amounts, so token A's is divided by the UI price.
    """
    price = ui_price(bal)
    quote_usd = float(bal['quoteUsd'])               # known: reopen() checked (quote_known)
    capital_quote = capital(bal) / quote_usd
    # The gas reserve, plus the rent the open itself takes (open_headroom)
    # and the other profiles' (native_reserve). Without it every open left
    # gas below the reserve (0.0429 of 0.05 on 2026-09-26).
    reserve = native_reserve(bal)
    avail_a = bal['balanceA'] - (reserve if bal.get('nativeSide') == 'A' else 0)
    avail_b = bal['balanceB'] - (reserve if bal.get('nativeSide') == 'B' else 0)
    cap_a = min(max(avail_a, 0), capital_quote * config.SIDE_CAP_FRACTION / price)
    cap_b = min(max(avail_b, 0), capital_quote * config.SIDE_CAP_FRACTION)
    return cap_a, cap_b


def position_usd(status):
    """Mark of the position: what a close would return right now, tokens AND
    the rent locked in the position accounts.

    The signer already values the tokens in dollars when it knows the quote
    token's price. Falling back to quote units is only correct when the quote
    token is a dollar, which is why the signer's figure is preferred. The rent
    is a deposit the chain refunds on close: a 387-bin Meteora position holds
    0.2 SOL of it, and a book that ignored it reported the first move to
    Meteora as a $27 loss that never happened.
    """
    rent = rent_usd(status)
    if status.get('positionUsd') is not None:
        return status['positionUsd'] + rent
    a, b, q = status.get('closeEstA'), status.get('closeEstB'), status.get('quoteUsd')
    if a is None or b is None or q is None:
        return None                                  # an unknown quote price is no dollar
    return (a * ui_price(status) + b) * q + rent


# The native token's last known USD price (rent is native): {'px', 'at'}.
NATIVE_PX = {}
NATIVE_PX_MAX_AGE_S = 3600        # a cached price this old still prices rent
NATIVE_SNAPSHOT_MAX_AGE_S = 900   # a native/stable pool's snapshot this old prices it


def note_native_px(px):
    """Remember a good native price (USD per native unit)."""
    if px is not None and px > 0:
        NATIVE_PX.update(px=float(px), at=time.time())


def native_usd():
    """USD per native token (SOL): the latest snapshot of a native/stable
    pool on this database (sol-usdc's pool) when it is fresh, else the last
    good price seen in this process when it is under an hour old, else None."""
    try:
        px = db.native_price(config.CAPS['native_mint'], sorted(engine.STABLE_MINTS), NATIVE_SNAPSHOT_MAX_AGE_S)
    except Exception:
        px = None
    if px:
        note_native_px(px)
        return px
    if NATIVE_PX and time.time() - NATIVE_PX['at'] <= NATIVE_PX_MAX_AGE_S:
        return NATIVE_PX['px']
    return None


def rent_usd(status):
    """The rent in the position accounts, in USD. The signer prices it when
    it can; a pool with no native side asks Jupiter, and one failed request
    gave null, which counted as $0 (2026-10-02: mu-usdc's equity jumped by
    the $8.26 rent on one poll in four). A null is priced here
    (native_usd); only with no price at all is it 0, said once."""
    usd, sol = status.get('rentUsd'), status.get('rentSol')
    if usd is not None:
        if sol:
            note_native_px(float(usd) / float(sol))
        return float(usd)
    if not sol:
        return 0.0
    px = native_usd()
    if px is None:
        if not RENT_UNPRICED.get('told'):
            RENT_UNPRICED['told'] = True
            notify('rent_unpriced', reason=f'{sol} native rent has no price; counted as $0 this poll')
        return 0.0
    RENT_UNPRICED.pop('told', None)
    return float(sol) * px


RENT_UNPRICED = {}                # 'told' while an unpriced rent has been said


def quote_price(rec):
    """USD per unit of the quote token for a balance or status read, or None
    when unknown. A null from the signer is unknown, never a dollar, except
    for a stablecoin quote by mint (the held pool's token B)."""
    q = rec.get('quoteUsd')
    if q is not None:
        return float(q)
    try:
        mb = pool_tokens()[1][0]
    except Exception:
        return None
    return 1.0 if is_stable_mint(mb) else None


def quote_known(state, bal, what):
    """Whether `bal` carries its quote token's price. When it does not, the
    move (`what`) is skipped and why is said once, until a price is back."""
    if bal.get('quoteUsd') is not None:
        if state.pop('quote_unknown_told', None) is not None:
            save(state)
        return True
    if not state.get('quote_unknown_told'):
        state['quote_unknown_told'] = time.time(); save(state)
        why = f'{config.PAIR_LABEL}: the quote token has no USD price; {what} skipped until it has one'
        notify('quote_unknown', reason=why)
        try:
            db.event('quote_unknown', why)
        except Exception:
            pass
    return False


# --- the tape, the forecast, the dividend -------------------------------------

_TAPE = {}                      # pool -> (fetched_at, candles)
TAPE_REFRESH = 3600             # one GeckoTerminal call an hour, at most
# GeckoTerminal prices a pool in UI units; the bands, the status price and
# the stored tape are pool-native. pool -> pool-native / UI price, from the
# last signer read that gave both (1 for a pool of plain mints). A Token-2022
# stock's multiplier (MSFTx 1.0059) is more than half a +/-1% band's margin.
UI_SCALE = {}


def note_scale(rec):
    """Remember the pool's native/UI price ratio from a signer read."""
    rec = rec or {}
    pool, px, ui = rec.get('whirlpool') or rec.get('pool'), rec.get('price'), rec.get('uiPrice')
    try:
        if pool and px and ui and float(px) > 0 and float(ui) > 0:
            UI_SCALE[pool] = float(px) / float(ui)
    except (TypeError, ValueError):
        pass


def native_bars(bars, scale):
    """Bars with their price columns (open, high, low, close) times `scale`:
    UI prices made pool-native. Pure; None stays None."""
    if bars is None or scale == 1.0:
        return bars
    return tuple(c * scale if i in (1, 2, 3, 4) else c for i, c in enumerate(bars))


def tape(pool):
    """The pool's hourly closes, refreshed at most once an hour. A fetch that
    fails leaves the last tape in place: a forecast an hour stale beats none."""
    t, c = _TAPE.get(pool, (0, None))
    if c is None or time.time() - t > TAPE_REFRESH:
        try:
            fresh = engine.candles(pool)
        except Exception:
            fresh = None
        if fresh:
            s = UI_SCALE.get(pool, 1.0)
            c = (fresh[0], fresh[1] * s, fresh[2]) if s != 1.0 else fresh
            _TAPE[pool] = (time.time(), c)
    return c


def forecast_for(status):
    """The band forecast for the held position, or None without a tape."""
    c = tape(status.get('whirlpool') or config.POOL)
    if not c:
        return None
    opened = db.position_opened(status['positionMint'])
    hours = ((db.now() - opened).total_seconds() / 3600) if opened else None
    return engine.band_forecast(c[1], status['price'], status['lowerPrice'], status['upperPrice'],
                                open_price=db.position_open_price(status['positionMint']),
                                hours_alive=hours, horizon=config.PROACTIVE_HORIZON,
                                threshold=config.PROACTIVE_THRESHOLD)


def harvest_due(state, status):
    """The dividend: accrued fees go to the wallet every HARVEST_INTERVAL,
    once at least MIN_HARVEST_USD has accrued. Never while a rebalance is
    about to harvest anyway."""
    if not config.HARVEST_INTERVAL:
        return False
    if (status.get('feesAccrued_USD') or 0) < config.MIN_HARVEST_USD:
        return False
    return time.time() - state.get('last_harvest', 0) >= config.HARVEST_INTERVAL


_POOL_REC = {}


def profit_wallet_pinned():
    """The payout destination must match the address pinned in the service
    environment, which a database write cannot change: the chain row names
    the variable (LPBOT_PROFIT_WALLET_PIN on Solana, LPBOT_EVM_PROFIT_WALLET_PIN
    on Base). payout.mjs and the EVM signer check the same pin themselves."""
    pin = os.environ.get(config.CAPS['pin_env'], '')
    # an EVM address is one address in any letter case (EIP-55 checksums)
    return bool(pin) and wallets.norm(pin) == wallets.norm(config.PROFIT_WALLET) and chains.is_address(config.CHAIN, pin)


def pool_record():
    """The held pool's record, cached for an hour (reward programs change)."""
    key = (config.DEX, config.POOL)
    t, rec = _POOL_REC.get(key, (0, None))
    if rec is None or time.time() - t > 3600:
        fresh = dexes.pool(config.DEX, config.POOL)
        if fresh:
            rec = fresh
            _POOL_REC[key] = (time.time(), rec)
    if rec is None:
        raise RuntimeError('pool record unavailable')
    return rec


def pool_tokens():
    """(mint, symbol) of token A and token B of the held pool; an EVM token
    address in lower case (wallets.norm), as every mint the loop compares."""
    rec = pool_record()
    return ((wallets.norm(rec['token_a']['address']), rec['token_a']['symbol']),
            (wallets.norm(rec['token_b']['address']), rec['token_b']['symbol']))


def distribute_rewards(state, position, signatures=()):
    """Reward tokens, after a harvest: every reward mint the pool names (and
    every one it has named before, so a program that just ended is still
    swept), other than the pool's own two tokens and every mint of every
    profile of the wallet. Only what this profile's harvests are shown to
    have brought is paid (txfees.inflow over the harvest's `signatures`,
    carried in state['reward_due'] until paid): the wallet's balance of a
    reward token may be another profile's. Under the 'payout' policy an amount
    worth at least reward_min_usd is swapped through Jupiter (LPBOT_SLEEVE:
    that amount at most) to the payout token and sent to the profit wallet;
    while gas is under the reserve it is swapped to native SOL and kept as
    gas. Never blocks a move."""
    if not config.PAYOUT_ENABLED or config.REWARD_POLICY != 'payout' or not config.CAPS.get('rewards'):
        return None
    rec = pool_record()
    own = {rec['token_a']['address'], rec['token_b']['address']}
    seen = state.setdefault('reward_mints_seen', [])
    for m in rec.get('reward_mints') or []:
        if guards.is_address(m) and m not in seen:
            seen.append(m)
    del seen[:-8]                                       # a bounded memory of programs
    theirs = wallet_mints()
    mints = [m for m in seen if m not in own and m not in theirs and guards.is_address(m)]
    if not mints:
        return None
    bal = wallet(config.POOL)
    arrived = txfees.inflow(config.RPC, signatures, bal.get('owner') or config.WALLET_ADDRESS, mints)
    due = state.setdefault('reward_due', {})
    if arrived is None:
        notify('reward_unmeasured', reason='the harvest transactions are unreadable: rewards wait')
        save(state)
        return None
    for m, v in arrived.items():
        due[m] = float(due.get(m, 0.0)) + v
    gas_low = (bal.get('sol') or 0.0) < config.GAS_RESERVE_SOL
    target = fees.NATIVE_MINT if gas_low else config.PAYOUT_MINT
    if not target:
        return None
    prices = dexes.jupiter_prices(mints)
    done = []
    for m in mints:
        out, err = chain('balance', m, dex='payout')
        amt = min(float((out or {}).get('amount') or 0.0), float(due.get(m, 0.0)))
        usd = amt * prices.get(m, 0.0)
        if amt <= 0 or usd < config.REWARD_MIN_USD:
            continue
        if usd > config.REWARD_MAX_USD:
            # a reward balance worth more than the cap is not swept blind: a
            # wrong price or an unexpected token needs an operator's look
            notify('reward_held', reason=f'${usd:.2f} of {m} exceeds reward_max_usd ${config.REWARD_MAX_USD:.2f}')
            continue
        before, _ = chain('balance', target, dex='payout')
        sw, err = chain('swap', m, target, f'{amt:.9f}', '--execute', dex='jupiter',
                        extra_env={'LPBOT_SLEEVE': json.dumps({m: amt})})
        after, _ = chain('balance', target, dex='payout')
        # Pay what actually arrived, not what the quote promised.
        measured = float((after or {}).get('amount') or 0.0) - float((before or {}).get('amount') or 0.0)
        quoted = float(((sw or {}).get('bought') or {}).get('amount') or 0.0)
        got = min(measured, quoted) if measured > 0 else 0.0
        if err or not (sw or {}).get('signature') or got <= 0:
            notify('reward_swap_failed', reason=err or 'no signature', mint=m, amount=amt)
            continue
        due[m] = max(float(due.get(m, 0.0)) - amt, 0.0)
        if gas_low:
            db.record_payout(config.PROFILE, position, target, 'SOL', got, usd, 'gas',
                             signature=sw['signature'], detail=f'reward {m} swapped for gas')
            done.append({'mint': m, 'usd': round(usd, 4), 'to': 'gas'})
            continue
        tx, err = chain('send', target, f'{got:.9f}', config.PROFIT_WALLET, '--execute', dex='payout')
        if tx and tx.get('signature') and not err:
            db.record_payout(config.PROFILE, position, target, 'reward', got, usd, 'paid',
                             to_address=config.PROFIT_WALLET, signature=tx['signature'],
                             detail=f'reward {m} swapped {sw["signature"]}')
            done.append({'mint': m, 'usd': round(usd, 4), 'to': 'profit wallet', 'signature': tx['signature']})
        else:
            owed = state.setdefault('payout_owed', {})
            owed[target] = float(owed.get(target, 0.0)) + got
            db.record_payout(config.PROFILE, position, target, 'reward', got, usd, 'owed',
                             to_address=config.PROFIT_WALLET, detail=err or 'no signature')
            notify('payout_failed', reason=err or 'no signature', symbol='reward', owed=round(got, 6))
    save(state)
    if done:
        notify('REWARD_PAYOUT', rewards=done, gas_low=gas_low)
    return done


def distribute(state, position, fee_a, fee_b):
    """Split one harvest by the owner's rule (fees.py): payout-token fees to
    the profit wallet now, native SOL to gas while it is under the reserve,
    the rest reinvested. A failed transfer is owed and retried with the next
    harvest; it never blocks a move and never counts toward a halt."""
    if not config.PAYOUT_ENABLED or not (fee_a or fee_b):
        return None
    try:
        (mint_a, sym_a), (mint_b, sym_b) = pool_tokens()
    except Exception as e:
        notify('payout_skipped', reason=f'pool tokens unreadable: {tidy(e)}')
        return None
    bal = wallet(config.POOL)
    if 'balanceA' not in bal:
        notify('payout_skipped', reason='could not read the LP wallet; the fees stay in it')
        return None
    q = quote_price(bal)
    if q is None:
        notify('payout_skipped', reason='the quote token has no USD price; the fees stay in the wallet')
        return None
    px_a, px_b = ui_price(bal) * q, q
    native_fee = fee_a if mint_a == fees.NATIVE_MINT else fee_b if mint_b == fees.NATIVE_MINT else 0.0
    sol_before = (bal.get('sol') or 0.0) - (native_fee or 0.0)
    # The harvest has landed, so the wallet holds every fee it names. A fee
    # larger than the wallet is a bad read, not income: on 2026-09-27 a
    # $6,237 claim on a $230 position put sol_before at -23 SOL and booked
    # $2,830 as gas and $3,408 as reinvested. Split nothing; the fees stay in
    # the wallet.
    if sol_before < 0 or (fee_a or 0.0) > float(bal['balanceA'] or 0.0) + 1e-9 \
            or (fee_b or 0.0) > float(bal['balanceB'] or 0.0) + 1e-9:
        notify('payout_skipped', reason=f'fees {fee_a} {sym_a} + {fee_b} {sym_b} exceed the LP wallet; '
                                        'the fee read is wrong, nothing split')
        return None
    parts = fees.split([(mint_a, sym_a, fee_a, px_a), (mint_b, sym_b, fee_b, px_b)],
                       wallets.norm(config.PAYOUT_MINT), sol_before, config.GAS_RESERVE_SOL)
    owed = state.setdefault('payout_owed', {})
    held = {mint_a: bal['balanceA'], mint_b: bal['balanceB']}
    sent = []
    pin_ok = profit_wallet_pinned()
    for p in parts:
        if p['kind'] != 'paid':
            db.record_payout(config.PROFILE, position, p['mint'], p['symbol'], p['amount'], p['usd'], p['kind'],
                             detail=('gas under the reserve' if p['kind'] == 'gas' else None))
            continue
        due = p['amount'] + float(owed.get(p['mint'], 0.0))
        amt = min(due, float(held.get(p['mint']) or 0.0))
        px = p['usd'] / p['amount'] if p['usd'] is not None and p['amount'] else None
        if amt <= 0:
            continue
        if not pin_ok:
            # The destination in the database does not match the address
            # pinned in the service environment: pay nothing, keep it owed.
            owed[p['mint']] = due
            db.record_payout(config.PROFILE, position, p['mint'], p['symbol'], p['amount'],
                             p['usd'], 'owed', to_address=config.PROFIT_WALLET,
                             detail='profit wallet does not match LPBOT_PROFIT_WALLET_PIN')
            notify('payout_refused', reason='profit_wallet in the database does not match the pinned address',
                   symbol=p['symbol'], owed=round(due, 6))
            continue
        out, err = chain('send', p['mint'], f'{amt:.9f}', config.PROFIT_WALLET, '--execute', dex=route('payout'))
        if out and out.get('signature') and not err:
            rest = due - amt
            if rest > 1e-9:
                owed[p['mint']] = rest          # what the wallet could not cover this time
            else:
                owed.pop(p['mint'], None)
            db.record_payout(config.PROFILE, position, p['mint'], p['symbol'], amt,
                             amt * px if px else None, 'paid', to_address=config.PROFIT_WALLET,
                             signature=out['signature'])
            sent.append({'symbol': p['symbol'], 'amount': round(amt, 6),
                         'usd': round(amt * px, 4) if px else None, 'signature': out['signature']})
        elif (out or {}).get('signature') or (out or {}).get('partial') or \
                re.search(r'timed out|timeout|confirm', str(err), re.I):
            # It may have landed. Never owe it, or the next harvest sends it
            # twice (review, 2026-09-26). Recorded as uncertain for the book.
            owed.pop(p['mint'], None)
            db.record_payout(config.PROFILE, position, p['mint'], p['symbol'], amt,
                             amt * px if px else None, 'uncertain', to_address=config.PROFIT_WALLET,
                             signature=(out or {}).get('signature'), detail=err)
            notify('payout_uncertain', reason=err, symbol=p['symbol'], amount=round(amt, 6),
                   signature=(out or {}).get('signature'))
        else:
            owed[p['mint']] = due
            db.record_payout(config.PROFILE, position, p['mint'], p['symbol'], amt,
                             amt * px if px else None, 'owed', to_address=config.PROFIT_WALLET,
                             detail=err or 'no signature')
            notify('payout_failed', reason=err or 'no signature', symbol=p['symbol'], owed=round(due, 6))
    save(state)
    summary = {k: round(sum((x['usd'] or 0) for x in parts if x['kind'] == k), 4)
               for k in ('paid', 'reinvested', 'gas')}
    notify('PAYOUT', sent=sent, split=summary, gas_low=sol_before < config.GAS_RESERVE_SOL,
           sol_before=round(sol_before, 6), to=config.PROFIT_WALLET,
           parts=[{k: (round(v, 6) if isinstance(v, float) else v) for k, v in x.items()} for x in parts])
    return parts


def measured_fees(out, status, a, b, usd):
    """What the harvest really took out of the pool, from its transactions
    (txfees.py): (a, b, usd). The status figures stand when the transactions
    cannot be read; a disagreement is reported. The status read is an
    estimate made before the harvest; the transaction is what happened.
    On a chain whose transactions txfees cannot read, the status figures
    stand, guarded as always."""
    if not config.CAPS.get('txfees'):
        why = fee_problem(dict(status, feesAccruedA=a, feesAccruedB=b, feesAccrued_USD=usd))
        if why:
            s2 = sanitised(status, why)
            return s2['feesAccruedA'], s2['feesAccruedB'], s2['feesAccrued_USD']
        return a, b, usd
    try:
        (mint_a, _), (mint_b, _) = pool_tokens()
        sigs = (out or {}).get('signatures') or [(out or {}).get('signature')]
        m = txfees.harvested(config.RPC, sigs, status.get('whirlpool') or config.POOL, mint_a, mint_b)
    except Exception as e:
        m = None
        notify('harvest_unmeasured', reason=f'{type(e).__name__}: {tidy(e)}')
    if m is None:
        why = fee_problem(dict(status, feesAccruedA=a, feesAccruedB=b, feesAccrued_USD=usd))
        if why:
            # Neither the transaction nor the status read can be trusted: the
            # last snapshot's figures, a lower bound, are booked instead.
            s2 = sanitised(status, why)
            return s2['feesAccruedA'], s2['feesAccruedB'], s2['feesAccrued_USD']
        notify('harvest_unmeasured', reason='harvest transactions unreadable; the status figures stand')
        return a, b, usd
    # txfees measures raw / 10^decimals; the book, like every amount the
    # signers report, is in UI units (a Token-2022 scaled mint's multiplier).
    ma, mb = m[0] * float(status.get('multiplierA') or 1.0), m[1] * float(status.get('multiplierB') or 1.0)
    q = quote_price(status)
    if q is None:
        # The amounts are the transaction's; their dollar value is not known.
        notify('harvest_measured', reported_usd=usd, measured_a=ma, measured_b=mb,
               reason='quote price unknown: the status dollar figure stands')
        return ma, mb, usd
    musd = (ma * ui_price(status) + mb) * q
    if abs(musd - (usd or 0.0)) > max(0.01, 0.05 * musd):
        notify('harvest_measured', reported_a=a, reported_b=b, reported_usd=usd,
               measured_a=ma, measured_b=mb, measured_usd=round(musd, 6))
    return ma, mb, musd


def band_profile(mint, event, reason=None):
    """The band's lifetime profile (db.record_band_profile), after a harvest
    or the rebalance that ends it. Never blocks a move."""
    try:
        db.record_band_profile(mint, event, reason)
    except Exception as e:
        notify('band_profile_failed', reason=f'{type(e).__name__}: {tidy(e)}', mint=mint, event=event)


def dividend(state, status):
    """Harvest into the wallet and report it. The ledger counts it once: the
    snapshot after the harvest records the position's counter at zero."""
    mint = status['positionMint']
    a, b, usd = status.get('feesAccruedA', 0.0), status.get('feesAccruedB', 0.0), status.get('feesAccrued_USD', 0.0)
    out, err = chain('harvest', mint, '--execute')
    state['last_harvest'] = time.time(); save(state)
    if held(err):
        return False                # said once by chain(); the next interval tries again
    if out and out.get('signature') and not err:
        a, b, usd = measured_fees(out, status, a, b, usd)
        db.record_harvest(mint, a, b, usd, out['signature'])
        db.snapshot(mint, ui_price(status), status.get('inRange'), status.get('liquidity'),
                    0.0, 0.0, 0.0, wallet(status['whirlpool']).get('walletUsd'), position_usd(status))
        db.event('DIVIDEND', f'${usd:.4f} harvested to the wallet')
        band_profile(mint, 'harvest')
        notify_book('DIVIDEND', collected_usd=round(usd, 4), collected_a=a, collected_b=b,
                    signature=out['signature'])
        try:
            distribute(state, mint, a, b)
            distribute_rewards(state, mint, signatures_of(out))
        except Exception as e:          # the split must never stop the loop
            notify('payout_failed', reason=f'{type(e).__name__}: {tidy(e)}')
        return True
    notify('harvest_skipped', reason=err or 'no signature returned', kind='dividend')
    return False


# --- calm mode: a tight band while the market is cold ---------------------------

_TAPE5 = {}                     # pool -> (fetched_at, bars)
LAST_CALM = {}                  # {'view': the latest calm.view}, for every book
TAPE5_REFRESH = 240             # one GeckoTerminal call per five-minute bar, at most


def tape_bars():
    return max(288, int(config.REGIME_TAPE_DAYS) * 288)


def _merge(bars_list):
    """Union of five-minute bar arrays, by timestamp, newest wins, oldest
    first, trimmed to the window."""
    rows = {}
    for b in bars_list:
        if b is None:
            continue
        for row in zip(*b):
            rows[int(row[0])] = row
    ks = sorted(rows)[-tape_bars():]
    if not ks:
        return None
    cols = list(zip(*[rows[k] for k in ks]))
    return tuple(np.array(c, dtype=float) for c in cols)


def tape5(pool, price, pair=None):
    """The pool's five-minute tape, `regime_tape_days` deep, from the
    database. Each refresh fetches the newest 1000 bars, pages back while the
    window is short, stores the new bars and deletes those older than the
    window. Memory holds this pool's window only (plus at most one other
    pool, for a move in progress). A 3.5-day tape made the width choice
    noisy and churned 6 to 7 moves a day in the replay."""
    t, b = _TAPE5.get(pool, (0, None))
    if b is not None and time.time() - t <= TAPE5_REFRESH:
        return with_surrogate(pool, b, price, pair)
    window_s = tape_bars() * calm.BAR_SECONDS
    if b is None:
        try:
            # an hour of slack: the newest stored bar can trail the clock, and
            # _merge trims to the window anyway
            b = db.tape_load(pool, time.time() - window_s - 3600)
        except Exception:
            b = None
        if b is not None and abs(b[4][-1] / price - 1) > 0.15:
            b = None                           # another orientation or stale rows: rebuild
    scale = UI_SCALE.get(pool, 1.0)
    try:
        fresh = native_bars(calm.tape_5m(pool, live_price=price / scale), scale)
    except Exception:
        fresh = None
    merged = _merge([b, fresh])
    tries = 0
    pages = int(np.ceil(tape_bars() / 1000)) + 1
    while merged is not None and len(merged[0]) < tape_bars() and tries < pages:
        try:
            # The orientation of a historical page is checked against the bar
            # it joins (the oldest one held), not today's price: SOL moved
            # more than 15% in a month, and the check threw every older page
            # away, capping the tape at ten days.
            older = native_bars(calm.tape_5m(pool, live_price=float(merged[4][0]) / scale,
                                             before=float(merged[0][0])), scale)
        except Exception:
            older = None
        if older is None:
            break
        n0 = len(merged[0]); merged = _merge([older, merged]); tries += 1
        if len(merged[0]) == n0:
            break
    if merged is None:
        return with_surrogate(pool, b, price, pair)
    if fresh is not None:
        try:
            db.tape_store(pool, merged, merged[0][-1] - window_s)
            # Pools left a day ago. Every profile's pool stays: each process
            # prunes, and keeping only its own cut the others' tapes to a day
            # (2026-10-02: mu-usdc's prune left sol-usdc 287 of 8640 bars,
            # so a restart would decide on a one-day tape).
            db.tape_prune_other_pools(db.config_pools() | {pool}, time.time() - 86400)
        except Exception:
            pass
        # memory: this pool, and at most one other
        for other in [p for p in _TAPE5 if p != pool][:-1]:
            _TAPE5.pop(other, None)
        _TAPE5[pool] = (time.time(), merged)
    return with_surrogate(pool, merged, price, pair)


_SURR = {}                      # pool -> (asked_at, name, surrogate bars); one pool
LAST_SURROGATE = {}             # pool -> tape_source(): where the last hour's bars came from
SURROGATE_LOOKBACK_S = 86400    # a slot GeckoTerminal lacks in the last day is filled
SURROGATE_REFRESH = TAPE5_REFRESH


def tape_source(ts, filled_ts, now, name, fresh, quiet_ts=()):
    """Where the last hour's bars came from, for the book: 'Gecko', 'Binance'
    (every bar of the hour), 'Gecko+Binance', or 'none' when the tape is
    stale. Pure."""
    hour = [t for t in (ts if ts is not None else []) if t >= now - 3600 - calm.BAR_SECONDS]
    filled = set(int(t) for t in filled_ts)
    n_s = sum(1 for t in hour if int(t) in filled)
    label = ('none' if not fresh else 'Gecko' if n_s == 0 else name if n_s == len(hour) else f'Gecko+{name}')
    quiet = set(int(t) for t in quiet_ts)
    return {'source': label, 'surrogate': name if filled else None,
            'filled_1h': n_s, 'bars_1h': len(hour), 'filled_24h': len(filled),
            'quiet_1h': sum(1 for t in hour if int(t) in quiet)}


_QUIET_MISMATCH = {}              # pool -> when the live price first left the last close
_QUIET_REF = {}                   # {'at', 'pool', 'ts'}: the canary pool's bar times, cached
QUIET_REF_REFRESH = 60


def quiet_ref_ts(pool, now):
    """Bar times of the last QUIET_HISTORY_S of the reference pool, the one
    pool that trades every slot (a native/stable profile's: sol-usdc's), from
    the database; None for that pool itself, when there is none, or on a
    failed read. Cached QUIET_REF_REFRESH seconds."""
    c = _QUIET_REF
    if c and c.get('for') == pool and now - c['at'] <= QUIET_REF_REFRESH:
        return c['ts']
    try:
        ref = db.tape_ref_pool(config.CAPS['native_mint'], sorted(engine.STABLE_MINTS), pool)
        got = db.tape_load(ref, now - calm.QUIET_HISTORY_S) if ref else None
        ts = got[0] if got is not None else None
    except Exception:
        ts = None
    _QUIET_REF.clear(); _QUIET_REF.update({'at': now, 'for': pool, 'ts': ts})
    return ts


def with_surrogate(pool, bars, price, pair=None):
    """_with_surrogate's tape with the slots a quiet pool did not trade in
    filled flat at the last close (calm.quiet_fill): GeckoTerminal emits no
    bar for a slot without a swap, and a thin pool (MU/USDC) read as STALE
    91.6% of the time. Flat bars are never stored; a GeckoTerminal or a
    surrogate bar always wins over one. The last hour's flat bars are
    `quiet_1h` in LAST_SURROGATE. Any failure returns the tape unfilled."""
    out = _with_surrogate(pool, bars, price, pair)
    if out is None:
        return None
    now = time.time()
    try:
        ok, _QUIET_MISMATCH[pool] = calm.quiet_tail_ok(price, float(out[4][-1]), _QUIET_MISMATCH.get(pool), now)
        window_s = tape_bars() * calm.BAR_SECONDS
        q = calm.quiet_fill(out[0], out[4], now, quiet_ref_ts(pool, now), ok, window_s)
        if q is None:
            return out
        merged = _merge_all([q, out])                         # the later array wins: the real bars
        keep = merged[0] >= merged[0][-1] - window_s
        merged = tuple(c[keep] for c in merged)
        src = LAST_SURROGATE.get(pool) or {}
        LAST_SURROGATE[pool] = dict(tape_source(merged[0], _surrogate_ts(src, out, bars), now, src.get('surrogate'),
                                                calm.tape_fresh(merged[0], now), q[0]))
        return merged
    except Exception as e:
        print(f'quiet fill failed: {type(e).__name__}: {e}', flush=True)
        return out


def _surrogate_ts(src, out, bars):
    """The bar times of `out` that are not GeckoTerminal's `bars`: the surrogate's."""
    gecko = set(int(t) for t in bars[0])
    return [t for t in out[0] if int(t) not in gecko]


def _merge_all(bars_list):
    """_merge without its trim to tape_bars(): union by timestamp, the later
    array winning, oldest first."""
    rows = {}
    for b in bars_list:
        for row in zip(*b):
            rows[int(row[0])] = row
    cols = list(zip(*[rows[k] for k in sorted(rows)]))
    return tuple(np.array(c, dtype=float) for c in cols)


def _with_surrogate(pool, bars, price, pair=None):
    """The GeckoTerminal tape with the slots it lacks in the last day filled
    from a surrogate (calm.surrogate_5m). GeckoTerminal wins wherever it has a
    bar; surrogate bars are never stored, so a late GeckoTerminal bar replaces
    them and the tape is GeckoTerminal's again as soon as it is complete. The
    surrogate is asked only while a slot is missing and not already filled,
    at most once per SURROGATE_REFRESH; a failed ask keeps the bars an earlier
    ask gave. Any failure returns the GeckoTerminal tape as it is."""
    if bars is None:
        return None
    now = time.time()
    try:
        pair = pair or (config.PAIR_LABEL if pool == config.POOL else None)
        gaps = calm.missing_slots(bars[0], now, SURROGATE_LOOKBACK_S)
        if not gaps:
            _SURR.pop(pool, None)
            LAST_SURROGATE[pool] = tape_source(bars[0], [], now, None, calm.tape_fresh(bars[0], now))
            return bars
        t, name, s = _SURR.get(pool, (0, None, None))
        have = set(int(x) for x in s[0]) if s is not None else set()
        if any(g not in have for g in gaps) and now - t > SURROGATE_REFRESH:
            got_name, got = calm.surrogate_5m(pair, gaps[0], price, ref=bars)
            if got is not None:
                name, s = got_name, _merge([s, got])
                s = tuple(c[s[0] >= now - SURROGATE_LOOKBACK_S - 3600] for c in s)   # the last day only
            for other in [p for p in _SURR if p != pool]:
                _SURR.pop(other, None)
            _SURR[pool] = (now, name, s)
        fill = None
        if s is not None:
            want = set(gaps)
            keep = np.array([int(x) in want for x in s[0]], dtype=bool)
            fill = tuple(c[keep] for c in s) if keep.any() else None
        out = _merge([fill, bars]) if fill is not None else bars    # the later array wins: GeckoTerminal
        LAST_SURROGATE[pool] = tape_source(out[0], fill[0] if fill is not None else [], now, name,
                                           calm.tape_fresh(out[0], now))
        return out
    except Exception as e:
        print(f'surrogate tape failed: {type(e).__name__}: {e}', flush=True)
        return bars


IDLE_MIN_AGE_S = 600              # an open this recent may still be settling
IDLE_DEPLOYS_PER_DAY = 3          # re-centres to deploy idle money, at most, in 24 h


def idle_deploys_left(times, now):
    """Idle-deploy re-centres still allowed in the 24 h before `now`. Pure."""
    return max(0, IDLE_DEPLOYS_PER_DAY - sum(1 for t in (times or []) if now - t < 86400))


def idle_to_deploy(deployable_usd, equity_usd, seconds_since_open):
    """Whether money idle beside an open position is worth a re-centre that
    deploys it: more than max($2, 2% of equity), the audit's idle limit, and
    the band at least IDLE_MIN_AGE_S old. A cycle costs about $0.012
    (lp-cost-structure); $22 idle forgoes about $0.20 a day of fees."""
    if seconds_since_open is None or seconds_since_open < IDLE_MIN_AGE_S:
        return False
    return deployable_usd > max(audit.IDLE_ABS_USD, audit.IDLE_SHARE * (equity_usd or 0.0))


def deploy_idle(state, status, wbal, rv, price):
    """Deploy-all: money idle beside the band (a swap that failed before an
    open, a leftover) goes in at the next allowed move, a re-centre at the
    regime's width, which swaps the wallet to 50/50 first. True when it moved."""
    if 'balanceA' not in wbal or wbal.get('walletUsd') is None:
        return False
    opened = db.position_opened(status['positionMint'])
    age = (datetime.now(timezone.utc) - opened).total_seconds() if opened else None
    idle = deployable_usd(wbal)
    if idle is None:
        return False                                     # quote price unknown: nothing is valued
    equity = float(wbal['walletUsd']) + (position_usd(status) or 0.0)
    # What a balanced open leaves out is its price tolerance, by design: in a
    # narrow band the deposit ratio moves ~80x faster than the price, so a
    # 7.5 bp tolerance leaves ~6% of one side (2026-09-28: $13 of $234). The
    # first reading of a band after such an open is that leftover; only new
    # money beyond it (a deposit, a sweep) is deployed. After an open that
    # skipped its swap, nothing is excused.
    base = state.get('idle_baseline') or {}
    if base.get('mint') != status['positionMint']:
        excused = 0.0 if state.get('open_unbalanced') else idle
        state['idle_baseline'] = {'mint': status['positionMint'], 'usd': round(excused, 4)}; save(state)
        base = state['idle_baseline']
    idle_new = idle - float(base['usd'])                 # always written as a number above
    if not (idle_to_deploy(idle_new, equity, age) and calm_budget_left(state) > 0 and voluntary_move_allowed(state)):
        return False
    # The re-centre deploys idle money only through its swap: while swaps
    # fail (the 'swap' breaker, exponential backoff, health.py), it would
    # close and reopen lopsided again, and spend the move budget the regime
    # needs (2026-09-30: every ~11 minutes). At most a few a day.
    now = time.time()
    ok, _st, wait, rec = health.allowed('swap', now)
    left = idle_deploys_left(state.get('idle_deploys'), now)
    if not ok or left <= 0:
        told = state.get('idle_deferred_told')
        key = f"{rec.get('last_fail')}:{left}"
        if told != key:
            state['idle_deferred_told'] = key; save(state)
            why = (f"the swap failed {rec.get('fails')}x in a row: next try in {wait / 60:.0f} min"
                   if not ok else f'{IDLE_DEPLOYS_PER_DAY} idle deploys in 24 h already')
            notify('deploy_idle_deferred', idle_usd=round(idle, 2), reason=why)
            db.event('deploy_idle_deferred', f'${idle:.2f} idle: {why}')
        return False
    state['idle_deploys'] = [t for t in (state.get('idle_deploys') or []) if now - t < 86400] + [now]; save(state)
    k = rv['choice'] if rv else math.sqrt(status['upperPrice'] / status['lowerPrice'])
    notify_book('DEPLOY_IDLE', idle_usd=round(idle, 2), price=price, lower=status['lowerPrice'],
                upper=status['upperPrice'])
    db.event('DEPLOY_IDLE', f'${idle:.2f} idle beside the band: re-centre to deploy it')
    rebalance(state, status, f'deploy ${idle:.2f} idle', band=k, calm_move=True)
    return True


SWEEP_EVERY_S = 600               # a token-account read at most every ten minutes
SWEEP_MIN_USD = 1.0               # smaller balances are dust: a swap would cost more than it moves


def plan_sweep(accounts, pool_mints, reward_mints, prices, facts):
    """Which foreign tokens in the LP wallet to convert into the pool's
    tokens, so they can be deployed (owner, 2026-09-28: "every idle capital
    should be put on the LP"). Pure. `accounts` are audit.token_accounts rows;
    `prices` USD per mint; `facts` Jupiter's token facts per mint. Never: the
    pool's own tokens, reward tokens (paid out by distribute_rewards), wrapped
    SOL, position NFTs, unverified tokens (airdropped spam), or balances under
    SWEEP_MIN_USD."""
    plan = []
    for a in accounts:
        m = a['mint']
        if a['amount'] <= 0 or m in pool_mints or m in reward_mints or m == fees.NATIVE_MINT:
            continue
        if a['decimals'] == 0 and a['amount'] == 1:
            continue                                   # a position NFT
        human = audit.human(a)                          # UI units: a scaled mint's multiplier applied
        usd = human * float(prices.get(m) or 0.0)
        if usd < SWEEP_MIN_USD or not (facts.get(m) or {}).get('verified'):
            continue
        plan.append({'mint': m, 'amount': human, 'usd': round(usd, 4),
                     'symbol': (facts.get(m) or {}).get('symbol')})
    return plan


def wallet_mints():
    """Every mint of every profile of this wallet, disabled ones too (their pools'
    tokens and deposit mints): what the sweep must not sell and the janitor
    must not close. Empty without a wallet. Raises when the table cannot be
    read: a chore that cannot tell whose a token is does not run."""
    if not config.WALLET_ID:
        return set()
    out = set()
    for p in wallets.wallet_profiles(config.WALLET_ID):
        out |= {wallets.norm(m) for m in (p.get('mints') or []) + [p.get('deposit_mint')] if m}
    return out


def sweep_foreign(state, bal):
    """Convert foreign tokens in the LP wallet into the pool's quote token
    (plan_sweep); deploy_idle then puts them in the band. Wallet-wide: the
    residual owner's chore only, and never a token any profile of the wallet
    uses. Never blocks the loop. Returns the swaps made."""
    if not housekeeper('sweep'):
        return []
    if time.time() - state.get('last_sweep', 0) < SWEEP_EVERY_S:
        return []
    state['last_sweep'] = time.time(); save(state)
    try:
        owner = bal.get('owner')
        if not owner:
            return []
        (mint_a, _), (mint_b, _) = pool_tokens()
        mine = {mint_a, mint_b} | wallet_mints()
        accounts = audit.token_accounts(config.RPC, owner)
        rewards = set(state.get('reward_mints_seen') or [])
        others = [a['mint'] for a in accounts if a['amount'] > 0 and a['mint'] not in mine]
        if not others:
            return []
        prices = dexes.jupiter_prices(others)
        # Facts only for what is worth a sweep: dust is never swapped, so its
        # token search is wasted Jupiter budget (2026-10-01). Valued in UI
        # units, so a scaled mint's multiplier counts (audit.human).
        worth = {a['mint'] for a in accounts if a['mint'] in prices
                 and audit.human(a) * float(prices[a['mint']] or 0.0) >= SWEEP_MIN_USD}
        facts = {m: dexes.jupiter_token(m) for m in others if m in worth}
        plan = plan_sweep(accounts, mine, rewards, prices, facts)
        target = mint_b if mint_b != fees.NATIVE_MINT else mint_a
        done = []
        for p in plan:
            sw, err = chain('swap', p['mint'], target, f"{p['amount']:.9f}", '--execute', dex='jupiter')
            if err or not (sw or {}).get('signature'):
                notify('sweep_failed', reason=err or 'no signature', mint=p['mint'], usd=p['usd'])
                continue
            db.event('SWEEP', f"{p['amount']} {p['symbol'] or p['mint']} (${p['usd']:.2f}) swapped to the pool, "
                              f"{sw['signature']}")
            done.append({**p, 'signature': sw['signature']})
        if done:
            notify('SWEEP', swept=done, total_usd=round(sum(d['usd'] for d in done), 2))
        return done
    except Exception as e:
        notify('sweep_failed', reason=f'{type(e).__name__}: {tidy(e)}')
        return []


def voluntary_move_allowed(state):
    """Whether a calm or regime move would pass rebalance()'s gap now, and
    the held venue's breaker allows its transactions (a venue in backoff gets
    no voluntary move; exits and failover still run). The loop asks first,
    so a move held back is not announced on every poll (review, 2026-09-26:
    up to five duplicate messages a move)."""
    now = time.time()
    recent = [t for t in state.get('calm_times', []) if now - t < 86400]
    last_any = max([state.get('last_rebalance', 0)] + recent)
    return now - last_any >= config.CALM_MIN_GAP and health.allowed(f'venue:{config.DEX}', now)[0]


FAILOVER_MIN_RATIO = 0.8          # a failover target earns at least this share of the held venue


def failover_pick(held_dex, venues, allowed, *, execute_dexes, signers, min_hours, min_ratio=FAILOVER_MIN_RATIO):
    """The venue to fail over to when `held_dex` is tripped, or None. Pure.
    `venues` is venue_view(); `allowed(dex)` says whether that venue's
    breaker allows it. A target has on-chain evidence (min_hours), an armed
    signer, a breaker that allows it, and at least `min_ratio` of the held
    venue's on-chain income ("similar fee goodness"); the best such wins.
    Without evidence on the held venue, any eligible venue qualifies."""
    held = next((v for v in venues if v.get('held')), None)
    floor = (held.get('total_pct_day') or 0.0) * min_ratio if held and (held.get('hours') or 0) >= min_hours else None
    ok = [v for v in venues
          if not v.get('held') and v.get('dex') != held_dex and v.get('row')
          and (v.get('hours') or 0) >= min_hours and v.get('dex') in execute_dexes and v.get('dex') in signers
          and allowed(v['dex']) and (floor is None or (v.get('total_pct_day') or 0.0) >= floor)]
    return max(ok, key=lambda v: v.get('total_pct_day') or 0.0) if ok else None


def venue_failover(state, status, price=None, quote=None):
    """Fail over when the held venue's breaker is tripped (TRIP_FAILS
    failures of its own transactions in a row): move to the best venue with
    similar on-chain income. With a position, a rebalance to the target
    (its close is one more probe of the tripped venue); without one, repoint
    and reopen there. The old venue cools down; the normal pool review can
    bring the bot back once it earns more again. True when a move started."""
    key = f'venue:{config.DEX}'
    _ok, st, wait, rec = health.allowed(key)
    if int(rec.get('fails') or 0) < health.TRIP_FAILS:    # failover follows the count, not the light
        return False
    told = state.get('failover_told')
    tell = told != rec.get('last_fail')
    if config.POOL_PINNED:
        if tell:
            state['failover_told'] = rec.get('last_fail'); save(state)
            notify('failover_none', venue=config.DEX, reason='pool pinned: no failover', fails=rec.get('fails'))
        return False
    p = price if price is not None else (status or {}).get('price')
    q = status.get('quoteUsd') if status else quote
    if not p or q is None:
        return False                    # income cannot be compared in dollars: no move on a guess
    try:
        venues = venue_view(p, q)
    except Exception as e:
        venues = []
        notify('venue_sample_failed', reason=tidy(e))
    target = failover_pick(config.DEX, venues, lambda d: health.allowed(f'venue:{d}')[0],
                           execute_dexes=config.EXECUTE_DEXES, signers=SIGNERS, min_hours=config.VENUE_MIN_HOURS)
    if not target:
        if tell:
            state['failover_told'] = rec.get('last_fail'); save(state)
            notify('failover_none', venue=config.DEX, fails=rec.get('fails'), retry_in_min=round(wait / 60),
                   reason=f'no venue with {FAILOVER_MIN_RATIO:.0%} of the income, evidence and a healthy breaker; '
                          f'backing off on {config.DEX}')
            db.event('failover_none', f"{config.DEX} tripped ({rec.get('fails')}x: {rec.get('last_error')}); no target")
        return False
    row = target['row']
    state['failover_told'] = rec.get('last_fail'); save(state)
    what = (f"{config.DEX} tripped after {rec.get('fails')} failures ({rec.get('last_error')}) -> "
            f"{target['dex']} {target['total_pct_day']:.2f}%/d on chain")
    notify('FAILOVER', venue=config.DEX, to=target['dex'], pool=row['address'], fails=rec.get('fails'),
           last_error=rec.get('last_error'), total_pct_day=target['total_pct_day'])
    db.event('FAILOVER', what)
    k = (regime_choice_now(row['address'], p, row.get('pair')) or config.REGIME_WIDTHS[-1]) \
        if config.REGIME_ENABLED else None
    if status and status.get('positionMint'):
        rebalance(state, status, f'failover: {what}', target=row, band=k, calm_move=True)
        return True
    repoint(row)
    pending = state.get('pending_reopen')
    if pending:
        # The intent's funds move with the venue: the failover is the reason.
        pending.update(pool=config.POOL, dex=config.DEX); save(state)
    reopen(state, f'failover: {what}', band=k)
    return True


def calm_budget_left(state):
    now = time.time()
    used = [t for t in state.get('calm_times', []) if now - t < 86400]
    return max(config.CALM_MAX_MOVES - len(used), 0)


def calm_view(state, status):
    """calm.view for the held position, or None when calm mode is off or the
    five-minute tape is unavailable. Remembers the calm state across polls,
    which the hysteresis needs."""
    if not config.CALM_ENABLED:
        return None
    bars = tape5(status.get('whirlpool') or config.POOL, status['price'])
    v = calm.view(bars, status['price'], status['lowerPrice'], status['upperPrice'],
                  was_calm=bool(state.get('calm')), cut=config.CALM_SIGMA_CUT,
                  exit_mult=config.CALM_EXIT_MULT, band=config.CALM_BAND,
                  horizon_minutes=config.CALM_HORIZON_MINUTES, threshold=config.CALM_THRESHOLD)
    if v:
        if bool(state.get('calm')) != v['calm']:
            state['calm'] = v['calm']; save(state)
            db.event('CALM_ON' if v['calm'] else 'CALM_OFF',
                     f"sigma {v['sigma_5m_pct']}% vs cut {v['cut_pct']}%")
        v['budget_left'] = calm_budget_left(state)
        v['moves_24h'] = config.CALM_MAX_MOVES - v['budget_left']
        LAST_CALM['view'] = v
    return v


LAST_REGIME = {}                # {'view': the latest calm.regime_view}, for every book
REGIME_UNSTALE_S = 900          # a tape back from STALE must stay fresh this long


def track_tape_source(state, src):
    """An event and a Telegram line when the tape's source changes between
    GeckoTerminal only, GeckoTerminal with a surrogate, and none (stale).
    'Binance' and 'Gecko+Binance' are one state here, so bars that come and
    go one at a time do not repeat the message."""
    kind = 'none' if src['source'] == 'none' else 'surrogate' if src['filled_1h'] else 'gecko'
    was = state.get('tape_source_kind')
    if was == kind:
        return
    state['tape_source_kind'] = kind; save(state)
    if was is None and kind == 'gecko':
        return                                        # first poll, all normal: nothing to say
    detail = (f"{src['source']}: {src['surrogate']} fills {src['filled_1h']} of {src['bars_1h']} bars "
              f"in the last hour (GeckoTerminal lacks them)" if kind == 'surrogate'
              else 'none: GeckoTerminal and every surrogate lack recent bars (STALE)' if kind == 'none'
              else 'Gecko: GeckoTerminal complete again, surrogate off')
    db.event('TAPE_SOURCE', detail)
    notify('TAPE_SOURCE', source=src['source'], was=was, kind=kind, detail=detail)


def regime_view(state, status):
    """calm.regime_view for the held position, or None when regime mode is
    off or the five-minute tape is unavailable."""
    if not config.REGIME_ENABLED:
        return None
    pool = status.get('whirlpool') or config.POOL
    bars = tape5(pool, status['price'])
    if bars is None:
        return None
    lq = liquidity_view(pool, config.DEX, bars)
    theta = min(max(config.REGIME_THRESHOLD * lq['factor'], 0.05), 0.40)
    v = calm.regime_view(bars, status['price'], status['lowerPrice'], status['upperPrice'],
                         widths=config.REGIME_WIDTHS, horizon_minutes=config.REGIME_HORIZON,
                         threshold=theta, guard=guard_config(pool, LAST_SURROGATE.get(pool)))
    raw_fresh = fresh = calm.tape_fresh(bars[0], time.time())
    hold_left = 0
    if fresh:
        # A tape that just came back must stay complete REGIME_UNSTALE_S
        # before the bot leaves STALE: sources that flicker cannot flip the
        # band between the widest width and a tight one.
        if state.get('regime_mode') == 'STALE':
            since = state.get('tape_fresh_since')
            if since is None:
                state['tape_fresh_since'] = since = time.time(); save(state)
            hold_left = max(0, int(REGIME_UNSTALE_S - (time.time() - since)))
            fresh = hold_left == 0
    elif state.get('tape_fresh_since') is not None:
        state['tape_fresh_since'] = None; save(state)
    if v and not fresh:
        # The newest bar is old, or the last half hour has a gap (a data
        # outage of GeckoTerminal and Binance both): the tape no longer
        # describes the market. Choose the widest width, so an exit never
        # reopens tight into a market nobody is measuring, and narrowing
        # cannot happen (review, 2026-09-26: a 6-hour-old calm tape chose
        # +/-1%; 2026-09-29: a lone bar after a 25-minute gap passed the
        # age check and the bot narrowed, then widened again).
        v = dict(v, choice=config.REGIME_WIDTHS[-1],
                 choice_pct=round((config.REGIME_WIDTHS[-1] - 1) * 100, 2), mode='STALE', stale=True,
                 unstale_in_s=hold_left if raw_fresh else None)
    if v:
        v['threshold_base'] = config.REGIME_THRESHOLD
        v['steps'] = config.REGIME_STEPS
        v['liquidity'] = lq
        LAST_REGIME['risk'] = calm.risk_metrics(bars)
        v['moves_24h'] = config.CALM_MAX_MOVES - calm_budget_left(state)
        v['guard'] = config.CALM_MAX_MOVES
        src = dict(LAST_SURROGATE.get(pool) or tape_source(bars[0], [], time.time(), None, raw_fresh))
        if not raw_fresh:
            src['source'] = 'none'
        v['data'] = src
        track_tape_source(state, src)
        if state.get('regime_mode') != v['mode']:
            db.event('REGIME', f"{state.get('regime_mode')} -> {v['mode']}: choice +/-{v['choice_pct']}% "
                               f"sigma {v['sigma_5m_pct']}% velocity {v['velocity']}")
            state['regime_mode'] = v['mode']; save(state)
        LAST_REGIME['view'] = v
    return v


_LIQ = {}                       # pool -> (fetched_at, record)
LIQ_REFRESH = 300


def liquidity_view(pool, dex, bars):
    """Liquidity inflow against its norm, from the pool's own record:

      inflow  = active liquidity now / its 24-hour median (our share of fees
                falls when liquidity floods in; the risk of a touch does not)
      factor  = clamp(1 / inflow, regime_liq_min, regime_liq_max)
      volume  = last 6 hours of 5-minute volume / the tape's median 6h, and
      tvl_change_24h, both reported for the book only

    The touch threshold is multiplied by `factor`. Bounded, and neutral (1.0)
    when any input is missing, because no liquidity history exists to fit it.
    Records a reading at most every LIQ_REFRESH seconds."""
    out = {'factor': 1.0, 'inflow': None, 'volume_x': None, 'tvl_change_24h': None,
           'liquidity': None, 'tvl_usd': None, 'readings': 0}
    t, rec = _LIQ.get(pool, (0, None))
    if rec is None or time.time() - t > LIQ_REFRESH:
        try:
            fresh = dexes.pool(dex, pool)
        except Exception:
            fresh = None
        if fresh:
            liq = fresh.get('liquidity') or fresh.get('active_bin_usd')
            try:
                db.record_pool_stats(dex, pool, liq, fresh.get('tvl_usd'), fresh.get('volume_24h_usd'),
                                     fresh.get('price'))
            except Exception:
                pass
            rec = fresh
            _LIQ[pool] = (time.time(), rec)
    if rec:
        out['liquidity'] = rec.get('liquidity') or rec.get('active_bin_usd')
        out['tvl_usd'] = rec.get('tvl_usd')
    try:
        summ = db.pool_stats_summary(pool)
    except Exception:
        summ = None
    if summ and out['liquidity'] and summ['median_liquidity'] > 0:
        out['inflow'] = round(float(out['liquidity']) / summ['median_liquidity'], 3)
        out['readings'] = summ['readings']
        if summ.get('tvl_then') and out['tvl_usd']:
            out['tvl_change_24h'] = round(float(out['tvl_usd']) / summ['tvl_then'] - 1, 4)
    if bars is not None and len(bars[5]) >= 288:
        v = np.asarray(bars[5], dtype=float)
        w = 72                                   # six hours of five-minute bars
        recent = float(v[-w:].sum())
        sums = np.convolve(v, np.ones(w), mode='valid')
        med = float(np.median(sums)) if len(sums) else 0.0
        if med > 0:
            out['volume_x'] = round(recent / med, 3)
    if out['inflow']:
        # Liquidity only. Volume is shown but does not scale the budget: it
        # falls every night and weekend, when the market is calm, and the
        # survival estimate already carries the variance that moves with it
        # (weekend study, 2026-09-26: volume per unit variance is unchanged).
        raw = 1.0 / out['inflow']
        out['factor'] = round(min(max(raw, config.REGIME_LIQ_MIN), config.REGIME_LIQ_MAX), 3)
    return out


FORECAST_SAMPLE_S = 600         # one touch forecast recorded per ten minutes


def track_touch_forecasts(state, rv, status):
    """Record the regime's forecast (every width, centred on the price) at
    most every FORECAST_SAMPLE_S, and resolve the ones whose horizon has
    passed. Stale views are not recorded: they are not forecasts."""
    if not rv or rv.get('stale') or not rv.get('probs'):
        return
    pool = status.get('whirlpool') or config.POOL
    try:
        if time.time() - state.get('last_touch_forecast', 0) >= FORECAST_SAMPLE_S:
            state['last_touch_forecast'] = time.time(); save(state)
            db.record_touch_forecast(pool, status['price'], rv['horizon_minutes'], rv['threshold'],
                                     rv['choice'], rv['probs'])
            db.resolve_touch_forecasts(pool)
        if time.time() - state.get('last_touch_calibration', 0) >= 3600:
            state['last_touch_calibration'] = time.time(); save(state)
            LAST_REGIME['calibration'] = db.touch_calibration(7, pool)
    except Exception as e:
        notify('forecast_track_failed', reason=tidy(e))
    if LAST_REGIME.get('calibration'):
        rv['calibration'] = LAST_REGIME['calibration']


def record_risk(status, rv, fc):
    """The risk profile of this poll, to rebalancer.risk_profile. Never
    blocks the loop: a failure is reported and the poll goes on."""
    if not rv:
        return
    try:
        db.record_risk_profile(status.get('whirlpool') or config.POOL, status.get('positionMint'),
                               status['price'], rv, LAST_REGIME.get('risk'), fc)
    except Exception as e:
        notify('risk_record_failed', reason=f'{type(e).__name__}: {tidy(e)}')


def daily_report(state):
    """Once per UTC day, after it closes: the day's line of the book (re-
    centres, fees, value against a 50/50 hold) to the feed and the events
    table. Never blocks the loop."""
    try:
        today = datetime.now(timezone.utc).date()
        day = today - dt.timedelta(days=1)
        if state.get('last_daily') == day.isoformat():
            return None
        line = db.daily_line(day)
        state['last_daily'] = day.isoformat(); save(state)
        if not line:
            return None
        notify('DAILY', **line)
        vs = line['vs_hold_usd']                # None on a day across pairs (sol-swing)
        db.event('DAILY', f"{line['day']}: {line['recentres']} re-centres, fees ${line['fees_usd']:.2f}, "
                          f"vs 50/50 hold {'-' if vs is None else f'{vs:+.2f}'}")
        return line
    except Exception as e:
        notify('daily_report_failed', reason=f'{type(e).__name__}: {tidy(e)}')
        return None


def portfolio_report(state):
    """Once per UTC day: every wallet's active pools, their subtotals and
    the TOTAL (stats.portfolio) as one PORTFOLIO row. Sent by one process
    only: the residual owner of the first wallet by id. Never blocks the
    loop."""
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        if not (config.WALLET_ID and config.RESIDUAL_OWNER) or state.get('last_portfolio') == today:
            return None
        ids = sorted(w['id'] for w in db.wallets())
        if not ids or ids[0] != config.WALLET_ID:
            return None
        state['last_portfolio'] = today; save(state)
        p = stats.portfolio()
        notify('PORTFOLIO', **p)
        return p
    except Exception as e:
        notify('portfolio_failed', reason=f'{type(e).__name__}: {tidy(e)}')
        return None


AUDIT_EVERY_S = 3600


def wallet_book():
    """The wallet as audit.run reconciles it: every profile of the wallet
    (a disabled one's position and tokens are still in the wallet), the mints they use,
    each one's pool mints, and the claims. None without a wallet: the ledger
    is one book."""
    if not config.WALLET_ID:
        return None
    rows = [{'name': r['name'], 'mints': list(r['mints']) if r.get('mints') else None,
             'deposit_mint': r.get('deposit_mint'), 'residual_owner': r.get('residual_owner'),
             'enabled': r.get('enabled')} for r in db.profiles(config.WALLET_ID, enabled_only=False)]
    return {'profiles': rows,
            'mints': {wallets.norm(m) for r in rows for m in (r['mints'] or []) + [r['deposit_mint']] if m},
            'mints_of': {r['name']: r['mints'] for r in rows if r['mints']},
            'claims': wallets.claims(config.WALLET_ID)}


def run_audits(state):
    """The hourly audit (audit.py) of the whole wallet: the residual owner's
    chore, where the chain has the audits. Never blocks the loop."""
    if not housekeeper('audit'):
        return None
    if time.time() - state.get('last_audit', 0) < AUDIT_EVERY_S:
        return None
    state['last_audit'] = time.time(); save(state)
    try:
        return audit.run(sys.modules[__name__], db, config, txfees, notify, wallet=wallet_book())
    except Exception as e:
        notify('audit_failed', reason=f'{type(e).__name__}: {tidy(e)}')
        return None


def janitor(state):
    """Once a day: reclaim the rent of empty token accounts the bot does not
    use (janitor.mjs). A dry run first, which costs nothing; a close only when
    there is rent to reclaim. The rent returns to the LP wallet and the next
    open deploys it. Wallet-wide: the residual owner's chore, keeping every
    mint of every profile of the wallet. Never blocks the loop."""
    if not housekeeper('janitor'):
        return None
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get('last_janitor') == today:
        return None
    state['last_janitor'] = today; save(state)
    try:
        (mint_a, _), (mint_b, _) = pool_tokens()
        keep = sorted(audit.keep_mints(sys.modules[__name__], mint_a, mint_b, wallet_mints()))
        plan, err = chain('close-empty', *keep, dex='janitor')
        if err or not plan:
            notify('janitor_failed', reason=err or 'no answer')
            return None
        # A mint closed before that has an account again was recreated by an
        # operation that needs it (2026-09-28: Raydium recreates RAY, the
        # pool's reward mint, at every close): keep it from now on.
        closed_before = set(state.get('janitor_closed') or [])
        back = sorted({a['mint'] for a in plan.get('closable') or []} & closed_before)
        if back:
            state['janitor_keep'] = sorted(set(state.get('janitor_keep') or []) | set(back)); save(state)
            keep = sorted(set(keep) | set(back))
            plan, err = chain('close-empty', *keep, dex='janitor')
            if err or not plan:
                notify('janitor_failed', reason=err or 'no answer')
                return None
        if not plan.get('closable'):
            return plan
        out, err = chain('close-empty', *keep, '--execute', dex='janitor')
        if err or not (out or {}).get('signature'):
            notify('janitor_failed', reason=err or 'no signature')
            return None
        state['janitor_closed'] = sorted(closed_before | {a['mint'] for a in out['closable']})
        state['last_audit'] = 0; save(state)          # re-audit next poll, on fresh reads
        db.event('JANITOR', f"closed {len(out['closable'])} empty token accounts, "
                            f"{out['reclaimSol']:.6f} SOL of rent back to the wallet")
        notify('JANITOR', accounts=[a['mint'] for a in out['closable']], reclaim_sol=out['reclaimSol'],
               signature=out['signature'])
        return out
    except Exception as e:
        notify('janitor_failed', reason=f'{type(e).__name__}: {tidy(e)}')
        return None


def guard_config(pool, src=None):
    """The fee/variance guard's settings for `pool` (calm.regime_view's
    `guard`), or None when it is off or `pool` is not one it was tested on
    (regime_guard_pools: sol-swing's DJT pool is not). Source 'real': the
    fees this profile's own positions accrued on `pool` over the window
    (guard_fee_yield). Source 'volume': the pool's fee constant, None while
    the tape (`src`, LAST_SURROGATE's record) holds slots filled from the
    surrogate in the last day, whose volume is another venue's."""
    if config.REGIME_GUARD == 'off' or pool not in config.REGIME_GUARD_POOLS:
        return None
    out = {'mode': config.REGIME_GUARD, 'source': config.REGIME_GUARD_SOURCE,
           'window_bars': config.REGIME_GUARD_WINDOW, 'threshold': config.REGIME_GUARD_THRESHOLD}
    if config.REGIME_GUARD_SOURCE == 'real':
        out['fee_yield'] = guard_fee_yield(pool, config.REGIME_GUARD_WINDOW)
    else:
        filled = (src or {}).get('filled_24h') or 0
        out['fee_c'] = None if filled else config.REGIME_GUARD_FEE_C.get(pool)
    return out


def guard_fee_yield(pool, window_bars):
    """calm.real_fee_yield over the last `window_bars` five-minute bars of
    this profile's positions on `pool`, or None for a pool other than the
    held one, or when its decimals, a dollar quote or the database are not
    there."""
    if pool != config.POOL:
        return None                          # pool_record() is the held pool's
    try:
        rec = pool_record()
        if not is_stable_mint(rec['token_b']['address']):
            return None
        since = datetime.now(timezone.utc) - dt.timedelta(seconds=window_bars * calm.BAR_SECONDS)
        rows, harv = db.guard_fee_rows(config.PROFILE, pool, since)
        return calm.real_fee_yield(rows, harv, int(rec['token_a']['decimals']), int(rec['token_b']['decimals']))
    except Exception as e:
        print(f'guard fee yield unavailable: {type(e).__name__}: {e}', flush=True)
        return None


def regime_choice_now(pool, price, pair=None):
    """The regime's width for a fresh band at `price` on `pool`, or None."""
    if not config.REGIME_ENABLED:
        return None
    bars = tape5(pool, price, pair)
    if bars is None:
        return None
    try:
        dex = config.DEX if pool == config.POOL else None
        f = liquidity_view(pool, dex, bars)['factor'] if dex else 1.0
    except Exception:
        f = 1.0
    theta = min(max(config.REGIME_THRESHOLD * f, 0.05), 0.40)
    v = calm.regime_view(bars, price, price / 1.01, price * 1.01, widths=config.REGIME_WIDTHS,
                         horizon_minutes=config.REGIME_HORIZON, threshold=theta, guard=guard_config(pool, LAST_SURROGATE.get(pool)))
    if not v or not calm.tape_fresh(bars[0], time.time()):
        return None
    return v['choice']


def calm_reopen_band(v, state):
    """The band to reopen at after a tight band exits: tight again while calm,
    budget left and a fresh tight band is unlikely to be touched soon;
    otherwise None, which means the ladder's band."""
    if not v or not v.get('calm') or calm_budget_left(state) <= 0:
        return None
    pf = v.get('p_touch_fresh')
    if pf is not None and pf >= config.CALM_THRESHOLD:
        return None
    return config.CALM_BAND


def resume_reopen(state):
    """Resume a confirmed CALM close without a pool review or a second move.

    The persisted intent survives a swap/open failure and a service restart.
    Reopen rechecks current conditions; missing data means wait, not wide-then-tight.
    """
    pending = state.get('pending_reopen')
    if not pending:
        return False
    if time.time() - pending.get('started_at', 0) > 86400:
        state.pop('pending_reopen', None); save(state)
        notify('idle', reason='dropped a CALM reopen intent older than a day')
        return False
    if (pending['pool'], pending['dex']) != (config.POOL, config.DEX):
        halt('pending reopen belongs to a different pool; refusing to spend its funds elsewhere')
        return True
    if not pending.get('closed'):
        # The process may have stopped after close landed but before recording it.
        db.close_position(pending['mint'], None, pending.get('withdraw_usd'))
        band_profile(pending['mint'], 'rebalance', pending.get('reason'))
        moves = state.setdefault('calm_times', [])
        if pending['started_at'] not in moves:
            moves.append(pending['started_at'])
        pending['closed'] = True
        save(state)
    if config.REGIME_ENABLED and time.time() - pending.get('started_at', 0) > 1800:
        # Half an hour without fresh data: open at the widest regime width
        # rather than leave the capital idle for a day (review, 2026-09-26).
        reopen(state, pending['reason'], band=config.REGIME_WIDTHS[-1])
        return True
    reopen(state, pending['reason'], band=pending['band'], recovering=True)
    return True


SWAP_RETRY_PAUSES = (15, 30)      # three attempts in all
# Jupiter's free API limits by IP per minute: its 429 needs the minute to pass,
# not 15 s (2026-10-01 20:33Z: all three attempts inside 76 s were refused).
# An RPC rate limit keeps the short pauses: rpc_policy moves to the next endpoint.
SWAP_RATE_LIMIT_PAUSES = (30, 60)
# When Jupiter fails without sending, the same swap is tried once directly on
# an Orca whirlpool (swap_orca.mjs), so a Jupiter outage or rate limit does not
# open a lopsided band (owner, 2026-10-01). '' turns the fallback off.
SWAP_FALLBACK = os.environ.get('LPBOT_SWAP_FALLBACK', 'orca-swap')


def balance_wallet(state, bal, rec):
    """Swap the wallet to about 50/50 through Jupiter before an open, when
    either side holds less than half the capital, which is what a centred
    band needs of each. Returns the wallet as it is after, or None when a
    swap failed (counted as a failure; the caller opens nothing).

    The audit of the calm study showed why the gate is "short of half" and
    not "empty": with swaps only on a one-sided wallet, every in-range move
    reopens capped by the scarcer token and the tight band's gain turns
    negative. The swap script itself does nothing within 2% of target.

    Without this an open after an exit is limited by the scarcer token: a
    band that left above holds only the quote token, and the reopen deposits
    a sliver of the capital. See SWAP_HOOK.md for the contract."""
    state['open_unbalanced'] = False
    if not config.REBALANCE_SWAP or not rec:
        return bal
    q = bal.get('quoteUsd')
    if q is None:
        notify('swap_skipped', reason='the quote token has no USD price; no swap sized on a guess')
        return bal
    res = native_reserve(bal)
    usd_a = max(bal['balanceA'] - (res if bal.get('nativeSide') == 'A' else 0), 0) * ui_price(bal) * q
    usd_b = max(bal['balanceB'] - (res if bal.get('nativeSide') == 'B' else 0), 0) * q
    C = capital(bal)
    # Swap when either side is short of what the open may deposit of it (its
    # target share, less 3% for price movement), not merely short of half: a
    # side at 51% capped a deposit at $195 while $45 sat idle (2026-09-26).
    # The swap script itself does nothing within 2% of target.
    need = C * side_target_fraction() * 0.97
    balanced = abs(usd_a - usd_b) <= 0.04 * (usd_a + usd_b)
    if min(usd_a, usd_b) >= need or balanced:
        return bal
    mint_a = (rec.get('token_a') or {}).get('address')
    mint_b = (rec.get('token_b') or {}).get('address')
    if not (chains.is_address(config.CHAIN, mint_a) and chains.is_address(config.CHAIN, mint_b)):
        notify('swap_skipped', reason='pool record has no mints')
        return bal
    # The swap script keeps the gas reserve out of what it sells, but not the
    # open's rent headroom: the native side's target carries it, or an open
    # after a buy of SOL comes up short by the headroom and leaves the other
    # token idle.
    head = res - config.GAS_RESERVE_SOL
    head_usd = head * ui_price(bal) * q if bal.get('nativeSide') == 'A' else \
        (head * q if bal.get('nativeSide') == 'B' else 0.0)
    share = C * side_target_fraction()
    target_a = f"{share + (head_usd if bal.get('nativeSide') == 'A' else 0.0):.2f}"
    target_b = f"{share + (head_usd if bal.get('nativeSide') == 'B' else 0.0):.2f}"
    # Prices and decimals the loop already has, so the swap needs no call to
    # Jupiter's rate-limited price API for the pool's own tokens.
    hints = {}
    try:
        ra, rb = rec.get('token_a') or {}, rec.get('token_b') or {}
        if ra.get('decimals') is not None and rb.get('decimals') is not None:
            hints = {mint_a: {'usd': ui_price(bal) * q, 'decimals': int(ra['decimals']), 'symbol': ra.get('symbol')},
                     mint_b: {'usd': q, 'decimals': int(rb['decimals']), 'symbol': rb.get('symbol')}}
    except (KeyError, TypeError, ValueError):
        hints = {}
    env = {'LPBOT_TOKEN_HINTS': json.dumps(hints)} if hints else None
    if config.WALLET_ID:
        # The wallet may hold other profiles' tokens: the swap plans from
        # this profile's sleeve only (swap_jupiter.mjs, swap_orca.mjs, the
        # venue signer).
        env = dict(env or {}, LPBOT_SLEEVE=json.dumps(wallets.sleeve_caps(bal, mint_a, mint_b)))
    swap_dex = route('swap')
    out, err = chain('rebalance', mint_a, mint_b, target_a, target_b, '--execute', dex=swap_dex, extra_env=env,
                     record=False)
    limited = re.search(r'Jupiter (rate limited|429)', str(err))        # an RPC limit rotates endpoints instead
    for pause in (SWAP_RATE_LIMIT_PAUSES if limited else SWAP_RETRY_PAUSES):
        # Nothing left this process: a transport failure is safe to repeat.
        # One retry was not enough: two rate limits in a row on 2026-09-28
        # 18:16Z opened a lopsided band and left $22 of SOL idle.
        if not ((err or not out) and not (out or {}).get('signature') and not (out or {}).get('partial')
                and re.search(r'rate limit|429|timeout|timed out|ECONNRESET|blockhash', str(err), re.I)):
            break
        time.sleep(pause)
        out, err = chain('rebalance', mint_a, mint_b, target_a, target_b, '--execute', dex=swap_dex, extra_env=env,
                         record=False)
    if held(err):
        return None                 # the open would be refused too: hold, no failure counted
    # The swapper's own health ('jupiter' on Solana, the venue signer on Base);
    # 'swap' is whether the bot could swap at all, by it or the fallback:
    # deploy_idle waits on 'swap' only.
    record_health(swap_dex, out, err)                 # one swap, one outcome, whatever the attempts
    if (SWAP_FALLBACK and SWAP_FALLBACK in SIGNERS and swap_dex == 'jupiter'
            and counts_as_failure(err or 'no result')
            and (err or not out) and not (out or {}).get('signature') and not (out or {}).get('partial')):
        notify('swap_fallback', reason=f'Jupiter failed without sending ({err or "no result"}); swapping on Orca')
        out, err = chain('rebalance', mint_a, mint_b, target_a, target_b, '--execute', dex=SWAP_FALLBACK,
                         extra_env=env, record=False)
    record_health('swap', out, err)
    if (err or not out) and not (out or {}).get('signature') and not (out or {}).get('partial'):
        # Nothing was sent. Open with what the wallet holds: a smaller
        # position earning fees beats capital idle until the next poll.
        notify('swap_skipped', reason=f'swap failed without sending ({err or "no result"}); opening with the wallet as it is')
        db.event('swap_skipped', f"{err or 'no result'} | full: {LAST_CHAIN_ERROR.get('text', '')}")
        state['open_unbalanced'] = True; save(state)   # its leftover is not tolerance: deploy_idle deploys it
        return bal
    if out and out.get('noop'):
        notify('swap_skipped', reason='already at target', usd_a=round(usd_a, 2), usd_b=round(usd_b, 2))
        return bal
    if err or not out or out.get('partial') or not out.get('sent'):
        state['failures'] += 1; save(state)
        db.event('swap_failed', err or 'no signature')
        notify('swap_failed', reason=err or 'no signature', failures=state['failures'],
               signature=(out or {}).get('signature'))
        if state['failures'] >= config.MAX_CONSECUTIVE_FAILURES:
            halt(f'{state["failures"]} consecutive failures')
        return None
    db.event('SWAP', f"{out.get('signature')} usd {out.get('swapUsdValue')}")
    notify('SWAP', signature=out.get('signature'), usd=out.get('swapUsdValue'),
           sold=out.get('sold'), bought=out.get('bought'),
           price_impact_pct=out.get('priceImpactPct'), route=out.get('routePlan'),
           before_usd_a=round(usd_a, 2), before_usd_b=round(usd_b, 2))
    time.sleep(5)
    after = wallet(config.POOL)
    return after if 'balanceA' in after else None


def best_band_for(pool, dex=None):
    """Score every candidate band on this pool's own recent data.

    Each band is replayed from many origins, not once: the bot decides on the
    median net return per day across rolling windows, so a band that fitted
    the last six weeks by luck does not beat one that works from most starting
    points. The churn gate then discards bands that rebalance too often.
    """
    try:
        p = dexes.pool(dex or config.DEX, pool)
    except Exception:
        p = None
    if not p:
        return None
    out = engine.ladder(p, engine.candles(pool), dexes.feasible_bands(p, config.BANDS),
                        config.CAPITAL_USD, config.SWAP_COST, policy=config.policy())
    if not out:
        return None
    rows, meta = out
    pick = dict(engine.choose(rows, config.MAX_REBALANCES_PER_DAY_MODELLED))
    pick['price'] = meta['price']
    pick['fee'] = meta['fee']
    pick['all_runs'] = rows
    pick['record'] = p
    return pick


# --- the board: which pool to be in -----------------------------------------

def board_pick(current_best, exclude_address):
    """The best pool on the board other than the one held, and the case for
    moving to it.

    Returns (run, best_row, gain, verdict). `gain` is the relative improvement
    of the board's best modelled net/day over `current_best`, the held pool's
    own best band under the same model. A candidate must be scored, screened,
    and — unless swaps are allowed — hold the same two tokens the wallet
    already holds, because entering a different pair means buying it.
    """
    run, rows = db.latest_scan(max_age_seconds=config.SCAN_INTERVAL * 3)
    if not rows:
        return run, None, None, 'no fresh board'
    held = (current_best or {}).get('record') or {}
    mints = {(held.get('token_a') or {}).get('address'), (held.get('token_b') or {}).get('address')}
    cands = []
    for r in rows:
        if r.get('net_day_pct') is None or r.get('skipped') or r['address'] == exclude_address:
            continue
        if not r.get('screen_ok'):
            continue
        if not config.ALLOW_SWAP and mints - {None}:
            if {r['token_a']['address'], r['token_b']['address']} != mints:
                continue
        cands.append(r)
    if not cands:
        return run, None, None, 'no eligible candidate on the board'
    # Ranked on the decision figure: the modelled median, scaled down where
    # the pool's own last-24h fees say the model's fee share is stale.
    use = lambda r: r.get('decision_day_pct') if r.get('decision_day_pct') is not None else r['net_day_pct']
    best = max(cands, key=use)
    if not current_best:
        return run, best, None, 'held pool unscorable'
    cur = current_best['net_day_pct']
    gain = (use(best) - cur) / max(abs(cur), 1e-9)
    return run, best, gain, None


def utc_hour():
    return db.now().hour


def busy_hour():
    """True when this UTC hour usually carries more than the average volume,
    so a voluntary move would cost the most. Uses the board's latest profile;
    with none, no hour is busy."""
    if not config.DEFER_MOVES_TO_QUIET_HOURS:
        return False
    o = db.season_outlook(db.season(), hour=utc_hour())
    return bool(o and not o['quiet'])


def consider_migration(state, status, current_best):
    """Compare the held pool with the board and act. Returns True when a
    rebalance was started (the caller then skips its own)."""
    if config.POOL_PINNED:
        return False
    run, best, gain, why = board_pick(current_best, config.POOL)
    if not best:
        notify('board_checked', verdict=why, scan=(run or {}).get('id'))
        return False
    cur_txt = (f"{config.DEX} {config.PAIR_LABEL} +/-{(current_best['band'] - 1) * 100:.0f}% "
               f"{current_best['net_day_pct']:.3f}%/day") if current_best else 'unscorable'
    best_txt = f"{best['dex']} {best['pair']} +/-{best['band_pct']:.0f}% {best['net_day_pct']:.3f}%/day"
    if gain is None:
        # The held pool could not be scored (a failed candle fetch): a missing
        # number is not a reason to pay for a move (review, 2026-09-26).
        notify('board_checked', held=cur_txt, best=best_txt,
               verdict='the held pool could not be scored; staying')
        return False
    if gain < config.MIGRATE_MIN_GAIN:
        notify('board_checked', held=cur_txt, best=best_txt,
               verdict=f'{gain * 100:.0f}% gain is under the {config.MIGRATE_MIN_GAIN * 100:.0f}% threshold')
        return False
    if best['dex'] not in config.EXECUTE_DEXES or best['dex'] not in SIGNERS:
        notify('MIGRATE_RECOMMENDED', held=cur_txt, best=best_txt,
               gain_pct=None if gain is None else round(gain * 100),
               pool=best['address'], dex=best['dex'],
               reason=f'no signer armed for {best["dex"]}; the bot stays where it is',
               command=f'python3 db.py repoint {config.PROFILE} {best["dex"]} {best["address"]}')
        return False
    if busy_hour():
        o = db.season_outlook(db.season(), hour=utc_hour())
        notify('move_deferred', kind='pool move', held=cur_txt, best=best_txt,
               reason=f"hour {o['hour_utc']:02d} UTC runs {o['now_x']}x the average; "
                      f"waiting for a quiet hour (trough {o['trough_hour_utc']:02d} UTC)")
        return False
    notify('MIGRATE', held=cur_txt, best=best_txt,
           gain_pct=None if gain is None else round(gain * 100),
           pool=best['address'], dex=best['dex'])
    db.event('MIGRATE', f'{config.DEX} {config.POOL} -> {best["dex"]} {best["address"]} '
                        f'({cur_txt} -> {best_txt})')
    rebalance(state, status, 'moved to a better pool', target=best)
    return True


LAST_VENUES = {}                # {'view': [...]} the latest on-chain venue ranking, for every book


def venue_candidates():
    """(dex, address, pair row) of the held pool and every screened pool of
    the same pair on the latest board, on a venue whose counters we read."""
    run, rows = db.latest_scan(max_age_seconds=config.SCAN_INTERVAL * 3)
    try:
        mints = {m for m, _ in pool_tokens()}
    except Exception:
        mints = None
    out = {config.POOL: (config.DEX, config.POOL, None)}
    for r in rows or []:
        if r.get('skipped') or not r.get('screen_ok') or r.get('dex') not in dexes.FEE_LAYOUT_DEXES:
            continue
        pair = {(r.get('token_a') or {}).get('address'), (r.get('token_b') or {}).get('address')}
        if mints and not config.ALLOW_SWAP and pair != mints:
            continue
        out[r['address']] = (r['dex'], r['address'], r)
    return list(out.values())


def sample_fee_growth(state, status=None):
    """Every VENUE_SAMPLE_S: one RPC call reads the fee counters of the held
    pool and every same-pair candidate, stores one sample each, and refreshes
    the ranking the book shows. Only where the chain has the counters."""
    if not config.CAPS.get('venues'):
        return
    if time.time() - state.get('last_fee_sample', 0) < config.VENUE_SAMPLE_S:
        return
    state['last_fee_sample'] = time.time(); save(state)
    try:
        cands = venue_candidates()
        states = dexes.fee_states([(d, a) for d, a, _ in cands])
        for d, a, _ in cands:
            if a in states:
                db.record_fee_state(d, a, states[a])
        if status and status.get('quoteUsd') is not None:
            venue_view(status['price'], status['quoteUsd'])
    except Exception as e:
        notify('venue_sample_failed', reason=tidy(e))


_REWARD_PX = {}                 # mint -> (fetched_at, usd): the last good price


def reward_prices(mints, max_age=6 * 3600):
    """Jupiter prices for reward mints, falling back to the last good price
    (up to six hours old) when the free API rate-limits: a missing price
    valued PancakeSwap's CAKE at nothing in the first live ranking."""
    try:
        fresh = dexes.jupiter_prices(mints)
    except Exception:
        fresh = {}
    now = time.time()
    for m, p in fresh.items():
        if p and p > 0:
            _REWARD_PX[m] = (now, float(p))
    return {m: _REWARD_PX[m][1] for m in mints if m in _REWARD_PX and now - _REWARD_PX[m][0] <= max_age}


def venue_income(pool, usd_a, usd_b, band=1.01):
    """The pool's on-chain income for a centred band, % per day, over up to
    the last 24 hours of samples, with the hours of evidence."""
    span = db.fee_state_span(pool)
    if not span:
        return None
    first, last, secs = span
    rw_mints = [m for m, _ in (last.get('rewards') or [])]
    rw_usd = reward_prices(rw_mints) if rw_mints else {}
    if rw_mints:
        dexes.mint_decimals(rw_mints)
    inc = dexes.band_income(first, last, secs, band, usd_a, usd_b, rw_usd)
    if not inc:
        return None
    return dict(inc, total_pct_day=inc['fee_pct_day'] + inc['reward_pct_day'], hours=round(secs / 3600, 1))


def venue_view(price, quote_usd=1.0):
    """Every candidate's on-chain +/-1% income, best first."""
    out = []
    for d, a, row in venue_candidates():
        inc = venue_income(a, price * quote_usd, quote_usd)
        if inc:
            out.append({'dex': d, 'address': a, 'held': a == config.POOL,
                        'pair': (row or {}).get('pair') or config.PAIR_LABEL, **inc, 'row': row})
    out.sort(key=lambda v: -v['total_pct_day'])
    LAST_VENUES['view'] = [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in x.items() if k != 'row'}
                           for x in out]
    return out


def calm_board_check(state, status):
    """The pool review while calm mode holds the tight band. The hourly ladder
    says nothing about a +/-1% band, but fee (and reward) density does: at any
    band, a dollar at the active price earns in proportion to it. Moves to the
    densest eligible pool, still tight, when it beats the held one by
    migrate_min_gain. Returns True when a move started."""
    if config.POOL_PINNED or status.get('quoteUsd') is None:
        return False
    # On-chain evidence first: the board's density put PancakeSwap 18% above
    # Raydium and live it earned half (2026-09-26). A move needs at least
    # VENUE_MIN_HOURS of counter samples on both pools.
    try:
        venues = venue_view(status['price'], status['quoteUsd'])
    except Exception as e:
        venues = []
        notify('venue_sample_failed', reason=tidy(e))
    held_v = next((v for v in venues if v['held']), None)
    ok = [v for v in venues if not v['held'] and v['hours'] >= config.VENUE_MIN_HOURS and v.get('row')
          and v['dex'] in config.EXECUTE_DEXES and v['dex'] in SIGNERS]
    if not held_v or held_v['hours'] < config.VENUE_MIN_HOURS:
        notify('board_checked', verdict=f'on-chain income: under {config.VENUE_MIN_HOURS}h of evidence on the held pool; staying')
        return False
    if not ok:
        notify('board_checked', held=f"{config.DEX} {held_v['total_pct_day']:.2f}%/d on chain",
               verdict='on-chain income: no other pool with enough evidence; staying')
        return False
    top = ok[0]
    gain = top['total_pct_day'] / max(held_v['total_pct_day'], 1e-9) - 1
    held_txt = f"{config.DEX} {config.PAIR_LABEL} {held_v['total_pct_day']:.2f}%/d at ±1% on chain ({held_v['hours']}h)"
    best_txt = f"{top['dex']} {top['pair']} {top['total_pct_day']:.2f}%/d at ±1% on chain ({top['hours']}h)"
    if gain < config.MIGRATE_MIN_GAIN:
        notify('board_checked', held=held_txt, best=best_txt,
               verdict=f'on-chain: {gain * 100:.0f}% more income is under the {config.MIGRATE_MIN_GAIN * 100:.0f}% threshold')
        return False
    best = top['row']
    notify('MIGRATE', held=held_txt, best=best_txt, gain_pct=round(gain * 100),
           pool=best['address'], dex=best['dex'], calm=True)
    db.event('MIGRATE', f'on-chain: {config.DEX} {config.POOL} -> {best["dex"]} {best["address"]} '
                        f'({held_txt} -> {best_txt})')
    k = (regime_choice_now(best['address'], status['price'], best.get('pair')) or config.REGIME_WIDTHS[0]) \
        if config.REGIME_ENABLED else config.CALM_BAND
    rebalance(state, status, 'moved to the pool that earns more on chain', target=best, band=k, calm_move=True)
    return True


def operator_target(spec):
    """Turn "<dex> <pool>" from the MIGRATE file into a board-shaped target,
    or explain why not."""
    if len(spec) != 2 or not guards.is_address(spec[1]):
        notify('migrate_refused', reason=f'MIGRATE must contain "<dex> <pool>", got {spec!r}')
        return None
    dex, pool = spec
    if dex not in SIGNERS or dex not in config.EXECUTE_DEXES:
        notify('migrate_refused', reason=f'{dex} has no armed signer (execute_dexes)')
        return None
    if dex == 'jupiter':
        notify('migrate_refused', reason='jupiter is a swap route, not a pool')
        return None
    try:
        rec = dexes.pool(dex, pool)
    except Exception:
        rec = None
    if not rec:
        notify('migrate_refused', reason=f'{dex} does not know a pool at {pool}')
        return None
    if dex == 'orca' and rec.get('adaptive_fee') and config.SIGNER_ENV.get('LPBOT_ORCA_ADAPTIVE') != '1':
        notify('migrate_refused', reason='adaptive-fee Orca pool: the signer cannot open it')
        return None
    try:
        held = {m for m, _ in pool_tokens()}
    except Exception:
        held = None
    want = {wallets.norm((rec.get('token_a') or {}).get('address')), wallets.norm((rec.get('token_b') or {}).get('address'))}
    if held is None or want != held:
        # Security review, 2026-09-26: with rebalance_swap on, the reopen
        # would swap half the capital into whatever the target names. A pair
        # change needs allow_swap AND a pool the service environment pins
        # (LPBOT_SWING_POOLS: the swing's two pools).
        if not config.ALLOW_SWAP:
            notify('migrate_refused', reason='target holds a different pair and allow_swap is off')
            return None
        if pool not in config.SWING_POOLS:
            notify('migrate_refused', reason='target holds a different pair and is not in LPBOT_SWING_POOLS')
            return None
    return {'dex': dex, 'address': pool, 'pair': rec['pair'], 'token_a': rec['token_a'],
            'token_b': rec['token_b'], 'net_day_pct': None, 'band_pct': None}


LEFT_BEHIND_RETRY_S = 600        # an unsold leftover is tried again this often
LEFT_BEHIND_GAS_MARGIN = 0.002   # native kept above reserve + rent: float dust must not fail gas_for_open


def left_behind(old_tokens, new_tokens):
    """The old pool's mints the new pool does not hold: what a pair-changing
    move leaves in the wallet. Pure."""
    new = {wallets.norm(m) for m, _ in new_tokens}
    return [wallets.norm(m) for m, _ in old_tokens if wallets.norm(m) not in new]


def repoint_with_leftovers(state, target):
    """repoint(), and remember in state['left_behind'] the old pool's tokens
    the new pool does not hold (sell_left_behind sells them)."""
    old = pool_tokens()
    repoint(target)
    rest = left_behind(old, pool_tokens())
    state['left_behind'] = sorted(set(state.get('left_behind') or []) | set(rest))
    state.pop('left_behind_at', None)
    save(state)
    notify('REPOINTED', dex=config.DEX, pool=config.POOL, pair=config.PAIR_LABEL, left_behind=rest)


def sell_left_behind(state, force=False):
    """Sell what a pair-changing move left behind (state['left_behind'])
    into the held pool's stablecoin side, so the reopen and deploy_idle put
    it in the band (the swing: SOL when it moves to DJT/USDC, DJT when it
    moves back). The native token is sold above the gas reserve only (the
    swap keeps it). Only a profile alone on its wallet sells: on a shared
    wallet the balance may be another profile's. Tried at most every
    LEFT_BEHIND_RETRY_S unless `force`; a mint leaves the list once sold or
    worth under SWEEP_MIN_USD. True when something was sold."""
    todo = list(state.get('left_behind') or [])
    if not todo:
        return False
    if not force and time.time() - float(state.get('left_behind_at') or 0) < LEFT_BEHIND_RETRY_S:
        return False
    state['left_behind_at'] = time.time(); save(state)
    mints, why, shared = claim_mints()
    if shared or mints is None:
        notify('left_behind_held', reason=why or 'the wallet is shared: a leftover may be another profile\'s',
               mints=todo)
        return False
    (ma, _), (mb, _) = pool_tokens()
    quote = mb if is_stable_mint(mb) or not is_stable_mint(ma) else ma
    native = wallets.norm(config.CAPS['native_mint'])
    got = wallets.read_balances(config.CHAIN, config.RPC, config.WALLET_ADDRESS, todo, native)
    if got is None:
        notify('left_behind_unsold', reason='balance unreadable', mints=todo)
        return False
    try:
        px = dexes.jupiter_prices(todo)
    except Exception:
        px = {}
    sold = False
    for m in todo:
        have = float(got[0].get(m) or 0.0)
        # The native token keeps the gas reserve (the swap keeps that itself)
        # and the new venue's open rent (held out of the cap): selling down
        # to the reserve left an Orca open 0.009 SOL short (test, 2026-10-02).
        cap = max(have - open_headroom(config.DEX) - LEFT_BEHIND_GAS_MARGIN, 0.0) if m == native else have
        amt = max(cap - config.GAS_RESERVE_SOL, 0.0) if m == native else cap
        usd = amt * float(px.get(m) or 0.0)
        if amt <= 0 or (px.get(m) and usd < SWEEP_MIN_USD):
            state['left_behind'] = [x for x in state['left_behind'] if x != m]; save(state)
            continue
        out, err = chain('rebalance', m, quote, '0', '1000000', '--execute', dex='jupiter',
                         extra_env={'LPBOT_SLEEVE': json.dumps({m: cap, quote: 0.0})})
        if out and out.get('signature') and not err:
            state['left_behind'] = [x for x in state['left_behind'] if x != m]; save(state)
            db.event('LEFT_BEHIND_SOLD', f"{m} {amt:.9f} -> {quote} {out['signature']}")
            notify('LEFT_BEHIND_SOLD', mint=m, amount=round(amt, 9), usd=round(usd, 4) if usd else None,
                   into=quote, signature=out['signature'])
            sold = True
        elif out and out.get('noop'):
            state['left_behind'] = [x for x in state['left_behind'] if x != m]; save(state)
        else:
            notify('left_behind_unsold', reason=err or 'no signature', mint=m, amount=round(amt, 9),
                   retry_in_s=LEFT_BEHIND_RETRY_S)
    return sold


def repoint(target):
    """Point the profile at the target pool and reload the configuration, so
    everything the loop reads from `config` is the new pool's."""
    guards.migration_target(target, execute_dexes=config.EXECUTE_DEXES, signers=SIGNERS,
                            known=dexes.KNOWN)
    db.repoint(config.PROFILE, target['dex'], target['address'], target['pair'],
               target['token_a']['symbol'], target['token_b']['symbol'])
    config.reload()
    assert config.DEX == target['dex'] and config.POOL == target['address'], \
        'config did not reload to the target pool'


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
WAIT_REFUSAL = re.compile(r'^refused: (wallet \S+ lock|claims unmeasurable|halted)')


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
            notify('mint_paused', reason=err, command=command,
                   action='holding: no open, no close, no failover until the mint is writable again')
            try:
                db.event('mint_paused', f'{command}: {err}')
            except Exception:
                pass
    elif MINT_HOLD['why'] and (out or {}).get('signature') and not err:
        MINT_HOLD['why'] = None
        notify('mint_resumed', command=command)


def gas_for_open(state, bal):
    """Whether native gas covers this open's rent and the reserve after it.
    A profile whose pool holds no native token pays its open's rent (a new
    DLMM bin array: 0.0435 SOL) from SOL another profile owns. A pool that
    holds it needs only the reserve in the wallet first: deposit_caps keeps
    reserve and rent out of the deposit, and the swap to 50/50 buys the rest;
    but every signer refuses every write below the reserve (2026-10-02: a
    SOL/USDC profile funded with USDC alone failed its open on each poll,
    toward a halt). Short of it, it holds, says so once, counts no failure."""
    side = bal.get('nativeSide')
    have = bal.get('sol')
    if have is None and side is not None:
        have = bal.get('balanceA' if side == 'A' else 'balanceB')      # the pool's native token is the gas
    have = float(have or 0.0)
    need = config.GAS_RESERVE_SOL if side is not None else native_reserve(bal)
    if have >= need:
        state.pop('gas_short_told', None)
        return True
    if not state.get('gas_short_told'):
        state['gas_short_told'] = time.time(); save(state)
        why = (f'{have:.4f} {config.CAPS["native_symbol"]} in the wallet, an open on {config.DEX} needs '
               f'{need:.4f} (reserve + rent): holding until gas is topped up')
        notify('gas_short', reason=why)
        db.event('gas_short', why)
    return False


def record_baseline(bal, at=None):
    """The capital a profile started with, once: its sleeve when its first
    open went in (capital_flows kind 'baseline', one per wallet and profile;
    db.since_start measures profit and the hold benchmarks against it). Token
    A in amounts[<mint A>] and, when it is native SOL, in `sol`; the stablecoin
    side in `usdc`; the price in UI units, B per A, as the snapshots'. A profile
    without a wallet (pre-020) has its baseline already. Never blocks the
    open that just landed. `at` is when `bal` was read: the baseline's time,
    so db.since_start counts every flow after the read and none before it."""
    if not config.WALLET_ID:
        return False
    try:
        (ma, _), (mb, _) = pool_tokens()
        a, b, q = float(bal.get('balanceA') or 0.0), float(bal.get('balanceB') or 0.0), float(bal['quoteUsd'])
        px = ui_price(bal)
        usdc = a if is_stable_mint(ma) and not is_stable_mint(mb) else b
        with db.cursor(commit=True) as cur:
            cur.execute("insert into capital_flows (ts, kind, sol, usdc, usd, price, signature, detail, amounts, "
                        "wallet_id, profile) values (%s, 'baseline', %s, %s, %s, %s, null, %s, %s, %s, %s) "
                        "on conflict do nothing",
                        (at or db.now(), a if ma == config.CAPS['native_mint'] else 0.0, usdc, (a * px + b) * q, px,
                         'the sleeve at the first open', json.dumps({ma: a, mb: b}), config.WALLET_ID,
                         config.PROFILE))
            return cur.rowcount == 1
    except Exception as e:
        notify('baseline_failed', reason=f'{type(e).__name__}: {tidy(e)}')
        return False


def reopen(state, reason, band=None, recovering=False):
    """Open a fresh position at the best band, or at `band` when calm mode
    asks for the tight one, sized to what the wallet holds (after a swap to
    50/50 when `rebalance_swap` is on and the wallet is lopsided)."""
    pool = config.POOL
    best = best_band_for(pool)
    if not best and band:
        # A tight calm band needs no ladder: it is centred on the live chain
        # price. Only the pool's record (tokens, for the swap) is needed. On
        # 2026-09-26 an hourly-candle fetch failure left the capital idle.
        try:
            best = {'band': band, 'price': None, 'net_day_pct': 0.0, 'rebal_per_day': 0.0,
                    'record': dexes.pool(config.DEX, pool)}
        except Exception:
            best = None
    if not best:
        notify('idle', reason='could not price the pool; opening nothing')
        return False
    read_at = db.now()                                   # before the read: the baseline's time
    bal = wallet(pool)
    if 'balanceA' not in bal:
        notify('idle', reason='could not read the wallet; opening nothing')
        return False
    if not quote_known(state, bal, 'the open'):
        return False
    if recovering and band and config.REGIME_ENABLED:
        k_now = regime_choice_now(pool, bal['price'])
        if k_now is None:
            notify('idle', reason='regime recovery waits for fresh five-minute data')
            return False
        band = k_now
    elif recovering and band:
        if not config.CALM_ENABLED:
            band = None
        else:
            price = bal['price']
            v = calm_view(state, {'price': price, 'whirlpool': pool,
                                 'lowerPrice': price / band, 'upperPrice': price * band})
            if not v or v['bar_age_s'] > 900 or v.get('p_touch_fresh') is None:
                notify('idle', reason='CALM recovery waits for fresh five-minute data')
                return False
            if not v['calm'] or v['p_touch_fresh'] >= config.CALM_THRESHOLD:
                band = None
    k = band or best['band']
    if not gas_for_open(state, bal):
        return False                                     # before the swap: no swap for an open that cannot run
    funded = bal                                         # the sleeve as funded: a first open's baseline
    bal = balance_wallet(state, bal, best.get('record'))
    if bal is None or not quote_known(state, bal, 'the open'):
        return False
    # A tight band is centred on the LIVE price: the ladder's price can be
    # minutes old, and on a +/-1% band that is a large part of the width.
    price = bal['price'] if band else best['price']
    lower, upper = price / k, price * k
    cap_a, cap_b = deposit_caps(bal)
    if cap_a * ui_price(bal) + cap_b < capital(bal) * 0.1 / bal['quoteUsd']:
        notify('idle', reason=f'wallet holds too little {bal["tokenA"]} and '
                              f'{bal["tokenB"]} to open; nothing to do',
               balanceA=bal['balanceA'], balanceB=bal['balanceB'])
        return False
    # Hard invariants before anything is signed. Each has been the shape of
    # a real loss somewhere: a band that does not contain the LIVE price, a
    # model price stale against the chain, a cap above the capital, a DEX
    # without an armed signer. A refusal is counted like a failed open.
    try:
        guards.open_request(pool=pool, dex=config.DEX, price=bal['price'], model_price=price,
                            lower=lower, upper=upper, cap_a=cap_a, cap_b=cap_b,
                            capital_usd=capital(bal), max_usd=config.MAX_USD,
                            quote_usd=bal['quoteUsd'], chain=config.CHAIN, ui_price=ui_price(bal),
                            execute_dexes=config.EXECUTE_DEXES, signers=SIGNERS)
    except guards.Refused as e:
        state['failures'] += 1; save(state)
        db.event('open_refused', str(e))
        notify('open_refused', reason=str(e), failures=state['failures'])
        if state['failures'] >= config.MAX_CONSECUTIVE_FAILURES:
            halt(f'{state["failures"]} consecutive failures')
        return False
    out, err = chain('open', pool, f'{lower:.6f}', f'{upper:.6f}',
                     f'{cap_a:.9f}', f'{cap_b:.9f}', '--execute')
    if held(err):
        return False                                     # held, said once by chain(): no failure, no retry storm
    if err:
        # The open may still have landed. Ask the chain before believing this.
        time.sleep(15)
        status, _ = read_status()
        if status and status.get('positionMint'):
            notify('open_recovered', detail='open reported an error but the '
                                            'position exists on chain',
                   positionMint=status['positionMint'])
            out = {'positionMint': status['positionMint'], 'signature': None}
        else:
            state['failures'] += 1; save(state)
            db.event('open_failed', err)
            notify('open_failed', reason=err, failures=state['failures'])
            if state['failures'] >= config.MAX_CONSECUTIVE_FAILURES:
                halt(f'{state["failures"]} consecutive failures')
            return False
    state['failures'] = 0; save(state)
    mint = (out or {}).get('positionMint')
    # What actually went in, not the configured capital: a wallet short of one
    # side opens a smaller position, and the ledger must say so. The chain's
    # own mark of the new position is the truth; the signer's estimate is the
    # fallback if that read fails.
    deposit_usd = None
    if mint:
        st, _ = read_status(mint)
        # The same mark the snapshots use: tokens plus the rent, so the
        # deposit and every later mark are measured the same way.
        deposit_usd = position_usd(st) if st else None
    if deposit_usd is None:
        deposit_usd = (out or {}).get('depositUsd')
    if deposit_usd is None:
        deposit_usd = min(capital(bal), (cap_a * ui_price(bal) + cap_b) * bal['quoteUsd'])
    db.open_position(mint, pool, config.PAIR_LABEL, lower, upper,
                     (k - 1) * 100, (out or {}).get('signature'),
                     deposit_usd, reason, config_name=config.PROFILE, dex=config.DEX)
    record_baseline(funded, read_at)
    state.pop('pending_reopen', None)
    save(state)
    notify_book('OPEN', pair=config.PAIR_LABEL, pool=pool, dex=config.DEX, lp_now_usd=deposit_usd,
           moves_24h_now=config.CALM_MAX_MOVES - calm_budget_left(state),
           opened_band=band_label(k), calm_band=bool(band),
           lower=round(lower, 4), upper=round(upper, 4),
           deposit_usd=round(deposit_usd, 2),
           deposit_a=(out or {}).get('depositEstA'), deposit_b=(out or {}).get('depositEstB'),
           cap_a=f'{cap_a:.6f} {bal["tokenA"]}', cap_b=f'{cap_b:.6f} {bal["tokenB"]}',
           expected_net_day_pct=None if band else round(best['net_day_pct'], 3),
           modelled_rebalances_per_day=None if band else round(best['rebal_per_day'], 2),
           model_scope='CALM policy not modelled by hourly ladder' if band else 'hourly ladder',
           signature=(out or {}).get('signature'), reason=reason)
    return True


def rebalance(state, status, reason, target=None, band=None, calm_move=False, exit_move=False, close_only=False,
              operator=False):
    """Harvest, close, and reopen: on the same pool, or on `target` (a board
    row) after repointing the profile. The close runs on the DEX the position
    is on, whatever the profile says by then.

    A calm move (narrow, re-centre, widen, or the exit of a tight band) has
    its own gap (`calm_min_gap_seconds`) and its own budget, counted apart
    from the normal ones; both together sit under one hard ceiling,
    max_rebalances_per_day + calm_max_moves_per_day, past which the bot
    halts as before. An operator move (the MIGRATE file: the swing's switch
    at the US open and close) never waits for the gap either; the ceilings
    hold."""
    now = time.time()
    recent = [t for t in state['rebalance_times'] if now - t < 86400]
    calm_recent = [t for t in state.get('calm_times', []) if now - t < 86400]
    last_any = max([state['last_rebalance']] + calm_recent)
    # An exit under regime mode never waits: out of range earns nothing.
    gap = 0 if (exit_move and config.REGIME_ENABLED) or operator else \
        (config.CALM_MIN_GAP if calm_move else config.MIN_REBALANCE_GAP)
    last = last_any if calm_move else state['last_rebalance']
    if now - last < gap:
        notify('rebalance_deferred', seconds_remaining=int(gap - (now - last)),
               kind='calm' if calm_move else 'normal')
        return
    if len(recent) + len(calm_recent) >= config.MAX_REBALANCES_PER_DAY + config.CALM_MAX_MOVES:
        halt(f'{len(recent) + len(calm_recent)} rebalances in 24h, at the hard ceiling')
        return
    if not calm_move and len(recent) >= config.MAX_REBALANCES_PER_DAY:
        halt(f'{len(recent)} rebalances in 24h, at the ceiling')
        return
    mint = status['positionMint']

    accrued_a = status.get('feesAccruedA', 0.0)
    accrued_b = status.get('feesAccruedB', 0.0)
    accrued_usd = status.get('feesAccrued_USD', 0.0)
    out, err = chain('harvest', mint, '--execute')
    if held(err):
        return                      # a paused mint refuses the close too: hold the position as it is
    state['last_harvest'] = now
    harvested = False
    if out and out.get('signature') and not err:
        harvested = True
        accrued_a, accrued_b, accrued_usd = measured_fees(out, status, accrued_a, accrued_b, accrued_usd)
        db.record_harvest(mint, accrued_a, accrued_b, accrued_usd,
                              out['signature'])
        # The fees just moved from the position to the wallet. Record the
        # position's counter at zero now, or the book double-counts them as
        # both realised and unrealised until the next poll.
        db.snapshot(mint, ui_price(status), status.get('inRange'),
                    status.get('liquidity'), 0.0, 0.0, 0.0,
                    wallet(status['whirlpool']).get('walletUsd'),
                    position_usd(status))
        notify('HARVEST', collected_usd=round(accrued_usd, 4),
               signature=out['signature'])
        try:
            distribute(state, mint, accrued_a, accrued_b)
            distribute_rewards(state, mint, signatures_of(out))
        except Exception as e:          # the split must never stop a move
            notify('payout_failed', reason=f'{type(e).__name__}: {tidy(e)}')
    else:
        # Never let a harvest block the close. An out-of-range position must
        # move, and the close collects any fees the harvest could not: the
        # Meteora and Byreal signers answer "nothing to claim" with no
        # signature, and treating that as a failure would leave the band out
        # of range until the bot halted.
        notify('harvest_skipped', reason=err or (out or {}).get('note') or 'no signature returned')

    if calm_move and target is None:
        state['pending_reopen'] = {'mint': mint, 'pool': config.POOL, 'dex': config.DEX,
                                   'band': band, 'reason': reason, 'started_at': now,
                                   'withdraw_usd': position_usd(status), 'closed': False}
        save(state)

    venue = health_key(('close',), config.DEX)
    out, err = chain('close', mint, '--execute', record=False)
    if held(err):
        state.pop('pending_reopen', None); save(state)
        return                      # held: said once by chain(), no failure counted
    if err and re.search(r'rate limit|429|timeout|timed out|ECONNRESET|blockhash', str(err), re.I):
        # A transport failure: if the position is provably still there, the
        # close did not land and one more try is safe. On 2026-09-26 a
        # rate-limited close left a move half-done until the next poll.
        time.sleep(15)
        check, _ = read_status()
        if check is not None and check.get('positionMint') == mint:
            notify('close_retry', reason=err)
            out, err = chain('close', mint, '--execute', record=False)
    if err:
        # A close that reports failure may have landed. This exact false
        # negative left a position closed and the capital idle in production.
        time.sleep(15)
        check, _ = read_status()
        if check is not None and not check.get('positionMint'):
            health.record_success(venue)              # the close landed: the venue works
            db.event('close_recovered', err)
            notify('close_recovered',
                   detail='close reported an error but the position is gone; '
                          'treating it as closed', error=err)
            out = {'signature': None}
        else:
            # The position is still open: nothing to resume later. A stale
            # intent would replay after an unrelated failure, on a pool the
            # bot may have left by then.
            state.pop('pending_reopen', None)
            record_health(venue, None, err)            # one close, one failure, whatever the attempts
            state['failures'] += 1; save(state)
            db.event('close_failed', err)
            notify('close_failed', reason=err, failures=state['failures'])
            if state['failures'] >= config.MAX_CONSECUTIVE_FAILURES:
                halt(f'{state["failures"]} consecutive failures')
            return
    else:
        record_health(venue, out, None)
    # What the close returns: the mark taken just before it, tokens plus the
    # rent the chain refunds. The best figure available without a second
    # read, and what per-pool P&L is measured against.
    db.close_position(mint, (out or {}).get('signature'), position_usd(status))
    if not harvested and (accrued_usd or 0) > 0:
        # The close collected the fees the harvest could not. Record them as
        # realised and split them, or they vanish from the ledger and from the
        # payout (review, 2026-09-26).
        db.record_harvest(mint, accrued_a, accrued_b, accrued_usd,
                          # 'close:' marks fees the close collected: its tx moves
                          # principal too, so the harvest audit cannot measure them
                          # from the vault outflow (audit 2026-09-30, row 63)
                          f"close:{(out or {}).get('signature') or mint}")
        try:
            distribute(state, mint, accrued_a, accrued_b)
        except Exception as e:
            notify('payout_failed', reason=f'{type(e).__name__}: {tidy(e)}')
    band_profile(mint, 'rebalance', reason)
    # The move counts from the moment the close landed: record it before the
    # CLOSE book, so the book's moves_24h includes it (audit, 2026-09-30: the
    # CLOSE and OPEN books were one short).
    if calm_move:
        state['calm_times'] = calm_recent + [now]
        if state.get('pending_reopen'):
            state['pending_reopen']['closed'] = True
    else:
        state['last_rebalance'] = now
        state['rebalance_times'] = recent + [now]
    save(state)
    notify_book('CLOSE', positionMint=mint, lp_now_usd=0.0, moves_24h_now=config.CALM_MAX_MOVES - calm_budget_left(state),
                signature=(out or {}).get('signature'), reason=reason)
    if close_only:
        return                      # a disabled profile's last move: nothing reopens
    if target:
        repoint_with_leftovers(state, target)
        sell_left_behind(state, force=True)
    reopen(state, reason, band=band)


def profile_enabled():
    """Whether this profile is enabled now (config.enabled, read every poll:
    an operator disables a running profile with an UPDATE). A pre-020
    profile (no wallet) is always. A read that fails changes nothing."""
    if not config.WALLET_ID:
        return True
    try:
        with db.cursor() as cur:
            cur.execute('select enabled from config where name = %s', (config.PROFILE,))
            r = cur.fetchone()
        return bool(r and r['enabled'])
    except Exception:
        return True


def disabled_hold(state, status):
    """A disabled profile with a position: it holds it (no swap, no open, no
    move), its claim and mints still its own, and closes it only when the
    operator asks (touch run/<profile>/CLOSE)."""
    if CLOSE.exists():
        CLOSE.unlink()                    # consumed before it runs, like the other triggers
        db.event('CLOSE_REQUESTED', 'operator touched CLOSE on a disabled profile')
        rebalance(state, status, 'operator closed a disabled profile', close_only=True)
        return
    if not state.get('disabled_told'):
        state['disabled_told'] = time.time(); save(state)
        notify('disabled', reason='profile disabled: holding its position; touch CLOSE to close it',
               positionMint=status['positionMint'])


DORMANT_POLL_S = 300              # a dormant profile reads its wallet at most this often


def dormant(state, bal):
    """Whether this profile, which holds no position, stays dormant: its
    sleeve could not deploy min_deploy_usd. A dormant profile attempts no
    open and fetches no tape; the feed hears 'dormant' once on entry and
    'deposit_seen' once on exit, never a line per poll. A wallet that cannot
    be read, or a quote with no price, changes nothing: a dormant profile
    stays dormant, an active one goes on as before. A reopen intent left by
    a close is never dormant: its funds are in the wallet."""
    was = bool(state.get('dormant'))
    if state.get('pending_reopen'):
        return False
    if 'balanceA' not in bal:
        return was
    usd = deployable_usd(bal)
    if usd is None:
        return was
    if usd < config.MIN_DEPLOY_USD:
        if not was:
            state['dormant'] = True; save(state)
            notify('dormant', deployable_usd=round(usd, 4), min_deploy_usd=config.MIN_DEPLOY_USD,
                   poll_seconds=max(DORMANT_POLL_S, config.POLL_SECONDS),
                   reason='no position and too little to deploy: no open until a deposit')
            db.event('dormant', f'${usd:.2f} deployable, under ${config.MIN_DEPLOY_USD:.2f}')
        return True
    if was:
        state['dormant'] = False; save(state)
        notify('deposit_seen', usd=round(usd, 2), deployable_usd=round(usd, 4),
               reason='waking: swap to 50/50, then open')
        db.event('deposit_seen', f'${usd:.2f} deployable: leaving dormant')
    return False


def main():
    if halted():
        print(f'HALT present: {halted()}')
        return 2
    config.require_wallet()
    probe_rpc()
    state = load()
    notify_book('startup', mode='ARMED — signs its own rebalances',
                **config.summary())
    # The board is wallet-wide work for the residual owner, and only for a
    # profile that may move pools.
    if housekeeper('scanner') and not config.POOL_PINNED:
        scanner.Scanner(notify, active=lambda: not state.get('dormant')).start()

    while True:
        why = halted()
        if why:
            notify('halted', reason=why)
            return 2

        status, err = read_status()

        if status is None and state.get('dormant'):
            # A dormant profile holds nothing and attempts nothing: a flaky
            # read is no news and no reason to halt. It waits for the next one.
            print(f'status unreadable while dormant: {err}', flush=True)
            time.sleep(max(DORMANT_POLL_S, config.POLL_SECONDS))
            continue
        if status is None:
            state['read_failures'] += 1; save(state)
            notify('status_unreadable', reason=err,
                   consecutive=state['read_failures'],
                   action='holding; opening nothing')
            if state['read_failures'] >= config.MAX_UNREADABLE_POLLS:
                halt(f'{state["read_failures"]} unreadable polls')
            time.sleep(config.POLL_SECONDS)
            continue
        state['read_failures'] = 0; save(state)
        if not profile_enabled():
            if not status.get('positionMint'):
                notify('disabled', reason='profile disabled and holds no position: the process stops')
                return 0
            disabled_hold(state, status)
            time.sleep(config.POLL_SECONDS)
            continue
        state.pop('disabled_told', None)
        portfolio_report(state)

        if not status.get('positionMint'):
            if MIGRATE.exists():
                # An operator move with nothing held (the swing's switch when
                # an open failed): no close, the pool changes and what the
                # old pair left behind is sold before the dormant test.
                spec = MIGRATE.read_text().split()
                MIGRATE.unlink()
                target = operator_target(spec)
                if target:
                    db.event('MIGRATE_REQUESTED', f'operator: {target["dex"]} {target["address"]} (no position)')
                    repoint_with_leftovers(state, target)
                    sell_left_behind(state, force=True)
            sell_left_behind(state)
            b0 = wallet(config.POOL)
            if dormant(state, b0):
                time.sleep(max(DORMANT_POLL_S, config.POLL_SECONDS))
                continue
            notify('no_position', detail='chain reports no open position')
            try:
                fo = venue_failover(state, None, price=b0.get('price'), quote=b0.get('quoteUsd'))
            except Exception as e:
                fo = False
                notify('failover_failed', reason=f'{type(e).__name__}: {tidy(e)}')
            if fo:
                time.sleep(config.CALM_POLL_SECONDS)
                continue
            if resume_reopen(state):
                time.sleep(config.CALM_POLL_SECONDS)
                continue
            # Nothing is held, so nothing is closed: choose the pool first.
            if not config.POOL_PINNED:
                cur = best_band_for(config.POOL)
                run, best, gain, why = board_pick(cur, config.POOL)
                if best and best['dex'] in config.EXECUTE_DEXES and best['dex'] in SIGNERS \
                        and gain is not None and gain >= config.MIGRATE_MIN_GAIN:
                    notify('MIGRATE', held=f'{config.DEX} {config.PAIR_LABEL} (no position)',
                           best=f"{best['dex']} {best['pair']} {best['net_day_pct']:.3f}%/day",
                           gain_pct=None if gain is None else round(gain * 100),
                           pool=best['address'], dex=best['dex'])
                    db.event('MIGRATE', f'{config.DEX} {config.POOL} -> {best["dex"]} {best["address"]} (no position)')
                    repoint(best)
            k0 = None
            if config.REGIME_ENABLED:
                try:
                    if b0.get('pool') != config.POOL:
                        b0 = wallet(config.POOL)                 # repointed above: the new pool's read
                    k0 = regime_choice_now(config.POOL, b0['price']) if 'price' in b0 else None
                except Exception:
                    k0 = None
            reopen(state, 'no position held', band=k0)
            time.sleep(config.POLL_SECONDS)
            continue

        price = status['price']
        wbal = wallet(status['whirlpool'])
        wusd = wbal.get('walletUsd')
        fc = forecast_for(status)
        sample_fee_growth(state, status)
        cv = calm_view(state, status) if not config.REGIME_ENABLED else None
        rv = regime_view(state, status)
        track_touch_forecasts(state, rv, status)
        if rv and fc:
            # the held band's survival over the long horizons, from the hourly
            # tape: what the MODE section shows next to the 2-hour choice
            rv['p_exit'] = {h: fc.get(f'p_exit_{h}h_regime', fc.get(f'p_exit_{h}h')) for h in (6, 24, 72, 168)}
            rv['median_life_hours'] = fc.get('median_life_hours_regime', fc.get('median_life_hours'))
        # Under regime mode every band is the regime's, whatever its width,
        # and with or without a view this poll: without one it holds, and an
        # exit reopens at the widest regime width. Letting the hourly rules
        # take a +/-1% band fired PROACTIVE at once (review, 2026-09-26).
        tight = bool(config.REGIME_ENABLED) or bool(cv and cv.get('tight_held'))
        if tight and fc:
            # The hourly six-hour rule says nothing about a +/-1% band; the
            # five-minute rule in calm.py is in charge of it.
            fc = dict(fc, act=False, suspended=('regime mode: the five-minute width rule is in charge' if rv
                                                  else 'tight band: the five-minute calm rule is in charge'))
        # The book's price is in UI units (Token-2022 scaled mints), as the
        # baseline's and the flows': a plain pool's uiPrice is its price.
        db.snapshot(status['positionMint'], ui_price(status), status.get('inRange'),
                        status.get('liquidity'),
                        status.get('feesAccruedA', 0.0),
                        status.get('feesAccruedB', 0.0),
                        status.get('feesAccrued_USD', 0.0),
                        wusd, position_usd(status), forecast=fc)
        record_risk(status, rv, fc)
        daily_report(state)
        run_audits(state)
        probe_breakers()

        try:
            fo = venue_failover(state, status)
        except Exception as e:
            fo = False
            notify('failover_failed', reason=f'{type(e).__name__}: {tidy(e)}')
        if fo:
            time.sleep(config.CALM_POLL_SECONDS)
            continue

        if not status.get('inRange'):
            side = 'above' if price > status['upperPrice'] else 'below'
            if config.REGIME_ENABLED:
                k = rv['choice'] if rv else config.REGIME_WIDTHS[-1]
            else:
                k = calm_reopen_band(cv, state) if tight else None
            notify('OUT_OF_BAND', side=side, price=price,
                   lower=status['lowerPrice'], upper=status['upperPrice'],
                   action=('harvest, close, reopen tight' if k else 'harvest, close, re-optimise, reopen'),
                   forecast=fc, calm=cv)
            rebalance(state, status, f'price went {side}', band=k, calm_move=tight, exit_move=True)
            time.sleep(config.POLL_SECONDS)
            continue

        # Calm mode: narrow when the market goes cold, re-centre the tight
        # band before it is touched, widen when the calm ends.
        sweep_foreign(state, wbal)
        if sell_left_behind(state):
            wbal = wallet(status['whirlpool'])                  # the sale's quote token is idle now
        if deploy_idle(state, status, wbal, rv, price):
            time.sleep(config.CALM_POLL_SECONDS)
            continue

        ract = calm.regime_decide(rv, widths=config.REGIME_WIDTHS, steps=config.REGIME_STEPS) if rv else None
        if ract == 'narrow' and rv.get('stale'):
            ract = None                                   # never narrow on a stale tape
        if ract and calm_budget_left(state) > 0 and voluntary_move_allowed(state):
            ev = 'REGIME_WIDEN' if ract == 'widen' else 'REGIME_NARROW'
            notify_book(ev, price=price, lower=status['lowerPrice'], upper=status['upperPrice'],
                        regime=rv, forecast=fc)
            db.event(ev, f"+/-{rv['held_pct']}% -> +/-{rv['choice_pct']}% ({rv['mode']}) "
                         f"sigma {rv['sigma_5m_pct']}% velocity {rv['velocity']}")
            rebalance(state, status, f"regime {rv['mode']}: +/-{rv['held_pct']}% -> +/-{rv['choice_pct']}%",
                      band=rv['choice'], calm_move=True)
            time.sleep(config.CALM_POLL_SECONDS)
            continue
        act = None if rv else calm.decide(cv, enabled=config.CALM_ENABLED,
                                          budget_left=(cv or {}).get('budget_left', 0))
        if act and not voluntary_move_allowed(state):
            act = None                                    # inside the gap: announce nothing, try next poll
        if act:
            what = {'narrow': ('CALM_NARROW', config.CALM_BAND, 'calm: tight band'),
                    'recentre': ('CALM_RECENTRE', config.CALM_BAND, 'calm: tight band re-centred before a touch'),
                    'widen': ('CALM_WIDEN', None, 'calm over: back to the ladder band')}[act]
            notify_book(what[0], price=price, lower=status['lowerPrice'], upper=status['upperPrice'],
                        calm=cv, forecast=fc)
            db.event(what[0], f"sigma {cv['sigma_5m_pct']}% cut {cv['cut_pct']}% "
                              f"p_touch {cv.get('p_touch')} fresh {cv.get('p_touch_fresh')}")
            rebalance(state, status, what[2], band=what[1], calm_move=True)
            time.sleep(config.CALM_POLL_SECONDS if what[1] else config.POLL_SECONDS)
            continue

        # Still inside, but likely not for long: re-centre now, at this price,
        # rather than at whatever price the exit happens to land on. Waits
        # for a quiet hour only while the probability is below the ceiling
        # (90%): past that the exit is imminent and the hour does not matter.
        if fc and fc.get('act') and config.PROACTIVE_THRESHOLD:
            p_now = fc.get('p_exit_horizon')
            if busy_hour() and (p_now or 0) < 0.9:
                o = db.season_outlook(db.season(), hour=utc_hour())
                notify('recentre_deferred', p_exit=p_now, horizon_hours=config.PROACTIVE_HORIZON,
                       reason=f"hour {o['hour_utc']:02d} UTC runs {o['now_x']}x the average; "
                              f"waiting for a quiet hour unless the probability reaches 90%",
                       forecast=fc)
            else:
                notify_book('PROACTIVE', price=price, lower=status['lowerPrice'],
                            upper=status['upperPrice'], p_exit=p_now,
                            horizon_hours=config.PROACTIVE_HORIZON,
                            threshold=config.PROACTIVE_THRESHOLD, forecast=fc,
                            action='harvest, close, re-centre on the current price')
                db.event('PROACTIVE', f"P(exit within {config.PROACTIVE_HORIZON}h) = {p_now} "
                                      f"at {price}, band {status['lowerPrice']}..{status['upperPrice']}")
                rebalance(state, status, f'P(exit within {config.PROACTIVE_HORIZON}h) '
                                         f'{(p_now or 0) * 100:.0f}% >= {config.PROACTIVE_THRESHOLD * 100:.0f}%')
                time.sleep(config.POLL_SECONDS)
                continue

        if harvest_due(state, status):
            dividend(state, status)
            status, err = read_status()
            if not status or not status.get('positionMint'):
                time.sleep(config.POLL_SECONDS)
                continue

        if MIGRATE.exists():
            # "<dex> <pool>". Consumed before it runs. The same gates as an
            # automatic move: a signer must exist and be armed for the DEX.
            spec = MIGRATE.read_text().split()
            MIGRATE.unlink()
            target = operator_target(spec)
            if target:
                db.event('MIGRATE_REQUESTED', f'operator: {target["dex"]} {target["address"]}')
                notify('MIGRATE', held=f'{config.DEX} {config.PAIR_LABEL}',
                       best=f"{target['dex']} {target['pair']} (operator)", gain_pct=None,
                       pool=target['address'], dex=target['dex'])
                # While calm holds the tight band, the move keeps it: reopen
                # tight on the new pool (if still calm) instead of at the
                # ladder band, which would cost a second move to narrow again.
                k = (regime_choice_now(target['address'], price, target.get('pair')) or rv['choice'] if rv
                     else calm_reopen_band(cv, state)) if tight else None
                rebalance(state, status, 'operator requested move', target=target,
                          band=k, calm_move=bool(k), operator=True)
                time.sleep(config.POLL_SECONDS)
                continue

        if REBALANCE.exists():
            # Consumed before it runs, so a failure cannot loop on the trigger.
            REBALANCE.unlink()
            db.event('REBALANCE_REQUESTED', 'operator touched REBALANCE')
            notify('REBALANCE_REQUESTED', price=price,
                   action='harvest, close, re-optimise, reopen')
            rebalance(state, status, 'operator requested')
            time.sleep(config.POLL_SECONDS)
            continue

        forced = REOPT.exists()
        if forced:
            REOPT.unlink()
            db.event('REOPT_REQUESTED', 'operator touched REOPT')
            notify('REOPT_REQUESTED', action='board and band review now')
        scan_id = db.latest_scan_id() if tight else None
        if tight and (forced or scan_id != state.get('calm_review_scan')):
            # The band review waits while calm holds the tight band; the pool
            # review does not. It ranks venues on fee and reward density,
            # which a band does not change, once for every new board.
            state['calm_review_scan'] = scan_id; state['last_reopt'] = time.time(); save(state)
            if calm_budget_left(state) > 0 and calm_board_check(state, status):
                time.sleep(config.CALM_POLL_SECONDS)
                continue
        elif not tight and (forced or time.time() - state.get('last_reopt', 0) > config.REOPT_INTERVAL):
            state['last_reopt'] = time.time(); save(state)
            best = best_band_for(status['whirlpool'])
            # The board first: a better pool elsewhere outranks a better band
            # here, and a move re-optimises the band on arrival anyway.
            if consider_migration(state, status, best):
                time.sleep(config.POLL_SECONDS)
                continue
            if best:
                # Score the band actually held against the best available, both
                # under the same model, so the comparison means something.
                # The held band's half-width is a property of the band, not of
                # where the price happens to sit inside it. Measuring it against
                # the current price returns a number that drifts the moment the
                # price moves off centre, misses the lookup below, and quietly
                # turns the whole re-optimiser into a no-op.
                held = (status['upperPrice'] / status['lowerPrice']) ** 0.5
                held_pct = round((held - 1) * 100)
                runs = {round((r['band'] - 1) * 100): r for r in best['all_runs']}
                cur_run = runs.get(held_pct) or nearest(runs, held_pct)
                if cur_run:
                    gain = ((best['net_day_pct'] - cur_run['net_day_pct'])
                            / max(abs(cur_run['net_day_pct']), 1e-9))
                    if gain >= config.REOPT_MIN_GAIN and busy_hour():
                        o = db.season_outlook(db.season(), hour=utc_hour())
                        notify('move_deferred', kind='reband',
                               held=f'+/-{held_pct}%', best=f'+/-{(best["band"] - 1) * 100:.0f}%',
                               reason=f"hour {o['hour_utc']:02d} UTC runs {o['now_x']}x the average; "
                                      f"waiting for a quiet hour (trough {o['trough_hour_utc']:02d} UTC)")
                        # come back next poll cycle rather than in six hours
                        state['last_reopt'] = time.time() - config.REOPT_INTERVAL + 1800; save(state)
                    elif gain >= config.REOPT_MIN_GAIN:
                        notify('REBAND', improvement_pct=round(gain * 100),
                               old_band=f'+/-{held_pct}%',
                               new_band=f'+/-{(best["band"] - 1) * 100:.0f}%',
                               old_net_day=round(cur_run['net_day_pct'], 3),
                               new_net_day=round(best['net_day_pct'], 3))
                        db.event('REBAND', f'{held_pct}% -> '
                                           f'{(best["band"] - 1) * 100:.0f}%')
                        rebalance(state, status, 're-optimised band')
                        time.sleep(config.POLL_SECONDS)
                        continue
                    notify('reopt_checked',
                           held=f'+/-{held_pct}% {cur_run["net_day_pct"]:.3f}%/day',
                           best=f'+/-{(best["band"] - 1) * 100:.0f}% '
                                f'{best["net_day_pct"]:.3f}%/day',
                           verdict=f'{gain * 100:.0f}% gain is under the '
                                   f'{config.REOPT_MIN_GAIN * 100:.0f}% threshold')

        if time.time() - state.get('last_book', 0) >= config.POLL_SECONDS - 5:
            state['last_book'] = time.time()
            notify_book('in_band', price=price, lower=status['lowerPrice'],
                        upper=status['upperPrice'],
                        liquidity=status.get('liquidity'), forecast=fc, calm=cv, regime=rv)
        # Last, and only in a poll that made no move: a transaction built
        # seconds after a close reads stale accounts. On 2026-09-28 18:56Z the
        # janitor closed the RAY account and the re-centre that followed
        # failed in simulation.
        janitor(state)
        time.sleep(config.CALM_POLL_SECONDS if tight else config.POLL_SECONDS)


if __name__ == '__main__':
    sys.exit(main() or 0)
