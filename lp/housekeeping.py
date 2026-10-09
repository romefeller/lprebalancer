"""Wallet-wide chores: audits and the janitor."""

import time
import types
from datetime import datetime, timezone

import audit
import config
import db
import txfees
from lp import books, capital, paths, signers, swaps
from venues.jupiter import prices as jupiter_api

AUDIT_EVERY_S = 3600


def bot_view():
    """What the audits read of the bot (audit.py's `bot`), looked up at each
    call, so a test that mocks one of these is the one the audit calls."""
    return types.SimpleNamespace(FEED=paths.FEED, load=paths.load, wallet=capital.wallet,
                                 deployable_usd=capital.deployable_usd, position_usd=capital.position_usd,
                                 pool_record=capital.pool_record, pool_tokens=capital.pool_tokens,
                                 plan_sweep=swaps.plan_sweep, read_status=signers.read_status, prices=jupiter_api)


def run_audits(state):
    """The hourly audit (audit.py) of the whole wallet: the residual owner's
    chore, where the chain has the audits. Never blocks the loop."""
    if not signers.housekeeper('audit'):
        return None
    if time.time() - state.get('last_audit', 0) < AUDIT_EVERY_S:
        return None
    state['last_audit'] = time.time(); paths.save(state)
    try:
        return audit.run(bot_view(), db, config, txfees, books.notify, wallet=capital.wallet_book())
    except Exception as e:
        books.notify('audit_failed', reason=f'{type(e).__name__}: {books.tidy(e)}')
        return None


def janitor(state):
    """Once a day: reclaim the rent of empty token accounts the bot does not
    use (chains/solana/janitor.mjs). A dry run first, which costs nothing; a close only when
    there is rent to reclaim. The rent returns to the LP wallet and the next
    open deploys it. Wallet-wide: the residual owner's chore, keeping every
    mint of every profile of the wallet. Never blocks the loop."""
    if not signers.housekeeper('janitor'):
        return None
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get('last_janitor') == today:
        return None
    state['last_janitor'] = today; paths.save(state)
    try:
        (mint_a, _), (mint_b, _) = capital.pool_tokens()
        keep = sorted(audit.keep_mints(bot_view(), mint_a, mint_b, swaps.wallet_mints()))
        plan, err = signers.chain('close-empty', *keep, dex='janitor')
        if err or not plan:
            books.notify('janitor_failed', reason=err or 'no answer')
            return None
        # A mint closed before that has an account again was recreated by an
        # operation that needs it (2026-09-28: Raydium recreates RAY, the
        # pool's reward mint, at every close): keep it from now on.
        closed_before = set(state.get('janitor_closed') or [])
        back = sorted({a['mint'] for a in plan.get('closable') or []} & closed_before)
        if back:
            state['janitor_keep'] = sorted(set(state.get('janitor_keep') or []) | set(back)); paths.save(state)
            keep = sorted(set(keep) | set(back))
            plan, err = signers.chain('close-empty', *keep, dex='janitor')
            if err or not plan:
                books.notify('janitor_failed', reason=err or 'no answer')
                return None
        if not plan.get('closable'):
            return plan
        out, err = signers.chain('close-empty', *keep, '--execute', dex='janitor')
        if err or not (out or {}).get('signature'):
            books.notify('janitor_failed', reason=err or 'no signature')
            return None
        state['janitor_closed'] = sorted(closed_before | {a['mint'] for a in out['closable']})
        state['last_audit'] = 0; paths.save(state)          # re-audit next poll, on fresh reads
        db.event('JANITOR', f"closed {len(out['closable'])} empty token accounts, "
                            f"{out['reclaimSol']:.6f} SOL of rent back to the wallet")
        books.notify('JANITOR', accounts=[a['mint'] for a in out['closable']], reclaim_sol=out['reclaimSol'],
                     signature=out['signature'])
        return out
    except Exception as e:
        books.notify('janitor_failed', reason=f'{type(e).__name__}: {books.tidy(e)}')
        return None
