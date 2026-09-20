from __future__ import annotations

import unittest
from collections import deque

import requests

from vghks_sdk import RequestPolicy
from vghks_sdk.core.errors import ConfigurationError, RequestError
from vghks_sdk.core.transport import SafeSessionTransport


class FakeCookies:
    def clear(self) -> None:
        pass


class FakeResponse:
    def __init__(self, status: int, content: bytes, content_type: str = "text/plain") -> None:
        self.status_code = status
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.encoding = None
        self.url = "https://example.test/query.do"
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = deque(outcomes)
        self.headers: dict[str, str] = {}
        self.cookies = FakeCookies()
        self.calls = 0

    def request(self, *args: object, **kwargs: object) -> FakeResponse:
        self.calls += 1
        outcome = self.outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]

    def close(self) -> None:
        pass


class FixedRng:
    def uniform(self, lower: float, upper: float) -> float:
        self.bounds = (lower, upper)
        return (lower + upper) / 2


class TransportTests(unittest.TestCase):
    def test_request_policy_caps_attempts_at_three(self) -> None:
        with self.assertRaises(ConfigurationError):
            RequestPolicy(max_attempts=4).validate()

    def test_every_request_is_throttled_within_configured_jitter_range(self) -> None:
        session = FakeSession([FakeResponse(200, b"ok")])
        sleeps: list[float] = []
        rng = FixedRng()
        transport = SafeSessionTransport(
            policy=RequestPolicy(min_delay_seconds=0.45, max_delay_seconds=1.10),
            session=session,  # type: ignore[arg-type]
            sleeper=sleeps.append,
            rng=rng,  # type: ignore[arg-type]
        )
        transport.request("GET", "https://example.test/query.do")
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 0.45)
        self.assertLessEqual(sleeps[0], 1.10)
        self.assertEqual(rng.bounds, (0.45, 1.10))

    def test_transport_retries_retryable_status_and_decodes_big5(self) -> None:
        session = FakeSession(
            [
                FakeResponse(429, b"busy"),
                FakeResponse(200, "眼科".encode("cp950"), "text/html; charset=Big5"),
            ]
        )
        sleeps: list[float] = []
        transport = SafeSessionTransport(
            policy=RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, max_attempts=3),
            session=session,  # type: ignore[arg-type]
            sleeper=sleeps.append,
        )
        response = transport.request("GET", "https://example.test/query.do")
        self.assertEqual(transport.text(response), "眼科")
        self.assertEqual(session.calls, 2)
        self.assertTrue(sleeps)

    def test_unsafe_post_is_not_retried_after_network_failure(self) -> None:
        session = FakeSession([requests.ConnectionError("offline")])
        transport = SafeSessionTransport(
            policy=RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, max_attempts=3),
            session=session,  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        with self.assertRaises(RequestError):
            transport.request("POST", "https://example.test/login.do")
        self.assertEqual(session.calls, 1)


if __name__ == "__main__":
    unittest.main()
