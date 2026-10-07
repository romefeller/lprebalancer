// The Telegram bridge over many feeds: run/*/events.jsonl and the legacy
// ROOT/events.jsonl, one cursor per file, the pool's label on every message.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { feedFiles, migrateState, readNew, tailAll, message, render, aSide, payoutHeld, MOVED_FEED, LEGACY_FEED }
  from '../telegram_bridge.mjs';
import { poolLabel, redact, portfolioText, equityLine, sinceStartLine, walletName } from '../book_format.mjs';

const HERE = path.dirname(new URL(import.meta.url).pathname);
// The live bot's directory: its feeds are real rows to render (read only).
const LIVE = path.join(HERE, '..', '..', 'lp_bot');
// The reference rendering: main just before the multi-wallet merge. Pinned to a
// commit, not read from a directory: after the merge every tree holds the new
// bridge, and comparing against it would be new against new.
const PRE_MERGE = 'c2c1cc2';
const gitShow = (file) => {
  try {
    const top = execFileSync('git', ['-C', HERE, 'rev-parse', '--show-toplevel'], { encoding: 'utf8' }).trim();
    return execFileSync('git', ['-C', top, 'show', `${PRE_MERGE}:${file}`], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
  } catch { return null; }
};

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'bridge_'));
const row = (event, extra = {}) => JSON.stringify({ t: '2026-10-01T00:00:00Z', event, ...extra }) + '\n';
const put = (root, rel, text, flag = 'a') => {
  fs.mkdirSync(path.dirname(path.join(root, rel)), { recursive: true });
  fs.writeFileSync(path.join(root, rel), text, { flag });
};
const run = (p) => path.join('run', p, 'events.jsonl');

async function tick(state, root) {
  const got = [];
  const saved = [];
  await tailAll(state, root, async (r) => { got.push(r); }, (st) => saved.push(JSON.stringify(st)));
  return { got, saved };
}

// --- the feeds ---------------------------------------------------------------------

test('every profile feed and the legacy one are tailed, each from its own cursor', async () => {
  const root = tmp();
  put(root, LEGACY_FEED, row('in_band', { n: 1 }));
  put(root, run('sol-usdc'), row('in_band', { n: 2, pair: 'SOL/USDC' }));
  put(root, run('mu-usdc'), row('OPEN', { n: 3, pair: 'MU/USDC' }));
  assert.deepEqual(feedFiles(root), [LEGACY_FEED, run('mu-usdc'), run('sol-usdc')]);
  const state = { files: {} };
  assert.deepEqual((await tick(state, root)).got.map(r => r.n), [1, 3, 2]);
  assert.deepEqual((await tick(state, root)).got, []);                   // nothing twice
  put(root, run('mu-usdc'), row('CLOSE', { n: 4 }));
  assert.deepEqual((await tick(state, root)).got.map(r => r.n), [4]);
  assert.deepEqual(Object.keys(state.files).sort(), [LEGACY_FEED, run('mu-usdc'), run('sol-usdc')].sort());
});

test('a profile directory that appears while running is read from its start', async () => {
  const root = tmp();
  put(root, run('sol-usdc'), row('in_band', { n: 1 }));
  const state = { files: {} };
  await tick(state, root);
  fs.mkdirSync(path.join(root, 'run', 'djt-usdc'));                        // no feed yet: nothing to read
  assert.deepEqual((await tick(state, root)).got, []);
  put(root, run('djt-usdc'), row('startup', { n: 2 }) + row('dormant', { n: 3 }));
  assert.deepEqual((await tick(state, root)).got.map(r => r.n), [2, 3]);
});

test('a line still being written waits for its newline', async () => {
  const root = tmp();
  const full = row('OPEN', { n: 1, pair: 'MU/USDC' });
  put(root, run('mu-usdc'), full.slice(0, 20));
  const state = { files: {} };
  assert.deepEqual((await tick(state, root)).got, []);
  assert.equal(state.files[run('mu-usdc')].pos, 0);
  put(root, run('mu-usdc'), full.slice(20, -1));
  assert.deepEqual((await tick(state, root)).got, []);
  put(root, run('mu-usdc'), '\n' + row('CLOSE', { n: 2 }).slice(0, 5));
  assert.deepEqual((await tick(state, root)).got.map(r => r.n), [1]);
  put(root, run('mu-usdc'), row('CLOSE', { n: 2 }).slice(5));
  assert.deepEqual((await tick(state, root)).got.map(r => r.n), [2]);
});

test('a line that is not JSON is dropped, the rest go out', async () => {
  const root = tmp();
  put(root, run('a'), row('x', { n: 1 }) + '{"event": "broken"\n' + 'garbage\n' + '\n' + row('x', { n: 2 }));
  const state = { files: {} };
  const errors = [];
  const orig = console.error;
  console.error = (...a) => errors.push(a.join(' '));
  try {
    assert.deepEqual((await tick(state, root)).got.map(r => r.n), [1, 2]);
  } finally { console.error = orig; }
  assert.ok(errors.some(e => e.includes('2 line(s) not JSON')), errors.join('|'));
});

test('a truncated feed is read again from 0', async () => {
  const root = tmp();
  put(root, run('a'), row('x', { n: 1 }) + row('x', { n: 2 }));
  const state = { files: {} };
  await tick(state, root);
  fs.truncateSync(path.join(root, run('a')), 0);
  put(root, run('a'), row('y', { n: 3 }));
  assert.deepEqual((await tick(state, root)).got.map(r => r.n), [3]);
});

test('a rotated feed (another inode, even a longer one) is read from 0', async () => {
  const root = tmp();
  const f = path.join(root, run('a'));
  put(root, run('a'), row('x', { n: 1 }));
  const state = { files: {} };
  await tick(state, root);
  fs.renameSync(f, f + '.1');
  put(root, run('a'), row('y', { n: 2 }) + row('y', { n: 3 }) + row('y', { n: 4 }), 'w');
  assert.ok(fs.statSync(f).size > state.files[run('a')].pos);
  assert.deepEqual((await tick(state, root)).got.map(r => r.n), [2, 3, 4]);
});

test('a moved feed keeps its cursor; a vanished one loses it', async () => {
  const root = tmp();
  put(root, LEGACY_FEED, row('x', { n: 1 }));
  const state = { files: {} };
  await tick(state, root);
  fs.mkdirSync(path.join(root, 'run', 'sol-usdc'), { recursive: true });
  fs.renameSync(path.join(root, LEGACY_FEED), path.join(root, MOVED_FEED));
  put(root, MOVED_FEED, row('x', { n: 2 }));
  assert.deepEqual((await tick(state, root)).got.map(r => r.n), [2]);
  assert.deepEqual(Object.keys(state.files), [MOVED_FEED]);
  fs.unlinkSync(path.join(root, MOVED_FEED));
  await tick(state, root);
  assert.deepEqual(state.files, {});
});

test('the cursor is saved before the rows are sent, and one failed send does not stop the rest', async () => {
  const root = tmp();
  put(root, run('a'), row('x', { n: 1 }) + row('x', { n: 2 }));
  const state = { files: {} };
  const order = [];
  const orig = console.error;
  console.error = () => {};
  try {
    await tailAll(state, root, async (r) => { order.push(`send ${r.n}`); if (r.n === 1) throw new Error('telegram down'); },
      (st) => order.push(`save ${st.files[run('a')].pos}`));
  } finally { console.error = orig; }
  const size = fs.statSync(path.join(root, run('a'))).size;
  assert.deepEqual(order, [`save ${size}`, 'send 1', 'send 2']);
});

// --- the cursor at the deploy ---------------------------------------------------------

test('the old single cursor follows the feed the deploy moved: nothing again, nothing skipped', async () => {
  const root = tmp();
  const old = row('in_band', { n: 1 }) + row('in_band', { n: 2 });
  put(root, MOVED_FEED, old + row('OPEN', { n: 3 }));                      // the moved feed, one row since
  put(root, LEGACY_FEED, row('CLOSE', { n: 4 }));                          // written by the old process after the move
  put(root, run('mu-usdc'), row('startup', { n: 5 }));                     // a new profile started before the bridge
  const state = migrateState({ pos: old.length }, root);
  assert.equal(state.files[MOVED_FEED].pos, old.length);
  assert.equal(state.files[MOVED_FEED].ino, fs.statSync(path.join(root, MOVED_FEED)).ino);
  assert.equal(state.files[LEGACY_FEED], undefined);                      // shorter than the cursor: a new file
  assert.deepEqual((await tick(state, root)).got.map(r => r.n).sort(), [3, 4, 5]);
});

test('the old cursor stays on the legacy feed when nothing moved it, and on both when the move left a copy', async () => {
  const root = tmp();
  const old = row('x', { n: 1 });
  put(root, LEGACY_FEED, old + row('x', { n: 2 }));
  let state = migrateState({ pos: old.length }, root);
  assert.deepEqual(Object.keys(state.files), [LEGACY_FEED]);
  assert.deepEqual((await tick(state, root)).got.map(r => r.n), [2]);
  put(root, MOVED_FEED, old + row('x', { n: 2 }));
  state = migrateState({ pos: old.length }, root);
  assert.deepEqual(Object.keys(state.files).sort(), [LEGACY_FEED, MOVED_FEED].sort());
  assert.deepEqual((await tick(state, root)).got.map(r => r.n), [2, 2]);  // the copy's one new row, once per file
});

test('a migrated state is kept; no state or a bad one starts every feed at 0', () => {
  const root = tmp();
  const files = { [run('a')]: { pos: 5, ino: 7 } };
  assert.deepEqual(migrateState({ files }, root), { files });
  for (const raw of [null, undefined, {}, { pos: 'x' }, { pos: -3 }, { pos: 0 }, 'junk']) {
    assert.deepEqual(migrateState(raw, root), { files: {} }, JSON.stringify(raw));
  }
  put(root, LEGACY_FEED, row('x'));
  assert.deepEqual(migrateState({ pos: 10_000 }, root), { files: {} }); // a cursor past every feed: rotated
});

test('readNew: a cursor at the end reads nothing; a lone newline is consumed', () => {
  const root = tmp();
  put(root, 'f', row('x', { n: 1 }));
  const f = path.join(root, 'f');
  const size = fs.statSync(f).size;
  for (const cur of [{ pos: size }, { pos: size, ino: fs.statSync(f).ino }]) {           // with and without an inode
    const r = readNew(f, cur);
    assert.deepEqual([r.rows, r.cur.pos], [[], size], JSON.stringify(cur));
  }
  put(root, 'f', '\n{"half');
  const r = readNew(f, { pos: size, ino: fs.statSync(f).ino });
  assert.deepEqual([r.rows, r.bad, r.cur.pos], [[], 0, size + 1]);
});

test('migrate: no cursor from zero or less; a feed exactly the cursor long keeps it', () => {
  const root = tmp();
  const text = row('x');
  put(root, MOVED_FEED, text);
  for (const pos of [0, -3, 'x', null]) assert.deepEqual(migrateState({ pos }, root), { files: {} }, String(pos));
  assert.equal(migrateState({ pos: text.length }, root).files[MOVED_FEED].pos, text.length);
});

test('readNew on a missing file keeps the cursor', () => {
  const cur = { pos: 9, ino: 1 };
  assert.deepEqual(readNew('/nonexistent/feed', cur), { rows: [], bad: 0, cur });
});

// --- the messages -------------------------------------------------------------------

test('every message carries its pool, except the portfolio of all pools', () => {
  assert.equal(poolLabel({ pair: 'MU/USDC', profile: 'mu-usdc' }), '[MU/USDC]');
  assert.equal(poolLabel({ profile: 'djt-usdc' }), '[djt-usdc]');
  assert.equal(poolLabel({}), '');
  assert.equal(poolLabel({ pair: '', profile: 'mu-usdc' }), '[mu-usdc]');
  assert.equal(poolLabel({ pair: '\u0000‮<b>x</b>' }), '[bx/b]');      // no control or markup characters
  assert.equal(poolLabel({ pair: 'A'.repeat(80) }).length, 34);
  assert.ok(message({ event: 'HARVEST', pair: 'MU/USDC', collected_usd: 1 }).startsWith('[MU/USDC] 🌾 HARVESTED $1.0000'));
  assert.ok(message({ event: 'HARVEST', collected_usd: 1 }).startsWith('🌾 HARVESTED'));
  assert.ok(message({ event: 'PORTFOLIO', pair: 'SOL/USDC', pools: [], wallets: [] }).startsWith('📊 PORTFOLIO'));
});

test('the pause events render', () => {
  assert.ok(render({ event: 'MACRO_PAUSE', kind: 'FOMC', event_at: '10-28 18:00', resume_minutes: 135, held: true, price: 120.5 })
    .startsWith('PAUSED · FOMC at 10-28 18:00 UTC: closing, waiting 50/50 · reopens in 135 min\nprice 120.5'));
  assert.ok(render({ event: 'MACRO_PAUSE', kind: 'FOMC', event_at: '10-28 18:00', resume_minutes: 130, held: false })
    .startsWith('PAUSED · FOMC at 10-28 18:00 UTC: no band held, waiting 50/50 · reopens in 130 min\n'));
  assert.equal(render({ event: 'hot_paused', kind: 'macro', paused_minutes: 30, until_minutes: 105 }),
    'paused 30 min · macro window · reopens in 105 min');
  assert.equal(render({ event: 'macro_blocked', kind: 'FOMC', event_at: '10-28 18:00', reason: 'r' }),
    'MACRO PAUSE HELD BACK · FOMC at 10-28 18:00 UTC · r');
  assert.equal(render({ event: 'macro_calendar_empty', reason: 'add dates' }), 'CALENDAR · add dates');
  assert.equal(render({ event: 'macro_unread', reason: 'x' }), 'CALENDAR UNREADABLE · x · no macro pause until it reads');
});

test('the new events render', () => {
  assert.equal(render({ event: 'dormant', deployable_usd: 1.5, min_deploy_usd: 5, poll_seconds: 300 }),
    'DORMANT · no position and too little to deploy\ndeployable $1.50 · opens from $5.00 · light poll every 300s');
  assert.equal(render({ event: 'dormant', reason: 'sleeve $0.20' }), 'DORMANT · sleeve $0.20');
  assert.equal(render({ event: 'deposit_seen', amount: 12.5, symbol: 'MU', usd: 125 }),
    'DEPOSIT SEEN · 12.5 MU ($125.00) waking: swap to 50/50, then open');
  assert.equal(render({ event: 'claim_overdraw', symbol: 'USDC', claim: 3.2, delta: -4.1 }),
    'CLAIM OVERDRAW · USDC · claim 3.2 · change -4.1 → floored at 0');
  // the fields rebalancer.settle and locked_chain send
  assert.equal(render({ event: 'claim_overdraw', reason: 'a write moved more', command: 'open', mint: 'EPjF', claim: 1,
                        delta: -2, overdraw: 1 }),
    'CLAIM OVERDRAW · EPjF · claim 1 · change -2 · over by 1 → floored at 0 (open)\na write moved more');
  assert.equal(render({ event: 'wallet_lock_timeout', wallet_id: 'sol-lp', reason: 'busy', command: 'open', waited_s: 20,
                        action: 'open not sent; retried at the next poll' }),
    'WALLET LOCK TIMEOUT · sol-lp busy for 20s · open not sent; retried at the next poll');
  assert.equal(render({ event: 'deposit_seen', usd: 12, deployable_usd: 12, reason: 'waking: swap to 50/50, then open' }),
    'DEPOSIT SEEN · ($12.00) waking: swap to 50/50, then open');
  assert.equal(render({ event: 'wallet_lock_timeout', wallet_id: 'sol-lp', waited_s: 30 }),
    'WALLET LOCK TIMEOUT · sol-lp busy for 30s · nothing sent; retried at the next poll');
  for (const ev of ['dormant', 'deposit_seen', 'claim_overdraw', 'wallet_lock_timeout', 'PORTFOLIO']) {
    assert.ok(!render({ event: ev }).includes('undefined'), ev);
    assert.notEqual(message({ event: ev }).split(' ')[0], '▫️', `${ev} has its own emoji`);
  }
  const emoji = JSON.parse(fs.readFileSync(path.join(HERE, '..', 'event_emoji.json'), 'utf8'));
  const mine = ['PORTFOLIO', 'dormant', 'deposit_seen', 'claim_overdraw', 'wallet_lock_timeout'].map(e => emoji[e]);
  const others = Object.entries(emoji).filter(([k]) => !k.startsWith('_') && !['PORTFOLIO', 'dormant', 'deposit_seen',
    'claim_overdraw', 'wallet_lock_timeout'].includes(k)).map(([, v]) => v);
  for (const e of mine) assert.ok(!others.includes(e), `${e} is grep-able: no other event has it`);
});

test('an unknown event shows its fields, without the pool fields the label already shows', () => {
  assert.equal(render({ event: 'zzz', a: 1, profile: 'mu-usdc', wallet_id: 'sol-lp', chain: 'solana', pair: 'MU/USDC' }),
    'zzz · {"event":"zzz","a":1}');
});

test('the DAILY line names the pool\'s token', () => {
  const d = { event: 'DAILY', day: '2026-10-01', recentres: 3, fees_usd: 1, fees_earned_usd: 2, vs_hold_usd: 0.5,
              price_open: 10, price_close: 11 };
  assert.ok(render(d).endsWith('SOL 10.00 → 11.00'));
  assert.ok(render({ ...d, pair: 'MU/USDC' }).endsWith('MU 10.00 → 11.00'));
});

test('the book names the pool\'s token, and a sum of pools has no token amount', () => {
  const r = { equity_usd: 100, last_price: 9.5, token_a: 'MU', since_start: { profit_usd: 1, start_usd: 90,
    start_sol: 2, since: '2026-10-01T00:00', value_usd: 91, vs_hold_start_assets_usd: 0.5 } };
  assert.equal(equityLine(r), 'equity      $100.00 · MU $9.50   P&L +1.00 since start · includes pending fees');
  assert.equal(sinceStartLine(r), 'start       $90.00 (2.0000 MU, 2026-10-01) · now $91.00 · vs holding it +0.50');
  assert.equal(sinceStartLine({ since_start: { ...r.since_start, start_sol: null } }),
    'start       $90.00 (2026-10-01) · now $91.00 · vs holding it +0.50');
});

test('secrets never reach Telegram', () => {
  const key = Array.from({ length: 64 }, (_, i) => i * 3 % 256).join(',');
  const cases = [
    ['rpc https://mainnet.helius-rpc.com/?api-key=abcd-1234&x=1', 'abcd-1234'],
    ['https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/sendMessage', 'AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw'],
    [`key file [${key}]`, key],
    [`private_key=0x${'ab'.repeat(32)}`, 'ab'.repeat(32)],
    [`"secret": "${'cd'.repeat(32)}"`, 'cd'.repeat(32)],
  ];
  for (const [text, secret] of cases) {
    assert.ok(!redact(text).includes(secret), text);
    assert.ok(!message({ event: 'zzz', detail: text }).includes(secret), text);
  }
  const tx = '0x' + 'ef'.repeat(32);                                      // a transaction hash is not a key
  assert.ok(redact(`sent ${tx}`).includes(tx));
  const sig = '5'.repeat(88);
  assert.ok(redact(`sig ${sig}`).includes(sig));
});

const PORTFOLIO = {
  event: 'PORTFOLIO', ts: '2026-10-01T00:00:00+00:00',
  pools: [
    { profile: 'mu-usdc', wallet_id: 'sol-lp', pair: 'MU/USDC', equity_usd: 101, fees_total_usd: 1.3,
      fees_per_day_24h_usd: 2.5, apr_24h_pct: 903.4, profit_usd: -0.4, in_range_pct: 88 },
    { profile: 'sol-usdc', wallet_id: 'sol-lp', pair: 'SOL/USDC', equity_usd: 241, fees_total_usd: 2.9,
      fees_per_day_24h_usd: 5.1, apr_24h_pct: 772.4, profit_usd: 1.2, in_range_pct: 100 },
    { profile: 'base-weth-usdc', wallet_id: 'base-lp', pair: 'WETH/USDC', equity_usd: 50, fees_total_usd: null,
      fees_per_day_24h_usd: null, apr_24h_pct: null, profit_usd: null, in_range_pct: null },
  ],
  wallets: [
    { wallet_id: 'base-lp', chain: 'base', pools: ['base-weth-usdc'],
      subtotal: { equity_usd: 50, lp_usd: 45, idle_usd: 5, fees_total_usd: null, paid_usd: 0 } },
    { wallet_id: 'sol-lp', chain: 'solana', pools: ['mu-usdc', 'sol-usdc'],
      subtotal: { equity_usd: 342, lp_usd: 315, idle_usd: 26.2, fees_total_usd: 4.2, paid_usd: 1.5 } },
  ],
  total: { pools: 3, equity_usd: 392, lp_usd: 360, idle_usd: 31.2, fees_total_usd: 4.2, fees_today_usd: 4.2,
           fees_per_day_24h_usd: 7.6, apr_24h_pct: 707.6, paid_usd: 1.5, reinvested_usd: 0.25, gas_usd: 0,
           profit_usd: 0.8 },
  dormant: ['djt-usdc'], disabled: [], disabled_holding: [],
};

test('the portfolio: pools by wallet, subtotals, the total in dollars, dormant names', () => {
  const t = portfolioText(PORTFOLIO);
  assert.equal(t.split('\n')[0], 'PORTFOLIO · 3 active pools · 2 wallets');
  assert.ok(t.includes('▸ sol-lp (solana)\n  MU/USDC  equity $101.00 · fees $1.3000 (24h $2.5000/d) · APR 24h 903% · P&L -0.40 · in range 88%'), t);
  assert.ok(t.includes('  WETH/USDC  equity $50.00 · fees — (24h —/d) · APR 24h — · P&L — · in range —'), t);
  assert.ok(t.includes('  subtotal  equity $342.00 · in LP $315.00'), t);
  assert.ok(t.includes('TOTAL\n  equity $392.00 · in LP $360.00 · idle $31.20'), t);
  assert.ok(t.includes('paid $1.5000 · reinvested $0.2500 · gas $0.0000 · P&L +0.80'), t);
  assert.ok(t.endsWith('dormant 1 (djt-usdc) · disabled 0'), t);
  const one = portfolioText({ ...PORTFOLIO, wallets: [PORTFOLIO.wallets[1]] });
  assert.ok(!one.includes('subtotal'));                                    // one wallet: its subtotal is the total
  assert.ok(portfolioText({ ...PORTFOLIO, disabled: ['x'], disabled_holding: ['x'] })
    .endsWith('disabled 1 (x) · DISABLED BUT HOLDING A POSITION: x'));
  assert.equal(portfolioText({}), 'PORTFOLIO · 0 active pools · 0 wallets\ndormant 0 · disabled 0');
  assert.equal(portfolioText({ pools: [{ profile: 'a', pair: '' }], wallets: [{ wallet_id: 'w', chain: '', pools: ['a', 'b'] }] }),
    'PORTFOLIO · 1 active pool · 1 wallet\n▸ w (—)\n'
    + '  a  equity — · fees — (24h —/d) · APR 24h — · P&L — · in range —\n'
    + '  b  equity — · fees — (24h —/d) · APR 24h — · P&L — · in range —\ndormant 0 · disabled 0');
  assert.ok(portfolioText({ pools: [], wallets: [{ wallet_id: 'w' }] }).includes('▸ w (—)'));
  assert.ok(!message(PORTFOLIO).includes('undefined'));
});

// --- a wallet's name: its id and its address's first 10 characters (2026-10-02) -------------

test('a wallet is named by its id and its address tag', () => {
  assert.equal(walletName({ wallet_id: 'sol-lp2', wallet_tag: 'FogqBWLC4y' }), 'sol-lp2 FogqBWLC4y');
  assert.equal(walletName({ wallet_id: 'sol-lp2', address: 'FogqBWLC4y94csrniURTbGrx7ff7jFa4e2qp1GsgyAmM' }),
    'sol-lp2 FogqBWLC4y');                                                  // no tag: the address's first 10
  assert.equal(walletName({ wallet_id: 'b', wallet_tag: 'TAG', address: 'ADDRESS12345' }), 'b TAG');   // the tag first
  assert.equal(walletName({ wallet_id: 'sol-lp' }), 'sol-lp');
  assert.equal(walletName({ wallet_id: 'sol-lp', wallet_tag: '', address: '' }), 'sol-lp');
  assert.equal(walletName({ wallet_tag: 'FogqBWLC4y' }), '— FogqBWLC4y');
  assert.equal(walletName(null), '—');
  assert.equal(walletName({ wallet_id: '' }), '');                         // an empty id is shown as it is
  assert.equal(walletName({ wallet_id: 'x', address: 12345678901234 }), 'x 1234567890');
});

test('the pool label carries the wallet tag: two wallets can run one pair', () => {
  assert.equal(poolLabel({ pair: 'SOL/USDC', wallet_tag: 'FogqBWLC4y' }), '[SOL/USDC · FogqBWLC4y]');
  assert.equal(poolLabel({ pair: 'SOL/USDC', wallet_tag: '83HxMUUC7c' }), '[SOL/USDC · 83HxMUUC7c]');
  assert.equal(poolLabel({ pair: 'SOL/USDC', wallet_tag: null }), '[SOL/USDC]');
  assert.equal(poolLabel({ pair: 'SOL/USDC', wallet_tag: '' }), '[SOL/USDC]');
  assert.equal(poolLabel({ pair: 'SOL/USDC', wallet_tag: '<b>‮0x2b35948898e1' }), '[SOL/USDC · b0x2b35948]');
  assert.equal(poolLabel({ wallet_tag: 'FogqBWLC4y' }), '');                 // no pool: no label
  assert.equal(poolLabel({ pair: '‮', wallet_tag: 'FogqBWLC4y' }), '');
  assert.equal(poolLabel({ profile: 'sol-swing', wallet_tag: 'FogqBWLC4y' }), '[sol-swing · FogqBWLC4y]');
  assert.ok(message({ event: 'HARVEST', pair: 'SOL/USDC', wallet_tag: 'FogqBWLC4y', collected_usd: 1 })
    .startsWith('[SOL/USDC · FogqBWLC4y] 🌾 HARVESTED $1.0000'));
  assert.equal(render({ event: 'wallet_lock_timeout', wallet_id: 'sol-lp2', wallet_tag: 'FogqBWLC4y', waited_s: 30 }),
    'WALLET LOCK TIMEOUT · sol-lp2 FogqBWLC4y busy for 30s · nothing sent; retried at the next poll');
  assert.equal(render({ event: 'wallet_lock_timeout', waited_s: 30, wallet_tag: 'FogqBWLC4y' }),
    'WALLET LOCK TIMEOUT · wallet busy for 30s · nothing sent; retried at the next poll');
  assert.equal(render({ event: 'zzz', a: 1, wallet_id: 'sol-lp2', wallet_tag: 'FogqBWLC4y', pair: 'SOL/USDC' }),
    'zzz · {"event":"zzz","a":1}');                                        // the label shows the tag
});

test('a DAILY line across two pairs has no hold and no price', () => {
  const d = { event: 'DAILY', day: '2026-10-01', recentres: 3, fees_usd: 1, fees_earned_usd: 2, vs_hold_usd: null,
              price_open: null, price_close: null };
  assert.equal(render(d), 'DAILY 2026-10-01 · 3 re-centres · fees earned $2.00 (harvested $1.00) · vs 50/50 hold —');
  assert.ok(render({ ...d, vs_hold_usd: -0.25, price_open: 10 }).endsWith('vs 50/50 hold -0.25 · SOL 10.00 → —'));
  assert.ok(render({ ...d, price_close: 11 }).endsWith('vs 50/50 hold — · SOL — → 11.00'));
  assert.ok(render({ ...d, vs_hold_usd: 0 }).includes('vs 50/50 hold +0.00'));
});

const TWO_SOL = {
  event: 'PORTFOLIO', ts: '2026-10-02T00:00:00+00:00',
  pools: [
    { profile: 'sol-usdc', wallet_id: 'sol-lp', wallet_tag: '83HxMUUC7c', pair: 'SOL/USDC', equity_usd: 240,
      fees_total_usd: 2.4, by_pool: [{ pool: '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj', pair_label: 'SOL/USDC',
        dex: 'raydium-clmm', open_now: 1, fees_usd: 2.4, in_range_pct: 100 }] },
    { profile: 'sol-swing', wallet_id: 'sol-lp2', wallet_tag: 'FogqBWLC4y', pair: 'SOL/USDC', equity_usd: 152,
      fees_total_usd: 2.8, by_pool: [
        { pool: '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj', pair_label: 'SOL/USDC', dex: 'raydium-clmm', open_now: 1,
          fees_usd: 2.3, in_range_pct: 90 },
        { pool: '7gkB2D1SqhUYgKrSpDU5cma4tK9efijHouYituABdJcG', pair_label: 'DJT/USDC', dex: 'orca', open_now: 0,
          fees_usd: 0.5, in_range_pct: null }] },
  ],
  wallets: [
    { wallet_id: 'sol-lp', wallet_tag: '83HxMUUC7c', chain: 'solana', pools: ['sol-usdc'],
      subtotal: { equity_usd: 240, fees_total_usd: 2.4 } },
    { wallet_id: 'sol-lp2', wallet_tag: 'FogqBWLC4y', chain: 'solana', pools: ['sol-swing'],
      subtotal: { equity_usd: 152, fees_total_usd: 2.8 } },
  ],
  total: { pools: 2, equity_usd: 392, fees_total_usd: 5.2 },
  dormant: [], disabled: [], disabled_holding: [],
};

test('the portfolio: two Solana wallets by tag, and the swing per pool', () => {
  const t = portfolioText(TWO_SOL);
  assert.ok(t.includes('▸ sol-lp 83HxMUUC7c (solana)\n  SOL/USDC  equity $240.00'), t);
  assert.ok(t.includes('▸ sol-lp2 FogqBWLC4y (solana)\n  SOL/USDC  equity $152.00 · fees $2.8000'), t);
  assert.ok(t.includes('\n    ▸ SOL/USDC raydium-clmm 8sLbNZoA1c  fees $2.3000 · in range 90%'
    + '\n    · DJT/USDC orca 7gkB2D1Sqh  fees $0.5000 · in range —\n  subtotal  equity $152.00'), t);
  assert.equal(t.split('\n').filter(l => l.startsWith('    ')).length, 2);   // one pool: no pool lines
  assert.ok(t.includes('  subtotal  equity $240.00'), t);
  assert.ok(t.includes('TOTAL\n  equity $392.00'), t);
  // a pool row with nothing in it says so, no 'undefined'
  const bare = portfolioText({ pools: [{ profile: 'p', by_pool: [{}, {}] }], wallets: [{ wallet_id: 'w', pools: ['p'] }] });
  assert.ok(bare.includes('\n    · — — —  fees — · in range —\n    · — — —  fees — · in range —'), bare);
  assert.ok(!bare.includes('undefined'));
  assert.ok(!portfolioText({ pools: [{ profile: 'p', by_pool: 'x' }], wallets: [{ wallet_id: 'w', pools: ['p'] }] })
    .includes('    '));                                                     // not a list: no pool lines
});

// --- the existing events render as they did, with the prefix ---------------------------

async function oldBridge() {
  // the pre-merge bridge's render(), from git: its file runs a loop at
  // import, so the function is cut out of it
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'old_bridge_'));
  const src = OLD_SRC.bridge;
  const body = src.slice(src.indexOf('function render(row) {'), src.indexOf('async function tail() {'));
  fs.writeFileSync(path.join(dir, 'book_format.mjs'), OLD_SRC.book);
  fs.writeFileSync(path.join(dir, 'old.mjs'),
    "import { equityLine, lpLine, sinceStartLine, emojiFor, healthLine } from './book_format.mjs';\n"
    + `const EMOJI = ${OLD_SRC.emoji};\n`
    + body + '\nexport const oldMessage = (row) => `${emojiFor(row.event, EMOJI)} ${render(row)}`;\n');
  return (await import(path.join(dir, 'old.mjs'))).oldMessage;
}

const OLD_SRC = { bridge: gitShow('telegram_bridge.mjs'), book: gitShow('book_format.mjs'), emoji: gitShow('event_emoji.json') };
const hasOld = Object.values(OLD_SRC).every(Boolean) && OLD_SRC.bridge.includes('async function tail() {');

test('every row of the real feed renders as before, behind its pool label', { skip: !hasOld }, async () => {
  const oldMessage = await oldBridge();
  const feeds = [path.join(HERE, '..', 'events.jsonl'), path.join(LIVE, 'events.jsonl'),
    path.join(LIVE, MOVED_FEED)].filter(f => fs.existsSync(f));
  if (!feeds.length) return;
  let n = 0;
  for (const f of feeds) {
    for (const line of fs.readFileSync(f, 'utf8').split('\n')) {
      if (!line.trim()) continue;
      let r;
      try { r = JSON.parse(line); } catch { continue; }
      const label = poolLabel(r);
      let want = oldMessage(r);
      if (want.includes(` ${r.event} · {`)) {                             // the generic JSON: the label's fields leave it
        const { profile, wallet_id, chain, pair, ...rest } = r;
        want = oldMessage(rest);
      }
      // the old bridge printed "move at 2 steps" whatever the configured steps (ab89dc0 shows them)
      if (r.regime?.steps != null) want = want.replace('move at 2 steps', `move at ${r.regime.steps} steps`);
      if (r.event === 'PAYOUT' && r.gas_low) continue;                    // the held payout's own text and ⛽ (payout-gas-emoji)
      let got = message(r);
      // DAILY lines gained "capital moved" (665c039) and the reference-period line (9aaf63c) after the old bridge
      if (r.event === 'DAILY') got = got.replace(/ · capital moved [^\n·]*/, '').replace(/\nvs [^\n]*?\(avg [^\n]*?(?= · SOL| ·? *$|$)/, '');
      assert.equal(got, (label ? label + ' ' : '') + want, `${f}: ${line.slice(0, 200)}`);
      n += 1;
    }
  }
  assert.ok(n > 1000, `only ${n} rows`);
});

// --- the program, end to end, against a stub Telegram -------------------------------------

test('the bridge migrates the old cursor at start and sends only what is new, labelled', async () => {
  const { spawn } = await import('node:child_process');
  const dir = tmp();
  for (const f of ['telegram_bridge.mjs', 'book_format.mjs', 'event_emoji.json']) {
    fs.copyFileSync(path.join(HERE, '..', f), path.join(dir, f));
  }
  const old = row('in_band', { n: 1 }) + row('in_band', { n: 2 });
  put(dir, MOVED_FEED, old + row('OPEN', { n: 3, pair: 'SOL/USDC', profile: 'sol-usdc' }));
  put(dir, run('mu-usdc'), row('dormant', { n: 4, pair: 'MU/USDC', reason: 'nothing to deploy' }) + '{"half');
  fs.writeFileSync(path.join(dir, 'telegram_bridge_state.json'), JSON.stringify({ pos: old.length }));
  const sent = path.join(dir, 'sent.jsonl');
  // Telegram, stubbed: every request is written to a file and answered ok
  fs.writeFileSync(path.join(dir, 'stub.mjs'), `import fs from 'fs';
globalThis.fetch = async (url, init) => { fs.appendFileSync(${JSON.stringify(sent)},
  JSON.stringify({ host: new URL(url).host, body: JSON.parse(init.body) }) + '\\n'); return { json: async () => ({ ok: true }) }; };`);
  const child = spawn(process.execPath, ['--import', path.join(dir, 'stub.mjs'), path.join(dir, 'telegram_bridge.mjs')],
    { env: { PATH: process.env.PATH, TELEGRAM_BOT_TOKEN: '1:x', TELEGRAM_CHAT_ID: '42' }, stdio: 'ignore' });
  try {
    const until = Date.now() + 15000;
    while (Date.now() < until && !(fs.existsSync(sent) && fs.readFileSync(sent, 'utf8').split('\n').filter(Boolean).length >= 2)) {
      await new Promise(r => setTimeout(r, 100));
    }
  } finally { child.kill(); }
  const msgs = fs.readFileSync(sent, 'utf8').split('\n').filter(Boolean).map(l => JSON.parse(l));
  assert.deepEqual(msgs.map(m => m.host), ['api.telegram.org', 'api.telegram.org']);
  assert.deepEqual(msgs.map(m => m.body.chat_id), ['42', '42']);
  const texts = msgs.map(m => m.body.text).sort();
  assert.ok(texts[0].startsWith('[MU/USDC] 😴 DORMANT · nothing to deploy'), texts[0]);
  assert.ok(texts[1].startsWith('[SOL/USDC] 🟩 OPENED'), texts[1]);
  const st = JSON.parse(fs.readFileSync(path.join(dir, 'telegram_bridge_state.json'), 'utf8'));
  assert.equal(st.files[MOVED_FEED].pos, fs.statSync(path.join(dir, MOVED_FEED)).size);
  assert.equal(st.files[run('mu-usdc')].pos, row('dormant', { n: 4, pair: 'MU/USDC', reason: 'nothing to deploy' }).length);
});

test('a DAILY line names the capital moved, and only when some moved', () => {
  const d = { event: 'DAILY', day: '2026-10-03', recentres: 1, fees_usd: 1, fees_earned_usd: 2, vs_hold_usd: 0.29,
              price_open: null, price_close: null };
  assert.equal(render({ ...d, net_flows_usd: -8.34 }),
    'DAILY 2026-10-03 · 1 re-centres · fees earned $2.00 (harvested $1.00) · vs 50/50 hold +0.29 · capital moved -8.34');
  assert.ok(!render({ ...d, net_flows_usd: 0 }).includes('capital moved'));
  assert.ok(!render(d).includes('capital moved'));                     // a line from before the field
});

test('the REGIME block names the configured steps, 2 for a feed from before the field', () => {
  const regime = { mode: 'WARM', held_pct: 1.5, choice_pct: 1.5, probs: [[1.5, 0.2]], threshold: 0.25, horizon_minutes: 120 };
  const text = (g) => render({ event: 'in_band', regime: g });
  assert.match(text({ ...regime, steps: 3 }), /narrowest width ≤ 25%, move at 3 steps, exits re-centre/);
  assert.match(text(regime), /move at 2 steps/);
});

test('a DAILY line names the reference period average under it', () => {
  const d = { event: 'DAILY', day: '2026-10-04', recentres: 6, fees_usd: 3, fees_earned_usd: 3.9, vs_hold_usd: 0.8,
              value_change_usd: 1.2, price_open: null, price_close: null,
              compare: { days: 6, from: '2026-09-27', to: '2026-10-02', fees_earned_usd: 3.5602, recentres: 10.3333,
                         value_change_usd: 0.1546, vs_hold_usd: 0.5748 } };
  assert.ok(render(d).endsWith('\nvs 09-27→10-02 (6d): fees $3.90 (avg $3.56) · value +1.20 (avg +0.15)'
    + ' · vs hold +0.80 (avg +0.57) · re-centres 6.0 (avg 10.3)'));
  assert.ok(!render({ ...d, compare: null }).includes('\nvs '));
  assert.ok(render({ ...d, vs_hold_usd: null, compare: { ...d.compare, vs_hold_usd: null } }).includes('vs hold — (avg —)'));
});

test('USDC/HYPE (stable token A): in-range and out-of-range lines show HYPE in dollars, band low to high', () => {
  const m = render({ event: 'in_band', pair: 'USDC/HYPE', price: 1 / 90, lower: 1 / 92, upper: 1 / 88 });
  assert.ok(m.includes('· 90.0000\n'), m);
  assert.ok(m.includes('band 88.0000 — 92.0000'), m);
  const o = render({ event: 'OUT_OF_BAND', pair: 'USDC/HYPE', side: 'above', price: 1 / 87, lower: 1 / 92, upper: 1 / 88, action: 'x' });
  assert.ok(o.includes('went below') && o.includes('price 87.0000'), o);
  const s = render({ event: 'OUT_OF_BAND', pair: 'SOL/USDC', side: 'above', price: 125, lower: 118, upper: 122, action: 'x' });
  assert.ok(s.includes('went above') && s.includes('band 118.0000 — 122.0000'), s);
});

// 2026-10-05: sol-swing held SOL/USDC and DJT/USDC; the book's A side has no
// single amount and showed '— DJT'. Each A token now shows on its own.
test('aSide: one token as before, a mixed side per token', () => {
  const tok = (x) => (x == null ? '—' : String(Number(Number(x).toFixed(6))));
  assert.equal(aSide(1.5, 'SOL', null, tok), '1.5 SOL');
  assert.equal(aSide(1.5, 'SOL', { DJT: 9 }, tok), '1.5 SOL');                 // a known amount wins
  assert.equal(aSide(null, 'DJT', null, tok), '— DJT');
  assert.equal(aSide(null, 'DJT', {}, tok), '— DJT');
  assert.equal(aSide(null, 'DJT', { DJT: null }, tok), '— DJT');
  assert.equal(aSide(null, 'DJT', { SOL: 0.025958, DJT: 0.046633 }, tok), '0.046633 DJT 0.025958 SOL');
  assert.equal(aSide(null, 'DJT', { DJT: 0.035972, SOL: 0 }, tok), '0.035972 DJT');      // a zero is noise
  assert.equal(aSide(null, 'DJT', { DJT: 0, SOL: 0 }, tok), '0 DJT 0 SOL');               // unless all are
  assert.equal(aSide(0, 'SOL', { DJT: 1 }, tok), '0 SOL');                                 // 0 is an amount
});

test('aSide property: every non-zero token once, largest first', () => {
  const tok = (x) => (x == null ? '—' : String(Number(Number(x).toFixed(6))));
  let seed = 7; const rnd = () => (seed = (seed * 48271) % 2147483647) / 2147483647;
  for (let i = 0; i < 300; i++) {
    const per = {};
    for (const t of ['SOL', 'DJT', 'MU']) if (rnd() < 0.7) per[t] = rnd() < 0.2 ? 0 : Math.round(rnd() * 1e6) / 1e3;
    const out = aSide(null, 'A', per, tok);
    if (!Object.keys(per).length) { assert.equal(out, '— A'); continue; }
    const parts = out.split(' ');
    const toks = parts.filter((_, j) => j % 2), vals = parts.filter((_, j) => !(j % 2)).map(Number);
    const nz = Object.keys(per).filter(t => per[t]);
    assert.deepEqual(new Set(toks), new Set(nz.length ? nz : Object.keys(per)), out);
    assert.deepEqual(vals, [...vals].sort((p, q) => q - p), out);
  }
});

test('the book of a swing names each A token', () => {
  const row = { event: 'in_band', token_a: 'DJT', token_b: 'USDC', pair: 'DJT/USDC',
    fees_today_a: null, fees_today_b: 0.5, fees_today_usd: 2.1,
    fees_realised_a: null, fees_realised_b: 3.281015, fees_realised_usd: 6.8273,
    fees_unrealised_a: null, fees_unrealised_b: 0.14, fees_unrealised_usd: 0.45,
    fees_total_a: null, fees_total_b: 3.42, fees_total_usd: 7.28,
    fees_a_by_token: { today: { DJT: 0.082605, SOL: 0.004741 }, realised: { DJT: 0.046633, SOL: 0.025958 },
                       unrealised: { DJT: 0.035972, SOL: 0 }, total: { DJT: 0.082605, SOL: 0.025958 } } };
  const text = render(row);
  assert.ok(!/— DJT/.test(text), text);
  assert.match(text, /today\s+0\.082605 DJT 0\.004741 SOL\s+0\.5 USDC/);
  assert.match(text, /realised\s+0\.046633 DJT 0\.025958 SOL\s+3\.281 USDC/);
  assert.match(text, /unrealised\s+0\.035972 DJT\s+0\.14 USDC/);
  assert.match(text, /TOTAL\s+0\.082605 DJT 0\.025958 SOL\s+3\.42 USDC/);
  const plain = render({ ...row, fees_a_by_token: null, fees_total_a: 1.25, token_a: 'SOL' });
  assert.match(plain, /TOTAL\s+1\.25 SOL/);
});

// --- a payout held for gas says so, with ⛽ ----------------------------------------------

test('a payout held for gas shows the pump, what stayed and why', () => {
  // the 2026-10-07 20:01Z DJT close: 0.694 USDC reinvested, the owner thought it a bug
  const row = { event: 'PAYOUT', pair: 'DJT/USDC', gas_low: true, sol_before: 0.049209, gas_reserve: 0.05,
                held: [{ symbol: 'USDC', amount: 0.693993, usd: 0.693993 }], sent: [],
                split: { paid: 0, reinvested: 1.2287, gas: 0 } };
  assert.equal(message(row),
    '[DJT/USDC] ⛽ PAYOUT HELD · gas low: 0.049209 SOL under the 0.05 SOL reserve\n'
    + '0.693993 USDC ($0.6940) reinvested, not sent to the profit wallet\n'
    + 'no SOL fee refilled gas · payouts resume when gas is back at the reserve\n'
    + 'split  paid $0.0000 · reinvested $1.2287 · gas $0.0000');
  // SOL/USDC: a SOL fee refills gas; an old row has no held list nor reserve
  assert.equal(payoutHeld({ gas_low: true, sol_before: 0.0429, split: { gas: 0.0068 } }),
    'PAYOUT HELD · gas low: 0.042900 SOL under the reserve\nnothing sent this harvest\n'
    + 'a SOL fee refilled gas $0.0068 · payouts resume when gas is back at the reserve');
  assert.ok(!message({ event: 'PAYOUT', gas_low: true }).includes('undefined'));
  // a normal payout keeps 💸 and its old text
  assert.equal(message({ event: 'PAYOUT', gas_low: false, sent: [], split: { paid: 0.3, reinvested: 0.3, gas: 0 } }),
    '💸 PAYOUT\nnothing sent this harvest\nsplit  paid $0.3000 · reinvested $0.3000 · gas $0.0000');
});
