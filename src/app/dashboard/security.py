from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from app.config import Settings

logger = logging.getLogger(__name__)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_TTL_SECONDS = 4 * 60 * 60


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _sign(secret: str, payload: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return _b64(digest)


def create_csrf_token(secret: str, *, now: int | None = None) -> str:
    issued_at = int(time.time() if now is None else now)
    nonce = secrets.token_urlsafe(18)
    payload = f"{issued_at}:{nonce}"
    return f"{payload}:{_sign(secret, payload)}"


def validate_csrf_token(secret: str, token: str, *, now: int | None = None) -> bool:
    parts = str(token or "").split(":")
    if len(parts) != 3:
        return False
    try:
        issued_at = int(parts[0])
    except ValueError:
        return False
    current = int(time.time() if now is None else now)
    if issued_at > current + 60 or current - issued_at > CSRF_TTL_SECONDS:
        return False
    payload = f"{parts[0]}:{parts[1]}"
    return hmac.compare_digest(parts[2], _sign(secret, payload))


def csrf_input(request: Request) -> str:
    token = getattr(request.state, "csrf_token", "")
    return f'<input type="hidden" name="csrf_token" value="{token}">'


def _host_without_port(host: str) -> str:
    host = host.strip().casefold()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host
    if ":" in host:
        return host.rsplit(":", 1)[0]
    return host


def validate_host_header(host: str, allowed_hosts: set[str]) -> bool:
    if not host:
        return False
    normalized = host.strip().casefold()
    allowed = {item.casefold() for item in allowed_hosts if item}
    if normalized in allowed:
        return True
    return _host_without_port(normalized) in allowed


def allowed_origins(settings: Settings) -> set[str]:
    origins = {"http://127.0.0.1:8081", "http://localhost:8081"}
    configured = settings.dashboard_public_origin.strip().rstrip("/")
    if configured:
        origins.add(configured)
    return origins


def validate_origin_or_referer(request: Request, settings: Settings) -> bool:
    expected = allowed_origins(settings)
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/") in expected
    referer = request.headers.get("referer")
    if not referer:
        return False
    parsed = urlparse(referer)
    if not parsed.scheme or not parsed.netloc:
        return False
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/") in expected


@dataclass
class RateLimitState:
    hits: dict[str, deque[float]]

    @classmethod
    def empty(cls) -> "RateLimitState":
        return cls(hits=defaultdict(deque))

    def allow(self, key: str, *, limit: int, now: float | None = None) -> bool:
        if limit <= 0:
            return True
        current = time.time() if now is None else now
        window_start = current - 60
        bucket = self.hits[key]
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(current)
        return True


class DashboardSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings
        self.rate_limits = RateLimitState.empty()

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = time.perf_counter()
        status_code = 500
        try:
            host = request.headers.get("host", "")
            if not validate_host_header(host, self.settings.dashboard_allowed_host_set):
                response: Response = PlainTextResponse("Invalid host", status_code=400)
            elif request.method not in SAFE_METHODS and not self._write_request_allowed(request):
                response = PlainTextResponse("Forbidden", status_code=403)
            else:
                if request.method in SAFE_METHODS:
                    request.state.csrf_token = create_csrf_token(self.settings.dashboard_csrf_secret)
                response = await call_next(request)
            status_code = response.status_code
            return self._with_security_headers(response)
        except HTTPException as exc:
            status_code = exc.status_code
            return self._with_security_headers(PlainTextResponse(str(exc.detail), status_code=exc.status_code))
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "dashboard request route=%s method=%s status=%s duration_ms=%s",
                request.url.path,
                request.method,
                status_code,
                elapsed_ms,
            )

    def _write_request_allowed(self, request: Request) -> bool:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.settings.dashboard_max_form_bytes:
                    raise HTTPException(status_code=413, detail="Request body is too large")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid content length") from exc
        if not validate_origin_or_referer(request, self.settings):
            raise HTTPException(status_code=403, detail="Invalid origin")
        client_host = request.client.host if request.client else "unknown"
        key = f"{client_host}:{request.url.path}"
        if not self.rate_limits.allow(key, limit=self.settings.dashboard_write_rate_limit_per_minute):
            raise HTTPException(status_code=429, detail="Too many write requests")
        return True

    @staticmethod
    def _with_security_headers(response: Response) -> Response:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data:; "
            "media-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'; "
            "script-src 'self'; "
            "style-src 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response
