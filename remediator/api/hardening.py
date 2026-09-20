"""Request-surface hardening shared by every operator-facing route.

* Body size limit: any request whose declared or streamed body exceeds the configured cap is
  rejected before a handler (or a JSON parser) sees it.
* Cookie CSRF: state-changing requests authenticated by the operator cookie must carry an
  Origin/Referer that matches the request host (or a `Sec-Fetch-Site` of same-origin/none).
  Bearer-authenticated calls are exempt because browsers cannot attach that header cross-site.
* Operator rate limit: an in-process token bucket per (client, route) for authenticated
  mutations; rejections are counted in `operator_requests_rejected_total`.
* Safe hrefs: provider-supplied URLs rendered as links are reduced to http(s) or dropped.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import metrics
from ..safe_urls import safe_href
from .auth import COOKIE_NAME

__all__ = [
    "BodySizeLimitMiddleware",
    "TokenBucketLimiter",
    "enforce_cookie_csrf",
    "make_operator_guard",
    "safe_href",
    "same_origin",
    "with_security_headers",
]

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """Rejects bodies above `max_bytes` with 413, whether declared via Content-Length or
    streamed in chunks. Raw ASGI so the guard runs before FastAPI buffers the body."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await self._reject(scope, send)
                    return
            except ValueError:
                await self._reject(scope, send, status=400, detail="invalid content-length")
                return
        received = 0
        rejected = False
        response_started = False

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if rejected:
                return
            response_started = True
            await send(message)

        async def limited_receive() -> Message:
            nonlocal received, rejected
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes and not rejected:
                    rejected = True
                    if not response_started:
                        await self._reject(scope, send)
                    return {"type": "http.disconnect"}
            return message

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not rejected:
                raise

    async def _reject(
        self, scope: Scope, send: Send, *, status: int = 413, detail: str = "request body too large"
    ) -> None:
        metrics.operator_requests_rejected_total.labels(reason="body_too_large").inc()
        response = JSONResponse({"detail": detail}, status_code=status)
        await response(scope, _noop_receive, send)


async def _noop_receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


def _origin_host(value: str) -> str | None:
    parsed = urlsplit(value)
    return parsed.netloc.lower() or None


def same_origin(request: Request) -> bool:
    """True when the browser-supplied provenance headers point at this host. Browsers always
    send Origin (and Sec-Fetch-Site) on cross-site POSTs, so a request that carries the
    operator cookie but no provenance header did not come from a browser form and is
    treated as cross-site; a cookie-less request with no provenance (login from a script)
    has nothing to forge and is allowed."""
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site in {"same-origin", "none"}
    host = request.headers.get("host", "").lower()
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if value:
            return _origin_host(value) == host
    return COOKIE_NAME not in request.cookies


def enforce_cookie_csrf(request: Request) -> None:
    """Reject cookie-authenticated mutations that did not originate from this site."""
    if request.method not in MUTATING_METHODS:
        return
    if request.headers.get("authorization", "").startswith("Bearer "):
        return
    if not same_origin(request):
        metrics.operator_requests_rejected_total.labels(reason="csrf").inc()
        raise HTTPException(status_code=403, detail="cross-site request rejected")


class TokenBucketLimiter:
    """Fixed-capacity token bucket per key; refills `rate` tokens per second."""

    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = capacity
        self.refill = refill_per_second
        self._clock = clock
        self._state: dict[str, tuple[float, float]] = defaultdict(
            lambda: (float(capacity), self._clock())
        )

    def allow(self, key: str) -> bool:
        tokens, last = self._state[key]
        now = self._clock()
        tokens = min(float(self.capacity), tokens + (now - last) * self.refill)
        if tokens < 1.0:
            self._state[key] = (tokens, now)
            return False
        self._state[key] = (tokens - 1.0, now)
        return True


def client_key(request: Request) -> str:
    client = request.client.host if request.client else "unknown"
    return f"{client}:{request.url.path}"


def make_operator_guard(limiter: TokenBucketLimiter) -> Callable[[Request], Awaitable[None]]:
    """FastAPI dependency: CSRF check then rate limit, for authenticated operator mutations."""

    async def guard(request: Request) -> None:
        if request.method not in MUTATING_METHODS:
            return
        enforce_cookie_csrf(request)
        if not limiter.allow(client_key(request)):
            metrics.operator_requests_rejected_total.labels(reason="rate_limited").inc()
            raise HTTPException(
                status_code=429,
                detail="too many operator actions; retry shortly",
                headers={"Retry-After": "1"},
            )

    return guard


def with_security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response
