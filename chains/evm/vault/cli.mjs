// The operator's tool for EVM vaults (a Safe and a Zodiac Roles module). LPBOT_CHAIN picks
// the chain module (default polygon); the bot's key is WALLET_SECRET_PATH.
//
//   node chains/evm/vault/cli.mjs connect --owners <a>[,<a>] [--threshold 1] --pools <pool>[,<pool>]
//        [--payout <wallet>[,<wallet>]] [--venue uniswap-v3-polygon] [--execute]
//       Creates the Safe with the bot's key as its only owner, adds the Roles module and the
//       bot's role, adds the owners and removes the bot, in three transactions the bot pays.
//       No owner signs anything. Prints one JSON object (ops/add_vault.py register reads it).
//   node chains/evm/vault/cli.mjs status --safe <safe> --roles <roles>
//
// The owners run the Safe with the Safe app (app.safe.global): withdraw is a transfer from
// the Safe; revoke is Settings > Modules > remove the Roles module.
import crypto from 'node:crypto';
import { createPublicClient, createWalletClient, encodeFunctionData, getAddress, http, isAddress } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { readKey } from '../keyfile.mjs';
import { chainModule } from '../chains.mjs';
import { isEntry } from '../../../shared/rpc_policy.mjs';
import { assertNotHalted } from '../../../shared/halt_guard.mjs';
import { BOT_ROOT } from '../../../bot_root.mjs';
import { compile } from './roles.mjs';
import { wrapStep } from './account.mjs';
import { checkOwners, connectProblems, execAsOwner, MODULE_FACTORY_ABI, readState, rolesSetup, SAFE_FACTORY_ABI, safeSetup, setupCalls } from './safe.mjs';

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

// The role this vault gets, before anything is sent.
export async function plan(f, { pub, U, account }) {
  const threshold = Number(f.threshold ?? 1);
  const owners = checkOwners(addresses(f.owners, '--owners'), threshold, [account.address]);
  const pools = await Promise.all(addresses(f.pools, '--pools').map(p => poolOf(pub, U, p)));
  if (!pools.length) throw new Error('--pools: at least one approved pool');
  const payouts = addresses(f.payout, '--payout');
  const venue = f.venue ?? U.DEX;
  const compiled = compile(venue, { npm: U.V3.npm, router: U.V3.router, stables: U.STABLES, pools, payouts });
  return { owners, threshold, pools, payouts, venue, compiled };
}

async function sent(pub, hash, what) {
  const r = await pub.waitForTransactionReceipt({ hash, timeout: 180_000 });
  if (r.status !== 'success') throw new Error(`${what} (${hash}) reverted`);
  return r;
}

export async function connect(f, deps = {}) {
  const { U, account, pub, wallet } = deps.pub ? deps : clients(deps.env);
  const p = await plan(f, { pub, U, account });
  const bot = account.address;
  const report = { kind: 'safe', chain: U.CHAIN, venue: p.venue, owners: p.owners, threshold: p.threshold, payer: bot, policyKey: bot,
    pools: p.pools.map(x => x.pool), payouts: p.payouts, role: p.compiled.role, scopes: p.compiled.functions.length, lookup: null };
  if (!f.execute) {
    console.log(JSON.stringify({ ...report, sent: false, functions: p.compiled.functions.map(x => `${x.name} @ ${x.target}`) }, null, 1));
    return report;
  }
  assertNotHalted(BOT_ROOT, process.env);
  const salt = BigInt(`0x${crypto.randomBytes(16).toString('hex')}`);
  const V = U.VAULT, receipts = [];
  // 1. the Safe, the bot its only owner
  const create = { address: V.safeFactory, abi: SAFE_FACTORY_ABI, functionName: 'createProxyWithNonce', args: [V.safeSingleton, safeSetup(bot, V.fallbackHandler), salt], account };
  const { result: safe } = await pub.simulateContract(create);
  receipts.push(await sent(pub, await wallet.writeContract(create), 'create the Safe'));
  // 2. the Roles module, owned by the Safe and acting on it
  const mod = { address: V.moduleFactory, abi: MODULE_FACTORY_ABI, functionName: 'deployModule', args: [V.rolesMastercopy, rolesSetup(safe), salt], account };
  const { result: roles } = await pub.simulateContract(mod);
  receipts.push(await sent(pub, await wallet.writeContract(mod), 'deploy the Roles module'));
  // 3. one Safe transaction: module, role, owners, the bot leaves
  const calls = setupCalls({ safe, roles, compiled: p.compiled, bot, owners: p.owners, threshold: p.threshold });
  const data = execAsOwner({ multiSendCallOnly: V.multiSendCallOnly, calls, bot });
  await pub.call({ account, to: safe, data });
  receipts.push(await sent(pub, await wallet.sendTransaction({ to: safe, data }), 'set the vault up'));
  const problems = connectProblems(await readState(pub, safe, roles), { safe, roles, owners: p.owners, threshold: p.threshold, botKeys: [bot] });
  // the role works: the bot may approve the position manager, not a stranger
  const vault = { safe, roles, role: p.compiled.role };
  const probe = async (spender) => {
    const step = wrapStep({ label: 'probe', to: p.pools[0].token0, data: encodeFunctionData({ abi: U.ERC20_ABI, functionName: 'approve', args: [spender, 0n] }) }, vault);
    try { await pub.call({ account, to: step.to, data: step.data }); return true; } catch { return false; }
  };
  if (!await probe(U.V3.npm)) problems.push('the role refuses an approve of the position manager');
  if (await probe(bot)) problems.push('the role allows an approve to the bot\'s own key');
  const gas = receipts.map(r => Number(r.gasUsed));
  const wei = receipts.reduce((a, r) => a + r.gasUsed * r.effectiveGasPrice, 0n);
  const out = { ...report, sent: true, vault: safe, settings: safe, roles, policies: [roles], gas, cost: Number(wei) / 1e18,
    signatures: receipts.map(r => r.transactionHash), problems };
  console.log(JSON.stringify(out, null, 1));
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
  if (cmd === 'connect') return connect(f);
  if (cmd === 'status') return status(f);
  console.log('commands: connect --owners <a,..> [--threshold n] --pools <p,..> [--payout <w,..>] [--venue v] [--execute] | status --safe <s> --roles <r>');
}

if (isEntry(import.meta.url)) {
  main().catch(e => { console.error('ERROR:', String(e?.shortMessage ?? e?.message ?? e).split('\n')[0]); process.exitCode = 1; });
}
