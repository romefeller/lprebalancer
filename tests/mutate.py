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
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH = pathlib.Path(os.environ.get('MUT_DIR') or tempfile.gettempdir()) / 'lp_bot_mutants'
WORKERS = int(os.environ.get('MUT_WORKERS', 6))
TIMEOUT = int(os.environ.get('MUT_TIMEOUT', 180))
PY = sys.executable

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
    'resilience': ('rebalancer.py', ['health_key', 'counts_as_failure', 'chain', 'failover_pick', 'venue_failover',
                                     'voluntary_move_allowed', 'idle_deploys_left', 'deploy_idle'],
                   PY_TESTS('test_health', 'test_deploy_idle', 'test_edges_0930')),
    'books': ('rebalancer.py', ['regime_at_move', 'notify_book', 'emoji_for', 'tidy'],
              PY_TESTS('test_move_books', 'test_observability', 'test_rebalancer', 'test_edges_0930')),
    'deployment': ('db.py', ['_pct', '_deployment', 'deployment_now', '_pnl'], PY_TESTS('test_move_books', 'test_audit_more', 'test_edges_0930', 'test_db')),
    'rpc_policy': ('rpc_policy.mjs', ['endpoints', 'errorKind', 'overEndpoints'], NODE_TESTS('test_rpc_policy.mjs')),
    'book_lines': ('book_format.mjs', ['shareAgrees', 'lpLine', 'emojiFor', 'healthLine'], NODE_TESTS('test_book_format.mjs')),
    'surrogate': ('calm.py', ['pair_tokens', 'clean_bars', 'fit_surrogate', 'binance_5m', 'surrogate_5m',
                              'missing_slots', 'tape_fresh'], PY_TESTS('test_tape_surrogate')),
    'surrogate_overlay': ('rebalancer.py', ['with_surrogate', 'tape_source', 'track_tape_source', 'regime_view'],
                          PY_TESTS('test_tape_surrogate', 'test_regime', 'test_hardening.StaleTape')),
    'daily_report': ('rebalancer.py', ['daily_report'], PY_TESTS('test_daily.Report')),
    'priority_fee': ('swap_jupiter.mjs', ['swapRequestBody', 'priorityFeeLamports', 'verifyPriorityFee'],
                     NODE_TESTS('test_priority_fee.mjs', 'test_security.mjs')),
    'audit_checks': ('audit.py', ['check_idle', 'check_gas', 'check_equity', 'lookalike', 'classify_tx', 'check_flows', 'check_harvest',
                                  'payout_received', 'check_positions', 'check_owed', 'check_empty', 'check_fee_reads',
                                  'keep_mints', 'known_signatures'], PY_TESTS('test_audit', 'test_audit_runner')),
    'audit_run': ('audit.py', ['run'], PY_TESTS('test_audit.Runner', 'test_audit_runner')),
    'capital_db': ('db.py', ['since_start', 'record_flow', 'audit_value', 'set_audit_value', 'record_audit',
                             '_since_start_or_none'], PY_TESTS('test_audit.SinceStart', 'test_audit.Runner', 'test_audit_more.SinceStartEdges')),
    'deploy_all': ('rebalancer.py', ['deployable_usd', 'capital', 'side_target_fraction', 'deposit_caps', 'balance_wallet'],
                   PY_TESTS('test_deploy_all', 'test_audit_more.QuoteFallbacks', 'test_rebalancer.DepositCaps', 'test_payout.SwapGate', 'test_payout.SwapRetry')),
    'loop_hooks': ('rebalancer.py', ['janitor', 'run_audits'], PY_TESTS('test_audit.Hooks', 'test_audit_more.Hooks', 'test_audit_more.JanitorKeepsWhatComesBack', 'test_audit_more.JanitorReplan', 'test_audit_more.JanitorUnsignedClose')),
    'janitor_js': ('janitor.mjs', ['planClose', 'closeInstructions', 'verifyCloseTx'], NODE_TESTS('test_janitor.mjs')),
    'book_format': ('book_format.mjs', ['equityLine', 'lpLine', 'sinceStartLine'], NODE_TESTS('test_book_format.mjs')),
    'deploy_idle': ('rebalancer.py', ['idle_to_deploy', 'deploy_idle', 'balance_wallet'], PY_TESTS('test_deploy_idle', 'test_deploy_all')),
    'idle_capital': ('rebalancer.py', ['idle_to_deploy', 'deploy_idle', 'plan_sweep', 'sweep_foreign'],
                     PY_TESTS('test_deploy_idle', 'test_sweep')),
    'orca_fees': ('orca_fees.mjs', ['growthInside', 'ownFees', 'checkOrca', 'transferFeeOf', 'feesFromOrcaSnapshot',
                                    'snapshotAddresses', 'consistentOrcaFees'], NODE_TESTS('test_orca_fees.mjs')),
    'fee_snapshot': ('fee_snapshot.mjs', ['wrappingSubU128', 'checkFees', 'feesFromSnapshot', 'snapshotKeys',
                                          'decodeSnapshot', 'consistentFees'],
                     NODE_TESTS('test_fee_snapshot.mjs')),
}

SQL_TARGETS = {'band_profile', 'daily', 'capital_db'}

# Mutants that cannot change behaviour, with the reason. Keyed by
# (target, function, description) as the report prints them.
EQUIVALENT = {
    ('health', 'summary', 'const 2->3 @L148'): 'the closed rank only has to sort after 0 and 1',
    ('health', 'summary', 'const 2->4 @L148'): 'the closed rank only has to sort after 0 and 1',
    ('resilience', 'deploy_idle', 'swap Lt->LtE @L1038'): 'only a deploy exactly 86400.0 s old differs: a float clock never lands there',
    ('resilience', 'voluntary_move_allowed', 'const 86400->86401 @L1120'): 'a move older than a day passes the gap anyway: keeping it in the window changes nothing',
    ('resilience', 'voluntary_move_allowed', 'const 86400->172800 @L1120'): 'a move older than a day passes the gap anyway: keeping it in the window changes nothing',
    ('resilience', 'voluntary_move_allowed', 'const 0->1 @L1121'): 'a last rebalance at epoch 0 or 1 is decades past the gap',
    ('resilience', 'voluntary_move_allowed', 'swap GtE->Gt @L1122'): 'only a gap of exactly CALM_MIN_GAP seconds differs: a float clock never lands there',
    ('resilience', 'failover_pick', 'drop operand 0 @L1138'): 'the held venue is the held dex (config.POOL is on config.DEX): `dex != held_dex` excludes it too',
    ('resilience', 'chain', 'flip bool @L288'): 'print flush only',
    ('resilience', 'voluntary_move_allowed', 'swap Lt->LtE @L1120'): 'only a move exactly 86400.0 s old differs: a float clock never lands there',
    ('books', 'regime_at_move', 'swap Gt->GtE @L209'): 'lower = 0 is caught by `lower and` first; upper = lower gives half 1.0, refused by `upper > lower`',
    ('book_lines', 'shareAgrees', '<= -> < @L25'): 'only a difference of exactly 0.2 points differs: a float share never lands there',
    ('book_lines', 'healthLine', '\\?\\? -> || @L76'): 'health.summary always sets emoji to a non-empty string: ?? and || agree',
    ('health', 'cooldown', 'const 30->31 @L43'): 'the exponent cap only guards overflow: 2^30 x 600 s is far past MAX_S either way',
    ('health', 'cooldown', 'const 30->60 @L43'): 'the exponent cap only guards overflow: 2^30 x 600 s is far past MAX_S either way',
    ('health', 'verdict', 'const 0.0->1.0 @L80'): 'retry_at None with failures on record: 0.0 or 1.0 are both decades past, allowed either way',
    ('resilience', 'chain', 'const 420->421 @L275'): 'one second more on a 420 s signer timeout',
    ('books', 'regime_at_move', 'swap Lt->LtE @L215'): 'widths and probs are both rounded to 2 decimals: a difference is 0 or >= 0.01',
    ('books', 'regime_at_move', 'const 1e-06->1.5e-06 @L215'): 'widths and probs are both rounded to 2 decimals: a difference is 0 or >= 0.01',
    ('books', 'regime_at_move', 'const 1e-06->5e-07 @L215'): 'widths and probs are both rounded to 2 decimals: a difference is 0 or >= 0.01',
    ('deployment', '_pct', 'swap LtE->Lt @L946'): 'a whole of exactly 0 is caught by `not whole` first',
    ('surrogate', 'pair_tokens', 'drop operand 1 @L101'): "str(None) is 'None', one part: refused either way",
    ('surrogate', 'clean_bars', 'swap GtE->Gt @L123'): 'a volume of exactly 0 becomes 0.0 either way',
    ('surrogate', 'fit_surrogate', 'const 1.0->0.5 @L130'): 'the scale is clamped at 1.0: a default below 1 is the identity',
    ('surrogate', 'fit_surrogate', 'const 1.0->0.0 @L130'): 'the scale is clamped at 1.0: a default below 1 is the identity',
    ('surrogate', 'fit_surrogate', 'const 1.0->1.5 @L147'): 'scale 1.0 through the formula is the identity: skipping it or not gives the same bars',
    ('surrogate', 'fit_surrogate', 'const 1.0->0.5 @L147'): 'scale 1.0 through the formula is the identity: skipping it or not gives the same bars',
    ('surrogate', 'fit_surrogate', 'const 1.0->0.0 @L147'): 'scale 1.0 through the formula is the identity: skipping it or not gives the same bars',
    ('surrogate', 'fit_surrogate', 'drop operand 4 @L135'): 'a negative live price fails both level checks: None either way',
    ('surrogate', 'fit_surrogate', 'swap LtE->Lt @L135'): 'a zero live price is caught by `not live_price` first',
    ('surrogate', 'fit_surrogate', 'const 0->1 @L135'): 'bars[0] and bars[1] have the same length (the <= 0 -> <= 1 twin is killed by the sub-1 price test)',
    ('surrogate', 'fit_surrogate', 'swap Gt->GtE @L138'): 'only a close exactly 0.5% off differs: a float never lands there',
    ('surrogate', 'fit_surrogate', 'swap Gt->GtE @L139'): 'only a close exactly 0.5% off differs: a float never lands there',
    ('surrogate', 'fit_surrogate', 'swap Gt->GtE @L145'): 'only a median exactly 0.2% differs: a float never lands there',
    ('surrogate', 'fit_surrogate', 'drop operand 1 @L142'): 'an empty reference gives no overlap: the join check is skipped either way',
    ('surrogate', 'fit_surrogate', 'const 0->1 @L142'): 'ref[0] and ref[1] have the same length',
    ('surrogate', 'binance_5m', 'and<->or @L169'): 'a dict, a string or [] yields no row and stops at the short-page check: [] either way',
    ('surrogate', 'binance_5m', 'drop operand 0 @L169'): 'a dict or a string yields no row and stops at the short-page check: [] either way',
    ('surrogate', 'binance_5m', 'drop operand 1 @L169'): '[] yields no row and stops at the short-page check',
    ('surrogate', 'surrogate_5m', 'skip if body @L196'): 'symbols(*None) raises inside the try, which skips every source: (None, None), no fetch',
    ('surrogate', 'missing_slots', 'const 1->2 @L216'): 'the range end is exclusive on a 300 s step: last + 1 and last + 2 give the same slots',
    ('surrogate_overlay', 'with_surrogate', 'skip if body @L831'): 'None[0] raises inside the try, which returns bars (None) either way',
    ('surrogate_overlay', 'with_surrogate', 'const 0->1 @L841'): 'an empty cache asked at t = 0 or t = 1: both are decades past the refresh',
    ('surrogate_overlay', 'with_surrogate', 'swap Gt->GtE @L843'): 'only an ask exactly SURROGATE_REFRESH later differs: a float clock never lands there',
    ('surrogate_overlay', 'with_surrogate', 'swap GtE->Gt @L847'): 'the trim is an hour beyond the fill window: a bar at its edge is never used',
    ('surrogate_overlay', 'with_surrogate', 'const 3600->3601 @L847'): 'the trim is an hour beyond the fill window: a bar at its edge is never used',
    ('surrogate_overlay', 'with_surrogate', 'flip bool @L861'): 'print flush only',
    ('surrogate_overlay', 'regime_view', 'const 0->1 @L1053'): 'hold_left is read only in STALE mode on a fresh tape, where it is assigned first',
    ('surrogate_overlay', 'regime_view', 'drop operand 0 @L1066'): 'calm.regime_view returns None only for no bars, which returned before',
    ('guards', 'fee_read_problem', 'drop operand 1 @L141'):
        'not isfinite(pos): a NaN or infinite position fails every later comparison, so the verdict is None either way',
    ('txfees', 'fetch', 'const 1->0 @L56'): 'the JSON-RPC request id is arbitrary',
    ('txfees', 'fetch', 'const 1->2 @L56'): 'the JSON-RPC request id is arbitrary',
    ('distribute', 'distribute', 'drop operand 1 @L578'):
        'split never yields a part of amount 0, so the zero-amount guard of the price cannot bind',
    ('distribute', 'distribute', 'swap Gt->GtE @L594'):
        'a remainder of exactly 1e-9 is not reachable in float and is below any token resolution',
    ('read_status', 'read_status', 'drop operand 1 @L234'):
        'an answer without a position carries no fee figures, so the check finds nothing either way',
    ('risk', 'chi2_sf_even', 'const 0->1 @L326'): 'k = 1 is odd and refused by the next test either way',
    ('risk', 'chi2_sf_even', 'swap LtE->Lt @L328'): 'at x = 0 the series gives exp(0) * 1 = 1.0, the same value',
    ('risk', 'chi2_sf_even', 'const 1.0->1.5 @L334'): 'exp(-h) times a partial sum of e^h is below 1 for h > 0: the clamp never binds',
    ('risk', 'arch_lm', 'const 1.0->1.5 @L353'): 'an OLS R^2 with an intercept lies in [0, 1]: the clamp never binds',
    ('risk', 'arch_lm', 'const 1.0->0.5 @L353'): 'an OLS R^2 with an intercept lies in [0, 1]: the clamp never binds',
    ('fees', 'split', 'swap Lt->LtE @L39'): 'a zero fee skipped or split adds no row and takes no gas: add() drops zero',
    ('measured', 'measured_fees', 'drop operand 1 @L635'):
        'out None: the mutant raises inside the try, which falls back exactly as the original does',
    ('risk', 'risk_metrics', 'swap Gt->GtE @L392'):
        'exact float equality with the tolerance; on a flat tape the other operand still refuses',
    ('orca_fees', 'feesFromOrcaSnapshot', '\\?\\? -> || @L93'): 'a tick is an object or undefined: ?? and || agree',
    ('orca_fees', 'feesFromOrcaSnapshot', '\\?\\? -> || @L111'): 'an error message is never empty: ?? and || agree',
    ('fee_snapshot', 'decodeSnapshot', '\\?(?=\\s) -> && false ? @L141'):
        'the array key is only echoed back inside the parsed container; no fee depends on it',
    ('daily', 'daily_line', 'swap GtE->Gt @L804'): 'only the exact instant of midnight differs; now() is never that instant in a test or a poll',
    ('audit_checks', 'known_signatures', "skip if body @L218"): "a cheap prefilter: a line without the word is skipped either way by the lookups below",
    ('audit_checks', 'keep_mints', 'drop operand 1 @L402'): "a None reward list raises inside the try, which keeps the base set either way",
    ('capital_db', 'record_flow', "sql 'on conflict (signature) do nothing' -> 'on conflict do nothing' @466"):
        "the only other unique constraint is on the baseline, which record_flow cannot write",
    ('janitor_js', 'planClose', '\\?\\? -> || @L45'): "a withheld amount is a number or absent: ?? and || agree",
    ('idle_capital', 'idle_to_deploy', 'const 0.0->1.0 @L815'): 'the $2 floor is above 2% of $1: a missing equity gives the floor either way',
    ('idle_capital', 'plan_sweep', 'drop operand 0 @L865'): 'an empty account is worth $0, under the $1 dust gate either way',
    ('idle_capital', 'plan_sweep', 'swap LtE->Lt @L865'): 'an empty account is worth $0, under the $1 dust gate either way',
    ('idle_capital', 'plan_sweep', 'const 0->1 @L865'): 'one raw unit of a token worth over $1 per raw unit does not exist among verified tokens; the dust gate decides',
    ('idle_capital', 'plan_sweep', 'drop operand 1 @L874'): 'the symbol is read only after facts were required to exist',
    ('idle_capital', 'sweep_foreign', 'skip if body @L893'): 'an empty list plans nothing either way; the early return only saves the price calls',
    ('idle_capital', 'sweep_foreign', 'and<->or @L892'): 'the list only chooses what to price; plan_sweep applies the rules again',
    ('idle_capital', 'sweep_foreign', 'drop operand 0 @L892'): 'the list only chooses what to price; plan_sweep applies the rules again',
    ('idle_capital', 'sweep_foreign', 'drop operand 1 @L892'): 'the list only chooses what to price; plan_sweep applies the rules again',
    ('idle_capital', 'sweep_foreign', 'swap Gt->GtE @L892'): 'the list only chooses what to price; plan_sweep applies the rules again',
    ('idle_capital', 'sweep_foreign', 'const 0->1 @L892'): 'the list only chooses what to price; plan_sweep applies the rules again',
    ('loop_hooks', 'run_audits', 'const 0->1 @L1118'): 'a first audit is due either way: time.time() is far past 3,601 s',
    ('loop_hooks', 'janitor', 'drop operand 1 @L1141'): 'a None plan raises inside the try, which reports janitor_failed exactly as the guard does',
    ('loop_hooks', 'janitor', 'drop operand 1 @L1153'): 'a None re-plan raises inside the try, which reports janitor_failed exactly as the guard does',
    ('loop_hooks', 'janitor', 'drop operand 1 @L1159'): 'a None close answer raises inside the try, which reports janitor_failed exactly as the guard does (the unsigned-answer mutant on this line is killed)',
}
# measured_fees line 651 only decides whether a disagreement is notified; the
# figures booked do not depend on it.
for d in ('swap Gt->GtE', 'const 0.01->0.015', 'const 0.01->0.005', 'const 0.01->0.0', 'drop operand 1',
          'const 0.05->0.025', 'const 0.0->1.0', 'const 0.05->0.07500000000000001', 'const 0.01->1.0',
          'const 0.05->1.0', 'const 0.05->0.0'):
    EQUIVALENT[('measured', 'measured_fees', f'{d} @L651')] = 'report-only: decides whether a disagreement is notified'
# Float-noise tolerances: the scale of the threshold is a choice between
# "exactly no variance" and any real variance, many decades apart. A mutant
# that moves it by 2x cannot change a result; moving it to 0 or 1 can, and
# those mutants are killed by tests.
for fn, line in (('arch_lm', 349), ('risk_metrics', 391), ('risk_metrics', 398)):
    base = {349: '1e-12', 391: '1e-06', 398: '1e-06'}[line]
    for new in ({'1e-12': ('1.5e-12', '5e-13'), '1e-06': ('1.5e-06', '5e-07')}[base]):
        EQUIVALENT[('risk', fn, f'const {base}->{new} @L{line}')] = 'a float-noise tolerance: 2x on its scale changes nothing'



# --- Python mutants ---------------------------------------------------------------

CMP_SWAP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
            ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
            ast.In: ast.NotIn, ast.NotIn: ast.In}
CMP_FLIP = {ast.Lt: ast.Gt, ast.LtE: ast.GtE, ast.Gt: ast.Lt, ast.GtE: ast.LtE}
BIN_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult,
            ast.FloorDiv: ast.Mult, ast.Pow: ast.Mult, ast.Mod: ast.FloorDiv}
NAME_SWAP = {'min': 'max', 'max': 'min', 'any': 'all', 'all': 'any'}


def py_mutants(src, functions):
    """(function, description, mutated source) for every mutant of
    `functions`. Only the function is re-generated; the rest of the file is
    spliced back byte for byte."""
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
                out.append((fn.name, f'{desc} @L{getattr(node, "lineno", "?")}', head + body + '\n' + tail))
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
                out.append((name, f'{pat} -> {rep} @L{lineno}', src[:a] + new + src[b:]))
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
                out.append((fn.name, f'sql {old!r} -> {new!r} @{i}', src[:a] + mutated + src[b:]))
                start = i + len(old)
    return out


# --- running -------------------------------------------------------------------------

def worker_setup(i):
    base = SCRATCH / f'w{i}'
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    shutil.copytree(ROOT, base / 'lp_bot', ignore=shutil.ignore_patterns('__pycache__', 'research', '*.jsonl'))
    (base / 'node_modules').symlink_to(ROOT.parent / 'node_modules')
    dbname = f'rebalancer_mut{i}_test'
    subprocess.run(['dropdb', '--if-exists', dbname], capture_output=True)
    r = subprocess.run(['createdb', '-T', 'rebalancer_test', dbname], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f'createdb {dbname}: {r.stderr}')
    return base / 'lp_bot', dbname


def run_tests(copy_root, dbname, cmd):
    env = dict(os.environ, LPBOT_DSN=f'dbname={dbname}', HYP_EXAMPLES=os.environ.get('HYP_EXAMPLES', '60'),
               PYTHONDONTWRITEBYTECODE='1')
    try:
        r = subprocess.run(cmd, cwd=copy_root / 'tests', env=env, capture_output=True, timeout=TIMEOUT)
        return r.returncode
    except subprocess.TimeoutExpired:
        return 'timeout'


def main(names):
    todo = []
    for name in names or TARGETS:
        f, fns, cmd = TARGETS[name]
        src = (ROOT / f).read_text()
        muts = js_mutants(src, fns) if f.endswith('.mjs') else py_mutants(src, fns)
        if name in SQL_TARGETS:
            muts += sql_mutants(src, fns)
        todo += [(name, f, cmd, fn, desc, code) for fn, desc, code in muts]
    print(f'{len(todo)} mutants over {len(names or TARGETS)} targets, {WORKERS} workers', flush=True)
    setups = [worker_setup(i) for i in range(WORKERS)]
    # baseline: the unmutated copy must pass every command
    for name in names or TARGETS:
        rc = run_tests(*setups[0], TARGETS[name][2])
        if rc != 0:
            raise SystemExit(f'baseline fails for {name} ({rc}): fix the tests first')
    queue = list(enumerate(todo))
    results = [None] * len(todo)
    free = list(range(WORKERS))
    t0 = time.time()

    def one(slot, k, item):
        name, f, cmd, fn, desc, code = item
        copy_root, dbname = setups[slot]
        path = copy_root / f
        original = (ROOT / f).read_text()
        path.write_text(code)
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
    for (name, f, cmd, fn, desc, code), rc in zip(todo, results):
        if rc == 0:
            key = (name, fn, desc)
            (equivalent if key in EQUIVALENT else survivors).append(key)
    killed = len(todo) - len(survivors) - len(equivalent)
    print(f'\n{killed}/{len(todo)} killed, {len(equivalent)} equivalent, {len(survivors)} SURVIVED '
          f'({time.time() - t0:.0f}s)')
    by = {}
    for (name, f, *_), rc in zip(todo, results):
        s = by.setdefault(name, [0, 0]); s[0] += 1; s[1] += rc != 0
    for name, (n, k) in by.items():
        print(f'  {name:14s} {k:4d}/{n:<4d} killed')
    for key in survivors:
        print('  SURVIVED', *key)
    for key in equivalent:
        print('  equivalent', *key, '--', EQUIVALENT[key])
    for i in range(WORKERS):
        subprocess.run(['dropdb', '--if-exists', f'rebalancer_mut{i}_test'], capture_output=True)
    shutil.rmtree(SCRATCH, ignore_errors=True)
    return 1 if survivors else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
