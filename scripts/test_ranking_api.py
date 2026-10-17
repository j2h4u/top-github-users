import http.client
import io
import json
import os
import unittest
import urllib.error
from collections.abc import Mapping, Sequence
from email.message import Message
from unittest.mock import Mock, patch
from urllib.response import addinfourl

import kazakhstan_ranking as ranking


def response(payload: Mapping[str, object] | None = None, headers: dict[str, str] | None = None) -> addinfourl:
    body = json.dumps(payload if payload is not None else {"data": {"ok": True}}).encode()
    return addinfourl(io.BytesIO(body), response_headers(headers), "https://api.github.com/graphql")


def response_headers(headers: dict[str, str] | None) -> Message:
    result = Message()
    for name, value in (headers or {}).items():
        result.add_header(name, value)
    return result


def http_error(
    status: int, headers: dict[str, str] | None = None, message: str = "rate limit"
) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.github.com/graphql", status, "error", response_headers(headers), io.BytesIO(message.encode())
    )


def response_body(body: bytes) -> addinfourl:
    return addinfourl(io.BytesIO(body), response_headers(None), "https://api.github.com/graphql")


class ApiTest(unittest.TestCase):
    def setUp(self):
        ranking.next_request_at = 0
        self.enterContext(patch.dict(os.environ, {"GH_TOKEN": "test-only"}))
        self.addCleanup(setattr, ranking, "next_request_at", 0)

    def run_requests(self, replies: Sequence[object], calls: int = 1) -> tuple[int, list[object]]:
        try:
            with (
                patch.object(ranking.urllib.request, "urlopen", side_effect=replies) as fetch,
                patch.object(ranking.time, "time", return_value=1000),
                patch.object(ranking.time, "sleep") as sleep,
            ):
                for _ in range(calls):
                    self.assertEqual(ranking.fetch_api("query", {}), {"ok": True})
                return fetch.call_count, [call.args[0] for call in sleep.call_args_list]
        finally:
            for reply in replies:
                if isinstance(reply, urllib.error.HTTPError) and reply.fp is not None and not reply.fp.closed:
                    reply.close()

    def assert_retry_exhausted(self, replies: Sequence[object], message: str, delays: list[int]) -> None:
        try:
            with (
                patch.object(ranking.urllib.request, "urlopen", side_effect=replies) as fetch,
                patch.object(ranking.time, "time", return_value=1000),
                patch.object(ranking.time, "sleep") as sleep,
                self.assertRaisesRegex(RuntimeError, message),
            ):
                ranking.fetch_api("query", {})
            self.assertEqual(fetch.call_count, 5)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], delays)
        finally:
            for reply in replies:
                if isinstance(reply, urllib.error.HTTPError) and reply.fp is not None and not reply.fp.closed:
                    reply.close()

    def test_successful_requests_have_no_fixed_pause(self):
        self.assertEqual(self.run_requests([response(), response()], calls=2), (2, []))

    def test_http_retry_after_is_respected(self):
        self.assertEqual(self.run_requests([http_error(429, {"Retry-After": "7"}), response()]), (2, [7]))
        self.assertEqual(self.run_requests([http_error(403, {"Retry-After": "9"}), response()]), (2, [9]))

    def test_primary_exhaustion_uses_reset_header(self):
        headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1010"}
        self.assertEqual(self.run_requests([http_error(403, headers), response()]), (2, [11]))
        self.assertEqual(self.run_requests([response(headers=headers), response()], calls=2), (2, [11]))

    def test_graphql_200_limits_are_retried(self):
        limited = {"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}
        self.assertEqual(self.run_requests([response(limited, {"Retry-After": "4"}), response()]), (2, [4]))
        headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1020"}
        self.assertEqual(self.run_requests([response(limited, headers), response()]), (2, [21]))

    def test_secondary_limit_without_headers_backs_off(self):
        self.assertEqual(self.run_requests([http_error(429), http_error(429), response()]), (3, [60, 120]))

    def test_http_errors_are_closed_after_retry_and_fatal_handling(self):
        cases = [
            (http_error(429, {"Retry-After": "1"}), True),
            (http_error(403, {"Retry-After": "1"}), True),
            (http_error(503, {"Retry-After": "1"}), True),
            (http_error(401), False),
            (http_error(403, message="Resource not accessible"), False),
        ]
        for error, retry in cases:
            self.addCleanup(error.close)
            close = Mock(wraps=error.close)
            with self.subTest(status=error.code, retry=retry), patch.object(error, "close", close):
                replies = [error, response()] if retry else [error]
                with (
                    patch.object(ranking.urllib.request, "urlopen", side_effect=replies) as fetch,
                    patch.object(ranking.time, "time", return_value=1000),
                    patch.object(ranking.time, "sleep"),
                ):
                    if retry:
                        self.assertEqual(ranking.fetch_api("query", {}), {"ok": True})
                        self.assertEqual(fetch.call_count, 2)
                    else:
                        with self.assertRaises((RuntimeError, urllib.error.HTTPError)):
                            ranking.fetch_api("query", {})
                        self.assertEqual(fetch.call_count, 1)
                close.assert_called_once()

        errors = [http_error(429) for _ in range(5)]
        for error in errors:
            self.addCleanup(error.close)
        close_mocks = [Mock(wraps=error.close) for error in errors]
        closures = [
            patch.object(error, "close", close_mock) for error, close_mock in zip(errors, close_mocks, strict=True)
        ]
        for closure in closures:
            closure.start()
        try:
            self.assert_retry_exhausted(errors, "after five attempts", [60, 120, 240, 480])
        finally:
            for closure in closures:
                closure.stop()
        for close_mock in close_mocks:
            close_mock.assert_called_once()

    def test_retry_after_http_date_and_zero(self):
        headers = {"Retry-After": "Thu, 01 Jan 1970 00:17:00 GMT"}
        self.assertEqual(self.run_requests([http_error(429, headers), response()]), (2, [20]))
        self.assertEqual(self.run_requests([http_error(429, {"Retry-After": "0"}), response()]), (2, []))

    def test_transient_failures_use_bounded_backoff(self):
        self.assertEqual(self.run_requests([http_error(503, {"Retry-After": "8"}), response()]), (2, [8]))
        self.assertEqual(self.run_requests([urllib.error.URLError("disconnected"), response()]), (2, [2]))
        self.assert_retry_exhausted([http_error(429) for _ in range(5)], "after five attempts", [60, 120, 240, 480])

    def test_interrupted_response_body_is_retried(self):
        for error in (http.client.IncompleteRead(b"partial", 20), ConnectionResetError("reset")):
            broken = response()
            with patch.object(broken, "read", side_effect=error):
                self.assertEqual(self.run_requests([broken, response()]), (2, [2]))
        with (
            patch.object(ranking.urllib.request, "urlopen", side_effect=ConnectionResetError("last reset")),
            patch.object(ranking.time, "sleep"),
            self.assertRaisesRegex(RuntimeError, "last reset"),
        ):
            ranking.fetch_api("query", {})

    def test_not_found_preserves_valid_partial_data_only_at_null_alias(self):
        payload = {
            "data": {"u0": {"id": "A"}, "u1": None},
            "errors": [{"type": "NOT_FOUND", "path": ["u1"], "message": "deleted"}],
        }
        with patch.object(ranking.urllib.request, "urlopen", return_value=response(payload)):
            self.assertEqual(ranking.fetch_api("query", {}), payload["data"])
        for errors in (
            [{"type": "NOT_FOUND", "path": ["u0"]}],
            [{"type": "NOT_FOUND", "path": ["u1", "field"]}],
            [{"type": "FORBIDDEN", "path": ["u1"]}],
        ):
            payload["errors"] = errors
            with patch.object(ranking.urllib.request, "urlopen", return_value=response(payload)) as fetch:
                with self.assertRaises(RuntimeError):
                    ranking.fetch_api("query", {})
                self.assertEqual(fetch.call_count, 1)
        payload["data"] = {"u0": None, "u1": None}
        payload["errors"] = [{"type": "NOT_FOUND", "path": ["u0", "contributionsCollection"]}]
        with patch.object(ranking.urllib.request, "urlopen", return_value=response(payload)) as fetch:
            with self.assertRaises(RuntimeError):
                ranking.fetch_api("query", {})
            self.assertEqual(fetch.call_count, 1)

    def test_truncated_json_and_utf8_responses_are_retried(self):
        for body in (b'{"data":', b'{"data":{"text":"\xd0'):
            broken = response_body(body)
            self.assertEqual(self.run_requests([broken, response()]), (2, [2]))

    def test_malformed_json_stops_after_five_attempts(self):
        replies = []
        for _ in range(5):
            broken = response_body(b'{"data":')
            replies.append(broken)
        self.assert_retry_exhausted(replies, "after five attempts", [2, 4, 8, 16])

    def test_permission_and_validation_errors_do_not_retry(self):
        for error in [http_error(401), http_error(403, message="Resource not accessible")]:
            self.addCleanup(error.close)
            with patch.object(ranking.urllib.request, "urlopen", side_effect=error) as fetch:
                with self.assertRaises((RuntimeError, urllib.error.HTTPError)):
                    ranking.fetch_api("query", {})
                self.assertEqual(fetch.call_count, 1)
        invalid = {"errors": [{"type": "GRAPHQL_VALIDATION_FAILED", "message": "Unknown field"}]}
        with patch.object(ranking.urllib.request, "urlopen", return_value=response(invalid)) as fetch:
            with self.assertRaises(RuntimeError):
                ranking.fetch_api("query", {})
            self.assertEqual(fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
