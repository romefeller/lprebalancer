// One vault for every venue the sol-usdc strategy may pick (execute_dexes),
// connected by the operator's tool on a fork of mainnet: each venue's
// policies, one lookup table, and a pool that does not fit its venue's rules
// refused before anything is signed.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'module';
import { Fork } from './_svm.mjs';
import { connectedVault, POOL as DLMM_POOL } from './_vault.mjs';
import { actingAccount } from '../chains/solana/vault/account.mjs';
import { compile } from '../chains/solana/vault/policy.mjs';
import { poolsByVenue, setupRequest } from '../chains/solana/vault/cli.mjs';
import { venue } from '../chains/solana/vault/venues.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { PublicKey, AddressLookupTableAccount } = require('@solana/web3.js');
const { PoolInfoLayout } = require('@raydium-io/raydium-sdk-v2');
const { getAssociatedTokenAddressSync } = require('@solana/spl-token');

const POOLS = {
  'meteora-dlmm': DLMM_POOL,
  'raydium-clmm': '3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv',
  orca: 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE',
  byreal: '9GTj99g9tbz9U6UYDsX6YeRTgUnkYG6GTnHv3qLa5aXq',
};

test('one owner set-up approves all four venues; each signs only through its own policies', { timeout: 600_000 }, async () => {
  const venues = Object.keys(POOLS);
  // the pools in another order than the venues: each is placed by its program
  const { fork, connection, out, envFor, payer } = await connectedVault({ venues, pools: Object.values(POOLS).reverse() });
  assert.equal(out.sent, true);
  for (const v of venues) {
    assert.deepEqual(out.venues[v].pools, [POOLS[v]]);
    const acting = await actingAccount({ connection, payer, venue: v, env: envFor(v) });
    assert.equal(acting.owner.toBase58(), out.vault);
    assert.equal(acting.policies.length, compile(v, { vault: acting.vault, payer: payer.publicKey, pools: [POOLS[v]], payouts: out.payouts }).policies.length);
  }
  // no policy serves two venues; a venue's config with another's policies is refused
  const all = venues.flatMap(v => out.venues[v].policies);
  assert.equal(new Set(all).size, all.length);
  await assert.rejects(actingAccount({ connection, payer, venue: 'orca', env: { ...envFor('orca'), LPBOT_VAULT_POLICIES: out.venues['raydium-clmm'].policies.join(',') } }),
    /differs from the orca rules|compile to/);
  const table = AddressLookupTableAccount.deserialize(fork.get(out.lookup).data);
  assert.equal(table.addresses.length, out.lookupAddresses);
  for (const v of venues) assert.ok(table.addresses.some(a => a.toBase58() === POOLS[v]), `${v}'s pool in the lookup table`);
});

test('set-up: no owner, no venue or no pool is refused before anything is built', { timeout: 300_000 }, async () => {
  const { connection, owner, payer, policyKey } = await connectedVault();
  const me = owner.publicKey.toBase58(), keys = { connection, payer: payer.publicKey, policyKey: policyKey.publicKey };
  await assert.rejects(setupRequest({ owner: me, venue: 'orca' }, keys), /setup needs --venue and --pools/);
  await assert.rejects(setupRequest({ owner: me, pools: POOLS.orca }, keys), /setup needs --venue and --pools/);
  await assert.rejects(setupRequest({ owner: me, venue: ' , ', pools: POOLS.orca }, keys), /setup needs --venue and --pools/);
  await assert.rejects(setupRequest({ venue: 'orca', pools: POOLS.orca }, keys), /^Error: setup needs --owner <the wallet that owns the vault>$/);
  await assert.rejects(setupRequest({ owner: policyKey.publicKey.toBase58(), venue: 'orca', pools: POOLS.orca }, keys), /cannot own the vault/);
  // an empty --payout-mint: USDC, as with none
  const plan = await setupRequest({ owner: me, venue: 'orca', pools: POOLS.orca, payout: me, 'payout-mint': '' }, keys);
  assert.deepEqual(plan.report.payouts, [String(getAssociatedTokenAddressSync(new PublicKey(SOLANA.usdc_mint), owner.publicKey, true))]);
  assert.deepEqual(plan.report.owners, [me]);
});

test('a pool no listed venue owns, or a venue without a pool, is refused', { timeout: 300_000 }, async () => {
  const fork = new Fork();
  const connection = fork.connection();
  const keys = (list) => list.map(p => new PublicKey(p));
  await assert.rejects(poolsByVenue(connection, ['orca'], keys([POOLS.orca, POOLS['raydium-clmm']])), /belongs to none of orca/);
  await assert.rejects(poolsByVenue(connection, ['orca', 'byreal'], keys([POOLS.orca])), /no pool for byreal/);
  const got = await poolsByVenue(connection, ['byreal', 'orca'], keys([POOLS.orca, POOLS.byreal]));
  assert.deepEqual(Object.fromEntries(Object.entries(got).map(([v, ps]) => [v, ps.map(String)])), { byreal: [POOLS.byreal], orca: [POOLS.orca] });
});

// The decrease rule pins one recipient per reward the venue's pools have. A
// pool with another count could pay a reward the rule does not pin.
test('a Raydium-layout pool with another reward count than its venue\'s rules is refused', { timeout: 300_000 }, async () => {
  const fork = new Fork();
  const connection = fork.connection();
  const pool = new PublicKey(POOLS['raydium-clmm']);
  const a = await connection.getAccountInfo(pool);
  assert.ok(await venue('raydium-clmm').poolAccounts(connection, pool));
  // the same pool with its one reward slot emptied, then with a second one filled
  for (const rewards of [0, 2]) {
    const p = PoolInfoLayout.decode(a.data);
    p.rewardInfos[0].mint = rewards ? p.rewardInfos[0].mint : PublicKey.default;
    if (rewards === 2) p.rewardInfos[1] = { ...p.rewardInfos[0] };
    const data = Buffer.alloc(a.data.length);
    PoolInfoLayout.encode(p, data);
    fork.set(pool, { ...a, data });
    await assert.rejects(venue('raydium-clmm').poolAccounts(connection, pool), new RegExp(`has ${rewards} rewards; the vault rules pin 1`));
  }
  await assert.rejects(venue('byreal').poolAccounts(connection, pool), /is not a byreal pool/);
  await assert.rejects(venue('orca').poolAccounts(connection, pool), /is not an Orca whirlpool/);
});

test('a whirlpool\'s accounts: the pool, its vaults and mints, its oracle', { timeout: 300_000 }, async () => {
  const connection = new Fork().connection();
  const { getWhirlpoolDecoder, getOracleAddress } = await import('@orca-so/whirlpools-client');
  const pool = new PublicKey(POOLS.orca);
  const w = getWhirlpoolDecoder().decode((await connection.getAccountInfo(pool)).data);
  const got = await venue('orca').poolAccounts(connection, pool);
  const want = [POOLS.orca, w.tokenVaultA, w.tokenVaultB, w.tokenMintA, w.tokenMintB, (await getOracleAddress(POOLS.orca))[0]].map(String);
  assert.deepEqual(got.accounts.slice(0, 6).map(String), want);
  assert.deepEqual(got.mints.slice(0, 2).map(String), [w.tokenMintA, w.tokenMintB].map(String));
  const rewards = w.rewardInfos.filter(r => String(r.mint) !== PublicKey.default.toBase58());
  assert.deepEqual(got.accounts.slice(6).map(String), rewards.map(r => String(r.vault)));
  assert.deepEqual(got.mints.slice(2).map(String), rewards.map(r => String(r.mint)));
});
