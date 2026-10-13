// The owner creates its own Safe vault on an anvil fork (chains/evm/vault/cli.mjs request
// create, then check): the owner's wallet sends each transaction and pays its gas; the bot's
// key sends nothing. Shared by the Polygon vault fork tests.
import assert from 'node:assert/strict';
import { check, request } from '../chains/evm/vault/cli.mjs';

// {safe, roles, venue, role, gas: [gas used per owner transaction], fromBlock, report}.
export async function ownerCreatesVault({ U, pub, ownerWallet, bot, pools, payouts = [], nonce = 0 }) {
  const quiet = () => {};
  const f = { owner: ownerWallet.account.address, pools: [].concat(pools).join(','), payout: payouts.join(','), nonce: String(nonce) };
  const deps = { U, pub, account: { address: bot }, log: quiet };
  const r = await request(f, deps);
  const gas = [];
  for (const tx of r.transactions) {
    const hash = await ownerWallet.sendTransaction({ to: tx.to, data: tx.data, value: BigInt(tx.value) });
    const receipt = await pub.waitForTransactionReceipt({ hash });
    assert.equal(receipt.status, 'success', 'an owner set-up transaction reverted');
    gas.push(Number(receipt.gasUsed));
  }
  const report = await check({ ...f, safe: r.safe, roles: r.roles, 'from-block': r.fromBlock }, deps);
  assert.deepEqual(report.problems, []);
  return { safe: r.safe, roles: r.roles, venue: report.venue, role: report.role, gas, fromBlock: r.fromBlock, report };
}
