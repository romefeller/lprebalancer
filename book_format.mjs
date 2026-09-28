// Lines of the Telegram book that carry money figures, as pure functions so
// they are tested (tests/test_book_format.mjs). telegram_bridge.mjs imports them.
const n = (x, d = 2) => (x === undefined || x === null || Number.isNaN(Number(x)) ? '—' : Number(x).toFixed(d));
const sign = (x) => (x === undefined || x === null ? '—' : (Number(x) >= 0 ? '+' : '') + n(x));

// equity, with P&L against the capital the bot started with (capital_flows),
// not the first snapshot; the old figure only when there is no baseline yet
export function equityLine(r) {
  const sol = r.last_price != null ? ` · SOL $${n(r.last_price)}` : '';
  const s = r.since_start;
  if (s && s.profit_usd != null) {
    return `equity      $${n(r.equity_usd)}${sol}   P&L ${sign(s.profit_usd)} since start · includes pending fees`;
  }
  return `equity      $${n(r.equity_usd)}${sol}   P&L ${sign(r.pnl_usd)} · includes pending fees`;
}

// the line after equity: what is in the LP position, its share, the wallet
export function lpLine(r) {
  if (r.lp_usd == null) return null;
  const share = r.deployed_pct != null ? ` (${n(r.deployed_pct, 1)}%)` : '';
  const wallet = r.wallet_usd != null ? ` · wallet $${n(r.wallet_usd)}` : '';
  return `in LP       $${n(r.lp_usd)}${share}${wallet}`;
}

// since the bot started: the capital then, and the value now against holding it
export function sinceStartLine(r) {
  const s = r.since_start;
  if (!s || s.start_usd == null) return null;
  return `start       $${n(s.start_usd)} (${n(s.start_sol, 4)} SOL, ${String(s.since).slice(0, 10)}) · now $${n(s.value_usd)}`
    + ` · vs holding it ${sign(s.vs_hold_start_assets_usd)}`;
}
