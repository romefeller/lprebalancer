// The program's own words for a failed Solana transaction, in the one error
// line that reaches the loop, the events table and Telegram.
//
// 2026-10-12 17:00:31Z: a test0 open failed with "Instruction 7: custom
// program error: 0x178b" and nothing else. The preflight had returned the
// program logs, but every signer reported only e.message, so the cause had to
// be inferred. describeFailure adds three things to the message:
//   - the failing program and the name of its custom error code
//     (shared/program_errors.json). A code means different things in
//     different programs (6027 is InvalidBinArray in DLMM and
//     SpendingLimitExceeded in Squads), so the program comes from the first
//     "Program <id> failed" log line (the innermost program that failed), or
//     else from the failing instruction's program id;
//   - the last LOG_LINES program log lines, each cut to LINE_CHARS;
//   - nothing secret: the text passes book_format.redact.
// The result is one line: " | program error: ..." and " | logs: a ; b ; c".
import { createRequire } from 'node:module';
import TABLE from './program_errors.json' with { type: 'json' };
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };
import { redact } from '../book_format.mjs';

const require = createRequire(import.meta.url);

// The loop keeps 2,000 characters of a signer failure (lp/signers.py,
// events.detail): the whole line fits.
export const LOG_LINES = 10;
export const LINE_CHARS = 160;
export const MAX_CHARS = 1800;
// Text that says a program refused the transaction (worth its logs).
export const PROGRAM_FAILURE = /custom program error|InstructionError|failed on chain|simulation failed|Program \w+ failed/i;
// Programs that run other programs' instructions (a vault's policy, an
// aggregator route). Decoded from the instruction index alone, their code
// may be an inner program's.
const CALLERS = new Set([SOLANA.programs.squads_smart_account, SOLANA.programs.jupiter_v6]);
const MARK = ' | program error: ';
const LOGS = ' | logs: ';

// {program, name} for `code` of `programId`, or null for a program not in
// the table. `name` is null for a code the program's table does not hold.
// Anchor's own codes (below 6000) are decoded for Anchor programs. Pure.
export function decodeCustom(programId, code) {
  const p = TABLE.programs[String(programId)];
  const c = Number(code);
  if (!p || !Number.isInteger(c) || c < 0) return null;
  const name = p.errors[String(c)] ?? (p.anchor && c < 6000 ? TABLE.anchor[String(c)] : undefined);
  return { program: p.name, name: name ?? null };
}

// The program log lines an error carries: its own, its cause's, down to six
// levels (SendTransactionError keeps them in transactionLogs; a simulation
// result in value.logs; a @solana/kit error in context.logs). The first
// non-empty list wins. Pure.
export function collectLogs(error) {
  let e = error;
  for (let depth = 0; e && depth < 6; depth += 1, e = e.cause) {
    for (const logs of [e.programLogs, e.transactionLogs, safeLogs(e), e.simulationResponse?.logs, e.value?.logs, e.context?.logs]) {
      if (Array.isArray(logs) && logs.length) return logs.map(String);
    }
  }
  return [];
}

function safeLogs(e) {
  try { return e.logs; } catch { return undefined; }
}

// {programId, code, index, source} of the failure, or null. `source` is
// 'logs' when a "Program X failed: custom program error" line names the
// program, 'instruction' when only the failing instruction's index does
// (programIds[index]). Pure.
export function failingProgram(text, logs = [], programIds = []) {
  const s = String(text ?? '');
  const at = s.match(/InstructionError"?\s*:?\s*\[\s*(\d+)\s*,\s*\{\s*"?Custom"?\s*:\s*(\d+)/i)
    ?? s.match(/(?:Error processing )?Instruction (\d+):\s*custom program error:\s*0x([0-9a-f]+)/i);
  let index = null, code = null;
  if (at) {
    index = Number(at[1]);
    code = /0x/i.test(at[0]) ? parseInt(at[2], 16) : Number(at[2]);
  }
  for (const line of logs) {
    const m = String(line).match(/^Program (\w{32,44}) failed: custom program error: 0x([0-9a-f]+)/i);
    if (m) return { programId: m[1], code: parseInt(m[2], 16), index, source: 'logs' };
  }
  if (code == null) {
    const bare = s.match(/custom program error:\s*0x([0-9a-f]+)/i);
    if (!bare) return null;
    code = parseInt(bare[1], 16);
  }
  const programId = index != null ? programIds[index] ?? null : null;
  return { programId, code, index, source: programId ? 'instruction' : null };
}

// The error's own message without web3.js's log dump and advice: the log
// lines are added once, bounded, by describeFailure. Pure.
export function baseMessage(error) {
  const raw = String(error?.message ?? error ?? '');
  const cut = raw.split(MARK)[0].split(LOGS)[0];
  return cut.replace(/Logs:\s*\[[\s\S]*\]\.?/g, '')
    .replace(/Catch the `SendTransactionError`[^\n]*/g, '')
    .split(/\s+/).join(' ').trim();
}

// The decoded part: "Meteora DLMM InvalidBinArray (6027 = 0x178b) at
// instruction 7". Pure.
export function decodedText(f) {
  if (!f) return null;
  const hex = `0x${f.code.toString(16)}`;
  const d = f.programId ? decodeCustom(f.programId, f.code) : null;
  const who = d ? `${d.program} ${d.name ?? 'unknown code'}`
    : f.programId ? `program ${f.programId} (not in the table)` : 'unknown program';
  const where = f.index != null ? ` at instruction ${f.index}` : '';
  const inner = f.source === 'instruction' && CALLERS.has(f.programId) ? ' (by instruction index: may be an inner program\'s code)' : '';
  return `${who} (${f.code} = ${hex})${where}${inner}`;
}

// The last `n` log lines, each cut to LINE_CHARS, newlines removed. Pure.
// Empty lines are dropped; '|' and ' ; ' (the line's own separators) become
// '/' and ', ', so the loop can split the line back (lp/books.program_logs).
export function logTail(logs, n = LOG_LINES) {
  return logs.map(l => String(l).split(/\s+/).join(' ').trim().replaceAll('|', '/').split(' ; ').join(', ').replace(/ ;$/, ','))
    .filter(Boolean).slice(-n)
    .map(one => (one.length > LINE_CHARS ? `${one.slice(0, LINE_CHARS - 1)}…` : one));
}

// One line: the error's message, the decoded program error, the log tail;
// redacted and at most MAX_CHARS long. Idempotent: a described message is
// described again from its base. Pure given `secrets`.
export function describeFailure(error, { programIds = [], logs = null, secrets } = {}) {
  const all = logs ?? collectLogs(error);
  const raw = String(error?.message ?? error ?? '');
  const base = baseMessage(error);
  const f = failingProgram(raw, all, programIds.length ? programIds : (error?.programIds ?? []));
  let out = base;
  // A line described before keeps its decoding unless new logs say more.
  const prior = raw.includes(MARK) ? raw.split(MARK)[1].split(LOGS)[0].trim() : null;
  const decoded = prior && !all.length ? prior : decodedText(f);
  if (decoded) out += `${MARK}${decoded}`;
  const tail = logTail(all);
  if (tail.length) out += `${LOGS}${tail.join(' ; ')}`;
  else if (raw.includes(LOGS)) out += LOGS + raw.split(LOGS)[1].trim();
  out = secrets === undefined ? redact(out) : redact(out, secrets);
  return out.length > MAX_CHARS ? `${out.slice(0, MAX_CHARS - 1)}…` : out;
}

// The program id of every top-level instruction of a serialized transaction
// (legacy or v0; program ids are never in a lookup table), or [] when the
// bytes do not parse.
export function instructionPrograms(raw) {
  try {
    const { VersionedTransaction } = require('@solana/web3.js');
    if (Array.isArray(raw?.instructions)) return raw.instructions.map(ix => ix.programId.toBase58());  // a legacy Transaction
    const msg = raw?.message ?? VersionedTransaction.deserialize(Uint8Array.from(raw)).message;
    const keys = msg.staticAccountKeys;
    return msg.compiledInstructions.map(ix => keys[ix.programIdIndex]?.toBase58() ?? null);
  } catch { return []; }
}

// The text of a failure for a signer's JSON report: the described line
// (bounded already), or the message cut to `limit`. Pure.
export function errorText(e, limit = 300) {
  return e?.described ? String(e.message) : String(e?.message ?? e).slice(0, limit);
}

// A refused simulation as one described line: its err, the decoded program
// error and the log tail. `tx` (optional) gives the instruction programs.
export function simulationFailure(err, logs, tx = null) {
  return describeFailure({ message: `simulation failed: ${JSON.stringify(err)}` },
    { logs: logs ?? [], programIds: tx ? instructionPrograms(tx) : [] });
}

async function within(promise, ms) {
  let timer;
  try {
    return await Promise.race([promise, new Promise(r => { timer = setTimeout(() => r(undefined), ms); })]);
  } finally { clearTimeout(timer); }
}

// The error, its message replaced by describeFailure's line, with the logs
// (programLogs) and instruction program ids (programIds) attached. Fetches
// the logs when the error has none: SendTransactionError.getLogs, else the
// landed transaction's meta (`signature`). Each read waits at most
// `timeoutMs`. An error with no logs that does not read as a program failure
// (a timeout, a 429) comes back as it was. Never throws: on any problem the
// error comes back as it was.
export async function enrichError(error, { connection = null, raw = null, signature = null, timeoutMs = 5000 } = {}) {
  if (!error || typeof error !== 'object' || error.described) return error;
  try {
    const programIds = raw ? instructionPrograms(raw) : (error.programIds ?? []);
    let logs = collectLogs(error);
    if (!logs.length && !PROGRAM_FAILURE.test(String(error.message))) return error;   // a transport error: as it is
    if (!logs.length && connection && typeof error.getLogs === 'function' && error.signature) {
      try { logs = (await within(error.getLogs(connection), timeoutMs)) ?? []; } catch { logs = []; }
    }
    if (!logs.length && connection && signature) {
      try {
        const tx = await within(connection.getTransaction(signature,
          { maxSupportedTransactionVersion: 0, commitment: 'confirmed' }), timeoutMs);
        logs = tx?.meta?.logMessages ?? [];
      } catch { logs = []; }
    }
    const text = describeFailure(error, { programIds, logs });
    Object.defineProperty(error, 'programLogs', { value: logs, configurable: true, writable: true });
    error.programIds = programIds;
    error.message = text;
    error.described = true;
  } catch { /* the error as it was */ }
  return error;
}
