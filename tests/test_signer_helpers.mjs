import test from 'node:test';
import assert from 'node:assert/strict';
import { PublicKey } from '@solana/web3.js';
import { positionRent } from '../position_rent.mjs';
import { executeBuilt, isProgramFailure, signerError } from '../signer_errors.mjs';

const key = n => new PublicKey(Buffer.alloc(32, n));
const TOKEN_2022 = new PublicKey('TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb');
const TOKEN = new PublicKey('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA');

test('legacy and lean refundable rent match audited close receipts; no duplicate NFT account', async () => {
  for (const lean of [false, true]) {
    let queries = 0;
    const rpc = {
      getMultipleAccountsInfo: async keys => {
        assert.equal(keys.length, 2);
        return [{ lamports: 2077720 }, { lamports: lean ? 1676400 : 1461600, owner: lean ? TOKEN_2022 : TOKEN }];
      },
      getTokenAccountsByOwner: async (owner, filter, config) => {
        queries++;
        assert.equal(config, undefined);
        assert.ok(filter.mint.equals(key(2)));
        const account = { pubkey: key(4), account: { lamports: lean ? 1513840 : 1488440 } };
        return { value: [account, account] }; // A repeated address cannot add rent twice.
      },
    };
    const sol = await positionRent(rpc, key(1), [key(2), key(2)], key(3), { refundMint: true });
    assert.equal(sol, lean ? .00526796 : .00356616);
    assert.equal(queries, 1);
  }
});

test('rent read failure propagates instead of returning a partial total', async () => {
  await assert.rejects(positionRent({ getMultipleAccountsInfo: async () => [null, null] }, key(1), [key(2)], key(3)));
});

test('program rejection takes precedence over retry noise', () => {
  const error = new Error('Earlier 429; transaction failed: {"InstructionError":[2,{"Custom":6017}]}');
  assert.ok(isProgramFailure(error));
  assert.match(signerError(error), /PriceSlippageCheck \(6017\)/);
  assert.ok(!isProgramFailure(new Error('429 Too Many Requests')));
});

test('an uncertain submission must not be executed again on another endpoint', async () => {
  let calls = 0;
  await assert.rejects(executeBuilt({ execute: async () => {
    calls++;
    throw new Error('429 while confirming transaction');
  }}), err => err.sent === true);
  assert.equal(calls, 1);
});
