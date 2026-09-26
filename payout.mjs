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
//
// `amount` is in human units of the mint. The recipient's associated token
// account is created if missing (idempotent instruction; the LP wallet pays
// its rent once, about 0.002 SOL). The native mint So111...112 sends lamports.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { Connection, Keypair, PublicKey, SystemProgram, Transaction, sendAndConfirmTransaction } = require('@solana/web3.js');
const spl = require('@solana/spl-token');

const DIR = path.dirname(new URL(import.meta.url).pathname);
const HALT = path.join(DIR, 'HALT');
const RPC = process.env.SOLANA_RPC_URL ?? process.env.LPBOT_RPC ?? 'https://api.mainnet-beta.solana.com';
const PROFIT = process.env.LPBOT_PROFIT_WALLET ?? '';
const GAS_RESERVE_SOL = Number(process.env.LPBOT_GAS_RESERVE_SOL ?? 0.05);
const NATIVE_MINT = 'So11111111111111111111111111111111111111112';

function guard() {
  if (fs.existsSync(HALT)) throw new Error(`HALT present: ${fs.readFileSync(HALT, 'utf8').trim()}`);
}

async function secretBytes() {
  const p = process.env.WALLET_SECRET_PATH;
  if (!p) throw new Error('WALLET_SECRET_PATH is not set; refusing to guess a key location');
  const raw = fs.readFileSync(p, 'utf8').trim();
  if (raw.startsWith('[')) return Uint8Array.from(JSON.parse(raw));
  const bs58 = (await import('bs58')).default;
  return bs58.decode(raw);
}

function key(s, what) {
  try { return new PublicKey(s); } catch { throw new Error(`${what} is not a valid address: ${s}`); }
}

async function send(mintArg, amountArg, toArg, execute) {
  guard();
  if (!PROFIT) throw new Error('LPBOT_PROFIT_WALLET is not set; refusing to send');
  const to = key(toArg, 'destination');
  if (to.toBase58() !== key(PROFIT, 'LPBOT_PROFIT_WALLET').toBase58()) {
    throw new Error('destination is not the configured profit wallet; refusing to send');
  }
  const amount = Number(amountArg);
  if (!Number.isFinite(amount) || amount <= 0) throw new Error(`amount must be a positive number, got ${amountArg}`);
  const connection = new Connection(RPC, 'confirmed');
  const payer = Keypair.fromSecretKey(await secretBytes());
  if (payer.publicKey.equals(to)) throw new Error('profit wallet equals the LP wallet; nothing to do');
  const mint = key(mintArg, 'mint');
  const tx = new Transaction();
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
    const raw = BigInt(Math.floor(amount * 10 ** m.decimals));
    if (raw <= 0n) throw new Error('amount rounds to zero');
    const src = spl.getAssociatedTokenAddressSync(mint, payer.publicKey, false, programId);
    const dst = spl.getAssociatedTokenAddressSync(mint, to, false, programId);
    const acct = await spl.getAccount(connection, src, 'confirmed', programId);
    if (acct.amount < raw) throw new Error(`LP wallet holds ${Number(acct.amount) / 10 ** m.decimals}, less than ${amount}`);
    const dstExists = !!(await connection.getAccountInfo(dst));
    tx.add(spl.createAssociatedTokenAccountIdempotentInstruction(payer.publicKey, dst, to, mint, programId));
    tx.add(spl.createTransferCheckedInstruction(src, mint, dst, payer.publicKey, raw, m.decimals, [], programId));
    report = { mint: mint.toBase58(), amount: Number(raw) / 10 ** m.decimals, raw: raw.toString(),
               decimals: m.decimals, recipientAccountExisted: dstExists };
  }
  report = { ...report, from: payer.publicKey.toBase58(), to: to.toBase58() };
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
  const signature = await sendAndConfirmTransaction(connection, tx, [payer], { commitment: 'confirmed' });
  console.log(JSON.stringify({ ...report, signature, sent: true }, null, 1));
}

async function main() {
  const args = process.argv.slice(2);
  const execute = args.includes('--execute');
  const [cmd, ...rest] = args.filter(x => x !== '--execute');
  if (cmd === 'send') return send(rest[0], rest[1], rest[2], execute);
  console.log('commands: send <mint> <amount> <to> [--execute]   (amount in human units)');
}

main().catch(e => {
  console.error('ERROR:', String(e?.message ?? e).replace(/[{}]/g, ' '));
  process.exitCode = 1;
});
