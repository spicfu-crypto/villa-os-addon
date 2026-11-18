"""Minimal Home Assistant WebSocket API client, read-only except for one narrow door.

Only the message types in ALLOWED_TYPES can ever be sent by the normal path.
The single exception is call_service(), which accepts ONLY the (domain, service) pairs
in ALLOWED_SERVICES, for exactly one entity, with only the data keys listed there.
It is used by the command path (commands.py), after the command has been verified.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import websockets

log = logging.getLogger(__name__)

# Closed list. Anything else raises ReadOnlyViolation before it reaches the socket.
ALLOWED_TYPES = frozenset({"get_states", "get_config", "subscribe_events", "ping"})
ALLOWED_EVENT_TYPES = frozenset({"state_changed"})
# (domain, service) -> data keys allowed besides the target entity. Locks are not here.
ALLOWED_SERVICES: dict[tuple[str, str], frozenset[str]] = {
    ("climate", "turn_off"): frozenset(),
    ("climate", "set_temperature"): frozenset({"temperature"}),
    ("light", "turn_on"): frozenset(),
    ("light", "turn_off"): frozenset(),
    ("switch", "turn_on"): frozenset(),
    ("switch", "turn_off"): frozenset(),
}


class HAError(Exception):
    pass


class HAAuthError(HAError):
    pass


class ReadOnlyViolation(HAError):
    pass


def websocket_url(ha_url: str) -> str:
    """http://host:8123 -> ws://host:8123/api/websocket (ws:// urls are kept as they are)."""
    parts = urlsplit(ha_url.rstrip("/"))
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(parts.scheme)
    if scheme is None:
        raise ValueError(f"unsupported Home Assistant URL scheme: {parts.scheme!r}")
    path = parts.path
    if not path.endswith("/websocket"):
        path = path + "/api/websocket"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


class HAClient:
    def __init__(self, url: str, token: str, *, open_timeout: float = 10.0):
        self._url = websocket_url(url)
        self._token = token
        self._open_timeout = open_timeout
        self._ws: Any = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self.ha_version: str | None = None

    async def __aenter__(self) -> HAClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        self._ws = await websockets.connect(
            self._url, open_timeout=self._open_timeout, max_size=32 * 1024 * 1024
        )
        first = json.loads(await self._ws.recv())
        if first.get("type") != "auth_required":
            raise HAError(f"unexpected first message from Home Assistant: {first.get('type')}")
        self.ha_version = first.get("ha_version")
        # The token is sent once, here, and never logged.
        await self._ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        reply = json.loads(await self._ws.recv())
        if reply.get("type") == "auth_invalid":
            raise HAAuthError("Home Assistant rejected the access token")
        if reply.get("type") != "auth_ok":
            raise HAError(f"unexpected auth reply: {reply.get('type')}")
        self.ha_version = reply.get("ha_version", self.ha_version)
        self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        if self._ws is not None:
            await self._ws.close()
        self._fail_pending(HAError("connection closed"))

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    log.warning("ignoring malformed message from Home Assistant")
                    continue
                # HA may batch several messages in one JSON array.
                for m in msg if isinstance(msg, list) else [msg]:
                    self._dispatch(m)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._fail_pending(HAError("connection to Home Assistant lost"))
            await self._events.put(None)  # wake up event consumers

    def _dispatch(self, m: Any) -> None:
        if not isinstance(m, dict):
            log.warning("ignoring non-object message from Home Assistant")
            return
        mtype = m.get("type")
        if mtype == "event":
            event = m.get("event")
            if isinstance(event, dict):
                self._events.put_nowait(event)
            else:
                log.warning("ignoring event message without event body")
            return
        if mtype in ("result", "pong"):
            fut = self._pending.pop(m.get("id"), None)  # type: ignore[arg-type]
            if fut is None or fut.done():
                return
            if mtype == "pong" or m.get("success"):
                fut.set_result(m.get("result"))
            else:
                err = m.get("error") or {}
                fut.set_exception(HAError(f"{err.get('code')}: {err.get('message')}"))

    async def call_service(
        self, domain: str, service: str, entity_id: str, data: dict[str, Any], timeout: float = 10.0
    ) -> None:
        """Call one allowed service on one entity. Anything else raises ReadOnlyViolation."""
        allowed = ALLOWED_SERVICES.get((domain, service))
        if allowed is None:
            raise ReadOnlyViolation(f"service {domain}.{service} is not allowed")
        if set(data) - allowed:
            raise ReadOnlyViolation(f"data keys not allowed for {domain}.{service}")
        if not entity_id.startswith(domain + ".") or "," in entity_id or " " in entity_id:
            raise ReadOnlyViolation("target must be exactly one entity of the same domain")
        await self._call(
            {
                "type": "call_service",
                "domain": domain,
                "service": service,
                "service_data": dict(data),
                "target": {"entity_id": entity_id},
            },
            timeout=timeout,
            _service_checked=True,
        )

    async def _call(
        self, payload: dict[str, Any], timeout: float = 30.0, *, _service_checked: bool = False
    ) -> Any:
        mtype = payload.get("type")
        if mtype == "call_service" and _service_checked:
            pass  # validated by call_service() above
        elif mtype not in ALLOWED_TYPES:
            raise ReadOnlyViolation(f"message type {mtype!r} is not allowed (read-only client)")
        if mtype == "subscribe_events" and payload.get("event_type") not in ALLOWED_EVENT_TYPES:
            raise ReadOnlyViolation("only state_changed events may be subscribed")
        if self._ws is None:
            raise HAError("not connected")
        msg_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send(json.dumps({**payload, "id": msg_id}))
        return await asyncio.wait_for(fut, timeout)

    async def get_states(self) -> list[dict[str, Any]]:
        result = await self._call({"type": "get_states"})
        return result if isinstance(result, list) else []

    async def get_config(self) -> dict[str, Any]:
        result = await self._call({"type": "get_config"})
        if isinstance(result, dict) and result.get("version"):
            self.ha_version = result["version"]
        return result if isinstance(result, dict) else {}

    async def ping(self, timeout: float = 10.0) -> None:
        await self._call({"type": "ping"}, timeout=timeout)

    async def subscribe_state_changed(self) -> None:
        await self._call({"type": "subscribe_events", "event_type": "state_changed"})

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield state_changed events until the connection drops."""
        while True:
            ev = await self._events.get()
            if ev is None:
                return
            yield ev
