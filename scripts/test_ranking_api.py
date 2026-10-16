import io
import http.client
import json
import os
import unittest
from unittest.mock import patch
import urllib.error

import kazakhstan_ranking as ranking


def response(payload=None, headers=None):
    result = io.BytesIO(json.dumps(payload if payload is not None else {'data': {'ok': True}}).encode())
    result.headers = headers or {}
    return result


def http_error(status, headers=None, message='rate limit'):
    return urllib.error.HTTPError('https://api.github.com/graphql', status, 'error',
                                  headers or {}, io.BytesIO(message.encode()))


class ApiTest(unittest.TestCase):
    def setUp(self):
        ranking.NEXT_REQUEST_AT = 0
        self.environment = patch.dict(os.environ, {'GH_TOKEN': 'test-only'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(setattr, ranking, 'NEXT_REQUEST_AT', 0)

    def run_requests(self, replies, calls=1):
        with patch.object(ranking.urllib.request, 'urlopen', side_effect=replies) as fetch:
            with patch.object(ranking.time, 'time', return_value=1000):
                with patch.object(ranking.time, 'sleep') as sleep:
                    for _ in range(calls):
                        self.assertEqual(ranking.fetch_api('query', {}), {'ok': True})
                    return fetch.call_count, [call.args[0] for call in sleep.call_args_list]

    def test_successful_requests_have_no_fixed_pause(self):
        self.assertEqual(self.run_requests([response(), response()], calls=2), (2, []))

    def test_http_retry_after_is_respected(self):
        self.assertEqual(self.run_requests([http_error(429, {'Retry-After': '7'}), response()]), (2, [7]))
        self.assertEqual(self.run_requests([http_error(403, {'Retry-After': '9'}), response()]), (2, [9]))

    def test_primary_exhaustion_uses_reset_header(self):
        headers = {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '1010'}
        self.assertEqual(self.run_requests([http_error(403, headers), response()]), (2, [11]))
        self.assertEqual(self.run_requests([response(headers=headers), response()], calls=2), (2, [11]))

    def test_graphql_200_limits_are_retried(self):
        limited = {'errors': [{'type': 'RATE_LIMITED', 'message': 'API rate limit exceeded'}]}
        self.assertEqual(self.run_requests([response(limited, {'Retry-After': '4'}), response()]), (2, [4]))
        headers = {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '1020'}
        self.assertEqual(self.run_requests([response(limited, headers), response()]), (2, [21]))

    def test_secondary_limit_without_headers_backs_off(self):
        self.assertEqual(self.run_requests([http_error(429), http_error(429), response()]), (3, [60, 120]))

    def test_retry_after_http_date_and_zero(self):
        headers = {'Retry-After': 'Thu, 01 Jan 1970 00:17:00 GMT'}
        self.assertEqual(self.run_requests([http_error(429, headers), response()]), (2, [20]))
        self.assertEqual(self.run_requests([http_error(429, {'Retry-After': '0'}), response()]), (2, []))

    def test_transient_failures_use_bounded_backoff(self):
        self.assertEqual(self.run_requests([http_error(503, {'Retry-After': '8'}), response()]), (2, [8]))
        self.assertEqual(self.run_requests([urllib.error.URLError('disconnected'), response()]), (2, [2]))
        with patch.object(ranking.urllib.request, 'urlopen', side_effect=[http_error(429) for _ in range(5)]) as fetch:
            with patch.object(ranking.time, 'time', return_value=1000), patch.object(ranking.time, 'sleep') as sleep:
                with self.assertRaises(RuntimeError):
                    ranking.fetch_api('query', {})
                self.assertEqual(fetch.call_count, 5)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [60, 120, 240, 480])

    def test_interrupted_response_body_is_retried(self):
        for error in (http.client.IncompleteRead(b'partial', 20), ConnectionResetError('reset')):
            broken = response()
            with patch.object(broken, 'read', side_effect=error):
                self.assertEqual(self.run_requests([broken, response()]), (2, [2]))
        with patch.object(ranking.urllib.request, 'urlopen', side_effect=ConnectionResetError('last reset')):
            with patch.object(ranking.time, 'sleep'), self.assertRaisesRegex(RuntimeError, 'last reset'):
                ranking.fetch_api('query', {})

    def test_not_found_preserves_valid_partial_data_only_at_null_alias(self):
        payload = {'data': {'u0': {'id': 'A'}, 'u1': None},
                   'errors': [{'type': 'NOT_FOUND', 'path': ['u1'], 'message': 'deleted'}]}
        with patch.object(ranking.urllib.request, 'urlopen', return_value=response(payload)):
            self.assertEqual(ranking.fetch_api('query', {}), payload['data'])
        for errors in ([{'type': 'NOT_FOUND', 'path': ['u0']}],
                       [{'type': 'NOT_FOUND', 'path': ['u1', 'field']}],
                       [{'type': 'FORBIDDEN', 'path': ['u1']}]):
            payload['errors'] = errors
            with patch.object(ranking.urllib.request, 'urlopen', return_value=response(payload)) as fetch:
                with self.assertRaises(RuntimeError):
                    ranking.fetch_api('query', {})
                self.assertEqual(fetch.call_count, 1)

    def test_truncated_json_and_utf8_responses_are_retried(self):
        for body in (b'{"data":', b'{"data":{"text":"\xd0'):
            broken = io.BytesIO(body)
            broken.headers = {}
            self.assertEqual(self.run_requests([broken, response()]), (2, [2]))

    def test_malformed_json_stops_after_five_attempts(self):
        replies = []
        for _ in range(5):
            broken = io.BytesIO(b'{"data":')
            broken.headers = {}
            replies.append(broken)
        with patch.object(ranking.urllib.request, 'urlopen', side_effect=replies) as fetch, \
             patch.object(ranking.time, 'time', return_value=1000), patch.object(ranking.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'after five attempts'):
                ranking.fetch_api('query', {})
        self.assertEqual(fetch.call_count, 5)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4, 8, 16])

    def test_permission_and_validation_errors_do_not_retry(self):
        for error in [http_error(401), http_error(403, message='Resource not accessible')]:
            with patch.object(ranking.urllib.request, 'urlopen', side_effect=error) as fetch:
                with self.assertRaises((RuntimeError, urllib.error.HTTPError)):
                    ranking.fetch_api('query', {})
                self.assertEqual(fetch.call_count, 1)
        invalid = {'errors': [{'type': 'GRAPHQL_VALIDATION_FAILED', 'message': 'Unknown field'}]}
        with patch.object(ranking.urllib.request, 'urlopen', return_value=response(invalid)) as fetch:
            with self.assertRaises(RuntimeError):
                ranking.fetch_api('query', {})
            self.assertEqual(fetch.call_count, 1)


if __name__ == '__main__':
    unittest.main()
