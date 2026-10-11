// The self-serve onboarding server behind romefeller.app/bot. Caddy proxies
// /bot* here; the server listens on 127.0.0.1 only.
//
//   node onboard/server.mjs [onboard/onboard.json]
//
// An owner opens the page, connects a wallet, fills chain, amount, profit
// wallet and name, and signs ONE message. With that signature the bot creates
// one vault the owner alone owns (the bot pays; the owner signs nothing on
// chain for it), registers it and one new profile copied from the chain's
// template. The owner's wallet then deposits; once the deposit is in the
// vault the profile's service starts. The page also lists the owner's vaults,
// with withdraw and revoke built for the owner's wallet to sign.
//
// What it never does: hold an owner's key, send a transaction the owner did
// not sign (other than the bot's own connect), spend anything without the
// invite and a valid signature, change a profile or wallet it did not create.
//
// Environment: LPBOT_DSN (the database), the RPC variables the chain modules
// read (LPBOT_RPC / SOLANA_RPC_URL, LPBOT_POLYGON_RPC), the key PATHS the
// config names (payerKeyEnv, policyKeyEnv, keyEnv) and the invite hashes
// (inviteEnv: comma-separated SHA-256 hex of the invite codes).
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'module';
import * as I from './intent.mjs';
import * as S from './solana.mjs';
import * as P from './polygon.mjs';
import { isEntry } from '../shared/rpc_policy.mjs';

const require = createRequire(import.meta.url);
const { Connection } = require('@solana/web3.js');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STORE = path.join(HERE, 'store.py');

// --- the config ---------------------------------------------------------------

export function loadConfig(file = path.join(HERE, 'onboard.json')) {
  const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
  const chains = {};
  if (raw.chains.solana) chains.solana = S.resolve(raw.chains.solana);
  if (raw.chains.polygon) chains.polygon = P.resolve(raw.chains.polygon);
  if (!(raw.fee.bps >= 0 && raw.fee.bps <= 5000)) throw new Error(`fee.bps ${raw.fee.bps}`);
  if (!raw.service.start.some(a => a.includes('{name}'))) throw new Error('service.start must name {name}');
  if (!['127.0.0.1', '::1'].includes(raw.listen.host)) throw new Error(`listen.host ${raw.listen.host}: loopback only (Caddy proxies)`);
  return { ...raw, chains };
}

// --- rate limits ----------------------------------------------------------------

// Events per key over a sliding window. Bounded: at most `keys` keys, the
// stalest dropped first.
export class Windows {
  constructor({ max, windowS }, now = () => Date.now(), keys = 10_000) {
    this.max = max; this.ms = windowS * 1000; this.now = now; this.keys = keys; this.seen = new Map();
  }

  recent(key) {
    const t = this.now(), list = (this.seen.get(key) ?? []).filter(x => t - x < this.ms);
    if (list.length) this.seen.set(key, list); else this.seen.delete(key);
    return list;
  }

  full(key) { return this.recent(key).length >= this.max; }

  add(key) {
    const list = this.recent(key);
    list.push(this.now());
    this.seen.delete(key);
    this.seen.set(key, list);
    while (this.seen.size > this.keys) this.seen.delete(this.seen.keys().next().value);
  }
}

// --- the database (onboard/store.py) and the service -------------------------------

export function run(cmd, args, { input = '', timeoutMs = 120_000, env = process.env } = {}) {
  return new Promise((resolve) => {
    const p = spawn(cmd, args, { env, stdio: ['pipe', 'pipe', 'pipe'] });
    let out = '', err = '';
    const timer = setTimeout(() => p.kill('SIGKILL'), timeoutMs);
    p.stdout.on('data', d => { out += d; });
    p.stderr.on('data', d => { err += d; });
    p.on('error', e => { clearTimeout(timer); resolve({ code: -1, out, err: String(e.message) }); });
    p.on('close', code => { clearTimeout(timer); resolve({ code, out, err }); });
    p.stdin.end(input);
  });
}

export class StoreError extends Error {}

export function pythonStore(env = process.env) {
  return async (command, request) => {
    const r = await run('python3', [STORE, command], { input: JSON.stringify(request), env });
    const last = r.out.trim().split('\n').pop() || '';
    let j = null;
    try { j = JSON.parse(last); } catch { /* below */ }
    if (j?.error) throw new StoreError(j.error);
    if (r.code !== 0 || !j) throw new Error(`store ${command} failed: ${(r.err.trim().split('\n').pop() || 'no output').slice(0, 300)}`);
    return j;
  };
}

// The command with {name} replaced; the name is checked again here.
export function serviceCommand(template, name) {
  if (!I.NAME.test(name) || name.includes('--')) throw new Error(`refused: profile name ${JSON.stringify(name)}`);
  return template.map(a => a.replaceAll('{name}', name));
}

// --- HTTP ----------------------------------------------------------------------------

class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

const PAGE_HEADERS = {
  'content-security-policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
    + "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  'x-content-type-options': 'nosniff',
  'referrer-policy': 'no-referrer',
  'cache-control': 'no-store',
};

function send(res, status, body, type = 'application/json; charset=utf-8') {
  const data = typeof body === 'string' ? body : JSON.stringify(body);
  res.writeHead(status, { ...PAGE_HEADERS, 'content-type': type, 'content-length': Buffer.byteLength(data) });
  res.end(data);
}

async function readJson(req, limit) {
  if (!String(req.headers['content-type'] ?? '').startsWith('application/json')) throw new HttpError(415, 'send JSON');
  let size = 0;
  const chunks = [];
  for await (const c of req) {
    size += c.length;
    if (size > limit) throw new HttpError(413, 'request too large');
    chunks.push(c);
  }
  try {
    const v = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
    if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error('not an object');
    return v;
  } catch { throw new HttpError(400, 'the body is not a JSON object'); }
}

// The client's address. Caddy (the only thing that reaches 127.0.0.1) sets
// X-Forwarded-For to the address it saw.
export function clientOf(req) {
  const peer = req.socket.remoteAddress ?? '';
  const fwd = String(req.headers['x-forwarded-for'] ?? '').split(',')[0].trim();
  return /^(::ffff:)?127\.|^::1$/.test(peer) && fwd ? fwd : peer;
}

// The server. `deps`: {chains: {solana: adapter, polygon: adapter}, store, start, status, now, log}.
export function makeServer(cfg, deps) {
  const now = deps.now ?? (() => Date.now());
  const log = deps.log ?? ((o) => console.log(JSON.stringify({ t: new Date(now()).toISOString(), ...o })));
  const L = cfg.limits;
  const limits = { requests: new Windows(L.requests, now), badInvites: new Windows(L.badInvites, now),
    creates: new Windows(L.creates, now), createsTotal: new Windows(L.createsTotal, now) };
  const intents = new Map();              // nonce -> {fields, text, expires}
  const hashes = String(deps.inviteHashes ?? '').split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
  const page = fs.readFileSync(path.join(HERE, 'index.html'), 'utf8');
  const app = fs.readFileSync(path.join(HERE, 'app.js'), 'utf8');
  const style = fs.readFileSync(path.join(HERE, 'style.css'), 'utf8');
  let queue = Promise.resolve();          // one connect at a time: one payer, one seed, one nonce
  const serial = (fn) => { const p = queue.then(fn, fn); queue = p.catch(() => {}); return p; };

  const chainOf = (name) => {
    const a = I.own(deps.chains, name);
    if (!a) throw new HttpError(400, `chain ${name} is not open here`);
    return a;
  };

  function gate(ip, body) {
    if (limits.badInvites.full(ip)) throw new HttpError(429, 'too many wrong invite codes; wait an hour');
    if (!I.inviteOk(body.invite, hashes)) {
      limits.badInvites.add(ip);
      throw new HttpError(403, 'this page needs a valid invite code');
    }
  }

  function prune() {
    const t = now();
    for (const [k, v] of intents) if (v.expires <= t) intents.delete(k);
  }

  async function checkRoom(f) {
    const wallet = f.name + I.WALLET_SUFFIX;
    if ((await deps.store('taken', { name: f.name, wallet_id: wallet })).taken) throw new HttpError(409, `the name ${f.name} is taken`);
    if ((await deps.store('owned', { owner: f.owner })).count >= L.vaultsPerOwner) {
      throw new HttpError(403, `this wallet already owns ${L.vaultsPerOwner} vaults, the most allowed`);
    }
  }

  // POST /api/intent: the message to sign, under a fresh nonce. It names the
  // gas the owner pays the bot's key, that key, and the deposit after it.
  async function intent(ip, body) {
    gate(ip, body);
    const adapter = chainOf(body.chain);
    const f = I.fields(body, cfg, adapter.keys.public);
    if (!adapter.venueReady()) throw new HttpError(503, `no venue on ${body.chain} can hold a vault yet`);
    await checkRoom(f);
    prune();
    if (intents.size >= L.openIntents) throw new HttpError(503, 'too many open requests; try again in a few minutes');
    const nonce = I.newNonce(), expires = now() + cfg.intentTtlS * 1000;
    const text = I.message(f, cfg.chains[f.chain], { site: cfg.site, nonce, expires, payer: adapter.keys.payee });
    intents.set(nonce, { fields: f, text, expires });
    return { nonce, message: text, expires: new Date(expires).toISOString(), gas: f.gas, gasToken: f.gasToken, deposit: f.deposit };
  }

  // POST /api/sign: the signed message. Kept in the database with the values
  // it names (only those are ever acted on); the answer is the gas payment
  // for the owner's wallet to sign and send.
  async function sign(ip, body) {
    gate(ip, body);
    const nonce = I.text(body.nonce);
    const it = intents.get(nonce);
    intents.delete(nonce);                // one try per nonce, good or bad
    if (!it) throw new HttpError(400, 'unknown or used request; sign a new one');
    if (it.expires <= now()) throw new HttpError(400, 'the request expired; sign a new one');
    if (body.message !== it.text) throw new HttpError(400, 'the signed message is not the one this server issued');
    const f = it.fields;
    if (!await I.verify(f.chain, f.owner, it.text, body.signature)) throw new HttpError(401, 'the signature is not the owner\'s');
    if (limits.creates.full(ip) || limits.createsTotal.full('all')) throw new HttpError(429, 'too many new vaults today; try tomorrow');
    const adapter = chainOf(f.chain);
    await checkRoom(f);
    // before the row: a wallet that cannot pay the gas and the deposit holds no name
    const payment = await adapter.payment({ owner: f.owner, payer: adapter.keys.payee, gas: f.gas, token: f.token, deposit: f.deposit });
    limits.creates.add(ip); limits.createsTotal.add('all');
    await deps.store('sign', { chain: f.chain, owner: f.owner, name: f.name, wallet_id: f.name + I.WALLET_SUFFIX, token: f.token,
      amount: f.amount, deposit: f.deposit, gas: f.gas, payer: adapter.keys.payee, profit_wallet: f.profitWallet, special: f.special,
      fee_bps: f.feeBps, fee_wallet: f.feeWallet, intent: it.text, signature: String(body.signature), nonce, expires: it.expires });
    log({ event: 'signed', chain: f.chain, name: f.name, owner: f.owner });
    return { name: f.name, ...payment };
  }

  const running = new Set();              // names whose setup runs in this process

  // POST /api/pay: the gas payment's signature (Solana) or hash (EVM). The
  // bot spends nothing until it is on chain: from the owner, to the bot's key,
  // at least the gas, confirmed, newer than the request, never used before.
  // A paid request whose setup stopped resumes here, with its own payment.
  async function pay(ip, body) {
    gate(ip, body);
    const name = I.name(body.name);
    // claimed before the first await: two calls for one request never both run
    if (running.has(name)) throw new HttpError(409, 'the setup of this vault is running; wait');
    running.add(name);
    try { return await payOrResume(name, body); } finally { running.delete(name); }
  }

  async function payOrResume(name, body) {
    const r = await requestOf(name, false);
    const adapter = chainOf(r.chain);
    if (r.state === 'registered' || r.state === 'started') return { name: r.name, state: r.state, vault: r.vault };
    if (r.state === 'signed') {
      const payment = I.text(body.payment);
      const v = await adapter.checkPayment(payment, { owner: r.owner, payer: r.payer, amount: r.gas,
        notBefore: Math.floor(r.created_at) - cfg.paymentSlackS });
      if (v.state === 'pending') throw new HttpError(409, `${v.reason}; the page asks again`);
      if (v.state !== 'ok') {
        log({ event: 'payment_refused', name: r.name, reason: v.reason });
        throw new HttpError(400, v.reason);
      }
      await deps.store('paid', { id: r.id, payment });
      log({ event: 'paid', chain: r.chain, name: r.name, payment });
    } else {
      await deps.store('resume', { id: r.id });
      log({ event: 'resume', chain: r.chain, name: r.name });
    }
    return setup(r.name, adapter);
  }

  // Connect (unless an earlier try did), then register. One at a time: one
  // payer, one seed, one nonce. The row is read inside the turn, so a vault an
  // earlier try created is never connected again. A failure leaves the
  // request stuck, resumable.
  function setup(name, adapter) {
    return serial(async () => {
      const r = await requestOf(name, false);
      if (r.state !== 'connecting') throw new HttpError(409, `the request is ${r.state}, not being set up`);
      let connected = r.connected?.sent ? r.connected : null;
      try {
        if (!connected) {
          const g = await adapter.gas();
          if (!g.ok) throw new Error(`the bot's key holds ${g.have} ${g.symbol}, less than one setup (${g.need})`);
          log({ event: 'connect', chain: r.chain, name: r.name, owner: r.owner });
          // the plan is saved before anything is sent, so a resume finishes this vault, never a second one
          connected = await adapter.connect({ owner: r.owner, profitWallet: r.profit_wallet, feeWallet: r.fee_wallet,
            planned: r.connected?.planned ?? null,
            onPlan: (planned) => deps.store('connected', { id: r.id, connected: { planned } }) });
          await deps.store('connected', { id: r.id, connected });
        }
        const venue = connected.venue, pool = connected.pools[0];
        const reg = await deps.store('register', { id: r.id, wallet_id: r.wallet_id, label: r.name, secret_env: adapter.keys.secretEnv,
          policy_key_env: adapter.keys.policyKeyEnv, connected,
          profile: { name: r.name, template: cfg.chains[r.chain].template, settings: cfg.chains[r.chain].settings ?? {}, dex: venue, pool, execute_dexes: [venue],
            rec: await adapter.record(venue, pool) } });
        log({ event: 'registered', chain: r.chain, name: r.name, vault: reg.vault });
        return { name: r.name, chain: r.chain, state: 'registered', vault: reg.vault, token: r.token, deposit: r.deposit };
      } catch (e) {
        connected ??= e.connected ?? null;
        await deps.store('stuck', { id: r.id, error: e.message, connected }).catch(() => {});
        log({ event: 'setup_stuck', chain: r.chain, name: r.name, error: e.message, vault: connected?.vault ?? null });
        throw new HttpError(502, `the setup stopped: ${e.message}. Your gas payment is kept: press Resume (nothing is paid twice)`
          + (connected?.sent ? `. The vault ${connected.vault} exists; do not deposit before it shows as waiting for a deposit` : ''));
      }
    });
  }

  async function requestOf(name, registered = true) {
    const r = (await deps.store('get', { name: I.name(name) })).request;
    if (!r || (registered && !r.vault_row)) throw new HttpError(404, `no vault named ${name}`);
    return r;
  }

  // POST /api/tx: an owner's transaction, unsigned. The deposit is the signed amount.
  // A chain whose transactions expire (Solana: a blockhash lives about a minute)
  // answers one at a time, `index` of `count`, each built when it is asked for.
  async function tx(ip, body) {
    gate(ip, body);
    const r = await requestOf(body.name);
    const adapter = chainOf(r.chain);
    const action = I.text(body.action);
    let answer;
    if (action === 'deposit') answer = await adapter.deposit({ owner: r.owner, vault: r.vault, token: r.token, amount: r.deposit });
    else if (action === 'withdraw' || action === 'revoke') {
      const amount = I.decimal(body.amount);
      if (action === 'withdraw' && amount !== 'all' && !/^\d{1,12}(\.\d{1,18})?$/.test(amount)) throw new HttpError(400, 'amount: a number or all');
      answer = await adapter.owner(r.vault_row, { action, owner: r.owner, token: I.text(body.token), amount });
    } else throw new HttpError(400, 'action: deposit, withdraw or revoke');
    const count = answer.transactions.length;
    if (!adapter.expires) return { ...answer, count };
    const index = body.index === undefined ? 0 : body.index;
    if (!(Number.isInteger(index) && index >= 0 && index < count)) throw new HttpError(400, `index: 0 to ${count - 1}`);
    return { ...answer, count, index, transactions: [answer.transactions[index]] };
  }

  // POST /api/start: the profile's service, once the vault holds a deposit.
  async function start(ip, body) {
    gate(ip, body);
    const r = await requestOf(body.name);
    const st = await chainOf(r.chain).status(r.vault_row);
    if (!chainOf(r.chain).hasDeposit(st)) throw new HttpError(409, 'the vault holds nothing yet; deposit first');
    if (!st.botActive) throw new HttpError(409, 'the bot is revoked on this vault');
    const out = await deps.start(serviceCommand(cfg.service.start, r.name));
    if (out.code !== 0) {
      log({ event: 'start_failed', name: r.name, error: out.err.trim().slice(0, 300) });
      throw new HttpError(502, 'the bot could not be started; the operator is told');
    }
    await deps.store('started', { id: r.id });
    log({ event: 'started', name: r.name });
    return { name: r.name, started: true };
  }

  // POST /api/vaults: the owner's vaults and what each holds.
  async function vaults(ip, body) {
    gate(ip, body);
    const chain = I.text(body.chain);
    const owner = I.address(chain, body.owner, 'owner');
    const rows = (await deps.store('list', { owner })).requests.filter(r => r.chain === chain);
    const out = [];
    for (const r of rows) {
      const v = { name: r.name, chain: r.chain, state: r.state, vault: r.vault, token: r.token, amount: r.amount, deposit: r.deposit,
        gas: r.gas, payer: r.payer, error: r.state === 'stuck' ? r.error : undefined, created: r.created_at };
      if (r.vault_row) {
        try { Object.assign(v, await chainOf(r.chain).status(r.vault_row)); } catch (e) { v.error = `chain read failed: ${e.message}`; }
        const s = await deps.status(serviceCommand(cfg.service.status, r.name));
        v.service = s.out.trim() || 'unknown';
      }
      out.push(v);
    }
    return { vaults: out };
  }

  function publicConfig() {
    return { site: cfg.site, feeBps: cfg.fee.bps,
      chains: Object.fromEntries(Object.keys(deps.chains).map(k => {
        const c = cfg.chains[k];
        return [k, { label: c.label, pair: c.pair, template: c.template, payoutToken: c.payoutToken, operatorWallet: c.operatorWallet,
          gas: I.fromRaw(I.gasTotal(c), I.nativeOf(c)[1].decimals), gasToken: I.nativeOf(c)[0],
          tokens: Object.fromEntries(Object.entries(c.tokens).map(([s, t]) => [s, { min: I.minimum(c, s), max: t.max, decimals: t.decimals,
            native: Boolean(t.native) }])) }];
      })) };
  }

  const routes = { '/api/intent': intent, '/api/sign': sign, '/api/pay': pay, '/api/tx': tx, '/api/start': start, '/api/vaults': vaults };

  return http.createServer(async (req, res) => {
    const ip = clientOf(req);
    let url;
    try { url = new URL(req.url, 'http://x'); } catch { return send(res, 400, { error: 'bad URL' }); }
    const p = url.pathname;
    if (p !== cfg.base && !p.startsWith(cfg.base + '/')) return send(res, 404, { error: 'not found' });
    const sub = p.slice(cfg.base.length) || '/';
    try {
      if (limits.requests.full(ip)) throw new HttpError(429, 'too many requests; wait a few minutes');
      limits.requests.add(ip);
      if (req.method === 'GET' || req.method === 'HEAD') {
        // the page's links are relative: /bot must be /bot/
        if (p === cfg.base) { res.writeHead(308, { location: `${cfg.base}/` }); return res.end(); }
        if (sub === '/') return send(res, 200, page, 'text/html; charset=utf-8');
        if (sub === '/app.js') return send(res, 200, app, 'text/javascript; charset=utf-8');
        if (sub === '/style.css') return send(res, 200, style, 'text/css; charset=utf-8');
        if (sub === '/api/config') return send(res, 200, publicConfig());
        return send(res, 404, { error: 'not found' });
      }
      const fn = req.method === 'POST' && routes[sub];
      if (!fn) return send(res, 404, { error: 'not found' });
      const body = await readJson(req, L.bodyBytes);
      return send(res, 200, await fn(ip, body));
    } catch (e) {
      if (e instanceof HttpError) return send(res, e.status, { error: e.message });
      if (e instanceof I.Invalid || e instanceof StoreError) return send(res, 400, { error: e.message });
      log({ event: 'error', path: sub, error: String(e?.message ?? e).slice(0, 500) });
      return send(res, 500, { error: 'the server failed; nothing more was done' });
    }
  });
}

// --- the chains as the server sees them -------------------------------------------------

export async function solanaAdapter(c, { connection, env = process.env, log }) {
  const keys = await S.botKeys(c, env);
  return { keys, expires: true, venueReady: () => S.vaultVenues(c).length > 0, gas: () => S.gas(connection, keys, c),
    connect: (f) => S.connectVault(connection, keys, c, f, (line) => log({ event: 'connect_report', line: String(line).slice(0, 4000) })),
    record: (venue, pool) => S.poolRecord(connection, venue, pool, c),
    payment: (a) => S.paymentTxs(connection, c, a), checkPayment: (sig, expect) => S.checkPayment(connection, sig, expect),
    deposit: (a) => S.depositTxs(connection, c, a), owner: (row, a) => S.ownerTxs(connection, c, row, keys, a),
    status: (row) => S.status(connection, c, row, keys), hasDeposit: S.hasDeposit };
}

export function polygonAdapter(c, { env = process.env, deps = null, log }) {
  const keys = P.botKeys(c, env, deps);
  return { keys, venueReady: () => c.venues.length > 0, gas: () => P.gas(keys, c),
    connect: (f) => P.connectVault(keys, c, f, (line) => log({ event: 'connect_report', line: String(line).slice(0, 4000) })),
    record: () => P.poolRecord(keys, c),
    payment: (a) => P.paymentTxs(keys, c, a), checkPayment: (hash, expect) => P.checkPayment(keys, c, hash, expect),
    deposit: (a) => P.depositTxs(keys, c, a), owner: (row, a) => P.ownerTxs(keys, c, row, a),
    status: (row) => P.status(keys, c, row), hasDeposit: P.hasDeposit };
}

async function main() {
  const cfg = loadConfig(process.argv[2]);
  const log = (o) => console.log(JSON.stringify({ t: new Date().toISOString(), ...o }));
  const chains = {};
  const rpc = process.env.LPBOT_RPC || process.env.SOLANA_RPC_URL || 'https://api.mainnet-beta.solana.com';
  for (const [name, make] of [['solana', () => solanaAdapter(cfg.chains.solana, { connection: new Connection(rpc, 'confirmed'), log })],
    ['polygon', () => polygonAdapter(cfg.chains.polygon, { log })]]) {
    if (!cfg.chains[name]) continue;
    try { chains[name] = await make(); } catch (e) { log({ event: 'chain_closed', chain: name, reason: e.message }); }
  }
  const hashes = process.env[cfg.inviteEnv] ?? '';
  if (!hashes) log({ event: 'no_invite', reason: `${cfg.inviteEnv} is not set: every request is refused` });
  const server = makeServer(cfg, { chains, store: pythonStore(), inviteHashes: hashes, log,
    start: (cmd) => run(cmd[0], cmd.slice(1), { timeoutMs: 60_000 }), status: (cmd) => run(cmd[0], cmd.slice(1), { timeoutMs: 10_000 }) });
  server.requestTimeout = 300_000;
  server.listen(cfg.listen.port, cfg.listen.host, () => log({ event: 'listening', host: cfg.listen.host, port: cfg.listen.port,
    chains: Object.keys(chains) }));
}

if (isEntry(import.meta.url)) main().catch(e => { console.error('ERROR:', e.message); process.exitCode = 1; });
