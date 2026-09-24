// Telegram bridge for the rebalancer.
//
// Tails events.jsonl and forwards every row to Telegram, so you see each poll,
// open, close, reband and breaker as it happens. It has NO trading, signing or
// execution path: it only reads a file and sends text.
//
// Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment.
import fs from 'fs';
import path from 'path';

const token = process.env.TELEGRAM_BOT_TOKEN;
const chatId = process.env.TELEGRAM_CHAT_ID;
if (!token || !chatId) throw new Error('needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID');

const DIR = path.dirname(new URL(import.meta.url).pathname);
const FEED = path.join(DIR, 'events.jsonl');
const STATE_F = path.join(DIR, 'telegram_bridge_state.json');

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

function render(row) {
  const { event } = row;
  const n = (x, d = 2) => (x === undefined || x === null ? '—' : Number(x).toFixed(d));
  const tok = (x, d = 6) => (x === undefined || x === null ? '—' : Number(x).toFixed(d).replace(/0+$/, '').replace(/\.$/, ''));
  const sign = (x) => (Number(x) >= 0 ? '+' : '') + n(x);
  const plural = (c, word) => (c == null ? `— ${word}s` : `${c} ${word}${c === 1 ? '' : 's'}`);

  // The standing report. Fees are earned in two currencies, so both are shown:
  // a single dollar figure moves when the price moves even if nothing was
  // earned. Realised is harvested and permanent; unrealised resets when the
  // position closes; the total only ever goes up.
  const book = (r) => {
    const A = r.token_a ?? 'A', B = r.token_b ?? 'B';
    return [
      `━━ FEES ━━`,
      `today       ${tok(r.fees_today_a)} ${A}   ${tok(r.fees_today_b, 4)} ${B}   $${n(r.fees_today_usd, 4)}`,
      `realised    ${tok(r.fees_realised_a)} ${A}   ${tok(r.fees_realised_b, 4)} ${B}   $${n(r.fees_realised_usd, 4)}`,
      `unrealised  ${tok(r.fees_unrealised_a)} ${A}   ${tok(r.fees_unrealised_b, 4)} ${B}   $${n(r.fees_unrealised_usd, 4)}`,
      `TOTAL       ${tok(r.fees_total_a)} ${A}   ${tok(r.fees_total_b, 4)} ${B}   $${n(r.fees_total_usd, 4)}`,
      ...(Array.isArray(r.by_pool) && r.by_pool.length > 1 ? [
        `━━ POOLS ━━`,
        ...r.by_pool.slice(0, 5).map(p =>
          `${p.open_now ? '▸' : '·'} ${p.dex} ${p.pair_label} ${n(p.days, 2)}d fees $${n(p.fees_usd, 4)}`
          + `${p.apr_pct != null ? ` APR ${n(p.apr_pct, 0)}%` : ''}`
          + `${p.pnl_usd != null ? ` P&L ${sign(p.pnl_usd)}` : ''}`
          + `${p.in_range_pct != null ? ` ${n(p.in_range_pct, 0)}% in` : ''}`),
        `all pools   fees $${n(r.fees_total_usd, 4)}   P&L ${r.pnl_all_pools_usd != null ? sign(r.pnl_all_pools_usd) : '—'}`,
      ] : []),
      `━━ BOOK ━━`,
      `equity      $${n(r.equity_usd)}   P&L ${sign(r.pnl_usd)}`,
      `rate        ${r.fees_per_day_usd != null ? '$' + n(r.fees_per_day_usd, 4) + '/day' : '— (needs an hour)'}`
        + `${r.apr_pct != null ? `   APR ${n(r.apr_pct, 1)}%` : ''} since start`,
      ...(r.fees_per_day_6h_usd != null ? [
        `last 6h     $${n(r.fees_per_day_6h_usd, 4)}/day${r.apr_6h_pct != null ? `   APR ${n(r.apr_6h_pct, 1)}%` : ''}`] : []),
      ...(r.fees_per_day_24h_usd != null ? [
        `last 24h    $${n(r.fees_per_day_24h_usd, 4)}/day${r.apr_24h_pct != null ? `   APR ${n(r.apr_24h_pct, 1)}%` : ''}`] : []),
      ...(r.season ? [
        `rhythm      ${String(r.season.hour_utc).padStart(2, '0')}h UTC ${n(r.season.now_x, 2)}x avg · next ${r.season.next_hours}h ${n(r.season.next_x, 2)}x`
        + `${r.expected_next_hours_fees_per_day_usd != null ? ` → ~$${n(r.expected_next_hours_fees_per_day_usd, 2)}/day` : ''}`
        + ` · peak ${String(r.season.peak_hour_utc).padStart(2, '0')}h trough ${String(r.season.trough_hour_utc).padStart(2, '0')}h`] : []),
      `in range    ${n(r.in_range_pct, 0)}%   over ${n(r.tracked_days, 2)}d`,
      `activity    ${plural(r.positions_opened, 'position')} · ${plural(r.rebands, 'reband')} · `
        + `${plural(r.harvests, 'harvest')}${r.failures ? ` · ${plural(r.failures, 'failure')}` : ''}`,
    ].join('\n');
  };

  switch (event) {
    case 'startup':
      return `REBALANCER · online\n${row.dex ?? 'orca'} ${row.pair} · capital $${n(row.capital_usd, 0)}\n`
        + `poll ${row.poll_seconds}s · reopt ${row.reopt_every_hours}h @ +${(row.reopt_min_gain * 100).toFixed(0)}%\n`
        + `board ${(row.scan_dexes ?? []).join(', ')} every ${row.scan_every_hours}h · `
        + `move @ +${((row.migrate_min_gain ?? 0) * 100).toFixed(0)}% · can open on ${(row.execute_dexes ?? []).join(', ')}\n`
        + book(row);
    case 'in_band':
      return `IN RANGE · ${row.position_dex ?? row.dex ?? ''} ${row.position_pair ?? row.pair ?? ''} · ${n(row.price, 4)}\n`
        + `band ${n(row.lower, 4)} — ${n(row.upper, 4)}\n`
        + book(row);
    case 'OUT_OF_BAND':
      return `OUT OF RANGE · went ${row.side}\n`
        + `price ${n(row.price, 4)} · band ${n(row.lower, 4)} — ${n(row.upper, 4)}\n${row.action}`;
    case 'move_deferred':
      return `${row.kind} deferred · holding ${row.held}, best ${row.best}\n${row.reason}`;
    case 'migrate_refused':
      return `move refused · ${row.reason}`;
    case 'REOPT_REQUESTED':
      return `REVIEW REQUESTED by operator · ${row.action}`;
    case 'REBALANCE_REQUESTED':
      return `REBALANCE REQUESTED by operator · price ${n(row.price, 4)}\n${row.action}`;
    case 'rebalance_deferred':
      return `rebalance deferred · ${row.seconds_remaining}s until the minimum gap`;
    case 'HARVEST':
      return `HARVESTED $${n(row.collected_usd, 4)}\n${row.signature ?? ''}`;
    case 'harvest_skipped':
      return `harvest skipped · ${row.reason}`;
    case 'CLOSE':
      return `CLOSED · ${row.reason}\n${row.signature ?? ''}\n` + book(row);
    case 'OPEN':
      return `OPENED · ${row.dex ? row.dex + ' ' : ''}${row.pair} ${row.band}\n`
        + `range ${n(row.lower, 4)} — ${n(row.upper, 4)}\n`
        + `deposited $${n(row.deposit_usd)} · caps ${row.cap_a ?? '—'} · ${row.cap_b ?? '—'}\n`
        + `modelled ${n(row.expected_net_day_pct, 3)}%/day at `
        + `${n(row.modelled_rebalances_per_day, 2)} rebalances/day\n`
        + `${row.reason}\n${row.signature ?? ''}\n` + book(row);
    case 'REBAND':
      return `REBANDED · ${row.old_band} → ${row.new_band}\n`
        + `${n(row.old_net_day, 3)}%/day → ${n(row.new_net_day, 3)}%/day (+${row.improvement_pct}%)`;
    case 'reopt_checked':
      return `band review · holding ${row.held}, best ${row.best}\n${row.verdict}`;
    case 'SCAN': {
      const top = (row.top ?? []).map(t =>
        `${t.can_open ? '▸' : '·'} ${t.dex} ${t.pair} ${t.band} ${n(t.net_day_pct, 3)}%/d`
        + `${t.drift != null && t.drift < 0.999 ? ` (tape ${n(t.drift, 2)}x → use ${n(t.use_day_pct, 3)})` : ''} `
        + `${n(t.rebal_per_day, 2)} reb/d $${n(t.tvl_musd, 1)}M${t.ok ? '' : ' ✗'}`).join('\n');
      const errs = row.errors ? `\nerrors: ${Object.entries(row.errors).map(([k, v]) => `${k}: ${v}`).join('; ')}` : '';
      return `BOARD #${row.run} · ${row.scored}/${row.listed} pools scored in ${row.seconds}s`
        + `${row.season_now != null ? ` · this hour ${n(row.season_now, 2)}x avg` : ''}\n`
        + `${(row.dexes ?? []).join(', ')}\n${top}${errs}\n▸ can open here · ✗ failed the token screen`;
    }
    case 'scan_failed':
      return `board scan failed · ${row.reason}`;
    case 'board_checked':
      return `pool review · ${row.held ? `holding ${row.held}\nbest ${row.best}\n` : ''}${row.verdict}`;
    case 'MIGRATE_RECOMMENDED':
      return `BETTER POOL ELSEWHERE${row.gain_pct != null ? ` (+${row.gain_pct}%)` : ''}\n`
        + `holding ${row.held}\nbest ${row.best}\n${row.reason}\nto move by hand:\n${row.command}`;
    case 'MIGRATE':
      return `MOVING POOL${row.gain_pct != null ? ` (+${row.gain_pct}%)` : ''}\n`
        + `from ${row.held}\nto ${row.best}\n${row.pool}`;
    case 'REPOINTED':
      return `profile now on ${row.dex} ${row.pair}\n${row.pool}`;
    case 'status_unreadable':
      return `chain unreadable (${row.consecutive}) · ${row.reason}\n${row.action}`;
    case 'close_recovered':
    case 'open_recovered':
      return `RECOVERED · ${row.detail}`;
    case 'open_refused':
      return `OPEN REFUSED · ${row.reason}\nfailures ${row.failures}`;
    case 'close_failed':
    case 'open_failed':
      return `${event.replace('_', ' ').toUpperCase()} · ${row.reason}\nfailures ${row.failures}`;
    case 'no_position':   return `no position · ${row.detail}`;
    case 'idle':          return `idle · ${row.reason}`;
    case 'BREAKER':       return `BREAKER · ${row.reason}\n${row.action}`;
    case 'halted':        return `HALTED · ${row.reason}`;
    default:              return `${event} · ${JSON.stringify(row)}`;
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

console.error('telegram_bridge running');
for (;;) {
  try { await tail(); } catch (e) { console.error('tail:', e.message); }
  await new Promise(s => setTimeout(s, 5000));
}
