"""Read-only public-data collection for the audit; never imports a signer."""
import concurrent.futures as cf
import datetime as dt
import json
import pathlib
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
POOLS = {
    'cetus_sui_5bp': '0x51e883ba7c0b566a26cbc8a94cd33eb0abd418a77cc1e60ad22fd9b1f29cd2ab',
    'cetus_sui_25bp': '0xb8d7d9e66a60c239e7a60110efcf8de6c705580ed924d0dde141f4a0e2c90105',
    'cetus_eth_25bp': '0x9e59de50d9e5979fc03ac5bcacdb581c823dbd27d63a036131e17b391f2fac88',
    'bluefin_btc_20bp': '0x1b0cc1c66185ceb8eccbc807c73243ce957f0053dfa1026149265bb2ff704a07',
}

def request(url, payload=None):
    err = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload else None,
                                         headers={'Content-Type': 'application/json', 'User-Agent': 'lp-calculation-audit'})
            with urllib.request.urlopen(req, timeout=25) as r:
                result = json.load(r)
            if isinstance(result, dict) and result.get('errors'):
                raise ValueError(str(result['errors'])[:250])
            return result
        except Exception as e:
            err = e
            time.sleep(attempt + 1)
    raise RuntimeError(str(err))

def snapshot(cp):
    parts = [f'checkpoint(sequenceNumber:{cp}){{timestamp}}']
    for name, address in POOLS.items():
        parts.append(f'{name}: object(address:"{address}",atCheckpoint:{cp}){{asMoveObject{{contents{{json}}}}}}')
    result = request('https://graphql.mainnet.sui.io/graphql', {'query': '{' + ' '.join(parts) + '}'})['data']
    return {'checkpoint': cp, 'timestamp': result['checkpoint']['timestamp'],
            'pools': {name: result[name]['asMoveObject']['contents']['json'] for name in POOLS}}

def candles(symbol):
    start = int(dt.datetime(2026, 8, 24, tzinfo=dt.UTC).timestamp()) * 1000
    end = int(dt.datetime(2026, 10, 4, 18, tzinfo=dt.UTC).timestamp()) * 1000
    out = []
    while start < end:
        url = f'https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=5m&startTime={start}&endTime={end-1}&limit=1000'
        rows = request(url)
        if not rows:
            break
        out.extend([[k[0] / 1000, *map(float, k[1:6])] for k in rows if k[0] + 300000 <= end])
        start = rows[-1][0] + 300000
    (DATA / f'{symbol}_5m.json').write_text(json.dumps(out))
    return symbol, len(out)

if __name__ == '__main__':
    with cf.ThreadPoolExecutor(4) as pool:
        for result in pool.map(candles, ['SUIUSDT', 'ETHUSDT', 'BTCUSDT', 'SOLUSDT']):
            print('candles', result, flush=True)
    # Exact checkpoint timestamps are read, never inferred from the average rate.
    cps = [330260774 - round(i * 383780 / 24) for i in range(721)]
    path = DATA / 'sui_hourly.jsonl'
    done = {json.loads(line)['checkpoint'] for line in path.open()} if path.exists() else set()
    with path.open('a') as f, cf.ThreadPoolExecutor(6) as pool:
        for i, row in enumerate(pool.map(snapshot, [cp for cp in cps if cp not in done])):
            f.write(json.dumps(row) + '\n')
            if i % 100 == 0:
                f.flush()
                print('Sui snapshots', i + 1 + len(done), flush=True)
    print('complete', flush=True)
