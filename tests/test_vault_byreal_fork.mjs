// A Byreal position run by a vault, end to end, on a local fork of mainnet:
// the deployed Squads and Byreal programs, the live SOL/USDC pool. Byreal is
// a Raydium CLMM fork: the vault rules are Raydium's ("like"), against
// Byreal's program, with the pool's reward count (none).
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'module';
import { connectedVault } from './_vault.mjs';
import * as sq from '../chains/solana/vault/squads.mjs';
import { actingAccount } from '../chains/solana/vault/account.mjs';
import { around, ata, bitmapFirst, ensureAtas } from '../venues/raydium_clmm/vault.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { PublicKey, Keypair } = require('@solana/web3.js');
const spl = require('@solana/spl-token');
const sdk = await import('@byreal-io/byreal-clmm-sdk');
const { Chain, Instruction, getPdaExBitmapAccount } = sdk;
const BN = createRequire(require.resolve('@byreal-io/byreal-clmm-sdk/package.json'))('bn.js');

const POOL = '9GTj99g9tbz9U6UYDsX6YeRTgUnkYG6GTnHv3qLa5aXq';      // SOL/USDC, no rewards
const OTHER = '2kdeJHNGAChkUGPTWe2FR2ah5bcJrhoynYNccVxV1GCN';     // SOL/USDC 1%, not approved
const USDC = new PublicKey(SOLANA.usdc_mint);
const SOL = spl.NATIVE_MINT;
const PROGRAM = new PublicKey(SOLANA.programs.byreal_clmm);

async function setup() {
  const v = await connectedVault({ venues: ['byreal'], pools: [POOL] });
  const { fork, connection, env, payer } = v;
  const vault = new PublicKey(v.out.vault);
  fork.fund(vault, 2);
  await fork.clone([USDC]);
  fork.tokenAccount(spl.getAssociatedTokenAddressSync(USDC, vault, true), USDC, vault, 300_000_000n);
  const acting = await actingAccount({ connection, payer, venue: 'byreal', env });
  const chain = new Chain({ connection, programId: PROGRAM });
  // The signer sends nothing a simulation refused; the fork declines
  // simulations (tests/_svm.mjs), so here every one passes and the send,
  // run in full by the programs, is the verdict.
  const passing = Object.create(connection, { simulateTransaction: { value: async () => ({ value: { err: null, logs: [], unitsConsumed: 0 } }) } });
  const deps = { urls: ['http://fork.test'], connectFn: async () => ({ connection: passing, payer, chain, acting }), sleep: async () => {} };
  const signer = await import('../venues/byreal/signer.mjs');
  process.env.LPBOT_POOL = POOL;
  process.env.LPBOT_MAX_USD = '100000';
  delete process.env.LPBOT_TOKEN_HINTS;
  return { ...v, vault, acting, chain, deps, signer };
}

async function said(fn) {
  const lines = [], log = console.log;
  console.log = (x) => lines.push(x);
  try { await fn(); } finally { console.log = log; }
  return JSON.parse(lines.find(x => typeof x === 'string' && x.startsWith('{')) ?? 'null');
}

const usdcOf = (fork, owner) => fork.tokenAmount(spl.getAssociatedTokenAddressSync(USDC, owner, true)) ?? 0n;
const tokens = [{ mint: SOL.toBase58(), program: spl.TOKEN_PROGRAM_ID }, { mint: USDC.toBase58(), program: spl.TOKEN_PROGRAM_ID }];

// An exact-in swap by `who` (a key wallet) on `pool`, as the SDK builds it.
async function swapIxs(chain, pool, who, mint, amount) {
  const poolInfo = await chain.getRawPoolInfoByPoolId(pool);
  const q = await chain.qouteSwap({ poolInfo, inputTokenMint: mint, amountIn: new BN(amount.toString()), slippage: 0.5, catchLiquidityInsufficient: true });
  const { instructions } = await Instruction.swapBaseInInstruction({ poolInfo,
    ownerInfo: { wallet: who, tokenAccountA: ata(SOL, who), tokenAccountB: ata(USDC, who) },
    amount: q.amountIn, otherAmountThreshold: new BN(0), sqrtPriceLimitX64: q.executionPrice, isInputMintA: q.isInputMintA,
    tickArray: q.remainingAccounts, exTickArrayBitmap: getPdaExBitmapAccount(PROGRAM, poolInfo.poolId).publicKey });
  return around(who, tokens, instructions, { wrap: mint.equals(SOL) ? amount : 0 });
}

// The pool takes swaps only through swap_v3_dyn, which the SDK does not
// build, so no trade can earn the position fees here: the fork's copy of the
// position is given fees owed (PersonalPositionLayout tokenFeesOwedA/B), which
// the harvest's decrease pays out of the pool's vaults as the program would.
const FEES_OWED_AT = 129;

test('the Byreal signer runs a vault: open, harvest, close', { timeout: 600_000 }, async () => {
  const { fork, vault, payer, deps, signer } = await setup();
  const b = await said(() => signer.balance(undefined, deps));
  assert.equal(b.owner, vault.toBase58());
  assert.equal(b.custody, 'vault');
  assert.equal(b.balanceB, 300);
  const payerBefore = fork.balance(payer.publicKey), vaultSolBefore = fork.balance(vault);

  const opened = await said(() => signer.open(POOL, String(b.price * 0.99), String(b.price * 1.01), '0.5', '100', true, deps));
  assert.equal(opened.sent, true, JSON.stringify(opened));
  const st = await said(() => signer.status(opened.positionMint, deps));
  assert.equal(st.positionMint, opened.positionMint);
  assert.ok(st.inRange && st.positionUsd > 50, JSON.stringify(st));
  assert.ok(fork.balance(vault) < vaultSolBefore - 0.1e9, 'the SOL side came from the vault');

  const position = PublicKey.findProgramAddressSync([Buffer.from('position'), new PublicKey(opened.positionMint).toBuffer()], PROGRAM)[0];
  const acct = fork.get(position);
  acct.data.writeBigUInt64LE(1_000_000n, FEES_OWED_AT);              // 0.001 SOL
  acct.data.writeBigUInt64LE(2_000_000n, FEES_OWED_AT + 8);          // 2 USDC
  fork.set(position, acct);
  const usdcPre = usdcOf(fork, vault);
  const h = await said(() => signer.harvest(opened.positionMint, true, deps));
  assert.equal(h.harvested, opened.positionMint, JSON.stringify(h));
  assert.equal(usdcOf(fork, vault) - usdcPre, 2_000_000n, 'the fees came into the vault');

  const c = await said(() => signer.close(opened.positionMint, true, deps));
  assert.equal(c.closed, opened.positionMint, JSON.stringify(c));
  assert.equal((await said(() => signer.status(undefined, deps))).positions, 0);
  // the vault paid the position's rent and got it back; the payer paid fees only
  assert.ok(payerBefore - fork.balance(payer.publicKey) < 0.0005e9, `payer spent ${(payerBefore - fork.balance(payer.publicKey)) / 1e9} SOL`);
  assert.equal(fork.get(ata(SOL, vault)), null, 'no WSOL left wrapped');
  assert.ok(fork.balance(vault) > vaultSolBefore - 0.01e9, 'the SOL came back to the vault');
});

test('Byreal: another pool, a swap, or tokens to anyone but the vault, are refused', { timeout: 600_000 }, async () => {
  const { fork, vault, acting, policyKey, chain, deps, signer } = await setup();
  const force = async (ixs, name) => {
    const p = acting.compiled.policies.findIndex(x => x.index.some(e => e.name === name));
    const c = acting.compiled.policies[p].index.findIndex(e => e.name === name);
    const call = sq.policyExecuteIx({ policy: acting.policies[p], signer: policyKey.publicKey, vault, ixs, indices: ixs.map(() => c) });
    return fork.sendSigned(acting.transaction([call], fork.svm.latestBlockhash(), [policyKey]));
  };
  const b = await said(() => signer.balance(undefined, deps));
  const opened = await said(() => signer.open(POOL, String(b.price * 0.99), String(b.price * 1.01), '0.2', '40', true, deps));
  assert.equal(opened.sent, true, JSON.stringify(opened));
  const poolInfo = await chain.getRawPoolInfoByPoolId(POOL);
  const [raw] = await chain.getRawPositionInfoListByUserAddress(vault);
  const thief = Keypair.generate().publicKey;
  const decrease = async (who) => (await Instruction.decreaseLiquidityInstructions({ poolInfo, ownerPosition: raw,
    ownerInfo: { wallet: vault, tokenAccountA: ata(SOL, who), tokenAccountB: ata(USDC, who) },
    liquidity: raw.liquidity, amountMinA: new BN(0), amountMinB: new BN(0) })).instructions
    .map(ix => bitmapFirst(ix, PROGRAM, poolInfo.poolId));
  // the vault's own accounts exist, so each refusal is the thief's account
  assert.ok((await fork.sendSigned(acting.transaction(ensureAtas(vault, tokens), fork.svm.latestBlockhash()))).ok);
  for (const m of [SOL, USDC]) fork.tokenAccount(ata(m, thief), m, thief, 0n);
  const [stolen] = await decrease(thief);
  const res = await force([stolen], 'decrease_liquidity_v2');
  assert.ok(!res.ok);
  assert.match(res.logs.join('\n'), /ProgramInteractionInvalidNumericValue/);
  // the vault's own accounts pass the same rule: the refusal was the thief's account
  const [mine] = await decrease(vault);
  const ok = await force([mine], 'decrease_liquidity_v2');
  assert.ok(ok.ok, ok.logs.join('\n'));
  // swaps are not in Byreal's rules, on this pool or another
  for (const pool of [POOL, OTHER]) {
    const [swap] = (await swapIxs(chain, pool, vault, SOL, 10_000_000n)).filter(ix => ix.programId.equals(PROGRAM));
    assert.throws(() => acting.wrap([swap]), /not in the byreal vault policy/);
    const forced = await force([swap], 'decrease_liquidity_v2');
    assert.ok(!forced.ok);
  }
  assert.equal(fork.tokenAmount(ata(USDC, thief)), 0n);
});
