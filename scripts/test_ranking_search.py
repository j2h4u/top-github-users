import json
import datetime
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import kazakhstan_ranking as ranking


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.previous = Path.cwd()
        self.directory = tempfile.TemporaryDirectory()
        os.chdir(self.directory.name)
        Path('config.json').write_text(json.dumps({'locations': [{'country': 'Kazakhstan', 'cities': ['Astana']}]}))
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(os.chdir, self.previous)

    def test_complete_partitioned_search_over_1000_without_option(self):
        profiles = [{'id': f'ID-{i}', 'login': f'u{i}', 'followers': {'totalCount': i % 2}} for i in range(1101)]
        def api(query, variables):
            self.assertIn('type:user', variables['q'])
            self.assertIn('location:astana', variables['q'])
            if 'first: 1)' in query:
                return {'search': {'nodes': [{'followers': {'totalCount': 1}}]}}
            low, high = map(int, re.search(r'followers:(\d+)\.\.(\d+)', variables['q']).groups())
            selected = [p for p in profiles if low <= p['followers']['totalCount'] <= high]
            cursor = int(variables['cursor'] or 0)
            end = cursor + 100
            return {'search': {'userCount': len(selected), 'nodes': selected[cursor:end],
                               'pageInfo': {'hasNextPage': end < len(selected), 'endCursor': str(end)}}}
        with patch.object(ranking, 'api', side_effect=api), patch('builtins.print'):
            users, partitions = ranking.collect_profiles()
        self.assertEqual(len(users), 1101)
        self.assertEqual(len(partitions), 2)
        self.assertTrue(all(p['userCount'] <= 1000 for p in partitions))

    def test_creation_dates_split_a_single_follower_bucket(self):
        profiles = [{'id': f'ID-{i}', 'login': f'u{i}', 'followers': {'totalCount': 0},
                     'created': datetime.date(2010 if i < 600 else 2020, 1, 1)} for i in range(1101)]
        def api(query, variables):
            if 'first: 1)' in query:
                return {'search': {'nodes': [{'followers': {'totalCount': 0}}]}}
            match = re.search(r'created:([\d-]+)\.\.([\d-]+)', variables['q'])
            selected = profiles
            if match:
                start, end = map(datetime.date.fromisoformat, match.groups())
                selected = [p for p in profiles if start <= p['created'] <= end]
            cursor = int(variables['cursor'] or 0)
            end = cursor + 100
            return {'search': {'userCount': len(selected), 'nodes': selected[cursor:end],
                               'pageInfo': {'hasNextPage': end < len(selected), 'endCursor': str(end)}}}
        with patch.object(ranking, 'api', side_effect=api), patch('builtins.print'):
            users, partitions = ranking.collect_profiles()
        self.assertEqual(len(users), 1101)
        self.assertTrue(all('created:' in p['query'] and p['userCount'] <= 1000 for p in partitions))

    def test_alias_groups_merge_unicode_and_spaced_locations_by_identity(self):
        aliases = [f'place{i}' for i in range(11)] + ['Қазақстан', 'Алматы', 'Qazaqstan', 'Nur Sultan']
        Path('config.json').write_text(json.dumps({'locations': [{'country': 'Kazakhstan', 'cities': ['Astana'],
                                                                 'locationAliases': aliases}]}))
        alice = {'id': 'ID-A', 'login': 'alice', 'followers': {'totalCount': 1}}
        bob = {'id': 'ID-B', 'login': 'bob', 'followers': {'totalCount': 1}}
        searches = []
        def api(query, variables):
            searches.append(variables['q'])
            nodes = [alice, bob] if 'location:алматы' in variables['q'] else [alice]
            if 'first: 1)' in query:
                return {'search': {'nodes': nodes[:1]}}
            return {'search': {'userCount': len(nodes), 'nodes': nodes,
                               'pageInfo': {'hasNextPage': False, 'endCursor': None}}}
        with patch.object(ranking, 'api', side_effect=api), patch('builtins.print'):
            users, partitions = ranking.collect_profiles()
        self.assertEqual({u['id'] for u in users}, {'ID-A', 'ID-B'})
        self.assertEqual(len(partitions), 2)
        self.assertTrue(any('location:қазақстан' in q and 'location:qazaqstan' in q for q in searches))
        self.assertTrue(any('location:"nur sultan"' in q for q in searches))
        self.assertTrue(all(q.count('location:') <= 12 for q in searches))

    def test_missing_repeated_and_cyclic_cursors_fail(self):
        for cursors in ([None], ['same', 'same'], ['a', 'b', 'a']):
            replies = [{'search': {'nodes': [{'followers': {'totalCount': 1}}]}}]
            replies += [{'search': {'userCount': 10, 'nodes': [], 'pageInfo': {'hasNextPage': True, 'endCursor': c}}} for c in cursors]
            with patch.object(ranking, 'api', side_effect=replies), patch('builtins.print'):
                with self.assertRaisesRegex(RuntimeError, 'pagination'):
                    ranking.collect_profiles()

    def test_empty_or_organization_maximum_fails_clearly(self):
        for nodes in ([], [{}]):
            with patch.object(ranking, 'api', return_value={'search': {'nodes': nodes}}):
                with self.assertRaisesRegex(RuntimeError, 'country search'):
                    ranking.collect_profiles()


if __name__ == '__main__':
    unittest.main()
