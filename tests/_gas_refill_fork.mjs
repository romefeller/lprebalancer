// Shared by test_gas_refill_fork.mjs and test_gas_refill_base_fork.mjs. The gas refill
// (`refill`) of both EVM signers end to end, on anvil forks: Polygon PoS
// (venues/uniswap_v3/signer.mjs, WPOL/USDT0) and Base (venues/aerodrome/signer.mjs,
// WETH/USDC). 2026-10-14: the Polygon wallet's POL fell under the 2 POL reserve while the
// fees arrived as WPOL; every add was refused, after a paid swap each time.
//
// Covered: a swap under the reserve is refused and sends nothing; the refill unwraps the
// wallet's wrapped token to the target exactly; a refill worth less than its gas is refused
// and sends nothing; at the target it is a noop; with the wrapped token short it buys the
// shortfall with the other token first, then unwraps exactly the need; with nothing to
// unwrap, a target not above the reserve or a HALT it refuses and sends nothing.
//
// Safety: LPBOT_RPC is a loopback URL, which the chain modules make the ONLY endpoint, and
// the test asserts the node answers web3_clientVersion "anvil/..." before any --execute.
// The wallets are anvil's public test accounts. Polygon: dRPC first, then publicnode.
import test from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { spawn, spawnSync } from 'node:child_process';
import { encodeFunctionData, parseAbi } from 'viem';
import { WPOL, USDT0 } from '../chains/evm/polygon.mjs';   // token A and B of the Polygon pool

const dir = path.dirname(new URL(import.meta.url).pathname);
const ERC20 = parseAbi(['function transfer(address to, uint256 amount) returns (bool)', 'function balanceOf(address) view returns (uint256)']);
const WRAP = parseAbi(['function deposit() payable']);
const LP = { key: '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80', addr: '0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266' };
const AWAY = '0x90F79bf6EB2c4f870365E785982E1f101E93b906';
const E = 10n ** 18n;
const hex = (n) => `0x${BigInt(n).toString(16)}`;

const CHAINS = {
  polygon: {
    urls: process.env.LPBOT_POLYGON_FORK_URL ? [process.env.LPBOT_POLYGON_FORK_URL] : ['https://polygon.drpc.org', 'https://polygon-bor-rpc.publicnode.com'],
    chainId: '0x89', signer: 'venues/uniswap_v3/signer.mjs', pool: '0x9B08288C3Be4F62bbf8d1C20Ac9C5e6f9467d8B7',
    wrapped: WPOL, other: USDT0, otherDecimals: 6, sym: 'POL', env: { LPBOT_CHAIN: 'polygon' },
    reserve: '1', target: '3',
  },
  base: {
    // Base's own public endpoint first: dRPC's public Base endpoint answers a fork's burst of
    // reads with 429 "Public endpoint rate limit" (2026-10-14), publicnode with a challenge page
    urls: process.env.LPBOT_FORK_URL ? [process.env.LPBOT_FORK_URL] : ['https://mainnet.base.org', 'https://base.drpc.org', 'https://base-rpc.publicnode.com'],
    chainId: '0x2105', signer: 'venues/aerodrome/signer.mjs', pool: '0xb2cc224c1c9feE385f8ad6a55b4d94E92359DC59',
    wrapped: '0x4200000000000000000000000000000000000006', other: '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913', otherDecimals: 6,
    sym: 'ETH', env: {}, reserve: '0.002', target: '0.006',
    l2: true,                     // an OP-stack chain: anvil also charges an L1 data fee the receipt does not show
  },
};

const anvilPath = spawnSync('bash', ['-c', 'command -v anvil'], { encoding: 'utf8' }).stdout.trim();
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'refill-fork-'));
fs.chmodSync(scratch, 0o700);
const keyFile = path.join(scratch, 'lp.secret');
fs.writeFileSync(keyFile, `${LP.key}\n`, { mode: 0o600 });
const nodes = [];

async function freePort() {
  return new Promise(r => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => r(p)); }); });
}

function rpcAt(url) {
  return async (method, params = []) => {
    const res = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) });
    const j = await res.json();
    if (j.error) throw new Error(`${method}: ${j.error.message}`);
    return j.result;
  };
}

// An anvil fork of the first endpoint that answers; null when none does.
async function fork(C) {
  for (const from of C.urls) {
    const port = await freePort();
    const url = `http://127.0.0.1:${port}`;
    const proc = spawn(anvilPath, ['--fork-url', from, '--port', String(port), '--silent', '--no-storage-caching',
      // a public endpoint rate-limits a burst of fork reads: anvil waits and asks again
      '--retries', '12', '--fork-retry-backoff', '2000', '--compute-units-per-second', '200'], { stdio: 'ignore' });
    nodes.push(proc);
    const rpc = rpcAt(url);
    for (let i = 0; i < 120; i++) {
      try { if (/^anvil\//.test(await rpc('web3_clientVersion'))) return { url, rpc, from }; } catch { /* not up yet */ }
      await new Promise(r => setTimeout(r, 500));
    }
    proc.kill();
  }
  return null;
}

test.after(() => {
  for (const p of nodes) p.kill();
  fs.rmSync(scratch, { recursive: true, force: true });
});

async function receipt(rpc, hash) {
  for (let i = 0; i < 40; i++) {
    const rc = await rpc('eth_getTransactionReceipt', [hash]);
    if (rc) return rc;
    await new Promise(r => setTimeout(r, 250));
  }
  throw new Error(`no receipt for ${hash}`);
}

// A transaction from `from` without its key: impersonated on the fork.
async function sendAs(rpc, from, to, data, value = 0n) {
  await rpc('anvil_impersonateAccount', [from]);
  const hash = await rpc('eth_sendTransaction', [{ from, to, data, value: hex(value) }]);
  const rc = await receipt(rpc, hash);
  await rpc('anvil_stopImpersonatingAccount', [from]);
  assert.strictEqual(rc.status, '0x1', 'setup transaction');
}

const balanceOf = async (rpc, token, who) => BigInt(await rpc('eth_call', [{ to: token, data: encodeFunctionData({ abi: ERC20, functionName: 'balanceOf', args: [who] }) }, 'latest']));
const native = async (rpc, who) => BigInt(await rpc('eth_getBalance', [who, 'latest']));
const nonce = async (rpc, who) => rpc('eth_getTransactionCount', [who, 'latest']);

// Registers the fork test of chain `name` ('polygon' or 'base') in the calling test file:
// one chain per file, so a mutation target runs only the fork it needs.
export function refillForkTest(name) {
  const C = CHAINS[name];
  test(`fork (${name}): refill unwraps the wrapped token to the target, buys the shortfall first, refuses the rest`, { timeout: 900_000 }, async (t) => {
    if (!anvilPath) return t.skip('anvil not installed');
    const f = await fork(C);
    if (!f) return t.skip(`anvil could not fork ${C.urls.join(' or ')}`);
    const { url, rpc } = f;
    assert.match(await rpc('web3_clientVersion'), /^anvil\//, 'never --execute against anything but anvil');
    assert.strictEqual(await rpc('eth_chainId'), C.chainId);
    const run = (args, env = {}) => {
      const r = spawnSync('node', [path.join(dir, '..', C.signer), ...args], {
        env: { PATH: process.env.PATH, WALLET_SECRET_PATH: keyFile, LPBOT_RPC: url, LPBOT_POOL: C.pool,
          LPBOT_GAS_RESERVE_NATIVE: C.reserve, LPBOT_GAS_REFILL_TO: C.target, ...C.env, ...env },
        encoding: 'utf8', timeout: 180_000,
      });
      let json = null;
      try { json = JSON.parse(r.stdout.slice(r.stdout.indexOf('{'), r.stdout.lastIndexOf('}') + 1)); } catch { /* none */ }
      return { ...r, json };
    };
    // the native coin after a refill: the target less the gas, exactly (on an L2 the L1 fee is
    // not in the receipt: then at most the target, and at most twice the L2 gas under it)
    const assertGasOnly = (got, want, gas, what) => {
      if (!C.l2) return assert.strictEqual(got, want - gas, what);
      assert.ok(got <= want - gas && want - got <= 2n * gas, `${what}: ${got} for ${want} - ${gas}`);
    };
    const reserve = BigInt(Math.round(Number(C.reserve) * 1e6)) * 10n ** 12n;
    const target = BigInt(Math.round(Number(C.target) * 1e6)) * 10n ** 12n;
    const otherRaw = 50n * 10n ** BigInt(C.otherDecimals);                  // $50 of the stablecoin

    // anvil's public accounts carry an EIP-7702 delegation on these chains (2026-10): a WETH9
    // withdraw pays out with a 2300 gas stipend, which a delegated account cannot take. The
    // bot's wallets are plain accounts (0x53F1...DAA7 has no code): so is the LP here.
    await rpc('anvil_setCode', [LP.addr, '0x']);
    // 0. Funds: the stablecoin from the pool's own balance, the wrapped token by wrapping.
    await rpc('anvil_setBalance', [C.pool, hex(E)]);
    await sendAs(rpc, C.pool, C.other, encodeFunctionData({ abi: ERC20, functionName: 'transfer', args: [LP.addr, otherRaw] }));
    await rpc('anvil_setBalance', [LP.addr, hex(target * 10n)]);
    await sendAs(rpc, LP.addr, C.wrapped, encodeFunctionData({ abi: WRAP, functionName: 'deposit' }), target * 4n);

    // 1. Under the reserve a swap is refused: nothing is sent (the paid swap before a refused add).
    await rpc('anvil_setBalance', [LP.addr, hex(reserve / 2n)]);
    let n0 = await nonce(rpc, LP.addr);
    const sw = run(['rebalance', C.wrapped, C.other, '0', '1000', '--execute']);          // sell the wrapped side
    assert.notStrictEqual(sw.status, 0);
    assert.match(sw.stdout + sw.stderr, /below the .* gas reserve/);
    assert.strictEqual(await nonce(rpc, LP.addr), n0, 'a swap under the reserve sent a transaction');

    // 2. Refill from the wrapped token alone: one unwrap, exactly the need, to the target.
    const before = await native(rpc, LP.addr);
    const wBefore = await balanceOf(rpc, C.wrapped, LP.addr);
    const r1 = run(['refill', '--execute']);
    assert.strictEqual(r1.status, 0, r1.stderr + r1.stdout);
    assert.deepStrictEqual(r1.json.steps, ['unwrap']);
    const need = target - before;
    assert.strictEqual(r1.json.unwrapped, Number(need) / 1e18);
    assert.strictEqual(await balanceOf(rpc, C.wrapped, LP.addr), wBefore - need, 'unwrapped more than the need');
    const rc = await receipt(rpc, r1.json.signature);
    assert.strictEqual(rc.to.toLowerCase(), C.wrapped.toLowerCase());
    const gasPaid = BigInt(rc.gasUsed) * BigInt(rc.effectiveGasPrice) + BigInt(rc.l1Fee ?? 0);
    assertGasOnly(await native(rpc, LP.addr), target, gasPaid, 'native is the target less the unwrap gas');
    assert.ok(BigInt(rc.gasUsed) <= 50_000n, `unwrap gas ${BigInt(rc.gasUsed)} over UNWRAP_GAS`);
    assert.ok(r1.json.unwrappedUsd > 0);

    // 3. Just under the target the refill is worth less than its gas: refused, nothing sent.
    if (gasPaid > 0n) {
      n0 = await nonce(rpc, LP.addr);
      const tiny = run(['refill', '--execute']);
      assert.notStrictEqual(tiny.status, 0);
      assert.match(tiny.stdout, /worth less than 4x its own gas/);
      assert.strictEqual(await nonce(rpc, LP.addr), n0);
    }
    // ... and at or above it, nothing to do.
    await rpc('anvil_setBalance', [LP.addr, hex(target)]);
    const noop = run(['refill', '--execute']);
    assert.strictEqual(noop.status, 0, noop.stderr);
    assert.strictEqual(noop.json.noop, true);

    // 4. The wrapped token short: buy the shortfall with the stablecoin, then unwrap the need.
    const keep = target / 8n;
    const wNow = await balanceOf(rpc, C.wrapped, LP.addr);
    await sendAs(rpc, LP.addr, C.wrapped, encodeFunctionData({ abi: ERC20, functionName: 'transfer', args: [AWAY, wNow - keep] }));
    const low = reserve / 2n;
    await rpc('anvil_setBalance', [LP.addr, hex(low)]);
    const oBefore = await balanceOf(rpc, C.other, LP.addr);
    const r2 = run(['refill', '--execute']);
    assert.strictEqual(r2.status, 0, r2.stderr + r2.stdout);
    assert.deepStrictEqual(r2.json.steps.map(s => s.split(' ')[0]), ['approve', 'swap', 'unwrap']);
    assert.strictEqual(r2.json.unwrapped, Number(target - low) / 1e18, 'unwrapped exactly the need');
    assert.strictEqual(r2.json.fromWrapped, Number(keep) / 1e18);
    const spent = oBefore - await balanceOf(rpc, C.other, LP.addr);
    assert.strictEqual(spent, BigInt(Math.round(r2.json.sold.amount * 10 ** C.otherDecimals)));
    // the stablecoin paid for the shortfall and at most the fee and the slippage over it
    const leftWrapped = await balanceOf(rpc, C.wrapped, LP.addr);
    assert.ok(leftWrapped <= (target - low - keep) * 2n / 100n, `kept ${leftWrapped} wei of the wrapped token: bought too much`);
    let paid = 0n;
    for (const h of r2.json.signatures) { const x = await receipt(rpc, h); paid += BigInt(x.gasUsed) * BigInt(x.effectiveGasPrice) + BigInt(x.l1Fee ?? 0); }
    assertGasOnly(await native(rpc, LP.addr), target, paid, 'native is the target less the gas of the three steps');

    // 5. Nothing to unwrap and nothing to buy it with: refused, nothing sent.
    await sendAs(rpc, LP.addr, C.wrapped, encodeFunctionData({ abi: ERC20, functionName: 'transfer', args: [AWAY, await balanceOf(rpc, C.wrapped, LP.addr)] }));
    await sendAs(rpc, LP.addr, C.other, encodeFunctionData({ abi: ERC20, functionName: 'transfer', args: [AWAY, await balanceOf(rpc, C.other, LP.addr)] }));
    await rpc('anvil_setBalance', [LP.addr, hex(low)]);
    n0 = await nonce(rpc, LP.addr);
    const empty = run(['refill', '--execute']);
    assert.notStrictEqual(empty.status, 0);
    assert.match(empty.stdout, /nothing to unwrap/);
    // 6. A target not above the reserve, and a HALT: refused before anything is built.
    const flat = run(['refill', '--execute'], { LPBOT_GAS_REFILL_TO: C.reserve });
    assert.notStrictEqual(flat.status, 0); assert.match(flat.stderr, /not above the gas reserve/);
    const runDir = path.join(scratch, `run-${name}`);
    fs.mkdirSync(runDir);
    fs.writeFileSync(path.join(runDir, 'HALT'), 'test halt\n');
    const halted = run(['refill', '--execute'], { LPBOT_RUN_DIR: runDir });
    assert.notStrictEqual(halted.status, 0); assert.match(halted.stderr, /HALT present/);
    assert.strictEqual(await nonce(rpc, LP.addr), n0, 'a refused refill sent a transaction');
  });
}
