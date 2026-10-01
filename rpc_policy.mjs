// RPC endpoint policy, shared by the swap script and the signers.
//
// 2026-09-30: Jupiter answered 429 during a pre-open swap. The swap script's
// retry matched /rate/ in Jupiter's message, took it for an RPC limit and
// moved to solana-rpc.publicnode.com, which refuses indexed reads
// (getTokenAccountsByOwner) without a personal token: HTTP 403, JSON-RPC
// -32602 "Indexed requests require a personal token". Every swap that
// reached it failed, and the bot opened lopsided.
//
// The rules:
//   1. A Jupiter error is Jupiter's: it never moves to another RPC endpoint.
//   2. An RPC rate limit, a refusal (403 / indexed requests) or a transport
//      failure moves to the next endpoint, BEFORE anything is signed.
//   3. After a transaction is signed, nothing moves and nothing is retried:
//      a send that failed without a signature may still have reached a node.
//   4. Every endpoint is tagged with what it serves; indexed reads go only to
//      endpoints that serve them.

import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { isProgramFailure } from './signer_errors.mjs';

export const MAINNET = 'https://api.mainnet-beta.solana.com';
export const PUBLICNODE = 'https://solana-rpc.publicnode.com';

// What a public endpoint serves without a key. An endpoint from the
// environment (SOLANA_RPC_URL / LPBOT_RPC) is assumed to serve everything.
export const CAPS = {
  [MAINNET]: { indexed: true },
  [PUBLICNODE]: { indexed: false },
};

export function endpoints(env = process.env, { indexed = false } = {}) {
  const primary = env.SOLANA_RPC_URL ?? env.LPBOT_RPC ?? MAINNET;
  return [primary, MAINNET, PUBLICNODE]
    .filter((v, i, a) => v && a.indexOf(v) === i)
    .filter(u => !indexed || (CAPS[u]?.indexed ?? true));
}

// Thrown by Jupiter HTTP helpers: never an RPC problem.
export class JupiterError extends Error {}
// Thrown once a transaction is signed: never retried anywhere.
export class AfterSignError extends Error {}

// What an error means for the endpoint loop:
//   'fatal'    stop, pass it to the caller (after signing, Jupiter, an answer)
//   'rotate'   try the next endpoint (rate limit, refusal, transport)
// A signer marks an error after a send with `sent`. A program failure and a
// HALT are the same on every endpoint. All three are fatal, whatever the text.
export function errorKind(e) {
  if (e instanceof AfterSignError || e?.name === 'SentError' || e?.constructor?.name === 'SentError') return 'fatal';
  if (e?.sent === true) return 'fatal';
  if (e instanceof JupiterError) return 'fatal';
  const m = String(e?.message ?? e ?? '');
  if (/^Jupiter\b/.test(m)) return 'fatal';
  if (/HALT present/.test(m) || isProgramFailure(e)) return 'fatal';
  if (/\b429\b|Too Many Requests|rate.?limit/i.test(m)) return 'rotate';
  if (/\b403\b|Forbidden|Indexed requests|personal token|Request blocked/i.test(m)) return 'rotate';
  if (/fetch failed|ECONNRESET|ECONNREFUSED|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|socket hang up|timed? ?out|(?<![$\d.,])\b50[0-4]\b(?![.,]\d)|Internal Server Error|Service Unavailable|Bad Gateway/i.test(m)) return 'rotate';
  return 'fatal';
}

// Run fn(url) over the endpoints until one answers. Rotating errors move on
// (with a short pause, `tries` times per endpoint); a fatal one is thrown at
// once. The final error names every endpoint and its last error.
export async function overEndpoints(urls, fn, { tries = 2, pauseMs = 2500, sleep = ms => new Promise(r => setTimeout(r, ms)) } = {}) {
  const seen = [];
  for (const url of urls) {
    for (let attempt = 0; attempt < tries; attempt++) {
      try {
        return await fn(url);
      } catch (e) {
        if (errorKind(e) === 'fatal') throw e;
        seen.push(`${new URL(url).host}: ${String(e?.message ?? e).slice(0, 160)}`);
        if (attempt + 1 < tries) await sleep(pauseMs * (attempt + 1));
      }
    }
  }
  throw new Error(`all RPC endpoints failed: ${seen.join(' | ')}`);
}

// True when the module at metaUrl is the script node was started with. A
// signer runs its CLI only then, so a test can import it without effects.
export function isEntry(metaUrl, argv = process.argv) {
  if (!argv[1]) return false;
  try {
    return fs.realpathSync(argv[1]) === fs.realpathSync(fileURLToPath(metaUrl));
  } catch {
    return false;
  }
}
