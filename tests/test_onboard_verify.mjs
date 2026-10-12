// onboard/verify.mjs: the owner's check outside the public server. The rules
// and pins come from a git commit; the transaction from the page's copy (or
// the server); the vault from the chain. The same decoder as the page.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'module';
import * as sq from '../chains/solana/vault/squads.mjs';
import { compile } from '../chains/solana/vault/policy.mjs';
import * as POLYGON from '../chains/evm/polygon.mjs';
import { ata } from '../onboard/decode_sol.mjs';
import { atCommit, chainProblems, check, flags, main, pinsOf } from '../onboard/verify.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { Keypair, PublicKey, Transaction } = require('@solana/web3.js');

const read = (f) => fs.readFileSync(new URL(`../${f}`, import.meta.url), 'utf8');
const ONBOARD = JSON.parse(read('onboard/onboard.json'));
const PINS = pinsOf(read('chains/solana/vault/rules.json'), read('chains/solana/solana.json'), read('onboard/onboard.json'), POLYGON);
const OWNER = Keypair.generate().publicKey, SETTINGS = Keypair.generate().publicKey, VAULT = sq.vaultPda(SETTINGS);
const VENUE = 'raydium-clmm', POOL = SOLANA.sol_usdc_pools[VENUE];
const USDC = new PublicKey(SOLANA.usdc_mint), PAYER = new PublicKey(ONBOARD.chains.solana.botKeys[0]);
const payoutWallets = [OWNER.toBase58(), SOLANA.operator_profit_wallet];
const compiled = compile(VENUE, { vault: VAULT, payer: PAYER, pools: [new PublicKey(POOL)], payouts: payoutWallets.map(w => new PublicKey(ata(w, USDC))) }).policies;
const POLICIES = compiled.map(() => Keypair.generate().publicKey.toBase58());
const expect = { kind: 'approve', owner: OWNER.toBase58(), settings: SETTINGS.toBase58(), vault: VAULT.toBase58(), policies: { [VENUE]: POLICIES },
  pools: { [VENUE]: [POOL] }, payoutWallets };

function approveTx(i, signer = new PublicKey(ONBOARD.chains.solana.botKeys[1]), policy = compiled[i].policy) {
  const tx = new Transaction().add(sq.settingsSyncIx({ settings: SETTINGS, signers: [OWNER], rentPayer: OWNER, extra: [new PublicKey(POLICIES[i])],
    actions: [{ kind: 'policyUpdate', address: new PublicKey(POLICIES[i]), policy, signers: [{ key: signer }] }] }));
  tx.feePayer = OWNER; tx.recentBlockhash = PublicKey.default.toBase58();
  return tx.serialize({ requireAllSignatures: false, verifySignatures: false }).toString('base64');
}

test('the pins of a commit: its rules (hashed), pools, the bot\'s keys and the operator', () => {
  assert.match(PINS.rulesSha256, /^[0-9a-f]{64}$/);
  assert.equal(PINS.solana.payer, ONBOARD.chains.solana.botKeys[0]);
  assert.equal(PINS.solana.policyKey, ONBOARD.chains.solana.botKeys[1]);
  assert.deepEqual(PINS.solana.pools[VENUE], [POOL]);
  assert.equal(PINS.polygon.botKey, ONBOARD.chains.polygon.botKeys[0]);
  assert.equal(PINS.polygon.npm, POLYGON.V3.npm);
  // git show, for a commit id only
  assert.equal(atCommit('abc1234', 'x', (args) => args.join(' ')), 'show abc1234:x');
  for (const bad of ['HEAD', 'main', '--output=x', 'abc', '']) assert.throws(() => atCommit(bad, 'x', () => ''), /commit id/);
  assert.deepEqual(flags(['--a', '1', '--b', '2']), { a: '1', b: '2' });
  assert.throws(() => flags(['--a']), /pairs/);
});

test('an approval is checked against the commit\'s rules: the right one passes, another key or rule does not', () => {
  for (let i = 0; i < POLICIES.length; i++) assert.equal(check('solana', [approveTx(i)], () => expect, PINS)[0].ok, true, `policy ${i}`);
  const r = check('solana', [approveTx(0, Keypair.generate().publicKey)], () => expect, PINS)[0];
  assert.equal(r.ok, false);
  assert.match(r.problems.join(), /not the bot's rules key/);
  const last = POLICIES.length - 1;
  const other = { ...PINS, solana: { ...PINS.solana, rules: { ...PINS.solana.rules, common: PINS.solana.rules.common.slice(1) } } };
  assert.equal(check('solana', [approveTx(last)], () => expect, other)[0].ok, false, 'the rules of another commit differ');
});

test('the vault on chain: the owner alone, the policies its own', async () => {
  // a connection that serves the settings and policy accounts a real vault has, built by the program's own layout
  const head = (s) => {
    const parts = [Buffer.from(require('node:crypto').createHash('sha256').update('account:Policy').digest().subarray(0, 8)), s.toBuffer(),
      Buffer.alloc(8), Buffer.from([255]), Buffer.alloc(16), Buffer.from([0, 0, 0, 0]), Buffer.alloc(6)];
    return Buffer.concat(parts);
  };
  const settingsData = (owners, threshold) => {
    const w = [Buffer.from(require('node:crypto').createHash('sha256').update('account:Settings').digest().subarray(0, 8)), Buffer.alloc(16), PublicKey.default.toBuffer()];
    const t = Buffer.alloc(2); t.writeUInt16LE(threshold);
    const n = Buffer.alloc(4); n.writeUInt32LE(owners.length);
    w.push(t, Buffer.alloc(4), Buffer.alloc(8), Buffer.alloc(8), Buffer.from([0]), Buffer.alloc(8), Buffer.from([255]), n,
      ...owners.map(o => Buffer.concat([o.toBuffer(), Buffer.from([7])])), Buffer.from([0]), Buffer.from([0]));
    return Buffer.concat(w);
  };
  const conn = (owners, threshold = 1, policyOf = SETTINGS) => ({
    getAccountInfo: async () => ({ data: settingsData(owners, threshold) }),
    getMultipleAccountsInfo: async (ks) => ks.map(() => ({ data: head(policyOf) })),
  });
  assert.deepEqual(await chainProblems(conn([OWNER]), expect), []);
  assert.match((await chainProblems(conn([OWNER, Keypair.generate().publicKey]), expect)).join(), /not your wallet alone/);
  assert.match((await chainProblems(conn([OWNER], 2), expect)).join(), /not your wallet alone/);
  assert.match((await chainProblems(conn([OWNER], 1, Keypair.generate().publicKey), expect)).join(), /belongs to another vault/);
  assert.match((await chainProblems(conn([OWNER]), { ...expect, vault: Keypair.generate().publicKey.toBase58() })).join(), /not the account of these settings/);
  assert.match((await chainProblems({ getAccountInfo: async () => null }, expect)).join(), /does not exist/);
});

test('the command: a pasted transaction at HEAD, OK; a tampered one, refused', async () => {
  const log = console.log, out = [];
  console.log = (x) => out.push(String(x));
  try {
    // the files of a commit, here this checkout's (git show stubbed)
    const git = (args) => read(args[1].split(':')[1]);
    const head = 'abcdef1';
    const paste = (tx) => JSON.stringify({ chain: 'solana', tx, expect });
    assert.equal(await main(['--commit', head, '--rpc', 'none', '--paste', paste(approveTx(0))], { git }), 0, out.join('\n'));
    assert.match(out.join('\n'), /transaction 1 of 1: OK/);
    out.length = 0;
    assert.equal(await main(['--commit', head, '--rpc', 'none', '--paste', paste(approveTx(0, OWNER))], { git }), 1);
    assert.match(out.join('\n'), /REFUSED[\s\S]*do not sign/);
  } finally { console.log = log; }
});
