"""Fixed-period public contribution ranking, using only Python's standard library."""
import datetime
import hashlib
from email.utils import parsedate_to_datetime
import json
import os
import tempfile
from pathlib import Path
import time
import urllib.error
import urllib.request

FROM = '2025-09-17T03:11:00Z'
TO = '2026-09-17T03:11:00Z'
FIELDS = '''login location followers { totalCount }
contributionsCollection(from: $from, to: $to) {
  contributionCalendar { totalContributions }
  restrictedContributionsCount
}'''
NEXT_REQUEST_AT = 0.0


def cache_path(key):
    # Separate credentials can see different restricted contribution counts.
    scope = hashlib.sha256(os.environ['GH_TOKEN'].encode()).hexdigest()
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    return Path('cache/api') / scope / (digest + '.json')


def contribution_ttl(period):
    historical = datetime.datetime.fromisoformat(period['to']) < datetime.datetime.now(datetime.timezone.utc)
    return None if historical else 3600


def read_cache(path, ttl):
    if os.environ.get('REFRESH_CACHE', 'false').lower() != 'true' and path.exists():
        try:
            if ttl is None or 0 <= time.time() - path.stat().st_mtime < ttl:
                return json.loads(path.read_text())
        except (OSError, ValueError):
            pass
    return None


def write_cache(path, data):
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=directory, delete=False) as output:
            temporary = Path(output.name)
            json.dump(data, output)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def api(query, variables):
    path = cache_path([query, variables])
    ttl = contribution_ttl(variables) if 'from' in variables else 86400
    data = read_cache(path, ttl)
    if data is None:
        data = fetch_api(query, variables)
        write_cache(path, data)
    return data


def fetch_users(logins, period):
    users, missing = {}, []
    for login in logins:
        user = read_cache(cache_path(['user-v1', login.lower(), period]), contribution_ttl(period))
        if user is None:
            missing.append(login)
        else:
            users[login.lower()] = user
    if not missing:
        return users
    fields = '\n'.join(f'u{i}: user(login: {json.dumps(login)}) {{ {FIELDS} }}'
                       for i, login in enumerate(missing))
    try:
        data = api('query($from: DateTime!, $to: DateTime!) { ' + fields + ' }', period)
    except RuntimeError as error:
        if 'RESOURCE_LIMITS_EXCEEDED' not in str(error) or len(missing) == 1:
            raise
        middle = len(missing) // 2
        users.update(fetch_users(missing[:middle], period))
        users.update(fetch_users(missing[middle:], period))
        return users
    for user in data.values():
        if user:
            login = user['login'].lower()
            write_cache(cache_path(['user-v1', login, period]), user)
            users[login] = user
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


def render_rankings(metadata):
    for user in metadata['users']:
        total, restricted = user['totalContributions'], user['restrictedContributionsCount']
        if not 0 <= restricted <= total or user['publicContributions'] != total - restricted:
            raise ValueError(f'Inconsistent contribution counts for {user["login"]}')
    for metric, directory, title in [
        ('publicContributions', 'public_contributions', 'Public'),
        ('totalContributions', 'total_contributions', 'Total'),
    ]:
        ranking = sorted(metadata['users'], key=lambda u: (-u[metric], -u['followers'], u['login'].lower()))
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
            if user[metric] != previous:
                current_rank = position
            previous = user[metric]
            if previous > 0:
                lines.append(f'| {current_rank} | [{user["login"]}](https://github.com/{user["login"]}) | '
                             f'{user["publicContributions"]} | {user["restrictedContributionsCount"]} | '
                             f'{user["totalContributions"]} | {user["followers"]} |')
        path = Path('markdown') / directory / 'kazakhstan.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(lines) + '\n')


def main():
    country = json.loads(Path('config.json').read_text())['locations'][0]
    base = ' '.join('location:' + place.replace(' ', '_').lower()
                    for place in [country['country'], *country['cities']])
    query = '''query($q: String!, $cursor: String) {
      search(type: USER, query: $q, first: 100, after: $cursor) {
        userCount pageInfo { hasNextPage endCursor }
        nodes { ... on User { login location followers { totalCount } } }
      }
    }'''
    period = {'from': FROM, 'to': TO}
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
    found = 'physshell' in users
    logins = [user['login'] for user in users.values()]
    if not found:
        logins.append('PhysShell')
    profiles, users = users, {}

    def fetch_batch(batch):
        for login, user in fetch_users(batch, period).items():
            users[login] = {**user, **profiles.get(login, {})}
        print(f'Contributions fetched: {len(users)}/{len(logins)}', flush=True)

    for start in range(0, len(logins), 20):
        fetch_batch(logins[start:start + 20])
    target = users.get('physshell')
    if not target:
        raise RuntimeError('PhysShell missing')
    for user in users.values():
        c = user.pop('contributionsCollection')
        user['followers'] = user['followers']['totalCount']
        user['totalContributions'] = c['contributionCalendar']['totalContributions']
        user['restrictedContributionsCount'] = c['restrictedContributionsCount']
        user['publicContributions'] = user['totalContributions'] - user['restrictedContributionsCount']
    ranking = sorted(users.values(), key=lambda u: (-u['publicContributions'], -u['followers'], u['login'].lower()))
    rank = 1 + sum(u['publicContributions'] > target['publicContributions'] for u in ranking)
    metadata = {**period, 'generatedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'selectionMode': 'full-location-search' if full_coverage else 'upstream-top-1000-search-results',
                'targetFoundInLocationSearch': found, 'targetRank': rank,
                'searchPartitions': partitions, 'users': ranking}
    Path('cache').mkdir(exist_ok=True)
    Path('cache/kazakhstan-fixed-period.json').write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + '\n')
    render_rankings(metadata)
    print(f'PhysShell: {target["publicContributions"]} contributions, #{rank}', flush=True)


if __name__ == '__main__':
    main()
