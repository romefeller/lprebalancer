// HALT for every signer and swap script: the HALT in the script's own
// directory stops every profile; LPBOT_RUN_DIR/HALT (the loop passes its
// run/<profile> directory) stops one profile. A run directory that is not a
// plain absolute path refuses everything: a HALT that cannot be found must
// not read as no HALT.
import fs from 'node:fs';
import path from 'node:path';

// The HALT files that stop a script in `dir` with environment `env`.
export function haltFiles(dir, env = process.env) {
  const files = [path.join(dir, 'HALT')];
  const run = env.LPBOT_RUN_DIR;
  if (run !== undefined && run !== '') {
    if (typeof run !== 'string' || !path.isAbsolute(run) || run.includes('\0')
        || run.split('/').includes('..')) {
      throw new Error(`HALT present: LPBOT_RUN_DIR ${JSON.stringify(run)} is not an absolute path; refusing`);
    }
    files.push(path.join(run, 'HALT'));
  }
  return files;
}

// Throws `HALT present: <text>` when one of them exists.
export function assertNotHalted(dir, env = process.env) {
  for (const f of haltFiles(dir, env)) {
    if (fs.existsSync(f)) throw new Error(`HALT present: ${fs.readFileSync(f, 'utf8').trim() || f}`);
  }
}
