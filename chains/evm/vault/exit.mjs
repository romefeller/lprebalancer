// The owner's way out of a Safe vault without the bot: the Uniswap v3 positions the Safe
// holds on a pool, the owner's transactions that close each one (the tokens stay in the
// Safe), and the transaction that gives the bot its role back after a revoke. The owner
// sends each with a pre-validated signature (threshold 1): it works with the Roles module
// disabled and with the bot's key gone.
import { encodeFunctionData, getAddress, maxUint128, maxUint256, toHex } from 'viem';
import { SAFE_ABI, preValidated } from './safe.mjs';

const ZERO = '0x0000000000000000000000000000000000000000';
const same = (a, b) => String(a).toLowerCase() === String(b).toLowerCase();

// One Safe transaction from its sole owner: a CALL (never a delegatecall), no gas refund.
export function ownerExec({ safe, owner, to, data, value = 0n }) {
  return { to: getAddress(safe), value: toHex(0n), data: encodeFunctionData({ abi: SAFE_ABI, functionName: 'execTransaction',
    args: [getAddress(to), value, data, 0, 0n, 0n, 0n, ZERO, ZERO, preValidated(owner)] }) };
}

// The position manager's token ids the Safe owns on `pool`: [{tokenId, liquidity, owed0, owed1}].
export async function vaultPositions(pub, { U, safe, pool }) {
  const read = (address, abi, functionName, args) => pub.readContract({ address, abi, functionName, args });
  const [token0, token1, fee] = await Promise.all(['token0', 'token1', 'fee'].map(f => read(pool, U.POOL_ABI, f)));
  const n = await read(U.V3.npm, U.NPM_ABI, 'balanceOf', [getAddress(safe)]);
  const out = [];
  for (let i = 0n; i < n; i++) {
    const tokenId = await read(U.V3.npm, U.NPM_ABI, 'tokenOfOwnerByIndex', [getAddress(safe), i]);
    const p = await read(U.V3.npm, U.NPM_ABI, 'positions', [tokenId]);
    if (!same(p[2], token0) || !same(p[3], token1) || Number(p[4]) !== Number(fee)) continue;
    out.push({ tokenId, liquidity: p[7], owed0: p[10], owed1: p[11] });
  }
  return out;
}

// The position manager's calls that empty and burn one position, every token to the Safe.
export function closeData(U, safe, { tokenId, liquidity }) {
  const npm = (functionName, args) => encodeFunctionData({ abi: U.NPM_ABI, functionName, args });
  const calls = [];
  // decreaseLiquidity refuses zero liquidity: a position already emptied is only collected
  if (liquidity > 0n) calls.push(npm('decreaseLiquidity', [{ tokenId, liquidity, amount0Min: 0n, amount1Min: 0n, deadline: maxUint256 }]));
  calls.push(npm('collect', [{ tokenId, recipient: getAddress(safe), amount0Max: maxUint128, amount1Max: maxUint128 }]));
  calls.push(npm('burn', [tokenId]));
  return npm('multicall', [calls]);
}

// The owner's transactions that close every position of the Safe on `pool`, one each. [] when none.
export async function ownerCloseCalls(pub, { U, safe, owner, pool }) {
  const held = await vaultPositions(pub, { U, safe, pool });
  return held.map(p => ownerExec({ safe, owner, to: U.V3.npm, data: closeData(U, safe, p) }));
}

// The re-grant after a revoke: the Safe enables its Roles module again (the role is kept in it).
export function regrantCall({ safe, owner, roles }) {
  return ownerExec({ safe, owner, to: safe, data: encodeFunctionData({ abi: SAFE_ABI, functionName: 'enableModule', args: [getAddress(roles)] }) });
}
