// The EVM LP wallet: create it once, then read its address.
//
//   node evm_wallet.mjs create  [--path <file>]   new key, printed: the checksummed address only
//   node evm_wallet.mjs address [--path <file>]   the address of the key on disk
//
// The key comes from viem's generatePrivateKey: @noble/curves secp256k1.utils.randomPrivateKey
// over crypto.getRandomValues, the OS CSPRNG. It is written by evm/keyfile.mjs: mode 0600, O_CREAT|O_EXCL, linked
// into place atomically, never over an existing file. Nothing here prints, logs or returns
// the key: stdout carries the address, stderr carries `ERROR: <message>` that names paths
// only. Default path: /home/ubuntu/.kamino-keys/evm-wallet.secret (outside the repository).
import { generatePrivateKey, privateKeyToAddress } from 'viem/accounts';
import { DEFAULT_KEY_PATH, readKey, writeNewKey } from './evm/keyfile.mjs';
import { isEntry } from './rpc_policy.mjs';

function keyPath(args) {
  const i = args.indexOf('--path');
  if (i >= 0) {
    if (!args[i + 1]) throw new Error('--path needs a file');
    return args[i + 1];
  }
  return DEFAULT_KEY_PATH;
}

// Create the wallet at `file` and return its checksummed address.
export function create(file) {
  const hex = generatePrivateKey();
  writeNewKey(file, hex);
  // Read back through the same reader the signer uses: the file on disk is the wallet.
  return privateKeyToAddress(readKey(file));
}

export function address(file) {
  return privateKeyToAddress(readKey(file));
}

function main() {
  const args = process.argv.slice(2);
  const cmd = args[0];
  if (cmd === 'create') return console.log(create(keyPath(args)));
  if (cmd === 'address') return console.log(address(keyPath(args)));
  console.log('commands: create [--path <file>] | address [--path <file>]');
}

if (isEntry(import.meta.url)) {
  try { main(); } catch (e) { console.error('ERROR:', e.message); process.exitCode = 1; }
}
