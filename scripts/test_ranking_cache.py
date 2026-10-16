import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import kazakhstan_ranking as ranking


class CacheTest(unittest.TestCase):
    def test_period_validation_and_normalization(self):
        self.assertEqual(ranking.parse_period(['--from', '2025-10-16', '--to', '2026-10-16']),
                         {'from': '2025-10-16T00:00:00Z', 'to': '2026-10-16T00:00:00Z'})
        self.assertEqual(ranking.parse_period(['--from', '2025-10-16T05:00:00+05:00', '--to', '2026-10-16']),
                         ranking.parse_period(['--from', '2025-10-16', '--to', '2026-10-16']))
        for start, end in [('2026-10-16', '2025-10-16'), ('2024-01-01', '2026-01-01'),
                           ('2026-10-16', '2099-01-01'), ('bad', '2026-10-16')]:
            with patch('sys.stderr'), self.assertRaises(SystemExit):
                ranking.parse_period(['--from', start, '--to', end])

    def test_new_period_preserves_existing_snapshot(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                Path('config.json').write_text(json.dumps({'locations': [{'country': 'Kazakhstan', 'cities': []}]}))
                profile = {'login': 'PhysShell', 'location': 'Kazakhstan', 'followers': {'totalCount': 1}}
                search = {'search': {'userCount': 1, 'nodes': [profile], 'pageInfo': {'hasNextPage': False}}}
                user = {**profile, 'contributionsCollection': {'contributionCalendar': {'totalContributions': 12},
                                                             'restrictedContributionsCount': 3}}
                with patch.object(ranking, 'api', return_value=search), patch.object(ranking, 'fetch_users', return_value={'physshell': user}), patch('builtins.print'):
                    with patch('sys.argv', ['ranking', '--from', '2025-09-17', '--to', '2026-09-17']):
                        ranking.main()
                    saved = {p: p.read_bytes() for p in Path('results').rglob('*') if p.is_file()}
                    with patch('sys.argv', ['ranking', '--from', '2025-10-16', '--to', '2026-10-16']):
                        ranking.main()
                self.assertEqual(len(list(Path('results').rglob('data.json'))), 2)
                for path, data in saved.items():
                    self.assertEqual(path.read_bytes(), data)
                for path in Path('results').rglob('data.json'):
                    entry = json.loads(path.read_text())['users'][0]
                    self.assertNotIn('publicContributions', entry)
                    self.assertEqual(entry['totalContributions'], 12)
                    self.assertEqual(entry['restrictedContributionsCount'], 3)
            finally:
                os.chdir(previous)

    def test_independent_rankings_and_ties_from_saved_counts(self):
        metadata = {'from': '2025-09-17T03:11:00Z', 'to': '2026-09-17T03:11:00Z', 'generatedAt': 'saved-time',
                    'selectionMode': 'full-location-search', 'users': [
                        {'login': 'A', 'followers': 1, 'totalContributions': 9,
                         'restrictedContributionsCount': 0},
                        {'login': 'B', 'followers': 0, 'totalContributions': 25,
                         'restrictedContributionsCount': 20},
                        {'login': 'C', 'followers': 2, 'totalContributions': 10,
                         'restrictedContributionsCount': 1},
                    ]}
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                ranking.render_rankings(metadata)
                public = (ranking.result_directory(metadata) / 'public_contributions.md').read_text()
                total = (ranking.result_directory(metadata) / 'total_contributions.md').read_text()
                self.assertIn('| 1 | [A]', public)
                self.assertIn('| 1 | [C]', public)
                self.assertIn('| 3 | [B]', public)
                self.assertIn('| 1 | [B]', total)
                self.assertIn('| 2 | [C]', total)
                self.assertIn('| 3 | [A]', total)
                self.assertIn('saved-time', total)
                metadata['users'][0]['restrictedContributionsCount'] = 999
                with self.assertRaises(ValueError):
                    ranking.render_rankings(metadata)
                self.assertEqual((ranking.result_directory(metadata) / 'total_contributions.md').read_text(), total)
            finally:
                os.chdir(previous)

    def test_reuse_expiration_refresh_and_credentials(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                with patch.dict(os.environ, {'GH_TOKEN': 'test-only', 'REFRESH_CACHE': 'false'}):
                    with patch.object(ranking, 'fetch_api', return_value={'value': 1}) as fetch:
                        period = {'from': '2025-09-17T03:11:00Z', 'to': '2026-09-17T03:11:00Z'}
                        self.assertEqual(ranking.api('query', period), {'value': 1})
                        self.assertEqual(ranking.api('query', period), {'value': 1})
                        self.assertEqual(fetch.call_count, 1)
                        key = ranking.cache_key(['query', period])
                        with ranking.cache_connection() as connection:
                            connection.execute('UPDATE responses SET fetched_at = 0 WHERE key = ?', (key,))
                        ranking.api('query', period)
                        self.assertEqual(fetch.call_count, 1)
                        with patch.dict(os.environ, {'GH_TOKEN': 'another-test-only'}):
                            ranking.api('query', period)
                        self.assertEqual(fetch.call_count, 2)
                        with patch.dict(os.environ, {'REFRESH_CACHE': 'true'}):
                            ranking.api('query', period)
                        self.assertEqual(fetch.call_count, 3)
                        with patch.object(ranking, 'fetch_api', side_effect=RuntimeError('API failed')):
                            with self.assertRaises(RuntimeError):
                                ranking.api('failed-query', period)
                        with ranking.cache_connection() as connection:
                            self.assertEqual(connection.execute('SELECT count(*) FROM responses').fetchone()[0], 2)
            finally:
                os.chdir(previous)

    def test_incremental_users_survive_reordering_and_batch_changes(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                with patch.dict(os.environ, {'GH_TOKEN': 'test-only', 'REFRESH_CACHE': 'false'}):
                    period = {'from': '2025-09-17T03:11:00Z', 'to': '2026-09-17T03:11:00Z'}
                    alice = {'login': 'Alice', 'contributionsCollection': {'value': 10}}
                    bob = {'login': 'Bob', 'contributionsCollection': {'value': 20}}
                    with patch.object(ranking, 'fetch_api', return_value={'u0': alice, 'u1': bob}) as fetch:
                        ranking.fetch_users(['Alice', 'Bob'], period)
                        self.assertEqual(fetch.call_count, 1)
                        with ranking.cache_connection() as connection:
                            connection.execute('UPDATE responses SET fetched_at = 0')
                            self.assertEqual(connection.execute('SELECT count(*) FROM responses').fetchone()[0], 2)
                        self.assertEqual(ranking.fetch_users(['BOB', 'alice'], period),
                                         {'alice': alice, 'bob': bob})
                        self.assertEqual(fetch.call_count, 1)
                        fetch.return_value = {'u0': {'login': 'Carol', 'contributionsCollection': {}}}
                        ranking.fetch_users(['Bob', 'Carol', 'Alice'], period)
                        self.assertEqual(fetch.call_count, 2)
                        self.assertIn('Carol', fetch.call_args.args[0])
                        self.assertNotIn('Bob', fetch.call_args.args[0])
                        self.assertNotIn('Alice', fetch.call_args.args[0])
                        ranking.fetch_users(['Alice'], {**period, 'from': '2025-09-18T03:11:00Z'})
                        self.assertEqual(fetch.call_count, 3)
                        with patch.dict(os.environ, {'REFRESH_CACHE': 'true'}):
                            ranking.fetch_users(['Alice'], period)
                        self.assertEqual(fetch.call_count, 4)
            finally:
                os.chdir(previous)

    def test_search_and_open_period_expire(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                with patch.dict(os.environ, {'GH_TOKEN': 'test-only', 'REFRESH_CACHE': 'false'}):
                    with patch.object(ranking, 'fetch_api', return_value={'value': 1}) as fetch:
                        for variables in ({'q': 'search'}, {'from': '2025-09-17T03:11:00Z', 'to': '2099-01-01T00:00:00Z'}):
                            ranking.api('query', variables)
                            key = ranking.cache_key(['query', variables])
                            with ranking.cache_connection() as connection:
                                connection.execute('UPDATE responses SET fetched_at = 0 WHERE key = ?', (key,))
                            ranking.api('query', variables)
                        self.assertEqual(fetch.call_count, 4)
            finally:
                os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
