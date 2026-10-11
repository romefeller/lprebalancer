// The Raydium CLMM instructions of an owner that does not sign for itself:
// a Squads vault (chains/solana/vault/account.mjs wraps what it signs).
// Byreal is a fork with the same layout: its signer builds with its own SDK
// and wraps with around() and bitmapFirst() from here.
//
// The SDK's own builders fund a temporary WSOL account from the signer and
// close it in the same transaction, and take token accounts from the owner's
// wallet as the SDK last read it. A vault does neither: its SOL is wrapped
// into its own WSOL ATA (vault/rules.json "wrap SOL") and unwrapped back into
// it after, and every token account is the vault's ATA, created by the vault
// if it is missing. The pool's own instructions come from the SDK's static
// builders (ClmmInstrument), so the bytes are the SDK's.
//
// Rent: Raydium takes the position's rent from `payer` and pays it back to
// the NFT owner at close, and `payer` is also the authority of the deposit.
// For a vault both are the vault: the rent goes round inside it.
import { createRequire } from 'node:module';
import { poolPda, RULES } from '../../chains/solana/vault/policy.mjs';
import SOLANA from '../../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { ClmmInstrument } = require('@raydium-io/raydium-sdk-v2');
const { PublicKey, SystemProgram, ComputeBudgetProgram, TransactionInstruction } = require('@solana/web3.js');
const spl = require('@solana/spl-token');
const BN = require('bn.js');

const NATIVE_MINT = new PublicKey(SOLANA.native_mint);
const TOKEN_2022 = new PublicKey(SOLANA.programs.token_2022);
// A Squads call around a Raydium instruction needs more than the default
// 200k units; measured on the fork, an open uses about 160k inside it.
export const VAULT_CU_LIMIT = 600_000;
// The decrease rule pins the pool's bitmap extension at this index, and each
// reward recipient after it (vault/rules.json).
const DECREASE = RULES.venues['raydium-clmm'].instructions.find(i => i.name === 'decrease_liquidity_v2');
const BITMAP = DECREASE.pins.find(p => p.role === 'pda');
const DECREASE_DISCRIMINATOR = Buffer.from(DECREASE.discriminator, 'hex');

export function ata(mint, owner, program = spl.TOKEN_PROGRAM_ID) {
  return spl.getAssociatedTokenAddressSync(new PublicKey(mint), owner, true, new PublicKey(program));
}

// The owner's ATA for each mint, created if missing (idempotent): the vault
// funds its own accounts and gets the rent back when it closes one.
export function ensureAtas(owner, mints) {
  return mints.map(({ mint, program }) => spl.createAssociatedTokenAccountIdempotentInstruction(
    owner, ata(mint, owner, program), owner, new PublicKey(mint), new PublicKey(program)));
}

// `lamports` of the owner's SOL into its WSOL ATA, ready for the program.
export function wrapSol(owner, lamports) {
  const wsol = ata(NATIVE_MINT, owner);
  return [SystemProgram.transfer({ fromPubkey: owner, toPubkey: wsol, lamports }),
    spl.createSyncNativeInstruction(wsol)];
}

// The WSOL ATA closed back into the owner: what is left is native SOL again.
export function unwrapSol(owner) {
  return spl.createCloseAccountInstruction(ata(NATIVE_MINT, owner), owner, owner);
}

// The two pool tokens as {mint, program}, in pool order.
function poolMints(poolInfo) {
  return [poolInfo.mintA, poolInfo.mintB].map(m => ({ mint: m.address, program: m.programId }));
}

// The pool's instructions `ixs` as the owner runs them: a compute budget
// (`budget` {units, microLamports}, else VAULT_CU_LIMIT units), the owner's
// token accounts for `mints`, `wrap` lamports of SOL wrapped first, and the
// WSOL account closed back into the owner after, when SOL is one of `mints`.
export function around(owner, mints, ixs, { wrap = 0, budget = null } = {}) {
  const lamports = BigInt(wrap.toString());
  return [ComputeBudgetProgram.setComputeUnitLimit({ units: budget?.units ?? VAULT_CU_LIMIT }),
    ...(budget ? [ComputeBudgetProgram.setComputeUnitPrice({ microLamports: budget.microLamports })] : []),
    ...ensureAtas(owner, mints), ...(lamports > 0n ? wrapSol(owner, lamports) : []), ...ixs,
    ...(mints.some(m => String(m.mint) === NATIVE_MINT.toBase58()) ? [unwrapSol(owner)] : [])];
}

// The native side's amount of a deposit, or 0: what to wrap first. Pure.
export function nativeIn(mintA, mintB, amountA, amountB) {
  if (String(mintA) === NATIVE_MINT.toBase58()) return amountA;
  if (String(mintB) === NATIVE_MINT.toBase58()) return amountB;
  return 0;
}

// The owner's two token accounts, as the SDK's builders name them.
function tokenAccounts(poolInfo, owner) {
  return { tokenAccountA: ata(poolInfo.mintA.address, owner, poolInfo.mintA.programId),
    tokenAccountB: ata(poolInfo.mintB.address, owner, poolInfo.mintB.programId) };
}

// What a deposit (open, increase) wraps: the native side's cap.
function depositWrap(poolInfo, base, baseAmount, otherAmountMax) {
  const [amountA, amountB] = base === 'MintA' ? [baseAmount, otherAmountMax] : [otherAmountMax, baseAmount];
  return nativeIn(poolInfo.mintA.address, poolInfo.mintB.address, amountA, amountB);
}

// Open a position owned by `owner`, paid by it. `lean`: a Token-2022 NFT and
// no Metaplex metadata (LPBOT_RAYDIUM_LEAN=1). Returns {ixs, signers, nftMint}.
export async function openIxs({ poolInfo, poolKeys, owner, tickLower, tickUpper, base, baseAmount, otherAmountMax, lean }) {
  const built = await ClmmInstrument.openPositionFromBaseInstructions({
    poolInfo, poolKeys, ownerInfo: { feePayer: owner, wallet: owner, ...tokenAccounts(poolInfo, owner) },
    tickLower, tickUpper, base, baseAmount, otherAmountMax, liquidity: new BN(0),
    withMetadata: lean ? 'no-create' : 'create', nft2022: Boolean(lean),
  });
  return { ixs: around(owner, poolMints(poolInfo), built.instructions, { wrap: depositWrap(poolInfo, base, baseAmount, otherAmountMax) }),
    signers: built.signers, nftMint: built.address.nftMint };
}

// Add to a position the owner holds.
export function increaseIxs({ poolInfo, poolKeys, position, owner, base, baseAmount, otherAmountMax, nft2022 }) {
  const built = ClmmInstrument.increasePositionFromBaseInstructions({
    poolInfo, poolKeys, ownerPosition: position, ownerInfo: { wallet: owner, ...tokenAccounts(poolInfo, owner) },
    base, baseAmount, otherAmountMax, nft2022,
  });
  return around(owner, poolMints(poolInfo), built.instructions, { wrap: depositWrap(poolInfo, base, baseAmount, otherAmountMax) });
}

// The decrease as the vault rule pins it: the pool's bitmap extension first
// among the extra accounts (the program finds it anywhere among them, by its
// address), so the reward recipients after it sit at fixed indexes. The SDK
// names it only when the band needs it. Pure.
export function bitmapFirst(ix, programId, pool) {
  if (!ix.programId.equals(programId) || !ix.data.subarray(0, 8).equals(DECREASE_DISCRIMINATOR)) return ix;
  const bitmap = poolPda(BITMAP.seeds, pool, programId);
  const extra = ix.keys.slice(BITMAP.index).filter(k => !k.pubkey.equals(bitmap));
  const keys = [...ix.keys.slice(0, BITMAP.index), { pubkey: bitmap, isSigner: false, isWritable: true }, ...extra];
  return new TransactionInstruction({ programId: ix.programId, keys, data: ix.data });
}

// Take `liquidity` out of a position (0: fees and rewards only), into the
// owner's accounts; `close` also burns the NFT and closes the position.
export function decreaseIxs({ poolInfo, poolKeys, position, owner, liquidity, amountMinA, amountMinB, nft2022, close, budget }) {
  const programId = new PublicKey(poolInfo.programId);
  const rewards = (poolKeys.rewardInfos ?? []).map(r => ({ mint: r.mint.address, program: r.mint.programId }));
  const built = ClmmInstrument.decreaseLiquidityInstructions({
    poolInfo, poolKeys, ownerPosition: position, nft2022,
    ownerInfo: { wallet: owner, ...tokenAccounts(poolInfo, owner), rewardAccounts: rewards.map(r => ata(r.mint, owner, r.program)) },
    liquidity, amountMinA, amountMinB,
  });
  const ixs = built.instructions.map(ix => bitmapFirst(ix, programId, poolInfo.id));
  if (close) {
    ixs.push(...ClmmInstrument.closePositionInstructions({ poolInfo, poolKeys, ownerPosition: position,
      ownerInfo: { wallet: owner }, nft2022 }).instructions);
  }
  return around(owner, [...poolMints(poolInfo), ...rewards], ixs, { budget });
}

// An exact-in swap on the pool, from and to the owner's accounts: SOL sold
// is wrapped first, SOL bought is unwrapped after.
export function swapIxs({ poolInfo, poolKeys, owner, inputMint, amountIn, amountOutMin, remainingAccounts }) {
  const built = ClmmInstrument.makeSwapBaseInInstructions({
    poolInfo, poolKeys, observationId: new PublicKey(poolKeys.observationId),
    ownerInfo: { wallet: owner, ...tokenAccounts(poolInfo, owner) },
    inputMint: new PublicKey(inputMint), amountIn, amountOutMin, sqrtPriceLimitX64: new BN(0), remainingAccounts,
  });
  return around(owner, poolMints(poolInfo), built.instructions, { wrap: new PublicKey(inputMint).equals(NATIVE_MINT) ? amountIn : 0 });
}

// Whether a position's NFT is a Token-2022 mint (a lean open): read once
// before a decrease or increase, which name the NFT's token program.
export async function isNft2022(connection, nftMint) {
  const a = await connection.getAccountInfo(new PublicKey(nftMint));
  return Boolean(a?.owner.equals(TOKEN_2022));
}
