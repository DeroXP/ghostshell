"""HTTP hardening, as raw ASGI middleware.

Why raw ASGI and not Starlette's `BaseHTTPMiddleware`: the latter buffers the
request through an extra task per request, breaks streaming, and is the usual
suspect when a FastAPI service is slower than it should be.  Each class below
is a plain callable that touches only the ASGI messages it needs to.

Stack (outermost first, see main.py):
  RequestIDMiddleware        every response gets X-Request-ID; logs get the id
  SecurityHeadersMiddleware  HSTS, nosniff, frame denial, CSP, no-store …
  UnhandledErrorMiddleware   anything that escapes becomes an opaque JSON 500
  RateLimitMiddleware        per-client token bucket, 429 + Retry-After
  BodyLimitMiddleware        413 for bodies over the configured size
  GZipMiddleware (Starlette) compress responses over 1 KiB

Client IP resolution (`client_ip`) is deliberately NOT uvicorn's
`--proxy-headers` logic.  With `--forwarded-allow-ips='*'` uvicorn takes the
*leftmost* X-Forwarded-For entry, which the client itself can set.  Behind a
known number of trusted proxies the trustworthy entry is the one those proxies
appended — counted from the *right* — so that is what this module uses.
"""

from __future__ import annotations

import logging
import math
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import orjson
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from logconfig import request_id_var
from security import hash_ip
from settings import settings

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

log = logging.getLogger("vispora-cloud.http")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


# ─── Helpers ─────────────────────────────────────────────────────────────
def _header(scope: Scope, name: bytes) -> bytes | None:
    for k, v in scope.get("headers", ()):
        if k == name:
            return v
    return None


async def _send_json(
    send: Send,
    status: int,
    payload: dict[str, Any],
    extra_headers: Iterable[tuple[bytes, bytes]] = (),
) -> None:
    body = orjson.dumps(payload)
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        *extra_headers,
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def client_ip(scope: Scope, trusted_proxy_hops: int) -> str:
    """Resolve the real client address behind `trusted_proxy_hops` proxies.

    hops == 0 → the TCP peer.  hops == N → the N-th X-Forwarded-For entry
    from the right (the one appended by the outermost trusted proxy).  A
    client-supplied XFF header can only ever push entries further *left*, so
    it cannot influence the result.
    """
    if trusted_proxy_hops > 0:
        raw = _header(scope, b"x-forwarded-for")
        if raw:
            parts = [p.strip() for p in raw.decode("latin-1").split(",") if p.strip()]
            if parts:
                idx = -trusted_proxy_hops if len(parts) >= trusted_proxy_hops else 0
                return parts[idx]
    client = scope.get("client")
    return client[0] if client else "unknown"


def request_client_ip(request: Request) -> str:
    """For route handlers: the resolved client IP (never log or store it raw)."""
    state = request.scope.get("state") or {}
    ip = state.get("client_ip")
    if ip is None:
        ip = client_ip(request.scope, settings.proxy_hops)
    return ip


# ─── Request ID ──────────────────────────────────────────────────────────
class RequestIDMiddleware:
    """Honour a well-formed incoming X-Request-ID, otherwise mint one.  The id
    is echoed on the response, stored in `scope["state"]["request_id"]`, and
    set on the logging contextvar for the life of the request.  Also adds an
    X-Response-Time-Ms header, which is the cheapest latency telemetry there is.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = _header(scope, b"x-request-id")
        rid = uuid.uuid4().hex
        if incoming:
            candidate = incoming.decode("latin-1")
            if _REQUEST_ID_RE.match(candidate):
                rid = candidate
        scope.setdefault("state", {})["request_id"] = rid
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                headers.append((b"x-request-id", rid.encode()))
                headers.append((b"x-response-time-ms", f"{elapsed_ms:.1f}".encode()))
            await send(message)

        token = request_id_var.set(rid)
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(token)


# ─── Security headers ────────────────────────────────────────────────────
_API_CSP = b"default-src 'none'; frame-ancestors 'none'"
# Swagger UI / ReDoc load their assets from jsdelivr and use inline scripts.
_DOCS_CSP = (
    b"default-src 'self'; "
    b"script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    b"style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    b"img-src 'self' data: https://fastapi.tiangolo.com; "
    b"font-src 'self' https://cdn.jsdelivr.net; "
    b"frame-ancestors 'none'"
)


class SecurityHeadersMiddleware:
    """Adds the browser-facing hardening headers to every response.  The API
    only ever talks to the desktop client, so the CSP is `default-src 'none'`;
    the OpenAPI UI pages (when enabled) get the one CSP that lets them render.
    Headers already set by the app are left alone.
    """

    def __init__(
        self, app: ASGIApp, *, hsts_max_age: int, docs_paths: frozenset[str] = frozenset()
    ) -> None:
        self.app = app
        self.docs_paths = docs_paths
        base: list[tuple[bytes, bytes]] = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
            (b"cache-control", b"no-store"),
        ]
        if hsts_max_age > 0:
            base.append(
                (
                    b"strict-transport-security",
                    f"max-age={hsts_max_age}; includeSubDomains".encode(),
                )
            )
        self.api_headers = [*base, (b"content-security-policy", _API_CSP)]
        self.docs_headers = [*base, (b"content-security-policy", _DOCS_CSP)]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        extra = self.docs_headers if path in self.docs_paths else self.api_headers

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {k.lower() for k, _ in headers}
                headers.extend((k, v) for k, v in extra if k not in present)
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ─── Unhandled errors ────────────────────────────────────────────────────
class UnhandledErrorMiddleware:
    """Last line of defence.  Any exception that escapes the routers becomes
    an opaque 500 — `{"ok": false, "err": "internal_error", "request_id": …}` —
    and the full traceback is logged under the request id.  Nothing about the
    exception (class name, message) reaches the client.

    This sits *inside* RequestID and SecurityHeaders so even this response
    carries their headers (Starlette's own 500 handler runs outside every
    user middleware and would not), and it swallows the exception so uvicorn
    does not log the same traceback a second time.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            rid = (scope.get("state") or {}).get("request_id", "-")
            log.exception(
                "Unhandled error in %s %s",
                scope.get("method"),
                scope.get("path"),
                extra={"request_id": rid},
            )
            if response_started:
                # Headers are already on the wire; let the server drop the connection.
                raise
            await _send_json(send, 500, {"ok": False, "err": "internal_error", "request_id": rid})


# ─── Rate limiting ───────────────────────────────────────────────────────
class RateLimitMiddleware:
    """Per-client token bucket.

    Capacity = `burst` tokens, refilled at `per_minute / 60` per second, so a
    client can spend its minute's budget at once and then proceeds at the
    sustained rate.  The bucket key is the peppered HMAC of the client IP
    (never the IP itself).  State lives in this process: one Railway replica,
    one budget.  Scaling to several replicas or `WEB_CONCURRENCY > 1`
    multiplies the effective budget — the upgrade path is a shared store
    (Redis), swapped in behind the same interface.

    Idle buckets cost ~100 bytes; the table is capped (LRU) and swept, so a
    flood from many addresses cannot grow memory without bound.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        per_minute: int,
        burst: int | None,
        trusted_proxy_hops: int,
        exempt_paths: frozenset[str] = frozenset(),
        max_keys: int = 50_000,
    ) -> None:
        self.app = app
        self.enabled = per_minute > 0
        self.rate = per_minute / 60.0
        self.capacity = float(burst or per_minute)
        self.trusted_proxy_hops = trusted_proxy_hops
        self.exempt_paths = exempt_paths
        self.max_keys = max_keys
        self._buckets: OrderedDict[bytes, tuple[float, float]] = OrderedDict()
        self._calls = 0
        self._limit_header = str(int(self.capacity)).encode()

    def _take(self, key: bytes, now: float) -> tuple[bool, float, int]:
        """Returns (allowed, seconds_until_next_token, tokens_remaining)."""
        tokens, last = self._buckets.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        self._buckets[key] = (tokens, now)
        self._buckets.move_to_end(key)
        self._maybe_sweep(now)
        retry_after = 0.0 if allowed else (1.0 - tokens) / self.rate
        return allowed, retry_after, int(tokens)

    def _maybe_sweep(self, now: float) -> None:
        # Hard cap: drop the least-recently-seen client.
        while len(self._buckets) > self.max_keys:
            self._buckets.popitem(last=False)
        # Every 1000 requests, forget buckets that have refilled completely —
        # they are indistinguishable from "never seen".
        self._calls += 1
        if self._calls % 1000 == 0:
            full_after = self.capacity / self.rate
            stale = [k for k, (_, last) in self._buckets.items() if now - last >= full_after]
            for k in stale:
                del self._buckets[k]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.enabled or scope.get("path") in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        ip = client_ip(scope, self.trusted_proxy_hops)
        scope.setdefault("state", {})["client_ip"] = ip
        allowed, retry_after, remaining = self._take(hash_ip(ip), time.monotonic())

        if not allowed:
            wait = max(1, math.ceil(retry_after))
            await _send_json(
                send,
                429,
                {"ok": False, "err": "rate_limited", "retry_after_s": wait},
                extra_headers=[
                    (b"retry-after", str(wait).encode()),
                    (b"x-ratelimit-limit", self._limit_header),
                    (b"x-ratelimit-remaining", b"0"),
                ],
            )
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((b"x-ratelimit-limit", self._limit_header))
                headers.append((b"x-ratelimit-remaining", str(remaining).encode()))
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ─── Body size limit ─────────────────────────────────────────────────────
class BodyTooLarge(StarletteHTTPException):
    """Raised from the receive() wrapper while a body streams in.

    It is an HTTPException on purpose: FastAPI's body reader re-raises
    HTTPExceptions untouched but turns every other exception into a generic
    400, and the app's HTTPException handler then renders this as a proper
    413 with the standard error shape.
    """

    code = "payload_too_large"

    def __init__(self, max_bytes: int) -> None:
        super().__init__(413, f"payload too large (max {max_bytes} bytes)")
        self.max_bytes = max_bytes


class BodyLimitMiddleware:
    """Reject request bodies over `max_bytes` with 413.

    A declared Content-Length over the limit is refused before a byte of body
    is read.  Chunked / undeclared bodies are counted as they stream and cut
    off at the limit; if the app has not started its response yet, a 413 is
    sent, otherwise the connection is simply dropped.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _header(scope, b"content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                await _send_json(send, 400, {"ok": False, "err": "bad_content_length"})
                return
            if length > self.max_bytes:
                await self._reject(send)
                return

        received = 0
        response_started = False

        async def receive_wrapper() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodyTooLarge(self.max_bytes)
            return message

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except BodyTooLarge:
            if not response_started:
                await self._reject(send)

    async def _reject(self, send: Send) -> None:
        exc = BodyTooLarge(self.max_bytes)
        await _send_json(send, 413, {"ok": False, "err": exc.code, "detail": exc.detail})
