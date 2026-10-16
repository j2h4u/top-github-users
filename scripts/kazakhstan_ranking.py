"""Contribution rankings for an exact period, using only the standard library."""
import argparse
from contextlib import closing
import datetime
import hashlib
from email.utils import parsedate_to_datetime
import json
import os
import sqlite3
import tempfile
from pathlib import Path
import time
import urllib.error
import urllib.request

FIELDS = '''login location followers { totalCount }
contributionsCollection(from: $from, to: $to) {
  contributionCalendar { totalContributions }
  restrictedContributionsCount
}'''
NEXT_REQUEST_AT = 0.0


def cache_key(key):
    # Separate credentials can see different restricted contribution counts.
    scope = hashlib.sha256(os.environ['GH_TOKEN'].encode()).hexdigest()
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    return scope + '/' + digest


def cache_connection():
    path = Path('cache/api.sqlite3')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    path.chmod(0o600)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, fetched_at REAL NOT NULL, data TEXT NOT NULL)')
    return connection


def contribution_ttl(period):
    historical = datetime.datetime.fromisoformat(period['to']) < datetime.datetime.now(datetime.timezone.utc)
    return None if historical else 3600


def read_cache(key, ttl):
    if os.environ.get('REFRESH_CACHE', 'false').lower() != 'true':
        with closing(cache_connection()) as connection:
            row = connection.execute('SELECT fetched_at, data FROM responses WHERE key = ?', (key,)).fetchone()
        if row is not None and (ttl is None or 0 <= time.time() - row[0] < ttl):
            return json.loads(row[1])
    return None


def write_cache(entries):
    with closing(cache_connection()) as connection, connection:
        connection.executemany('INSERT OR REPLACE INTO responses VALUES (?, ?, ?)',
                               [(key, time.time(), json.dumps(data, separators=(',', ':')))
                                for key, data in entries.items()])


def write_json(path, data):
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=directory, delete=False) as output:
            temporary = Path(output.name)
            json.dump(data, output, ensure_ascii=False, separators=(',', ':'))
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def api(query, variables):
    path = cache_key([query, variables])
    ttl = contribution_ttl(variables) if 'from' in variables else 86400
    data = read_cache(path, ttl)
    if data is None:
        data = fetch_api(query, variables)
        write_cache({path: data})
    return data


def cached_users(logins, period):
    users = {}
    for login in logins:
        user = read_cache(cache_key(['user-v1', login.lower(), period]), contribution_ttl(period))
        if user is not None:
            users[login.lower()] = user
    return users


def fetch_users(logins, period):
    users = cached_users(logins, period)
    missing = [login for login in logins if login.lower() not in users]
    if not missing:
        return users
    fields = '\n'.join(f'u{i}: user(login: {json.dumps(login)}) {{ {FIELDS} }}'
                       for i, login in enumerate(missing))
    try:
        data = fetch_api('query($from: DateTime!, $to: DateTime!) { ' + fields + ' }', period)
    except RuntimeError as error:
        if 'RESOURCE_LIMITS_EXCEEDED' not in str(error) or len(missing) == 1:
            raise
        middle = len(missing) // 2
        users.update(fetch_users(missing[:middle], period))
        users.update(fetch_users(missing[middle:], period))
        return users
    entries = {}
    for user in data.values():
        if user:
            login = user['login'].lower()
            entries[cache_key(['user-v1', login, period])] = user
            users[login] = user
    write_cache(entries)
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
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                headers = response.headers
                result = json.load(response)
            if headers.get('x-ratelimit-remaining') == '0':
                NEXT_REQUEST_AT = time.time() + retry_delay(headers, attempt, True)
            if result.get('errors'):
                message = json.dumps(result['errors'])
                limited = any(e.get('type') == 'RATE_LIMITED' for e in result['errors']) or 'rate limit' in message.lower()
                if not limited:
                    if 'timeout' in message.lower() or 'something went wrong' in message.lower():
                        raise TimeoutError(message)
                    raise RuntimeError(message)
            else:
                if not isinstance(result.get('data'), dict):
                    raise RuntimeError('GitHub returned no data object')
                return result['data']
        except urllib.error.HTTPError as error:
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
        except (urllib.error.URLError, TimeoutError):
            pass
        if attempt == 4:
            raise RuntimeError('GitHub request failed after five attempts')
        NEXT_REQUEST_AT = max(NEXT_REQUEST_AT, time.time() + retry_delay(headers, attempt, limited))


def contribution_count(user, metric):
    if metric == 'publicContributions':
        return user['totalContributions'] - user['restrictedContributionsCount']
    return user[metric]


def result_directory(period):
    return Path('results') / (period['from'] + '--' + period['to']).replace(':', '')


def render_rankings(metadata):
    for user in metadata['users']:
        total, restricted = user['totalContributions'], user['restrictedContributionsCount']
        if not 0 <= restricted <= total:
            raise ValueError(f'Inconsistent contribution counts for {user["login"]}')
    for metric, directory, title in [
        ('publicContributions', 'public_contributions', 'Public'),
        ('totalContributions', 'total_contributions', 'Total'),
    ]:
        ranking = sorted(metadata['users'], key=lambda u: (-contribution_count(u, metric), -u['followers'], u['login'].lower()))
        lines = [f'# Kazakhstan {title.lower()} contributions — fixed period', '',
            f'Period: `{metadata["from"]}` → `{metadata["to"]}`.',
            f'Data collected: `{metadata["generatedAt"]}`.',
            f'Accounts fetched: **{len(ranking)}**.', '',
            f'Selection mode: `{metadata["selectionMode"]}`.', '',
            'Equal contribution counts share a rank.',
            'Coverage includes configured GitHub location search results, not every developer in Kazakhstan.',
            'Counts include contribution activity, not only commits.',
            'Public is the legacy label for total minus restricted-detail contributions; classification depends on viewer access.',
            'Total includes the restricted-detail counts disclosed by each account; undisclosed activity is unavailable.',
            'Locations and follower counts reflect collection time.', '',
            '| Rank | User | Public | Restricted detail | Total | Followers |',
            '| --- | --- | --- | --- | --- | --- |']
        previous = None
        for position, user in enumerate(ranking, 1):
            count = contribution_count(user, metric)
            if count != previous:
                current_rank = position
            previous = count
            if previous > 0:
                lines.append(f'| {current_rank} | [{user["login"]}](https://github.com/{user["login"]}) | '
                             f'{contribution_count(user, "publicContributions")} | {user["restrictedContributionsCount"]} | '
                             f'{user["totalContributions"]} | {user["followers"]} |')
        path = result_directory(metadata) / (directory + '.md')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(lines) + '\n')


def parse_period(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from', dest='start', required=True, help='Inclusive ISO-8601 start (UTC if date only)')
    parser.add_argument('--to', dest='end', required=True, help='ISO-8601 end (UTC if date only); at most one year after start')
    args = parser.parse_args(arguments)
    try:
        start, end = [datetime.datetime.fromisoformat(value) for value in (args.start, args.end)]
        start, end = [value.replace(tzinfo=datetime.timezone.utc) if value.tzinfo is None
                      else value.astimezone(datetime.timezone.utc) for value in (start, end)]
        if not start < end <= start.replace(year=start.year + 1, day=min(start.day, 28) if start.month == 2 else start.day):
            raise ValueError('Period must be positive and at most one calendar year')
        if end > datetime.datetime.now(datetime.timezone.utc):
            raise ValueError('Period end must not be in the future')
    except ValueError as error:
        parser.error(str(error))
    return {key: value.isoformat().replace('+00:00', 'Z') for key, value in [('from', start), ('to', end)]}


def main():
    started = time.monotonic()
    period = parse_period()
    print(f'Period: {period["from"]} -> {period["to"]}', flush=True)
    country = json.loads(Path('config.json').read_text())['locations'][0]
    base = ' '.join('location:' + place.replace(' ', '_').lower()
                    for place in [country['country'], *country['cities']])
    query = '''query($q: String!, $cursor: String) {
      search(type: USER, query: $q, first: 100, after: $cursor) {
        userCount pageInfo { hasNextPage endCursor }
        nodes { ... on User { login location followers { totalCount } } }
      }
    }'''
    users, partitions = {}, []

    def collect(low, high, created_start=None, created_end=None):
        q = f'{base} followers:{low}..{high} sort:followers-desc'
        if created_start is not None:
            q += f' created:{created_start.isoformat()}..{created_end.isoformat()}'
        cursor, first = None, True
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
                if user.get('login'):
                    users[user['login'].lower()] = user
            print(f'Fetched {len(users)} users', flush=True)
            if not data['pageInfo']['hasNextPage']:
                return
            cursor = data['pageInfo']['endCursor']

    maximum = api('''query($q: String!) {
      search(type: USER, query: $q, first: 1) {
        nodes { ... on User { followers { totalCount } } }
      }
    }''', {'q': base + ' sort:followers-desc'})['search']['nodes']
    if not maximum:
        raise RuntimeError('Empty country search')
    full_coverage = os.environ.get('FULL_COVERAGE', 'false').lower() == 'true'
    if full_coverage:
        collect(0, maximum[0]['followers']['totalCount'])
    else:
        cursor = None
        for page in range(10):
            q = base + ' sort:followers-desc'
            data = api(query, {'q': q, 'cursor': cursor})['search']
            if page == 0:
                partitions.append({'query': q, 'userCount': data['userCount'], 'resultLimit': 1000})
            for user in data['nodes']:
                if user.get('login'):
                    users[user['login'].lower()] = user
            print(f'Fetched {len(users)} users (upstream selection)', flush=True)
            if not data['pageInfo']['hasNextPage']:
                break
            cursor = data['pageInfo']['endCursor']
    print(f'Location search completed in {time.monotonic() - started:.1f}s: {len(users)} users', flush=True)
    found = 'physshell' in users
    logins = [user['login'] for user in users.values()]
    if not found:
        logins.append('PhysShell')
    profiles, users = users, {}

    def add_users(fetched):
        for login, user in fetched.items():
            users[login] = {**user, **profiles.get(login, {})}

    add_users(cached_users(logins, period))
    missing = [login for login in logins if login.lower() not in users]
    print(f'Contributions cached: {len(users)}/{len(logins)}; to fetch: {len(missing)}', flush=True)
    fetching_started = time.monotonic()
    for start in range(0, len(missing), 20):
        add_users(fetch_users(missing[start:start + 20], period))
        completed = min(start + 20, len(missing))
        elapsed = time.monotonic() - fetching_started
        eta = datetime.timedelta(seconds=round(elapsed * (len(missing) - completed) / completed))
        print(f'Contributions fetched: {len(users)}/{len(logins)}; ETA ~{eta}', flush=True)
    target = users.get('physshell')
    if not target:
        raise RuntimeError('PhysShell missing')
    for user in users.values():
        c = user.pop('contributionsCollection')
        user['followers'] = user['followers']['totalCount']
        user['totalContributions'] = c['contributionCalendar']['totalContributions']
        user['restrictedContributionsCount'] = c['restrictedContributionsCount']
    ranking = sorted(users.values(), key=lambda u: (-contribution_count(u, 'publicContributions'), -u['followers'], u['login'].lower()))
    rank = 1 + sum(contribution_count(u, 'publicContributions') > contribution_count(target, 'publicContributions') for u in ranking)
    metadata = {**period, 'generatedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'selectionMode': 'full-location-search' if full_coverage else 'upstream-top-1000-search-results',
                'targetFoundInLocationSearch': found, 'targetRank': rank,
                'searchPartitions': partitions, 'users': ranking}
    render_rankings(metadata)
    write_json(result_directory(period) / 'data.json', metadata)
    for login in ('j2h4u', 'physshell'):
        if login in users:
            user = users[login]
            for metric in ('publicContributions', 'totalContributions'):
                count = contribution_count(user, metric)
                rank = 1 + sum(contribution_count(other, metric) > count for other in ranking)
                print(f'{user["login"]}: {metric}={count}, rank={rank}', flush=True)
    print(f'Completed in {time.monotonic() - started:.1f}s; results: {result_directory(period)}', flush=True)


if __name__ == '__main__':
    main()
