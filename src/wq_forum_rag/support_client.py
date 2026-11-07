"""Read-only client for the WorldQuant Support (Zendesk) JSON API.

The BRAIN login is exchanged for a Support session by following the
``/authentication/support`` redirects by hand and stopping before any
Cloudflare-protected ``/hc/`` HTML page. Only the standard library is used.

Secrets stay out of logs and exception messages: URLs are reduced to
scheme://host/path, and cookie values, the Authorization header and the
bodies of auth hops are never included.
"""

from __future__ import annotations

import base64
import http.client
import http.cookiejar
import json
import logging
import os
import random
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

API_BASE = "https://api.worldquantbrain.com"
SUPPORT_BASE = "https://support.worldquantbrain.com"
SUPPORT_HOST = urllib.parse.urlparse(SUPPORT_BASE).hostname or ""
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
RETURN_TO = f"{SUPPORT_BASE}/hc/en-us/community/topics"
USERNAME_ENV = "WQ_BRAIN_USERNAME"
PASSWORD_ENV = "WQ_BRAIN_PASSWORD"

MAX_REDIRECTS = 10
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_RATE_LIMIT_RETRIES = 6
MAX_RATE_LIMIT_DELAY = 90.0
MAX_TRANSIENT_RETRIES = 3
TRANSIENT_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    ConnectionError,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
    ssl.SSLError,
)

logger = logging.getLogger("wq_forum_rag.sync")

Headers = dict[str, str]
Response = tuple[int, Headers, bytes]
Transport = Callable[[str, str, Headers, bytes | None, float], Response]


class SupportError(RuntimeError):
    """Base class for Support API failures; messages never carry secrets."""


class SupportCredentialsMissing(SupportError):
    pass


class SupportAuthError(SupportError):
    pass


class SupportBlockedError(SupportError):
    pass


class SupportNetworkError(SupportError):
    pass


class SupportHTTPError(SupportError):
    def __init__(self, status: int, safe_path: str) -> None:
        self.status = status
        self.safe_path = safe_path
        super().__init__(f"Support API returned HTTP {status} for {safe_path}")


@dataclass(frozen=True, repr=False)
class SupportCredentials:
    username: str
    password: str

    @classmethod
    def from_env(cls) -> SupportCredentials:
        username = os.environ.get(USERNAME_ENV, "").strip()
        password = os.environ.get(PASSWORD_ENV, "")
        if not username or not password:
            raise SupportCredentialsMissing(
                f"Set {USERNAME_ENV} and {PASSWORD_ENV} to sync the forum"
            )
        return cls(username=username, password=password)

    def __repr__(self) -> str:
        return "SupportCredentials(username='***', password='***')"

    __str__ = __repr__


def _safe_url(url: str) -> str:
    """Return scheme://host/path only; query strings may carry JWTs."""
    parsed = urllib.parse.urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _header(headers: Headers, name: str) -> str:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return str(value)
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _default_transport_factory(jar: http.cookiejar.CookieJar) -> Transport:
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar), _NoRedirect()
    )

    def transport(
        method: str, url: str, headers: Headers, data: bytes | None, timeout: float
    ) -> Response:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as err:
            try:
                return err.code, dict(err.headers or {}), err.read()
            finally:
                err.close()

    return transport


class SupportClient:
    def __init__(
        self,
        credentials: SupportCredentials,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        min_interval: float = 0.3,
        timeout: float = 30.0,
        max_workers: int = 3,
    ) -> None:
        self.credentials = credentials
        self.jar = http.cookiejar.CookieJar()
        self._transport = transport or _default_transport_factory(self.jar)
        self._sleep = sleep
        self._monotonic = monotonic
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_workers = max_workers
        self._throttle_lock = threading.Lock()
        self._login_lock = threading.Lock()
        self._next_allowed: float | None = None
        self.last_iteration_truncated = False

    # -- auth ---------------------------------------------------------------

    def login(self) -> dict[str, Any]:
        with self._login_lock:
            return self._login()

    def _login(self) -> dict[str, Any]:
        token = base64.b64encode(
            f"{self.credentials.username}:{self.credentials.password}".encode()
        ).decode()
        status, headers, _ = self._send(
            "POST",
            f"{API_BASE}/authentication",
            {"Authorization": f"Basic {token}", "Accept": "application/json"},
            b"",
        )
        if status == 401 and "persona" in _header(headers, "WWW-Authenticate").lower():
            raise SupportAuthError("Persona verification required")
        if status not in (200, 201):
            raise SupportAuthError(f"BRAIN login failed: HTTP {status}")
        login_status = status

        return_to = urllib.parse.quote(RETURN_TO, safe="")
        url = f"{API_BASE}/authentication/support?return_to={return_to}"
        hops = 0
        for _ in range(MAX_REDIRECTS + 1):
            parsed = urllib.parse.urlparse(url)
            if parsed.hostname == SUPPORT_HOST and parsed.path.startswith("/hc/"):
                logger.debug("auth: stop before %s", _safe_url(url))
                break
            if hops >= MAX_REDIRECTS:
                raise SupportAuthError("Support session exchange: too many redirects")
            status, headers, _ = self._send(
                "GET",
                url,
                {"Accept": "text/html,application/json;q=0.9,*/*;q=0.8"},
            )
            hops += 1
            logger.debug("auth hop %d: %s -> HTTP %d", hops, _safe_url(url), status)
            if _header(headers, "cf-mitigated"):
                raise SupportBlockedError(f"Cloudflare challenge at {_safe_url(url)}")
            location = _header(headers, "Location")
            if status in REDIRECT_STATUSES and location:
                url = urllib.parse.urljoin(url, location)
                continue
            if status >= 400:
                raise SupportAuthError(
                    f"Support session exchange failed: HTTP {status} at {_safe_url(url)}"
                )
            break

        status, _, body = self._send(
            "GET",
            f"{SUPPORT_BASE}/api/v2/users/me.json",
            {"Accept": "application/json"},
        )
        if status != 200:
            raise SupportAuthError(f"Support session not established: HTTP {status}")
        try:
            role = (json.loads(body).get("user") or {}).get("role")
        except (ValueError, AttributeError) as exc:
            raise SupportAuthError(
                "Support session check returned invalid JSON"
            ) from exc
        logger.info("Support login ok (%d hops, role=%s)", hops, role)
        return {"login_status": login_status, "hops": hops, "role": role}

    # -- requests -----------------------------------------------------------

    def get_json(
        self, path_or_url: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = path_or_url
        if not url.startswith(("https://", "http://")):
            url = f"{SUPPORT_BASE}{url}"
        if params:
            separator = "&" if urllib.parse.urlparse(url).query else "?"
            url = f"{url}{separator}{urllib.parse.urlencode(params)}"
        relogged = False
        while True:
            status, headers, body = self._send(
                "GET", url, {"Accept": "application/json"}
            )
            if 200 <= status < 300:
                try:
                    payload = json.loads(body)
                except ValueError as exc:
                    raise SupportError(f"Invalid JSON from {_safe_url(url)}") from exc
                if not isinstance(payload, dict):
                    raise SupportError(f"Unexpected JSON from {_safe_url(url)}")
                return payload
            if status == 401:
                if relogged:
                    raise SupportAuthError(
                        f"Support API rejected the session for {_safe_url(url)}"
                    )
                relogged = True
                logger.info("Support session expired; logging in again")
                self.login()
                continue
            if status == 403 and _header(headers, "cf-mitigated"):
                raise SupportBlockedError(f"Cloudflare challenge at {_safe_url(url)}")
            raise SupportHTTPError(status, _safe_url(url))

    def iter_pages(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_pages: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        self.last_iteration_truncated = False
        url = path
        current_params = params
        pages = 0
        while True:
            payload = self.get_json(url, current_params)
            pages += 1
            yield payload
            next_url = (payload.get("links") or {}).get("next") or payload.get(
                "next_page"
            )
            if (payload.get("meta") or {}).get("has_more") is False or not next_url:
                return
            if max_pages is not None and pages >= max_pages:
                self.last_iteration_truncated = True
                return
            parsed = urllib.parse.urlparse(str(next_url))
            if parsed.scheme != "https" or parsed.hostname != SUPPORT_HOST:
                raise SupportError(
                    f"Refusing pagination link to {_safe_url(str(next_url))}"
                )
            url = str(next_url)
            current_params = None

    def map_concurrent(
        self, fn: Callable[[Any], Any], items: Iterable[Any]
    ) -> list[tuple[Any, Any]]:
        items = list(items)
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = [pool.submit(fn, item) for item in items]
            results: list[tuple[Any, Any]] = []
            for item, future in zip(items, futures, strict=True):
                error = future.exception()
                results.append((item, error if error is not None else future.result()))
        return results

    # -- transport helpers --------------------------------------------------

    def _throttle(self) -> None:
        with self._throttle_lock:
            now = self._monotonic()
            if self._next_allowed is not None and self._next_allowed > now:
                self._sleep(self._next_allowed - now)
                now = max(self._monotonic(), self._next_allowed)
            self._next_allowed = now + self.min_interval

    def _send(
        self,
        method: str,
        url: str,
        headers: Headers | None = None,
        data: bytes | None = None,
    ) -> Response:
        request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        safe = _safe_url(url)
        rate_retries = 0
        transient_retries = 0
        while True:
            self._throttle()
            try:
                status, response_headers, body = self._transport(
                    method, url, request_headers, data, self.timeout
                )
            except TRANSIENT_ERRORS as exc:
                if transient_retries >= MAX_TRANSIENT_RETRIES:
                    raise SupportNetworkError(
                        f"Network error for {safe}: {type(exc).__name__}"
                    ) from None
                delay = 2.0 * 2**transient_retries
                transient_retries += 1
                logger.warning(
                    "%s %s failed (%s); retry in %.0f s",
                    method,
                    safe,
                    type(exc).__name__,
                    delay,
                )
                self._sleep(delay)
                continue
            logger.debug("%s %s -> HTTP %d", method, safe, status)
            if status == 429:
                if rate_retries >= MAX_RATE_LIMIT_RETRIES:
                    raise SupportHTTPError(status, safe)
                delay = _retry_after_delay(response_headers, rate_retries)
                rate_retries += 1
                logger.warning("%s rate limited; retry in %.1f s", safe, delay)
                self._sleep(delay)
                continue
            if 500 <= status < 600:
                if transient_retries >= MAX_TRANSIENT_RETRIES:
                    raise SupportNetworkError(f"HTTP {status} from {safe}")
                delay = 2.0 * 2**transient_retries
                transient_retries += 1
                logger.warning("%s HTTP %d; retry in %.0f s", safe, status, delay)
                self._sleep(delay)
                continue
            return status, response_headers, body


def _retry_after_delay(headers: Headers, attempt: int) -> float:
    value = _header(headers, "Retry-After").strip()
    delay: float | None = None
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                when = None
            if when is not None:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                delay = (when - datetime.now(UTC)).total_seconds()
    if delay is None:
        delay = 2.0 * 2**attempt + random.uniform(0.0, 1.0)
    return min(max(delay, 0.0), MAX_RATE_LIMIT_DELAY)
