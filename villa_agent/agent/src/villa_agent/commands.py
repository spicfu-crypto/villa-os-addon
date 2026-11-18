"""Commands from the cloud: the agent is the real security boundary (Prompt 6).

The cloud may be wrong or compromised, so every command is checked HERE, against
rules that live only in this file and in the villa's own site file:

1. The kill switch COMMANDS_ENABLED (default false) refuses everything.
2. The message is signed with HMAC-SHA256 using this villa's own secret
   (COMMAND_SECRET). The signature covers the exact payload bytes, so there is no
   re-encoding ambiguity. Compared in constant time.
3. It is for this villa (site_id), not expired (max 60 s + small clock skew), and
   its id has never been seen before (replay protection survives restarts).
4. The command type is in the closed list below. Locks are NOT in it.
5. The target is a device in the site file, of the right type, with
   allow_commands: true. Parameters are checked for type and range; unknown
   parameters are rejected.
6. A rate limit stops a runaway sender from flapping devices.

Only then is the canonical name translated to the Home Assistant entity_id and one
of the services in ha_ws.ALLOWED_SERVICES called. Every decision is written to a
local audit table and to the log (never the secret).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from villa_agent.site import Device, Site

MAX_PAYLOAD_BYTES = 4096
MAX_LIFETIME_S = 60
CLOCK_SKEW_S = 30
RATE_LIMIT = 20  # commands per minute
_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class CommandRejected(Exception):
    """A command that must not run. `reason` is a short machine-readable code."""

    def __init__(self, reason: str, command_id: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.command_id = command_id


@dataclass(frozen=True)
class ServiceCall:
    command_id: str
    command_type: str
    target: str
    domain: str
    service: str
    entity_id: str
    data: dict[str, Any]


def _on_off(params: dict[str, Any]) -> str:
    on = params.get("on")
    if not isinstance(on, bool):
        raise CommandRejected("bad_params")
    return "turn_on" if on else "turn_off"


def _set_temp(params: dict[str, Any]) -> dict[str, Any]:
    t = params.get("temperature")
    if isinstance(t, bool) or not isinstance(t, int | float) or not 16 <= t <= 30:
        raise CommandRejected("bad_params")
    return {"temperature": round(float(t) * 2) / 2}  # half degrees, like most A/C units


# The closed list: command type -> (device type, allowed parameter names).
# Nothing outside this table can ever reach Home Assistant.
COMMAND_TYPES: dict[str, tuple[str, frozenset[str]]] = {
    "ac_off": ("ac", frozenset()),
    "ac_set": ("ac", frozenset({"temperature"})),
    "light_set": ("light", frozenset({"on"})),
    "plug_set": ("plug", frozenset({"on"})),
}


def _service_for(kind: str, dev: Device, params: dict[str, Any]) -> tuple[str, str, dict]:
    if kind == "ac_off":
        return "climate", "turn_off", {}
    if kind == "ac_set":
        return "climate", "set_temperature", _set_temp(params)
    # light_<name> may be a light or a switch entity; plug_<name> is a switch.
    domain = dev.domain if kind == "light_set" else "switch"
    if domain not in ("light", "switch") or dev.domain != domain:
        raise CommandRejected("bad_target")
    return domain, _on_off(params), {}


def sign(payload: bytes, secret: bytes) -> str:
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _parse_time(v: Any) -> float:
    if not isinstance(v, str) or len(v) > 40:
        raise CommandRejected("bad_message")
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise CommandRejected("bad_message") from None
    if dt.tzinfo is None:
        raise CommandRejected("bad_message")
    return dt.timestamp()


class SeenStore:
    """Command ids already handled, plus a local audit trail. Survives restarts."""

    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, expires_at REAL NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS audit (ts TEXT NOT NULL, id TEXT, type TEXT,"
            " target TEXT, ok INTEGER NOT NULL, reason TEXT)"
        )
        self.db.commit()

    def claim(self, command_id: str, expires_at: float, now: float) -> bool:
        """Record the id. False if it was seen before (a replay)."""
        self.db.execute("DELETE FROM seen WHERE expires_at < ?", (now - 3600,))
        try:
            self.db.execute("INSERT INTO seen VALUES (?, ?)", (command_id, expires_at))
        except sqlite3.IntegrityError:
            self.db.commit()
            return False
        self.db.commit()
        return True

    def audit(self, cid: str | None, ctype: str | None, target: str | None, ok: bool, reason: str):
        self.db.execute(
            "INSERT INTO audit VALUES (?, ?, ?, ?, ?, ?)",
            (
                datetime.now(UTC).isoformat(timespec="seconds"),
                cid,
                ctype,
                target,
                int(ok),
                reason,
            ),
        )
        # Keep the local trail bounded (about a year of normal use).
        self.db.execute(
            "DELETE FROM audit WHERE rowid IN (SELECT rowid FROM audit ORDER BY rowid DESC"
            " LIMIT -1 OFFSET 20000)"
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()


class CommandGate:
    """Decides whether a raw MQTT command message may run. Pure apart from SeenStore."""

    def __init__(self, site: Site, secret: bytes | None, enabled: bool, store: SeenStore):
        self.site = site
        self.secret = secret
        self.enabled = enabled
        self.store = store
        self._recent: deque[float] = deque()

    def check(self, raw: bytes, now: float | None = None) -> ServiceCall:
        now = time.time() if now is None else now
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise CommandRejected("too_large")
        try:
            outer = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise CommandRejected("bad_message") from None
        if not isinstance(outer, dict) or set(outer) != {"payload", "sig"}:
            raise CommandRejected("bad_message")
        payload, sig = outer["payload"], outer["sig"]
        if not isinstance(payload, str) or not isinstance(sig, str):
            raise CommandRejected("bad_message")
        try:
            body = json.loads(payload)
        except ValueError:
            raise CommandRejected("bad_message") from None
        cid = body.get("id") if isinstance(body, dict) else None
        cid = cid if isinstance(cid, str) and _ID.match(cid) else None

        if not self.enabled:
            raise CommandRejected("commands_disabled", cid)
        if not self.secret:
            raise CommandRejected("no_secret", cid)
        if not hmac.compare_digest(sign(payload.encode(), self.secret), sig.lower()):
            raise CommandRejected("bad_signature", cid)

        # From here on the message really comes from someone holding our secret.
        if not isinstance(body, dict) or cid is None:
            raise CommandRejected("bad_message", cid)
        allowed = {"v", "id", "site_id", "type", "target", "params", "issued_at", "expires_at"}
        if set(body) - allowed or body.get("v") != 1:
            raise CommandRejected("bad_message", cid)
        if body.get("site_id") != self.site.site_id:
            raise CommandRejected("wrong_site", cid)
        issued, expires = _parse_time(body.get("issued_at")), _parse_time(body.get("expires_at"))
        if expires - issued > MAX_LIFETIME_S or issued > now + CLOCK_SKEW_S:
            raise CommandRejected("bad_expiry", cid)
        if now > expires + CLOCK_SKEW_S:
            raise CommandRejected("expired", cid)
        if not self.store.claim(cid, expires, now):
            raise CommandRejected("replay", cid)

        kind = body.get("type")
        if not isinstance(kind, str) or kind not in COMMAND_TYPES:
            raise CommandRejected("unknown_type", cid)
        device_type, param_names = COMMAND_TYPES[kind]
        target = body.get("target")
        dev = self.site.devices.get(target) if isinstance(target, str) else None
        if dev is None:
            raise CommandRejected("unknown_target", cid)
        if dev.type.name != device_type or not dev.type.commandable:
            raise CommandRejected("bad_target", cid)
        if not dev.allow_commands:
            raise CommandRejected("not_allowed", cid)
        params = body.get("params", {})
        if not isinstance(params, dict) or set(params) - param_names:
            raise CommandRejected("bad_params", cid)

        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= RATE_LIMIT:
            raise CommandRejected("rate_limited", cid)

        try:
            domain, service, data = _service_for(kind, dev, params)
        except CommandRejected as e:
            raise CommandRejected(e.reason, cid) from None
        self._recent.append(now)
        return ServiceCall(cid, kind, dev.canonical, domain, service, dev.entity_id, data)
