"""Private durable observations and complete report snapshots."""
from contextlib import closing
import datetime
import json
from pathlib import Path
import sqlite3
import time

DATABASE = Path('data/rankings.sqlite3')


def connect():
    DATABASE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not DATABASE.exists():
        DATABASE.touch(mode=0o600, exist_ok=True)
    DATABASE.chmod(0o600)
    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA journal_mode=WAL')
    connection.executescript('''
        CREATE TABLE IF NOT EXISTS responses (
            key TEXT PRIMARY KEY, fetched_at REAL NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY, scope TEXT NOT NULL, user_id TEXT NOT NULL, login TEXT NOT NULL,
            period_from TEXT NOT NULL, period_to TEXT NOT NULL, fetched_at REAL NOT NULL,
            total INTEGER NOT NULL CHECK(total >= 0),
            restricted INTEGER NOT NULL CHECK(restricted BETWEEN 0 AND total));
        CREATE INDEX IF NOT EXISTS observation_lookup
            ON observations(scope, period_from, period_to, user_id, fetched_at DESC);
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY, period_from TEXT NOT NULL, period_to TEXT NOT NULL,
            generated_at TEXT NOT NULL, metadata TEXT NOT NULL,
            UNIQUE(period_from, period_to));
        CREATE TABLE IF NOT EXISTS members (
            snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
            position INTEGER NOT NULL, login TEXT NOT NULL COLLATE NOCASE,
            profile TEXT NOT NULL, observation_id INTEGER REFERENCES observations(id),
            status TEXT NOT NULL CHECK(status IN ('ok', 'unavailable')),
            PRIMARY KEY(snapshot_id, login), UNIQUE(snapshot_id, position),
            CHECK((status = 'ok') = (observation_id IS NOT NULL)));
    ''')
    return connection


def validate_counts(total, restricted):
    if type(total) is not int or type(restricted) is not int or not 0 <= restricted <= total:
        raise ValueError('Inconsistent contribution counts')


def save_observations(scope, period, users):
    saved = {}
    with closing(connect()) as connection, connection:
        for user in users:
            counts = user['contributionsCollection']
            total = counts['contributionCalendar']['totalContributions']
            restricted = counts['restrictedContributionsCount']
            validate_counts(total, restricted)
            cursor = connection.execute('''INSERT INTO observations
                (scope, user_id, login, period_from, period_to, fetched_at, total, restricted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (scope, user['id'], user['login'], period['from'], period['to'], time.time(), total, restricted))
            saved[user['id']] = cursor.lastrowid
    return saved


def cached_observations(scope, profiles, period, ttl, refresh=False):
    if refresh:
        return {}
    identifiers = [profile['id'] for profile in profiles]
    rows = []
    with closing(connect()) as connection:
        batch_size = connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) - 3
        for start in range(0, len(identifiers), batch_size):
            batch = identifiers[start:start + batch_size]
            placeholders = ','.join('?' for _ in batch)
            rows.extend(connection.execute(f'''SELECT id, user_id, fetched_at, total, restricted
                FROM observations WHERE scope=? AND period_from=? AND period_to=?
                AND user_id IN ({placeholders}) ORDER BY fetched_at, id''',
                (scope, period['from'], period['to'], *batch)).fetchall())
    closed_at = (datetime.datetime.fromisoformat(period['to']).replace(hour=0, minute=0, second=0, microsecond=0)
                 + datetime.timedelta(days=1)).timestamp()
    now = time.time()
    by_id = {user_id: (identifier, total, restricted) for identifier, user_id, fetched, total, restricted in rows
             if 0 <= now - fetched and (fetched >= closed_at if ttl is None else now - fetched < ttl)}
    return {profile['login'].lower(): {**profile, '_observation': by_id[profile['id']][0],
                'totalContributions': by_id[profile['id']][1], 'restrictedContributionsCount': by_id[profile['id']][2]}
            for profile in profiles if profile['id'] in by_id}


def save_snapshot(metadata):
    users = metadata['users']
    extra = {k: v for k, v in metadata.items() if k not in ('from', 'to', 'generatedAt', 'users', 'missingUsers', 'unavailableUsers')}
    with closing(connect()) as connection, connection:
        cursor = connection.execute('''INSERT INTO snapshots (period_from, period_to, generated_at, metadata)
            VALUES (?, ?, ?, ?) ON CONFLICT(period_from, period_to) DO UPDATE SET
            generated_at=excluded.generated_at, metadata=excluded.metadata RETURNING id''',
            (metadata['from'], metadata['to'], metadata['generatedAt'], json.dumps(extra, ensure_ascii=False)))
        snapshot = cursor.fetchone()[0]
        connection.execute('DELETE FROM members WHERE snapshot_id=?', (snapshot,))
        for position, user in enumerate(users):
            observation = connection.execute('''SELECT total, restricted, period_from, period_to, user_id
                FROM observations WHERE id=?''', (user['_observation'],)).fetchone()
            if observation is None or observation[:4] != (user['totalContributions'], user['restrictedContributionsCount'], metadata['from'], metadata['to']):
                raise ValueError('Snapshot does not match its saved observation')
            if user['id'] != observation[4]:
                raise ValueError('Snapshot identity does not match its saved observation')
            profile = {k: v for k, v in user.items() if k not in ('_observation', 'totalContributions', 'restrictedContributionsCount', 'login', 'id')}
            connection.execute('INSERT INTO members VALUES (?, ?, ?, ?, ?, ?)',
                               (snapshot, position, user['login'], json.dumps(profile, ensure_ascii=False), user['_observation'], 'ok'))
        unavailable = metadata.get('unavailableUsers', [])
        for position, user in enumerate(unavailable, len(users)):
            profile = {k: v for k, v in user.items() if k != 'login'}
            connection.execute('INSERT INTO members VALUES (?, ?, ?, ?, NULL, ?)',
                               (snapshot, position, user['login'], json.dumps(profile, ensure_ascii=False), 'unavailable'))
        connection.execute('''DELETE FROM observations WHERE id NOT IN (
            SELECT MAX(id) FROM observations GROUP BY scope, user_id, period_from, period_to
            UNION SELECT observation_id FROM members WHERE observation_id IS NOT NULL)''')


def load_snapshots():
    # One read transaction keeps metadata and members from the same committed generation.
    with closing(connect()) as connection, connection:
        connection.execute('BEGIN')
        result = []
        for identifier, start, end, generated, extra in connection.execute('SELECT * FROM snapshots ORDER BY period_from, period_to'):
            users, missing, unavailable = [], [], []
            for login, profile, status, total, restricted, user_id in connection.execute('''
                SELECT m.login, m.profile, m.status, o.total, o.restricted, o.user_id
                FROM members m LEFT JOIN observations o ON o.id=m.observation_id
                WHERE m.snapshot_id=? ORDER BY m.position''', (identifier,)):
                if status == 'unavailable':
                    missing.append(login)
                    unavailable.append({'login': login, **json.loads(profile)})
                else:
                    users.append({'login': login, 'id': user_id, **json.loads(profile), 'totalContributions': total, 'restrictedContributionsCount': restricted})
            metadata = {**json.loads(extra), 'from': start, 'to': end, 'generatedAt': generated, 'users': users}
            if missing:
                metadata['missingUsers'] = missing
            if unavailable:
                metadata['unavailableUsers'] = unavailable
            result.append(metadata)
        return result
