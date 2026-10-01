"""One request slot at a time for Jupiter's free API, across every process.

Jupiter's free API (lite-api.jup.ag) limits requests per IP per minute. The
loop, its scanner thread, the audit and every signer and swap process ask it
for prices or quotes from this one host. On 2026-10-01 20:33Z they bursted
together and a pre-open swap got 429 three times in a row.

The protocol, shared with jupiter_gate.mjs:
    1. take the lock: create GATE.lock exclusively (O_CREAT | O_EXCL);
       a lock older than STALE_S is a crashed holder and is removed;
    2. read the last reserved slot from GATE (epoch seconds, as text);
    3. reserve next = max(now, last + spacing), write it, remove the lock;
    4. sleep until next.
The lock is held for a read and a write only. If it cannot be taken within
WAIT_S the caller goes on without a slot: the gate never stops the bot.
"""
import os
import time

GATE = os.environ.get('LPBOT_JUP_GATE', '/tmp/lp_bot_jupiter.gate')
SPACING_S = float(os.environ.get('LPBOT_JUP_SPACING_MS', '1100')) / 1000.0
STALE_S = 5.0
WAIT_S = 5.0


def _take(lock, now=time.time, sleep=time.sleep):
    """Creates the lock file; True when taken, False after WAIT_S."""
    t0 = now()
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return True
        except FileExistsError:
            try:
                if now() - os.path.getmtime(lock) > STALE_S:
                    os.unlink(lock)               # a crashed holder
                    continue
            except OSError:
                continue                          # removed meanwhile: try again
            if now() - t0 > WAIT_S:
                return False
            sleep(0.02)
        except OSError:
            return False                          # no writable directory: no gate


def reserve(gate=None, spacing=None, now=time.time, sleep=time.sleep):
    """Reserves the next request slot and returns its epoch time. Never raises."""
    gate = gate or GATE
    spacing = SPACING_S if spacing is None else spacing
    lock = gate + '.lock'
    if not _take(lock, now, sleep):
        return now()
    try:
        try:
            with open(gate) as fh:
                last = float(fh.read().strip() or 0.0)
        except (OSError, ValueError):
            last = 0.0
        t = now()
        nxt = max(t, last + spacing) if last <= t + 3600 else t     # a slot far ahead is garbage
        try:
            with open(gate, 'w') as fh:
                fh.write(f'{nxt:.6f}')
        except OSError:
            pass
        return nxt
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def wait_turn(gate=None, spacing=None, now=time.time, sleep=time.sleep):
    """Waits until this process may send one Jupiter request. Never raises."""
    try:
        nxt = reserve(gate, spacing, now, sleep)
        delay = nxt - now()
        if delay > 0:
            sleep(min(delay, 60.0))
    except Exception:
        pass
