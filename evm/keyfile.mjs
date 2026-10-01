// The EVM wallet's key file: one 0x-prefixed 32-byte hex string and a newline.
//
// The key lives outside the repository (default /home/ubuntu/.kamino-keys/evm-wallet.secret),
// mode 0600. It is read inside the process that signs, handed to viem, and never printed,
// logged, returned, put in argv or in the environment. Error messages name the path, never
// the content.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

export const DEFAULT_KEY_PATH = '/home/ubuntu/.kamino-keys/evm-wallet.secret';
const KEY_RE = /^0x[0-9a-f]{64}$/;

// secp256k1 group order: a key must be in [1, n-1].
const N = 0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141n;

// True when `hex` is a valid secp256k1 private key in our file format. Pure.
export function validKey(hex) {
  if (typeof hex !== 'string' || !KEY_RE.test(hex)) return false;
  const k = BigInt(hex);
  return k > 0n && k < N;
}

// Write a NEW key file. Refuses to overwrite anything that exists at `file`, including a
// dangling symlink. The content goes to a temporary file opened O_CREAT|O_EXCL with mode
// 0600 (and fchmod'ed to 0600 whatever the umask), is fsynced, then hard-linked to `file`:
// link(2) fails with EEXIST if the name exists, so the final name appears atomically and
// only ever with complete content.
export function writeNewKey(file, hex) {
  if (!validKey(hex)) throw new Error('refusing to write an invalid key');
  const dir = path.dirname(file);
  const st = fs.statSync(dir);
  if (!st.isDirectory()) throw new Error(`${dir} is not a directory`);
  if ((st.mode & 0o077) !== 0) throw new Error(`${dir} is readable by group or others (mode ${(st.mode & 0o777).toString(8)}); refusing`);
  let exists = false;
  try { fs.lstatSync(file); exists = true; } catch (e) { if (e.code !== 'ENOENT') throw e; }
  if (exists) throw new Error(`${file} exists; refusing to overwrite a key`);
  const tmp = path.join(dir, `.${path.basename(file)}.${crypto.randomBytes(8).toString('hex')}.tmp`);
  const fd = fs.openSync(tmp, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, 0o600);
  try {
    fs.fchmodSync(fd, 0o600);
    fs.writeSync(fd, `${hex}\n`);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  try {
    fs.linkSync(tmp, file);
  } catch (e) {
    if (e.code === 'EEXIST') throw new Error(`${file} exists; refusing to overwrite a key`);
    throw e;
  } finally {
    fs.unlinkSync(tmp);
  }
  const dfd = fs.openSync(dir, 'r');
  try { fs.fsyncSync(dfd); } finally { fs.closeSync(dfd); }
}

// Read the key at `file`. Refuses a file that group or others can read (as ssh does), a
// file that is not a regular file, and content that is not exactly one valid key.
export function readKey(file) {
  if (!file) throw new Error('WALLET_SECRET_PATH is not set; refusing to guess a key location');
  const st = fs.statSync(file);
  if (!st.isFile()) throw new Error(`key path ${file} is not a regular file`);
  if ((st.mode & 0o077) !== 0) {
    throw new Error(`key file ${file} has mode ${(st.mode & 0o777).toString(8)}; it must be 0600`);
  }
  const hex = fs.readFileSync(file, 'utf8').trim().toLowerCase();
  if (!validKey(hex)) throw new Error(`key file ${file} does not hold one 0x-prefixed 32-byte secp256k1 key`);
  return hex;
}
