"""Review artifact: observed HTTP authoring and load probes, not a release gate.
No deletes or migrations. Writes fabricated records to the supplied disposable API.
Review status/error outputs against workflow-validation.md; a printed response alone
is not a correctness assertion. Only Python standard library is required.
"""
import concurrent.futures
import json
import time
import urllib.error
import urllib.request

import argparse

parser = argparse.ArgumentParser(description='Write fabricated planning fixtures only to a disposable review API and record observed responses.')
parser.add_argument('--base-url', required=True, help='API of an isolated review project, not an existing household database')
parser.add_argument('--disposable-fixtures', action='store_true', help='Confirm that the target uses a disposable fabricated database')
args = parser.parse_args()
if not args.disposable_fixtures:
    parser.error('This probe writes fabricated records; use an isolated database and --disposable-fixtures.')
BASE = args.base_url.rstrip('/')
results = []

def call(method, path, body=None):
    start = time.perf_counter()
    request = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(), method=method, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status, data = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, data = error.code, error.read()
    return status, json.loads(data), (time.perf_counter() - start) * 1000

def record(name, method, path, body=None):
    status, response, elapsed = call(method, path, body)
    results.append({'case': name, 'method': method, 'path': path, 'status': status, 'response': response, 'elapsed_ms': round(elapsed, 3)})
    return response

record('empty runtime rule catalog', 'GET', '/api/v1/reference/canadian-rule-packs')
record('invalid Person date', 'POST', '/api/v1/people', {'name': 'Fabricated Invalid', 'birthDate': '1970-02-30'})
p = record('create fabricated Person', 'POST', '/api/v1/people', {'name': 'Fabricated Review Adult', 'birthDate': '1970-01-01', 'region': 'ON'})
h = record('create household', 'POST', '/api/v1/households', {'name': 'Fabricated Review Plan', 'personIds': [p['id']]})
a = record('create settlement Account', 'POST', '/api/v1/accounts', {'householdId': h['id'], 'displayName': 'Fabricated Chequing', 'accountType': 'Chequing', 'defaultCurrency': 'CAD', 'ownerships': [{'personId': p['id'], 'share': '1'}]})
balance = record('exact balance', 'POST', '/api/v1/accounts/' + a['id'] + '/balances', {'effectiveDate': '2026-01-01', 'expectedRevision': 0, 'amount': '100000.10', 'sourceKind': 'user_reported'})
path = '/api/v1/households/' + h['id']
s = record('create selected Snapshot', 'POST', path + '/snapshots', {'name': 'Fabricated Baseline', 'asOfDate': '2026-01-01', 'selections': [{'kind': 'account_balance', 'recordId': a['id'], 'effectiveDate': '2026-01-01', 'revision': balance['revision']}]})
sp = path + '/snapshots/' + s['draft']['id']
f = record('finalize Snapshot', 'POST', sp + '/finalize', {'expectedVersion': s['draft']['version']})
record('reject finalized Snapshot edit', 'PUT', sp + '/selections', {'expectedVersion': f['draft']['version'], 'selections': []})
scenario = record('create retirement horizon Scenario', 'POST', path + '/scenarios', {'name': 'Fabricated age 56 to 96', 'baselineSnapshotId': s['draft']['id'], 'baselineSnapshotVersion': f['draft']['version'], 'startDate': '2026-01-01', 'endDate': '2065-12-31', 'annualInflationRate': '0.02', 'annualNominalReturnRate': '0.04', 'growthOverrides': []})
record('compile against missing production pack', 'POST', path + '/scenarios/' + scenario['id'] + '/compiled-inputs', {'rulePackVersion': 'canadian-planning-rules-v1'})
with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
    reads = list(pool.map(lambda _: call('GET', path + '/snapshots'), range(200)))
latencies = sorted(row[2] for row in reads)
results.append({'case': '200 Snapshot-list requests / 20 threads', 'statuses': {str(code): sum(row[0] == code for row in reads) for code in set(row[0] for row in reads)}, 'p50_ms': round(latencies[99], 3), 'p95_ms': round(latencies[189], 3), 'max_ms': round(latencies[-1], 3), 'scope': 'one local fabricated Household, empty surrounding dataset; diagnostic only'})
print(json.dumps(results, indent=2))
