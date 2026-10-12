// Shared by the onboarding fork tests: rows in the test database. The page
// itself opens in Chromium (tests/_browser.mjs).
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';

export const DSN = process.env.LPBOT_DSN ?? '';

export function psql(sql) {
  assert.match(DSN, /^dbname=\w+_test\s*$/, 'the test database only');
  const r = spawnSync('psql', ['-X', '-q', '-v', 'ON_ERROR_STOP=1', '-At', '-d', DSN.replace(/^dbname=/, '').trim(), '-c', sql], { encoding: 'utf8' });
  if (r.status !== 0) throw new Error(r.stderr);
  return r.stdout.trim();
}

export const json = (sql) => JSON.parse(psql(sql));

// A template profile as a genesis one looks: the test database's sol-usdc
// tuning on a wallet of `chain`, paying `payoutMint` to `profit`.
export function template(name, { chain, address, payoutMint, profit }) {
  psql(`insert into rebalancer.wallets (id, chain, address, secret_env, label) values ('${name}-lp', '${chain}', '${address}', 'WALLET_SECRET_PATH', 't');
    create temp table t as select * from rebalancer.config where name = 'sol-usdc';
    alter table t drop column id;
    update t set name = '${name}', wallet_id = '${name}-lp', active = false, payout_mint = '${payoutMint}',
      profit_wallet = '${profit}', payout_enabled = true, special = true, fee_bps = 0, fee_wallet = null;
    do $$ declare cols text; begin
      select string_agg(quote_ident(attname), ', ') into cols from pg_attribute where attrelid = 't'::regclass and attnum > 0 and not attisdropped;
      execute format('insert into rebalancer.config (%s) select %s from t', cols, cols);
    end $$;`);
}

// Every row of the onboarded profile `name` and of the template `tpl`.
export function clean(name, tpl) {
  psql(`delete from rebalancer.onboard where name = '${name}';
    delete from rebalancer.config where name in ('${name}', '${tpl}');
    delete from rebalancer.vault_policies where wallet_id = '${name}-vault';
    delete from rebalancer.vaults where wallet_id = '${name}-vault';
    delete from rebalancer.wallets where id in ('${name}-vault', '${tpl}-lp');`);
}

export const wait = async (fn, ms = 240_000) => {
  const end = Date.now() + ms;
  while (Date.now() < end) { const v = fn(); if (v) return v; await new Promise(r => setTimeout(r, 100)); }
  throw new Error('timed out waiting');
};
