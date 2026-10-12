// The account an EVM signer acts for. Without a vault: its own key. With one (LPBOT_VAULT_SAFE
// set): the Safe holds the money and is every call's subject and recipient; the bot's key
// sends each call through the Roles module, which lets only its role's calls reach the Safe.
import { encodeFunctionData, getAddress, isAddress } from 'viem';
import { ROLES_ABI, roleKey } from './roles.mjs';
import { readState } from './safe.mjs';

export class VaultMismatch extends Error {}

const list = (s, what) => String(s ?? '').split(',').map(x => x.trim()).filter(Boolean).map(a => {
  if (!isAddress(a, { strict: false })) throw new Error(`${what}: ${a} is not an address`);
  return getAddress(a);
});

// The vault named by the environment, or null. Half a configuration refuses.
export function vaultConfig(env = process.env) {
  const names = ['LPBOT_VAULT_SAFE', 'LPBOT_VAULT_ROLES', 'LPBOT_VAULT_VENUE', 'LPBOT_VAULT_POOLS'];
  const set = names.filter(n => env[n]);
  if (!set.length) return null;
  if (set.length !== names.length) throw new Error(`a vault needs ${names.join(', ')}; missing ${names.filter(n => !env[n]).join(', ')}`);
  const [safe] = list(env.LPBOT_VAULT_SAFE, 'LPBOT_VAULT_SAFE'), [roles] = list(env.LPBOT_VAULT_ROLES, 'LPBOT_VAULT_ROLES');
  return { safe, roles, venue: env.LPBOT_VAULT_VENUE, role: roleKey(env.LPBOT_VAULT_VENUE),
    pools: list(env.LPBOT_VAULT_POOLS, 'LPBOT_VAULT_POOLS'), payouts: list(env.LPBOT_VAULT_PAYOUTS, 'LPBOT_VAULT_PAYOUTS') };
}

// One step of a signer ({label, to, data, value}) as the role call the bot's key sends.
export function wrapStep(step, vault) {
  if (step.value && BigInt(step.value) !== 0n) throw new Error(`refused: ${step.label} carries ${BigInt(step.value)} wei; a vault call carries no value`);
  return { label: step.label, to: vault.roles, value: 0n, data: encodeFunctionData({ abi: ROLES_ABI, functionName: 'execTransactionWithRole',
    args: [getAddress(step.to), 0n, step.data, 0, vault.role, true] }) };
}

// Refuses to act for a Safe whose Roles module is not enabled, or is not the Safe's own:
// an owner's revoke (disableModule) stops the bot here, before anything is signed.
export async function verifyVault(pub, vault) {
  const s = await readState(pub, vault.safe, vault.roles);
  const same = (a, b) => String(a).toLowerCase() === String(b).toLowerCase();
  if (!s.modules.some(m => same(m, vault.roles))) throw new VaultMismatch(`refused: the owners revoked the bot: the Safe ${vault.safe} has not enabled the Roles module ${vault.roles}`);
  if (!same(s.avatar, vault.safe) || !same(s.target, vault.safe)) throw new VaultMismatch(`refused: the Roles module ${vault.roles} does not act on the Safe ${vault.safe}`);
  return s;
}

// What a signer's connection carries: `me` holds the money, `from` signs and pays the gas,
// `wrap` turns a step into what `from` sends.
export function acting(address, vault) {
  if (!vault) return { me: address, from: address, wrap: (s) => s, vault: null };
  return { me: vault.safe, from: address, wrap: (s) => wrapStep(s, vault), vault };
}
