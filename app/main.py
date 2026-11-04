from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.ai import LocalAIAnalyst
from app.api.routes import router as api_router
from app.api.websocket import is_allowed_host, is_allowed_origin
from app.api.websocket import router as websocket_router
from app.config import ALLOWED_HTTP_HOSTS, DATABASE_PATH, FRONTEND_DIR
from app.service import TelemetryService
from app.storage.database import Database


class LocalSecurityMiddleware:
    """Enforce local browser boundaries and add static security headers."""

    def __init__(self, app: ASGIApp, allowed_hosts: frozenset[str]) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host")
        if not is_allowed_host(host, self.allowed_hosts):
            response = PlainTextResponse("Invalid Host header", status_code=400)
            await response(scope, receive, send)
            return
        if scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = headers.get("origin")
            if origin is not None and not is_allowed_origin(origin):
                response = PlainTextResponse("Invalid request origin", status_code=403)
                await response(scope, receive, send)
                return

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Content-Type-Options"] = "nosniff"
                response_headers["X-Frame-Options"] = "DENY"
                response_headers["Referrer-Policy"] = "no-referrer"
                response_headers["Permissions-Policy"] = (
                    "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
                )
                response_headers["Cross-Origin-Resource-Policy"] = "same-origin"
                response_headers["Content-Security-Policy"] = (
                    "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                    "form-action 'self'; object-src 'none'; img-src 'self' data:; "
                    "font-src 'self'; style-src 'self'; script-src 'self'; "
                    "connect-src 'self' ws://127.0.0.1:8765 ws://localhost:8765"
                )
            await send(message)

        await self.app(scope, receive, secure_send)


def create_app(database_path: Path | None = None, start_background: bool = True) -> FastAPI:
    database = Database(database_path or DATABASE_PATH)
    telemetry = TelemetryService(database)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        await telemetry.initialize_storage()
        if start_background:
            await telemetry.start()
        application.state.database = database
        application.state.telemetry = telemetry
        try:
            yield
        finally:
            if start_background:
                await telemetry.stop()

    app = FastAPI(
        title="GhostWatch V3",
        description="Local-only Windows cyber defense monitoring dashboard",
        version="3.0.0-rc.5",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    allowed_hosts = set(ALLOWED_HTTP_HOSTS)
    if not start_background:
        allowed_hosts.add("testserver")
    app.state.allowed_hosts = frozenset(allowed_hosts)
    app.state.ai_analyst = LocalAIAnalyst()
    app.add_middleware(
        LocalSecurityMiddleware, allowed_hosts=app.state.allowed_hosts
    )

    app.include_router(api_router)
    app.include_router(websocket_router)
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="assets")

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    return app


app = create_app()
