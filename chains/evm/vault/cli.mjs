// The operator's tool for EVM vaults (a Safe and a Zodiac Roles module). LPBOT_CHAIN picks
// the chain module (default polygon); the bot's key is WALLET_SECRET_PATH (its address only:
// the bot sends nothing here).
//
//   node chains/evm/vault/cli.mjs request create --owner <a> --pools <pool>[,<pool>]
//        [--payout <wallet>[,<wallet>]] [--venue uniswap-v3-polygon] [--nonce <n>]
//       The owner's transactions that create its own vault (owner_setup.mjs): the Safe with
//       the owner alone, then the Roles module and the bot's role. The owner's wallet sends
//       them and pays their gas; the bot is never an owner. --nonce n (default 0): the owner's
//       n-th vault, so a resume prints the same addresses. Prints one JSON object.
//   node chains/evm/vault/cli.mjs check --owner <a> --safe <s> --roles <r> --pools <pool>[,<pool>]
//        [--payout <wallet>[,<wallet>]] [--venue v] --from-block <n>
//       After the owner sent them: the vault as planned (verifySetup), from block n (at or
//       before the setup). Prints the JSON ops/add_vault.py register reads; exit 1 on a problem.
//   node chains/evm/vault/cli.mjs status --safe <safe> --roles <roles>
//   node chains/evm/vault/cli.mjs keys <file>
//       a new bot key (0600, in a directory only its user can read). Prints its address;
//       the owner sends gas (POL) to it. One key can serve every vault (onboard/).
//
// The owners run the Safe with the Safe app (app.safe.global): withdraw is a transfer from
// the Safe; revoke is Settings > Modules > remove the Roles module.
import crypto from 'node:crypto';
import { createPublicClient, createWalletClient, getAddress, http, isAddress } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { readKey, writeNewKey } from '../keyfile.mjs';
import { chainModule } from '../chains.mjs';
import { isEntry } from '../../../shared/rpc_policy.mjs';
import { compile } from './roles.mjs';
import { checkOwners, readState } from './safe.mjs';
import { ownerSetupCalls, saltOf, verifySetup } from './owner_setup.mjs';

function flags(argv) {
  const f = {};
  for (let i = 0; i < argv.length; i++) {
    if (!argv[i].startsWith('--')) continue;
    const k = argv[i].slice(2);
    f[k] = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true;
  }
  return f;
}

const addresses = (s, what) => String(s ?? '').split(',').map(x => x.trim()).filter(Boolean).map(a => {
  if (!isAddress(a, { strict: false })) throw new Error(`${what}: ${a} is not an address`);
  return getAddress(a);
});

export function clients(env = process.env) {
  const U = chainModule(env.LPBOT_CHAIN || 'polygon');
  if (!U.VAULT) throw new Error(`${U.NAME} has no vault contracts in its chain module`);
  const url = U.endpoints(env)[0];
  const account = privateKeyToAccount(readKey(env.WALLET_SECRET_PATH));
  const transport = http(url, { retryCount: 1, timeout: 30_000 });
  return { U, account, pub: createPublicClient({ chain: U.VIEM_CHAIN, transport }),
    wallet: createWalletClient({ chain: U.VIEM_CHAIN, transport, account }) };
}

// A pool of the chain's v3 factory: its tokens and fee, checked both ways.
export async function poolOf(pub, U, pool) {
  const read = (functionName) => pub.readContract({ address: pool, abi: U.POOL_ABI, functionName });
  const [factory, token0, token1, fee] = await Promise.all([read('factory'), read('token0'), read('token1'), read('fee')]);
  const back = await pub.readContract({ address: U.V3.factory, abi: U.FACTORY_ABI, functionName: 'getPool', args: [token0, token1, fee] });
  if (factory.toLowerCase() !== U.V3.factory.toLowerCase() || back.toLowerCase() !== pool.toLowerCase()) {
    throw new Error(`${pool} is not a pool of the Uniswap v3 factory ${U.V3.factory}`);
  }
  return { pool, token0, token1, fee };
}

// The role this vault gets, before anything is sent: one owner (never the bot's key).
export async function plan(f, { pub, U, bot }) {
  const list = addresses(f.owner, '--owner');
  if (list.length > 1) throw new Error('--owner: one owner, whose vault it is');
  const [owner] = checkOwners(list, 1, [bot]);
  const pools = await Promise.all(addresses(f.pools, '--pools').map(p => poolOf(pub, U, p)));
  if (!pools.length) throw new Error('--pools: at least one approved pool');
  const payouts = addresses(f.payout, '--payout');
  const venue = f.venue ?? U.DEX;
  const compiled = compile(venue, { npm: U.V3.npm, router: U.V3.router, stables: U.STABLES, pools, payouts });
  return { owner, pools, payouts, venue, compiled };
}

// What ops/add_vault.py register (and onboard/store.py) reads of a Safe vault.
export function vaultReport({ U, p, safe, roles, bot }) {
  return { sent: true, kind: 'safe', chain: U.CHAIN, venue: p.venue, vault: safe, settings: safe, owners: [p.owner], threshold: 1,
    roles, policies: [roles], pools: p.pools.map(x => x.pool), payouts: p.payouts, role: p.compiled.role, payer: bot, policyKey: bot, lookup: null };
}

// The owner's transactions for a new vault. The Roles module is left out of them when it
// exists already (anyone may deploy it; it is the Safe's all the same).
export async function request(f, deps = {}) {
  const { U, account, pub } = deps.pub ? deps : clients(deps.env);
  const log = deps.log ?? console.log;
  const bot = account.address;
  const p = await plan(f, { pub, U, bot });
  const n = f.nonce === undefined ? 0 : Number(f.nonce);
  const saltNonce = saltOf(p.owner, n);
  const plain = ownerSetupCalls({ U, owner: p.owner, bot, compiled: p.compiled, saltNonce });
  const [safeCode, rolesCode] = await Promise.all([pub.getCode({ address: plain.safe }), pub.getCode({ address: plain.roles })]);
  const s = rolesCode && rolesCode !== '0x' ? ownerSetupCalls({ U, owner: p.owner, bot, compiled: p.compiled, saltNonce, rolesDeployed: true }) : plain;
  const created = Boolean(safeCode && safeCode !== '0x');
  const out = { owner: p.owner, chainId: U.CHAIN_ID, safe: s.safe, roles: s.roles, nonce: n, saltNonce: String(saltNonce),
    fromBlock: String(await pub.getBlockNumber({ cacheTime: 0 })), created,
    transactions: created ? s.txs.slice(1) : s.txs, functions: p.compiled.functions.map(x => `${x.name} @ ${x.target}`) };
  log(JSON.stringify(out, null, 1));
  return out;
}

// After the owner sent them: the register JSON, with the problems verifySetup finds.
export async function check(f, deps = {}) {
  const { U, account, pub } = deps.pub ? deps : clients(deps.env);
  const log = deps.log ?? console.log;
  const bot = account.address;
  const p = await plan(f, { pub, U, bot });
  const [safe] = addresses(f.safe, '--safe'), [roles] = addresses(f.roles, '--roles');
  if (!/^\d+$/.test(String(f['from-block'] ?? ''))) throw new Error('--from-block: the block number `request create` printed');
  const problems = await verifySetup(pub, { U, safe, roles, owner: p.owner, bot, compiled: p.compiled, fromBlock: BigInt(f['from-block']) });
  const out = { ...vaultReport({ U, p, safe, roles, bot }), sent: problems.length === 0, problems };
  log(JSON.stringify(out, null, 1));
  if (problems.length) process.exitCode = 1;
  return out;
}

export async function status(f, deps = {}) {
  const { pub } = deps.pub ? deps : clients(deps.env);
  const [safe] = addresses(f.safe, '--safe'), [roles] = addresses(f.roles, '--roles');
  const s = await readState(pub, safe, roles);
  const out = { safe, roles, owners: s.owners, threshold: s.threshold, modules: s.modules, guard: s.guard,
    rolesOwner: s.rolesOwner, avatar: s.avatar, target: s.target,
    botActive: s.modules.some(m => m.toLowerCase() === roles.toLowerCase()) };
  console.log(JSON.stringify(out, null, 1));
  return out;
}

async function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  const f = flags(rest);
  if (cmd === 'request' && rest[0] === 'create') return request(f);
  if (cmd === 'check') return check(f);
  if (cmd === 'status') return status(f);
  if (cmd === 'keys') {
    const file = rest[0];
    if (!file || file.startsWith('--')) throw new Error('keys <file>: where to write the new key');
    writeNewKey(file, `0x${crypto.randomBytes(32).toString('hex')}`);
    return console.log(JSON.stringify({ file, address: privateKeyToAccount(readKey(file)).address,
      next: 'send gas (POL) to this address; it pays the gas of every vault it runs' }));
  }
  console.log('commands: request create --owner <a> --pools <p,..> [--payout <w,..>] [--venue v] [--nonce n] '
    + '| check --owner <a> --safe <s> --roles <r> --pools <p,..> [--payout <w,..>] [--venue v] --from-block <n> '
    + '| status --safe <s> --roles <r> | keys <file>');
}

if (isEntry(import.meta.url)) {
  main().catch(e => { console.error('ERROR:', String(e?.shortMessage ?? e?.message ?? e).split('\n')[0]); process.exitCode = 1; });
}
