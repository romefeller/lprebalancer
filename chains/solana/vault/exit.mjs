// The owners' way out: every LP position the vault holds closed into the
// vault, signed by an owner alone. No bot key signs, pays or receives
// anything, so it works with the bot revoked, broke or gone.
//
// Each venue's SDK builds the close for the vault as owner, as the venue's
// signer does; what it built is then sorted for an owner's transaction:
//   - associated token accounts the vault lacks are created by the owner
//     (idempotent, outside the vault's call: the owner pays the rent);
//   - the venue's own instructions run as the vault in one owner call
//     (squads.ownerExecuteIx), and only those EXIT_INSTRUCTIONS names, with
//     every token account the vault's own and every rent receiver the vault;
//   - the vault's WSOL account is closed back into the vault last.
// Compute-budget instructions of the SDK are dropped; each message sets its
// own limit. No lookup table: an owner's wallet signs legacy messages, so a
// close that does not fit one transaction is split over several, in order.
//
// Venue SDKs load on first use: importing EXIT_INSTRUCTIONS loads none.
import { createRequire } from 'module';
import * as sq from './squads.mjs';
import { instructionsOf, RULES } from './policy.mjs';
import { EXIT_INSTRUCTIONS } from './exit_rules.mjs';
import SOLANA from '../solana.json' with { type: 'json' };

export { EXIT_INSTRUCTIONS };

const require = createRequire(import.meta.url);
const { ComputeBudgetProgram, PublicKey, TransactionMessage } = require('@solana/web3.js');
const spl = require('@solana/spl-token');

// A signed legacy transaction: one signature (the owner's) and the message.
export const TX_BYTES = 1232;
const SIGNED_OVERHEAD = 1 + 64;
// The limit each message with a vault call sets. The fee is per signature
// (no priority price), so a high limit costs nothing.
const EXIT_UNITS = 1_400_000;
// The price move a close tolerates between build and landing: each minimum
// is what the position returns at the price this far against that token.
export const EXIT_PRICE_BPS = 100;

const NATIVE_MINT = new PublicKey(SOLANA.native_mint);
const TOKEN = new PublicKey(SOLANA.programs.token);
const TOKEN_PROGRAMS = [TOKEN, new PublicKey(SOLANA.programs.token_2022)];
const ATA_PROGRAM = new PublicKey(SOLANA.programs.associated_token);
const CLOSE_ACCOUNT = 9;

export class NotAnExit extends Error {}

function ata(mint, owner, program = spl.TOKEN_PROGRAM_ID) {
  return spl.getAssociatedTokenAddressSync(new PublicKey(mint), new PublicKey(owner), true, new PublicKey(program));
}

// An Anchor instruction's discriminator, as rules.json writes it.
function hex8(data) {
  return Buffer.from(data.subarray(0, 8)).toString('hex');
}

// The exit rules of `venue`: its rules.json instructions named in
// EXIT_INSTRUCTIONS, with their pins.
function exitRules(venue) {
  const allowed = EXIT_INSTRUCTIONS[venue];
  if (!allowed) throw new NotAnExit(`venue ${venue} has no owner exit`);
  const rules = instructionsOf(venue).filter(r => allowed.includes(r.name));
  if (rules.length !== allowed.length) throw new Error(`${venue}: an exit instruction is not in rules.json`);
  return rules;
}

// The vault's instructions of an exit, checked: the WSOL account closed into
// the vault, or a venue instruction EXIT_INSTRUCTIONS names, its owner and
// rent receiver the vault, its token accounts in `tokenAccounts` (the vault's
// own), its pool one of `pools` when given. Returns the names; throws
// NotAnExit on anything else. Pure.
export function checkExitIxs(venue, ixs, { vault, tokenAccounts = [], pools = null }) {
  const rules = exitRules(venue);
  const program = new PublicKey(RULES.venues[venue].program);
  vault = new PublicKey(vault);
  const own = new Set(tokenAccounts.map(k => new PublicKey(k).toBase58()));
  const wsol = ata(NATIVE_MINT, vault);
  return ixs.map(ix => {
    if (ix.programId.equals(TOKEN) && ix.data.length === 1 && ix.data[0] === CLOSE_ACCOUNT) {
      if (!ix.keys[0]?.pubkey.equals(wsol) || !ix.keys[1]?.pubkey.equals(vault)) {
        throw new NotAnExit('a token account closed other than the vault\'s WSOL account into the vault');
      }
      return 'unwrap SOL';
    }
    if (!ix.programId.equals(program)) throw new NotAnExit(`program ${ix.programId.toBase58()} is not ${venue}'s`);
    const rule = rules.find(r => r.discriminator === hex8(ix.data));
    if (!rule) throw new NotAnExit(`${venue} instruction ${hex8(ix.data)} is none of ${EXIT_INSTRUCTIONS[venue].join(', ')}`);
    for (const p of rule.pins) {
      const key = ix.keys[p.index]?.pubkey;
      if (!key) throw new NotAnExit(`${rule.name}: account ${p.index} is missing`);
      const bad = (what) => new NotAnExit(`${rule.name}: account ${p.index} is ${key.toBase58()}, not ${what}`);
      if ((p.role === 'vault' || p.role === 'vaultOrPayer') && !key.equals(vault)) throw bad('the vault');
      if (p.role === 'vaultToken' && !own.has(key.toBase58())) throw bad('a token account of the vault');
      if (p.role === 'pool' && pools && !pools.some(x => new PublicKey(x).equals(key))) throw bad('an approved pool');
    }
    return rule.name;
  });
}

// Rent the venue pays back on a close goes to the vault: the account a
// `vaultOrPayer` pin names (the bot's payer when the bot closes). Pure.
export function rentToVault(venue, ixs, vault) {
  const rules = exitRules(venue);
  const program = new PublicKey(RULES.venues[venue].program);
  return ixs.map(ix => {
    const rule = ix.programId.equals(program) && rules.find(r => r.discriminator === hex8(ix.data));
    const at = rule ? rule.pins.filter(p => p.role === 'vaultOrPayer').map(p => p.index) : [];
    if (!at.length) return ix;
    const keys = ix.keys.map((k, i) => at.includes(i) ? { ...k, pubkey: new PublicKey(vault) } : k);
    return { programId: ix.programId, keys, data: ix.data };
  });
}

// What an SDK built for the vault, sorted for an owner's transaction:
// `creates` (the vault's token accounts, paid by the owner), `inner` (the
// vault's instructions, as built), `unwrap` (it closed the WSOL account).
// Compute budget is dropped. Pure.
export function sortOut(ixs, vault, owner) {
  vault = new PublicKey(vault); owner = new PublicKey(owner);
  const wsol = ata(NATIVE_MINT, vault);
  const out = { creates: [], inner: [], unwrap: false };
  for (const ix of ixs) {
    if (ix.programId.equals(ComputeBudgetProgram.programId)) continue;
    if (ix.programId.equals(ATA_PROGRAM)) {
      const [, account, wallet, mint, , program] = ix.keys.map(k => k.pubkey);
      if (!wallet?.equals(vault) || !account.equals(ata(mint, vault, program))) {
        throw new NotAnExit(`a token account ${account?.toBase58()} that is not the vault's own`);
      }
      out.creates.push(spl.createAssociatedTokenAccountIdempotentInstruction(owner, account, vault, mint, program));
      continue;
    }
    if (ix.programId.equals(TOKEN) && ix.data[0] === CLOSE_ACCOUNT && ix.keys[0]?.pubkey.equals(wsol)) { out.unwrap = true; continue; }
    out.inner.push(ix);
  }
  return out;
}

// One owner message: the creates, then one call running `inner` as the vault.
function message(outside, inner, { settings, vault, owner }) {
  const ixs = inner.length
    ? [ComputeBudgetProgram.setComputeUnitLimit({ units: EXIT_UNITS }), ...outside,
      sq.ownerExecuteIx({ settings, signers: [owner], vault, ixs: inner })]
    : outside;
  return Buffer.from(new TransactionMessage({ payerKey: owner, recentBlockhash: PublicKey.default.toBase58(), instructions: ixs })
    .compileToLegacyMessage().serialize());
}

// Steps ({outside: ix} or {inside: ix}), in order, packed into as few owner
// messages as fit a signed transaction each (TX_BYTES). A message runs its
// outside steps before its call. Returns base64 legacy messages; throws when
// one step alone does not fit.
export function packMessages(steps, { settings, vault, owner, limit = TX_BYTES }) {
  const ctx = { settings: new PublicKey(settings), vault: new PublicKey(vault), owner: new PublicKey(owner) };
  const build = (group) => message(group.filter(s => s.outside).map(s => s.outside), group.filter(s => s.inside).map(s => s.inside), ctx);
  const fits = (group) => build(group).length + SIGNED_OVERHEAD <= limit;
  const groups = [];
  let current = [];
  for (const step of steps) {
    if (fits([...current, step])) { current.push(step); continue; }
    if (!current.length || !fits([step])) throw new Error(`one instruction of the exit does not fit a ${limit}-byte transaction`);
    groups.push(current);
    current = [step];
  }
  if (current.length) groups.push(current);
  return groups.map(g => build(g).toString('base64'));
}

// Each minimum of a CLMM close: what `liquidity` between the two ticks
// returns at the pool's price moved `bps` against that token (as the Raydium
// signer's close). Raydium's math; Byreal's is the same.
function clmmMinimums({ sqrtPriceX64, tickLower, tickUpper, liquidity, bps }) {
  const { TickUtil, LiquidityMathUtil } = require('@raydium-io/raydium-sdk-v2');
  const BN = require('bn.js');
  const S = 1_000_000_000_000;
  const f = new BN(Math.round(Math.sqrt(1 + bps / 1e4) * S));
  const p = new BN(sqrtPriceX64.toString()), L = new BN(liquidity.toString());
  const lo = TickUtil.getSqrtPriceAtTick(tickLower), hi = TickUtil.getSqrtPriceAtTick(tickUpper);
  const up = LiquidityMathUtil.getAmountsForLiquidity(p.mul(f).div(new BN(S)), lo, hi, L, false);
  const dn = LiquidityMathUtil.getAmountsForLiquidity(p.mul(new BN(S)).div(f), lo, hi, L, false);
  return { minA: up.amountA.toString(), minB: dn.amountB.toString() };
}

// --- venues: the vault's positions and each one's close, as the SDK builds it
// Each <venue>Exit(connection, {vault, pools, priceBps, rpc}) returns
// {positions: [{pool, position, nftMint?, liquidity, build}], mints: [{mint,
// program}]}: the vault's positions, each with build() giving its close as the
// SDK builds it, and the tokens the closes pay into the vault.

// Each mint with the token program that owns it.
async function mintPrograms(connection, mints) {
  const infos = await connection.getMultipleAccountsInfo(mints.map(m => new PublicKey(m)));
  return mints.map((m, i) => {
    if (!TOKEN_PROGRAMS.some(t => infos[i]?.owner.equals(t))) throw new Error(`mint ${m} is not a token mint`);
    return { mint: new PublicKey(m), program: infos[i].owner };
  });
}

async function dlmmExit(connection, { vault, pools }) {
  const pkg = require('@meteora-ag/dlmm');
  const DLMM = pkg.default ?? pkg;
  const { BN } = require('@coral-xyz/anchor');
  const positions = [], mints = [];
  for (const pool of pools) {
    const dlmm = await DLMM.create(connection, new PublicKey(pool));
    mints.push({ mint: dlmm.tokenX.publicKey, program: dlmm.tokenX.owner }, { mint: dlmm.tokenY.publicKey, program: dlmm.tokenY.owner });
    dlmm.lbPair.rewardInfos.forEach((r, i) => {
      if (!r.mint.equals(PublicKey.default)) mints.push({ mint: r.mint, program: dlmm.rewards[i].owner });
    });
    const { userPositions } = await dlmm.getPositionsByUserAndLbPair(vault);
    for (const p of userPositions) {
      const d = p.positionData;
      positions.push({ pool, position: p.publicKey.toBase58(),
        liquidity: d.positionBinData.reduce((n, b) => n + BigInt(String(b.positionLiquidity).split('.')[0]), 0n).toString(),
        build: async () => (await dlmm.removeLiquidity({ user: vault, position: p.publicKey, fromBinId: d.lowerBinId, toBinId: d.upperBinId,
          bps: new BN(10000), shouldClaimAndClose: true })).flatMap(tx => tx.instructions) });
    }
  }
  return { positions, mints };
}

async function raydiumExit(connection, { vault, pools, priceBps }) {
  const { Raydium, getPdaPersonalPositionAddress } = require('@raydium-io/raydium-sdk-v2');
  const BN = require('bn.js');
  const vaultIx = await import('../../../venues/raydium_clmm/vault.mjs');
  const programId = new PublicKey(RULES.venues['raydium-clmm'].program);
  const raydium = await Raydium.load({ connection, owner: vault, cluster: 'mainnet', disableFeatureCheck: true, disableLoadToken: true });
  const all = await raydium.clmm.getOwnerPositionInfo({ programId });
  const positions = [], mints = [];
  for (const pool of pools) {
    const r = await raydium.clmm.getPoolInfoFromRpc(pool);
    mints.push(...[r.poolInfo.mintA, r.poolInfo.mintB, ...(r.poolKeys.rewardInfos ?? []).map(x => x.mint)]
      .map(m => ({ mint: new PublicKey(m.address), program: new PublicKey(m.programId) })));
    for (const p of all.filter(x => x.poolId.toBase58() === pool)) {
      positions.push({ pool, position: getPdaPersonalPositionAddress(programId, p.nftMint).publicKey.toBase58(),
        nftMint: p.nftMint.toBase58(), liquidity: p.liquidity.toString(),
        build: async () => {
          const m = clmmMinimums({ sqrtPriceX64: r.rpcPoolInfo.sqrtPriceX64, tickLower: p.tickLower, tickUpper: p.tickUpper,
            liquidity: p.liquidity, bps: priceBps });
          return vaultIx.decreaseIxs({ poolInfo: r.poolInfo, poolKeys: r.poolKeys, position: p, owner: vault, liquidity: p.liquidity,
            amountMinA: new BN(m.minA), amountMinB: new BN(m.minB), nft2022: await vaultIx.isNft2022(connection, p.nftMint), close: true });
        } });
    }
  }
  return { positions, mints };
}

async function byrealExit(connection, { vault, pools, priceBps }) {
  const sdk = await import('@byreal-io/byreal-clmm-sdk');
  const { Chain, Instruction, getPdaPersonalPositionAddress } = sdk;
  const BN = createRequire(require.resolve('@byreal-io/byreal-clmm-sdk/package.json'))('bn.js');
  const { bitmapFirst } = await import('../../../venues/raydium_clmm/vault.mjs');
  const programId = new PublicKey(RULES.venues.byreal.program);
  const chain = new Chain({ connection, programId });
  const all = await chain.getRawPositionInfoListByUserAddress(vault);
  const positions = [], mints = [];
  for (const pool of pools) {
    const poolInfo = await chain.getRawPoolInfoByPoolId(pool);
    const tokens = await mintPrograms(connection, [poolInfo.mintA, poolInfo.mintB]);
    mints.push(...tokens);
    for (const raw of all.filter(x => x.poolId.toBase58() === pool)) {
      positions.push({ pool, position: getPdaPersonalPositionAddress(programId, raw.nftMint).publicKey.toBase58(),
        nftMint: raw.nftMint.toBase58(), liquidity: raw.liquidity.toString(),
        build: async () => {
          const m = clmmMinimums({ sqrtPriceX64: poolInfo.sqrtPriceX64, tickLower: raw.tickLower, tickUpper: raw.tickUpper,
            liquidity: raw.liquidity, bps: priceBps });
          const { instructions } = await Instruction.decreaseLiquidityInstructions({ poolInfo, ownerPosition: raw,
            ownerInfo: { wallet: vault, tokenAccountA: ata(tokens[0].mint, vault, tokens[0].program),
              tokenAccountB: ata(tokens[1].mint, vault, tokens[1].program) },
            liquidity: raw.liquidity, amountMinA: new BN(m.minA), amountMinB: new BN(m.minB) });
          const close = await Instruction.closePositionInstruction({ programId, nftMint: raw.nftMint, ownerWallet: vault });
          // the vault's token accounts are created as Raydium's builders create them (sortOut makes them the owner's to pay)
          const creates = tokens.map(t => spl.createAssociatedTokenAccountIdempotentInstruction(vault, ata(t.mint, vault, t.program), vault, t.mint, t.program));
          return [...creates, ...instructions.map(ix => bitmapFirst(ix, programId, poolInfo.poolId)), ...close.instructions];
        } });
    }
  }
  return { positions, mints };
}

async function orcaExit(connection, { vault, pools, priceBps, rpc }) {
  const { closePositionInstructions, fetchPositionsForOwner, setNativeMintWrappingStrategy } = await import('@orca-so/whirlpools');
  const { fetchWhirlpool } = await import('@orca-so/whirlpools-client');
  const { address, createNoopSigner, createSolanaRpc } = await import('@solana/kit');
  const { kitToWeb3 } = await import('../../../venues/orca/swap.mjs');
  rpc ??= createSolanaRpc(connection.rpcEndpoint);
  setNativeMintWrappingStrategy('ata');                // as the Orca signer: SOL through the vault's WSOL ATA
  const authority = createNoopSigner(address(vault.toBase58()));
  const all = await fetchPositionsForOwner(rpc, address(vault.toBase58()));
  const positions = [], mints = [];
  for (const pool of pools) {
    const w = (await fetchWhirlpool(rpc, address(pool))).data;
    mints.push(...await mintPrograms(connection, [w.tokenMintA, w.tokenMintB,
      ...w.rewardInfos.map(r => r.mint).filter(m => String(m) !== PublicKey.default.toBase58())].map(String)));
    for (const p of all.filter(x => !x.isPositionBundle && String(x.data.whirlpool) === pool)) {
      positions.push({ pool, position: String(p.address), nftMint: String(p.data.positionMint), liquidity: String(p.data.liquidity),
        build: async () => (await closePositionInstructions(rpc, p.data.positionMint, { slippageToleranceBps: priceBps, authority }))
          .instructions.map(kitToWeb3) });
    }
  }
  return { positions, mints };
}

const VENUES = { 'meteora-dlmm': { open: dlmmExit }, 'raydium-clmm': { open: raydiumExit }, byreal: { open: byrealExit }, orca: { open: orcaExit } };

function venueExit(venue) {
  if (!VENUES[venue]) throw new NotAnExit(`venue ${venue} has no owner exit (have: ${Object.keys(VENUES).join(', ')})`);
  return VENUES[venue];
}

// A position as the caller sees it: strings, no builder.
function positionView(venue, { pool, position, nftMint, liquidity }) {
  return { venue, pool, position, ...(nftMint ? { nftMint } : {}), liquidity };
}

// The vault's open positions on `pools` of `venue`, found as the venue's
// signer finds the bot's: [{venue, pool, position, nftMint?, liquidity}].
// `rpc`: a @solana/kit RPC for Orca (default: one on the connection's endpoint).
export async function vaultPositions(connection, { venue, pools, vault, rpc = null }) {
  const v = await venueExit(venue).open(connection, { vault: new PublicKey(vault), pools: pools.map(String), rpc, priceBps: EXIT_PRICE_BPS });
  return v.positions.map(p => positionView(venue, p));
}

// An owner's messages that close every position the vault holds on `pools`
// of `venue` into the vault: {summary, positions, messages}, `messages`
// base64 legacy messages (placeholder blockhash, fee payer `owner`) to sign
// and send in order. Throws NotAnExit before building anything an exit
// should not run. `builder` replaces the venue's SDK side ({open}, as in
// VENUES) in tests.
export async function ownerCloseMessages(connection, { settings, vault, owner, venue, pools, rpc = null, priceBps = EXIT_PRICE_BPS,
  builder = null }) {
  [settings, vault, owner] = [settings, vault, owner].map(k => new PublicKey(k));
  pools = pools.map(String);
  const v = await (builder ?? venueExit(venue)).open(connection, { vault, pools, rpc, priceBps });
  const creates = new Map(), inner = [];
  let unwrap = v.mints.some(m => new PublicKey(m.mint).equals(NATIVE_MINT));
  for (const p of v.positions) {
    const sorted = sortOut(await p.build(), vault, owner);
    for (const c of sorted.creates) creates.set(c.keys[1].pubkey.toBase58(), c);
    inner.push(...rentToVault(venue, sorted.inner, vault));
    unwrap ||= sorted.unwrap;
  }
  if (!v.positions.length) unwrap = false;
  if (unwrap) {
    const wsol = ata(NATIVE_MINT, vault);
    // the close needs the account to exist, whatever the venue touched
    if (!creates.has(wsol.toBase58())) creates.set(wsol.toBase58(), spl.createAssociatedTokenAccountIdempotentInstruction(owner, wsol, vault, NATIVE_MINT));
    inner.push(spl.createCloseAccountInstruction(wsol, vault, vault));
  }
  checkExitIxs(venue, inner, { vault, tokenAccounts: v.mints.map(m => ata(m.mint, vault, m.program)), pools });
  const messages = packMessages([...[...creates.values()].map(ix => ({ outside: ix })), ...inner.map(ix => ({ inside: ix }))],
    { settings, vault, owner });
  const positions = v.positions.map(p => positionView(venue, p));
  const summary = positions.length
    ? `close ${positions.length} ${venue} position${positions.length > 1 ? 's' : ''} of the vault ${vault.toBase58()} into the vault `
      + `(liquidity, fees and rewards to the vault's token accounts, rent to the vault): ${messages.length} transaction`
      + `${messages.length > 1 ? 's' : ''}, sign and send in order`
    : `the vault ${vault.toBase58()} holds no ${venue} position on ${pools.join(', ')}`;
  return { summary, positions, messages };
}
