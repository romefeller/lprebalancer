// The sandboxed onboarding page (2026-10-12 security audit): the unit's
// directives, the root start helper, and the config overrides the unit sets.
// The root steps themselves (ops/onboard/setup_sandbox.sh) check the running
// unit when the owner runs them; here everything that runs without root.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { loadConfig, serviceCommand } from '../onboard/server.mjs';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const LIVE = '/home/ubuntu/agent-fin/lp_bot';
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');

// [key, value] of every directive line, comments dropped
function directives(text) {
  return text.split('\n').map(l => l.trim()).filter(l => l && !l.startsWith('#') && !l.startsWith('['))
    .map(l => [l.slice(0, l.indexOf('=')), l.slice(l.indexOf('=') + 1)]);
}

test('the unit runs as lp-onboard in a sandbox, with only the keys and variables it needs', () => {
  const d = directives(read('ops/lp-onboard.service'));
  const one = (k) => { const v = d.filter(([x]) => x === k).map(([, v]) => v); assert.equal(v.length, 1, k); return v[0]; };
  const all = (k) => d.filter(([x]) => x === k).map(([, v]) => v);
  assert.equal(one('User'), 'lp-onboard');
  assert.equal(one('Group'), 'lp-onboard');
  for (const [k, v] of [['NoNewPrivileges', 'yes'], ['ProtectSystem', 'strict'], ['ProtectHome', 'tmpfs'], ['PrivateTmp', 'yes'],
    ['PrivateDevices', 'yes'], ['CapabilityBoundingSet', ''], ['AmbientCapabilities', ''], ['RestrictSUIDSGID', 'yes'],
    ['RestrictNamespaces', 'yes'], ['LockPersonality', 'yes'], ['ProtectKernelTunables', 'yes'], ['ProtectKernelModules', 'yes'],
    ['ProtectControlGroups', 'yes'], ['SystemCallArchitectures', 'native'], ['RestrictAddressFamilies', 'AF_UNIX AF_INET AF_INET6'],
    ['BindReadOnlyPaths', LIVE], ['WorkingDirectory', LIVE], ['UMask', '0077']]) assert.equal(one(k), v, k);
  assert.deepEqual(all('SystemCallFilter'), ['@system-service', '~@privileged @resources']);
  assert.equal(all('ReadWritePaths').length, 0, 'it writes only its StateDirectory');
  // the keys: three credentials, none of the bot's own wallets
  assert.deepEqual(all('LoadCredential').sort(), [
    'evm.secret:/home/ubuntu/.kamino-keys/vault-test/evm.secret',
    'payer.json:/home/ubuntu/.kamino-keys/vault-test/payer.json',
    'policy.json:/home/ubuntu/.kamino-keys/vault-test/policy.json']);
  // the environment: one root file that setup_sandbox.sh writes, never the shared ones
  assert.deepEqual(all('EnvironmentFile'), ['/etc/lp-onboard/env']);
  const env = Object.fromEntries(all('Environment').map(v => v.replace(/^"|"$/g, '')).map(v => [v.slice(0, v.indexOf('=')), v.slice(v.indexOf('=') + 1)]));
  assert.deepEqual(Object.keys(env).sort(), ['LPBOT_DSN', 'LPBOT_ONBOARD_START', 'LPBOT_VAULT_EVM_KEY_PATH', 'LPBOT_VAULT_SOL_PAYER_PATH',
    'LPBOT_VAULT_SOL_POLICY_PATH', 'PATH', 'PYTHONDONTWRITEBYTECODE', 'SOLANA_RPC_URL']);
  assert.equal(env.LPBOT_VAULT_SOL_PAYER_PATH, '%d/payer.json');
  // the variables the server's config names for the keys are the ones set here
  const cfg = JSON.parse(read('onboard/onboard.json'));
  assert.ok(env[cfg.chains.solana.payerKeyEnv] && env[cfg.chains.solana.policyKeyEnv] && env[cfg.chains.polygon.keyEnv]);
  assert.doesNotMatch(read('ops/lp-onboard.service').replace(/^#.*$/gm, ''), /sudo|kamino-keys\/env|onboard\.env|MemoryDenyWriteExecute/);
  // the spool the page writes is the one the path unit watches and the helper reads
  const spool = '/var/lib/lp-onboard/start';
  assert.equal(env.LPBOT_ONBOARD_START, `/usr/bin/touch ${spool}/{name}`);
  assert.match(read('ops/onboard/lp-onboard-start.path'), new RegExp(`^DirectoryNotEmpty=${spool}$`, 'm'));
  assert.match(read('ops/onboard/lp-onboard-start.sh'), new RegExp(`SPOOL="\\$\\{LPBOT_ONBOARD_SPOOL:-${spool}\\}"`));
  assert.ok(all('StateDirectory')[0].split(/\s+/).includes('lp-onboard/start'));
});

test('the setup script parses, needs root, and holds no secret', () => {
  execFileSync('bash', ['-n', path.join(ROOT, 'ops/onboard/setup_sandbox.sh')]);
  const text = read('ops/onboard/setup_sandbox.sh');
  assert.doesNotMatch(text, /api-key=|[A-Za-z0-9]{32,}|\[\s*\d+\s*,\s*\d+/);
  if (process.getuid() !== 0) {
    const r = spawnSync('bash', [path.join(ROOT, 'ops/onboard/setup_sandbox.sh')], { encoding: 'utf8' });
    assert.equal(r.status, 1);
    assert.match(r.stderr, /run as root/);
  }
  // the switch happens only after the try on 8091 passed its checks
  const order = ['check_unit lp-onboard-next.service 8091', 'cp -p "$UNIT" "$BACKUP"', 'install -m 0644 -o root -g root "$REPO/ops/lp-onboard.service" "$UNIT"'];
  const at = order.map(s => text.indexOf(s));
  assert.ok(at.every(i => i > 0) && at[0] < at[1] && at[1] < at[2], JSON.stringify(at));
});

// --- the root start helper, run here with a fake systemctl -------------------------

function runHelper(entries) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lp-onboard-start-'));
  try {
    const spool = path.join(dir, 'start'), log = path.join(dir, 'calls'), outside = path.join(dir, 'outside');
    fs.mkdirSync(spool); fs.writeFileSync(outside, 'keep');
    const fake = path.join(dir, 'systemctl');
    fs.writeFileSync(fake, `#!/bin/bash\nprintf '%s\\n' "$*" >> ${JSON.stringify(log)}\n[ "$3" != lp-bot@failing.service ]\n`, { mode: 0o755 });
    for (const e of entries) {
      const p = path.join(spool, e.name);
      if (e.kind === 'link') fs.symlinkSync(outside, p);
      else if (e.kind === 'dir') { fs.mkdirSync(p); fs.writeFileSync(path.join(p, 'inner'), 'x'); }
      else fs.writeFileSync(p, '');
    }
    const r = spawnSync('bash', [path.join(ROOT, 'ops/onboard/lp-onboard-start.sh')], { encoding: 'utf8',
      env: { PATH: '/usr/bin:/bin', LPBOT_ONBOARD_SPOOL: spool, LPBOT_ONBOARD_SYSTEMCTL: fake } });
    return { status: r.status, out: r.stdout + r.stderr, calls: fs.existsSync(log) ? fs.readFileSync(log, 'utf8').trim().split('\n') : [],
      left: fs.readdirSync(spool), outside: fs.readFileSync(outside, 'utf8') };
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
}

test('the helper enables a well-named profile and nothing else', () => {
  const r = runHelper([{ name: 'alice-sol' }, { name: 'a' }, { name: 'Bad' }, { name: 'x--y' }, { name: '-rf' }, { name: 'ends-' },
    { name: 'has space' }, { name: 'semi;reboot' }, { name: 'dollar$(id)' }, { name: 'link-name', kind: 'link' }, { name: 'dir-name', kind: 'dir' },
    { name: 'failing' }, { name: 'x'.repeat(33) }, { name: 'abc' }]);
  assert.equal(r.status, 0, r.out);
  assert.deepEqual(r.calls.sort(), ['enable --now lp-bot@abc.service', 'enable --now lp-bot@alice-sol.service', 'enable --now lp-bot@failing.service']);
  assert.deepEqual(r.left, [], 'every entry is removed, acted on or not');
  assert.equal(r.outside, 'keep', 'a symlink is removed, never followed');
  assert.match(r.out, /refused: name 'x--y'/);
  assert.match(r.out, /refused: 'link-name' is not a regular file/);
  assert.match(r.out, /failed: systemctl enable --now lp-bot@failing.service/);
});

test('property: whatever the names, only names the page accepts reach systemctl, at most 20 a run', () => {
  const alphabet = 'abcz09-_.;$ A/'.split('').filter(c => c !== '/');
  const NAME = /^[a-z][a-z0-9-]{1,30}[a-z0-9]$/;
  let seed = 11;
  const rnd = (n) => { seed = (seed * 1103515245 + 12345) % 2 ** 31; return seed % n; };
  for (let round = 0; round < 12; round += 1) {
    const names = new Set();
    const count = 5 + rnd(30);
    while (names.size < count) names.add(Array.from({ length: 1 + rnd(36) }, () => alphabet[rnd(alphabet.length)]).join('').replace(/^\.+$/, 'dot'));
    const r = runHelper([...names].map(name => ({ name })));
    assert.equal(r.status, 0, r.out);
    const good = [...names].filter(n => NAME.test(n) && !n.includes('--'));
    assert.equal(r.calls.length, Math.min(good.length, 20));
    for (const c of r.calls) {
      const m = c.match(/^enable --now lp-bot@(.+)\.service$/);
      assert.ok(m && good.includes(m[1]), c);
    }
  }
  // more good names than one run takes: 20 now, the rest at the next run
  const many = runHelper(Array.from({ length: 25 }, (_, i) => ({ name: `p${String(i).padStart(2, '0')}x` })));
  assert.equal(many.calls.length, 20);
  assert.equal(many.left.length, 5);
});

// --- the overrides the sandboxed unit sets --------------------------------------------

test('LPBOT_ONBOARD_START and LPBOT_ONBOARD_PORT override the file; a bad port is refused', () => {
  const base = loadConfig(undefined, {});
  assert.deepEqual(base.service.start.slice(0, 2), ['sudo', '-n']);
  assert.equal(base.listen.port, 8090);
  const c = loadConfig(undefined, { LPBOT_ONBOARD_START: ' /usr/bin/touch  /var/lib/lp-onboard/start/{name} ', LPBOT_ONBOARD_PORT: '8091' });
  assert.deepEqual(c.service.start, ['/usr/bin/touch', '/var/lib/lp-onboard/start/{name}']);
  assert.equal(c.listen.port, 8091);
  assert.equal(c.listen.host, base.listen.host);
  assert.deepEqual(c.service.status, base.service.status);
  assert.deepEqual(serviceCommand(c.service.start, 'alice-sol'), ['/usr/bin/touch', '/var/lib/lp-onboard/start/alice-sol']);
  assert.throws(() => serviceCommand(c.service.start, '../etc'), /refused/);
  for (const port of ['80', '0', '70000', 'x', '8091.5']) assert.throws(() => loadConfig(undefined, { LPBOT_ONBOARD_PORT: port }), /LPBOT_ONBOARD_PORT/);
  assert.throws(() => loadConfig(undefined, { LPBOT_ONBOARD_START: '/usr/bin/touch /tmp/x' }), /must name \{name\}/);
});

test('loadConfig bounds: ports 1024 and 65535, fee.bps 0 and 5000 are taken; one past each is refused', () => {
  for (const port of ['1024', '65535']) assert.equal(loadConfig(undefined, { LPBOT_ONBOARD_PORT: port }).listen.port, Number(port));
  for (const port of ['1023', '65536']) assert.throws(() => loadConfig(undefined, { LPBOT_ONBOARD_PORT: port }), /LPBOT_ONBOARD_PORT/);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'onboard-bps-'));
  try {
    const raw = JSON.parse(read('onboard/onboard.json'));
    const withBps = (bps) => { const f = path.join(dir, `c${bps}.json`); fs.writeFileSync(f, JSON.stringify({ ...raw, fee: { ...raw.fee, bps } })); return f; };
    for (const bps of [0, 5000]) assert.equal(loadConfig(withBps(bps), {}).fee.bps, bps);
    for (const bps of [-1, 5001, 6000]) assert.throws(() => loadConfig(withBps(bps), {}), /fee\.bps/, String(bps));
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});
