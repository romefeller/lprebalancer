// Token-2022 mint facts for the signers: scaled UI amounts, pause, transfer hook.
//
// The tokenized stocks (MU, DJT, MSFTx) are Token-2022 mints with the
// scaledUiAmountConfig extension. The chain and every pool hold RAW amounts;
// a wallet, Jupiter and the owner see UI amounts:
//
//   UI = raw / 10^decimals × multiplier
//
// where the multiplier is `newMultiplier` once `newMultiplierEffectiveTimestamp`
// has passed (the token program's own rule, `now >= timestamp`), else
// `multiplier`. A stock split or a dividend reinvestment changes the
// multiplier and not one raw unit. A signer that reported raw/10^decimals as
// the balance of MSFTx (multiplier 1.0059) would be 0.59% short of what the
// owner holds and of what Jupiter prices.
//
// So: every human amount a signer reports is UI. The pool price stays
// pool-native (B per A in raw/10^decimals units), because the pool math, the
// ticks and the bins speak it; `uiPrice` is the same price in UI units:
//
//   uiPrice = price × multiplierB / multiplierA
//
// Pause and transfer hook: a paused mint refuses every transfer, so a write
// fails on chain anyway; a transfer hook runs a program the issuer chooses on
// every transfer, which none of the signers pass extra accounts for and which
// can take or block funds. Both refuse writes before anything is built.
// Reads keep working and report the flags.
//
// Pure functions only; the RPC read is a function the signer hands in, so the
// tests run on recorded accounts.

export const TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA';
export const TOKEN_2022_PROGRAM = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb';
export const REFUSE_PAUSED = 'refused: mint paused';
export const REFUSE_HOOK = 'refused: transfer hook';

// Extensions that change what an amount means in a way this module does not
// model. A mint with one of them is refused outright, for reads as well: a
// wrong amount is worse than no amount.
const UNSUPPORTED = new Set(['interestBearingConfig', 'unparseableExtension']);

function bad(mint, what) {
  return new Error(`mint ${mint}: ${what}`);
}

function positiveNumber(v) {
  const n = typeof v === 'string' || typeof v === 'number' ? Number(v) : NaN;
  return Number.isFinite(n) && n > 0 ? n : null;
}

/**
 * The multiplier in force at `nowSec` (unix seconds) for a scaledUiAmountConfig state.
 * @param {{multiplier: string|number, newMultiplier: string|number,
 *          newMultiplierEffectiveTimestamp: string|number}} cfg the extension state as jsonParsed gives it
 * @param {number} nowSec the time to evaluate at, in unix seconds
 * @returns {number} the multiplier; `newMultiplier` from its timestamp on (inclusive)
 * @throws {Error} when a field is missing, not a number, or not positive
 */
export function effectiveMultiplier(cfg, nowSec) {
  const m = positiveNumber(cfg?.multiplier);
  const next = positiveNumber(cfg?.newMultiplier);
  const at = Number(cfg?.newMultiplierEffectiveTimestamp);
  if (m == null || next == null || !Number.isFinite(at) || at < 0) {
    throw new Error('scaledUiAmountConfig unreadable');
  }
  if (!Number.isFinite(nowSec)) throw new Error('no clock to choose the multiplier');
  return nowSec >= at ? next : m;
}

/**
 * The facts a signer needs about one mint, from its jsonParsed account.
 * @param {string} mint the mint address, for error messages
 * @param {object|null} account the account as getMultipleAccounts(jsonParsed) returns it
 * @param {number} nowSec unix seconds, for the multiplier switch
 * @returns {{mint: string, programId: string, decimals: number, multiplier: number,
 *            paused: boolean, transferHook: string|null}} the mint's facts
 * @throws {Error} when the account is missing, not a mint of a token program, or
 *   carries extension data this module cannot read
 */
export function mintFacts(mint, account, nowSec) {
  if (!account) throw bad(mint, 'account not found');
  const owner = String(account.owner?.toBase58?.() ?? account.owner ?? '');
  if (owner !== TOKEN_PROGRAM && owner !== TOKEN_2022_PROGRAM) {
    throw bad(mint, `owner ${owner || 'unknown'} is not a token program`);
  }
  const parsed = account.data?.parsed;
  const info = parsed?.info;
  if (parsed?.type !== 'mint' || !info || typeof info !== 'object') throw bad(mint, 'no parsed mint data');
  const decimals = info.decimals;
  if (!Number.isInteger(decimals) || decimals < 0 || decimals > 18) throw bad(mint, `decimals ${decimals} unreadable`);
  const facts = { mint, programId: owner, decimals, multiplier: 1, paused: false, transferHook: null };
  if (info.extensions === undefined) return facts;
  if (!Array.isArray(info.extensions)) throw bad(mint, 'extensions unreadable');
  for (const e of info.extensions) {
    const name = e?.extension;
    if (typeof name !== 'string') throw bad(mint, 'an extension without a name');
    if (UNSUPPORTED.has(name)) throw bad(mint, `unsupported extension ${name}`);
    const s = e.state;
    if (name === 'scaledUiAmountConfig') {
      try { facts.multiplier = effectiveMultiplier(s, nowSec); } catch (err) { throw bad(mint, err.message); }
    } else if (name === 'pausableConfig') {
      if (typeof s?.paused !== 'boolean') throw bad(mint, 'pausableConfig unreadable');
      facts.paused = s.paused;
    } else if (name === 'transferHook') {
      if (!s || !('programId' in s) || (s.programId !== null && typeof s.programId !== 'string')) {
        throw bad(mint, 'transferHook unreadable');
      }
      facts.transferHook = s.programId;
    }
  }
  return facts;
}

/**
 * Read and decode several mints in one RPC call.
 * @param {(mints: string[]) => Promise<object[]>} fetchMany returns jsonParsed accounts in order
 * @param {string[]} mints mint addresses
 * @param {number} [nowSec] unix seconds; defaults to the local clock
 * @returns {Promise<object[]>} one mintFacts result per mint, in order
 * @throws {Error} when the RPC answer is not one account per mint, or a mint is unreadable
 */
export async function readMints(fetchMany, mints, nowSec = Date.now() / 1000) {
  const got = await fetchMany(mints);
  if (!Array.isArray(got) || got.length !== mints.length) {
    throw new Error(`mint read returned ${Array.isArray(got) ? got.length : typeof got} accounts for ${mints.length} mints`);
  }
  return mints.map((m, i) => mintFacts(m, got[i], nowSec));
}

/**
 * A raw on-chain amount in UI units.
 * @param {bigint|number|string|{toString(): string}} raw the raw amount (BN and decimal strings accepted)
 * @param {number} decimals the mint's decimals
 * @param {number} multiplier the effective multiplier (1 for a plain mint)
 * @returns {number} raw / 10^decimals × multiplier
 */
export function rawToUi(raw, decimals, multiplier) {
  const x = Number(String(raw));            // a JS number prints and parses back exactly
  return x * multiplier / 10 ** decimals;
}

/**
 * A UI amount as the raw amount that is at most worth it: a cap stays a ceiling.
 * @param {number|string} ui the UI amount, not negative
 * @param {number} decimals the mint's decimals
 * @param {number} multiplier the effective multiplier
 * @returns {bigint} floor(ui × 10^decimals / multiplier)
 * @throws {Error} when the amount is negative, not finite, or beyond exact integer range
 */
export function uiToRaw(ui, decimals, multiplier) {
  const u = Number(ui);
  if (!Number.isFinite(u) || u < 0) throw new Error(`amount ${ui} is not a non-negative number`);
  const x = u * 10 ** decimals / multiplier;
  if (x > Number.MAX_SAFE_INTEGER) throw new Error(`amount ${ui} is too large to convert exactly`);
  return BigInt(Math.floor(x));
}

/**
 * A UI amount in pool-native human units (raw / 10^decimals): what the pool math takes.
 * @param {number} ui the UI amount
 * @param {number} multiplier the effective multiplier
 * @returns {number} ui / multiplier
 */
export function uiToNative(ui, multiplier) {
  return Number(ui) / multiplier;
}

/**
 * The pool-native price (B per A, raw/10^decimals units) in UI units.
 * @param {number} price the pool-native price
 * @param {number} multiplierA token A's effective multiplier
 * @param {number} multiplierB token B's effective multiplier
 * @returns {number} price × multiplierB / multiplierA
 */
export function uiPrice(price, multiplierA, multiplierB) {
  return price * multiplierB / multiplierA;
}

/**
 * Why a write on a pool with these mints must not be built, or null when it may.
 * @param {object[]} facts mintFacts results for the pool's mints
 * @returns {string|null} REFUSE_PAUSED, REFUSE_HOOK, or null
 */
export function writeRefusal(facts) {
  if (facts.some(f => f.paused)) return REFUSE_PAUSED;
  if (facts.some(f => f.transferHook)) return REFUSE_HOOK;
  return null;
}

/**
 * Throw the refusal for a write on a pool with these mints, if there is one.
 * @param {object[]} facts mintFacts results for the pool's mints
 * @throws {Error} with message REFUSE_PAUSED or REFUSE_HOOK
 */
export function assertWritable(facts) {
  const why = writeRefusal(facts);
  if (why) throw new Error(why);
}

/**
 * The fields every signer adds to `pool`, `balance` and `status` for a pool's two mints.
 * @param {object} a mintFacts of token A
 * @param {object} b mintFacts of token B
 * @returns {object} multiplierA/B, paused (either side), pausedA/B, transferHookA/B, tokenProgramA/B
 */
export function mintFields(a, b) {
  return {
    multiplierA: a.multiplier, multiplierB: b.multiplier,
    paused: a.paused || b.paused, pausedA: a.paused, pausedB: b.paused,
    transferHookA: a.transferHook, transferHookB: b.transferHook,
    tokenProgramA: a.programId, tokenProgramB: b.programId,
  };
}
