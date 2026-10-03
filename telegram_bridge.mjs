// Telegram bridge for the rebalancer.
//
// Tails every profile's feed (run/<profile>/events.jsonl) and the legacy
// ROOT/events.jsonl, and forwards every row to Telegram with its pool's label,
// so you see each poll, open, close, reband and breaker as it happens. It has
// NO trading, signing or execution path: it only reads files and sends text.
//
// One cursor per file (telegram_bridge_state.json): the byte offset sent so far
// and the file's inode, so a rotated or replaced file starts over and a moved
// one keeps its place. A line is consumed only once its newline is written.
//
// Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment.
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { equityLine, lpLine, sinceStartLine, splitMessage, emojiFor, healthLine, poolLabel, redact,
  portfolioText, walletName } from './book_format.mjs';

const SELF = fileURLToPath(import.meta.url);
const DIR = path.dirname(SELF);
const STATE_F = path.join(DIR, 'telegram_bridge_state.json');
// The profile whose feed was ROOT/events.jsonl before 020; the deploy moves it to run/<this>/.
export const LEGACY_PROFILE = 'sol-usdc';
export const LEGACY_FEED = 'events.jsonl';
export const MOVED_FEED = path.join('run', LEGACY_PROFILE, 'events.jsonl');

let EMOJI = {};
try { EMOJI = JSON.parse(fs.readFileSync(new URL('./event_emoji.json', import.meta.url), 'utf8')); } catch {}

const statOf = (f) => fs.statSync(f, { throwIfNoEntry: false });

// The feeds under `root`, relative to it: the legacy one when it exists, then
// every run/<profile>/events.jsonl, sorted. A profile's directory that appears
// while the bridge runs is found at the next tick.
export function feedFiles(root) {
  const out = [];
  if (statOf(path.join(root, LEGACY_FEED))?.isFile()) out.push(LEGACY_FEED);
  let dirs = [];
  try { dirs = fs.readdirSync(path.join(root, 'run'), { withFileTypes: true }); } catch {}
  for (const d of dirs.filter(d => d.isDirectory()).map(d => d.name).sort()) {
    const rel = path.join('run', d, 'events.jsonl');
    if (statOf(path.join(root, rel))?.isFile()) out.push(rel);
  }
  return out;
}

// The state file as {files: {relative path: {pos, ino}}}. The pre-020 state
// is one offset, {pos}, into ROOT/events.jsonl, which the deploy moves to
// run/sol-usdc/: the offset goes to whichever of the two holds at least that
// many bytes (both, when the move left a copy), with that file's inode. A
// shorter one is a new file and starts at 0. Nothing is sent twice, nothing
// is skipped.
export function migrateState(raw, root) {
  if (raw && raw.files && typeof raw.files === 'object') return { files: { ...raw.files } };
  const files = {};
  const pos = Number(raw?.pos);
  if (Number.isFinite(pos) && pos > 0) {
    for (const rel of [MOVED_FEED, LEGACY_FEED]) {
      const st = statOf(path.join(root, rel));
      if (st && st.size >= pos) files[rel] = { pos, ino: st.ino };
    }
  }
  return { files };
}

// The complete new lines of one feed from its cursor: rows parsed, the count
// of lines that are not JSON (dropped), and the new cursor. A file shorter
// than the cursor, or another inode, is a new file: read from 0. The bytes
// after the last newline are a line still being written: left for the next read.
export function readNew(file, cur) {
  const st = statOf(file);
  if (!st) return { rows: [], bad: 0, cur };
  let pos = cur && cur.ino === st.ino && st.size >= cur.pos ? cur.pos : 0;
  if (cur && cur.ino == null && st.size >= cur.pos) pos = cur.pos;      // a cursor with no inode yet
  const rows = [];
  let bad = 0;
  const fd = fs.openSync(file, 'r');
  const buf = Buffer.alloc(st.size - pos);
  try { fs.readSync(fd, buf, 0, buf.length, pos); } finally { fs.closeSync(fd); }
  const end = buf.lastIndexOf(0x0a);
  if (end >= 0) {
    for (const line of buf.subarray(0, end).toString('utf8').split('\n')) {
      if (!line.trim()) continue;
      try { rows.push(JSON.parse(line)); } catch { bad += 1; }
    }
    pos += end + 1;
  }
  return { rows, bad, cur: { pos, ino: st.ino } };
}

// One tick over every feed. A feed with no cursor takes the cursor of a feed
// that vanished with the same inode (it was moved), else starts at 0. Cursors
// of vanished feeds are dropped. The state is saved before sending, as it
// always was: a crash mid-send loses rows rather than repeating them.
export async function tailAll(state, root, sendRow, save = () => {}) {
  const now = feedFiles(root);
  const gone = Object.keys(state.files).filter(rel => !now.includes(rel));
  for (const rel of now) {
    if (state.files[rel]) continue;
    const ino = statOf(path.join(root, rel))?.ino;
    const was = gone.find(g => state.files[g].ino === ino);
    if (was) state.files[rel] = state.files[was];
  }
  for (const g of gone) delete state.files[g];
  for (const rel of now) {
    const { rows, bad, cur } = readNew(path.join(root, rel), state.files[rel]);
    state.files[rel] = cur;
    save(state);
    if (bad) console.error(`${rel}: ${bad} line(s) not JSON, dropped`);
    for (const row of rows) {
      try { await sendRow(row); } catch (e) { console.error('send:', redact(e.message)); }
    }
  }
}

// The message of one row: the pool's label, the event's emoji, the text,
// with every secret-shaped string masked.
export function message(row) {
  const label = row && row.event !== 'PORTFOLIO' ? poolLabel(row) : '';
  return redact(`${label ? label + ' ' : ''}${emojiFor(row.event, EMOJI)} ${render(row)}`);
}

// The pool's base token: the row's token_a, else the first half of its pair.
const baseSymbol = (row) => row.token_a ?? (typeof row.pair === 'string' && row.pair.includes('/') ? row.pair.split('/')[0] : 'SOL');

export function render(row) {
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
      `━━ 💰 FEES ━━`,
      `today       ${tok(r.fees_today_a)} ${A}   ${tok(r.fees_today_b, 4)} ${B}   $${n(r.fees_today_usd, 4)}`,
      `realised    ${tok(r.fees_realised_a)} ${A}   ${tok(r.fees_realised_b, 4)} ${B}   $${n(r.fees_realised_usd, 4)}`,
      `unrealised  ${tok(r.fees_unrealised_a)} ${A}   ${tok(r.fees_unrealised_b, 4)} ${B}   $${n(r.fees_unrealised_usd, 4)}`,
      `TOTAL       ${tok(r.fees_total_a)} ${A}   ${tok(r.fees_total_b, 4)} ${B}   $${n(r.fees_total_usd, 4)}`,
      ...(r.split ? [
        `profit      $${n(r.split.paid_usd, 4)} paid to the profit wallet · $${n(r.split.paid_today_usd, 4)} today`,
        `reinvested  $${n(r.split.reinvested_usd, 4)} back in the LP · gas $${n(r.split.gas_usd, 4)}`] : []),
      ...(Array.isArray(r.by_pool) && r.by_pool.length > 1 ? [
        `━━ 🏊 POOLS ━━`,
        ...r.by_pool.slice(0, 5).map(p =>
          `${p.open_now ? '▸' : '·'} ${p.dex} ${p.pair_label} ${n(p.days, 2)}d fees $${n(p.fees_usd, 4)}`
          + `${p.apr_pct != null ? ` APR ${n(p.apr_pct, 0)}%` : ''}`
          + `${p.pnl_usd != null ? ` P&L ${sign(p.pnl_usd)}` : ''}`
          + `${p.in_range_pct != null ? ` ${n(p.in_range_pct, 0)}% in` : ''}`),
        `all pools   fees $${n(r.fees_total_usd, 4)}   P&L ${r.pnl_all_pools_usd != null ? sign(r.pnl_all_pools_usd) : '—'}`,
      ] : []),
      `━━ 📒 BOOK ━━`,
      equityLine(r),
      ...[lpLine(r), sinceStartLine(r), healthLine(r.health)].filter(Boolean),
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
      ...band(r),
      ...(r.regime ? regimeBlock(r.regime) : calmBlock(r.calm)),
      ...(Array.isArray(r.venues) && r.venues.length ? [`venues      ±1% on chain: ` + r.venues.map(v =>
        `${v.held ? '▸' : ''}${v.dex.replace('-clmm', '').replace('-v3-solana', '')} ${n(v.total_pct_day, 2)}%/d`
        + `${v.reward_pct_day > 0.001 ? ` (+${n(v.reward_pct_day, 2)} rwd)` : ''} ${n(v.hours, 1)}h`).join(' · ')] : []),
      `in range    ${n(r.in_range_pct, 0)}%   over ${n(r.tracked_days, 2)}d`,
      `activity    ${plural(r.positions_opened, 'position')} · ${plural(r.rebands, 'reband')} · `
        + `${plural(r.harvests, 'harvest')}${r.failures ? ` · ${plural(r.failures, 'failure')}` : ''}`,
    ].join('\n');
  };

  // The survival block. The full forecast rides on the event when the loop
  // made one this poll; otherwise the figures recorded at the last poll.
  // Calm mode: five-minute volatility against the cut, and the tight band's
  // own touch risk. Present only when calm mode is on.
  // Regime mode: the width the market allows, and the survival behind it.
  function regimeBlock(g) {
    if (!g) return [];
    const pct = (x) => (x == null ? '—' : `${Math.round(Number(x) * 100)}%`);
    const probs = (Array.isArray(g.probs) ? g.probs : Object.entries(g.probs ?? {}))
      .map(([w, p]) => `${w}% ${pct(p)}`).join(' · ');
    return [`━━ 📈 REGIME ━━`,
      `mode        ${g.mode}${g.stale ? (g.unstale_in_s ? ` (tape back: widest width for ${Math.ceil(g.unstale_in_s / 60)} more min)` : ' (tape stale: widest width, no narrowing)') : ''} · holding ±${n(g.held_pct, 2)}% · market says ±${n(g.choice_pct, 2)}%`,
      `data        ${dataSource(g.data)}`,
      `vol 5m      σ ${n(g.sigma_5m_pct, 4)}% · velocity ${sign(g.velocity)}/h · instability ${n(g.instability, 3)}`,
      `P(touch ≤${g.horizon_minutes}m)  ${probs}`,
      `held band   P(touch) ${pct(g.p_held)} · rule: narrowest width ≤ ${pct(g.threshold)}, move at ${g.steps ?? 2} steps, exits re-centre`,
      ...(g.guard ? [`fee guard   ${g.guard.mode}${g.guard.source ? ` (${g.guard.source} fees)` : ''} · fees/variance ${g.guard.ratio == null ? '— (no ratio: the touch rule stands)' : n(g.guard.ratio, 2)}`
        + ` · concentrate at ≥ ${n(g.guard.threshold, 2)} over ${Math.round(g.guard.window_bars * 5 / 60 * 10) / 10}h`
        + (g.guard.acting ? ` · ACTING (touch rule said ±${n(g.guard.touch_choice_pct, 2)}%)` : '')] : []),
      ...(g.p_exit ? [`P(exit)     6h ${pct(g.p_exit[6] ?? g.p_exit['6'])}   24h ${pct(g.p_exit[24] ?? g.p_exit['24'])}`
        + `   72h ${pct(g.p_exit[72] ?? g.p_exit['72'])}   7d ${pct(g.p_exit[168] ?? g.p_exit['168'])}`
        + ` · median life ${g.median_life_hours != null ? n(g.median_life_hours, 0) + 'h' : '> 7d'} (hourly tape)`] : []),
      ...(g.liquidity ? [`liquidity   ${g.liquidity.inflow != null ? n(g.liquidity.inflow, 2) + 'x its 24h median' : 'building history'}`
        + ` · TVL ${g.liquidity.tvl_change_24h != null ? sign(g.liquidity.tvl_change_24h * 100) + '%' : '—'} 24h`
        + ` → risk ×${n(g.liquidity.factor, 2)} (${pct(g.threshold_base)} → ${pct(g.threshold)})`
        + ` · volume ${g.liquidity.volume_x != null ? n(g.liquidity.volume_x, 2) + 'x' : '—'} (info)`] : []),
      ...(g.calibration && g.calibration.chosen && g.calibration.chosen.n
        ? [`check 7d    chosen width said ${pct(g.calibration.chosen.said)} saw ${pct(g.calibration.chosen.saw)}`
          + ` (n ${g.calibration.chosen.n})` + (g.calibration.widths && g.calibration.widths['1'] && g.calibration.widths['1'].n
            ? ` · ±1% said ${pct(g.calibration.widths['1'].said)} saw ${pct(g.calibration.widths['1'].saw)}` : '')]
        : [`check 7d    building: forecasts resolve 2h after they are made`]),
      `guard       ${g.moves_24h ?? '—'}/${g.guard ?? '—'} moves in 24h`];
  }

  // Where the five-minute bars came from: GeckoTerminal, a surrogate
  // (Binance klines) in the slots GeckoTerminal lacks, or none (stale). A
  // quiet pool's slots without a swap are flat bars (quiet_1h of the hour).
  function dataSource(d) {
    if (!d || !d.source) return 'Gecko';
    if (d.source === 'none') return 'none · GeckoTerminal and the surrogate both lack recent bars';
    const quiet = d.quiet_1h ? ` · quiet pool: ${d.quiet_1h} flat bars of the last hour (no swap)` : '';
    if (d.source === 'Gecko') return 'Gecko' + (d.filled_24h ? ` · ${d.surrogate} still fills ${d.filled_24h} older bars of 24h` : '') + quiet;
    return `${d.source} · ${d.surrogate} fills ${d.filled_1h}/${d.bars_1h} bars of the last hour (Gecko lacks them)` + quiet;
  }

  function calmBlock(c) {
    if (!c) return [];
    const pct = (x) => (x == null ? '—' : `${Math.round(Number(x) * 100)}%`);
    return [`━━ ❄️ CALM ━━`,
      `sigma 5m    ${n(c.sigma_5m_pct, 4)}% · cut ${n(c.cut_pct, 4)}% (${n(c.ratio, 2)}x) · leave above ${n(c.exit_cut_pct, 4)}%`,
      `state       ${c.calm ? 'CALM' : 'normal'} · ${c.tight_held ? `holding ±${n(c.band_pct, 1)}%` : 'normal band'}`
        + ` · calm ${pct(c.calm_share_24h)} of last 24h`,
      `touch ≤${c.horizon_minutes}m  ${c.tight_held ? `held ${pct(c.p_touch)} · ` : ''}fresh ±${n(c.band_pct, 1)}% ${pct(c.p_touch_fresh)}`
        + ` · act at ${pct(c.threshold)}`,
      `budget      ${c.moves_24h ?? '—'} calm moves in 24h · ${c.budget_left ?? '—'} left`];
  }

  function band(r) {
    const f = r.forecast;
    const pct = (x) => (x == null ? '—' : `${Math.round(Number(x) * 100)}%`);
    if (f) {
      const lines = [`━━ 🧭 BAND ━━`];
      if (f.inside) {
        lines.push(`price       ${n(f.to_lower_pct, 1)}% above the floor · ${n(f.to_upper_pct, 1)}% below the ceiling`
          + `${f.hours_alive != null ? ` · alive ${n(f.hours_alive, 0)}h` : ''}`);
        lines.push(`P(exit)     6h ${pct(f.p_exit_6h_regime ?? f.p_exit_6h)}   24h ${pct(f.p_exit_24h_regime ?? f.p_exit_24h)}`
          + `   72h ${pct(f.p_exit_72h_regime ?? f.p_exit_72h)}   7d ${pct(f.p_exit_168h_regime ?? f.p_exit_168h)}`);
        const med = f.median_life_hours_regime ?? f.median_life_hours;
        lines.push(`life        median ${med != null ? `${n(med, 0)}h` : '> 7d'} · vol ${n(f.vol_regime_x, 2)}x normal`
          + ` (${f.origins_regime ?? f.origins} origins)`);
      } else {
        lines.push(`OUT         ${n(f.beyond_half_widths, 2)} half-widths beyond the edge`);
      }
      if (f.il_now_pct != null) {
        lines.push(`if closed   locks ${n(f.il_now_pct, 2)}% vs holding · price ${sign(f.since_open_pct)}% since open`);
      }
      lines.push(f.suspended
        ? `rule        hourly rule off · the ${r.regime ? 'REGIME' : 'CALM'} block below decides (hourly figures above are context only)`
        : `rule        act at P(exit ≤${f.horizon_hours}h) ≥ ${pct(f.threshold)} · now ${pct(f.p_exit_horizon)}`
          + ` → ${f.act ? 'RE-CENTRE' : 'hold'}`);
      return lines;
    }
    const b = r.band;
    if (!b || b.p_exit_24h == null) return [];
    return [`━━ 🧭 BAND ━━`,
      `${b.in_range ? 'in range' : 'OUT'}    position ${sign(b.position)} · alive ${n(b.hours_alive, 0)}h`
      + ` · P(exit) 6h ${pct(b.p_exit_6h)}  24h ${pct(b.p_exit_24h)}  72h ${pct(b.p_exit_72h)}`];
  }

  switch (event) {
    case 'startup':
      return `REBALANCER · online\n${row.dex ?? 'orca'} ${row.pair} · capital $${n(row.capital_usd, 0)}\n`
        + `poll ${row.poll_seconds}s · reopt ${row.reopt_every_hours}h @ +${(row.reopt_min_gain * 100).toFixed(0)}%\n`
        + (row.proactive_threshold ? `re-centre at P(exit ≤${row.proactive_horizon_hours}h) ≥ ${Math.round(row.proactive_threshold * 100)}%`
          + ` · dividend every ${row.harvest_every_hours}h from $${n(row.min_harvest_usd, 2)}\n` : '')
        + `board ${(row.scan_dexes ?? []).join(', ')} every ${row.scan_every_hours}h · `
        + `move @ +${((row.migrate_min_gain ?? 0) * 100).toFixed(0)}% · can open on ${(row.execute_dexes ?? []).join(', ')}\n`
        + book(row);
    case 'in_band':
      return `IN RANGE · ${row.position_dex ?? row.dex ?? ''} ${row.position_pair ?? row.pair ?? ''} · ${n(row.price, 4)}\n`
        + `band ${n(row.lower, 4)} — ${n(row.upper, 4)}\n`
        + book(row);
    case 'OUT_OF_BAND':
      return `OUT OF RANGE · went ${row.side}\n`
        + `price ${n(row.price, 4)} · band ${n(row.lower, 4)} — ${n(row.upper, 4)}\n${row.action}`
        + (row.forecast ? '\n' + band(row).join('\n') : '');
    case 'PROACTIVE':
      return `RE-CENTRING · P(exit within ${row.horizon_hours}h) ${Math.round(row.p_exit * 100)}%`
        + ` ≥ ${Math.round(row.threshold * 100)}%\n`
        + `price ${n(row.price, 4)} · band ${n(row.lower, 4)} — ${n(row.upper, 4)}\n${row.action}\n` + book(row);
    case 'recentre_deferred':
      return `re-centre deferred · P(exit within ${row.horizon_hours}h) ${Math.round(row.p_exit * 100)}%\n${row.reason}`;
    case 'FAILOVER':
      return `FAILOVER · ${row.venue} failed ${row.fails}x (${row.last_error ?? '—'}) → ${row.to} ${n(row.total_pct_day, 2)}%/d on chain\n${row.pool ?? ''}`;
    case 'failover_none':
      return `no failover · ${row.venue} failed ${row.fails ?? '—'}x · ${row.reason}`;
    case 'failover_failed':
      return `failover failed · ${row.reason}`;
    case 'TAPE_SOURCE':
      return `DATA SOURCE · ${row.detail}`;
    case 'REGIME_WIDEN':
      return `HEATING · widening ±${n(row.regime?.held_pct, 2)}% → ±${n(row.regime?.choice_pct, 2)}% (${row.regime?.mode})\n`
        + `σ ${n(row.regime?.sigma_5m_pct, 4)}% · velocity ${sign(row.regime?.velocity)}/h\n` + book(row);
    case 'REGIME_NARROW':
      return `COOLING · narrowing ±${n(row.regime?.held_pct, 2)}% → ±${n(row.regime?.choice_pct, 2)}% (${row.regime?.mode})\n`
        + `σ ${n(row.regime?.sigma_5m_pct, 4)}% · velocity ${sign(row.regime?.velocity)}/h\n` + book(row);
    case 'CALM_NARROW':
      return `CALM · narrowing to ±${n(row.calm?.band_pct, 1)}%\nsigma ${n(row.calm?.sigma_5m_pct, 4)}% under the cut ${n(row.calm?.cut_pct, 4)}%\n` + book(row);
    case 'CALM_RECENTRE':
      return `CALM · re-centring the tight band · P(touch ≤${row.calm?.horizon_minutes}m) ${Math.round((row.calm?.p_touch ?? 0) * 100)}%\n` + book(row);
    case 'CALM_WIDEN':
      return `CALM OVER · widening to the ladder band\nsigma ${n(row.calm?.sigma_5m_pct, 4)}% vs leave-above ${n(row.calm?.exit_cut_pct, 4)}%\n` + book(row);
    case 'SWAP':
      return `SWAP · $${n(row.usd, 2)} to 50/50 before the open · impact ${n(row.price_impact_pct, 3)}\n${row.signature ?? ''}`;
    case 'swap_fallback':
      return `swap fallback · ${row.reason}`;
    case 'swap_skipped':
      return `swap skipped · ${row.reason}`;
    case 'swap_failed':
      return `SWAP FAILED · ${row.reason}\nfailures ${row.failures} · nothing opened`;
    case 'PAYOUT': {
      const sent = (row.sent ?? []).map(x => `${x.amount} ${x.symbol} ($${n(x.usd, 4)}) → profit wallet\n${x.signature}`).join('\n');
      return `PAYOUT${row.gas_low ? ' · gas low, SOL refilled gas, payout reinvested' : ''}\n`
        + (sent || 'nothing sent this harvest') + `\n`
        + `split  paid $${n(row.split?.paid, 4)} · reinvested $${n(row.split?.reinvested, 4)} · gas $${n(row.split?.gas, 4)}`;
    }
    case 'REWARD_PAYOUT':
      return `REWARDS${row.gas_low ? ' · gas low, swapped to SOL for gas' : ''}\n`
        + (row.rewards ?? []).map(x => `$${n(x.usd, 4)} → ${x.to}${x.signature ? `\n${x.signature}` : ''}`).join('\n');
    case 'reward_swap_failed':
      return `reward swap failed · ${row.reason}\n${row.amount} of ${row.mint} stays in the LP wallet`;
    case 'payout_uncertain':
      return `PAYOUT UNCONFIRMED · ${row.amount} ${row.symbol} may have landed; it will not be sent again\n${row.signature ?? ''}\n${row.reason ?? ''}`;
    case 'payout_refused':
      return `PAYOUT REFUSED · ${row.reason}\nowed ${row.owed} ${row.symbol}; check profit_wallet and LPBOT_PROFIT_WALLET_PIN`;
    case 'reward_held':
      return `reward held for review · ${row.reason}`;
    case 'payout_failed':
      return `PAYOUT FAILED · ${row.reason}${row.owed != null ? `\nowed ${row.owed} ${row.symbol}, retried at the next harvest` : ''}`;
    case 'payout_skipped':
      return `payout skipped · ${row.reason}`;
    case 'DIVIDEND':
      return `DIVIDEND · $${n(row.collected_usd, 4)} harvested to the wallet\n${row.signature ?? ''}\n` + book(row);
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
    case 'deploy_idle_deferred':
      return `idle $${n(row.idle_usd)} waits · ${row.reason}`;
    case 'DEPLOY_IDLE':
      return `DEPLOYING IDLE $${n(row.idle_usd)} · re-centre at ${n(row.price, 4)} to put it in the LP\n` + book(row);
    case 'SWEEP':
      return `SWEPT $${n(row.total_usd)} of other tokens into the pool · `
        + (row.swept || []).map(x => `${x.symbol ?? String(x.mint).slice(0, 6)} $${n(x.usd)}`).join(' · ');
    case 'sweep_failed':
      return `sweep failed · ${row.reason}${row.usd != null ? ` ($${n(row.usd)})` : ''}`;
    case 'JANITOR':
      return `JANITOR · closed ${(row.accounts || []).length} empty token accounts, ${n(row.reclaim_sol, 6)} SOL rent back to the wallet`;
    case 'AUDIT':
      return `AUDIT ${row.check} · ${String(row.status).toUpperCase()}${row.was ? ` (was ${row.was})` : ''}\n`
        + JSON.stringify(row.detail ?? {}).slice(0, 600);
    case 'DAILY':
      return `DAILY ${row.day} · ${row.recentres} re-centres${row.idle_redeploys ? ` (${row.idle_redeploys} idle redeploys)` : ''}`
        + ` · fees earned $${n(row.fees_earned_usd ?? row.fees_usd)}${row.fees_earned_usd != null ? ` (harvested $${n(row.fees_usd)})` : ''}`
        // a day across pairs (sol-swing) has no hold benchmark and no one price
        + ` · vs 50/50 hold ${row.vs_hold_usd == null ? '—' : sign(row.vs_hold_usd)}`
        // deposits, withdrawals and rent moved to other profiles: capital, not P&L
        + (Number(row.net_flows_usd) ? ` · capital moved ${sign(row.net_flows_usd)}` : '')
        + (row.price_open == null && row.price_close == null ? ''
          : ` · ${baseSymbol(row)} ${n(row.price_open)} → ${n(row.price_close)}`);
    case 'HARVEST':
      return `HARVESTED $${n(row.collected_usd, 4)}\n${row.signature ?? ''}`;
    case 'harvest_skipped':
      return `harvest skipped · ${row.reason}`;
    case 'CLOSE':
      return `CLOSED · ${row.reason}\n${row.signature ?? ''}\n` + book(row);
    case 'OPEN':
      return `OPENED · ${row.dex ? row.dex + ' ' : ''}${row.pair} ${row.opened_band ?? (typeof row.band === 'string' ? row.band : '')}\n`
        + `range ${n(row.lower, 4)} — ${n(row.upper, 4)}\n`
        + `deposited $${n(row.deposit_usd)} · caps ${row.cap_a ?? '—'} · ${row.cap_b ?? '—'}\n`
        + (row.expected_net_day_pct != null && row.modelled_rebalances_per_day != null
          ? `modelled ${n(row.expected_net_day_pct, 3)}%/day at ${n(row.modelled_rebalances_per_day, 2)} rebalances/day\n` : '')
        + `${row.reason}\n${row.signature ?? ''}\n` + book(row);
    case 'REBAND':
      return `REBANDED · ${row.old_band} → ${row.new_band}\n`
        + `${n(row.old_net_day, 3)}%/day → ${n(row.new_net_day, 3)}%/day (+${row.improvement_pct}%)`;
    case 'reopt_checked':
      return `band review · holding ${row.held}, best ${row.best}\n${row.verdict}`;
    case 'SCAN': {
      const top = (row.top ?? []).map(t =>
        `${t.can_open ? '▸' : '·'} ${t.dex} ${t.pair} ${t.band} ${n(t.net_day_pct, 3)}%/d`
        + `${t.reward_day_pct ? ` +${n(t.reward_day_pct, 3)} rewards` : ''}`
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
    case 'PORTFOLIO':
      return portfolioText(row);
    case 'dormant':
      return `DORMANT · ${row.reason ?? 'no position and too little to deploy'}`
        + (row.deployable_usd != null ? `\ndeployable $${n(row.deployable_usd)} · opens from $${n(row.min_deploy_usd)}` : '')
        + (row.poll_seconds != null ? ` · light poll every ${row.poll_seconds}s` : '');
    case 'deposit_seen':
      return `DEPOSIT SEEN · ${row.amount != null ? `${tok(row.amount)} ${row.symbol ?? ''} ` : ''}`
        + `${row.usd != null ? `($${n(row.usd)}) ` : ''}${row.reason ?? 'waking: swap to 50/50, then open'}`;
    case 'claim_overdraw':
      return `CLAIM OVERDRAW · ${row.symbol ?? row.mint ?? 'token'} · claim ${row.claim ?? '—'} · change ${row.delta ?? '—'}`
        + `${row.overdraw != null ? ` · over by ${row.overdraw}` : ''} → floored at 0`
        + `${row.command ? ` (${row.command})` : ''}${row.reason ? `\n${row.reason}` : ''}`;
    case 'wallet_lock_timeout':
      return `WALLET LOCK TIMEOUT · ${row.wallet_id != null ? walletName(row) : 'wallet'} busy${row.waited_s != null ? ` for ${n(row.waited_s, 0)}s` : ''}`
        + ` · ${row.action ?? 'nothing sent; retried at the next poll'}`;
    // the pool's label is the prefix: the row's own copy of it is not repeated
    default: {
      const { profile, wallet_id, wallet_tag, chain, pair, ...rest } = row;
      return `${event} · ${JSON.stringify(rest)}`;
    }
  }
}

async function main() {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  const chatId = process.env.TELEGRAM_CHAT_ID;
  if (!token || !chatId) throw new Error('needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID');

  async function send(text) {
    // Telegram refuses a message over 4,096 characters (2026-09-28: a
    // DEPLOY_IDLE book was lost that way). Long messages go out in parts.
    for (const part of splitMessage(text)) {
      const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ chat_id: chatId, text: part }) });
      const j = await r.json();
      if (!j.ok) throw new Error(`telegram: ${JSON.stringify(j)}`);
    }
  }

  let raw = null;
  try { raw = JSON.parse(fs.readFileSync(STATE_F, 'utf8')); } catch {}
  const state = migrateState(raw, DIR);
  const save = (st) => fs.writeFileSync(STATE_F, JSON.stringify(st));
  save(state);
  console.error('telegram_bridge running');
  for (;;) {
    try { await tailAll(state, DIR, (row) => send(message(row)), save); } catch (e) { console.error('tail:', redact(e.message)); }
    await new Promise(s => setTimeout(s, 5000));
  }
}

// Run as a program (node telegram_bridge.mjs, through a symlink too), not when a test imports it.
const isMain = () => { try { return fs.realpathSync(process.argv[1]) === fs.realpathSync(SELF); } catch { return false; } };
if (process.argv[1] && isMain()) await main();
