// Rebalancer — reclaims rent from the LP wallet's empty token accounts.
//
// One job: close token accounts the wallet owns that hold exactly zero, and
// return their rent to the wallet itself. Nothing is sent anywhere else: the
// rent destination is the wallet, fixed in the code, never an argument.
// Accounts of the mints named on the command line (the pool's two tokens,
// the payout token, reward tokens) are kept even when empty, because the bot
// would pay their rent again at the next open or harvest. Same conventions as
// the signers (SIGNER_CONTRACT.md): HALT guard, key read from
// WALLET_SECRET_PATH and never printed, one JSON object on stdout,
// `ERROR: <message>` on stderr with exit 1, dry run by default.
//
//   node chains/solana/janitor.mjs close-empty <keepMint> [<keepMint> ...] [--execute]
//
// 2026-09-28: four empty accounts (mSOL, RAY, ZEC, JitoSOL) held 0.00705 SOL.
// A close costs 5,000 lamports and returns 1.5-2.0 million.
import fs from 'node:fs';
import path from 'node:path';
import { assertNotHalted } from '../../shared/halt_guard.mjs';
import { BOT_ROOT } from '../../bot_root.mjs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { Connection, Keypair, PublicKey, Transaction, sendAndConfirmTransaction } = require('@solana/web3.js');
const spl = require('@solana/spl-token');

const RPC = process.env.SOLANA_RPC_URL ?? process.env.LPBOT_RPC ?? 'https://api.mainnet-beta.solana.com';
export const MAX_PER_TX = 8;
export const TOKEN_PROGRAMS = [spl.TOKEN_PROGRAM_ID.toBase58(), spl.TOKEN_2022_PROGRAM_ID.toBase58()];

// Which accounts may be closed. `accounts` are jsonParsed token accounts:
// { pubkey, program, info: { mint, owner, state, tokenAmount: { amount } }, lamports }.
// Pure: the whole policy is here.
export function planClose(accounts, owner, keepMints) {
  const keep = new Set(keepMints);
  return accounts.filter(a =>
    a.info && a.info.owner === owner &&
    a.info.state === 'initialized' &&
    String(a.info.tokenAmount?.amount) === '0' &&
    !keep.has(a.info.mint) &&
    TOKEN_PROGRAMS.includes(a.program) &&
    // an account with a close authority other than the owner is not ours to close
    (a.info.closeAuthority == null || a.info.closeAuthority === owner) &&
    // Token-2022 accounts holding withheld transfer fees cannot be closed
    !(a.info.extensions || []).some(e => e.extension === 'transferFeeAmount' && Number(e.state?.withheldAmount ?? 0) > 0));
}

// The instructions for one batch: CloseAccount, rent to the owner, nothing else.
export function closeInstructions(plan, owner) {
  const o = new PublicKey(owner);
  return plan.map(a => spl.createCloseAccountInstruction(new PublicKey(a.pubkey), o, o, [], new PublicKey(a.program)));
}

// A transaction may only close accounts in the plan, with the rent going to
// the owner, and be paid by the owner. Checked before it is signed.
export function verifyCloseTx(tx, owner, plan) {
  const allowed = new Set(plan.map(a => a.pubkey));
  if (tx.feePayer?.toBase58() !== owner) throw new Error('fee payer is not the wallet; refusing');
  if (!tx.instructions.length || tx.instructions.length > MAX_PER_TX) throw new Error('unexpected instruction count; refusing');
  for (const ix of tx.instructions) {
    const pid = ix.programId.toBase58();
    if (!TOKEN_PROGRAMS.includes(pid)) throw new Error(`instruction for program ${pid}; refusing`);
    if (ix.data.length !== 1 || ix.data[0] !== 9) throw new Error('an instruction other than CloseAccount; refusing');
    const [acct, dest, auth] = ix.keys.map(k => k.pubkey.toBase58());
    if (!allowed.has(acct)) throw new Error(`closes ${acct}, not in the plan; refusing`);
    if (dest !== owner || auth !== owner) throw new Error('rent would go to someone else; refusing');
  }
  return true;
}

function guard() {
  assertNotHalted(BOT_ROOT);                // the global HALT and this profile's (shared/halt_guard.mjs)
}

async function secretBytes() {
  const p = process.env.WALLET_SECRET_PATH;
  if (!p) throw new Error('WALLET_SECRET_PATH is not set; refusing to guess a key location');
  const raw = fs.readFileSync(p, 'utf8').trim();
  if (raw.startsWith('[')) return Uint8Array.from(JSON.parse(raw));
  const bs58 = (await import('bs58')).default;
  return bs58.decode(raw);
}

async function tokenAccounts(connection, owner) {
  const out = [];
  for (const prog of TOKEN_PROGRAMS) {
    const r = await connection.getParsedTokenAccountsByOwner(new PublicKey(owner), { programId: new PublicKey(prog) });
    for (const a of r.value) {
      out.push({ pubkey: a.pubkey.toBase58(), program: prog, lamports: a.account.lamports, info: a.account.data.parsed.info });
    }
  }
  return out;
}

async function closeEmpty(keepMints, execute) {
  guard();
  for (const m of keepMints) new PublicKey(m);                   // each must be an address
  const payer = Keypair.fromSecretKey(await secretBytes());
  const owner = payer.publicKey.toBase58();
  const connection = new Connection(RPC, 'confirmed');
  const plan = planClose(await tokenAccounts(connection, owner), owner, keepMints);
  const report = { owner, closable: plan.map(a => ({ account: a.pubkey, mint: a.info.mint, lamports: a.lamports })),
                   reclaimSol: plan.reduce((n, a) => n + a.lamports, 0) / 1e9 };
  if (!plan.length || !execute) {
    console.log(JSON.stringify({ ...report, sent: false }, null, 1));
    return;
  }
  const signatures = [];
  for (let i = 0; i < plan.length; i += MAX_PER_TX) {
    const batch = plan.slice(i, i + MAX_PER_TX);
    const tx = new Transaction().add(...closeInstructions(batch, owner));
    tx.feePayer = payer.publicKey;
    verifyCloseTx(tx, owner, batch);
    signatures.push(await sendAndConfirmTransaction(connection, tx, [payer], { commitment: 'confirmed' }));
  }
  console.log(JSON.stringify({ ...report, sent: true, signature: signatures[signatures.length - 1], signatures }, null, 1));
}

async function main() {
  const args = process.argv.slice(2);
  const execute = args.includes('--execute');
  const rest = args.filter(a => a !== '--execute');
  if (rest[0] !== 'close-empty') throw new Error('usage: chains/solana/janitor.mjs close-empty <keepMint>... [--execute]');
  await closeEmpty(rest.slice(1), execute);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch(e => { console.error(`ERROR: ${e.message}`); process.exit(1); });
}
