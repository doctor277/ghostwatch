from __future__ import annotations

from contextlib import suppress
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import ALLOWED_HTTP_HOSTS, ALLOWED_WEBSOCKET_ORIGINS

router = APIRouter()


def is_allowed_host(
    host: str | None, allowed_hosts: set[str] | frozenset[str] = ALLOWED_HTTP_HOSTS
) -> bool:
    return bool(host) and host.casefold() in {item.casefold() for item in allowed_hosts}


def is_allowed_origin(origin: str | None) -> bool:
    if not origin:
        return False
    try:
        parsed = urlparse(origin)
        normalized = f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"
    except ValueError:
        return False
    return (
        not parsed.username
        and not parsed.password
        and not parsed.path
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
        and normalized in ALLOWED_WEBSOCKET_ORIGINS
    )


@router.websocket("/ws/telemetry")
async def telemetry_socket(websocket: WebSocket) -> None:
    allowed_hosts = websocket.app.state.allowed_hosts
    if not is_allowed_host(websocket.headers.get("host"), allowed_hosts):
        await websocket.close(code=1008, reason="Local Host header required")
        return
    if not is_allowed_origin(websocket.headers.get("origin")):
        await websocket.close(code=1008, reason="Local origin required")
        return
    service = websocket.app.state.telemetry
    if not await service.register_client(websocket):
        await websocket.close(code=1013, reason="Too many local clients")
        return
    try:
        await websocket.accept()
        if not await service.send_snapshot(websocket):
            with suppress(Exception):
                await websocket.close(code=1011, reason="Initial telemetry send failed")
            return
        await service.activate_client(websocket)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await service.unregister_client(websocket)
