// One request slot at a time for Jupiter's free API, across every process.
// The protocol is jupgate.py's (read it): exclusive lock file, last reserved
// slot in the gate file, next = max(now, last + spacing), sleep until next.
// Python and Node share the same two files, so their requests never burst
// together. Never throws; if the lock cannot be taken, the caller goes on.

import fs from 'node:fs';

export const GATE = process.env.LPBOT_JUP_GATE ?? '/tmp/lp_bot_jupiter.gate';
export const SPACING_MS = Number(process.env.LPBOT_JUP_SPACING_MS ?? 1100);
export const STALE_MS = 5000;
export const WAIT_MS = 5000;

const pause = ms => new Promise(r => setTimeout(r, ms));

async function take(lock, now, sleep) {
  const t0 = now();
  for (;;) {
    try {
      fs.closeSync(fs.openSync(lock, 'wx', 0o600));
      return true;
    } catch (e) {
      if (e?.code !== 'EEXIST') return false;          // no writable directory: no gate
      try {
        if (now() - fs.statSync(lock).mtimeMs > STALE_MS) { fs.unlinkSync(lock); continue; }   // a crashed holder
      } catch { continue; }                            // removed meanwhile: try again
      if (now() - t0 > WAIT_MS) return false;
      await sleep(20);
    }
  }
}

// Reserves the next slot; returns its time in epoch milliseconds.
export async function reserve({ gate = GATE, spacingMs = SPACING_MS, now = Date.now, sleep = pause } = {}) {
  const lock = `${gate}.lock`;
  if (!(await take(lock, now, sleep))) return now();
  try {
    let last = 0;
    try { last = Number(fs.readFileSync(gate, 'utf8').trim()) * 1000 || 0; } catch {}
    const t = now();
    const next = last <= t + 3_600_000 ? Math.max(t, last + spacingMs) : t;   // a slot far ahead is garbage
    try { fs.writeFileSync(gate, (next / 1000).toFixed(6)); } catch {}
    return next;
  } finally {
    try { fs.unlinkSync(lock); } catch {}
  }
}

// Waits until this process may send one Jupiter request.
export async function waitTurn(opts = {}) {
  try {
    const now = opts.now ?? Date.now, sleep = opts.sleep ?? pause;
    const delay = (await reserve(opts)) - now();
    if (delay > 0) await sleep(Math.min(delay, 60_000));
  } catch {}
}
