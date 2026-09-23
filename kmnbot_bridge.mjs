// KMNBOT bridge for the LP rebalancer.
//
// Tails lp_bot/kmnbot_feed.jsonl and forwards every row to Telegram, so the
// bot is updated on each signal, entry, exit, skip and breaker event. It has NO
// trading, signing or execution path: it only reads a file and sends text.
//
// Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, the same pair kmn_notifier.mjs
// uses. Run it with the same EnvironmentFile as kamino-live.
import fs from 'fs';
import path from 'path';

const token = process.env.TELEGRAM_BOT_TOKEN;
const chatId = process.env.TELEGRAM_CHAT_ID;
if (!token || !chatId) throw new Error('needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID');

const DIR = path.dirname(new URL(import.meta.url).pathname);
const FEED = path.join(DIR, 'kmnbot_feed.jsonl');
const STATE_F = path.join(DIR, 'kmnbot_bridge_state.json');

let state = { pos: 0 };
try { state = { ...state, ...JSON.parse(fs.readFileSync(STATE_F, 'utf8')) }; } catch {}
const save = () => fs.writeFileSync(STATE_F, JSON.stringify(state));

async function send(text) {
  const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ chat_id: chatId, text }) });
  const j = await r.json();
  if (!j.ok) throw new Error(`telegram: ${JSON.stringify(j)}`);
}

const money = (x) => (x >= 0 ? '+' : '') + Number(x).toFixed(2);

function render(row) {
  const { event } = row;
  const n = (x, d = 2) => (x === undefined || x === null ? '-' : Number(x).toFixed(d));
  // Every message that reports money carries the same cumulative block, so a
  // rebalance can never make the earnings look like they went back to zero.
  const money = (r) =>
      `FEES  realised $${n(r.fees_realised_usd, 4)}`
    + `  +  unrealised $${n(r.fees_unrealised_usd, 4)}`
    + `  =  TOTAL $${n(r.fees_total_usd, 4)}\n`
    + `equity $${n(r.equity_usd)}`
    + (r.pnl_usd !== undefined && r.pnl_usd !== null ? `   P&L ${r.pnl_usd >= 0 ? '+' : ''}${n(r.pnl_usd)}` : '')
    + (r.fees_per_day_usd ? `\nrate $${n(r.fees_per_day_usd, 4)}/day` : '')
    + (r.in_range_pct !== undefined && r.in_range_pct !== null ? `   in range ${n(r.in_range_pct, 0)}%` : '')
    + `\npositions ${r.positions_opened ?? '-'}   rebands ${r.rebands ?? '-'}   harvests ${r.harvests ?? '-'}`;
  switch (event) {
    case 'startup':
      return `APERTURE online — ${row.mode}\n`
        + `${row.pair} · ${row.pool}\n`
        + `capital $${row.capital_usd}   poll ${row.poll_seconds}s\n`
        + `reopt every ${row.reopt_every_hours}h, min gain ${(row.reopt_min_gain * 100).toFixed(0)}%\n`
        + money(row);
    case 'in_band':
      return `IN BAND   price ${n(row.price, 4)}\n`
        + `band ${n(row.lower, 4)} — ${n(row.upper, 4)}\n` + money(row);
    case 'OUT_OF_BAND':
      return `OUT OF BAND — ${row.side}\n`
        + `price ${n(row.price, 4)}   band ${n(row.lower, 4)} — ${n(row.upper, 4)}\n`
        + `${row.action}`;
    case 'HARVEST':
      return `FEES COLLECTED  $${n(row.collected_usd, 4)}\n${row.signature ?? ''}`;
    case 'CLOSE':
      return `CLOSED (${row.reason})\n${row.signature ?? ''}\n` + money(row);
    case 'OPEN':
      return `NEW LP  ${row.pair}\n`
        + `band ${row.band}   ${n(row.lower, 4)} — ${n(row.upper, 4)}\n`
        + `expected ${n(row.expected_net_day_pct, 3)}%/day, `
        + `${n(row.modelled_rebalances_per_day, 2)} rebalances/day\n`
        + `reason: ${row.reason}\n${row.signature ?? ''}\n` + money(row);
    case 'REBAND':
      return `RE-OPTIMISED  ${row.old_band} -> ${row.new_band}\n`
        + `${n(row.old_net_day, 3)}%/day -> ${n(row.new_net_day, 3)}%/day `
        + `(+${row.improvement_pct}%)`;
    case 'reopt_checked':
      return `band check: holding ${row.held}, best ${row.best}\n${row.verdict}`;
    case 'status_unreadable':
      return `cannot read chain (${row.consecutive}): ${row.reason}\n${row.action}`;
    case 'close_recovered':
    case 'open_recovered':
      return `RECOVERED — ${row.detail}`;
    case 'close_failed':
    case 'open_failed':
      return `${event.toUpperCase()}: ${row.reason}\nfailures ${row.failures}`;
    case 'no_position':
      return `no position: ${row.detail}`;
    case 'idle':
      return `idle: ${row.reason}`;
    case 'BREAKER':
      return `BREAKER TRIPPED\n${row.reason}\n${row.action}`;
    case 'halted':
      return `HALTED: ${row.reason}`;
    default:
      return `${event}: ${JSON.stringify(row)}`;
  }
}

async function tail() {
  const st = fs.statSync(FEED, { throwIfNoEntry: false });
  if (!st) return;
  if (st.size < state.pos) state.pos = 0;        // the feed was rotated
  if (st.size === state.pos) return;
  const fd = fs.openSync(FEED, 'r');
  const buf = Buffer.alloc(st.size - state.pos);
  fs.readSync(fd, buf, 0, buf.length, state.pos);
  fs.closeSync(fd);
  state.pos = st.size;
  save();
  for (const line of buf.toString().split('\n')) {
    if (!line.trim()) continue;
    let row;
    try { row = JSON.parse(line); } catch { continue; }
    try { await send(render(row)); } catch (e) { console.error('send:', e.message); }
  }
}

console.error('kmnbot_bridge running');
for (;;) {
  try { await tail(); } catch (e) { console.error('tail:', e.message); }
  await new Promise(s => setTimeout(s, 5000));
}
