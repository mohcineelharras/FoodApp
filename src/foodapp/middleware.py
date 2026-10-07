"""Request limits and response headers."""

from __future__ import annotations

import html

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from foodapp.config import Settings
from foodapp.security import security_headers


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        extras = security_headers(self.settings, path)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw_headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"server"
                ]
                present = {key.lower() for key, _value in raw_headers}
                for key, value in extras:
                    if key not in present:
                        raw_headers.append((key, value))
                message = {**message, "headers": raw_headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


class BodyLimitMiddleware:
    """Buffer small form posts and refuse anything larger before the handler runs."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET") not in {"POST", "PUT", "PATCH", "DELETE"}:
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body = message.get("body", b"")
            total += len(body)
            if total > self.max_bytes:
                await _send_html(send, 413, "That submission is too large.")
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break

        payload = b"".join(chunks)
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}

        await self.app(scope, replay, send)


async def _send_html(send: Send, status: int, message: str) -> None:
    body = (
        "<!DOCTYPE html><html lang=\"en\"><meta charset=\"utf-8\">"
        f"<title>FoodApp</title><body><p>{html.escape(message)}</p></body></html>"
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
