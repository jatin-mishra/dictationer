import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from contextvars import ContextVar
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

HEADER = b"x-request-id"
MAX_LEN = 128

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdMiddleware:
    """Pure-ASGI middleware: reuse the inbound X-Request-ID or mint one, expose it via a
    ContextVar for logging, and echo it on the response. Pure ASGI (not BaseHTTPMiddleware)
    so streaming responses and background tasks are not affected.

    The ContextVar is deliberately not reset on exit: the server runs each request in its own
    task (own context copy), and Starlette's catch-all 500 handler runs *outside* this
    middleware, so a reset would blank the id in the 500 envelope and its log line."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = dict(scope.get("headers", [])).get(HEADER, b"").decode("latin-1")
        request_id = inbound[:MAX_LEN] if inbound else uuid.uuid4().hex
        request_id_ctx.set(request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((HEADER, request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_id)
