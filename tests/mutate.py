"""Mutation testing for the money path.

Every mutant is one small change to one function the fee book depends on
(a comparison flipped, a bound moved, an `and` turned `or`, a guard's `return`
removed). The tests must fail on every mutant; a mutant that survives is a
change the tests cannot see, so a bug of that shape could ship.

Never runs in the bot's directory: the signers are spawned from disk on every
call, so a mutant written there would reach the live bot. Each worker copies
lp_bot into the scratch directory, has its own test database, and restores
its copy after every mutant.

    tests/mutate.py                   # every target
    tests/mutate.py fees txfees       # a subset, by target name
    MUT_WORKERS=4 tests/mutate.py

Exit status 0 only when every mutant is killed or listed in EQUIVALENT with a
reason.
"""
import ast
import concurrent.futures as cf
import copy
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH = pathlib.Path(os.environ.get('MUT_DIR') or tempfile.gettempdir()) / 'lp_bot_mutants'
WORKERS = int(os.environ.get('MUT_WORKERS', 6))
TIMEOUT = int(os.environ.get('MUT_TIMEOUT', 180))
PY = sys.executable
# Test databases are <MUT_DB_PREFIX><worker>_test: give a second concurrent run its own prefix.
DB_PREFIX = os.environ.get('MUT_DB_PREFIX', 'rebalancer_mut')
# The database each worker's is cloned from (createdb -T needs it idle: a run
# beside other test runs clones a private copy instead).
DB_TEMPLATE = os.environ.get('MUT_DB_TEMPLATE', 'rebalancer_test')

PY_TESTS = lambda *mods: [PY, '-m', 'unittest', '-q', '-f', *mods]
NODE_TESTS = lambda *files: ['node', '--test', *files]

# name: (file, functions, test command run from tests/)
TARGETS = {
    'fees': ('fees.py', ['split'], PY_TESTS('test_money_paths.Split', 'test_fee_integrity.SplitProperties', 'test_payout')),
    'guards': ('guards.py', ['_nonneg', 'fee_read_problem'], PY_TESTS('test_money_paths.FeeReadBounds', 'test_fee_integrity.FeeReadGuard')),
    'txfees': ('txfees.py', ['_raw', 'outflow', 'parse', 'fetch', 'harvested'],
               PY_TESTS('test_money_paths.TxParse', 'test_money_paths.TxFetch', 'test_fee_integrity.TxFees', 'test_fee_integrity.Replay')),
    'distribute': ('rebalancer.py', ['distribute'],
                   PY_TESTS('test_money_paths.Distribute', 'test_fee_integrity.DistributeProperties', 'test_fee_integrity.Replay', 'test_payout')),
    'read_status': ('rebalancer.py', ['read_status', 'fee_problem', 'sanitised'],
                    PY_TESTS('test_money_paths.Status', 'test_fee_integrity.ReadStatus', 'test_fee_integrity.Replay')),
    'measured': ('rebalancer.py', ['measured_fees'], PY_TESTS('test_money_paths.Measured', 'test_fee_integrity.Replay')),
    'risk': ('calm.py', ['chi2_sf_even', 'arch_lm', '_rms', 'risk_metrics'],
             PY_TESTS('test_money_paths.RiskMath', 'test_fee_integrity.RiskMetrics')),
    'risk_db': ('db.py', ['risk_row', 'record_risk_profile', 'last_fees'],
                PY_TESTS('test_money_paths.RiskRecord', 'test_fee_integrity.RiskProfileRecord', 'test_fee_integrity.Replay')),
    'record_risk': ('rebalancer.py', ['record_risk'], PY_TESTS('test_money_paths.RiskRecord', 'test_fee_integrity.RiskProfileRecord')),
    'rate_limit': ('engine.py', ['rate_limited', 'curl'], PY_TESTS('test_rate_limit')),
    'band_profile': ('db.py', ['record_band_profile'], PY_TESTS('test_band_profile')),
    'band_hooks': ('rebalancer.py', ['band_profile'], PY_TESTS('test_band_profile.Hooks')),
    'daily': ('db.py', ['daily_line', 'daily_lines', '_daily_or_none'], PY_TESTS('test_daily')),
    'health': ('health.py', ['cooldown', 'after_failure', 'after_success', 'verdict', 'load', 'record_failure',
                             'record_success', 'allowed', 'summary'], PY_TESTS('test_health', 'test_edges_0930')),
    'jupgate': ('jupgate.py', ['_take', 'reserve', 'wait_turn'], PY_TESTS('test_jupiter_gate.Gate')),
    'jupiter_gate_js': ('jupiter_gate.mjs', ['take', 'reserve', 'waitTurn'], NODE_TESTS('test_jupiter_gate.mjs')),
    'one_outcome': ('rebalancer.py', ['record_health', 'chain', 'balance_wallet', 'sweep_foreign'],
                    PY_TESTS('test_jupiter_gate', 'test_health', 'test_deploy_all', 'test_deploy_idle', 'test_sweep',
                             'test_multi_loop', 'test_scaled', 'test_review_edges')),
    'token_facts': ('dexes.py', ['jupiter_token', '_get'], PY_TESTS('test_jupiter_gate.LessJupiterTraffic', 'test_jupiter_gate.Gate')),
    'swap_orca': ('swap_orca.mjs', ['guard', 'toRaw', 'parseHints', 'checkPool', 'direction', 'planSwap', 'spotOutPerIn',
                                    'priceImpact', 'valueLossOk', 'verifyQuote', 'checkImpact', 'chooseCuPrice', 'cuLimit',
                                    'withWsolClose', 'decodeSwapV2', 'verifyTxShape', 'checkDeltas', 'noopReport', 'sendOnce'],
                  NODE_TESTS('test_swap_orca.mjs')),
    'resilience': ('rebalancer.py', ['health_key', 'counts_as_failure', 'chain', 'failover_pick', 'venue_failover',
                                     'voluntary_move_allowed', 'idle_deploys_left', 'deploy_idle'],
                   PY_TESTS('test_health', 'test_deploy_idle', 'test_edges_0930', 'test_multi_loop', 'test_scaled', 'test_review_edges')),
    'books': ('rebalancer.py', ['regime_at_move', 'notify_book', 'emoji_for', 'tidy'],
              PY_TESTS('test_move_books', 'test_observability', 'test_rebalancer', 'test_edges_0930')),
    'deployment': ('db.py', ['_pct', '_deployment', 'deployment_now', '_pnl'], PY_TESTS('test_move_books', 'test_audit_more', 'test_edges_0930', 'test_db')),
    'rpc_policy': ('rpc_policy.mjs', ['endpoints', 'errorKind', 'overEndpoints', 'isEntry'], NODE_TESTS('test_rpc_policy.mjs', 'test_signer_rpc.mjs')),
    'rpc_raydium': ('signer_raydium.mjs', ['withRpc', 'sendAll'], NODE_TESTS('test_signer_rpc.mjs')),
    'rpc_dlmm': ('signer_dlmm.mjs', ['withRpc', 'sendAll'], NODE_TESTS('test_signer_rpc.mjs')),
    'rpc_pancake': ('signer_pancake.mjs', ['withRpc', 'sendOne'], NODE_TESTS('test_signer_rpc.mjs')),
    'rpc_byreal': ('signer_byreal.mjs', ['withRpc', 'sendAll', 'confirm'], NODE_TESTS('test_signer_rpc.mjs')),
    'rpc_orca': ('signer2.mjs', ['withRpc', 'sendOnce'], NODE_TESTS('test_signer_rpc.mjs')),
    # Token-2022 stocks (MU, DJT, MSFTx): scaled UI amounts, pause and hook refusals, and the
    # signers' mint reads and position marks built on them.
    'token2022': ('token2022.mjs', ['effectiveMultiplier', 'mintFacts', 'readMints', 'rawToUi', 'uiToRaw',
                                    'uiToNative', 'uiPrice', 'writeRefusal', 'assertWritable', 'mintFields'],
                  NODE_TESTS('test_token2022.mjs', 'test_signer_stocks.mjs')),
    'stocks_dlmm': ('signer_dlmm.mjs', ['poolMints', 'positionView', 'unionView'], NODE_TESTS('test_signer_stocks.mjs')),
    'stocks_raydium': ('signer_raydium.mjs', ['poolMints', 'positionView', 'unionView'], NODE_TESTS('test_signer_stocks.mjs')),
    'stocks_orca': ('signer2.mjs', ['poolMints', 'chainView'], NODE_TESTS('test_signer_stocks.mjs')),
    'book_lines': ('book_format.mjs', ['shareAgrees', 'lpLine', 'emojiFor', 'healthLine'], NODE_TESTS('test_book_format.mjs')),
    'surrogate': ('calm.py', ['pair_tokens', 'clean_bars', 'fit_surrogate', 'binance_5m', 'surrogate_5m',
                              'missing_slots', 'tape_fresh'], PY_TESTS('test_tape_surrogate')),
    'surrogate_overlay': ('rebalancer.py', ['with_surrogate', '_with_surrogate', 'tape_source', 'track_tape_source', 'regime_view'],
                          PY_TESTS('test_tape_surrogate', 'test_regime', 'test_hardening.StaleTape',
                                   'test_quiet_pool')),
    'daily_report': ('rebalancer.py', ['daily_report'], PY_TESTS('test_daily.Report')),
    'priority_fee': ('swap_jupiter.mjs', ['swapRequestBody', 'priorityFeeLamports', 'verifyPriorityFee'],
                     NODE_TESTS('test_priority_fee.mjs', 'test_security.mjs')),
    'audit_checks': ('audit.py', ['check_idle', 'check_gas', 'check_equity', 'lookalike', 'classify_tx', 'check_flows', 'check_harvest',
                                  'payout_received', 'check_positions', 'check_owed', 'check_empty', 'check_fee_reads',
                                  'keep_mints', 'known_signatures'], PY_TESTS('test_audit', 'test_audit_runner')),
    'audit_run': ('audit.py', ['run'], PY_TESTS('test_audit.Runner', 'test_audit_runner', 'test_audit_edges', 'test_audit_more',
                                                  'test_multi_loop', 'test_scaled', 'test_audit_wallet')),
    'capital_db': ('db.py', ['since_start', 'record_flow', 'audit_value', 'set_audit_value', 'record_audit',
                             '_since_start_or_none'], PY_TESTS('test_audit.SinceStart', 'test_audit.Runner', 'test_audit_more.SinceStartEdges',
                                                      'test_db', 'test_multi_loop', 'test_scaled', 'test_since_start_scope')),
    'deploy_all': ('rebalancer.py', ['deployable_usd', 'capital', 'side_target_fraction', 'deposit_caps', 'balance_wallet'],
                   PY_TESTS('test_deploy_all', 'test_audit_more.QuoteFallbacks', 'test_rebalancer.DepositCaps', 'test_payout.SwapGate',
                            'test_payout.SwapRetry', 'test_multi_loop', 'test_scaled', 'test_review_edges', 'test_jupiter_gate', 'test_health',
                            'test_deploy_idle')),
    'loop_hooks': ('rebalancer.py', ['janitor', 'run_audits'], PY_TESTS('test_audit.Hooks', 'test_audit_more.Hooks', 'test_audit_more.JanitorKeepsWhatComesBack', 'test_audit_more.JanitorReplan', 'test_audit_more.JanitorUnsignedClose')),
    'janitor_js': ('janitor.mjs', ['planClose', 'closeInstructions', 'verifyCloseTx'], NODE_TESTS('test_janitor.mjs')),
    'book_format': ('book_format.mjs', ['equityLine', 'lpLine', 'sinceStartLine'], NODE_TESTS('test_book_format.mjs')),
    'deploy_idle': ('rebalancer.py', ['idle_to_deploy', 'deploy_idle', 'balance_wallet'],
                    PY_TESTS('test_deploy_idle', 'test_deploy_all', 'test_multi_loop', 'test_scaled', 'test_review_edges', 'test_jupiter_gate', 'test_health', 'test_edges_0930')),
    'idle_capital': ('rebalancer.py', ['idle_to_deploy', 'deploy_idle', 'plan_sweep', 'sweep_foreign'],
                     PY_TESTS('test_deploy_idle', 'test_sweep', 'test_multi_loop', 'test_scaled', 'test_review_edges', 'test_health', 'test_edges_0930')),
    'orca_fees': ('orca_fees.mjs', ['growthInside', 'ownFees', 'checkOrca', 'transferFeeOf', 'feesFromOrcaSnapshot',
                                    'snapshotAddresses', 'consistentOrcaFees'], NODE_TESTS('test_orca_fees.mjs')),
    'book_scope': ('db.py', ['book_profiles', 'book_scope', 'open_by_profile', 'flow_totals', '_uncounted_usd'],
                   PY_TESTS('test_stats.Scope', 'test_stats.Attribution', 'test_stats.Portfolio', 'test_stats.OtherWallet')),
    'book_sums': ('db.py', ['_sum_known', '_same', 'combine_days', 'combine_since', 'combine_books'],
                  PY_TESTS('test_stats.CombineBooks', 'test_stats.CombineExact', 'test_stats.Attribution')),
    'stats_sum': ('stats.py', ['record', 'total', 'classify', 'portfolio'],
                  PY_TESTS('test_stats.Total', 'test_stats.Classify', 'test_stats.Record', 'test_stats.Portfolio',
                           'test_stats.OtherWallet')),
    'bridge_tail': ('telegram_bridge.mjs', ['feedFiles', 'migrateState', 'readNew', 'tailAll', 'message'],
                    NODE_TESTS('test_telegram_bridge.mjs')),
    'bridge_lines': ('book_format.mjs', ['poolLabel', 'redact', 'portfolioText'], NODE_TESTS('test_telegram_bridge.mjs')),
    'wallets': ('wallets.py', ['norm', 'users', 'holder', 'sole_owner', 'split', 'claim_after', 'claimed_mints',
                               'with_self', 'sleeve', 'sleeve_caps'], PY_TESTS('test_wallets')),
    'wallets_db': ('wallets.py', ['wallet_profiles', 'register_mints', 'claims', '_adjust', 'adjust', 'settle_state',
                                  'set_pending', 'book', 'wallet_lock', '_solana_balance', '_evm_call', '_evm_balance',
                                  '_evm_head', 'read_balances', '_solana_write_slot', '_evm_write_slot', 'write_slot'],
                   PY_TESTS('test_wallets', 'test_multi_loop', 'test_claims_units')),
    'claims_loop': ('rebalancer.py', ['me_row', 'claim_mints', 'tries_in', 'measure', 'signatures_of', 'settle_pending',
                                      'unmeasurable', 'locked_chain', 'held', 'profile_enabled', 'disabled_hold', 'sleeve_of',
                                      'is_stable_mint', 'quote_known', 'dormant', 'halted', 'route', 'housekeeper',
                                      'wallet_mints', 'wallet_book', 'portfolio_report', 'record_baseline'],
                    PY_TESTS('test_multi_loop', 'test_claims_units')),
    # 2026-10-02: rent and fees one profile pays from another's native token, and rent priced as $0
    'shared_books': ('wallets.py', ['native_giver', 'internal_flows', 'book'],
                     PY_TESTS('test_shared_wallet_books', 'test_wallets', 'test_claims_units')),
    'shared_books_loop': ('rebalancer.py', ['native_giver', 'native_flows', 'settle_pending', 'locked_chain',
                                            'note_native_px', 'native_usd', 'rent_usd', 'position_usd',
                                            'record_baseline'],
                          PY_TESTS('test_shared_wallet_books', 'test_claims_units', 'test_multi_loop')),
    'shared_books_db': ('db.py', ['since_start', 'native_price'],
                        PY_TESTS('test_shared_wallet_books', 'test_since_start_scope', 'test_audit.SinceStart',
                                 'test_audit_more.SinceStartEdges', 'test_db', 'test_scaled')),
    'quiet_pool': ('calm.py', ['quiet_tail_ok', 'quiet_fill'], PY_TESTS('test_quiet_pool')),
    'quiet_overlay': ('rebalancer.py', ['quiet_ref_ts', 'with_surrogate', '_surrogate_ts', '_merge_all', 'tape_source'],
                      PY_TESTS('test_quiet_pool', 'test_tape_surrogate')),
    'quiet_db': ('db.py', ['tape_ref_pool'], PY_TESTS('test_quiet_pool')),
    # 2026-10-02: a payout's priority fee, and the send loop that proves an expired one never landed
    'payout_send': ('payout.mjs', ['payoutCuPrice', 'sendUntilLanded'], NODE_TESTS('test_payout_send.mjs')),
    # 2026-10-02: the swing (a profile on one pool in its market's session, another outside it)
    'swing_calendar': ('swing.py', ['session', 'is_open', 'wanted', 'decide', 'audit'],
                       PY_TESTS('test_swing.Calendar', 'test_swing.Decide', 'test_swing.Audit', 'test_swing.TickMore')),
    'swing_tick': ('swing.py', ['tick', 'tick_all', 'main', 'feed', 'rows', 'left_behind'],
                   PY_TESTS('test_swing.Tick', 'test_swing.TickMore')),
    'swing_loop': ('rebalancer.py', ['left_behind', 'repoint_with_leftovers', 'sell_left_behind', 'operator_target'],
                   PY_TESTS('test_swing', 'test_multi_loop', 'test_rebalancer')),
    # rebalance() as a whole: 2026-10-02, 38 survivors in lines older than the swing (harvest, close retries,
    # the 24 h windows, failure counts) are open work, not equivalents.
    'swing_rebalance': ('rebalancer.py', ['rebalance'],
                        PY_TESTS('test_swing', 'test_multi_loop', 'test_calm', 'test_regime', 'test_band_profile',
                                 'test_fee_integrity', 'test_hardening', 'test_rewards', 'test_jupiter_gate',
                                 'test_rebalancer', 'test_money_paths', 'test_move_books', 'test_edges_0930')),
    'add_profile': ('ops/add_profile.py', ['pool_spec', 'signer_env', 'build', 'drop_in', 'main'],
                    PY_TESTS('test_add_profile')),
    'tape_prune': ('db.py', ['config_pools', 'tape_prune_other_pools'], PY_TESTS('test_tape_prune')),
    'stock_loop': ('rebalancer.py', ['ui_price', 'open_headroom', 'native_reserve', 'deployable_usd', 'deposit_caps',
                                     'position_usd', 'note_scale', 'native_bars', 'mint_refusal', 'note_mint_refusal',
                                     'gas_for_open'],
                   PY_TESTS('test_scaled', 'test_multi_loop', 'test_deploy_all', 'test_rebalancer.DepositCaps')),
    'stock_audit': ('audit.py', ['ui_amount', 'mint_scale', 'human', 'classify_tx', 'payout_received', 'check_positions',
                                 'flow_owner'],
                    PY_TESTS('test_scaled', 'test_audit', 'test_audit_edges')),
    'rewards_measured': ('rebalancer.py', ['distribute_rewards'], PY_TESTS('test_rewards', 'test_hardening', 'test_reward_payout')),
    'reward_inflow': ('txfees.py', ['_ui', 'inflow'], PY_TESTS('test_rewards', 'test_hardening', 'test_reward_payout')),
    'halt_guard': ('halt_guard.mjs', ['haltFiles', 'assertNotHalted'], NODE_TESTS('test_halt_guard.mjs')),
    'swap_sleeve': ('swap_jupiter.mjs', ['parseSleeve', 'sleeveCap', 'uiOf', 'amountToRaw'], NODE_TESTS('test_sleeve.mjs')),
    'orca_sleeve': ('swap_orca.mjs', ['sellable'], NODE_TESTS('test_swap_orca.mjs')),
    'fee_snapshot': ('fee_snapshot.mjs', ['wrappingSubU128', 'checkFees', 'feesFromSnapshot', 'snapshotKeys',
                                          'decodeSnapshot', 'consistentFees'],
                     NODE_TESTS('test_fee_snapshot.mjs')),
    # Base / Aerodrome Slipstream (EVM): the deposit arithmetic, the refusals, the deployment
    # registry (which factory, NPM, router and quoter a pool may use), the endpoint policy and
    # the key file. The fork test is not run per mutant (it needs anvil and a fork).
    'evm_math': ('evm/clmath.mjs', ['sqrtRatioAtTick', 'amount0Delta', 'amount1Delta', 'amountsForLiquidity',
                                    'liquidityForAmount0', 'liquidityForAmount1', 'liquidityForAmounts', 'depositFor',
                                    'minWithSlippage', 'tickAtPrice', 'bandTicks', 'wrapPlan', 'toRaw', 'rawFromFloat',
                                    'parseSleeve', 'sleeveCap', 'capped'], NODE_TESTS('test_aerodrome.mjs')),
    'evm_signer': ('signer_aerodrome.mjs', ['guard', 'marketRefusals', 'spendable', 'positionView', 'simulateSequence',
                                            'runSteps', 'planOpen', 'closeCalls', 'checkRecipient', 'isNative',
                                            'deploymentOf', 'describe', 'ownPositions'],
                   NODE_TESTS('test_aerodrome.mjs')),
    'evm_rpc': ('evm/rpc.mjs', ['baseEndpoints', 'isLoopback', 'evmErrorKind', 'overBase'], NODE_TESTS('test_aerodrome.mjs')),
    'evm_key': ('evm/keyfile.mjs', ['validKey', 'writeNewKey', 'readKey'], NODE_TESTS('test_evm_wallet.mjs')),
}

SQL_TARGETS = {'band_profile', 'daily', 'capital_db', 'book_scope', 'book_sums', 'stats_sum', 'wallets_db'}

# Mutants that cannot change behaviour, with the reason. Keyed by the mutant's
# identity (see "identity" below):
#     (target, function, description, stripped source line, occurrence)
# The report prints each survivor's key: copy it here with a reason. The line
# number is not in the key, so an edit above a mutant keeps its entry valid.
EQUIVALENT = {
    # the swing (2026-10-02)
    ('swing_calendar', 'decide', 'const 0->1', "now_utc.timestamp() - float(last_request.get('at') or 0) < REQUEST_AGAIN_S:", 0):
        'a request without a time is read at epoch 0 or 1: decades past REQUEST_AGAIN_S either way',
    ('swing_tick', 'feed', 'flip bool', 'print(json.dumps(row), flush=True)', 0):
        'print flush only',
    ('swing_tick', 'tick', 'negate if', 'if dry:', 0):
        "the body is one print: the dry run's report line, nothing else",
    ('swing_tick', 'tick', 'skip if body', 'if dry:', 0):
        "the body is one print: the dry run's report line, nothing else",
    ('swing_tick', 'tick', 'flip bool', 'print(f\'{profile}: hold {want[0]} {want[1]} (position on {row["position_pool"]})\', flush=True)', 0):
        'print flush only',
    ('swing_tick', 'tick', 'flip bool', 'print(f\'{profile}: would write {migrate}: {want[0]} {want[1]} (holds {row["held_pool"]})\', flush=True)', 0):
        'print flush only',
    ('swing_tick', 'tick_all', 'flip bool', 'print(f\'swing {row["profile"]}: tick failed: {type(e).__name__}: {e}\', flush=True)', 0):
        'print flush only',
    ('swing_tick', 'main', 'flip bool', 'print(f\'swing: serving {", ".join(r["profile"] for r in rows()) or "no row"}\', flush=True)', 0):
        'print flush only',
    ('swing_tick', 'main', 'flip bool', "print(f'swing tick failed: {type(e).__name__}: {e}', flush=True)", 0):
        'print flush only',
    ('swing_loop', 'operator_target', 'drop operand 0', 'if held is None or want != held:', 0):
        'held None makes want != held true anyway: want is a set, never None',
    ('swing_loop', 'sell_left_behind', 'const 0->1', "if not force and time.time() - float(state.get('left_behind_at') or 0) < LEFT_BEHIND_RETRY_S:", 0):
        'a leftover never tried is read at epoch 0 or 1: decades past LEFT_BEHIND_RETRY_S either way',
    ('add_profile', 'build', 'drop operand 1', "if not (WALLET_ID.match(args.wallet) and ADDRESS[chain](args.address or '')", 1):
        'ADDRESS[chain] checks isinstance(str) first: None and empty are both refused',
    ('swing_rebalance', 'rebalance', 'flip bool', 'sell_left_behind(state, force=True)', 0):
        'repoint_with_leftovers pops left_behind_at just before: the retry wait is already over',
    ('payout_send', 'payoutCuPrice', '\\?\\? -> ||', 'const fees = (recent ?? []).map(r => Number(r?.prioritizationFee ?? r)).filter(f => f > 0).sort((a, b) => a - b);', 0):
        'recent is an array, null or undefined (a failed read gives []): || and ?? agree on all three',
    ('payout_send', 'payoutCuPrice', '\\?\\? -> ||', 'const fees = (recent ?? []).map(r => Number(r?.prioritizationFee ?? r)).filter(f => f > 0).sort((a, b) => a - b);', 1):
        'they differ only for a fee of 0, where || gives the row object; Number(object) is NaN and f > 0 drops it as it drops 0',
    # the surrogate overlay's body, renamed _with_surrogate on 2026-10-02 (reasons as before)
    ('surrogate_overlay', '_with_surrogate', 'skip if body', 'if bars is None:', 0):
        'None[0] raises inside the try, which returns bars (None) either way',
    ('surrogate_overlay', '_with_surrogate', 'swap Gt->GtE', 'if any(g not in have for g in gaps) and now - t > SURROGATE_REFRESH:', 0):
        'only an ask exactly SURROGATE_REFRESH later differs: a float clock never lands there',
    ('surrogate_overlay', '_with_surrogate', 'const 0->1', 't, name, s = _SURR.get(pool, (0, None, None))', 0):
        'an empty cache asked at t = 0 or t = 1: both are decades past the refresh',
    ('surrogate_overlay', '_with_surrogate', 'flip bool', "print(f'surrogate tape failed: {type(e).__name__}: {e}', flush=True)", 0):
        'print flush only',
    ('surrogate_overlay', '_with_surrogate', 'swap GtE->Gt', 's = tuple(c[s[0] >= now - SURROGATE_LOOKBACK_S - 3600] for c in s)   # the last day only', 0):
        'the trim is an hour beyond the fill window: a bar at its edge is never used',
    ('surrogate_overlay', '_with_surrogate', 'const 3600->3601', 's = tuple(c[s[0] >= now - SURROGATE_LOOKBACK_S - 3600] for c in s)   # the last day only', 0):
        'the trim is an hour beyond the fill window: a bar at its edge is never used',
    ('surrogate_overlay', 'regime_view', 'const 0->1', 'hold_left = 0', 0):
        'hold_left is read only in STALE mode on a fresh tape, where it is assigned first',
    ('surrogate_overlay', 'regime_view', 'drop operand 0', 'if v and not fresh:', 0):
        'calm.regime_view returns None only for no bars, which returned before',
    # the quiet-pool fill (2026-10-02)
    ('quiet_pool', 'quiet_fill', 'swap LtE->Lt', 'while j + 1 < len(t_arr) and t_arr[j + 1] <= s:', 0):
        'a bar at slot s is in `have` and skipped; j steps onto it at the next slot, before it is used',
    ('quiet_pool', 'quiet_fill', 'swap Gt->GtE', 'quiet = (s < now - history_s or s in ref or (ref_live and s > ref_newest))', 0):
        's == ref_newest is in ref, so the slot is quiet either way',
    ('quiet_pool', 'quiet_fill', 'swap Lt->LtE', 'if quiet and (s < last_bar or tail_fill):', 0):
        's == last_bar is in `have` and was skipped before this line',
    ('quiet_overlay', 'quiet_ref_ts', 'drop operand 0', "if c and c.get('for') == pool and now - c['at'] <= QUIET_REF_REFRESH:", 0):
        "an empty cache has no 'for', and None is never a pool: the test fails either way",
    ('quiet_overlay', 'with_surrogate', 'skip if body', 'if out is None:', 0):
        'out[4] of None raises inside the try, whose except returns out (None) either way',
    ('quiet_overlay', 'with_surrogate', 'skip if body', 'if q is None:', 0):
        '_merge_all over None raises inside the try, whose except returns out either way',
    ('quiet_overlay', 'with_surrogate', 'flip bool', "print(f'quiet fill failed: {type(e).__name__}: {e}', flush=True)", 0):
        'print flush only',
    ('shared_books_loop', 'settle_pending', 'const 0.0->1.0', 'def settle_pending(wait_s=0.0):', 0):
        'as claims_loop: tries_in(1.0) == tries_in(0.0) == 1 (1.0 // CLAIM_POLL_S is 0): one read either way',
    ('shared_books_loop', 'settle_pending', 'const 0->1', "if at is None and time.time() - float(p.get('sent_at') or 0) <= PENDING_EXPIRE_S:", 0):
        'as claims_loop: a missing sent_at at 0 or 1 is decades older than PENDING_EXPIRE_S either way',
    ('shared_books', 'internal_flows', 'swap Lt->LtE', 'out_p, in_p = (giver, taker) if delta < 0 else (taker, giver)', 0):
        'delta == 0 never reaches this line: |delta| <= DUST returns [] first',
    ('audit_run', 'run', 'drop operand 0', "if a['mint'] in px and a['mint'] != NATIVE:", 0):
        'a mint without a price adds nothing: idle_sleeves_usd values it at prices.get(m) or 0.0, and px holds no price for it',
    ('audit_run', 'run', 'drop operand 1', "if a['mint'] in px and a['mint'] != NATIVE:", 0):
        'held[NATIVE] is set to native / 1e9 right after the loop: what the loop put there is replaced',
    ('audit_run', 'run', 'and<->or', "if a['mint'] in px and a['mint'] != NATIVE:", 0):
        'it adds mints without a price (valued at 0 by idle_sleeves_usd) and the wrapped SOL account (replaced by native / 1e9 after the loop)',
    ('capital_db', '_since_start_or_none', 'const 0.0->1.0', 'return since_start(_uncounted_usd(names[0]) if len(names) == 1 else 0.0, profile, wallet_id)', 0):
        'with several names since_start never reads extra_usd: each book adds its own _uncounted_usd',
    ('wallets_db', '_solana_write_slot', 'drop operand 0', "if not st or st.get('confirmationStatus') not in ('confirmed', 'finalized'):", 0):
        'a None status raises AttributeError on .get; the only caller, write_slot, turns every exception into None: the same answer as the `return None`',
    ('wallets_db', '_evm_write_slot', 'skip if body', "if not rc or rc.get('blockNumber') is None:", 0):
        "a None receipt or block number raises in int(rc['blockNumber'], 16); the only caller, write_slot, turns every exception into None: the same answer as the `return None`",
    ('wallets_db', '_evm_write_slot', 'and<->or', "if not rc or rc.get('blockNumber') is None:", 0):
        'None raises AttributeError, {} KeyError, a None block TypeError; the only caller, write_slot, turns every exception into None: the same answer as the `return None`',
    ('wallets_db', '_evm_write_slot', 'drop operand 0', "if not rc or rc.get('blockNumber') is None:", 0):
        'a None receipt raises AttributeError on .get; the only caller, write_slot, turns every exception into None: the same answer as the `return None`',
    ('wallets_db', '_evm_write_slot', 'drop operand 1', "if not rc or rc.get('blockNumber') is None:", 0):
        'a None block number raises TypeError in int(None, 16); the only caller, write_slot, turns every exception into None: the same answer as the `return None`',
    ('rewards_measured', 'distribute_rewards', 'const 0.0->1.0', 'due[m] = max(float(due.get(m, 0.0)) - amt, 0.0)', 1):
        'txfees.inflow answers every mint it is asked, so due holds every m of the loop: the default is never read',
    ('resilience', 'venue_failover', 'const 0->1', "if int(rec.get('fails') or 0) < health.TRIP_FAILS:    # failover follows the count, not the light", 0):
        'no failure on record reads as 0 or 1: both are under TRIP_FAILS (3), so neither fails over',
    # The claims core and the measured rewards (review fixes, 2026-10-02)
    ('claims_loop', 'claim_mints', 'flip bool', "return None, f'mints unknown: {type(e).__name__}: {tidy(e)}', True", 0):
        'its one caller (locked_chain) refuses on mints None before it reads `shared`',
    ('claims_loop', 'measure', 'const 0.0->1.0', 'def measure(mints, min_slot=0, wait_s=0.0):', 0):
        'tries_in(1.0) == tries_in(0.0) == 1 (1.0 // CLAIM_POLL_S is 0): one read either way',
    ('claims_loop', 'settle_pending', 'const 0.0->1.0', 'def settle_pending(wait_s=0.0):', 0):
        'tries_in(1.0) == tries_in(0.0) == 1 (1.0 // CLAIM_POLL_S is 0): one read either way',
    ('claims_loop', 'settle_pending', 'const 0->1', "if at is None and time.time() - float(p.get('sent_at') or 0) <= PENDING_EXPIRE_S:", 0):
        'a missing sent_at reads as epoch 0 or 1: both are decades past PENDING_EXPIRE_S',
    ('claims_loop', 'held', 'drop operand 0', 'return mint_refusal(err) or (bool(err) and bool(WAIT_REFUSAL.match(str(err))))', 1):
        "a falsy err is None or '': str() of it never matches the anchored '^refused: ' pattern",
    ('rewards_measured', 'distribute_rewards', 'swap Gt->GtE', 'got = min(measured, quoted) if measured > 0 else 0.0', 0):
        'at measured == 0 the mutant gives min(0, quoted) <= 0: `got <= 0` refuses it, as it refuses 0.0',
    ('rewards_measured', 'distribute_rewards', 'const 0.0->1.0', "amt = min(float((out or {}).get('amount') or 0.0), float(due.get(m, 0.0)))", 1):
        'txfees.inflow answers every mint it is asked, so due holds every m of the loop: the default is never read',
    # Re-keyed 2026-10-02 (the multi-pool branch rewrote these lines; each reason checked again)
    ('resilience', 'chain', 'const 420->421', 'def chain(*args, dex=None, timeout=420, extra_env=None, record=True):', 0):
        'one second more on a 420 s signer timeout',
    ('resilience', 'deploy_idle', 'swap Lt->LtE', "state['idle_deploys'] = [t for t in (state.get('idle_deploys') or []) if now - t < 86400] + [now]; save(state)", 0):
        'only a deploy exactly 86400.0 s old differs: a float clock never lands there',
    ('resilience', 'voluntary_move_allowed', 'const 86400->86401', "recent = [t for t in state.get('calm_times', []) if now - t < 86400]", 0):
        'a move older than a day passes the gap anyway: keeping it in the window changes nothing',
    ('resilience', 'voluntary_move_allowed', 'const 86400->172800', "recent = [t for t in state.get('calm_times', []) if now - t < 86400]", 0):
        'a move older than a day passes the gap anyway: keeping it in the window changes nothing',
    ('resilience', 'voluntary_move_allowed', 'swap Lt->LtE', "recent = [t for t in state.get('calm_times', []) if now - t < 86400]", 0):
        'only a move exactly 86400.0 s old differs: a float clock never lands there',
    ('resilience', 'voluntary_move_allowed', 'const 0->1', "last_any = max([state.get('last_rebalance', 0)] + recent)", 0):
        'a last rebalance at epoch 0 or 1 is decades past the gap',
    ('resilience', 'voluntary_move_allowed', 'swap GtE->Gt', "return now - last_any >= config.CALM_MIN_GAP and health.allowed(f'venue:{config.DEX}', now)[0]", 0):
        'only a gap of exactly CALM_MIN_GAP seconds differs: a float clock never lands there',
    ('resilience', 'failover_pick', 'drop operand 0', "if not v.get('held') and v.get('dex') != held_dex and v.get('row')", 0):
        'the held venue is the held dex (config.POOL is on config.DEX): `dex != held_dex` excludes it too',
    ('one_outcome', 'balance_wallet', 'drop operand 0', "if (SWAP_FALLBACK and SWAP_FALLBACK in SIGNERS and swap_dex == 'jupiter'", 0):
        "an empty fallback name is never a SIGNERS key: '' in SIGNERS is False, as `SWAP_FALLBACK and` is",
    ('one_outcome', 'balance_wallet', 'const 0.0->1.0', "(head * q if bal.get('nativeSide') == 'B' else 0.0)", 0):
        'with no native side neither target adds head_usd: its value is never read',
    # wallets.py, the loop's sleeves (CORE, 2026-10-01)
    ('wallets', 'claim_after', 'swap GtE->Gt', 'return (v, 0.0) if v >= 0 else (0.0, -v)', 0):
        'at v = 0 the mutant returns (0.0, -0.0): the same numbers',
    ('wallets', 'sleeve', 'swap Gt->GtE', 'if over_a > DUST or over_b > DUST:', 0):
        'only an overdraw of exactly 1e-12 differs: float noise never lands there',
    ('wallets', 'sleeve', 'swap Gt->GtE', 'if over_a > DUST or over_b > DUST:', 1):
        'only an overdraw of exactly 1e-12 differs: float noise never lands there',
    ('wallets', 'sleeve', 'drop operand 0', "keep_native = bal.get('nativeSide') is not None or native_owner == name", 0):
        "a pool with a native side has no native term in the signer's walletUsd beyond its own tokens: native_usd "
        "is that figure's 4-decimal rounding at most, kept or not",
    ('wallets_db', 'wallet_lock', 'swap GtE->Gt', 'if time.monotonic() >= deadline:', 0):
        'a monotonic clock equal to the deadline is one instant: the next poll decides either way',
    ('wallets', 'split', 'skip if body', 'if h is not None:', 0):
        "every view starts at 0.0: the holder's 0.0 after an overdraw is set already",
    ('claims_loop', 'sleeve_of', 'drop operand 1', 'if bal.get(\'quoteUsd\') is None and mb and is_stable_mint(mb):', 0):
        'is_stable_mint(None) is False: a missing mint fills nothing either way',
    ('claims_loop', 'wallet_mints', 'skip if body', 'if not config.WALLET_ID:', 0):
        'a NULL wallet id matches no profile row: the query returns none, set() either way',
    ('claims_loop', 'portfolio_report', 'drop operand 0', "if not (config.WALLET_ID and config.RESIDUAL_OWNER) or state.get('last_portfolio') == today:", 1):
        'without a wallet id the first wallet by id is never this one: the next guard returns None before any write',
    ('claims_loop', 'dormant', 'skip if body', "if 'balanceA' not in bal:", 0):
        'an empty read has no quoteUsd: deployable_usd is None and the next guard returns the same `was`',
    ('stock_loop', 'note_scale', 'drop operand 1', 'if pool and px and ui and float(px) > 0 and float(ui) > 0:', 0):
        'a missing price raises in float() (caught) and a zero one fails > 0: no update either way',
    ('stock_loop', 'note_scale', 'drop operand 2', 'if pool and px and ui and float(px) > 0 and float(ui) > 0:', 0):
        'a missing uiPrice raises in float() (caught) and a zero one fails > 0: no update either way',
    ('stock_loop', 'note_scale', 'swap Gt->GtE', 'if pool and px and ui and float(px) > 0 and float(ui) > 0:', 0):
        'a zero price is already refused by `px and`: > and >= differ only at 0',
    ('stock_loop', 'note_scale', 'swap Gt->GtE', 'if pool and px and ui and float(px) > 0 and float(ui) > 0:', 1):
        'a zero uiPrice is already refused by `ui and`: > and >= differ only at 0',
    ('stock_loop', 'mint_refusal', 'drop operand 0', 'return bool(err) and bool(REFUSED_MINT.match(str(err)))', 0):
        "str(None) is 'None', which the refusal pattern never matches: False either way",
    ('swap_sleeve', 'uiOf', '\\?\\? -> ||', 'const m = info.multiplier ?? 1;', 0):
        'a multiplier is never 0 (token2022.mintFacts refuses it): ?? and || agree',
    ('swap_sleeve', 'amountToRaw', '\\?\\? -> ||', 'const m = info.multiplier ?? 1;', 0):
        'a multiplier is never 0 (token2022.mintFacts refuses it): ?? and || agree',
    ('swap_sleeve', 'uiOf', '\\?(?=\\s) -> && false ?', 'return m === 1 ? toHuman(raw, info.decimals) : rawToUi(raw, info.decimals, m);', 0):
        'rawToUi(raw, d, 1) is Number(raw) * 1 / 10^d: toHuman exactly',
    ('stocks_dlmm', 'unionView', '\\?\\? -> ||', 'const sum = (k) => views.reduce((n, v) => n + (v[k] ?? 0), 0);', 0):
        'every summed key (bins, closeEst, feesAccrued) is a number on every view: never nullish, and 0 || 0 is 0',
    ('stocks_raydium', 'unionView', '\\?\\? -> ||', 'const sum = (k) => views.reduce((n, v) => n + (v[k] ?? 0), 0);', 0):
        'every summed key (closeEst, feesAccrued) is a number on every view: never nullish, and 0 || 0 is 0',
    ('audit_checks', 'lookalike', 'drop operand 1', "a = str(addr or '')", 0): "str(None) is 'None', 4 characters, never over 8: False either way",
    ('audit_checks', 'known_signatures', 'skip if body', 'if \'"signature\' not in line:', 0): 'a cheap prefilter: an object line without the word has no signature key, and non-objects are skipped below',
    ('audit_checks', 'keep_mints', 'drop operand 1', "keep |= {m for m in (bot.pool_record().get('reward_mints') or []) if m}", 0): 'a None reward list raises inside the try, which keeps the set built so far either way',
    ('audit_run', 'run', 'drop operand 1', "sigs = rpc(url, 'getSignaturesForAddress', [owner, params]) or []", 0): "None and [] both take the 'not sigs' return: ok, new 0",
    ('audit_run', 'run', 'const 0->1', "for s in sorted(sigs, key=lambda x: x.get('blockTime') or 0):", 0): 'a missing blockTime sorts first either way: real block times are ~1.8e9, never 0 or 1',
    ('audit_run', 'run', 'drop operand 1', "ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(tx.get('blockTime') or time.time()))", 0): 'time.gmtime(None) is the current time: the same ts',
    ('audit_run', 'run', 'const 0->1', "db.set_audit_value('flows_cursor', max(sigs, key=lambda x: x.get('blockTime') or 0)['signature'])", 0): 'a missing blockTime never wins the max against a real block time (~1.8e9)',
    ('health', 'summary', 'const 2->3', 'order = {TRIPPED: 0, BACKOFF: 1, CLOSED: 2}', 0):
        'the closed rank only has to sort after 0 and 1',
    ('health', 'summary', 'const 2->4', 'order = {TRIPPED: 0, BACKOFF: 1, CLOSED: 2}', 0):
        'the closed rank only has to sort after 0 and 1',
    ('book_lines', 'shareAgrees', '<= -> <', 'return Number.isFinite(lp) && Math.abs(lp / eq * 100 - p) <= 0.2;', 0):
        'only a difference of exactly 0.2 points differs: a float share never lands there',
    ('book_lines', 'healthLine', '\\?\\? -> ||', "return `🩺 health   ` + bad.map(x => `${x.emoji ?? (x.state === 'tripped' ? '🔴' : '🟡')} ${x.key} ${x.fails}x`", 0):
        'health.summary always sets emoji to a non-empty string: ?? and || agree',
    ('health', 'cooldown', 'const 30->31', 'return float(min(base_s * 2 ** min(fails - 1, 30), max_s))', 0):
        'the exponent cap only guards overflow: 2^30 x 600 s is far past MAX_S either way',
    ('health', 'cooldown', 'const 30->60', 'return float(min(base_s * 2 ** min(fails - 1, 30), max_s))', 0):
        'the exponent cap only guards overflow: 2^30 x 600 s is far past MAX_S either way',
    ('surrogate', 'pair_tokens', 'drop operand 1', "parts = [p.strip().upper() for p in str(pair or '').replace('-', '/').split('/')]", 0):
        "str(None) is 'None', one part: refused either way",
    ('surrogate', 'clean_bars', 'swap GtE->Gt', 'out[int(t)] = (t, o, h, l, c, v if math.isfinite(v) and v >= 0 else 0.0)', 0):
        'a volume of exactly 0 becomes 0.0 either way',
    ('surrogate', 'fit_surrogate', 'const 1.0->0.5', 'def fit_surrogate(bars, live_price, ref=None, range_scale=1.0):', 0):
        'the scale is clamped at 1.0: a default below 1 is the identity',
    ('surrogate', 'fit_surrogate', 'const 1.0->0.0', 'def fit_surrogate(bars, live_price, ref=None, range_scale=1.0):', 0):
        'the scale is clamped at 1.0: a default below 1 is the identity',
    ('surrogate', 'fit_surrogate', 'const 1.0->1.5', 'if range_scale != 1.0:', 0):
        'scale 1.0 through the formula is the identity: skipping it or not gives the same bars',
    ('surrogate', 'fit_surrogate', 'const 1.0->0.5', 'if range_scale != 1.0:', 0):
        'scale 1.0 through the formula is the identity: skipping it or not gives the same bars',
    ('surrogate', 'fit_surrogate', 'const 1.0->0.0', 'if range_scale != 1.0:', 0):
        'scale 1.0 through the formula is the identity: skipping it or not gives the same bars',
    ('surrogate', 'fit_surrogate', 'drop operand 4', 'if bars is None or not len(bars[0]) or not live_price or not math.isfinite(live_price) or live_price <= 0:', 0):
        'a negative live price fails both level checks: None either way',
    ('surrogate', 'fit_surrogate', 'swap LtE->Lt', 'if bars is None or not len(bars[0]) or not live_price or not math.isfinite(live_price) or live_price <= 0:', 0):
        'a zero live price is caught by `not live_price` first',
    ('surrogate', 'fit_surrogate', 'swap Gt->GtE', 'if abs(c[-1] / live_price - 1) > SURROGATE_MATCH:', 0):
        'only a close exactly 0.5% off differs: a float never lands there',
    ('surrogate', 'fit_surrogate', 'swap Gt->GtE', 'if abs((1 / c[-1]) / live_price - 1) > SURROGATE_MATCH:', 0):
        'only a close exactly 0.5% off differs: a float never lands there',
    ('surrogate', 'fit_surrogate', 'swap Gt->GtE', 'if len(d) >= SURROGATE_JOIN_MIN and float(np.median(d)) > SURROGATE_BASIS_MAX:', 0):
        'only a median exactly 0.2% differs: a float never lands there',
    ('surrogate', 'fit_surrogate', 'drop operand 1', 'if ref is not None and len(ref[0]):', 0):
        'an empty reference gives no overlap: the join check is skipped either way',
    ('surrogate', 'fit_surrogate', 'const 0->1', 'if ref is not None and len(ref[0]):', 0):
        'ref[0] and ref[1] have the same length',
    ('surrogate', 'binance_5m', 'and<->or', 'if not isinstance(d, list) or not d:', 0):
        'a dict, a string or [] yields no row and stops at the short-page check: [] either way',
    ('surrogate', 'binance_5m', 'drop operand 0', 'if not isinstance(d, list) or not d:', 0):
        'a dict or a string yields no row and stops at the short-page check: [] either way',
    ('surrogate', 'binance_5m', 'drop operand 1', 'if not isinstance(d, list) or not d:', 0):
        '[] yields no row and stops at the short-page check',
    ('surrogate', 'surrogate_5m', 'skip if body', 'if toks is None:', 0):
        'symbols(*None) raises inside the try, which skips every source: (None, None), no fetch',
    ('surrogate', 'missing_slots', 'const 1->2', 'return [s for s in range(first, last + 1, BAR_SECONDS) if s not in have]', 0):
        'the range end is exclusive on a 300 s step: last + 1 and last + 2 give the same slots',
    ('guards', 'fee_read_problem', 'drop operand 1', 'if not isinstance(pos, (int, float)) or not math.isfinite(pos) or pos <= 0 or usd is None:', 0):
        'not isfinite(pos): a NaN or infinite position fails every later comparison, so the verdict is None either way',
    ('txfees', 'fetch', 'const 1->0', "body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'getTransaction',", 0):
        'the JSON-RPC request id is arbitrary',
    ('txfees', 'fetch', 'const 1->2', "body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'getTransaction',", 0):
        'the JSON-RPC request id is arbitrary',
    ('distribute', 'distribute', 'swap Gt->GtE', 'if rest > 1e-9:', 0):
        'a remainder of exactly 1e-9 is not reachable in float and is below any token resolution',
    ('read_status', 'read_status', 'drop operand 1', "if not (out and out.get('positionMint')):", 0):
        'an answer without a position carries no fee figures, so the check finds nothing either way',
    ('risk', 'chi2_sf_even', 'const 0->1', 'if k <= 0 or k % 2:', 0):
        'k = 1 is odd and refused by the next test either way',
    ('risk', 'chi2_sf_even', 'swap LtE->Lt', 'if x <= 0:', 0):
        'at x = 0 the series gives exp(0) * 1 = 1.0, the same value',
    ('risk', 'chi2_sf_even', 'const 1.0->1.5', 'return min(1.0, math.exp(-h) * total)', 0):
        'exp(-h) times a partial sum of e^h is below 1 for h > 0: the clamp never binds',
    ('fees', 'split', 'swap Lt->LtE', 'if amt < 0:', 0):
        'a zero fee skipped or split adds no row and takes no gas: add() drops zero',
    ('orca_fees', 'feesFromOrcaSnapshot', '\\?\\? -> ||', 'return arr.ticks[getTickIndexInArray(tick, start, s)] ?? null;', 0):
        'a tick is an object or undefined: ?? and || agree',
    ('orca_fees', 'feesFromOrcaSnapshot', '\\?\\? -> ||', 'return { ok: false, reason: `the SDK quote failed: ${e?.message ?? e}`, ...fallback };', 0):
        'an error message is never empty: ?? and || agree',
    ('fee_snapshot', 'decodeSnapshot', '\\?(?=\\s) -> && false ?', 'if (a && a.owner.equals(programId)) arrays.set(starts[i], { data: a.data, key: arrayKeys ? arrayKeys[i] : null });', 0):
        'the array key is only echoed back inside the parsed container; no fee depends on it',
    ('capital_db', 'record_flow', "sql 'on conflict (signature) do nothing' -> 'on conflict do nothing'", "'values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (signature) do nothing',", 0):
        'the only other unique index is one baseline per book (020), and record_flow refuses kind baseline',
    ('janitor_js', 'planClose', '\\?\\? -> ||', "!(a.info.extensions || []).some(e => e.extension === 'transferFeeAmount' && Number(e.state?.withheldAmount ?? 0) > 0));", 0):
        'a withheld amount is a number or absent: ?? and || agree',
    ('idle_capital', 'idle_to_deploy', 'const 0.0->1.0', 'return deployable_usd > max(audit.IDLE_ABS_USD, audit.IDLE_SHARE * (equity_usd or 0.0))', 0):
        'the $2 floor is above 2% of $1: a missing equity gives the floor either way',
    ('idle_capital', 'plan_sweep', 'drop operand 0', "if a['amount'] <= 0 or m in pool_mints or m in reward_mints or m == fees.NATIVE_MINT:", 0):
        'an empty account is worth $0, under the $1 dust gate either way',
    ('idle_capital', 'plan_sweep', 'swap LtE->Lt', "if a['amount'] <= 0 or m in pool_mints or m in reward_mints or m == fees.NATIVE_MINT:", 0):
        'an empty account is worth $0, under the $1 dust gate either way',
    ('idle_capital', 'plan_sweep', 'const 0->1', "if a['amount'] <= 0 or m in pool_mints or m in reward_mints or m == fees.NATIVE_MINT:", 0):
        'one raw unit of a token worth over $1 per raw unit does not exist among verified tokens; the dust gate decides',
    ('idle_capital', 'plan_sweep', 'drop operand 1', "'symbol': (facts.get(m) or {}).get('symbol')})", 0):
        'the symbol is read only after facts were required to exist',
    ('idle_capital', 'sweep_foreign', 'skip if body', 'if not others:', 0):
        'an empty list plans nothing either way; the early return only saves the price calls',
    ('loop_hooks', 'run_audits', 'const 0->1', "if time.time() - state.get('last_audit', 0) < AUDIT_EVERY_S:", 0):
        'a first audit is due either way: time.time() is far past 3,601 s',
    ('loop_hooks', 'janitor', 'drop operand 1', 'if err or not plan:', 0):
        'a None plan raises inside the try, which reports janitor_failed exactly as the guard does',
    ('loop_hooks', 'janitor', 'drop operand 1', 'if err or not plan:', 1):
        'a None re-plan raises inside the try, which reports janitor_failed exactly as the guard does',
    # measured_fees: the disagreement check only decides whether a disagreement is
    # notified; the figures booked do not depend on it.
    ('measured', 'measured_fees', 'swap Gt->GtE', 'if abs(musd - (usd or 0.0)) > max(0.01, 0.05 * musd):', 0):
        'report-only: decides whether a disagreement is notified',
    ('measured', 'measured_fees', 'const 0.01->0.015', 'if abs(musd - (usd or 0.0)) > max(0.01, 0.05 * musd):', 0):
        'report-only: decides whether a disagreement is notified',
    ('measured', 'measured_fees', 'const 0.01->0.005', 'if abs(musd - (usd or 0.0)) > max(0.01, 0.05 * musd):', 0):
        'report-only: decides whether a disagreement is notified',
    ('measured', 'measured_fees', 'const 0.01->0.0', 'if abs(musd - (usd or 0.0)) > max(0.01, 0.05 * musd):', 0):
        'report-only: decides whether a disagreement is notified',
    ('measured', 'measured_fees', 'const 0.05->0.025', 'if abs(musd - (usd or 0.0)) > max(0.01, 0.05 * musd):', 0):
        'report-only: decides whether a disagreement is notified',
    ('measured', 'measured_fees', 'const 0.0->1.0', 'if abs(musd - (usd or 0.0)) > max(0.01, 0.05 * musd):', 0):
        'report-only: decides whether a disagreement is notified',
    ('measured', 'measured_fees', 'const 0.05->0.07500000000000001', 'if abs(musd - (usd or 0.0)) > max(0.01, 0.05 * musd):', 0):
        'report-only: decides whether a disagreement is notified',
    ('measured', 'measured_fees', 'const 0.01->1.0', 'if abs(musd - (usd or 0.0)) > max(0.01, 0.05 * musd):', 0):
        'report-only: decides whether a disagreement is notified',
    ('measured', 'measured_fees', 'const 0.05->1.0', 'if abs(musd - (usd or 0.0)) > max(0.01, 0.05 * musd):', 0):
        'report-only: decides whether a disagreement is notified',
    ('measured', 'measured_fees', 'const 0.05->0.0', 'if abs(musd - (usd or 0.0)) > max(0.01, 0.05 * musd):', 0):
        'report-only: decides whether a disagreement is notified',
    # Float-noise tolerances: the scale of the threshold is a choice between
    # "exactly no variance" and any real variance, many decades apart. A mutant
    # that moves it by 2x cannot change a result; moving it to 0 or 1 can, and
    # those mutants are killed by tests.
    ('risk', 'arch_lm', 'const 1e-12->1.5e-12', 'if ss_tot <= 1e-12 * float((y * y).sum()):        # no variance, up to float noise', 0):
        'a float-noise tolerance: 2x on its scale changes nothing',
    ('risk', 'arch_lm', 'const 1e-12->5e-13', 'if ss_tot <= 1e-12 * float((y * y).sum()):        # no variance, up to float noise', 0):
        'a float-noise tolerance: 2x on its scale changes nothing',
    ('risk', 'risk_metrics', 'const 1e-06->1.5e-06', 'tiny = 1e-6 * float(np.mean(e))                  # float noise, not variance', 0):
        'a float-noise tolerance: 2x on its scale changes nothing',
    ('risk', 'risk_metrics', 'const 1e-06->5e-07', 'tiny = 1e-6 * float(np.mean(e))                  # float noise, not variance', 0):
        'a float-noise tolerance: 2x on its scale changes nothing',
    ('risk', 'risk_metrics', 'const 1e-06->1.5e-06', 'if sd > 1e-6 * rms24:', 0):
        'a float-noise tolerance: 2x on its scale changes nothing',
    ('risk', 'risk_metrics', 'const 1e-06->5e-07', 'if sd > 1e-6 * rms24:', 0):
        'a float-noise tolerance: 2x on its scale changes nothing',
    # Signer send paths: `e?.message ?? e` only builds the text of an error.
    ('rpc_raydium', 'sendAll', '\\?\\? -> ||', 'signatures: sigs, error: String(e?.message ?? e).slice(0, 300) }, null, 1));', 0):
        'error-message text only: ?? and || differ only for an error with an empty message',
    ('rpc_raydium', 'sendAll', '\\?\\? -> ||', 'throw Object.assign(new Error(`partial send: ${sigs.length}/${builts.length} sent; ${e?.message ?? e}`), { sent: true });', 0):
        'error-message text only: ?? and || differ only for an error with an empty message',
    ('rpc_dlmm', 'sendAll', '\\?\\? -> ||', 'if (!sigs.length) throw new AfterSignError(`send failed after signing (not retried): ${e?.message ?? e}`);', 0):
        'error-message text only: ?? and || differ only for an error with an empty message',
    ('rpc_dlmm', 'sendAll', '\\?\\? -> ||', 'return { sigs, error: String(e?.message ?? e).slice(0, 300) };', 0):
        'error-message text only: ?? and || differ only for an error with an empty message',
    ('rpc_pancake', 'sendOne', '\\?\\? -> ||', 'throw new AfterSignError(`send failed after signing (not retried): ${e?.message ?? e}`);', 0):
        'error-message text only: ?? and || differ only for an error with an empty message',
    ('rpc_byreal', 'sendAll', '\\?\\? -> ||', 'return { sigs, error: String(e?.message ?? e).slice(0, 300) };', 0):
        'error-message text only: ?? and || differ only for an error with an empty message',
    ('rpc_byreal', 'confirm', '\\?\\? -> ||', 'if (!s || !s.confirmationStatus) throw new Error(`${sig} not confirmed: ${String(e?.message ?? e).slice(0, 120)}`);', 0):
        'error-message text only: ?? and || differ only for an error with an empty message',
    ('rpc_orca', 'sendOnce', '\\?\\? -> ||', 'throw new AfterSignError(`send failed after signing (not retried): ${e?.message ?? e}`);', 0):
        'error-message text only: ?? and || differ only for an error with an empty message',
    # swap_orca.mjs
    ('swap_orca', 'planSwap', '(?<![<>=!-])>(?![>=]) -> >=', 'const raw = amount > 0 ? BigInt(Math.floor(amount * 10 ** sellInfo.decimals)) : 0n;', 0):
        'only amount 0 (or -0) differs, and BigInt(Math.floor(0)) is 0n, the same as the else branch',
    ('swap_orca', 'chooseCuPrice', '\\?\\? -> ||', 'const fees = (recent ?? []).map(r => Number(r.prioritizationFee ?? r)).filter(f => f > 0).sort((a, b) => a - b);', 1):
        'a falsy fee (0, empty string, false, NaN) is dropped by f > 0, and the fallback Number(plain object) is NaN, dropped too',
    ('swap_orca', 'verifyTxShape', '\\?\\? -> ||', "if ((msg.addressTableLookups ?? []).length) throw new Refused('transaction uses lookup tables; refusing');", 0):
        'for every falsy non-nullish value v, v.length is 0 or undefined: falsy, like [].length',
    ('swap_orca', 'verifyTxShape', '\\?\\? -> ||', "if (pid === undefined || !ALLOWED_PROGRAMS.has(pid)) throw new Refused(`transaction calls ${pid ?? 'a program from a lookup table'}; refusing`);", 0):
        'error text only, and pid is undefined or a base58 key from toBase58, never the empty string',
    # jupgate.py
    ('jupgate', 'reserve', 'drop operand 1', 'last = float(fh.read().strip() or 0.0)', 0):
        "float('') raises ValueError, which the except turns into last = 0.0: the same slot",
    # record_health, balance_wallet (one outcome per operation, 2026-10-01)
    ('one_outcome', 'record_health', 'flip bool', 'f"retry in {wait / 60:.0f} min: {err}", flush=True)', 0):
        'print flush only',
    ('one_outcome', 'balance_wallet', 'drop operand 0', "if out and out.get('noop'):", 0):
        'out is truthy here: a falsy out took the nothing-sent return above',
    ('one_outcome', 'balance_wallet', 'drop operand 1', "if err or not out or out.get('partial') or not out.get('sent'):", 0):
        'out is truthy here: a falsy out took the nothing-sent return above',
    ('one_outcome', 'balance_wallet', 'drop operand 1', "ra, rb = rec.get('token_a') or {}, rec.get('token_b') or {}", 0):
        'token_a is a dict here: the mint guard above returned unless it holds a valid address',
    ('one_outcome', 'balance_wallet', 'drop operand 1', "ra, rb = rec.get('token_a') or {}, rec.get('token_b') or {}", 1):
        'token_b is a dict here: the mint guard above returned unless it holds a valid address',
    ('one_outcome', 'balance_wallet', 'and<->or', "if ra.get('decimals') is not None and rb.get('decimals') is not None:", 0):
        'a missing or None decimal makes int(...) raise KeyError or TypeError, which the except turns into hints = {}',
    ('one_outcome', 'balance_wallet', 'drop operand 0', "if ra.get('decimals') is not None and rb.get('decimals') is not None:", 0):
        'a missing or None decimal makes int(...) raise KeyError or TypeError, which the except turns into hints = {}',
    ('one_outcome', 'balance_wallet', 'drop operand 1', "if ra.get('decimals') is not None and rb.get('decimals') is not None:", 0):
        'a missing or None decimal makes int(...) raise KeyError or TypeError, which the except turns into hints = {}',
    ('one_outcome', 'balance_wallet', 'drop operand 0', "if not ((err or not out) and not (out or {}).get('signature') and not (out or {}).get('partial')", 0):
        "(err or not out) is false only for a falsy err, and str() of a falsy err ('None', '') never matches the transport regex",
    ('one_outcome', 'balance_wallet', 'drop operand 1', "if not ((err or not out) and not (out or {}).get('signature') and not (out or {}).get('partial')", 1):
        "err alone differs only for a falsy err, and str() of a falsy err ('None', '') never matches the transport regex",
    # Base / Aerodrome Slipstream (EVM). Each is a boundary where both sides compute the same value.
    ('evm_math', 'sqrtRatioAtTick', '(?<![<>=!])<(?![<=]) -> <=', 'const t = BigInt(tick < 0 ? -tick : tick);', 0):
        '-0 is 0: tick 0 gives t = 0n either way',
    ('evm_math', 'sqrtRatioAtTick', '(?<![<>=!-])>(?![>=]) -> >=', 'if (tick > 0) r = MAX_UINT256 / r;', 0):
        'at tick 0, r = 2^128 and MAX_UINT256 / 2^128 = 2^128 - 1, which rounds up to the same 2^96 (pinned by a test)',
    ('evm_math', 'amountsForLiquidity', '(?<![<>=!])<(?![<=]) -> <=', 'if (sp < sb) return [amount0Delta(sp, sb, L, roundUp), amount1Delta(sa, sp, L, roundUp)];', 0):
        'at sp == sb the middle branch gives amount0Delta(sb, sb) = 0 and amount1Delta(sa, sb): the same pair',
    ('evm_math', 'amountsForLiquidity', '<= -> <', 'if (sp <= sa) return [amount0Delta(sa, sb, L, roundUp), 0n];', 0):
        'at sp == sa the middle branch gives amount0Delta(sa, sb) and amount1Delta(sa, sa) = 0: the same pair',
    ('evm_math', 'liquidityForAmounts', '(?<![<>=!])<(?![<=]) -> <=', 'return l0 < l1 ? l0 : l1;', 0):
        'the minimum of two equal values is either one',
    ('evm_math', 'depositFor', '(?<![<>=!])<(?![<=]) -> <=', 'return { liquidity: L, amountA: a < capA ? a : capA, amountB: b < capB ? b : capB };', 0):
        'a == capA returns a or capA: the same value',
    ('evm_math', 'depositFor', '(?<![<>=!])<(?![<=]) -> <=', 'return { liquidity: L, amountA: a < capA ? a : capA, amountB: b < capB ? b : capB };', 1):
        'b == capB returns b or capB: the same value',
    ('evm_math', 'wrapPlan', '(?<![<>=!-])>(?![>=]) -> >=', 'const wrap = need > weth ? need - weth : 0n;', 0):
        'need == weth gives need - weth = 0n, the same as the else branch',
    ('evm_math', 'wrapPlan', '(?<![<>=!-])>(?![>=]) -> >=', 'const spendable = eth > reserve ? eth - reserve : 0n;', 0):
        'eth == reserve gives eth - reserve = 0n, the same as the else branch',
    ('evm_math', 'capped', '(?<![<>=!])<(?![<=]) -> <=', 'return cap == null || raw < cap ? raw : cap;', 0):
        'raw == cap returns raw or cap: the same value',
    ('evm_signer', 'spendable', '(?<![<>=!-])>(?![>=]) -> >=', 'const above = h.eth > cfg.gasReserve ? h.eth - cfg.gasReserve : 0n;', 0):
        'eth == reserve gives eth - reserve = 0n, the same as the else branch',
    ('evm_signer', 'simulateSequence', '\\?\\? -> ||', 'blocks: [{ calls: steps.map(s => ({ from: me, to: s.to, data: s.data, value: s.value ?? 0n })) }],', 0):
        'a step value is a bigint or absent: 0n || 0n and undefined || 0n are both 0n',
    ('evm_signer', 'simulateSequence', '\\?\\? -> ||', 'await pub.call({ account: me, to: s.to, data: s.data, value: s.value ?? 0n });', 0):
        'a step value is a bigint or absent: 0n || 0n and undefined || 0n are both 0n',
    ('evm_signer', 'simulateSequence', '\\?\\? -> ||', "label: steps[i].label, ok: c.status === 'success', gasUsed: c.gasUsed?.toString() ?? null,", 0):
        'gasUsed?.toString() is a non-empty digit string or undefined: never falsy but nullish',
    ('evm_signer', 'checkRecipient', '\\?\\? -> ||', "if (!isAddress(String(to ?? ''), { strict: false })) throw new Error(`refused: destination ${to} is not an address`);", 0):
        "every falsy `to` ('' , 0, false, null) fails isAddress either way",
    ('evm_signer', 'ownPositions', '(?<![<>=!])<(?![<=]) -> <=', '.sort((x, y) => (x.tokenId < y.tokenId ? -1 : 1));', 0):
        'token ids of one NPM are distinct: the comparator never sees two equal ids',
    ('evm_rpc', 'evmErrorKind', '(?<![<>=!])<(?![<=]) -> <=', 'for (let x = e, depth = 0; x && depth < 8; x = x.cause, depth++) {', 0):
        'the bound only stops a cyclic cause chain; viem chains are at most 4 deep, so 8 or 9 links walk the same errors',
    ('evm_rpc', 'evmErrorKind', '\\?\\? -> ||', 'const name = x.name ?? x.constructor?.name;', 0):
        'an Error always has a non-empty name: never falsy where it is not nullish',
    ('deploy_idle', 'idle_to_deploy', 'const 0.0->1.0', 'return deployable_usd > max(audit.IDLE_ABS_USD, audit.IDLE_SHARE * (equity_usd or 0.0))', 0):
        'with no equity, IDLE_SHARE x 1.0 is $0.02, under the $2 IDLE_ABS_USD floor: max() gives the floor either way',
}


def share_equivalents(table, function, source, targets):
    """Copy `source`'s entries for `function` to each of `targets` that mutate
    the same function. Equivalence is a fact of the code, not of the target:
    one reason, checked once, holds wherever the same mutant is generated."""
    for (t, f, d, line, occ), why in list(table.items()):
        if t == source and f == function:
            for other in targets:
                table.setdefault((other, f, d, line, occ), why)


share_equivalents(EQUIVALENT, 'balance_wallet', 'one_outcome', ('deploy_all', 'deploy_idle'))
share_equivalents(EQUIVALENT, 'deploy_idle', 'resilience', ('deploy_idle', 'idle_capital'))
share_equivalents(EQUIVALENT, 'with_surrogate', 'quiet_overlay', ('surrogate_overlay',))

# Old-style entries (target, function, 'description @L<line>'). They did not
# resolve to exactly one mutant when the keys were migrated on 2026-09-30:
# the line moved, or two mutants shared the key. They still match during the
# transition, with a warning; each one needs review. For each, find the one
# mutant the reason describes in a report and replace the entry with its key.
EQUIVALENT_OLD = {
    # no older copy has this line. Candidates now: "state['idle_deploys'] = [t for t in (state.get('idle_deploys"
    # no older copy has this line. Candidates now: "recent = [t for t in state.get('calm_times', []) if now - t "
    # no older copy has this line. Candidates now: "recent = [t for t in state.get('calm_times', []) if now - t "
    # no older copy has this line. Candidates now: 'return now - last_any >= config.CALM_MIN_GAP and health.allo' / "last_any = max([state.get('last_rebalance', 0)] + recent)"
    # no older copy has this line. Candidates now: 'return now - last_any >= config.CALM_MIN_GAP and health.allo'
    # no older copy has this line. Candidates now: 8 lines
    # no older copy has this line. Candidates now: 'f"retry in {wait / 60:.0f} min: {err}", flush=True)'
    # no older copy has this line. Candidates now: "recent = [t for t in state.get('calm_times', []) if now - t "
    # no older copy has this line. Candidates now: 'if lower and upper and upper > lower > 0:' / 'if lower and upper and upper > lower > 0:' #1
    ('books', 'regime_at_move', 'swap Gt->GtE @L209'):
        'lower = 0 is caught by `lower and` first; upper = lower gives half 1.0, refused by `upper > lower`',
    # 2 mutants on this line share the old key; it hides all of them. Candidates now: 'return CLOSED, True, 0.0' / "wait = min(max(0.0, float(rec.get('retry_at') or 0.0) - now)" / "wait = min(max(0.0, float(rec.get('retry_at') or 0.0) - now)" #1
    ('health', 'verdict', 'const 0.0->1.0 @L80'):
        'retry_at None with failures on record: 0.0 or 1.0 are both decades past, allowed either way',
    # no older copy has this line. Candidates now: 'def chain(*args, dex=None, timeout=420, extra_env=None):'
    # no older copy has this line. Candidates now: "v['p_held'] = next((p for w, p in (v.get('probs') or []) if "
    ('books', 'regime_at_move', 'swap Lt->LtE @L215'):
        'widths and probs are both rounded to 2 decimals: a difference is 0 or >= 0.01',
    # no older copy has this line. Candidates now: "v['p_held'] = next((p for w, p in (v.get('probs') or []) if "
    ('books', 'regime_at_move', 'const 1e-06->1.5e-06 @L215'):
        'widths and probs are both rounded to 2 decimals: a difference is 0 or >= 0.01',
    # no older copy has this line. Candidates now: "v['p_held'] = next((p for w, p in (v.get('probs') or []) if "
    ('books', 'regime_at_move', 'const 1e-06->5e-07 @L215'):
        'widths and probs are both rounded to 2 decimals: a difference is 0 or >= 0.01',
    # no older copy has this line. Candidates now: 'if not whole or whole <= 0:'
    ('deployment', '_pct', 'swap LtE->Lt @L946'):
        'a whole of exactly 0 is caught by `not whole` first',
    # 2 mutants on this line share the old key; it hides all of them. Candidates now: 5 lines
    ('surrogate', 'fit_surrogate', 'const 0->1 @L135'):
        'bars[0] and bars[1] have the same length (the <= 0 -> <= 1 twin is killed by the sub-1 price test)',
    # older copies give different lines. Candidates now: 19 lines
    ('distribute', 'distribute', 'drop operand 1 @L578'):
        'split never yields a part of amount 0, so the zero-amount guard of the price cannot bind',
    # in an older copy, 2+ mutants shared this key on the line. Candidates now: 'r2 = min(max(1.0 - ss_res / ss_tot, 0.0), 1.0)' / 'r2 = min(max(1.0 - ss_res / ss_tot, 0.0), 1.0)' #1
    ('risk', 'arch_lm', 'const 1.0->1.5 @L353'):
        'an OLS R^2 with an intercept lies in [0, 1]: the clamp never binds',
    # in an older copy, 2+ mutants shared this key on the line. Candidates now: 'r2 = min(max(1.0 - ss_res / ss_tot, 0.0), 1.0)' / 'r2 = min(max(1.0 - ss_res / ss_tot, 0.0), 1.0)' #1
    ('risk', 'arch_lm', 'const 1.0->0.5 @L353'):
        'an OLS R^2 with an intercept lies in [0, 1]: the clamp never binds',
    # in an older copy, 2+ mutants shared this key on the line. Candidates now: 6 lines
    ('measured', 'measured_fees', 'drop operand 1 @L635'):
        'out None: the mutant raises inside the try, which falls back exactly as the original does',
    # in an older copy, 2+ mutants shared this key on the line. Candidates now: 4 lines
    ('risk', 'risk_metrics', 'swap Gt->GtE @L392'):
        'exact float equality with the tolerance; on a flat tape the other operand still refuses',
    # no older copy has this line. Candidates now: "return {'day': day.isoformat(), 'complete': now() >= end,"
    ('daily', 'daily_line', 'swap GtE->Gt @L804'):
        'only the exact instant of midnight differs; now() is never that instant in a test or a poll',
    # no older copy has this line. Candidates now: 'if \'"signature\' not in line:' / "if r.get('signature'):"
    # no older copy has this line. Candidates now: "keep |= set(st.get('reward_mints_seen') or []) | set(st.get(" / "keep |= set(st.get('reward_mints_seen') or []) | set(st.get(" #1 / "keep |= {m for m in (bot.pool_record().get('reward_mints') o"
    # in an older copy, 2+ mutants shared this key on the line. Candidates now: 7 lines
    ('loop_hooks', 'janitor', 'drop operand 1 @L1159'):
        'a None close answer raises inside the try, which reports janitor_failed exactly as the guard does (the unsigned-answer mutant on this line is killed)',
    # older copies give different lines. Candidates now: 6 lines
    ('measured', 'measured_fees', 'drop operand 1 @L651'):
        'report-only: decides whether a disagreement is notified',
}
EQUIVALENT.update(EQUIVALENT_OLD)


# --- Python mutants ---------------------------------------------------------------

CMP_SWAP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
            ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
            ast.In: ast.NotIn, ast.NotIn: ast.In}
CMP_FLIP = {ast.Lt: ast.Gt, ast.LtE: ast.GtE, ast.Gt: ast.Lt, ast.GtE: ast.LtE}
BIN_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult,
            ast.FloorDiv: ast.Mult, ast.Pow: ast.Mult, ast.Mod: ast.FloorDiv}
NAME_SWAP = {'min': 'max', 'max': 'min', 'any': 'all', 'all': 'any'}


def py_mutants(src, functions):
    """(function, description, line, legacy description, mutated source) for
    every mutant of `functions`. Only the function is re-generated; the rest of
    the file is spliced back byte for byte."""
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    fns = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in functions]
    out = []
    for fn in fns:
        start = fn.lineno - 1 - len(fn.decorator_list)
        head, tail = ''.join(lines[:start]), ''.join(lines[fn.end_lineno:])
        indent = re.match(r'\s*', lines[fn.lineno - 1]).group(0)
        nodes = [n for n in ast.walk(fn) if n is not fn]
        skip = report_only(fn)
        for idx, node in enumerate(nodes):
            if id(node) in skip:
                continue
            for desc, apply in _node_mutations(node):
                f2 = copy.deepcopy(fn)
                target = [n for n in ast.walk(f2) if n is not f2][idx]
                if apply(target) is False:
                    continue
                try:
                    body = ast.unparse(ast.fix_missing_locations(f2))
                except Exception:
                    continue
                body = ''.join(indent + l if l.strip() else l for l in body.splitlines(keepends=True))
                line = getattr(node, 'lineno', None)
                out.append((fn.name, desc, line, f'{desc} @L{line or "?"}', head + body + '\n' + tail))
    return out


# Code that only reports: what a notification, an event line, a formatted
# message or a sleep says cannot change the book. No mutants there.
REPORT_CALLS = {'notify', 'notify_book', 'tidy', 'event', 'sleep'}


def report_only(fn):
    ids = set()
    for n in ast.walk(fn):
        f = getattr(n, 'func', None)
        name = getattr(f, 'id', None) or getattr(f, 'attr', None)
        if (isinstance(n, ast.Call) and name in REPORT_CALLS) or isinstance(n, ast.JoinedStr):
            ids |= {id(x) for x in ast.walk(n)}
        if isinstance(n, ast.Call) and name == 'round' and len(n.args) > 1:
            ids |= {id(x) for x in ast.walk(n.args[1])}
    return ids


def _node_mutations(n):
    m = []
    if isinstance(n, ast.Compare):
        for i, op in enumerate(n.ops):
            for table, word in ((CMP_SWAP, 'swap'), (CMP_FLIP, 'flip')):
                if type(op) in table:
                    new = table[type(op)]
                    def ap(t, i=i, new=new):
                        t.ops[i] = new()
                    m.append((f'{word} {type(op).__name__}->{new.__name__}', ap))
    elif isinstance(n, ast.BinOp) and type(n.op) in BIN_SWAP:
        new = BIN_SWAP[type(n.op)]
        def ap(t, new=new):
            t.op = new()
        m.append((f'{type(n.op).__name__}->{new.__name__}', ap))
    elif isinstance(n, ast.BoolOp):
        def ap(t):
            t.op = ast.Or() if isinstance(t.op, ast.And) else ast.And()
        m.append(('and<->or', ap))
        for i in range(len(n.values)):
            def drop(t, i=i):
                if len(t.values) < 2:
                    return False
                del t.values[i]
            m.append((f'drop operand {i}', drop))
    elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
        def drop_not(t):
            t.op = ast.UAdd()                  # +x keeps x's truth: `not` removed
        m.append(('drop not', drop_not))
    elif isinstance(n, ast.If):
        def neg(t):
            t.test = ast.UnaryOp(op=ast.Not(), operand=t.test)
        m.append(('negate if', neg))
        if not n.orelse:
            def skip(t):
                t.body = [ast.Pass()]
            m.append(('skip if body', skip))
    elif isinstance(n, ast.Return) and n.value is not None:
        def none(t):
            if isinstance(t.value, ast.Constant) and t.value.value is None:
                return False
            t.value = ast.Constant(None)
        m.append(('return None', none))
    elif isinstance(n, ast.Constant) and isinstance(n.value, bool):
        def flip(t):
            t.value = not t.value
        m.append(('flip bool', flip))
    elif isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
        v = n.value
        cands = ({0: [1], 1: [0, 2]}.get(v) or [v + 1, v * 2]) if isinstance(v, int) else [v * 1.5, v / 2, 0.0, 1.0]
        for new in [c for c in dict.fromkeys(cands) if c != v]:
            def ap(t, new=new):
                t.value = new
            m.append((f'const {v!r}->{new!r}', ap))
    elif isinstance(n, ast.Name) and n.id in NAME_SWAP and isinstance(getattr(n, 'ctx', None), ast.Load):
        def ap(t):
            t.id = NAME_SWAP[t.id]
        m.append((f'{n.id}->{NAME_SWAP[n.id]}', ap))
    elif isinstance(n, ast.AugAssign) and type(n.op) in BIN_SWAP:
        new = BIN_SWAP[type(n.op)]
        def ap(t, new=new):
            t.op = new()
        m.append((f'aug {type(n.op).__name__}->{new.__name__}', ap))
    return m


# --- JavaScript mutants -------------------------------------------------------------

JS_SWAPS = [
    (r'(?<![<>=!])<(?![<=])', '<='), (r'<=', '<'), (r'(?<![<>=!-])>(?![>=])', '>='), (r'>=', '>'),
    (r'===', '!=='), (r'!==', '==='), (r'&&', '||'), (r'\|\|', '&&'),
    (r'\.gte\(', '.gt('), (r'\.lt\(', '.lte('), (r'\.add\(', '.sub('), (r'\.sub\(', '.add('),
    (r'\.isNeg\(\)', '.isZero()'), (r'\.isZero\(\)', '.isNeg()'), (r'\.eq\(', '.gt('),
    (r'\bshln\(128\)', 'shln(127)'), (r'\bshln\(127\)', 'shln(126)'), (r'\bshln\(64\)', 'shln(63)'),
    (r'\btries = 3\b', 'tries = 1'), (r'\bi \+ 1 < tries\b', 'i + 1 <= tries'),
    (r'return null;', "return 'mutant';"), (r'\bok: !reason\b', 'ok: true'), (r'\bif \(!', 'if ('),
    (r'\bnew BN\(0\)', 'new BN(1)'), (r'\.umod\(U128\)', ''), (r'd\.add\(U128\)', 'd'),
    (r'\bslice\(0, nftMints\.length\)', 'slice(0, nftMints.length + 1)'),
    (r'\bslice\(nftMints\.length\)', 'slice(nftMints.length + 1)'),
    (r'(?<![!=])!(?!=)', ''), (r'\bok: false\b', 'ok: true'), (r'\bi\+\+', 'i += 2'),
    (r'>= 0n', '> 0n'), (r'> 0n', '>= 0n'), (r'< 0n', '<= 0n'), (r'\b64n\b', '63n'), (r'\?\?', '||'),
    (r'\?(?=\s)', '&& false ?'), (r'\+ U128\) % U128', ') % U128'),
    (r'\bfeeA\b(?=[,}])', 'feeB'), (r'\.mod\(U128\)', ''),
]


def js_functions(src, names):
    """{name: (start, end)} character spans of top-level functions."""
    spans = {}
    for name in names:
        m = re.search(rf'^(export )?(async )?function {name}\b', src, re.M)
        if not m:
            raise SystemExit(f'function {name} not found')
        i = src.index('{', m.end())
        depth = 0
        for j in range(i, len(src)):
            if src[j] == '{':
                depth += 1
            elif src[j] == '}':
                depth -= 1
                if depth == 0:
                    spans[name] = (m.start(), j + 1)
                    break
    return spans


def js_mutants(src, functions):
    out = []
    for name, (a, b) in js_functions(src, functions).items():
        body = src[a:b]
        for pat, rep in JS_SWAPS:
            for m in re.finditer(pat, body):
                # skip comments and arrow functions
                line_start = body.rfind('\n', 0, m.start()) + 1
                line = body[line_start:body.find('\n', m.start()) if '\n' in body[m.start():] else len(body)]
                if line.lstrip().startswith('//') or body[m.start():m.start() + 2] == '=>':
                    continue
                if '//' in body[line_start:m.start()]:
                    continue                                   # inside a trailing comment
                if pat.startswith('(?<![<>=!-])>') and body[m.start() - 1:m.start() + 1] == '=>':
                    continue
                new = body[:m.start()] + rep + body[m.end():]
                lineno = src[:a + m.start()].count('\n') + 1
                out.append((name, f'{pat} -> {rep}', lineno, f'{pat} -> {rep} @L{lineno}', src[:a] + new + src[b:]))
    return out


# --- SQL mutants: text swaps inside the SQL a Python function runs -----------------

SQL_SWAPS = [
    (' asc', ' desc'), (' desc', ' asc'), ('avg(', 'max('), ('max(', 'min('), ('count(*) polls', '(count(*) + 1) polls'),
    ('coalesce(closed_at, %(at)s)', 'coalesce(%(at)s, closed_at)'),
    ('between p.opened_at and p.end_at', "between p.opened_at - interval '1 hour' and p.end_at"),
    ('between p.opened_at and p.end_at', "between p.opened_at and p.end_at + interval '2 hours'"),
    ('then 1.0 else 0.0', 'then 0.0 else 1.0'), ('n desc, mode', 'n asc, mode'), ('3600.0', '60.0'),
    ('coalesce(band_profile.rebalanced_at, excluded.rebalanced_at)', 'coalesce(excluded.rebalanced_at, band_profile.rebalanced_at)'),
    ('coalesce(excluded.exit_reason, band_profile.exit_reason)', 'coalesce(band_profile.exit_reason, excluded.exit_reason)'),
    ("= 'rebalance' and p.closed", "= 'rebalance' or p.closed"), ("= 'rebalance' then", "= 'harvest' then"),
    ('r.mint = p.mint', 'r.pool = r.pool'), ('sn.mint = p.mint', 'sn.pool = sn.pool' if False else 'true'),
    ('where mint = %(mint)s),', 'where true),'), ('sum(fee_usd)', 'max(fee_usd)'), ('abs(velocity)', 'velocity'),
    ('nullif(agg.polls, 0)', 'nullif(agg.polls + 1, 0)'), ('limit 1) mode_main', 'offset 1 limit 1) mode_main'),
    ('ts >= %(s)s and ts < %(e)s', 'ts > %(s)s and ts <= %(e)s'), ('opened_at < %(e)s', 'opened_at <= %(e)s + interval \'1 day\''),
    ("kind in ('paid', 'uncertain')", "kind in ('paid')"), ("kind in ('paid', 'uncertain')", "kind in ('paid', 'uncertain', 'owed')"),
    ('equity_usd is not null', 'true'), ('sum(fee_usd)', 'avg(fee_usd)'),
    ("when kind = 'deposit' then usd else -usd", "when kind = 'deposit' then usd else usd"),
    ("when kind = 'deposit' then sol else -sol", "when kind = 'deposit' then sol else sol"),
    ("when kind = 'deposit' then usdc else -usdc", "when kind = 'deposit' then usdc else usdc"),
    ("kind in ('deposit', 'withdrawal')", "kind in ('deposit')"), ("and ts >= %s", "and ts >= %s - interval '30 days'"),
    ("on conflict (signature) do nothing", "on conflict do nothing"), ("interval '30 days'\")", "interval '30 minutes'\")"),
]


def sql_mutants(src, functions):
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    out = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in functions]:
        a = sum(len(l) for l in lines[:fn.lineno - 1]); b = sum(len(l) for l in lines[:fn.end_lineno])
        body = src[a:b]
        for old, new in SQL_SWAPS:
            start = 0
            while (i := body.find(old, start)) >= 0:
                mutated = body[:i] + new + body[i + len(old):]
                desc = f'sql {old!r} -> {new!r}'
                out.append((fn.name, desc, src[:a + i].count('\n') + 1, f'{desc} @{i}', src[:a] + mutated + src[b:]))
                start = i + len(old)
    return out


# --- identity -----------------------------------------------------------------------
#
# A mutant's identity is its key:
#     (target, function, description, text, occurrence)
# `text` is the stripped source line the change starts on. `occurrence` counts
# the mutants of the same function that have the same description and text,
# in generation order (0 for the first). The line number is not in the key, so
# an edit above a mutant does not change its key. Two mutants with the same
# description on one line get different occurrences, so each has its own key.
# The report prints the line number and the occurrence for humans.

class Mutant(NamedTuple):
    target: str
    function: str
    desc: str          # the change, without a line number
    text: str          # stripped source line of the change
    occ: int           # index among mutants of `function` with this desc and text
    line: int          # for the report only: not in the key
    legacy: str        # the old description ('... @L<line>', or '... @<offset>' for SQL)
    code: str

    @property
    def key(self):
        return (self.target, self.function, self.desc, self.text, self.occ)

    @property
    def old_key(self):
        return (self.target, self.function, self.legacy)

    def label(self):
        return f'{self.target} {self.function} {self.desc} @L{self.line}' + (f' #{self.occ}' if self.occ else '')


def identify(target, src, raw):
    """Mutants with keys from (function, desc, line, legacy, code) tuples."""
    lines = src.splitlines()
    seen = {}
    out = []
    for fn, desc, line, legacy, code in raw:
        text = lines[line - 1].strip() if line and 0 < line <= len(lines) else ''
        occ = seen.get((fn, desc, text), 0)
        seen[(fn, desc, text)] = occ + 1
        out.append(Mutant(target, fn, desc, text, occ, line, legacy, code))
    return out


def target_mutants(name, root=ROOT):
    f, fns, _ = TARGETS[name]
    src = (root / f).read_text()
    raw = js_mutants(src, fns) if f.endswith('.mjs') else py_mutants(src, fns)
    if name in SQL_TARGETS:
        raw += sql_mutants(src, fns)
    return identify(name, src, raw)


def match_equivalent(mutants, table, targets=None):
    """({mutant key: reason}, [warning]) for `mutants` against `table`.

    A 5-tuple entry matches only the mutant with that key. A 3-tuple entry is
    an old-style key (target, function, 'desc @L<line>'): it still matches,
    with a warning, during the transition. An entry of a target in `targets`
    that matches no mutant gets a warning: it is stale."""
    targets = set(targets if targets is not None else {m.target for m in mutants})
    by_old = {}
    for m in mutants:
        by_old.setdefault(m.old_key, []).append(m)
    keys = {m.key for m in mutants}
    reasons, warnings = {}, []
    for entry, reason in table.items():
        if len(entry) == 5:
            if entry in keys:
                reasons[entry] = reason
            elif entry[0] in targets:
                warnings.append(f'stale EQUIVALENT entry, matches no mutant: {entry!r}')
        elif len(entry) == 3:
            hits = by_old.get(entry, [])
            if not hits:
                if entry[0] in targets:
                    warnings.append(f'stale old-style EQUIVALENT entry, matches no mutant: {entry!r}')
                continue
            if len(hits) > 1:
                warnings.append(f'old-style EQUIVALENT entry {entry!r} matches {len(hits)} mutants '
                                f'({", ".join(m.label() for m in hits)}): give each its own key')
            for m in hits:
                warnings.append(f'old-style EQUIVALENT key {entry!r}: migrate to {m.key!r}')
                reasons.setdefault(m.key, reason)
        else:
            raise SystemExit(f'EQUIVALENT key must have 5 (or old-style 3) parts: {entry!r}')
    return reasons, warnings


def migrate(table, root=ROOT):
    """(resolved {new key: reason}, unresolved [(old key, why)]) for every
    old-style entry of `table`. An entry resolves only when exactly one mutant
    of its function has its description on its line in the current source.
    Nothing is guessed."""
    cache, resolved, unresolved = {}, {}, []
    for entry, reason in table.items():
        if len(entry) != 3:
            continue
        target, fn, old = entry
        if target not in TARGETS:
            unresolved.append((entry, 'unknown target'))
            continue
        if target not in cache:
            cache[target] = target_mutants(target, root)
        hits = [m for m in cache[target] if m.function == fn and m.legacy == old]
        if len(hits) == 1:
            resolved[hits[0].key] = reason
        elif not hits:
            near = sorted({m.line for m in cache[target] if m.function == fn and old.startswith(m.desc + ' @')})
            unresolved.append((entry, f'no mutant {old!r} in {fn} now' +
                               (f' (same description on lines {near})' if near else ' (description not in function)')))
        else:
            unresolved.append((entry, f'{len(hits)} mutants share {old!r}: '
                               + '; '.join(repr(m.key) for m in hits)))
    return resolved, unresolved


# --- running -------------------------------------------------------------------------

def worker_setup(i):
    base = SCRATCH / f'w{i}'
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    shutil.copytree(ROOT, base / 'lp_bot', ignore=shutil.ignore_patterns('__pycache__', 'research', '*.jsonl'))
    (base / 'node_modules').symlink_to(ROOT.parent / 'node_modules')
    dbname = f'{DB_PREFIX}{i}_test'
    subprocess.run(['dropdb', '--if-exists', dbname], capture_output=True)
    r = subprocess.run(['createdb', '-T', DB_TEMPLATE, dbname], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f'createdb {dbname}: {r.stderr}')
    return base / 'lp_bot', dbname


KILL_GRACE_S = 5                  # SIGTERM to SIGKILL, for a test group that outlives its timeout


def kill_group(proc, grace_s=KILL_GRACE_S):
    """Stop every process of `proc`'s session (it is the group leader):
    SIGTERM, then SIGKILL after `grace_s` to whatever is left. A grandchild
    (node --test spawns one per file) dies too, and no orphan keeps a CPU
    or a test database (2026-10-02: two node runs alive 33 min and 1 h 26
    after their mutant timed out)."""
    for sig, wait_s in ((signal.SIGTERM, grace_s), (signal.SIGKILL, 0)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            proc.poll()                         # reap the leader: a zombie still counts in its group
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)


def survived(rc):
    """Whether a mutant survived its tests: only a clean pass. A failure, a
    crash and a timeout all kill it."""
    return rc == 0


def run_tests(copy_root, dbname, cmd, timeout=None):
    """The return code of `cmd` in the copy's tests directory, or 'timeout'
    (counted as KILLED). The command runs in its own session; on a timeout
    the whole process group is killed, and after a normal exit any straggler
    of the group is too."""
    env = dict(os.environ, LPBOT_DSN=f'dbname={dbname}', HYP_EXAMPLES=os.environ.get('HYP_EXAMPLES', '60'),
               PYTHONDONTWRITEBYTECODE='1')
    proc = subprocess.Popen(cmd, cwd=copy_root / 'tests', env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    try:
        proc.communicate(timeout=TIMEOUT if timeout is None else timeout)
        return proc.returncode
    except subprocess.TimeoutExpired:
        return 'timeout'
    finally:
        kill_group(proc)
        try:
            proc.communicate(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            pass


def main(names):
    if names and names[0] == '--migrate':
        return print_migration(EQUIVALENT)
    names = names or list(TARGETS)
    todo = [m for name in names for m in target_mutants(name)]
    reasons, warnings = match_equivalent(todo, EQUIVALENT, names)
    for w in warnings:
        print('  WARNING', w, flush=True)
    print(f'{len(todo)} mutants over {len(names)} targets, {WORKERS} workers', flush=True)
    setups = [worker_setup(i) for i in range(WORKERS)]
    # baseline: the unmutated copy must pass every command
    for name in names:
        rc = run_tests(*setups[0], TARGETS[name][2])
        if rc != 0:
            raise SystemExit(f'baseline fails for {name} ({rc}): fix the tests first')
    queue = list(enumerate(todo))
    results = [None] * len(todo)
    free = list(range(WORKERS))
    t0 = time.time()

    def one(slot, k, m):
        f, _, cmd = TARGETS[m.target]
        copy_root, dbname = setups[slot]
        path = copy_root / f
        original = (ROOT / f).read_text()
        path.write_text(m.code)
        try:
            rc = run_tests(copy_root, dbname, cmd)
        finally:
            path.write_text(original)
        return slot, k, rc

    with cf.ThreadPoolExecutor(WORKERS) as ex:
        futs = set()
        while queue or futs:
            while queue and free:
                k, item = queue.pop(0)
                futs.add(ex.submit(one, free.pop(), k, item))
            done, futs = cf.wait(futs, return_when=cf.FIRST_COMPLETED)
            for d in done:
                slot, k, rc = d.result()
                free.append(slot)
                results[k] = rc
                n = sum(r is not None for r in results)
                if n % 25 == 0:
                    print(f'  {n}/{len(todo)} in {time.time() - t0:.0f}s', flush=True)
    survivors, equivalent = [], []
    for m, rc in zip(todo, results):
        if survived(rc):
            (equivalent if m.key in reasons else survivors).append(m)
    killed = len(todo) - len(survivors) - len(equivalent)
    print(f'\n{killed}/{len(todo)} killed, {len(equivalent)} equivalent, {len(survivors)} SURVIVED '
          f'({time.time() - t0:.0f}s)')
    by = {}
    for m, rc in zip(todo, results):
        s = by.setdefault(m.target, [0, 0]); s[0] += 1; s[1] += not survived(rc)
    for name, (n, k) in by.items():
        print(f'  {name:14s} {k:4d}/{n:<4d} killed')
    for m in survivors:
        print('  SURVIVED', m.label(), '|', m.text)
        print('      key:', repr(m.key))
    for m in equivalent:
        print('  equivalent', m.label(), '--', reasons[m.key])
    for i in range(WORKERS):
        subprocess.run(['dropdb', '--if-exists', f'{DB_PREFIX}{i}_test'], capture_output=True)
    shutil.rmtree(SCRATCH, ignore_errors=True)
    return 1 if survivors else 0


def print_migration(table):
    """Print new-style entries for the old-style entries of `table`, and every
    entry that does not resolve. Exit status 1 when one does not resolve."""
    resolved, unresolved = migrate(table)
    for key, reason in resolved.items():
        print(f'    {key!r}: {reason!r},')
    for entry, why in unresolved:
        print('UNRESOLVED', repr(entry), '--', why)
    print(f'# {len(resolved)} resolved, {len(unresolved)} unresolved')
    return 1 if unresolved else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
