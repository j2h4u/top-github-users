import datetime
from contextlib import closing
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import kazakhstan_ranking as ranking
import ranking_store as store


PERIOD = {'from': '2025-09-17T00:00:00Z', 'to': '2026-09-17T00:00:00Z'}


def profile(login='Alice', identifier='ID-A', followers=1):
    return {'id': identifier, 'login': login, 'location': 'Kazakhstan', 'followers': {'totalCount': followers}}


def api_user(item, total=12, restricted=3):
    return {**item, 'contributionsCollection': {'contributionCalendar': {'totalContributions': total},
                                               'restrictedContributionsCount': restricted}}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.previous = Path.cwd()
        self.directory = tempfile.TemporaryDirectory()
        os.chdir(self.directory.name)
        self.environment = patch.dict(os.environ, {'GH_TOKEN': 'test-only', 'REFRESH_CACHE': 'false'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(os.chdir, self.previous)

    def snapshot(self, users, period=None, **extra):
        return {**(period or PERIOD), 'generatedAt': 'saved-time', 'selectionMode': 'full-location-search',
                'observerPolicy': 'authenticated-viewer', 'periodSemantics': 'inclusive-UTC-calendar-dates',
                'users': [{**user, 'followers': user['followers']['totalCount']} for user in users], **extra}

    def test_period_normalization_inclusive_dates_and_open_day(self):
        self.assertEqual(ranking.parse_period(['--from', '2025-09-17T03:11:00Z', '--to', '2026-09-17T12:00:00Z']), PERIOD)
        self.assertEqual(ranking.parse_period(['--from', '2025-09-17T05:00:00+05:00', '--to', '2026-09-17']), PERIOD)
        self.assertEqual(ranking.parse_period(['--from', '2025-09-17T23:00:00-02:00', '--to', '2026-09-17'])['from'], '2025-09-18T00:00:00Z')
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        current = ranking.parse_period(['--from', today, '--to', today])
        self.assertEqual(ranking.contribution_ttl(current), 3600)
        self.assertIsNone(ranking.contribution_ttl(PERIOD))
        for start, end in [('2026-10-16', '2025-10-16'), ('2024-01-01', '2026-01-01'), ('bad', '2026-10-16'), ('2026-10-16', '2099-01-01')]:
            with patch('sys.stderr'), self.assertRaises(SystemExit):
                ranking.parse_period(['--from', start, '--to', end])
        self.assertIsNone(ranking.parse_period(['--project']))

    def test_stable_identity_reuse_rename_and_login_reassignment(self):
        alice = profile()
        with patch.object(ranking, 'fetch_api', return_value={'u0': api_user(alice)}) as fetch:
            ranking.fetch_users([alice], PERIOD)
            renamed = profile('NewName', 'ID-A')
            reused = ranking.fetch_users([renamed], PERIOD)
            self.assertEqual(reused['newname']['totalContributions'], 12)
            self.assertEqual(fetch.call_count, 1)
            replacement = profile('ALICE', 'ID-B')
            fetch.return_value = {'u0': api_user(replacement, 0, 0)}
            self.assertEqual(ranking.fetch_users([replacement], PERIOD)['alice']['totalContributions'], 0)
            self.assertEqual(fetch.call_count, 2)
            self.assertIn('node(id:', fetch.call_args.args[0])
            with patch.dict(os.environ, {'GH_TOKEN': 'other-credential'}):
                ranking.fetch_users([replacement], PERIOD)
            self.assertEqual(fetch.call_count, 3)
            with patch.dict(os.environ, {'REFRESH_CACHE': 'true'}):
                ranking.fetch_users([replacement], PERIOD)
            self.assertEqual(fetch.call_count, 4)

    def test_expiration_search_cache_and_failed_response(self):
        alice = profile()
        with patch.object(ranking, 'fetch_api', return_value={'value': 1}) as fetch:
            ranking.api('search', {'q': 'test'})
            ranking.api('search', {'q': 'test'})
            self.assertEqual(fetch.call_count, 1)
            with closing(store.connect()) as connection, connection:
                connection.execute('UPDATE responses SET fetched_at=0')
                connection.execute("INSERT INTO responses VALUES ('retired-search-key', 0, '{}')")
            ranking.api('search', {'q': 'test'})
            self.assertEqual(fetch.call_count, 2)
        with patch.object(ranking, 'fetch_api', return_value={'u0': api_user(alice)}):
            ranking.fetch_users([alice], PERIOD)
        with closing(store.connect()) as connection, connection:
            connection.execute('UPDATE observations SET fetched_at=?',
                               (datetime.datetime(2026, 9, 18, tzinfo=datetime.timezone.utc).timestamp(),))
        self.assertEqual(len(ranking.cached_users([alice], PERIOD)), 1)
        current = {**PERIOD, 'to': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        with patch.object(ranking, 'fetch_api', return_value={'u0': api_user(alice)}):
            ranking.fetch_users([alice], current)
        with closing(store.connect()) as connection, connection:
            connection.execute('UPDATE observations SET fetched_at=0')
        self.assertEqual(ranking.cached_users([alice], current), {})
        with patch.object(ranking, 'fetch_api', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                ranking.api('failed', {})
        with closing(store.connect()) as connection, connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM responses').fetchone()[0], 1)

    def test_wrong_identity_missing_alias_and_bad_counts_are_not_cached(self):
        alice = profile()
        for reply in ({}, {'u0': api_user(profile(identifier='OTHER'))}, {'u0': api_user(alice, 1, 2)}):
            with patch.object(ranking, 'fetch_api', return_value=reply):
                with self.assertRaises((RuntimeError, ValueError)):
                    ranking.fetch_users([alice], PERIOD)
            self.assertEqual(ranking.cached_users([alice], PERIOD), {})

    def test_resource_limited_batch_splits_without_losing_successes(self):
        profiles = [profile(), profile('Bob', 'ID-B')]
        replies = [RuntimeError('RESOURCE_LIMITS_EXCEEDED'), {'u0': api_user(profiles[0])}, {'u0': api_user(profiles[1])}]
        with patch.object(ranking, 'fetch_api', side_effect=replies) as fetch:
            self.assertEqual(len(ranking.fetch_users(profiles, PERIOD)), 2)
            self.assertEqual(fetch.call_count, 3)
        self.assertEqual(len(ranking.cached_users(profiles, PERIOD)), 2)

    def test_partial_day_is_not_promoted_to_permanent_after_midnight(self):
        alice = profile()
        period = {**PERIOD, 'to': '2026-10-16T00:00:00Z'}
        midnight = datetime.datetime(2026, 10, 17, tzinfo=datetime.timezone.utc).timestamp()
        with patch.object(store.time, 'time', return_value=midnight - 600):
            store.save_observations(ranking.credential_scope(), period, [api_user(alice)])
        with patch.object(store.time, 'time', return_value=midnight + 60):
            self.assertEqual(store.cached_observations(ranking.credential_scope(), [alice], period, None), {})
            store.save_observations(ranking.credential_scope(), period, [api_user(alice, 14, 4)])
        with patch.object(store.time, 'time', return_value=midnight + 90 * 86400):
            cached = store.cached_observations(ranking.credential_scope(), [alice], period, None)
            self.assertEqual(cached['alice']['totalContributions'], 14)
            self.assertEqual(cached['alice']['restrictedContributionsCount'], 4)

    def test_partial_null_accounts_and_independent_snapshots(self):
        alice, bob = profile(), profile('Bob', 'ID-B')
        with patch.object(ranking, 'fetch_api', return_value={'u0': api_user(alice), 'u1': None}):
            users = ranking.fetch_users([alice, bob], PERIOD)
        first = self.snapshot(list(users.values()), unavailableUsers=[bob])
        store.save_snapshot(first)
        before = store.load_snapshots()[0]
        self.assertEqual(before['missingUsers'], ['Bob'])
        self.assertEqual(len(before['users']), 1)
        self.assertNotIn('_observation', before['users'][0])
        other = {**PERIOD, 'from': '2025-09-18T00:00:00Z'}
        with patch.object(ranking, 'fetch_api', return_value={'u0': api_user(alice, 10, 1)}):
            users = ranking.fetch_users([alice], other)
        store.save_snapshot(self.snapshot(list(users.values()), other))
        self.assertEqual(store.load_snapshots()[0], before)
        self.assertEqual(len(store.load_snapshots()), 2)
        with closing(store.connect()) as connection, connection:
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(store.DATABASE.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(Path('results').glob('**/*.json')), [])

    def test_transaction_failure_preserves_old_snapshot(self):
        alice = profile()
        with patch.object(ranking, 'fetch_api', return_value={'u0': api_user(alice)}):
            users = ranking.fetch_users([alice], PERIOD)
        good = self.snapshot(list(users.values()))
        store.save_snapshot(good)
        before = store.load_snapshots()
        bad = self.snapshot(list(users.values()))
        bad['users'][0]['totalContributions'] = 999
        with self.assertRaises(ValueError):
            store.save_snapshot(bad)
        self.assertEqual(store.load_snapshots(), before)

    def test_atomic_projector_two_rankings_ties_and_policy(self):
        metadata = {**PERIOD, 'generatedAt': 'saved-time', 'selectionMode': 'full-location-search',
                    'observerPolicy': 'authenticated-viewer', 'periodSemantics': 'inclusive-UTC-calendar-dates', 'users': [
            {'login': 'A', 'followers': 1, 'totalContributions': 9, 'restrictedContributionsCount': 0},
            {'login': 'B', 'followers': 0, 'totalContributions': 25, 'restrictedContributionsCount': 20},
            {'login': 'C', 'followers': 2, 'totalContributions': 10, 'restrictedContributionsCount': 1}]}
        path = ranking.render_rankings(metadata)
        old = path.read_bytes()
        text = old.decode()
        visible, total = text.split('## Total')
        self.assertIn('## Visible to acquisition viewer', visible)
        self.assertNotIn('## Public', text)
        self.assertIn('| 1 | [A]', visible)
        self.assertIn('| 1 | [C]', visible)
        self.assertIn('| 3 | [B]', visible)
        self.assertIn('| 1 | [B]', total)
        metadata['users'][0]['totalContributions'] = 99
        with patch.object(ranking.os, 'fsync', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                ranking.render_rankings(metadata)
        self.assertEqual(path.read_bytes(), old)
        self.assertEqual(list(path.parent.iterdir()), [path])
        metadata['users'][0]['restrictedContributionsCount'] = 999
        with self.assertRaises(ValueError):
            ranking.render_rankings(metadata)
        self.assertEqual(path.read_bytes(), old)

    def test_bulk_cached_reads_use_one_connection(self):
        profiles = [profile(f'u{i}', f'ID-{i}') for i in range(300)]
        store.save_observations(ranking.credential_scope(), PERIOD, [api_user(p) for p in profiles])
        connect = store.connect
        def limited_connection():
            connection = connect()
            connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 20)
            return connection
        with patch.object(store, 'connect', side_effect=limited_connection) as connection:
            users = ranking.cached_users(profiles, PERIOD)
        self.assertEqual(len(users), 300)
        self.assertEqual(connection.call_count, 1)

    def test_offline_projector_and_writer_lock(self):
        alice = profile()
        with patch.object(ranking, 'fetch_api', return_value={'u0': api_user(alice)}):
            users = ranking.fetch_users([alice], PERIOD)
        store.save_snapshot(self.snapshot(list(users.values())))
        with patch('sys.argv', ['ranking', '--project']), patch.object(ranking, 'fetch_api', side_effect=AssertionError('offline')):
            ranking.main()
            with store.DATABASE.open('rb') as lock:
                ranking.fcntl.flock(lock, ranking.fcntl.LOCK_EX | ranking.fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    ranking.main()

    def test_integration_missing_account_no_special_target_and_warm_restart(self):
        profiles = [profile(f'u{i}', f'ID-{i}') for i in range(41)]
        def fetch(query, variables):
            ids = [p for p in profiles if json.dumps(p['id']) in query]
            return {f'u{i}': None if p['id'] == 'ID-0' else api_user(p) for i, p in enumerate(ids)}
        with patch.object(ranking, 'collect_profiles', return_value=(profiles, [])), patch.object(ranking, 'fetch_api', side_effect=fetch) as fetching, patch('builtins.print') as printing:
            ranking.collect_ranking(PERIOD)
            self.assertEqual(fetching.call_count, 3)
            self.assertTrue(any('ETA ~' in call.args[0] for call in printing.call_args_list))
            fetching.reset_mock()
            ranking.collect_ranking(PERIOD)
            self.assertEqual(fetching.call_count, 1)
        saved = store.load_snapshots()[0]
        self.assertEqual(len(saved['users']), 40)
        self.assertEqual(saved['missingUsers'], ['u0'])
        report = (ranking.result_directory(saved) / 'contributions.md').read_text()
        self.assertIn('snapshot incomplete', report)


if __name__ == '__main__':
    unittest.main()
