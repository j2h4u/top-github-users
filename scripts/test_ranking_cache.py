import datetime
from contextlib import closing
from dataclasses import fields, replace
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
    return {'id': item['id'], 'login': item['login'], 'contributionsCollection': {'contributionCalendar': {'totalContributions': total},
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
        return {**(period or PERIOD), 'generatedAt': '2026-09-18T12:00:00Z', 'selectionMode': 'full-location-search',
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
        for reply in ({}, {'u0': api_user(profile(identifier='OTHER'))}, {'u0': api_user(alice, 1, 2)},
                      {'u0': {**api_user(alice), 'login': ''}}, {'u0': {**api_user(alice), 'login': None}}):
            with patch.object(ranking, 'fetch_api', return_value=reply):
                with self.assertRaises((RuntimeError, ValueError)):
                    ranking.fetch_users([alice], PERIOD)
            self.assertEqual(ranking.cached_users([alice], PERIOD), {})

    def test_rename_and_reused_login_keep_each_account_and_its_counts(self):
        alice, bob = profile('OldName'), profile('OtherSearchName', 'ID-B')
        renamed = {**api_user(alice), 'login': 'NewName'}
        reused = {**api_user(bob, 20, 4), 'login': 'OldName'}
        with patch.object(ranking, 'collect_profiles', return_value=([alice, bob], [])), \
             patch.object(ranking, 'fetch_api', return_value={'u0': renamed, 'u1': reused}), patch('builtins.print'):
            ranking.collect_ranking(PERIOD)
        saved = store.load_snapshots()[0]
        self.assertNotIn('missingUsers', saved)
        users = {u['id']: u for u in saved['users']}
        self.assertEqual((users['ID-A']['login'], users['ID-A']['totalContributions']), ('NewName', 12))
        self.assertEqual((users['ID-B']['login'], users['ID-B']['totalContributions']), ('OldName', 20))
        text = (ranking.result_directory(PERIOD) / 'README.md').read_text()
        self.assertIn('[NewName](https://github.com/NewName)', text)
        with patch.object(ranking, 'fetch_api', side_effect=AssertionError('cached')):
            cached = ranking.fetch_users([profile('NewName'), profile('OldName', 'ID-B')], PERIOD)
        self.assertEqual(cached['newname']['totalContributions'], 12)
        self.assertEqual(cached['oldname']['totalContributions'], 20)

    def test_case_only_rename_uses_returned_login(self):
        alice = profile()
        with patch.object(ranking, 'fetch_api', return_value={'u0': {**api_user(alice), 'login': 'ALICE'}}):
            users = ranking.fetch_users([alice], PERIOD)
        self.assertEqual(users['alice']['login'], 'ALICE')

    def test_cached_search_cannot_revert_a_newer_login_observation(self):
        baseline = datetime.datetime(2026, 9, 18, tzinfo=datetime.timezone.utc).timestamp()
        alice = {**profile('OldName'), '_profileFetchedAt': baseline + 100}
        with patch.object(store.time, 'time', return_value=baseline + 200), \
             patch.object(ranking, 'fetch_api', return_value={'u0': {**api_user(alice), 'login': 'NewName'}}):
            original = ranking.fetch_users([alice], PERIOD)
        self.assertEqual(original['oldname']['login'], 'NewName')
        with patch.object(ranking, 'fetch_api', side_effect=AssertionError('cached')):
            warm = ranking.fetch_users([alice], PERIOD)
            self.assertEqual(warm['oldname']['login'], 'NewName')
            fresh_profile = {**alice, 'login': 'NewestName', '_profileFetchedAt': baseline + 300}
            fresh = ranking.fetch_users([fresh_profile], PERIOD)
            self.assertEqual(fresh['newestname']['login'], 'NewestName')

    def test_final_login_collisions_preserve_previous_snapshot_and_report(self):
        ready = profile('Ready', 'ID-READY')
        with patch.object(ranking, 'fetch_api', return_value={'u0': api_user(ready)}):
            original = ranking.fetch_users([ready], PERIOD)
        store.save_snapshot(self.snapshot(list(original.values())))
        before = store.load_snapshots()
        report = ranking.render_rankings(before[0])
        previous = report.read_bytes()
        for unavailable in (False, True):
            profiles = [profile(f'u{i}', f'ID-{i}') for i in range(21)]
            profiles[-1]['login'] = 'same'
            def fetch(query, variables):
                batch = [p for p in profiles if json.dumps(p['id']) in query]
                return {f'u{i}': None if unavailable and p['id'] == 'ID-20' else
                        {**api_user(p), 'login': 'Same' if p['id'] in ('ID-0', 'ID-20') else p['login']}
                        for i, p in enumerate(batch)}
            with patch.dict(os.environ, {'REFRESH_CACHE': 'true'}), \
                 patch.object(ranking, 'collect_profiles', return_value=(profiles, [])), \
                 patch.object(ranking, 'fetch_api', side_effect=fetch), patch('builtins.print'):
                with self.assertRaisesRegex(RuntimeError, 'names changed during collection'):
                    ranking.collect_ranking(PERIOD)
            self.assertEqual(store.load_snapshots(), before)
            self.assertEqual(report.read_bytes(), previous)

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

    def test_expansion_keeps_provisional_warning_until_final_counts_are_acquired(self):
        alice, bob = profile(), profile('Bob', 'ID-B')
        closed_at = datetime.datetime(2026, 9, 18, tzinfo=datetime.timezone.utc).timestamp()
        with patch.object(store.time, 'time', return_value=closed_at - 600), \
             patch.object(ranking, 'fetch_api', return_value={'u0': api_user(alice)}):
            original = ranking.fetch_users([alice], PERIOD)
        store.save_snapshot(self.snapshot(list(original.values())))
        with closing(store.connect()) as connection:
            before = connection.execute('SELECT id,total,restricted,fetched_at FROM observations').fetchone()
        with patch.object(store.time, 'time', return_value=closed_at + 60), \
             patch.object(ranking, 'collect_profiles', return_value=([alice, bob], [])), \
             patch.object(ranking, 'fetch_api', return_value={'u0': api_user(bob, 20, 4)}) as fetch, \
             patch('builtins.print'):
            ranking.collect_ranking(PERIOD, expand=True)
        self.assertEqual(fetch.call_count, 1)
        self.assertNotIn('ID-A', fetch.call_args.args[0])
        saved = store.load_snapshots()[0]
        self.assertTrue(saved['countsProvisional'])
        with closing(store.connect()) as connection:
            self.assertEqual(connection.execute('SELECT id,total,restricted,fetched_at FROM observations WHERE user_id=?',
                                                (alice['id'],)).fetchone(), before)
        with patch.dict(os.environ, {}, clear=True), patch('sys.argv', ['ranking', '--project']), \
             patch.object(ranking, 'fetch_api', side_effect=AssertionError('offline')), patch('builtins.print'):
            ranking.main()
        report = ranking.result_directory(PERIOD) / 'README.md'
        self.assertIn('counts_provisional: true', report.read_text())
        with patch.object(store.time, 'time', return_value=closed_at), \
             patch.dict(os.environ, {'REFRESH_CACHE': 'true'}), \
             patch.object(ranking, 'collect_profiles', return_value=([alice, bob], [])), \
             patch.object(ranking, 'fetch_api', return_value={'u0': api_user(alice, 14, 4), 'u1': api_user(bob, 20, 4)}), \
             patch('builtins.print'):
            ranking.collect_ranking(PERIOD)
        self.assertFalse(store.load_snapshots()[0]['countsProvisional'])
        self.assertIn('counts_provisional: false', report.read_text())

    def test_refresh_preserves_ready_snapshot_then_removes_old_observation(self):
        alice = profile()
        with patch.object(ranking, 'fetch_api', return_value={'u0': api_user(alice)}):
            original = ranking.fetch_users([alice], PERIOD)
        store.save_snapshot(self.snapshot(list(original.values())))
        with patch.dict(os.environ, {'REFRESH_CACHE': 'true'}):
            with patch.object(ranking, 'fetch_api', return_value={'u0': api_user(alice, 20, 4)}):
                refreshed = ranking.fetch_users([alice], PERIOD)
        self.assertEqual(store.load_snapshots()[0]['users'][0]['totalContributions'], 12)
        with closing(store.connect()) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM observations').fetchone()[0], 2)
        store.save_snapshot(self.snapshot(list(refreshed.values())))
        self.assertEqual(store.load_snapshots()[0]['users'][0]['totalContributions'], 20)
        with closing(store.connect()) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM observations').fetchone()[0], 1)

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
        metadata = {**PERIOD, 'generatedAt': '2026-09-18T12:00:00Z', 'selectionMode': 'full-location-search',
                    'observerPolicy': 'authenticated-viewer', 'periodSemantics': 'inclusive-UTC-calendar-dates',
                    'countsProvisional': False, 'users': [
            {'login': 'A', 'followers': 1, 'totalContributions': 9, 'restrictedContributionsCount': 0},
            {'login': 'B', 'followers': 0, 'totalContributions': 25, 'restrictedContributionsCount': 20},
            {'login': 'C', 'followers': 2, 'totalContributions': 10, 'restrictedContributionsCount': 1}]}
        path = ranking.render_rankings(metadata)
        self.assertIn('counts_provisional: false', path.read_text())
        metadata['generatedAt'] = '2026-09-17T12:00:00Z'
        metadata['countsProvisional'] = True
        path = ranking.render_rankings(metadata)
        old = path.read_bytes()
        text = old.decode()
        self.assertIn('counts_provisional: true', text)
        visible, total = text.split('## Top 100 — Total')
        self.assertIn('## Top 100 — Visible to acquisition viewer', visible)
        self.assertNotIn('## Public', text)
        self.assertIn('| 1 | [A]', visible)
        self.assertIn('| 1 | [C]', visible)
        self.assertIn('| 3 | [B]', visible)
        self.assertIn('| 1 | [B]', total)
        before = {p.name: p.read_bytes() for p in path.parent.iterdir()}
        metadata['users'][0]['totalContributions'] = 99
        with patch.object(ranking.os, 'fsync', side_effect=[None, None, OSError('disk full')]):
            with self.assertRaises(OSError):
                ranking.render_rankings(metadata)
        self.assertEqual({p.name: p.read_bytes() for p in path.parent.iterdir()}, before)
        metadata['users'][0]['restrictedContributionsCount'] = 999
        with self.assertRaises(ValueError):
            ranking.render_rankings(metadata)
        self.assertEqual(path.read_bytes(), old)

    def test_summary_limits_each_ranking_to_100_and_links_to_complete_tables(self):
        users = [{'login': f'u{i}', 'followers': 0, 'totalContributions': i + 1,
                  'restrictedContributionsCount': 0 if i < 65 else i} for i in range(130)]
        users += [{'login': 'Zero', 'followers': 0, 'totalContributions': 0, 'restrictedContributionsCount': 0},
                  {'login': 'RestrictedOnly', 'followers': 0, 'totalContributions': 50, 'restrictedContributionsCount': 50}]
        metadata = {**self.snapshot([]), 'countsProvisional': False, 'users': users}
        directory = ranking.result_directory(metadata)
        directory.mkdir(parents=True)
        (directory / 'contributions.md').write_text('old combined report')
        path = ranking.render_rankings(metadata)
        self.assertEqual(path.name, 'README.md')
        self.assertEqual({p.name for p in directory.iterdir()}, {'README.md', 'visible-full.md', 'total-full.md'})
        summary = path.read_text()
        sections = summary.split('## Top 100 — ')[1:]
        self.assertEqual(len(sections), 2)
        for section, filename, expected in zip(sections, ('visible-full.md', 'total-full.md'), (130, 131)):
            full = (directory / filename).read_text()
            short_rows = [line for line in section.splitlines() if line.startswith('| ')][2:]
            full_rows = [line for line in full.splitlines() if line.startswith('| ')][2:]
            self.assertEqual(len(full_rows), expected)
            self.assertEqual(short_rows, full_rows[:100])
            self.assertIn(f'[Full ranking]({filename})', section)
            self.assertIn('[Back to top 100](README.md)', full)
            self.assertNotIn('[Zero]', full)
        self.assertNotIn('[RestrictedOnly]', (directory / 'visible-full.md').read_text())
        self.assertIn('[RestrictedOnly]', (directory / 'total-full.md').read_text())

    def test_frontmatter_schema_round_trips_metadata_and_rejects_wrong_types(self):
        metadata = self.snapshot([{**profile(), 'totalContributions': 12, 'restrictedContributionsCount': 3}],
                                 countsProvisional=True, missingUsers=['Bob'])
        path = ranking.render_rankings(metadata)
        headers = []
        for filename in ('README.md', 'visible-full.md', 'total-full.md'):
            prefix, header, body = (path.parent / filename).read_text().split('---\n', 2)
            self.assertEqual(prefix, '')
            values = {key: json.loads(value) for key, value in
                      (line.split(': ', 1) for line in header.splitlines())}
            self.assertEqual(set(values), {field.name for field in fields(ranking.ReportFrontmatter)})
            self.assertEqual(values, {
                'period_start': PERIOD['from'], 'period_end': PERIOD['to'],
                'snapshot_completed': datetime.datetime.fromisoformat(metadata['generatedAt']).isoformat(timespec='seconds'),
                'accounts_fetched': 1, 'counts_provisional': True, 'unavailable_accounts': 1})
            self.assertTrue(body.startswith('\n# Kazakhstan contribution rankings'))
            self.assertIn('Both UTC dates are included. Counts reflect when each account was queried.', body)
            self.assertNotIn('Snapshot completed:', body)
            self.assertNotIn('Selection mode:', body)
            headers.append(header)
        self.assertEqual(len(set(headers)), 1)
        parameters = ranking.ReportFrontmatter(**values)
        special = 'period: "Қазақстан"\nsecond line'
        self.assertEqual(json.loads(replace(parameters, period_start=special).to_lines()[1].split(': ', 1)[1]), special)
        for invalid in ({'accounts_fetched': True}, {'counts_provisional': 'true'}, {'snapshot_completed': None}):
            with self.assertRaises(TypeError):
                replace(parameters, **invalid).to_lines()
        with self.assertRaises(ValueError):
            replace(parameters, unavailable_accounts=-1).to_lines()

    def test_interrupted_publication_keeps_summary_and_reprojection_recovers(self):
        metadata = {**self.snapshot([]), 'countsProvisional': False, 'users': [
            {'login': 'Alice', 'followers': 0, 'totalContributions': 12, 'restrictedContributionsCount': 3}]}
        path = ranking.render_rankings(metadata)
        previous = path.read_bytes()
        legacy = path.parent / 'contributions.md'
        legacy.write_text('old combined report')
        metadata['users'][0]['totalContributions'] = 99
        replace = Path.replace
        def interrupted(source, target):
            if target.name == 'total-full.md':
                raise OSError('interrupted publication')
            return replace(source, target)
        with patch.object(Path, 'replace', new=interrupted), self.assertRaises(OSError):
            ranking.render_rankings(metadata)
        self.assertEqual(path.read_bytes(), previous)
        self.assertTrue(legacy.exists())
        self.assertEqual({p.name for p in path.parent.iterdir()},
                         {'README.md', 'visible-full.md', 'total-full.md', 'contributions.md'})
        ranking.render_rankings(metadata)
        self.assertFalse(legacy.exists())
        self.assertNotEqual(path.read_bytes(), previous)
        for filename in ('README.md', 'visible-full.md', 'total-full.md'):
            self.assertIn('| 99 |', (path.parent / filename).read_text())

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

    def test_period_filter_reads_one_membership_and_project_still_renders_all(self):
        alice = profile()
        other = {**PERIOD, 'from': '2025-09-18T00:00:00Z'}
        for period in (PERIOD, other):
            with patch.object(ranking, 'fetch_api', return_value={'u0': api_user(alice)}):
                users = ranking.fetch_users([alice], period)
            store.save_snapshot(self.snapshot(list(users.values()), period))
        all_snapshots = store.load_snapshots()
        statements = []
        connect = store.connect
        def traced_connection():
            connection = connect()
            connection.set_trace_callback(statements.append)
            return connection
        with patch.object(store, 'connect', side_effect=traced_connection):
            selected = store.load_snapshots(PERIOD)
        self.assertEqual(selected, [all_snapshots[0]])
        self.assertEqual(sum('FROM members m' in sql for sql in statements), 1)
        self.assertEqual(store.load_snapshots({**PERIOD, 'from': '2025-09-19T00:00:00Z'}), [])
        path = ranking.render_rankings(selected[0])
        before = path.read_bytes()
        with patch.dict(os.environ, {}, clear=True), patch('sys.argv', ['ranking', '--project']), \
             patch.object(ranking, 'fetch_api', side_effect=AssertionError('offline')), patch('builtins.print'):
            ranking.main()
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue((ranking.result_directory(other) / 'README.md').is_file())
        with patch.object(store, 'load_snapshots', wraps=store.load_snapshots) as loading, \
             patch.object(ranking, 'collect_profiles', return_value=([alice], [])), patch('builtins.print'):
            ranking.collect_ranking(PERIOD)
        loading.assert_called_once_with(PERIOD)

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

    def test_expand_retains_expired_snapshot_counts_and_fetches_new_identity_only(self):
        alice = profile()
        with patch.object(ranking, 'fetch_api', return_value={'u0': api_user(alice)}):
            original = ranking.fetch_users([alice], PERIOD)
        store.save_snapshot(self.snapshot(list(original.values())))
        with closing(store.connect()) as connection, connection:
            connection.execute('UPDATE observations SET fetched_at=0')
        renamed = profile('Renamed', alice['id'])
        bob = profile('Bob', 'ID-B')
        with patch.object(ranking, 'collect_profiles', return_value=([renamed, bob], [])), \
             patch.object(ranking, 'fetch_api', return_value={'u0': api_user(bob, 20, 4)}) as fetch, \
             patch('builtins.print'):
            ranking.collect_ranking(PERIOD, expand=True)
        self.assertEqual(fetch.call_count, 1)
        self.assertIn('ID-B', fetch.call_args.args[0])
        self.assertNotIn('ID-A', fetch.call_args.args[0])
        saved = {u['login']: u for u in store.load_snapshots()[0]['users']}
        self.assertEqual(saved['Renamed']['totalContributions'], 12)
        self.assertEqual(saved['Bob']['totalContributions'], 20)
        expanded = ranking.parse_period(['--from', PERIOD['from'], '--to', PERIOD['to'], '--expand'])
        self.assertTrue(expanded.pop('_expand'))
        self.assertEqual(expanded, PERIOD)

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
        report = (ranking.result_directory(saved) / 'README.md').read_text()
        self.assertIn('unavailable_accounts: 1', report)


if __name__ == '__main__':
    unittest.main()
