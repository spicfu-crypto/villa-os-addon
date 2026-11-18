"""The Villa Agent main loop (READ-ONLY phase).

  Home Assistant --(WebSocket, outbound)--> agent --(MQTT over TLS, outbound)--> cloud

- Listens to state_changed events, keeps only devices in the site file, translates
  entity_id -> canonical name.
- Publishes a full snapshot on start and every SNAPSHOT_INTERVAL_S (default 300 s).
- Publishes each change immediately, a heartbeat every HEARTBEAT_INTERVAL_S (60 s).
- Subscribes to exactly one topic, villaos/<site>/command, and only when this villa has
  a COMMAND_SECRET. Every command is verified by commands.CommandGate (signature,
  expiry, replay, closed list, allow_commands, ranges, kill switch) before Home
  Assistant is called. Results go back on villaos/<site>/command_result.

Reliability rules:
- No single error (disk full, broken message, network) may silently stop a loop.
  Loops log and continue; if a loop dies anyway, the whole process exits non-zero so
  Docker / the HA Supervisor restarts it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import random
import signal
import sqlite3
import ssl
import time
from datetime import UTC, datetime
from typing import Any

import aiomqtt

from villa_agent import __version__
from villa_agent.commands import CommandGate, CommandRejected, SeenStore
from villa_agent.config import AgentConfig, ConfigError
from villa_agent.ha_ws import HAAuthError, HAClient, HAError
from villa_agent.logs import setup_logging
from villa_agent.outbox import Outbox
from villa_agent.site import Device, Site, SiteFileError, load_site

log = logging.getLogger("villa_agent")

SCHEMA_VERSION = 1
# Attributes we forward, per device type (besides battery and device_class).
_EXTRA_ATTRS = {"ac": ("current_temperature", "temperature", "hvac_action")}
# A connection must stay up this long before its retry delay resets (no reconnect storms).
STABLE_AFTER_S = 10.0
# After this many token rejections in a row, retry only hourly (HA may ban the IP otherwise).
AUTH_FAILURES_BEFORE_SLOWDOWN = 3


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def device_view(dev: Device, ha_state: dict[str, Any] | None) -> dict[str, Any]:
    """Translate one HA state object into the canonical payload for a device."""
    if ha_state is None:
        return {"device": dev.canonical, "type": dev.type.name, "state": None, "missing": True}
    attrs = ha_state.get("attributes") or {}
    picked: dict[str, Any] = {}
    battery = attrs.get("battery", attrs.get("battery_level"))
    if battery is not None:
        picked["battery"] = battery
    if attrs.get("device_class"):
        picked["device_class"] = attrs["device_class"]
    for a in _EXTRA_ATTRS.get(dev.type.name, ()):
        if a in attrs:
            picked[a] = attrs[a]
    return {
        "device": dev.canonical,
        "type": dev.type.name,
        "state": ha_state.get("state"),
        "unit": attrs.get("unit_of_measurement"),
        "attributes": picked,
        "changed_at": ha_state.get("last_changed"),
    }


class Backoff:
    """Exponential retry delay with jitter, reset only after a connection proved stable."""

    def __init__(self, start: float = 1.0, cap: float = 60.0):
        self.start, self.cap, self.current = start, cap, start
        self._connected_at: float | None = None

    def connected(self) -> None:
        self._connected_at = time.monotonic()

    def next_delay(self) -> float:
        if (
            self._connected_at is not None
            and time.monotonic() - self._connected_at >= STABLE_AFTER_S
        ):
            self.current = self.start
        self._connected_at = None
        delay = self.current * random.uniform(0.8, 1.2)  # noqa: S311 - jitter, not crypto
        self.current = min(self.current * 2, self.cap)
        return delay


INVENTORY_MAX = 300


def entity_ref(entity_id: str) -> str:
    return hashlib.sha256(entity_id.encode()).hexdigest()[:16]


def inventory_items(states: list[dict[str, Any]], used: dict[str, Any]) -> list[dict[str, Any]]:
    """Supported entities not used by this villa yet (never people, phones or zones)."""
    from villa_agent.discover import entities_from_states, suggest_mapping

    entities = [e for e in entities_from_states(states) if e.entity_id not in used]
    mapping, _ = suggest_mapping(entities)
    by_state = {s.get("entity_id"): s for s in states if isinstance(s, dict)}
    items = []
    for canonical, e in sorted(mapping.items(), key=lambda kv: kv[1].entity_id):
        st = by_state.get(e.entity_id) or {}
        items.append(
            {
                # entity_ids never leave the villa: Villa OS refers to it by this ref,
                # which only the agent can map back.
                "ref": entity_ref(e.entity_id),
                # Without a friendly name, HA's object id (not the full entity_id).
                "name": (
                    e.friendly_name
                    if e.friendly_name != e.entity_id
                    else e.object_id.replace("_", " ")
                )[:120],
                "suggested": canonical[:80],
                "state": None if st.get("state") is None else str(st.get("state"))[:255],
                "unit": e.unit[:20] if e.unit else None,
            }
        )
    return items


class Agent:
    def __init__(
        self, cfg: AgentConfig, site: Site, outbox: Outbox, gate: CommandGate | None = None
    ):
        self.cfg = cfg
        self.gate = gate
        self._ha: HAClient | None = None
        self._results_sent: list[float] = []
        self.site = site
        self.outbox = outbox
        self._by_entity = site.by_entity_id()
        self._states: dict[str, dict[str, Any]] = {}  # entity_id -> latest HA state
        self._last_sent: dict[str, tuple[Any, ...]] = {}  # canonical -> fingerprint
        self._inventory_fp: str | None = None
        self.connected_to_ha = False
        self.connected_to_mqtt = False
        self.ha_version: str | None = None
        self.enqueue_failures = 0
        self.auth_failures = 0
        self._hb_fallback = 0
        self._started = time.monotonic()
        self._wake = asyncio.Event()
        self._mqtt: aiomqtt.Client | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------ topics & messages
    def topic(self, kind: str) -> str:
        return f"villaos/{self.site.site_id}/{kind}"

    def _envelope(self, kind: str, seq: int) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "site_id": self.site.site_id,
            "agent_instance": self.outbox.instance_id,
            "seq": seq,
            "ts": utc_now(),
            "kind": kind,
        }

    def enqueue(self, kind: str, body: dict[str, Any]) -> bool:
        """Queue a message. Never raises: a full or broken disk must not stop the agent."""
        try:
            msg = {**self._envelope(kind, self.outbox.next_seq()), **body}
            self.outbox.put(
                self.topic(kind), msg, replace_topic=(kind in ("snapshot", "inventory"))
            )
        except sqlite3.Error as e:
            self.enqueue_failures += 1
            log.error(
                "cannot write to the local queue, message lost",
                extra={"kind": kind, "error": str(e)},
            )
            return False
        self._wake.set()
        return True

    def publish_snapshot(self) -> None:
        devices = []
        for d in self.site.devices.values():
            v = device_view(d, self._states.get(d.entity_id))
            self._last_sent[d.canonical] = _fingerprint(v)
            devices.append({**v, "critical": d.critical, "allow_commands": d.allow_commands})
        self.enqueue("snapshot", {"devices": devices})

    def publish_inventory(self, states: list[dict[str, Any]], *, force: bool = False) -> bool:
        """Send the supported Home Assistant entities that this villa does NOT use yet, so
        Villa OS can show "new device". Sent on connect and when the list changes."""
        items = inventory_items(states, self._by_entity)
        fingerprint = json.dumps(items, sort_keys=True)
        if not force and fingerprint == self._inventory_fp:
            return False
        self._inventory_fp = fingerprint
        self.enqueue("inventory", {"unused": items[:INVENTORY_MAX], "unused_total": len(items)})
        return True

    def on_state_changed(self, event: dict[str, Any]) -> None:
        data = event.get("data")
        if not isinstance(data, dict):
            return
        eid = data.get("entity_id")
        dev = self._by_entity.get(eid) if isinstance(eid, str) else None
        if dev is None:
            return  # not in the site file: ignored, never leaves the villa
        new = data.get("new_state")
        if new is not None and not isinstance(new, dict):
            return
        if new is not None:
            # Events queued while we read the full state list can be older than it.
            known = _parse_ts((self._states.get(dev.entity_id) or {}).get("last_updated"))
            incoming = _parse_ts(new.get("last_updated"))
            if known and incoming and incoming <= known:
                return
            self._states[dev.entity_id] = new
        else:
            self._states.pop(dev.entity_id, None)
        view = device_view(dev, new)
        fp = _fingerprint(view)
        if self._last_sent.get(dev.canonical) == fp:
            return  # only an unimportant attribute changed
        self._last_sent[dev.canonical] = fp
        self.enqueue("event", view)

    def heartbeat(self) -> dict[str, Any]:
        self._hb_fallback += 1
        try:
            queue_length, dropped, seq = (
                len(self.outbox),
                self.outbox.dropped,
                self.outbox.next_heartbeat_seq(),
            )
            self._hb_fallback = seq
        except sqlite3.Error:
            # Disk problem: still send a valid heartbeat (seq >= 1), flagged queue_length -1.
            queue_length, dropped, seq = -1, -1, self._hb_fallback
        return {
            **self._envelope("heartbeat", seq),
            "agent_version": __version__,
            "ha_version": self.ha_version,
            "uptime_s": round(time.monotonic() - self._started),
            "connected_to_ha": self.connected_to_ha,
            "queue_length": queue_length,
            "queue_dropped": dropped,
            "queue_write_failures": self.enqueue_failures,
        }

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), seconds)

    # ------------------------------------------------------------ Home Assistant side
    async def ha_loop(self) -> None:
        backoff = Backoff()
        while not self._stop.is_set():
            try:
                async with HAClient(self.cfg.ha_url, self.cfg.ha_token) as ha:
                    self._ha = ha
                    self.auth_failures = 0
                    await ha.get_config()
                    self.ha_version = ha.ha_version
                    await ha.subscribe_state_changed()  # subscribe first: no gap
                    states = await ha.get_states()
                    self._states = {
                        s["entity_id"]: s
                        for s in states
                        if isinstance(s, dict) and s.get("entity_id") in self._by_entity
                    }
                    self.connected_to_ha = True
                    backoff.connected()
                    log.info("connected to Home Assistant", extra={"ha_version": self.ha_version})
                    self.publish_snapshot()
                    self.publish_inventory(states, force=True)
                    watchdog = asyncio.create_task(self._ha_watchdog(ha))
                    try:
                        async for ev in ha.events():
                            try:
                                self.on_state_changed(ev)
                            except Exception:
                                log.exception("could not handle a Home Assistant event")
                    finally:
                        watchdog.cancel()
                log.warning("connection to Home Assistant closed")
            except HAAuthError:
                self.auth_failures += 1
                log.error(
                    "Home Assistant rejected the token; check HA_TOKEN",
                    extra={"failures_in_a_row": self.auth_failures},
                )
            except (OSError, HAError, TimeoutError) as e:
                log.warning("cannot reach Home Assistant", extra={"error": type(e).__name__})
            except Exception:
                log.exception("unexpected error in Home Assistant loop")
            self.connected_to_ha = False
            self._ha = None
            delay = backoff.next_delay()
            if self.auth_failures >= AUTH_FAILURES_BEFORE_SLOWDOWN:
                delay = 3600.0
            await self._sleep(delay)

    async def _ha_watchdog(self, ha: HAClient) -> None:
        """Detect a silently dead connection (e.g. HA host lost power)."""
        while True:
            await asyncio.sleep(30)
            try:
                await ha.ping(timeout=10)
            except Exception:
                log.warning("Home Assistant did not answer ping, reconnecting")
                await ha.close()
                return

    # ------------------------------------------------------------ MQTT side
    def _tls_context(self) -> ssl.SSLContext | None:
        if not self.cfg.mqtt_tls:
            return None
        ctx = ssl.create_default_context(cafile=self.cfg.mqtt_ca_file)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if self.cfg.mqtt_cert_file:
            ctx.load_cert_chain(self.cfg.mqtt_cert_file, self.cfg.mqtt_key_file)
        return ctx

    def _client(self) -> aiomqtt.Client:
        return aiomqtt.Client(
            hostname=self.cfg.mqtt_host,
            port=self.cfg.mqtt_port,
            username=self.cfg.mqtt_username,
            password=self.cfg.mqtt_password,
            identifier=f"villa-agent-{self.site.site_id}",
            tls_context=self._tls_context(),
            keepalive=30,
            will=aiomqtt.Will(self.topic("status"), b"offline", qos=1, retain=True),
        )

    async def mqtt_loop(self) -> None:
        backoff = Backoff()
        while not self._stop.is_set():
            try:
                async with self._client() as client:
                    self._mqtt = client
                    await client.publish(self.topic("status"), b"online", qos=1, retain=True)
                    self.connected_to_mqtt = True
                    backoff.connected()
                    log.info("connected to MQTT broker", extra={"queued": len(self.outbox)})
                    reader = None
                    if self.gate is not None:
                        await client.subscribe(self.topic("command"), qos=1)
                        reader = asyncio.create_task(self._command_reader(client))
                    try:
                        await self._drain(client)
                    finally:
                        if reader is not None:
                            reader.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await reader
                    # Clean stop: the broker only sends the "last will" on a crash, so say it.
                    self.connected_to_mqtt = False
                    self._mqtt = None
                    with contextlib.suppress(Exception):
                        await client.publish(
                            self.topic("status"), b"offline", qos=1, retain=True, timeout=5
                        )
            except aiomqtt.MqttError as e:
                log.warning("MQTT connection problem", extra={"error": str(e)})
            except Exception:
                log.exception("unexpected error in MQTT loop")
            self.connected_to_mqtt = False
            self._mqtt = None
            if not self._stop.is_set():
                await self._sleep(backoff.next_delay())

    async def _drain(self, client: aiomqtt.Client) -> None:
        """Send queued messages in order; delete each only after the broker's PUBACK."""
        disk_backoff = 1.0
        while not self._stop.is_set():
            try:
                batch = self.outbox.peek(100)
                if not batch:
                    self._wake.clear()
                    if not self.outbox.peek(1):
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(self._wake.wait(), 1)
                    if not _is_connected(client):
                        raise aiomqtt.MqttError("connection to broker lost")
                    continue
                for m in batch:
                    if self._stop.is_set():
                        return
                    await client.publish(m.topic, m.payload, qos=1, timeout=30)
                    self.outbox.ack(m.id)
                disk_backoff = 1.0
            except sqlite3.Error as e:
                # Keep the MQTT connection; the cloud deduplicates any resent message.
                log.error("local queue read/delete failed", extra={"error": str(e)})
                await self._sleep(disk_backoff)
                disk_backoff = min(disk_backoff * 2, 60.0)

    # ------------------------------------------------------------ commands
    async def _command_reader(self, client: aiomqtt.Client) -> None:
        async for message in client.messages:
            if str(message.topic) != self.topic("command"):
                continue  # we never subscribed to anything else
            raw = message.payload if isinstance(message.payload, bytes | bytearray) else b""
            try:
                await self.handle_command(bytes(raw))
            except Exception:
                log.exception("command handling failed")

    async def handle_command(self, raw: bytes) -> dict[str, Any] | None:
        """Verify, run, audit and answer one command. Returns the result sent (for tests)."""
        assert self.gate is not None  # noqa: S101 - only called when commands are configured
        try:
            call = self.gate.check(raw)
        except CommandRejected as e:
            log.warning("command rejected", extra={"command_id": e.command_id, "reason": e.reason})
            self.gate.store.audit(e.command_id, None, None, False, e.reason)
            return self._result(e.command_id, False, e.reason)
        ha = self._ha
        if ha is None or not self.connected_to_ha:
            self.gate.store.audit(call.command_id, call.command_type, call.target, False, "ha_down")
            return self._result(call.command_id, False, "ha_unavailable")
        try:
            await ha.call_service(call.domain, call.service, call.entity_id, call.data)
        except Exception as e:
            log.warning(
                "Home Assistant refused the command",
                extra={"command_id": call.command_id, "error": type(e).__name__},
            )
            self.gate.store.audit(call.command_id, call.command_type, call.target, False, "ha")
            return self._result(call.command_id, False, "ha_error")
        log.info(
            "command executed",
            extra={"command_id": call.command_id, "type": call.command_type, "target": call.target},
        )
        self.gate.store.audit(call.command_id, call.command_type, call.target, True, "ok")
        return self._result(call.command_id, True, None)

    def _result(self, command_id: str | None, ok: bool, error: str | None) -> dict[str, Any] | None:
        if command_id is None:
            return None  # nothing the cloud could match it to
        now = time.monotonic()
        self._results_sent = [t for t in self._results_sent if now - t < 60]
        if len(self._results_sent) >= 60:
            return None  # a flood of junk must not flood the cloud back
        self._results_sent.append(now)
        body = {"command_id": command_id, "ok": ok, "error": error}
        self.enqueue("command_result", body)
        return body

    # ------------------------------------------------------------ timers
    async def snapshot_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep(self.cfg.snapshot_interval_s)
            if self.connected_to_ha and not self._stop.is_set():
                try:
                    self.publish_snapshot()  # only real data: no snapshot while HA is down
                except Exception:
                    log.exception("snapshot failed")
                ha = self._ha
                if ha is not None and self.connected_to_ha:
                    try:
                        self.publish_inventory(await ha.get_states())
                    except Exception:
                        log.warning("inventory skipped this time", exc_info=True)

    async def heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            client = self._mqtt
            if client is not None and self.connected_to_mqtt:
                try:
                    payload = json.dumps(self.heartbeat()).encode()
                    await client.publish(self.topic("heartbeat"), payload, qos=0)
                except aiomqtt.MqttError:
                    pass  # heartbeats are live-only; a missed one is fine
                except Exception:
                    log.exception("heartbeat failed")
            await self._sleep(self.cfg.heartbeat_interval_s)

    # ------------------------------------------------------------ lifecycle
    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def run(self) -> None:
        """Run until stop(). Raises RuntimeError if any loop dies unexpectedly."""
        tasks = {
            asyncio.create_task(self.ha_loop(), name="ha"),
            asyncio.create_task(self.mqtt_loop(), name="mqtt"),
            asyncio.create_task(self.snapshot_loop(), name="snapshot"),
            asyncio.create_task(self.heartbeat_loop(), name="heartbeat"),
        }
        stopper = asyncio.create_task(self._stop.wait(), name="stop")
        done, _ = await asyncio.wait(tasks | {stopper}, return_when=asyncio.FIRST_COMPLETED)
        # A loop that ends while no stop was requested has died (bug or unhandled error).
        died = [] if self._stop.is_set() else [t for t in done if t is not stopper]
        self.stop()
        # Give the MQTT loop a moment to publish "offline" on a clean stop.
        mqtt_task = next(t for t in tasks if t.get_name() == "mqtt")
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(mqtt_task), 6)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, stopper, return_exceptions=True)
        if died:
            names = ", ".join(t.get_name() for t in died)
            log.error(
                "a loop stopped unexpectedly, exiting so it gets restarted", extra={"loops": names}
            )
            raise RuntimeError(f"agent loop(s) died: {names}")


def _is_connected(client: aiomqtt.Client) -> bool:
    """aiomqtt has no public flag; the underlying paho client knows (aiomqtt pinned <3)."""
    paho = getattr(client, "_client", None)
    return bool(paho.is_connected()) if paho is not None else True


def _fingerprint(view: dict[str, Any]) -> tuple[Any, ...]:
    return (
        view.get("state"),
        view.get("unit"),
        tuple(sorted((view.get("attributes") or {}).items())),
    )


async def amain(cfg: AgentConfig) -> None:
    site = load_site(cfg.site_file)
    outbox = Outbox(cfg.queue_path, max_age_s=cfg.queue_max_age_s, max_bytes=cfg.queue_max_bytes)
    gate = None
    if cfg.command_secret:
        gate = CommandGate(
            site, cfg.command_secret.encode(), cfg.commands_enabled, SeenStore(cfg.commands_db)
        )
    agent = Agent(cfg, site, outbox, gate)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, agent.stop)
    allowed = [d.canonical for d in site.devices.values() if d.allow_commands]
    log.info(
        "villa agent starting",
        extra={
            "version": __version__,
            "site_id": site.site_id,
            "devices": len(site.devices),
            "commands": "enabled" if gate and gate.enabled else "disabled",
            "commandable_devices": allowed if gate and gate.enabled else [],
        },
    )
    try:
        await agent.run()
    finally:
        outbox.close()
        if gate is not None:
            gate.store.close()


def main() -> int:
    try:
        cfg = AgentConfig.from_env()
    except ConfigError as e:
        setup_logging("INFO", [])
        log.error("configuration error", extra={"error": str(e)})
        return 2
    setup_logging(cfg.log_level, cfg.secrets())
    try:
        asyncio.run(amain(cfg))
    except SiteFileError as e:
        log.error("invalid site file", extra={"problems": e.problems})
        return 2
    except RuntimeError:
        return 1
    return 0
