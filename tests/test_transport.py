from __future__ import annotations

import unittest
from collections import deque

import requests

from vghks_sdk import RequestPolicy
from vghks_sdk.core.errors import ConfigurationError, RequestError
from vghks_sdk.core.response_text import private_use_characters
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

    def test_transport_decodes_windows_cp950_user_defined_characters(self) -> None:
        transport = SafeSessionTransport(policy=RequestPolicy())
        self.addCleanup(transport.close)
        raw = b"\x8e\xf5" + "甲乙".encode("cp950")
        for content_type in ("text/html; charset=Big5", "text/html"):
            with self.subTest(content_type=content_type):
                response = FakeResponse(200, raw, content_type)
                response.encoding = requests.utils.get_encoding_from_headers(response.headers)
                self.assertEqual(transport.text(response), "\ue3a4甲乙")
        damaged = FakeResponse(200, raw + b"\xff", "text/html; charset=Big5")
        self.assertEqual(transport.text(damaged), "\ue3a4甲乙\ufffd")

    def test_transport_keeps_valid_utf8_on_a_mislabeled_big5_page(self) -> None:
        transport = SafeSessionTransport(policy=RequestPolicy())
        self.addCleanup(transport.close)
        # Extended CP950 can also read these bytes. The prior UTF-8 choice wins.
        response = FakeResponse(200, "丁A".encode(), "text/html; charset=Big5")
        self.assertEqual(transport.text(response), "丁A")

    def test_private_use_diagnostics_return_only_codes_and_round_trippable_pairs(self) -> None:
        self.assertEqual(private_use_characters("合成\ue3a4\uf70f", "\ue3a4\ufffd\U000F0000"), [
            {"code_point": "U+E3A4", "windows_cp950": "8E F5"},
            {"code_point": "U+F70F", "windows_cp950": None},
            {"code_point": "U+F0000", "windows_cp950": None},
        ])
        self.assertEqual(len(private_use_characters("".join(chr(0xE000 + i) for i in range(20)))), 16)

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
