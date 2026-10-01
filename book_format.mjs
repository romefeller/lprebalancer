// Lines of the Telegram book that carry money figures, as pure functions so
// they are tested (tests/test_book_format.mjs). telegram_bridge.mjs imports them.
const n = (x, d = 2) => (x === undefined || x === null || Number.isNaN(Number(x)) ? '—' : Number(x).toFixed(d));
const sign = (x) => (x === undefined || x === null ? '—' : (Number(x) >= 0 ? '+' : '') + n(x));

// equity, with P&L against the capital the bot started with (capital_flows),
// not the first snapshot; the old figure only when there is no baseline yet
export function equityLine(r) {
  const sol = r.last_price != null ? ` · ${r.token_a ?? 'SOL'} $${n(r.last_price)}` : '';
  const s = r.since_start;
  if (s && s.profit_usd != null) {
    return `equity      $${n(r.equity_usd)}${sol}   P&L ${sign(s.profit_usd)} since start · includes pending fees`;
  }
  return `equity      $${n(r.equity_usd)}${sol}   P&L ${sign(r.pnl_usd)} · includes pending fees`;
}

// A share is shown only when it is in [0, 100] and, with the equity known,
// agrees with lp_usd / equity (within 0.2 points): a book whose figures
// disagree shows no share, not a wrong one (2026-09-30: "$0.00 (0.0%)").
export function shareAgrees(r) {
  const p = Number(r.deployed_pct);
  if (r.deployed_pct == null || !Number.isFinite(p) || p < 0 || p > 100) return false;
  const eq = Number(r.equity_usd), lp = Number(r.lp_usd);
  if (r.equity_usd == null || !Number.isFinite(eq) || eq <= 0) return true;
  return Number.isFinite(lp) && Math.abs(lp / eq * 100 - p) <= 0.2;
}

// the line after equity: what is in the LP position, its share, the wallet
export function lpLine(r) {
  if (r.lp_usd == null) return null;
  const share = shareAgrees(r) ? ` (${n(r.deployed_pct, 1)}%)` : '';
  const wallet = r.wallet_usd != null ? ` · wallet $${n(r.wallet_usd)}` : '';
  return `in LP       $${n(r.lp_usd)}${share}${wallet}`;
}

// since the bot started: the capital then, and the value now against holding it
export function sinceStartLine(r) {
  const s = r.since_start;
  if (!s || s.start_usd == null) return null;
  // a sum over pools of different tokens has no token amount
  const held = s.start_sol != null ? `${n(s.start_sol, 4)} ${r.token_a ?? 'SOL'}, ` : '';
  return `start       $${n(s.start_usd)} (${held}${String(s.since).slice(0, 10)}) · now $${n(s.value_usd)}`
    + ` · vs holding it ${sign(s.vs_hold_start_assets_usd)}`;
}

// Telegram's limit is 4,096 characters: split on line breaks, never inside a
// line unless a single line is longer than the limit.
export function splitMessage(text, limit = 4000) {
  if (text.length <= limit) return [text];
  const parts = [];
  let cur = '';
  for (const line of text.split('\n')) {
    const pieces = line.length > limit ? line.match(new RegExp(`.{1,${limit}}`, 'gs')) : [line];
    for (const piece of pieces) {
      if (cur && cur.length + 1 + piece.length > limit) { parts.push(cur); cur = ''; }
      cur = cur ? `${cur}\n${piece}` : piece;
    }
  }
  if (cur) parts.push(cur);
  return parts;
}

// One emoji per event, shared with the loop's log lines (event_emoji.json;
// rebalancer.emoji_for has the same rule): grep the emoji to find the event.
export function emojiFor(event, map = {}) {
  const e = String(event);
  if (Object.prototype.hasOwnProperty.call(map, e) && !e.startsWith('_')) return map[e];
  if (/fail|unreadable|refused|rejected|error/i.test(e)) return '❌';
  if (/defer|skip|wait|held/i.test(e)) return '⏳';
  return '▫️';
}

// The breakers (health.py): all green in one word, else each one that is not.
export function healthLine(h) {
  if (!Array.isArray(h)) return null;
  const bad = h.filter(x => x && x.state && x.state !== 'closed');
  if (!bad.length) return `🩺 health   🟢 all systems${h.length ? ` (${h.length} watched)` : ''}`;
  return `🩺 health   ` + bad.map(x => `${x.emoji ?? (x.state === 'tripped' ? '🔴' : '🟡')} ${x.key} ${x.fails}x`
    + (x.wait_s > 0 ? ` retry ${Math.ceil(x.wait_s / 60)}m` : ' probing')).join(' · ');
}

// The prefix of a message: the pool it is about, from the row's pair (or its
// profile), e.g. "[MU/USDC]". Empty for a row that names no pool (pre-020).
export function poolLabel(row) {
  const raw = row?.pair || row?.profile;      // an empty pair names no pool
  if (raw == null) return '';
  const clean = String(raw).replace(/[^\w/.\- ]/g, '').slice(0, 32).trim();
  return clean ? `[${clean}]` : '';
}

// Secret-shaped strings never reach Telegram: an api-key in a URL (the keyed
// RPC; rebalancer.redact masks it at the source too), a Telegram bot token, a
// key file's 64-byte array, a hex key next to the word that names it.
const SECRETS = [
  [/(api[-_]?key=)[^&\s"'<>]+/gi, '$1***'],
  [/\d{6,12}:[A-Za-z0-9_-]{30,}/g, '***'],
  [/\[\s*(?:\d{1,3}\s*,\s*){63}\d{1,3}\s*\]/g, '[***]'],
  [/((?:private[_ -]?key|secret[_ -]?key|secret|mnemonic|seed)\\?["']?\s*[:=]\s*\\?["']?)(?:0x)?[0-9a-fA-F]{64}/gi, '$1***'],
];

export function redact(text) {
  return SECRETS.reduce((t, [re, to]) => t.replace(re, to), String(text));
}

// The daily PORTFOLIO (stats.portfolio()): every active pool by wallet, the
// wallet subtotals, the TOTAL in dollars (token amounts never add across
// pools), and the dormant and disabled profiles by name.
export function portfolioText(p) {
  const d = (x, k = 2) => (x == null ? '—' : `$${n(x, k)}`);
  const pct = (x) => (x == null ? '—' : `${n(x, 0)}%`);
  const pools = Array.isArray(p.pools) ? p.pools : [];
  const wallets = Array.isArray(p.wallets) ? p.wallets : [];
  const byName = Object.fromEntries(pools.map(r => [r.profile, r]));
  const sum = (t) => [
    `equity ${d(t.equity_usd)} · in LP ${d(t.lp_usd)} · idle ${d(t.idle_usd)}`,
    `fees ${d(t.fees_total_usd, 4)} · today ${d(t.fees_today_usd, 4)} · 24h ${d(t.fees_per_day_24h_usd, 4)}/d · APR 24h ${pct(t.apr_24h_pct)}`,
    `paid ${d(t.paid_usd, 4)} · reinvested ${d(t.reinvested_usd, 4)} · gas ${d(t.gas_usd, 4)} · P&L ${sign(t.profit_usd)}`];
  const lines = [`PORTFOLIO · ${pools.length} active pool${pools.length === 1 ? '' : 's'} · `
    + `${wallets.length} wallet${wallets.length === 1 ? '' : 's'}`];
  for (const w of wallets) {
    lines.push(`▸ ${w.wallet_id} (${w.chain || '—'})`);
    for (const name of w.pools || []) {
      const r = byName[name] || {};
      lines.push(`  ${r.pair || name}  equity ${d(r.equity_usd)} · fees ${d(r.fees_total_usd, 4)}`
        + ` (24h ${d(r.fees_per_day_24h_usd, 4)}/d) · APR 24h ${pct(r.apr_24h_pct)} · P&L ${sign(r.profit_usd)}`
        + ` · in range ${pct(r.in_range_pct)}`);
    }
    if (wallets.length > 1 && w.subtotal) lines.push(`  subtotal  ${sum(w.subtotal).join(' · ')}`);
  }
  if (p.total) lines.push(`TOTAL`, ...sum(p.total).map(x => `  ${x}`));
  const names = (xs) => (Array.isArray(xs) && xs.length ? `${xs.length} (${xs.join(', ')})` : '0');
  lines.push(`dormant ${names(p.dormant)} · disabled ${names(p.disabled)}`
    + (Array.isArray(p.disabled_holding) && p.disabled_holding.length
      ? ` · DISABLED BUT HOLDING A POSITION: ${p.disabled_holding.join(', ')}` : ''));
  return lines.join('\n');
}
