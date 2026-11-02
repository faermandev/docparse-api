"""Pure ASGI middlewares (no BaseHTTPMiddleware, so streaming and context vars behave)."""

import re
import time
import uuid

import structlog
from fastapi import status
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.schemas.errors import ErrorBody, ErrorResponse

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_METHODS_WITH_BODY = frozenset({"POST", "PUT", "PATCH"})

logger = structlog.get_logger(__name__)


class RequestContextMiddleware:
    """Assigns a request id, exposes it in the response, and logs one line per request.

    A client-supplied X-Request-ID is reused when well-formed, so ids can be traced across
    services; otherwise a new one is generated.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            logger.info(
                "request_completed",
                method=scope["method"],
                path=scope["path"],
                status_code=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            structlog.contextvars.clear_contextvars()


class BodySizeLimitMiddleware:
    """Rejects oversized request bodies before they are read or spooled to disk.

    Requires Content-Length on requests with a body; the ASGI server guarantees the body is
    not longer than the declared length.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _METHODS_WITH_BODY:
            await self.app(scope, receive, send)
            return

        content_length = Headers(scope=scope).get("content-length")
        if content_length is None or not content_length.isdigit():
            response = _error(
                status.HTTP_411_LENGTH_REQUIRED,
                "length_required",
                "A Content-Length header is required.",
            )
        elif int(content_length) > self.max_body_bytes:
            response = _error(
                status.HTTP_413_CONTENT_TOO_LARGE,
                "file_too_large",
                "Request body exceeds the maximum allowed size.",
                {"max_body_bytes": self.max_body_bytes},
            )
        else:
            await self.app(scope, receive, send)
            return
        await response(scope, receive, send)


def _error(status_code: int, code: str, message: str, details: object = None) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details))
    return JSONResponse(status_code=status_code, content=body.model_dump())
