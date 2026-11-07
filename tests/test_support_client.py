from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse

import pytest

from wq_forum_rag.support_client import (
    SupportAuthError,
    SupportBlockedError,
    SupportClient,
    SupportCredentials,
    SupportCredentialsMissing,
    SupportError,
    SupportHTTPError,
    SupportNetworkError,
)

USERNAME = "quant@example.com"
PASSWORD = "s3cr3t-Pa55word"
BASIC_TOKEN = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
COOKIE_VALUE = "COOKIEVALUE123"
JWT_VALUE = "eyJhbGciOiJIUzI1NiJ9.SECRETJWT"
SECRETS = (PASSWORD, BASIC_TOKEN, COOKIE_VALUE, JWT_VALUE, "jwt=")

API = "https://api.worldquantbrain.com"
ZENDESK = "https://worldquantbrain.zendesk.com"
SUPPORT = "https://support.worldquantbrain.com"
POSTS = f"{SUPPORT}/api/v2/community/posts.json"


def _json(payload: dict, status: int = 200, headers: dict | None = None):
    return status, dict(headers or {}), json.dumps(payload).encode()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeTransport:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.routes: dict[tuple[str, str, str], list] = {}
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.times: list[float] = []

    def add(self, method: str, url: str, *responses) -> None:
        parsed = urllib.parse.urlparse(url)
        key = (method, parsed.hostname or "", parsed.path)
        self.routes.setdefault(key, []).extend(responses)

    def __call__(self, method, url, headers, data, timeout):
        parsed = urllib.parse.urlparse(url)
        key = (method, parsed.hostname or "", parsed.path)
        self.calls.append((method, url, dict(headers)))
        self.times.append(self.clock.now)
        queue = self.routes.get(key)
        if not queue:
            raise AssertionError(f"unexpected request {key}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def paths(self) -> list[str]:
        return [urllib.parse.urlparse(url).path for _, url, _ in self.calls]


def _add_auth_chain(transport: FakeTransport) -> None:
    transport.add(
        "POST",
        f"{API}/authentication",
        (201, {"Set-Cookie": f"t={COOKIE_VALUE}; Path=/"}, b'{"user": {}}'),
    )
    transport.add(
        "GET",
        f"{API}/authentication/support",
        (302, {"Location": f"{ZENDESK}/access/jwt?jwt={JWT_VALUE}"}, b""),
    )
    return_to = urllib.parse.quote(f"{SUPPORT}/hc/en-us/community/topics", safe="")
    transport.add(
        "GET",
        f"{ZENDESK}/access/jwt",
        (302, {"location": f"{SUPPORT}/access/return_to?return_to={return_to}"}, b""),
    )
    transport.add(
        "GET",
        f"{SUPPORT}/access/return_to",
        (
            302,
            {
                "Location": f"{SUPPORT}/hc/en-us/community/topics",
                "Set-Cookie": f"_zendesk_session={COOKIE_VALUE}",
            },
            f"session {COOKIE_VALUE}".encode(),
        ),
    )
    transport.add(
        "GET", f"{SUPPORT}/api/v2/users/me.json", _json({"user": {"role": "end-user"}})
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def transport(clock: FakeClock) -> FakeTransport:
    return FakeTransport(clock)


@pytest.fixture
def client(clock: FakeClock, transport: FakeTransport) -> SupportClient:
    return SupportClient(
        SupportCredentials(USERNAME, PASSWORD),
        transport=transport,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


@pytest.fixture(autouse=True)
def _debug_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="wq_forum_rag.sync")


def _assert_clean(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


def test_auth_chain_follows_redirects_and_stops_before_hc(
    client: SupportClient, transport: FakeTransport, caplog
) -> None:
    _add_auth_chain(transport)

    result = client.login()

    assert result == {"login_status": 201, "hops": 3, "role": "end-user"}
    assert transport.paths() == [
        "/authentication",
        "/authentication/support",
        "/access/jwt",
        "/access/return_to",
        "/api/v2/users/me.json",
    ]
    assert not any(path.startswith("/hc/") for path in transport.paths())
    assert transport.calls[0][2]["Authorization"] == f"Basic {BASIC_TOKEN}"
    assert all("Authorization" not in headers for _, _, headers in transport.calls[1:])
    support_url = transport.calls[1][1]
    assert "return_to=" in support_url
    assert urllib.parse.quote(f"{SUPPORT}/hc/en-us/community/topics", safe="") in (
        support_url
    )
    _assert_clean(caplog.text)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            (401, {"WWW-Authenticate": 'persona realm="BRAIN"'}, PASSWORD.encode()),
            "Persona verification required",
        ),
        ((403, {}, f"bad {PASSWORD}".encode()), "HTTP 403"),
    ],
)
def test_login_rejections_raise_auth_error(
    client: SupportClient, transport: FakeTransport, caplog, response, expected
) -> None:
    transport.add("POST", f"{API}/authentication", response)

    with pytest.raises(SupportAuthError) as excinfo:
        client.login()

    assert expected in str(excinfo.value)
    assert transport.paths() == ["/authentication"]
    _assert_clean(str(excinfo.value))
    _assert_clean(caplog.text)


def test_support_session_failure_message_has_no_secrets(
    client: SupportClient, transport: FakeTransport, caplog
) -> None:
    _add_auth_chain(transport)
    transport.routes[
        ("GET", "support.worldquantbrain.com", "/api/v2/users/me.json")
    ] = [(401, {}, f"{COOKIE_VALUE}".encode())]

    with pytest.raises(SupportAuthError) as excinfo:
        client.login()

    assert "HTTP 401" in str(excinfo.value)
    _assert_clean(str(excinfo.value))
    _assert_clean(caplog.text)


def test_rate_limit_honors_retry_after(
    client: SupportClient, transport: FakeTransport, clock: FakeClock
) -> None:
    transport.add(
        "GET",
        POSTS,
        (429, {"Retry-After": "5"}, b""),
        _json({"posts": [{"id": 1}]}),
    )

    assert client.get_json("/api/v2/community/posts.json") == {"posts": [{"id": 1}]}
    assert 5.0 in clock.sleeps
    assert len(transport.calls) == 2


def test_rate_limit_delay_is_capped(
    client: SupportClient, transport: FakeTransport, clock: FakeClock
) -> None:
    transport.add(
        "GET", POSTS, (429, {"Retry-After": "500"}, b""), _json({"posts": []})
    )

    assert client.get_json(POSTS) == {"posts": []}
    assert 90.0 in clock.sleeps
    assert max(clock.sleeps) == 90.0


def test_rate_limit_gives_up_after_six_retries(
    client: SupportClient, transport: FakeTransport, clock: FakeClock
) -> None:
    transport.add("GET", POSTS, (429, {"Retry-After": "1"}, b""))

    with pytest.raises(SupportHTTPError) as excinfo:
        client.get_json(POSTS)

    assert excinfo.value.status == 429
    assert len(transport.calls) == 7


def test_transient_network_error_is_retried(
    client: SupportClient, transport: FakeTransport, clock: FakeClock
) -> None:
    transport.add(
        "GET",
        POSTS,
        urllib.error.URLError(TimeoutError("_ssl.c:993: The handshake timed out")),
        _json({"posts": []}),
    )

    assert client.get_json(POSTS) == {"posts": []}
    assert 2.0 in clock.sleeps
    assert len(transport.calls) == 2


def test_network_errors_and_5xx_give_up_after_three_retries(
    client: SupportClient, transport: FakeTransport, clock: FakeClock, caplog
) -> None:
    transport.add(
        "GET",
        POSTS,
        ConnectionResetError("reset"),
        (502, {}, b""),
        TimeoutError("slow"),
        (503, {}, b""),
    )

    with pytest.raises(SupportNetworkError) as excinfo:
        client.get_json(POSTS, {"jwt": JWT_VALUE})

    assert [s for s in clock.sleeps if s >= 2] == [2.0, 4.0, 8.0]
    assert len(transport.calls) == 4
    _assert_clean(str(excinfo.value))
    _assert_clean(caplog.text)


def test_unauthorized_logs_in_again_once(
    client: SupportClient, transport: FakeTransport
) -> None:
    _add_auth_chain(transport)
    transport.add("GET", POSTS, (401, {}, b""), _json({"posts": []}))

    assert client.get_json(POSTS) == {"posts": []}
    assert transport.paths().count("/authentication") == 1
    assert transport.paths().count("/api/v2/community/posts.json") == 2


def test_cloudflare_challenge_raises_blocked(
    client: SupportClient, transport: FakeTransport
) -> None:
    transport.add("GET", POSTS, (403, {"cf-mitigated": "challenge"}, b"<html>"))

    with pytest.raises(SupportBlockedError):
        client.get_json(POSTS)
    assert len(transport.calls) == 1


def test_other_http_errors_report_safe_path(
    client: SupportClient, transport: FakeTransport
) -> None:
    transport.add("GET", POSTS, (404, {}, b"missing"))

    with pytest.raises(SupportHTTPError) as excinfo:
        client.get_json(POSTS, {"page[size]": 2, "jwt": JWT_VALUE})

    assert excinfo.value.status == 404
    assert excinfo.value.safe_path == POSTS
    _assert_clean(str(excinfo.value))


def test_iter_pages_follows_links_next_until_has_more_false(
    client: SupportClient, transport: FakeTransport
) -> None:
    next_url = f"{POSTS}?page%5Bafter%5D=abc&page%5Bsize%5D=2"
    transport.add(
        "GET",
        POSTS,
        _json(
            {"posts": [1, 2], "meta": {"has_more": True}, "links": {"next": next_url}}
        ),
        _json(
            {
                "posts": [3],
                "meta": {"has_more": False},
                "links": {"next": f"{POSTS}?page%5Bafter%5D=def"},
            }
        ),
    )

    pages = list(client.iter_pages("/api/v2/community/posts.json", {"page[size]": 2}))

    assert [page["posts"] for page in pages] == [[1, 2], [3]]
    assert transport.calls[0][1] == f"{POSTS}?page%5Bsize%5D=2"
    assert transport.calls[1][1] == next_url
    assert client.last_iteration_truncated is False


def test_iter_pages_follows_next_page_without_links(
    client: SupportClient, transport: FakeTransport
) -> None:
    transport.add(
        "GET",
        POSTS,
        _json({"posts": [1], "next_page": f"{POSTS}?page=2"}),
        _json({"posts": [2], "next_page": None}),
    )

    pages = list(client.iter_pages(POSTS))

    assert len(pages) == 2
    assert client.last_iteration_truncated is False


def test_iter_pages_max_pages_marks_truncation(
    client: SupportClient, transport: FakeTransport
) -> None:
    transport.add(
        "GET",
        POSTS,
        _json({"posts": [1], "meta": {"has_more": True}, "links": {"next": POSTS}}),
    )

    pages = list(client.iter_pages(POSTS, max_pages=1))

    assert len(pages) == 1
    assert len(transport.calls) == 1
    assert client.last_iteration_truncated is True


def test_iter_pages_rejects_foreign_next_link(
    client: SupportClient, transport: FakeTransport
) -> None:
    transport.add(
        "GET",
        POSTS,
        _json(
            {
                "posts": [1],
                "meta": {"has_more": True},
                "links": {"next": f"https://evil.example/steal?jwt={JWT_VALUE}"},
            }
        ),
    )

    with pytest.raises(SupportError) as excinfo:
        list(client.iter_pages(POSTS))

    assert "evil.example" in str(excinfo.value)
    assert len(transport.calls) == 1
    _assert_clean(str(excinfo.value))


def test_iter_pages_rejects_plain_http_next_link(
    client: SupportClient, transport: FakeTransport
) -> None:
    transport.add(
        "GET",
        POSTS,
        _json({"posts": [1], "links": {"next": POSTS.replace("https", "http")}}),
    )

    with pytest.raises(SupportError):
        list(client.iter_pages(POSTS))


def test_throttle_spaces_requests(
    client: SupportClient, transport: FakeTransport, clock: FakeClock
) -> None:
    transport.add("GET", POSTS, _json({"posts": []}))

    for _ in range(4):
        client.get_json(POSTS)

    gaps = [b - a for a, b in zip(transport.times, transport.times[1:], strict=False)]
    assert len(gaps) == 3
    assert all(gap >= 0.3 - 1e-9 for gap in gaps)


def test_map_concurrent_returns_results_and_errors_in_order(
    client: SupportClient,
) -> None:
    def work(value: int) -> int:
        if value == 2:
            raise ValueError("boom")
        return value * 10

    results = client.map_concurrent(work, [1, 2, 3])

    assert [item for item, _ in results] == [1, 2, 3]
    assert results[0][1] == 10
    assert isinstance(results[1][1], ValueError)
    assert results[2][1] == 30


def test_credentials_from_env(monkeypatch) -> None:
    monkeypatch.setenv("WQ_BRAIN_USERNAME", USERNAME)
    monkeypatch.setenv("WQ_BRAIN_PASSWORD", PASSWORD)

    credentials = SupportCredentials.from_env()

    assert credentials.username == USERNAME
    assert credentials.password == PASSWORD


@pytest.mark.parametrize("present", [None, "WQ_BRAIN_USERNAME", "WQ_BRAIN_PASSWORD"])
def test_missing_credentials_raise(monkeypatch, present) -> None:
    if present:
        monkeypatch.setenv(present, PASSWORD)

    with pytest.raises(SupportCredentialsMissing) as excinfo:
        SupportCredentials.from_env()

    message = str(excinfo.value)
    assert "WQ_BRAIN_USERNAME" in message
    assert "WQ_BRAIN_PASSWORD" in message
    assert PASSWORD not in message


def test_credentials_repr_masks_secrets() -> None:
    credentials = SupportCredentials(USERNAME, PASSWORD)

    assert PASSWORD not in repr(credentials)
    assert PASSWORD not in str(credentials)
    assert USERNAME not in repr(credentials)


def test_default_transport_is_disabled_in_tests() -> None:
    with pytest.raises(RuntimeError, match="network disabled in tests"):
        SupportClient(SupportCredentials(USERNAME, PASSWORD))
