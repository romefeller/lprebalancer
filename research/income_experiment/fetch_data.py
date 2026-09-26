import datetime as dt
import hashlib
import json
import pathlib
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / 'data'
POOL = 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE'
if (DATA/'candles_5m.json').exists() and (DATA/'manifest.json').exists():
    print('An archived dataset already exists. Use that dataset for reproduction.')
    raise SystemExit(0)
cached = DATA / 'candles_5m_00.json'
end = (max(r[0] for r in json.loads(cached.read_text())['data']['attributes']['ohlcv_list'])
       if cached.exists() else int(time.time()) // 300 * 300)
start = end - 90 * 86400
manifest = {'pool': POOL, 'requested_start': start, 'end_exclusive': end, 'pages': []}

def get(url, dest):
    if dest.exists():
        return json.loads(dest.read_text())
    for attempt in range(6):
        proc = subprocess.run(['curl','-fsS','--max-time','45',url], capture_output=True, text=True)
        if proc.returncode == 0:
            try:
                obj = json.loads(proc.stdout)
                if 'status' not in obj or not obj['status'].get('error_code'):
                    dest.write_text(proc.stdout)
                    return obj
            except (ValueError, AttributeError):
                pass
        print('retry', attempt+1, proc.stderr[-180:], flush=True)
        time.sleep(min(10 * (attempt + 1), 60))
    raise RuntimeError('Public data request failed: ' + url)

before = end
rows = {}
for page in range(30):
    url = f'https://api.geckoterminal.com/api/v2/networks/solana/pools/{POOL}/ohlcv/minute?aggregate=5&limit=1000&currency=usd&include_empty_intervals=true&before_timestamp={before}'
    path = DATA / f'candles_5m_{page:02d}.json'
    obj = get(url, path)
    batch = obj.get('data',{}).get('attributes',{}).get('ohlcv_list',[])
    manifest['pages'].append({'url':url,'file':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'rows':len(batch)})
    if not batch:
        break
    for row in batch:
        if start <= row[0] < end:
            rows[row[0]] = row
    earliest = min(x[0] for x in batch)
    print('page',page,'rows',len(batch),'earliest',dt.datetime.fromtimestamp(earliest,dt.timezone.utc).isoformat(),flush=True)
    if earliest <= start or earliest >= before:
        break
    before = earliest - 1
    time.sleep(10)
manifest['downloaded_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
(DATA/'candles_5m.json').write_text(json.dumps(sorted(rows.values()),separators=(',',':')))
(DATA/'manifest.json').write_text(json.dumps(manifest,indent=2))
print('TOTAL',len(rows),flush=True)
get(f'https://api.orca.so/v2/solana/pools/{POOL}',DATA/'orca_pool.json')
print('SNAPSHOT SAVED',flush=True)
