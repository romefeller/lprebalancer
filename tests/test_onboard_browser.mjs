// The onboarding page in Chromium, every view and flow, on a phone and on a
// desktop: the server (onboard/server.mjs) with a chain and a database kept in
// memory, and a Wallet Standard wallet signing in node. The transactions are
// real Solana transactions built for this test's vault, so the page's decoder
// (onboard/decode_sol.mjs, in the bundle) reads and checks every one; a
// tampered one is refused before the wallet sees it. The same flows on a fork
// of mainnet with the real builders and database: test_onboard_fork.mjs.
//
//   LPBOT_SHOTS=/tmp/shots node --test test_onboard_browser.mjs     # and keep the screenshots
import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { createRequire } from 'module';
import { browser, testBundle, openPage, solanaWallet, shot, flowDone, edSign, PHONE, DESKTOP } from './_browser.mjs';
import { loadConfig, makeServer, StoreError } from '../onboard/server.mjs';
import * as I from '../onboard/intent.mjs';
import * as sq from '../chains/solana/vault/squads.mjs';
import { compile, instructionsOf, RULES } from '../chains/solana/vault/policy.mjs';
import { ata } from '../onboard/decode_sol.mjs';
import { ownerSetupPlan } from '../chains/solana/vault/admin.mjs';
import { setupTxs } from '../onboard/solana.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { Keypair, PublicKey, SystemProgram, Transaction, TransactionInstruction } = require('@solana/web3.js');
const spl = require('@solana/spl-token');
const bs58 = require('bs58').default;

const INVITE = 'browser-test-invite';
const PAYER = Keypair.generate(), POLICY = Keypair.generate(), OWNER = Keypair.generate();
const VENUE = 'raydium-clmm', POOL = SOLANA.sol_usdc_pools[VENUE];
const USDC = new PublicKey(SOLANA.usdc_mint), WSOL = new PublicKey(SOLANA.native_mint);
const OPERATOR = SOLANA.operator_profit_wallet;
const TREASURY = Keypair.generate().publicKey;

// execute_settings_transaction_sync's discriminator
const SYNC = sq.settingsSyncIx({ settings: PublicKey.default, signers: [], rentPayer: PublicKey.default, actions: [] }).data.subarray(0, 8);
const programConfig = () => {
  const disc = crypto.createHash('sha256').update('account:ProgramConfig').digest().subarray(0, 8);
  const n = Buffer.alloc(16); n.writeBigUInt64LE(41n);
  return Buffer.concat([disc, n, Keypair.generate().publicKey.toBuffer(), Buffer.alloc(8), Keypair.generate().publicKey.toBuffer()]);
};
const unsigned = (ixs, payer) => {
  const tx = new Transaction().add(...ixs);
  tx.feePayer = payer; tx.recentBlockhash = PublicKey.default.toBase58();
  return tx.serialize({ requireAllSignatures: false, verifySignatures: false }).toString('base64');
};

// The chain, in memory: one vault, its balances, policies and positions.
class Chain {
  constructor() {
    // the owner's own vault at a seed of the program's: its settings, vault and policies are those addresses
    this.seed = 700_000n + BigInt(Math.floor(Math.random() * 1e6));
    this.settings = sq.settingsPda(this.seed); this.vault = sq.vaultPda(this.settings);
    this.payouts = [OWNER.publicKey.toBase58(), OPERATOR];
    this.compiled = compile(VENUE, { vault: this.vault, payer: PAYER.publicKey, pools: [new PublicKey(POOL)],
      payouts: this.payouts.map(w => new PublicKey(ata(w, USDC))) }).policies;
    this.policies = this.compiled.map((_, i) => sq.policyPda(this.settings, i + 1).toBase58());
    this.sol = 0n; this.usdc = 0n; this.positions = 0; this.signer = POLICY.publicKey.toBase58(); this.sent = []; this.tamper = null;
    this.funded = null;                                    // the set-up's transaction that paid the bot's gas
    this.created = false; this.made = 0;                   // the vault's creation and the policies made, on this chain
    this.others = []; this.held = new Map();               // the signers beside the owner; what each set-up key holds
  }

  // The owner's set-up as solana.mjs setupTxs builds it (the real builder),
  // over this chain: cli.mjs setupRequest's answer from what landed here.
  async setup(a) {
    const chain = this;
    const request = async (f, keys) => {
      const compiled = compile(VENUE, { vault: chain.vault, payer: keys.payer, pools: [new PublicKey(POOL)],
        payouts: f.payout.split(',').map(w => new PublicKey(ata(w, USDC))) });
      const plan = ownerSetupPlan({ owner: f.owner, treasury: TREASURY, seed: chain.seed, policyKey: keys.policyKey, venues: [compiled], memo: 'lp-bot vault',
        setupKey: f['setup-key'] ?? null });
      const done = [...(chain.created ? [0] : []), ...Array.from({ length: chain.made }, (_, i) => i + 1)];
      return { seed: String(chain.seed), settings: chain.settings.toBase58(), vault: chain.vault.toBase58(), done, others: [...chain.others],
        release: plan.release, report: { venues: { [VENUE]: { policies: chain.policies, pools: [POOL] } } },
        steps: plan.steps.filter((_, i) => !done.includes(i)) };
    };
    const conn = { getMinimumBalanceForRentExemption: async (n) => 890_880 + 6960 * n, getLatestBlockhash: async () => ({ blockhash: Keypair.generate().publicKey.toBase58() }),
      getBalance: async (k) => (k.equals(OWNER.publicKey) ? 10e9 : Number(chain.held.get(k.toBase58()) ?? 0n)),
      getAccountInfo: async (k) => (k.equals(sq.programConfigPda()) ? { data: programConfig() } : { owner: spl.TOKEN_PROGRAM_ID }) };
    const out = await setupTxs(conn, { payer: PAYER, policyKey: POLICY }, loadConfig().chains.solana, a, { request, find: async () => chain.funded });
    this.lastSetup = out;
    if (this.tamper === 'setup' && out.fund.length) {
      // the gas to someone else
      const tx = Transaction.from(Buffer.from(out.fund.at(-1), 'base64'));
      const ixs = tx.instructions.map(ix => (ix.programId.equals(SystemProgram.programId) && ix.keys[1].pubkey.equals(PAYER.publicKey)
        ? SystemProgram.transfer({ fromPubkey: ix.keys[0].pubkey, toPubkey: Keypair.generate().publicKey, lamports: ix.data.readBigUInt64LE(4) }) : ix));
      out.fund[out.fund.length - 1] = unsigned(ixs, tx.feePayer);
    }
    return out;
  }

  // the vault whole: created, every policy, the owner alone on it
  whole() { return this.created && this.made === this.compiled.length && !this.others.length; }

  row() { return { settings: this.settings.toBase58(), venues: { [VENUE]: { policies: this.policies, pools: [POOL], payouts: [] } } }; }

  // what a transaction the owner (or the page's set-up key) sent did to this chain
  land(tx) {
    const sig = bs58.encode(tx.signature);
    this.sent.push(tx);
    // a set-up key pays its fee, and each policy's rent (a policy account's bound)
    const by = tx.feePayer?.toBase58();
    if (this.held.has(by)) this.held.set(by, this.held.get(by) - 5000n);
    for (const ix of tx.instructions) {
      const d = Buffer.from(ix.data);
      if (ix.programId.equals(SystemProgram.programId)) {
        const [from, to] = [ix.keys[0].pubkey.toBase58(), ix.keys[1].pubkey.toBase58()], lamports = d.readBigUInt64LE(4);
        if (to === PAYER.publicKey.toBase58()) this.funded = sig;
        if (to === this.vault.toBase58()) this.sol += lamports;
        if (this.held.has(from)) this.held.set(from, this.held.get(from) - lamports);
        if (this.held.has(to) || this.others.includes(to)) this.held.set(to, (this.held.get(to) ?? 0n) + lamports);
      }
      if (ix.programId.equals(spl.TOKEN_PROGRAM_ID) && d[0] === 12) this.usdc += d.readBigUInt64LE(1);
      if (ix.programId.equals(sq.PROGRAM_ID)) this.squads(ix, tx);
    }
    return sig;
  }

  squads(ix, tx) {
    const d = Buffer.from(ix.data);
    // the vault's creation: its signers (the owner, the page's set-up key)
    if (ix.keys.length === 6 && ix.keys[5].pubkey.equals(this.settings)) {
      const n = d.readUInt32LE(8 + 1 + 2);
      for (let i = 0; i < n; i++) {
        const k = new PublicKey(d.subarray(15 + 33 * i, 15 + 33 * i + 32)).toBase58();
        if (k !== OWNER.publicKey.toBase58()) { this.others.push(k); if (!this.held.has(k)) this.held.set(k, 0n); }
      }
      this.created = true;
      return;
    }
    if (ix.keys[0].pubkey.equals(this.settings) && d.subarray(0, 8).equals(SYNC) && d.readUInt32LE(9) >= 1 && [0, 1, 7].includes(d[13])) {
      // the set-up's settings changes: a signer on or off, a policy created (one a transaction)
      let at = 13;
      for (let i = 0; i < d.readUInt32LE(9); i++) {
        const kind = d[at];
        if (kind === 7) {
          const payer = ix.keys[1].pubkey.toBase58();
          if (this.held.has(payer)) this.held.set(payer, this.held.get(payer) - BigInt(1536 + 128) * 6960n);
          this.made++;
          return;
        }
        const k = new PublicKey(d.subarray(at + 1, at + 33)).toBase58();
        if (kind === 0) { this.others.push(k); if (!this.held.has(k)) this.held.set(k, 0n); at += 34; } else { this.others = this.others.filter(x => x !== k); at += 33; }
      }
      return;
    }
    if (ix.keys[0].pubkey.equals(this.settings) && ix.keys.length > 5 && ix.keys[1].pubkey.equals(OWNER.publicKey) && ix.keys[4]?.pubkey.equals(OWNER.publicKey)) {
      // a policy update: its signer is the key after the address, as the page decoded it
      const a = Buffer.from(ix.data);
      this.signer = new PublicKey(a.subarray(8 + 1 + 4 + 1 + 32 + 4, 8 + 1 + 4 + 1 + 32 + 4 + 32)).toBase58();
      return;
    }
    // an owner's execute: a close empties the positions, a withdrawal the vault
    if (this.closing) { this.positions = 0; this.sol += 500_000_000n; this.closing = false; }
    if (this.withdrawing) { this.sol = this.withdrawing === 'all' ? 0n : this.sol - this.withdrawing; this.withdrawing = null; }
    void tx;
  }

  status() {
    return { vault: this.vault.toBase58(), balances: { SOL: I.fromRaw(this.sol, 9), USDC: I.fromRaw(this.usdc, 6) },
      botActive: this.signer === POLICY.publicKey.toBase58(), rulesCurrent: true, positions: this.positions };
  }

  // what the server builds for each owner action, as the real builders do
  owner({ action, token, amount }) {
    const o = OWNER.publicKey, v = this.vault;
    const exec = (ixs) => sq.ownerExecuteIx({ settings: this.settings, signers: [o], vault: v, ixs });
    if (action === 'revoke' || action === 'approve') {
      const signer = action === 'revoke' ? o : POLICY.publicKey;
      const rules = this.policies.map((address, i) => unsigned([sq.settingsSyncIx({ settings: this.settings, signers: [o], rentPayer: o,
        extra: [new PublicKey(address)], actions: [{ kind: 'policyUpdate', address: new PublicKey(address),
          policy: this.tamper === 'policy' ? this.compiled[(i + 1) % this.compiled.length].policy : this.compiled[i].policy,
          signers: [{ key: signer }] }] })], o));
      // a re-grant with more capital: the deposit after the rules, in the last transaction
      if (action === 'approve' && token) rules.push(unsigned([SystemProgram.transfer({ fromPubkey: o, toPubkey: v, lamports: I.toRaw(amount, 9) })], o));
      return rules;
    }
    if (action === 'withdraw') {
      const lamports = amount === 'all' || token === 'ALL' ? this.sol : I.toRaw(amount, 9);
      this.withdrawing = token === 'ALL' || amount === 'all' ? 'all' : lamports;
      const to = this.tamper === 'withdraw' ? Keypair.generate().publicKey : o;
      return [unsigned([exec([SystemProgram.transfer({ fromPubkey: v, toPubkey: to, lamports })])], o)];
    }
    if (action === 'close') {
      if (!this.positions) return [];
      this.closing = true;
      return [unsigned([exec([closeIx(v)])], o)];
    }
    throw new Error(action);
  }
}

// A Raydium decrease_liquidity_v2 as the rules pin it, out of the vault into its own accounts.
function closeIx(vault) {
  const rule = instructionsOf(VENUE).find(r => r.name === 'decrease_liquidity_v2');
  const program = new PublicKey(RULES.venues[VENUE].program), pool = new PublicKey(POOL), reward = Keypair.generate().publicKey;
  const keys = Array.from({ length: 20 }, () => Keypair.generate().publicKey);
  keys[14] = WSOL; keys[15] = USDC; keys[19] = reward;
  for (const p of rule.pins) {
    keys[p.index] = p.role === 'pool' ? pool : p.role === 'vault' ? vault : p.role === 'pda'
      ? PublicKey.findProgramAddressSync(p.seeds.map(s => s === 'pool' ? pool.toBuffer() : Buffer.from(s)), program)[0]
      : new PublicKey(p.index >= 16 ? ata(vault, reward) : ata(vault, p.index % 2 ? WSOL : USDC));
  }
  return new TransactionInstruction({ programId: program, data: Buffer.concat([Buffer.from(rule.discriminator, 'hex'), Buffer.alloc(24)]),
    keys: keys.map(k => ({ pubkey: k, isSigner: false, isWritable: true })) });
}

// The database, in memory, as store.py keeps it.
function memoryStore(chain) {
  const rows = [];
  const feed = {};
  let id = 0, exitReq = null;
  const store = async (cmd, r) => {
    if (cmd === 'taken') return { taken: rows.some(x => x.name === r.name) };
    if (cmd === 'owned') return { count: rows.filter(x => x.owner === r.owner).length };
    if (cmd === 'sign') { rows.push({ ...r, id: ++id, state: 'signed', created_at: Date.now() / 1000, vault_row: null }); return { id }; }
    if (cmd === 'planned') { Object.assign(rows.find(x => x.id === r.id), { connected: r.connected, gas: r.gas, deposit: r.deposit }); return { planned: true }; }
    if (cmd === 'paid') { Object.assign(rows.find(x => x.id === r.id), { state: 'connecting', payment: r.payment }); return { paid: true }; }
    if (cmd === 'resume') { rows.find(x => x.id === r.id).state = 'connecting'; return {}; }
    if (cmd === 'connected') { Object.assign(rows.find(x => x.id === r.id), { connected: r.connected }); return {}; }
    if (cmd === 'stuck') { Object.assign(rows.find(x => x.id === r.id), { state: 'stuck', error: r.error }); return {}; }
    if (cmd === 'register') {
      Object.assign(rows.find(x => x.id === r.id), { state: 'registered', vault: chain.vault.toBase58(), vault_row: chain.row() });
      return { vault: chain.vault.toBase58() };
    }
    if (cmd === 'get') return { request: rows.find(x => x.name === r.name) ?? null };
    if (cmd === 'started') { rows.find(x => x.id === r.id).state = 'started'; if (exitReq && !exitReq.done_at) exitReq = { ...exitReq, done_at: Date.now() / 1000, outcome: 'cancelled' }; return { started: true }; }
    if (cmd === 'list') return { requests: rows.filter(x => x.owner === r.owner) };
    if (cmd === 'feed') return { feed };
    if (cmd === 'exits') return { exit: exitReq };
    if (cmd === 'exit') { exitReq = { id: 1, created_at: Date.now() / 1000, done_at: null }; store.onExit?.(); return { requested: true }; }
    if (cmd === 'deposited') return { recorded: true, max_usd: 10 };
    throw new StoreError(`unknown ${cmd}`);
  };
  store.rows = rows; store.feed = feed;
  store.finishExit = () => { exitReq = { ...exitReq, done_at: Date.now() / 1000, outcome: 'closed' }; };
  return store;
}

// The bot's book as the feed holds it: an in_band row and a DAILY row (shape of run/test0/events.jsonl).
const BOOK = { t: '2026-10-12T18:53:41Z', event: 'in_band', profile: 'my-pool', wallet_id: 'my-pool-vault', chain: 'solana', pair: 'SOL/USDC',
  token_a: 'SOL', token_b: 'USDC', price: 107.53, last_price: 107.53, lower: 105.4, upper: 109.7, dex: 'raydium-clmm', position_dex: 'raydium-clmm',
  fees_today_a: 0.006998, fees_today_b: 0.707853, fees_today_usd: 1.4776, fees_realised_a: 0.006536, fees_realised_b: 0.663733, fees_realised_usd: 1.3839,
  fees_unrealised_a: 0.000467, fees_unrealised_b: 0.045065, fees_unrealised_usd: 0.0953, fees_total_a: 0.007004, fees_total_b: 0.708798, fees_total_usd: 1.4792,
  fees_per_day_usd: 1.6626, apr_pct: 332.71, harvests: 7, positions_opened: 8, rebands: 0, failures: 0, equity_usd: 182.39, lp_usd: 181.73, wallet_usd: 0.57,
  deployed_pct: 99.6, in_range_pct: 98.5, tracked_days: 0.89, split: { paid_usd: 0.6637, paid_today_usd: 0.6637, reinvested_usd: 0.7203, gas_usd: 0 },
  band: { in_range: true, position: -0.143, p_exit_6h: 0.312, p_exit_24h: 0.854, p_exit_72h: 1.0, hours_alive: 1.8 },
  regime: { mode: 'CALM', held_pct: 1, choice_pct: 1, sigma_5m_pct: 0.08, velocity: 0.1, instability: 0.2, horizon_minutes: 120, probs: [[1, 0.18], [2, 0.05]],
    p_held: 0.18, threshold: 0.2, steps: 2, moves_24h: 3, guard: 12 } };
const DAILY = { t: '2026-10-12T00:00:05Z', event: 'DAILY', day: '2026-10-11', recentres: 1, fees_usd: 0, fees_earned_usd: 0.0016, vs_hold_usd: 0.0194,
  price_open: 110.41, price_close: 111.25, token_a: 'SOL', token_b: 'USDC', pair: 'SOL/USDC' };

async function rig({ chain = new Chain(), wc = { projectId: '', relayUrl: 'wss://relay.walletconnect.org', verify: [] } } = {}) {
  const cfg = loadConfig();
  const C = cfg.chains.solana;
  const store = memoryStore(chain);
  let service = 'inactive';
  const adapter = {
    keys: { public: [PAYER.publicKey.toBase58(), POLICY.publicKey.toBase58()], payee: PAYER.publicKey.toBase58(), secretEnv: 'P', policyKeyEnv: 'Q' },
    expires: true, venueReady: () => true,
    gas: async () => ({ ok: true }),
    setup: (a) => chain.setup(a),
    setupState: async () => (chain.funded ? { state: 'done' } : { state: 'pending', missing: 1 }),
    funding: async () => (chain.funded ? { state: 'ok', payment: chain.funded, deposit: chain.funded } : { state: 'pending', reason: 'not on chain' }),
    finish: async (r) => ({ vault: chain.vault.toBase58(), settings: chain.settings.toBase58(), sent: true, owners: [r.owner], lookup: 'LUT',
      venues: { [VENUE]: { policies: chain.policies, pools: [POOL] } } }),
    record: async () => ({ token_a: { symbol: 'SOL' }, token_b: { symbol: 'USDC' } }),
    deposit: async (a) => ({ summary: 'deposit', transactions: [a.token === 'SOL'
      ? unsigned([SystemProgram.transfer({ fromPubkey: new PublicKey(a.owner), toPubkey: chain.vault, lamports: I.toRaw(a.amount, 9) })], new PublicKey(a.owner))
      : unsigned([spl.createAssociatedTokenAccountIdempotentInstruction(new PublicKey(a.owner), new PublicKey(ata(chain.vault, USDC)), chain.vault, USDC),
        spl.createTransferCheckedInstruction(new PublicKey(ata(a.owner, USDC)), USDC, new PublicKey(ata(chain.vault, USDC)), new PublicKey(a.owner), I.toRaw(a.amount, 6), 6)],
      new PublicKey(a.owner))] }),
    owner: async (row, a) => ({ summary: a.action, transactions: chain.owner(a) }),
    status: async () => chain.status(),
    hasDeposit: (st) => Object.values(st.balances).some(v => Number(v) > 0),
    holdings: async () => ({ SOL: 3_000_000_000n, USDC: 50_000_000n }),
    context: (r) => ({ settings: chain.settings.toBase58(), vault: chain.vault.toBase58(), policies: { [VENUE]: chain.policies }, pools: { [VENUE]: [POOL] },
      payoutWallets: [r.profit_wallet, ...(r.fee_wallet ? [r.fee_wallet] : [])] }),
    mintOf: (t) => C.tokens[t]?.mint,
    checkDeposit: async () => ({ state: 'ok', deposits: [{ token: 'SOL', amount: '1' }] }),
    confirm: async () => ({ state: 'confirmed' }),
    // a WalletConnect wallet that only signs: the server sends it
    broadcast: async (b64) => chain.land(Transaction.from(Buffer.from(b64, 'base64'))),
  };
  const bundle = await testBundle({ solana: { payer: PAYER.publicKey.toBase58(), policyKey: POLICY.publicKey.toBase58() } });
  const server = makeServer(cfg, { chains: { solana: adapter }, store, inviteHashes: crypto.createHash('sha256').update(INVITE).digest('hex'),
    log: () => {}, bundle, walletConnect: wc,
    start: async () => { service = 'active'; setTimeout(() => { if (chain.sol > 0n) chain.positions = 1; }, 300); return { code: 0, out: '', err: '' }; },
    status: async () => ({ code: 0, out: `${service}\n`, err: '' }) });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  return { server, base, chain, store, setService: (s) => { service = s; } };
}

const B = await browser();
const skip = B ? false : 'Chromium is not installed (onboard/web: npx playwright install chromium)';
test.after(() => B?.close());

const click = (page, sel) => page.locator(sel).click();
const visible = (page, id) => page.waitForSelector(`#${id}:not([hidden])`, { timeout: 30_000 });

async function connect(page) {
  await click(page, '#w-injected');
  await page.waitForFunction(() => !['v-connect', 'v-loading'].includes(document.body.dataset.view), null, { timeout: 30_000 });
}

for (const device of [PHONE, DESKTOP]) {
  const dev = device === PHONE ? 'phone' : 'desktop';

  test(`page (${dev}): connect, create, the bot's book, add capital, withdraw part, revoke, add capital again, withdraw all`, { skip, timeout: 300_000 }, async () => {
    const r = await rig();
    const { page, errors, wallet } = await openPage(B, r.base, INVITE, { device, setup: (ctx) => solanaWallet(ctx, OWNER, async (tx) => r.chain.land(tx)) });
    try {
      // connect: the browser's wallet; no WalletConnect project on this server
      assert.equal(await page.isVisible('#w-wc'), false, 'no project id: no WalletConnect button');
      assert.equal(await page.isVisible('#wc-off'), true);
      await shot(page, '01-connect');
      await connect(page);
      assert.equal(await page.evaluate(() => document.body.dataset.view), 'v-create', 'a wallet without a vault creates one');
      assert.equal(await page.inputValue('#c-profit'), OWNER.publicKey.toBase58());
      await page.fill('#c-amount', '2');
      await page.fill('#c-name', 'my-pool');
      await shot(page, '02-create');
      await click(page, '#c-go');
      let f = await flowDone(page);
      assert.deepEqual(f.steps.map(s => s.state), Array(5).fill('done'), JSON.stringify(f));
      const review = f.review.join('\n');
      assert.match(review, /Create your vault .*you its owner, threshold 1; this page's one-time set-up key .* its second signer/);
      assert.match(review, /The set-up key takes itself off your vault: you are its only owner again/);
      assert.ok(r.chain.whole(), 'every rule on the vault, the owner alone on it');
      assert.match(review, /exactly the rules in git/);
      assert.match(review, /Funding: Send 1\.8814 SOL from your wallet .* to your vault/);
      assert.match(review, /Funding, sent last, once the set-up key is off your vault: Send 0\.1186 SOL from your wallet .* to the bot's gas key/);
      assert.deepEqual(wallet.map(x => x.kind), ['signAll'], 'one approval in the wallet: the whole set-up, no message');
      assert.equal(wallet[0].txs.length, 2);
      assert.equal(r.chain.sol, 1_881_400_000n);
      await shot(page, '03-created');

      // the bot runs: its book, as Telegram renders it
      r.store.feed.book = { row: BOOK }; r.store.feed.DAILY = { row: DAILY }; r.chain.positions = 1; r.chain.sol = 0n;
      await click(page, '#f-done');
      await visible(page, 'v-active');
      const text = await page.textContent('#a-text');
      assert.match(text, /IN RANGE · raydium-clmm SOL\/USDC · 107\.5300/);
      assert.match(text, /━━ 💰 FEES ━━/);
      assert.match(text, /TOTAL\s+0\.007004 SOL\s+0\.7088 USDC\s+\$1\.4792/);
      assert.match(await page.textContent('#a-daily'), /DAILY 2026-10-11 · 1 re-centres/);
      assert.match(await page.textContent('#a-tiles'), /Equity\$182\.39/);
      assert.match(await page.textContent('#a-pill'), /Running/);
      await shot(page, '04-active');

      // add capital
      await click(page, '#a-add');
      await page.fill('#sh-body input', '0.5');
      await shot(page, '05-add-sheet');
      await click(page, '#sh-go');
      f = await flowDone(page);
      assert.deepEqual(f.steps.map(s => s.state), ['done', 'done'], JSON.stringify(f));
      assert.equal(r.chain.sol, 500_000_000n);
      await click(page, '#f-done');
      await visible(page, 'v-active');

      // withdraw part: the idle SOL covers it, no close
      await click(page, '#a-withdraw');
      await page.selectOption('#sh-body select', 'SOL');
      await page.fill('#sh-body input', '0.2');
      await shot(page, '06-withdraw-sheet');
      await click(page, '#sh-go');
      f = await flowDone(page);
      assert.deepEqual(f.steps.map(s => s.label), ['Withdraw 0.2 SOL'], JSON.stringify(f));
      assert.equal(f.steps[0].state, 'done');
      assert.equal(r.chain.sol, 300_000_000n);
      await click(page, '#f-done');
      await visible(page, 'v-active');

      // revoke with a position, the bot answering: it closes into the vault, then the owner revokes
      r.store.onExit = () => setTimeout(() => { r.chain.positions = 0; r.store.finishExit(); r.setService('inactive'); }, 500);
      await click(page, '#a-revoke');
      await shot(page, '07-revoke-sheet');
      await click(page, '#sh-go');
      f = await flowDone(page);
      assert.deepEqual(f.steps.map(s => [s.label, s.state]), [['The bot closes its position into your vault', 'done'],
        ['Remove the bot\'s key from your vault', 'done']], JSON.stringify(f));
      assert.equal(r.chain.signer, OWNER.publicKey.toBase58(), 'every policy names the owner alone');
      assert.match(f.review.join('\n'), /signer only you; \d+ allowed instructions, exactly the rules in git/);
      assert.match(await page.textContent('#alert'), /The bot is revoked/);
      await shot(page, '08-revoked-offer');
      await click(page, '#f-done');                             // keep it in the vault

      // revoked: only Add capital
      await visible(page, 'v-revoked');
      assert.deepEqual(await page.$$eval('#v-revoked button', bs => bs.filter(b => !b.hidden).map(b => b.textContent)), ['Add capital']);
      await shot(page, '09-revoked');
      // Add capital again: the rules back (each checked against the pinned rules), the deposit, the bot starts
      await click(page, '#r-add');
      await page.fill('#sh-body input', '1');
      await shot(page, '10-regrant-sheet');
      await click(page, '#sh-go');
      f = await flowDone(page);
      assert.deepEqual(f.steps.map(s => [s.label, s.state]), [['Approve the bot\'s rules and deposit 1 SOL', 'done'], ['Start the bot', 'done']], JSON.stringify(f));
      assert.match(f.review.join('\n'), /Deposit: Send 1 SOL from your wallet .* to your vault/);
      assert.equal(wallet.at(-1).kind, 'signAll', 'the rules and the deposit: one approval');
      assert.equal(r.chain.signer, POLICY.publicKey.toBase58(), 'the bot\'s key is back');
      assert.equal(r.chain.sol, 1_300_000_000n);
      await click(page, '#f-done');
      await visible(page, 'v-active');

      // withdraw everything: the bot is down, no position: straight to the withdrawal
      r.setService('inactive');
      await click(page, '#a-withdraw');
      await click(page, '#sh-go');                                // Everything is the default
      f = await flowDone(page);
      assert.deepEqual(f.steps.map(s => [s.label, s.state]), [['Withdraw everything to your wallet', 'done']], JSON.stringify(f));
      assert.equal(r.chain.sol, 0n);
      await shot(page, '11-withdrawn');
      assert.deepEqual(errors.filter(e => !/favicon/.test(e)), []);
    } finally { await page.context().close(); r.server.close(); }
  });

  test(`page (${dev}): the bot does not answer: revoke first, then the owner closes the position`, { skip, timeout: 300_000 }, async () => {
    const r = await rig();
    r.chain.positions = 1;
    const { page } = await openPage(B, r.base, INVITE, { device, setup: (ctx) => solanaWallet(ctx, OWNER, async (tx) => r.chain.land(tx)) });
    try {
      // a vault the bot runs, its service down
      r.store.rows.push({ id: 9, name: 'down-pool', owner: OWNER.publicKey.toBase58(), chain: 'solana', state: 'started', vault: r.chain.vault.toBase58(),
        vault_row: r.chain.row(), token: 'SOL', deposit: '1', profit_wallet: OWNER.publicKey.toBase58(), fee_wallet: OPERATOR });
      await connect(page);
      await visible(page, 'v-active');
      assert.match(await page.textContent('#a-pill'), /Bot stopped · position open/);
      assert.equal(await page.isVisible('#a-empty'), true, 'no book yet: said so');
      await shot(page, '12-active-bot-down');
      await click(page, '#a-revoke');
      await click(page, '#sh-go');
      const f = await flowDone(page);
      assert.deepEqual(f.steps.map(s => [s.label, s.state]), [['Remove the bot\'s key from your vault', 'done'], ['Close the position yourself', 'done']],
        JSON.stringify(f));
      assert.equal(r.chain.positions, 0);
      assert.match(f.review.join('\n'), /Raydium: decrease_liquidity_v2/);
      await shot(page, '13-owner-closed');
      // and the offer: everything to the wallet
      await click(page, '#f-alt');
      const g = await flowDone(page);
      assert.deepEqual(g.steps.map(s => s.state), ['done']);
      assert.equal(r.chain.sol, 0n);
    } finally { await page.context().close(); r.server.close(); }
  });

  test(`page (${dev}): a tampered transaction is refused before the wallet sees it; a declined one says so; retry works`, { skip, timeout: 300_000 }, async () => {
    const r = await rig();
    let asked = 0;
    const { page } = await openPage(B, r.base, INVITE, { device, setup: (ctx) => solanaWallet(ctx, OWNER, async (tx) => { asked++; return r.chain.land(tx); },
      { onSignAll: () => { asked++; } }) });
    try {
      r.store.rows.push({ id: 9, name: 'my-pool', owner: OWNER.publicKey.toBase58(), chain: 'solana', state: 'started', vault: r.chain.vault.toBase58(),
        vault_row: r.chain.row(), token: 'SOL', deposit: '1', profit_wallet: OWNER.publicKey.toBase58(), fee_wallet: OPERATOR });
      r.chain.sol = 1_000_000_000n;
      await connect(page);
      await visible(page, 'v-active');
      // a withdrawal to someone else
      r.chain.tamper = 'withdraw';
      await click(page, '#a-withdraw');
      await page.selectOption('#sh-body select', 'SOL');
      await page.fill('#sh-body input', '0.1');
      await click(page, '#sh-go');
      let f = await flowDone(page);
      assert.equal(f.steps[0].state, 'failed');
      assert.match(f.alert, /The page refused this transaction: .*from your vault to your wallet/);
      assert.equal(asked, 0, 'the wallet was never asked');
      await shot(page, '14-refused');
      // the server stops tampering: Try again goes through
      r.chain.tamper = null;
      await click(page, '#f-retry');
      f = await flowDone(page);
      assert.equal(f.steps[0].state, 'done', JSON.stringify(f));
      assert.equal(asked, 1);
      await click(page, '#f-done');
      await visible(page, 'v-active');
      // a revoke whose rules differ from the pinned ones
      r.chain.tamper = 'policy';
      await click(page, '#a-revoke');
      await click(page, '#sh-go');
      f = await flowDone(page);
      assert.match(f.alert, /differs from the rules in git/);
      assert.equal(r.chain.signer, POLICY.publicKey.toBase58(), 'nothing changed');
      assert.equal(asked, 1);
    } finally { await page.context().close(); r.server.close(); }
  });
}

test('page: a declined set-up, a tampered one, a stopped setup that resumes, a wallet that signs one at a time', { skip, timeout: 300_000 }, async () => {
  const r = await rig();
  let reject = true;
  const mode = { get reject() { return reject; } };
  const { page } = await openPage(B, r.base, INVITE, { device: PHONE, setup: (ctx) => solanaWallet(ctx, OWNER, async (tx) => r.chain.land(tx), mode) });
  try {
    await connect(page);
    await page.fill('#c-amount', '2');
    await page.fill('#c-name', 'led-pool');
    await click(page, '#c-go');
    let f = await flowDone(page);
    assert.equal(f.steps[0].state, 'failed');
    assert.match(f.alert, /You declined in your wallet\. Nothing was sent\./);
    await shot(page, '15-declined');
    // the server builds a set-up that pays someone else: refused before the wallet sees it
    reject = false;
    r.chain.tamper = 'setup';
    await click(page, '#f-retry');
    f = await flowDone(page);
    assert.equal(f.steps[0].state, 'failed');
    assert.match(f.alert, /The page refused this transaction: .*0 payments to the bot's gas key/);
    assert.equal(r.chain.sent.length, 0, 'nothing sent');
    r.chain.tamper = null;
    await click(page, '#f-retry');
    f = await flowDone(page);
    assert.deepEqual(f.steps.map(s => s.state), Array(5).fill('done'), JSON.stringify(f));
    const row = r.store.rows.find(x => x.name === 'led-pool');
    assert.equal(row.state, 'started');
    assert.equal(row.signature, '', 'no message signed');
  } finally { await page.context().close(); r.server.close(); }

  // a wallet that only signs and sends one transaction at a time: asked once per transaction, in order
  const r3 = await rig();
  const { page: p3, wallet: w3 } = await openPage(B, r3.base, INVITE, { device: PHONE,
    setup: (ctx) => solanaWallet(ctx, OWNER, async (tx) => r3.chain.land(tx), { noSignAll: true }) });
  try {
    await connect(p3);
    await p3.fill('#c-amount', '2');
    await p3.fill('#c-name', 'one-by-one');
    await click(p3, '#c-go');
    const f = await flowDone(p3);
    assert.deepEqual(f.steps.map(s => s.state), Array(5).fill('done'), JSON.stringify(f));
    assert.deepEqual(w3.map(x => x.kind), ['tx', 'tx']);
    assert.equal(r3.chain.funded, bs58.encode(w3[1].tx.signature), 'the funding came last');
  } finally { await p3.context().close(); r3.server.close(); }

  // a stopped setup: the page offers Continue, which pays nothing again
  const r2 = await rig();
  const { page: p2 } = await openPage(B, r2.base, INVITE, { device: PHONE, setup: (ctx) => solanaWallet(ctx, OWNER, async (tx) => r2.chain.land(tx)) });
  try {
    r2.chain.funded = 'x'; r2.chain.sol = 1_000_000_000n;
    r2.store.rows.push({ id: 5, name: 'stuck-pool', owner: OWNER.publicKey.toBase58(), chain: 'solana', state: 'stuck', error: 'pool read timed out',
      payment: 'x', created_at: Date.now() / 1000, vault_row: null, token: 'SOL', deposit: '1', gas: '0.1186',
      connected: { planned: { seed: String(r2.chain.seed) }, deposit: 'x' } });
    await connect(p2);
    await visible(p2, 'v-setup');
    assert.match(await p2.textContent('#s-text'), /The bot's finish stopped: pool read timed out\. Your vault and your money are yours; nothing is paid twice\./);
    await shot(p2, '16-setup-stuck');
    await click(p2, '#s-go');
    const f = await flowDone(p2);
    assert.deepEqual(f.steps.map(s => s.state), Array(5).fill('done'), JSON.stringify(f));
    assert.equal(r2.chain.sent.length, 0, 'no second set-up, no second gas payment');
  } finally { await p2.context().close(); r2.server.close(); }
});

test('page: a server whose bot keys are not the pinned ones gets no signature', { skip, timeout: 120_000 }, async () => {
  const r = await rig();
  const bundle = await testBundle({ solana: { payer: Keypair.generate().publicKey.toBase58(), policyKey: POLICY.publicKey.toBase58() } });
  const cfg = loadConfig();
  const other = makeServer(cfg, { chains: { solana: { keys: { public: [PAYER.publicKey.toBase58(), POLICY.publicKey.toBase58()] } } }, store: async () => ({}),
    inviteHashes: crypto.createHash('sha256').update(INVITE).digest('hex'), log: () => {}, bundle,
    walletConnect: { projectId: '', relayUrl: 'wss://relay.walletconnect.org', verify: [] }, start: async () => ({}), status: async () => ({ out: '' }) });
  await new Promise(res => other.listen(0, '127.0.0.1', res));
  const { page } = await openPage(B, `http://127.0.0.1:${other.address().port}`, INVITE, { device: PHONE,
    setup: (ctx) => solanaWallet(ctx, OWNER, async () => { throw new Error('never'); }) });
  try {
    assert.match(await page.textContent('#alert'), /bot keys differ from the keys this page was built with/);
    await click(page, '#w-injected');
    assert.equal(await page.evaluate(() => document.body.dataset.view), 'v-connect');
    await shot(page, '17-keys-differ');
  } finally { await page.context().close(); other.close(); r.server.close(); }
});

test('page: the CSP is strict; WalletConnect\'s relay only with a project id; no inline script or style', { skip, timeout: 60_000 }, async () => {
  const r = await rig({ wc: { projectId: 'a'.repeat(32), relayUrl: 'wss://relay.walletconnect.org', verify: ['https://verify.walletconnect.org'] } });
  try {
    const res = await fetch(`${r.base}/bot/`);
    const policy = res.headers.get('content-security-policy');
    assert.match(policy, /script-src 'self'(;|$)/);
    assert.match(policy, /connect-src 'self' wss:\/\/relay\.walletconnect\.org https:\/\/verify\.walletconnect\.org/);
    assert.match(policy, /frame-src https:\/\/verify\.walletconnect\.org/);
    assert.doesNotMatch(policy, /unsafe/);
    const html = await res.text();
    assert.doesNotMatch(html, /<script>|style="|\son[a-z]+="/, 'no inline script, style or handler');
    assert.equal((await fetch(`${r.base}/bot/bundle.js`)).status, 200);
    assert.equal((await fetch(`${r.base}/bot/app.js`)).status, 404, 'the old script is gone');
    const { page, errors } = await openPage(B, r.base, INVITE, { device: DESKTOP, setup: (ctx) => solanaWallet(ctx, OWNER, async () => 'x') });
    assert.equal(await page.isVisible('#w-wc'), true, 'a project id: WalletConnect is offered');
    await shot(page, '18-connect-walletconnect');
    assert.deepEqual(errors.filter(e => /Content Security Policy|Refused/.test(e)), [], 'nothing the CSP blocks');
    await page.context().close();
  } finally { r.server.close(); }
});

// A phone wallet behind the mock relay, a Tangem card's limit: it signs no
// message over 930 bytes (the card's APDU, tangem-app-android
// SOLANA_TRANSACTION_SIZE_THRESHOLD_BYTES). `signAll`: it offers
// solana_signAllTransactions. `text`: it answers as Tangem's Android app
// does, JSON as text, a signature as loose text. `sizes`: each message's bytes.
async function wcWallet(rl, { signAll = false, text = false, redirect, onRequest = async () => {} } = {}) {
  const { wallet } = await import('./_wc_relay.mjs');
  const addr = OWNER.publicKey.toBase58();
  const sizes = [];
  const signed = (b64) => {
    const tx = Transaction.from(Buffer.from(b64, 'base64'));
    const n = tx.compileMessage().serialize().length;
    sizes.push(n);
    if (n > 930) throw new Error(`The card cannot sign a transaction of ${n} bytes`);
    tx.partialSign(OWNER);
    return tx;
  };
  const w = await wallet(rl.url, {
    redirect,
    accounts: { solana: [`solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp:${addr}`] },
    methods: { solana: ['solana_signMessage', 'solana_signTransaction', ...(signAll ? ['solana_signAllTransactions'] : [])] },
    handle: async (method, p) => {
      await onRequest(method);
      if (method === 'solana_signMessage') return { signature: bs58.encode(edSign(OWNER, bs58.decode(p.message))) };
      // a signature only: the page attaches it, the server sends
      if (method === 'solana_signTransaction') {
        const sig = bs58.encode(signed(p.transaction).signature);
        return text ? `{ signature: "${sig}" }` : { signature: sig };
      }
      if (method === 'solana_signAllTransactions') {
        const out = { transactions: p.transactions.map(t => signed(t).serialize({ requireAllSignatures: false }).toString('base64')) };
        return text ? JSON.stringify(out) : out;
      }
      throw new Error(`no ${method}`);
    } });
  return Object.assign(w, { sizes });
}

test('page: WalletConnect (a phone wallet that only signs): pair by the code, create one by one, the wallet brought forward, revoke', { skip, timeout: 300_000 }, async () => {
  const { relay } = await import('./_wc_relay.mjs');
  const rl = await relay();
  const r = await rig({ wc: { projectId: 'a'.repeat(32), relayUrl: rl.url, verify: [] } });
  const addr = OWNER.publicKey.toBase58();
  let page;
  // each request: the page shows the big button to its wallet (the session's own deep link) while it waits
  const seen = [];
  const w = await wcWallet(rl, { text: true, redirect: { native: 'tangemtest://wc' }, onRequest: async () => {
    seen.push(await page.evaluate(() => ({ shown: !document.getElementById('f-wallet').hidden, text: document.getElementById('f-wallet-text').textContent,
      href: document.getElementById('f-open').getAttribute('href'), open: !document.getElementById('f-open').hidden })));
  } });
  ({ page } = await openPage(B, r.base, INVITE, { device: PHONE }));
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  try {
    assert.equal(await page.isVisible('#w-injected'), false, 'no wallet in this browser');
    await click(page, '#w-wc');
    await visible(page, 'v-wc');
    const uri = await page.getAttribute('#wc-open', 'href');
    assert.match(uri, /^wc:[0-9a-f]{64}@2\?/);
    assert.match(await page.getAttribute('#wc-qr', 'src'), /^data:image\/gif;base64,/);
    await shot(page, '19-walletconnect-code');
    await w.pair(uri);
    await page.waitForFunction(() => document.body.dataset.view === 'v-create', null, { timeout: 60_000 });
    assert.match(await page.textContent('#who-wallet'), /Tangem/);
    await page.fill('#c-amount', '2');
    await page.fill('#c-name', 'wc-pool');
    await click(page, '#c-go');
    let f = await flowDone(page);
    assert.deepEqual(f.steps.map(s => s.state), Array(5).fill('done'), JSON.stringify(f));
    assert.deepEqual(w.requests.map(q => q.method), ['solana_signTransaction', 'solana_signTransaction'], 'one request per transaction, no message');
    assert.ok(w.sizes.length === 2 && w.sizes.every(n => n <= 900), `the card signs short messages only: ${w.sizes}`);
    assert.ok(r.chain.whole(), 'the vault has every policy, the owner alone on it');
    assert.equal(r.chain.sol, 1_881_400_000n, 'the deposit the server sent for the wallet');
    assert.equal(seen.length, 2);
    for (const x of seen) {
      assert.deepEqual([x.shown, x.open, x.href], [true, true, 'tangemtest://wc'], JSON.stringify(x));
      assert.match(x.text, /Tangem asks you to sign/);
    }
    assert.equal(await page.isVisible('#f-wallet'), false, 'gone once the wallet answered');
    await click(page, '#f-done');
    await visible(page, 'v-active');
    r.store.onExit = () => setTimeout(() => { r.chain.positions = 0; r.store.finishExit(); r.setService('inactive'); }, 300);
    // a revoke rewrites each policy, longer than the card signs: said so before the card is asked, nothing sent
    const before = w.requests.length;
    await click(page, '#a-revoke');
    await click(page, '#sh-go');
    f = await flowDone(page);
    assert.equal(f.steps.at(-1).state, 'failed', JSON.stringify(f));
    assert.match(f.alert, /Tangem cannot sign this step: one of its transactions is \d+ bytes, and the card signs no more than 930\. Nothing was sent\./);
    assert.deepEqual(w.requests.slice(before).map(q => q.method), ['solana_signMessage'], 'the card signed the close request only; never asked for the long one');
    assert.equal(r.chain.signer, POLICY.publicKey.toBase58(), 'nothing changed');
    void addr;
    assert.deepEqual(errors.filter(e => !/favicon/.test(e)), []);
    assert.ok(rl.log.includes('wc_proposeSession') && rl.log.includes('wc_approveSession'), rl.log.join(','));
  } finally { await page.context().close(); r.server.close(); await w.close(); await rl.close(); }
});

test('page: WalletConnect with signAll: the set-up in ONE request; the page\'s socket dropped while away comes back without a reload', { skip, timeout: 300_000 }, async () => {
  const { relay } = await import('./_wc_relay.mjs');
  const rl = await relay();
  const r = await rig({ wc: { projectId: 'a'.repeat(32), relayUrl: rl.url, verify: [] } });
  let page, dropped = false;
  const w = await wcWallet(rl, { signAll: true, onRequest: async (method) => {
    if (dropped || method !== 'solana_signAllTransactions') return;
    // the owner is in the wallet app: the phone closes the page's socket; then the page is in front again
    dropped = true;
    rl.dropBrowser();
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
      document.dispatchEvent(new Event('visibilitychange'));
      Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
      document.dispatchEvent(new Event('visibilitychange'));
    });
  } });
  ({ page } = await openPage(B, r.base, INVITE, { device: DESKTOP }));
  try {
    await click(page, '#w-wc');
    await visible(page, 'v-wc');
    await w.pair(await page.getAttribute('#wc-open', 'href'));
    await page.waitForFunction(() => document.body.dataset.view === 'v-create', null, { timeout: 60_000 });
    await page.fill('#c-amount', '2');
    await page.fill('#c-name', 'wc-all');
    await click(page, '#c-go');
    // a computer: the person is told to look at the phone, no button
    await page.waitForFunction(() => !document.getElementById('f-wallet').hidden, null, { timeout: 60_000 });
    assert.match(await page.textContent('#f-wallet-text'), /^Check your phone: Tangem asks you to sign\.$/);
    assert.equal(await page.isVisible('#f-open'), false);
    const f = await flowDone(page, 120_000);
    assert.deepEqual(f.steps.map(s => s.state), Array(5).fill('done'), JSON.stringify(f));
    assert.deepEqual(w.requests.map(q => q.method), ['solana_signAllTransactions'], 'one approval for the whole set-up');
    assert.equal(w.requests[0].params.transactions.length, 2, 'the creation with the key\'s SOL, and the funding');
    assert.ok(w.sizes.every(n => n <= 900), `the card signs short messages only: ${w.sizes}`);
    assert.ok(r.chain.whole());
    assert.ok(dropped);
    assert.ok(rl.connections().filter(u => /browser/.test(decodeURIComponent(u))).length >= 1, 'the page is connected again');
    assert.equal(r.chain.sol, 1_881_400_000n);
    // 2026-10-14: Add capital on Tangem showed no sign box for one
    // solana_signTransaction. One deposit goes as a set of one, the path above.
    await click(page, '#f-done');
    await visible(page, 'v-active');
    await click(page, '#a-add');
    await page.fill('#sh-body input', '0.1');
    await click(page, '#sh-go');
    const g = await flowDone(page, 120_000);
    assert.ok(g.steps.every(s => s.state === 'done'), JSON.stringify(g));
    assert.deepEqual(w.requests.map(q => q.method), ['solana_signAllTransactions', 'solana_signAllTransactions'], 'no single-transaction request');
    assert.equal(w.requests[1].params.transactions.length, 1);
    assert.equal(r.chain.sol, 1_981_400_000n);
  } finally { await page.context().close(); r.server.close(); await w.close(); await rl.close(); }
});

// The owner's report, 2026-10-14: "Tangem is not working even on step 1".
// The set-up asked the card to sign the bot's rules, each longer than the
// card signs. Now: the card signs only short messages; the page stops
// half-way (the network refuses one of the key's transactions), the error is
// shown, and Continue setup goes on from what the chain has: the card is
// not asked for the vault again, nothing is paid twice.
test('page: Tangem over WalletConnect: stopped half-way, the error shown, Continue setup resumes from the chain', { skip, timeout: 300_000 }, async () => {
  const { relay } = await import('./_wc_relay.mjs');
  const rl = await relay();
  const r = await rig({ wc: { projectId: 'a'.repeat(32), relayUrl: rl.url, verify: [] } });
  let page, fail = 1;
  // the network refuses the set-up key's second policy, once
  const land = r.chain.land.bind(r.chain);
  r.chain.land = (tx) => {
    if (tx.feePayer && !tx.feePayer.equals(OWNER.publicKey) && r.chain.made === 1 && fail-- > 0) throw new I.Invalid('the network refused the transaction: node down');
    return land(tx);
  };
  const w = await wcWallet(rl, { signAll: true, text: true });
  ({ page } = await openPage(B, r.base, INVITE, { device: PHONE }));
  try {
    await click(page, '#w-wc');
    await visible(page, 'v-wc');
    await w.pair(await page.getAttribute('#wc-open', 'href'));
    await page.waitForFunction(() => document.body.dataset.view === 'v-create', null, { timeout: 60_000 });
    await page.fill('#c-amount', '2');
    await page.fill('#c-name', 'tangem-pool');
    await click(page, '#c-go');
    let f = await flowDone(page, 120_000);
    assert.equal(f.steps[0].state, 'failed', JSON.stringify(f));
    assert.match(f.alert, /node down/, 'the error is shown, not swallowed');
    assert.deepEqual([r.chain.created, r.chain.made, r.chain.funded], [true, 1, null], 'the vault and one rule on chain; no money moved');
    assert.equal(r.chain.others.length, 1, 'the page\'s set-up key still on the vault');
    await shot(page, '20-tangem-stopped');
    // Back: the page shows the set-up to continue
    await click(page, '#f-done');
    await visible(page, 'v-setup');
    assert.match(await page.textContent('#s-text'), /not all on chain yet/);
    await shot(page, '21-tangem-continue');
    const asked = w.requests.length;
    await click(page, '#s-go');
    f = await flowDone(page, 120_000);
    assert.deepEqual(f.steps.map(s => s.state), Array(5).fill('done'), JSON.stringify(f));
    assert.equal(w.requests.length, asked + 1, 'the card asked once more, for the funding alone: the vault is not asked for again');
    assert.equal(w.requests.at(-1).params.transactions.length, 1);
    assert.ok(r.chain.whole(), 'every rule, the set-up key off: the owner alone');
    assert.equal(r.chain.held.get(r.chain.lastSetup.planned.setupKeys[0]), 0n, 'the set-up key sent all it held back');
    assert.equal(r.chain.sol, 1_881_400_000n, 'the deposit, once');
    assert.equal(r.chain.sent.filter(t => t.instructions.some(ix => ix.programId.equals(SystemProgram.programId) && ix.keys[1].pubkey.equals(PAYER.publicKey))).length, 1,
      'the bot\'s gas, once');
    assert.ok(w.sizes.every(n => n <= 900), `the card signs short messages only: ${w.sizes}`);
    assert.equal(await page.evaluate(() => Object.keys(localStorage).filter(k => k.startsWith('aperture-setup-key-')).length), 0, 'the set-up key is forgotten');
  } finally { await page.context().close(); r.server.close(); await w.close(); await rl.close(); }
});

// The old set-up (one approval for the vault and every rule) asked a card for
// more than it signs: the card's refusal is shown in the page as it came.
test('page: a wallet\'s own refusal is shown as it came, never swallowed', { skip, timeout: 120_000 }, async () => {
  const r = await rig();
  const { page } = await openPage(B, r.base, INVITE, { device: PHONE,
    setup: (ctx) => solanaWallet(ctx, OWNER, async (tx) => r.chain.land(tx), { failAll: 'The card cannot sign a transaction of 1147 bytes' }) });
  try {
    await connect(page);
    await page.fill('#c-amount', '2');
    await page.fill('#c-name', 'refused-pool');
    await click(page, '#c-go');
    const f = await flowDone(page);
    assert.equal(f.steps[0].state, 'failed');
    assert.match(f.alert, /The card cannot sign a transaction of 1147 bytes/);
    assert.match(f.steps[0].detail, /The card cannot sign/);
    assert.equal(r.chain.sent.length, 0, 'nothing sent');
  } finally { await page.context().close(); r.server.close(); }
});

// The funding is signed with the owner's part (one approval) and sent last.
// A card is slow: when the network refuses it as too old, it is built again
// and the wallet asked once more, saying why; nothing is paid twice.
test('page: the funding signed first and too old by the time it goes: built again, asked once more, paid once', { skip, timeout: 120_000 }, async () => {
  const r = await rig();
  let refused = 0;
  const land = r.chain.land.bind(r.chain);
  r.chain.land = (tx) => {
    const pays = tx.instructions.some(ix => ix.programId.equals(SystemProgram.programId) && ix.keys[1].pubkey.equals(PAYER.publicKey));
    if (pays && refused++ === 0) throw new I.Invalid('the network refused the transaction: Blockhash not found');
    return land(tx);
  };
  const { page, wallet } = await openPage(B, r.base, INVITE, { device: PHONE, setup: (ctx) => solanaWallet(ctx, OWNER, async (tx) => r.chain.land(tx)) });
  try {
    await connect(page);
    await page.fill('#c-amount', '2');
    await page.fill('#c-name', 'slow-pool');
    await click(page, '#c-go');
    const f = await flowDone(page);
    assert.deepEqual(f.steps.map(s => s.state), Array(5).fill('done'), JSON.stringify(f));
    assert.deepEqual(wallet.map(x => [x.kind, x.txs.length]), [['signAll', 2], ['signAll', 1]], 'the set-up, then the funding alone once more');
    assert.equal(refused, 2, 'the old funding refused once; the new one landed');
    assert.equal(r.chain.sol, 1_881_400_000n, 'the deposit, once');
    assert.ok(r.chain.whole());
  } finally { await page.context().close(); r.server.close(); }
});
