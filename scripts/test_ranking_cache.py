import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import kazakhstan_ranking as ranking


class CacheTest(unittest.TestCase):
    def test_independent_rankings_and_ties_from_saved_counts(self):
        metadata = {'from': ranking.FROM, 'to': ranking.TO, 'generatedAt': 'saved-time',
                    'selectionMode': 'full-location-search', 'users': [
                        {'login': 'A', 'followers': 1, 'totalContributions': 9,
                         'restrictedContributionsCount': 0, 'publicContributions': 9},
                        {'login': 'B', 'followers': 0, 'totalContributions': 25,
                         'restrictedContributionsCount': 20, 'publicContributions': 5},
                        {'login': 'C', 'followers': 2, 'totalContributions': 10,
                         'restrictedContributionsCount': 1, 'publicContributions': 9},
                    ]}
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                ranking.render_rankings(metadata)
                public = Path('markdown/public_contributions/kazakhstan.md').read_text()
                total = Path('markdown/total_contributions/kazakhstan.md').read_text()
                self.assertIn('| 1 | [A]', public)
                self.assertIn('| 1 | [C]', public)
                self.assertIn('| 3 | [B]', public)
                self.assertIn('| 1 | [B]', total)
                self.assertIn('| 2 | [C]', total)
                self.assertIn('| 3 | [A]', total)
                self.assertIn('saved-time', total)
                metadata['users'][0]['publicContributions'] = 999
                with self.assertRaises(ValueError):
                    ranking.render_rankings(metadata)
                self.assertEqual(Path('markdown/total_contributions/kazakhstan.md').read_text(), total)
            finally:
                os.chdir(previous)

    def test_reuse_expiration_refresh_and_credentials(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                with patch.dict(os.environ, {'GH_TOKEN': 'test-only', 'REFRESH_CACHE': 'false'}):
                    with patch.object(ranking, 'fetch_api', return_value={'value': 1}) as fetch:
                        period = {'from': ranking.FROM, 'to': ranking.TO}
                        self.assertEqual(ranking.api('query', period), {'value': 1})
                        self.assertEqual(ranking.api('query', period), {'value': 1})
                        self.assertEqual(fetch.call_count, 1)
                        path = next(Path('cache/api').glob('*/*.json'))
                        path.write_text('broken')
                        ranking.api('query', period)
                        self.assertEqual(fetch.call_count, 2)
                        os.utime(path, (0, 0))
                        ranking.api('query', period)
                        self.assertEqual(fetch.call_count, 2)
                        with patch.dict(os.environ, {'GH_TOKEN': 'another-test-only'}):
                            ranking.api('query', period)
                        self.assertEqual(fetch.call_count, 3)
                        with patch.dict(os.environ, {'REFRESH_CACHE': 'true'}):
                            ranking.api('query', period)
                        self.assertEqual(fetch.call_count, 4)
                        with patch.object(ranking, 'fetch_api', side_effect=RuntimeError('API failed')):
                            with self.assertRaises(RuntimeError):
                                ranking.api('failed-query', period)
                        self.assertEqual(len(list(Path('cache/api').glob('*/*.json'))), 2)
            finally:
                os.chdir(previous)

    def test_incremental_users_survive_reordering_and_batch_changes(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                with patch.dict(os.environ, {'GH_TOKEN': 'test-only', 'REFRESH_CACHE': 'false'}):
                    period = {'from': ranking.FROM, 'to': ranking.TO}
                    alice = {'login': 'Alice', 'contributionsCollection': {'value': 10}}
                    bob = {'login': 'Bob', 'contributionsCollection': {'value': 20}}
                    with patch.object(ranking, 'fetch_api', return_value={'u0': alice, 'u1': bob}) as fetch:
                        ranking.fetch_users(['Alice', 'Bob'], period)
                        self.assertEqual(fetch.call_count, 1)
                        for path in Path('cache/api').glob('*/*.json'):
                            os.utime(path, (0, 0))
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
                        for variables in ({'q': 'search'}, {'from': ranking.FROM, 'to': '2099-01-01T00:00:00Z'}):
                            ranking.api('query', variables)
                            path = ranking.cache_path(['query', variables])
                            os.utime(path, (0, 0))
                            ranking.api('query', variables)
                        self.assertEqual(fetch.call_count, 4)
            finally:
                os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
