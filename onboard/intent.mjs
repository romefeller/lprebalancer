// What an owner signs to create a vault, and how the server checks it. Pure,
// except verify() (it hashes and checks a signature, nothing else).
//
// The server builds the message from fields it validated, keeps it under a
// fresh nonce, and the owner's wallet signs exactly that text with
// signMessage (ed25519 over the UTF-8 bytes). Only the stored message and
// its fields are ever acted on.
import crypto from 'node:crypto';
import { createRequire } from 'module';
import { toRaw, fromRaw } from './units.mjs';

const require = createRequire(import.meta.url);
const { PublicKey } = require('@solana/web3.js');
const bs58 = require('bs58').default;

export class Invalid extends Error {}

// Profile names: what Telegram shows. Lower case, digits and inner dashes.
export const NAME = /^[a-z][a-z0-9-]{1,30}[a-z0-9]$/;
// The wallet row of a vault: the profile name and this suffix (both unique).
export const WALLET_SUFFIX = '-vault';

const SOLANA_ADDRESS = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;

// obj[key] when obj has it as its own entry ("__proto__" or "constructor" is no chain or token).
export const own = (obj, key) => (Object.hasOwn(obj ?? {}, key) ? obj[key] : undefined);

// A field as text: a JSON string, trimmed; anything else is empty.
export const text = (s) => (typeof s === 'string' ? s.trim() : '');

// An owner or payout address on `chain` (Solana only), in its one canonical
// spelling. A Solana wallet must be a key (on the curve): a vault or token
// account pasted by mistake cannot sign and would strand the payouts.
export function address(chain, s, what) {
  if (chain !== 'solana') throw new Invalid(`chain ${chain}: Solana only`);
  const v = text(s);
  let k = null;
  if (SOLANA_ADDRESS.test(v)) { try { k = new PublicKey(v); } catch { k = null; } }
  if (!k || k.toBase58() !== v) throw new Invalid(`${what}: not a Solana address`);
  if (!PublicKey.isOnCurve(k.toBytes())) throw new Invalid(`${what}: not a wallet (an account address, not a key)`);
  return v;
}

export const sameAddress = (chain, a, b) => a === b;

// A decimal as typed: a phone keyboard in many regions (iPhone, pt-BR) has a
// comma, not a point, so one comma and no point is a decimal comma ("0,18").
export function decimal(s) {
  const v = text(s);
  return /^\d+,\d+$/.test(v) ? v.replace(',', '.') : v;
}

// A positive decimal amount of `token` within its bounds, as a canonical
// string ("1.5", never "01.50"). Bounds are decimal strings in the config;
// `extra` (raw units) raises the minimum (the gas a native deposit pays).
export function amount(s, token, extra = 0n) {
  const v = decimal(s);
  if (!/^\d{1,12}(\.\d+)?$/.test(v)) throw new Invalid('amount: a number like 1.5');
  if (v.includes('.') && v.split('.')[1].length > token.decimals) throw new Invalid(`amount: at most ${token.decimals} decimals`);
  const raw = toRaw(v, token.decimals);
  if (raw <= 0n) throw new Invalid('amount: more than zero');
  const min = toRaw(token.min, token.decimals) + extra;
  if (raw < min) throw new Invalid(`amount: at least ${fromRaw(min, token.decimals)}`);
  if (raw > toRaw(token.max, token.decimals)) throw new Invalid(`amount: at most ${token.max}`);
  return fromRaw(raw, token.decimals);
}

// More capital for a vault that exists: a positive amount of `token` up to
// its max. No minimum: the gas was paid when the vault was made. Pure.
export function addAmount(s, token) {
  return amount(s, { ...token, min: '0' });
}

// Integer units: onboard/units.mjs (the page's bundle uses them too).
export { toRaw, fromRaw };

export function name(s, reserved = []) {
  const v = text(s);
  if (!NAME.test(v) || v.includes('--')) {
    throw new Invalid('name: 3 to 32 of a-z, 0-9 and single inner dashes, starting with a letter');
  }
  if (reserved.includes(v) || reserved.includes(v + WALLET_SUFFIX)) throw new Invalid(`name: ${v} is reserved`);
  return v;
}

// The chain's native coin: [symbol, token]. Exactly one token is native.
export function nativeOf(c) {
  const n = Object.entries(c.tokens).filter(([, t]) => t.native);
  if (n.length !== 1) throw new Error(`${c.label}: exactly one native token, found ${n.length}`);
  return n[0];
}

// What the owner pays the bot's key before anything is spent, in raw native
// units: the measured setup, its margin and the instance's running gas.
export function gasTotal(c) {
  const [, n] = nativeOf(c);
  return toRaw(c.gas.setup, n.decimals) + toRaw(c.gas.margin, n.decimals) + toRaw(c.gas.reserve, n.decimals);
}

// The least an owner can put in with `token`: the pool's minimum capital,
// plus the gas when the token is the native coin (it pays both). Pure.
export function minimum(c, token) {
  const t = c.tokens[token];
  return fromRaw(toRaw(t.min, t.decimals) + (t.native ? gasTotal(c) : 0n), t.decimals);
}

// The most a wallet can put in with `token` (the page's Max), from what it
// holds: `held` maps the token's and the native coin's symbols to raw units.
// The native coin keeps back the owner's fees (tokens.fees); a token goes in
// whole, and the native coin must hold the bot's gas and the fees beside it.
// `add`: more capital for a vault that exists, so no gas and no minimum.
// Never above the token's max. {amount} (a decimal with a point) or {short}
// (why there is none). Pure.
export function maxAmount(c, token, held, { add = false } = {}) {
  const t = c.tokens[token], [native, n] = nativeOf(c);
  const fees = toRaw(t.fees, n.decimals), have = held[token], gas = add ? 0n : gasTotal(c);
  const min = add ? 1n : toRaw(minimum(c, token), t.decimals);
  const room = t.native ? have - fees : have;
  if (room < min) {
    const need = t.native ? `${fromRaw(min + fees, t.decimals)} ${token} with your fees` : `${fromRaw(min, t.decimals)} ${token}`;
    return { short: `not enough: you have ${fromRaw(have, t.decimals)} ${token}, the minimum is ${need}` };
  }
  if (!t.native && held[native] < gas + fees) {
    return { short: `not enough ${native} for ${add ? 'your fees' : 'the bot\'s gas and your fees'}: you have ${fromRaw(held[native], n.decimals)} ${native}, `
      + `you need ${fromRaw(gas + fees, n.decimals)} ${native}` };
  }
  const max = toRaw(t.max, t.decimals);
  return { amount: fromRaw(room < max ? room : max, t.decimals) };
}

// Who pays a fee share, from the config: none when the profit wallet is the
// operator's own; else `fee.bps` of the payout-token fees to the operator.
export function feeTerms(chain, profit, operator, bps) {
  if (sameAddress(chain, profit, operator)) return { special: true, feeBps: 0, feeWallet: null };
  return { special: false, feeBps: bps, feeWallet: operator };
}

// The fields of a request, validated, or Invalid. `cfg` is onboard.json,
// `botKeys` the bot's public keys on the chain (never an owner or payee).
export function fields(body, cfg, botKeys = []) {
  const chain = text(body?.chain);
  const c = own(cfg.chains, chain);
  if (!c) throw new Invalid(`chain: one of ${Object.keys(cfg.chains).join(', ')}`);
  const token = text(body.token);
  const t = own(c.tokens, token);
  if (!t) throw new Invalid(`token: one of ${Object.keys(c.tokens).join(', ')}`);
  const owner = address(chain, body.owner, 'owner');
  const profit = address(chain, body.profitWallet, 'profit wallet');
  for (const k of botKeys) {
    if (sameAddress(chain, owner, k) || sameAddress(chain, profit, k)) throw new Invalid('the bot\'s own key cannot own a vault or take payouts');
  }
  const fee = feeTerms(chain, profit, c.operatorWallet, cfg.fee.bps);
  const [native, nt] = nativeOf(c);
  const gas = gasTotal(c);
  const total = amount(body.amount, t, t.native ? gas : 0n);
  const deposit = t.native ? fromRaw(toRaw(total, t.decimals) - gas, t.decimals) : total;
  return { chain, owner, profitWallet: profit, token, amount: total, deposit, gas: fromRaw(gas, nt.decimals), gasToken: native,
    name: name(body.name, cfg.reservedNames), ...fee };
}

// The text the owner signs. One field per line; nothing the server acts on
// is left out of it.
export function message(f, c, { site, nonce, expires, payer }) {
  const share = f.special ? 'none (the operator\'s own wallet)'
    : `${f.feeBps / 100}% of the ${c.payoutToken} fees, paid to ${f.feeWallet}`;
  return [
    `${site}: create one vault and one pool for this wallet.`,
    '',
    `Chain: ${c.label}`,
    `Pool: ${c.pair} (the strategy of ${c.template})`,
    `Amount: ${f.amount} ${f.token}`,
    `Gas to the bot: ${f.gas} ${f.gasToken}, paid now to the bot's key ${payer} (setup and running gas)`,
    `Deposit into the vault: ${f.deposit} ${f.token}`,
    `Profit wallet: ${f.profitWallet}`,
    `Name: ${f.name}`,
    `Owner: ${f.owner}`,
    `Fee share: ${share}`,
    `Nonce: ${nonce}`,
    `Expires: ${new Date(expires).toISOString()}`,
  ].join('\n');
}

export const newNonce = () => crypto.randomBytes(16).toString('hex');

// --- Solana off-chain messages ------------------------------------------------
//
// A hardware wallet (Ledger, and wallets that sign like it) does not sign the
// raw text: it signs the Solana off-chain message that carries it, so that no
// signed message can ever be a transaction. Two layouts are in use:
//   short  (solana-sdk OffchainMessage v0, the Ledger app):
//          "\xffsolana offchain" | version 0 | format | length u16 LE | text
//   full   (the later v0 spec, with an application domain and the signers):
//          "\xffsolana offchain" | version 0 | domain (32) | format | signer count | signers | length u16 LE | text
// format: 0 printable ASCII (0x20-0x7e), 1 UTF-8 of at most 1212 bytes, 2 UTF-8.
// The first byte 0xff is no transaction's: a message starts with its
// signature count (at most 127 for a legacy one) or 0x80 + a version.
export const OFFCHAIN_DOMAIN = Buffer.concat([Buffer.from([0xff]), Buffer.from('solana offchain', 'ascii')]);
const LIMITED_UTF8 = 1212;

// The formats `bytes` may be declared as. Pure.
export function offchainFormats(bytes) {
  const out = [];
  if (bytes.length && bytes.every(b => b >= 0x20 && b <= 0x7e)) out.push(0);
  if (bytes.length <= LIMITED_UTF8) out.push(1);
  out.push(2);
  return out;
}

// The short layout of `text` declared as `format`. Pure.
export function offchainShort(text, format) {
  const msg = Buffer.from(text, 'utf8');
  const len = Buffer.alloc(2); len.writeUInt16LE(msg.length);
  return Buffer.concat([OFFCHAIN_DOMAIN, Buffer.from([0, format]), len, msg]);
}

// What a signed off-chain message says: {layout, format, signers, text}, or
// null when the bytes are not one (a wrong length, an unknown version or
// format, bytes after the text). Pure.
export function parseOffchain(bytes) {
  const b = Buffer.from(bytes);
  if (b.length < 20 || !b.subarray(0, 16).equals(OFFCHAIN_DOMAIN) || b[16] !== 0) return null;
  // the text at `at`: both callers know the two length bytes are there
  const text = (at, format, signers, layout) => {
    const len = b.readUInt16LE(at), msg = b.subarray(at + 2);
    if (msg.length !== len || !offchainFormats(msg).includes(format)) return null;
    let s;
    try { s = new TextDecoder('utf-8', { fatal: true }).decode(msg); } catch { return null; }
    return { layout, format, signers, text: s };
  };
  const short = text(18, b[17], [], 'short');
  if (short) return short;
  // full: domain 17..49, format 49, count 50, signers, length
  if (b.length < 53) return null;
  const count = b[50], at = 51 + 32 * count;
  if (count < 1 || b.length < at + 2) return null;
  const signers = Array.from({ length: count }, (_, i) => new PublicKey(b.subarray(51 + 32 * i, 83 + 32 * i)).toBase58());
  return text(at, b[49], signers, 'full');
}

// The byte strings an owner's wallet may have signed for `text`: the text
// itself, or an off-chain message of exactly this text whose only signer (in
// the full layout) is the owner. `signed` (the bytes the wallet says it
// signed, base64) narrows it to that one; anything else is refused. Pure.
export function signedForms(text, owner, signed = null) {
  const raw = Buffer.from(text, 'utf8');
  if (signed) {
    const b = Buffer.from(String(signed), 'base64');
    if (b.equals(raw)) return [b];
    const m = parseOffchain(b);
    if (!m || m.text !== text) return [];
    if (m.layout === 'full' && !(m.signers.length === 1 && m.signers[0] === owner)) return [];
    return [b];
  }
  return [raw, ...offchainFormats(raw).map(f => offchainShort(text, f))];
}

// Whether `signature` is `owner`'s over `text`: a base58 or base64 ed25519
// signature by the owner's key over the text or over an off-chain message of
// it (signedForms; `signed` is what the wallet reports it signed). Any chain
// but Solana: false.
export async function verify(chain, owner, text, signature, signed = null) {
  if (chain !== 'solana') return false;
  const sig = decodeSignature(signature);
  if (sig.length !== 64) return false;
  const key = crypto.createPublicKey({ format: 'jwk', key: { kty: 'OKP', crv: 'Ed25519', x: Buffer.from(new PublicKey(owner).toBytes()).toString('base64url') } });
  if (signed !== null && !/^[A-Za-z0-9+/]+={0,2}$/.test(String(signed))) return false;
  return signedForms(text, owner, signed).some(m => crypto.verify(null, m, key, sig));
}

// A signature's 64 bytes from base58 or base64, or no bytes.
function decodeSignature(s) {
  const v = text(s);
  if (/^[A-Za-z0-9+/]{86}==$/.test(v)) return Buffer.from(v, 'base64');
  try { return Buffer.from(bs58.decode(v)); } catch { return Buffer.alloc(0); }
}

// The invite: its SHA-256 must be one of `hashes` (hex). Constant time.
export function inviteOk(code, hashes) {
  if (typeof code !== 'string' || code.length < 8 || code.length > 128) return false;
  const h = crypto.createHash('sha256').update(code, 'utf8').digest();
  let ok = false;
  for (const x of hashes) {
    if (!/^[0-9a-f]{64}$/.test(x)) continue;
    if (crypto.timingSafeEqual(h, Buffer.from(x, 'hex'))) ok = true;
  }
  return ok;
}
