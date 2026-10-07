// Rebalancer — payouts from the LP wallet to the profit wallet.
//
// One job: send an amount of one SPL token (or native SOL) from the LP wallet
// to the profit wallet named in the environment. It will not send anywhere
// else: the destination argument must equal LPBOT_PROFIT_WALLET, so a bad
// argument cannot redirect funds. Same conventions as the signers
// (SIGNER_CONTRACT.md): HALT guard, key read from WALLET_SECRET_PATH and never
// printed, one JSON object on stdout, `ERROR: <message>` on stderr with exit 1,
// dry run by default.
//
//   node payout.mjs send <mint> <amount> <to> [--execute]
//   node payout.mjs balance <mint>
//
// `amount` is in human units of the mint: UI units, so a Token-2022 scaled
// mint's multiplier applies (token2022.mjs), as in every signer. A paused mint
// or one with a transfer hook refuses. The recipient's associated token
// account is created if missing (idempotent instruction; the LP wallet pays
// its rent once, about 0.002 SOL). The native mint So111...112 sends lamports.
import fs from 'node:fs';
import path from 'node:path';
import { assertNotHalted } from './halt_guard.mjs';
import { createRequire } from 'node:module';

import { readMints, rawToUi, uiToRaw, writeRefusal } from './token2022.mjs';
import { isEntry } from './rpc_policy.mjs';
import { NeverLanded, priorityCuPrice, sendUntilLanded, REBROADCAST_MS } from './tx_send.mjs';

export { NeverLanded, sendUntilLanded, REBROADCAST_MS };

const require = createRequire(import.meta.url);
const { ComputeBudgetProgram, Connection, Keypair, PublicKey, SystemProgram, Transaction } = require('@solana/web3.js');
const spl = require('@solana/spl-token');

const DIR = path.dirname(new URL(import.meta.url).pathname);
const RPC = process.env.SOLANA_RPC_URL ?? process.env.LPBOT_RPC ?? 'https://api.mainnet-beta.solana.com';
const PROFIT = process.env.LPBOT_PROFIT_WALLET ?? '';
// The pin lives in the service environment, not in the database: a database
// write alone cannot redirect payouts (security review, 2026-09-26).
const PIN = process.env.LPBOT_PROFIT_WALLET_PIN ?? '';
const GAS_RESERVE_SOL = Number(process.env.LPBOT_GAS_RESERVE_SOL ?? 0.05);
const NATIVE_MINT = 'So11111111111111111111111111111111111111112';

// The priority fee. 2026-10-02: two payouts sent with none, and sent once,
// expired unconfirmed ("block height exceeded") while the swaps beside them,
// which pay one, landed. A payout is two small instructions (an idempotent
// ATA create and a transfer), well under CU_LIMIT; its price is the 75th
// percentile of the recent fees on its own writable accounts, at least
// CU_PRICE_FLOOR, and never more than PRIORITY_MAX_LAMPORTS in all.
export const CU_LIMIT = 60_000;
export const CU_PRICE_FLOOR = 10_000;                       // micro-lamports per unit: 600 lamports in all
export const PRIORITY_MAX_LAMPORTS = Number(process.env.LPBOT_PAYOUT_PRIORITY_MAX_LAMPORTS ?? 50_000);

// Compute-unit price (micro-lamports) from recent prioritization fees. Pure.
export function payoutCuPrice(recent, units = CU_LIMIT, cap = PRIORITY_MAX_LAMPORTS) {
  return priorityCuPrice(recent, units, cap, CU_PRICE_FLOOR);
}

function guard() {
  assertNotHalted(DIR);                     // the global HALT and this profile's (halt_guard.mjs)
}

async function secretBytes() {
  const p = process.env.WALLET_SECRET_PATH;
  if (!p) throw new Error('WALLET_SECRET_PATH is not set; refusing to guess a key location');
  const raw = fs.readFileSync(p, 'utf8').trim();
  if (raw.startsWith('[')) return Uint8Array.from(JSON.parse(raw));
  const bs58 = (await import('bs58')).default;
  return bs58.decode(raw);
}

// The mint's facts (multiplier, pause, transfer hook) from its parsed account.
async function facts(connection, mint) {
  const [f] = await readMints(async ms => (await connection.getMultipleParsedAccounts(
    ms.map(x => new PublicKey(x)), 'confirmed')).value, [mint.toBase58()]);
  return f;
}

function key(s, what) {
  try { return new PublicKey(s); } catch { throw new Error(`${what} is not a valid address: ${s}`); }
}

async function send(mintArg, amountArg, toArg, execute) {
  guard();
  if (!PROFIT) throw new Error('LPBOT_PROFIT_WALLET is not set; refusing to send');
  if (!PIN) throw new Error('LPBOT_PROFIT_WALLET_PIN is not set; refusing to send');
  const to = key(toArg, 'destination');
  if (to.toBase58() !== key(PROFIT, 'LPBOT_PROFIT_WALLET').toBase58()
      || to.toBase58() !== key(PIN, 'LPBOT_PROFIT_WALLET_PIN').toBase58()) {
    throw new Error('destination is not the pinned profit wallet; refusing to send');
  }
  const amount = Number(amountArg);
  if (!Number.isFinite(amount) || amount <= 0) throw new Error(`amount must be a positive number, got ${amountArg}`);
  const connection = new Connection(RPC, 'confirmed');
  const payer = Keypair.fromSecretKey(await secretBytes());
  if (payer.publicKey.equals(to)) throw new Error('profit wallet equals the LP wallet; nothing to do');
  const mint = key(mintArg, 'mint');
  const tx = new Transaction();
  const writable = [payer.publicKey];
  let report;
  if (mint.toBase58() === NATIVE_MINT) {
    const lamports = Math.floor(amount * 1e9);
    const bal = await connection.getBalance(payer.publicKey);
    if ((bal - lamports) / 1e9 < GAS_RESERVE_SOL) {
      throw new Error(`sending ${amount} SOL would leave ${(bal - lamports) / 1e9} SOL, below the ${GAS_RESERVE_SOL} gas reserve`);
    }
    tx.add(SystemProgram.transfer({ fromPubkey: payer.publicKey, toPubkey: to, lamports }));
    report = { mint: NATIVE_MINT, symbol: 'SOL', amount: lamports / 1e9, raw: String(lamports) };
  } else {
    const info = await connection.getAccountInfo(mint);
    if (!info) throw new Error(`mint ${mint.toBase58()} not found`);
    const programId = info.owner;
    const m = await spl.getMint(connection, mint, 'confirmed', programId);
    const f = await facts(connection, mint);
    const refusal = writeRefusal([f]);
    if (refusal) throw new Error(refusal);
    const raw = f.multiplier === 1 ? BigInt(Math.floor(amount * 10 ** m.decimals)) : uiToRaw(amount, m.decimals, f.multiplier);
    if (raw <= 0n) throw new Error('amount rounds to zero');
    const src = spl.getAssociatedTokenAddressSync(mint, payer.publicKey, false, programId);
    const dst = spl.getAssociatedTokenAddressSync(mint, to, false, programId);
    const acct = await spl.getAccount(connection, src, 'confirmed', programId);
    if (acct.amount < raw) throw new Error(`LP wallet holds ${rawToUi(acct.amount, m.decimals, f.multiplier)}, less than ${amount}`);
    const dstExists = !!(await connection.getAccountInfo(dst));
    writable.push(src, dst);
    tx.add(spl.createAssociatedTokenAccountIdempotentInstruction(payer.publicKey, dst, to, mint, programId));
    tx.add(spl.createTransferCheckedInstruction(src, mint, dst, payer.publicKey, raw, m.decimals, [], programId));
    report = { mint: mint.toBase58(), amount: rawToUi(raw, m.decimals, f.multiplier), raw: raw.toString(),
               decimals: m.decimals, multiplier: f.multiplier, recipientAccountExisted: dstExists };
  }
  let recent = [];
  try { recent = await connection.getRecentPrioritizationFees({ lockedWritableAccounts: writable }); } catch { recent = []; }
  const cuPrice = payoutCuPrice(recent);
  tx.instructions.unshift(ComputeBudgetProgram.setComputeUnitLimit({ units: CU_LIMIT }),
                          ComputeBudgetProgram.setComputeUnitPrice({ microLamports: cuPrice }));
  report = { ...report, from: payer.publicKey.toBase58(), to: to.toBase58(),
             priorityMicroLamports: cuPrice, priorityLamports: Math.ceil(cuPrice * CU_LIMIT / 1e6) };
  if (!execute) {
    tx.feePayer = payer.publicKey;
    tx.recentBlockhash = (await connection.getLatestBlockhash()).blockhash;
    const sim = await connection.simulateTransaction(tx, [payer]);
    console.log(JSON.stringify({ ...report, simulation: { ok: !sim.value.err, err: sim.value.err ?? null },
                                 signature: null, sent: false }, null, 1));
    console.log('DRY RUN — transfer built and simulated. Pass --execute to sign and send.');
    return;
  }
  guard();
  // Send, re-send until confirmed or expired (sendUntilLanded). A failure
  // after the send is not a failed transfer: report the signature as partial
  // so the loop never pays it twice (review, 2026-09-26). An expired payout
  // the chain has no record of never landed: an error, so the loop owes it.
  const bh = await connection.getLatestBlockhash('confirmed');
  tx.feePayer = payer.publicKey; tx.recentBlockhash = bh.blockhash;
  tx.sign(payer);
  let signature;
  try {
    signature = await sendUntilLanded(connection, tx.serialize(), bh.lastValidBlockHeight);
  } catch (e) {
    if (e instanceof NeverLanded) throw new NeverLanded(`payout ${e.message}, it is owed again`);
    if (!e.afterSend) throw e;                                   // the first send failed: nothing proves it went
    console.log(JSON.stringify({ ...report, signature: e.signature, sent: true, partial: true,
                                 error: String(e.message ?? e) }, null, 1));
    process.exitCode = 1;
    return;
  }
  console.log(JSON.stringify({ ...report, signature, sent: true }, null, 1));
}

// What the LP wallet holds of one mint, in human units. Read-only.
async function balance(mintArg) {
  const connection = new Connection(RPC, 'confirmed');
  const payer = Keypair.fromSecretKey(await secretBytes());
  const mint = key(mintArg, 'mint');
  if (mint.toBase58() === NATIVE_MINT) {
    const lamports = await connection.getBalance(payer.publicKey);
    console.log(JSON.stringify({ mint: NATIVE_MINT, amount: lamports / 1e9, decimals: 9 }, null, 1));
    return;
  }
  const info = await connection.getAccountInfo(mint);
  if (!info) throw new Error(`mint ${mint.toBase58()} not found`);
  const m = await spl.getMint(connection, mint, 'confirmed', info.owner);
  const f = await facts(connection, mint);
  const ata = spl.getAssociatedTokenAddressSync(mint, payer.publicKey, false, info.owner);
  let raw = 0n;
  try { raw = (await spl.getAccount(connection, ata, 'confirmed', info.owner)).amount; } catch { raw = 0n; }
  console.log(JSON.stringify({ mint: mint.toBase58(), amount: rawToUi(raw, m.decimals, f.multiplier),
                               raw: raw.toString(), decimals: m.decimals, multiplier: f.multiplier }, null, 1));
}

async function main() {
  const args = process.argv.slice(2);
  const execute = args.includes('--execute');
  const [cmd, ...rest] = args.filter(x => x !== '--execute');
  if (cmd === 'send') return send(rest[0], rest[1], rest[2], execute);
  if (cmd === 'balance') return balance(rest[0]);
  console.log('commands: send <mint> <amount> <to> [--execute] | balance <mint>   (amounts in human units)');
}

if (isEntry(import.meta.url)) {
  main().catch(e => {
    console.error('ERROR:', String(e?.message ?? e).replace(/[{}]/g, ' '));
    process.exitCode = 1;
  });
}
