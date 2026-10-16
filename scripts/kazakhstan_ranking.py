"""Contribution rankings for inclusive UTC calendar dates; standard library only."""
import argparse
from contextlib import closing
import datetime
import hashlib
import fcntl
import http.client
from email.utils import parsedate_to_datetime
import json
import os
import tempfile
from pathlib import Path
import time
import urllib.error
import urllib.request

import ranking_store as store

FIELDS = '''id login
contributionsCollection(from: $from, to: $to) {
  contributionCalendar { totalContributions }
  restrictedContributionsCount
}'''
NEXT_REQUEST_AT = 0.0

def cache_key(key):
    # Separate credentials can see different restricted contribution counts.
    scope = credential_scope()
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    return scope + '/' + digest


def contribution_ttl(period):
    historical = datetime.datetime.fromisoformat(period['to']).date() < datetime.datetime.now(datetime.timezone.utc).date()
    return None if historical else 3600

def read_cache(key, ttl):
    if os.environ.get('REFRESH_CACHE', 'false').lower() != 'true':
        with closing(store.connect()) as connection:
            row = connection.execute('SELECT fetched_at, data FROM responses WHERE key = ?', (key,)).fetchone()
        if row is not None and (ttl is None or 0 <= time.time() - row[0] < ttl):
            return json.loads(row[1])
    return None

def write_cache(entries):
    with closing(store.connect()) as connection, connection:
        connection.execute('DELETE FROM responses WHERE fetched_at < ?', (time.time() - 86400,))
        connection.executemany('INSERT OR REPLACE INTO responses VALUES (?, ?, ?)',
                               [(key, time.time(), json.dumps(data, separators=(',', ':')))
                                for key, data in entries.items()])


def api(query, variables):
    path = cache_key([query, variables])
    data = read_cache(path, 86400)
    if data is None:
        data = fetch_api(query, variables)
        write_cache({path: data})
    return data

def credential_scope():
    return hashlib.sha256(os.environ['GH_TOKEN'].encode()).hexdigest()

def cached_users(profiles, period, reuse_snapshot=False):
    return store.cached_observations(credential_scope(), profiles, period, contribution_ttl(period),
                                    os.environ.get('REFRESH_CACHE', 'false').lower() == 'true', reuse_snapshot)

def fetch_users(profiles, period):
    users = cached_users(profiles, period)
    missing = [profile for profile in profiles if profile['login'].lower() not in users]
    if not missing:
        return users
    fields = '\n'.join(f'u{i}: node(id: {json.dumps(profile["id"])}) {{ ... on User {{ {FIELDS} }} }}'
                       for i, profile in enumerate(missing))
    try:
        data = fetch_api('query($from: DateTime!, $to: DateTime!) { ' + fields + ' }', period)
    except RuntimeError as error:
        if 'RESOURCE_LIMITS_EXCEEDED' not in str(error) or len(missing) == 1:
            raise
        middle = len(missing) // 2
        users.update(fetch_users(missing[:middle], period))
        users.update(fetch_users(missing[middle:], period))
        return users
    valid = []
    for i, profile in enumerate(missing):
        if f'u{i}' not in data:
            raise RuntimeError('GitHub omitted a requested account alias')
        user = data[f'u{i}']
        if user is not None:
            if user.get('id') != profile['id']:
                raise RuntimeError('GitHub returned a different account identity')
            valid.append(user)
    saved = store.save_observations(credential_scope(), period, valid)
    for i, profile in enumerate(missing):
        user = data[f'u{i}']
        if user is not None:
            counts = user['contributionsCollection']
            users[profile['login'].lower()] = {**profile, '_observation': saved[user['id']],
                'totalContributions': counts['contributionCalendar']['totalContributions'],
                'restrictedContributionsCount': counts['restrictedContributionsCount']}
    return users

def retry_delay(headers, attempt, limited):
    delay = None
    if headers.get('Retry-After') is not None:
        try:
            delay = max(0, float(headers['Retry-After']))
        except ValueError:
            try:
                delay = max(0, parsedate_to_datetime(headers['Retry-After']).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                pass
    if headers.get('x-ratelimit-remaining') == '0':
        try:
            delay = max(delay or 0, float(headers['x-ratelimit-reset']) - time.time() + 1)
        except (KeyError, ValueError):
            pass
    return max(0, delay) if delay is not None else (60 if limited else 2) * 2 ** attempt

def fetch_api(query, variables):
    global NEXT_REQUEST_AT
    request = urllib.request.Request('https://api.github.com/graphql',
        json.dumps({'query': query, 'variables': variables}).encode(),
        {'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
         'Content-Type': 'application/json', 'User-Agent': 'kazakhstan-ranking'})
    for attempt in range(5):
        wait = NEXT_REQUEST_AT - time.time()
        if wait > 0:
            print(f'GitHub retry/limit; waiting {wait:.1f}s', flush=True)
            time.sleep(wait)
        NEXT_REQUEST_AT = 0.0
        limited, headers = False, {}
        failure = None
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                headers = response.headers
                result = json.load(response)
            if headers.get('x-ratelimit-remaining') == '0':
                NEXT_REQUEST_AT = time.time() + retry_delay(headers, attempt, True)
            if result.get('errors'):
                message = json.dumps(result['errors'])
                failure = message
                limited = any(e.get('type') == 'RATE_LIMITED' for e in result['errors']) or 'rate limit' in message.lower()
                if (not limited and isinstance(result.get('data'), dict)
                        and all(e.get('type') == 'NOT_FOUND' and len(e.get('path', [])) == 1
                                and e['path'][0] in result['data'] and result['data'][e['path'][0]] is None
                                for e in result['errors'])):
                    return result['data']
                if not limited:
                    if 'timeout' in message.lower() or 'something went wrong' in message.lower():
                        raise TimeoutError(message)
                    raise RuntimeError(message)
            else:
                if not isinstance(result.get('data'), dict):
                    raise RuntimeError('GitHub returned no data object')
                return result['data']
        except urllib.error.HTTPError as error:
            failure = error
            headers = error.headers or {}
            if error.code in (403, 429):
                body = error.read().decode('utf-8', errors='replace')
                limited = (error.code == 429 or headers.get('Retry-After') is not None
                           or headers.get('x-ratelimit-remaining') == '0'
                           or any(term in body.lower() for term in ('rate limit', 'abuse', 'temporarily')))
                if not limited:
                    raise RuntimeError(f'GitHub HTTP 403: {body}') from error
            elif error.code not in (500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError, http.client.IncompleteRead, ConnectionError) as error:
            failure = error
        if attempt == 4:
            raise RuntimeError(f'GitHub request failed after five attempts: {failure}')
        NEXT_REQUEST_AT = max(NEXT_REQUEST_AT, time.time() + retry_delay(headers, attempt, limited))

def contribution_count(user, metric):
    if metric == 'visibleContributions':
        return user['totalContributions'] - user['restrictedContributionsCount']
    return user[metric]

def result_directory(period):
    return Path('results') / (period['from'] + '--' + period['to']).replace(':', '')

def render_rankings(metadata):
    for user in metadata['users']:
        store.validate_counts(user['totalContributions'], user['restrictedContributionsCount'])
    label = 'Visible to acquisition viewer'
    lines = ['# Kazakhstan contribution rankings', '',
        f'Period: `{metadata["from"]}` → `{metadata["to"]}`.',
        f'Snapshot completed: `{metadata["generatedAt"]}`.',
        f'Accounts fetched: **{len(metadata["users"])}**.',
        f'Selection mode: `{metadata["selectionMode"]}`.',
        f'Observer policy: `{metadata["observerPolicy"]}`.',
        'Both UTC dates are included.' + (' Some counts were collected before the final UTC day ended and may be incomplete.'
                                        if metadata['countsProvisional'] else ''),
        'Counts reflect their individual acquisition times.',
        f'Unavailable accounts: {len(metadata.get("missingUsers", []))}; snapshot ' +
            ('incomplete.' if metadata.get('missingUsers') else 'complete.'), '',
        'Equal contribution counts share a rank. Counts are contributions, not only commits.',
        'Coverage is configured GitHub location search, not a census of Kazakhstan.',
        'The first metric is total minus restricted-detail counts. With an unrestricted or unknown observer, it may include private activity visible to that observer.',
        'Total includes disclosed restricted counts; undisclosed activity is unavailable.',
        'Locations and followers reflect collection time.', '']
    if metadata.get('missingUsers'):
        lines += ['Unavailable accounts: ' + ', '.join(metadata['missingUsers']) + '.', '']
    for metric, title in [('visibleContributions', label), ('totalContributions', 'Total')]:
        ranking = sorted(metadata['users'], key=lambda u: (-contribution_count(u, metric), -u['followers'], u['login'].lower()))
        lines += [f'## {title}', '', f'| Rank | User | {label} | Restricted detail | Total | Followers |',
                  '| --- | --- | --- | --- | --- | --- |']
        previous = None
        for position, user in enumerate(ranking, 1):
            count = contribution_count(user, metric)
            if count != previous:
                current_rank = position
            previous = count
            if count > 0:
                lines.append(f'| {current_rank} | [{user["login"]}](https://github.com/{user["login"]}) | '
                             f'{contribution_count(user, "visibleContributions")} | {user["restrictedContributionsCount"]} | '
                             f'{user["totalContributions"]} | {user["followers"]} |')
        lines.append('')
    directory = result_directory(metadata)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write('\n'.join(lines))
            output.flush()
            os.fsync(output.fileno())
            temporary.replace(directory / 'contributions.md')
        finally:
            temporary.unlink(missing_ok=True)
    return directory / 'contributions.md'

def parse_period(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from', dest='start', help='First included UTC calendar date')
    parser.add_argument('--to', dest='end', help='Last included UTC calendar date; at most one year after start')
    parser.add_argument('--expand', action='store_true', help='Expand membership, retaining existing snapshot counts regardless of age')
    parser.add_argument('--project', action='store_true', help='Rebuild all reports from SQLite, without GitHub')
    args = parser.parse_args(arguments)
    if args.project:
        if args.start or args.end or args.expand:
            parser.error('--project takes no period: it projects all stored snapshots')
        return None
    if not args.start or not args.end:
        parser.error('--from and --to are required for collection')
    try:
        start, end = [datetime.datetime.fromisoformat(value) for value in (args.start, args.end)]
        start, end = [value.replace(tzinfo=datetime.timezone.utc) if value.tzinfo is None
                      else value.astimezone(datetime.timezone.utc) for value in (start, end)]
        start, end = [value.replace(hour=0, minute=0, second=0, microsecond=0) for value in (start, end)]
        anniversary = start.replace(year=start.year + 1, day=28 if start.month == 2 and start.day == 29 else start.day)
        if not start <= end <= anniversary:
            raise ValueError('Period must be ordered and at most one calendar year')
        if end.date() > datetime.datetime.now(datetime.timezone.utc).date():
            raise ValueError('Period end must not be in the future')
    except (ValueError, OverflowError) as error:
        parser.error(str(error))
    period = {key: value.isoformat().replace('+00:00', 'Z') for key, value in [('from', start), ('to', end)]}
    if args.expand:
        period['_expand'] = True
    return period

def collect_profiles():
    country = json.loads(Path('config.json').read_text(encoding='utf-8'))['locations'][0]
    locations = list(dict.fromkeys(place.lower() for place in
        [country['country'], *country['cities'], *country.get('locationAliases', [])]))
    query = '''query($q: String!, $cursor: String) {
      search(type: USER, query: $q, first: 100, after: $cursor) {
        userCount pageInfo { hasNextPage endCursor }
        nodes { ... on User { id login location followers { totalCount } } }
      }
    }'''
    users, partitions = {}, []

    def collect(low, high, created_start=None, created_end=None):
        q = f'{base} followers:{low}..{high} sort:followers-desc'
        if created_start is not None:
            q += f' created:{created_start.isoformat()}..{created_end.isoformat()}'
        cursor, seen, first = None, set(), True
        while True:
            data = api(query, {'q': q, 'cursor': cursor})['search']
            if first and data['userCount'] > 1000:
                if low != high:
                    middle = (low + high) // 2
                    collect(low, middle)
                    collect(middle + 1, high)
                else:
                    start = created_start or datetime.date(2008, 1, 1)
                    end = created_end or datetime.datetime.now(datetime.timezone.utc).date()
                    if start == end:
                        raise RuntimeError(f'Search limit exceeded at {low} followers on {start}')
                    middle = start + (end - start) // 2
                    collect(low, high, start, middle)
                    collect(low, high, middle + datetime.timedelta(days=1), end)
                return
            if first:
                partitions.append({'query': q, 'userCount': data['userCount']})
            first = False
            for user in data['nodes']:
                if not user.get('id') or not user.get('login'):
                    raise RuntimeError('User search returned an account without identity')
                if not any(fragment.lower() in (user.get('location') or '').lower()
                           for fragment in country.get('excludedLocationFragments', [])):
                    users[user['id']] = user
            print(f'Fetched {len(users)} users', flush=True)
            if not data['pageInfo']['hasNextPage']:
                return
            cursor = data['pageInfo']['endCursor']
            if not cursor or cursor in seen:
                raise RuntimeError('GitHub pagination did not advance')
            seen.add(cursor)

    # Bound location groups so expanding aliases does not exceed GitHub search limits.
    for offset in range(0, len(locations), 12):
        base = 'type:user ' + ' '.join('location:' + (json.dumps(place, ensure_ascii=False) if ' ' in place else place)
            for place in locations[offset:offset + 12])
        maximum = api('''query($q: String!) {
          search(type: USER, query: $q, first: 1) {
            nodes { ... on User { followers { totalCount } } }
          }
        }''', {'q': base + ' sort:followers-desc'})['search']['nodes']
        if maximum:
            if 'followers' not in maximum[0]:
                raise RuntimeError('Invalid country search')
            collect(0, maximum[0]['followers']['totalCount'])
    if not users:
        raise RuntimeError('Empty country search')
    if len({user['login'].lower() for user in users.values()}) != len(users):
        raise RuntimeError('Account names changed during search; refresh the search before retrying')
    return list(users.values()), partitions

def collect_ranking(period, expand=False):
    started = time.monotonic()
    print(f'Included UTC calendar dates: {period["from"][:10]} -> {period["to"][:10]}', flush=True)
    profiles, partitions = collect_profiles()
    print(f'Location search completed in {time.monotonic() - started:.1f}s: {len(profiles)} users', flush=True)
    users = cached_users(profiles, period, reuse_snapshot=expand)
    missing = [profile for profile in profiles if profile['login'].lower() not in users]
    print(f'Contributions cached: {len(users)}/{len(profiles)}; to fetch: {len(missing)}', flush=True)
    fetching_started = time.monotonic()
    for start in range(0, len(missing), 20):
        users.update(fetch_users(missing[start:start + 20], period))
        completed = min(start + 20, len(missing))
        elapsed = time.monotonic() - fetching_started
        eta = datetime.timedelta(seconds=round(elapsed * (len(missing) - completed) / completed))
        print(f'Contributions fetched: {len(users)}/{len(profiles)}; ETA ~{eta}', flush=True)
    records = [{**user, 'followers': user['followers']['totalCount']} for user in users.values()]
    metadata = {**period, 'generatedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'selectionMode': 'full-location-search', 'periodSemantics': 'inclusive-UTC-calendar-dates',
                'observerPolicy': 'authenticated-viewer', 'credentialScope': credential_scope(),
                'searchPartitions': partitions, 'users': records,
                'unavailableUsers': [p for p in profiles if p['login'].lower() not in users]}
    store.save_snapshot(metadata)
    # Project the committed database, never an uncommitted in-memory candidate.
    saved = next(s for s in store.load_snapshots() if s['from'] == period['from'] and s['to'] == period['to'])
    path = render_rankings(saved)
    print(f'Completed in {time.monotonic() - started:.1f}s; report: {path}', flush=True)

def main():
    period = parse_period()
    with closing(store.connect()):
        pass
    # Serialize collection and projection without holding a database transaction during HTTP.
    with store.DATABASE.open('rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if period is None:
            for metadata in store.load_snapshots():
                print(render_rankings(metadata))
        else:
            collect_ranking(period, expand=period.pop('_expand', False))


if __name__ == '__main__':
    main()
