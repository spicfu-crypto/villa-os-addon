"""Agent configuration, read only from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(Exception):
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _num(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True)
class AgentConfig:
    site_file: str
    ha_url: str
    ha_token: str = field(repr=False)
    mqtt_host: str
    mqtt_port: int
    mqtt_tls: bool
    mqtt_ca_file: str | None
    mqtt_cert_file: str | None
    mqtt_key_file: str | None
    mqtt_username: str | None
    mqtt_password: str | None = field(repr=False)
    queue_path: str
    queue_max_age_s: float
    queue_max_bytes: int
    snapshot_interval_s: float
    heartbeat_interval_s: float
    log_level: str
    # Commands (Prompt 6). Off unless BOTH are set: the kill switch and the villa's secret.
    commands_enabled: bool = False
    command_secret: str | None = field(default=None, repr=False)
    commands_db: str = "/data/commands.sqlite"

    def secrets(self) -> list[str]:
        """Values that must never appear in logs."""
        return [s for s in (self.ha_token, self.mqtt_password, self.command_secret) if s]

    @classmethod
    def from_env(cls) -> AgentConfig:
        missing = [
            n
            for n in ("SITE_FILE", "HA_URL", "HA_TOKEN", "MQTT_HOST")
            if not os.environ.get(n, "").strip()
        ]
        if missing:
            raise ConfigError("missing environment variable(s): " + ", ".join(missing))
        tls = _bool("MQTT_TLS", False)
        cert, key = (
            os.environ.get("MQTT_CERT_FILE") or None,
            os.environ.get("MQTT_KEY_FILE") or None,
        )
        if bool(cert) != bool(key):
            raise ConfigError("MQTT_CERT_FILE and MQTT_KEY_FILE must be set together")
        if cert and not tls:
            raise ConfigError("client certificates need MQTT_TLS=true")
        if os.environ.get("MQTT_PASSWORD") and not tls:
            raise ConfigError("MQTT_PASSWORD without MQTT_TLS=true would send it in plain text")
        for name in ("MQTT_CA_FILE", "MQTT_CERT_FILE", "MQTT_KEY_FILE"):
            path = os.environ.get(name)
            if path and not os.path.isfile(path):
                raise ConfigError(f"{name} points to a missing file: {path}")
        secret = os.environ.get("COMMAND_SECRET", "").strip() or None
        if secret is not None and len(secret) < 32:
            raise ConfigError("COMMAND_SECRET must be at least 32 characters (use make add-site)")
        commands_enabled = _bool("COMMANDS_ENABLED", False)
        if commands_enabled and secret is None:
            raise ConfigError("COMMANDS_ENABLED=true needs COMMAND_SECRET")
        queue_path = os.environ.get("QUEUE_PATH", "/data/queue.sqlite")
        from villa_agent.ha_ws import websocket_url

        try:
            websocket_url(os.environ["HA_URL"].strip())
        except ValueError as e:
            raise ConfigError(f"HA_URL: {e}") from None
        return cls(
            site_file=os.environ["SITE_FILE"].strip(),
            ha_url=os.environ["HA_URL"].strip(),
            ha_token=os.environ["HA_TOKEN"].strip(),
            mqtt_host=os.environ["MQTT_HOST"].strip(),
            mqtt_port=int(_num("MQTT_PORT", 8883 if tls else 1883, 1)),
            mqtt_tls=tls,
            mqtt_ca_file=os.environ.get("MQTT_CA_FILE") or None,
            mqtt_cert_file=cert,
            mqtt_key_file=key,
            mqtt_username=os.environ.get("MQTT_USERNAME") or None,
            mqtt_password=os.environ.get("MQTT_PASSWORD") or None,
            queue_path=queue_path,
            queue_max_age_s=_num("QUEUE_MAX_AGE_DAYS", 7, 0.0001) * 86400,
            queue_max_bytes=int(_num("QUEUE_MAX_MB", 50, 0.001) * 1024 * 1024),
            snapshot_interval_s=_num("SNAPSHOT_INTERVAL_S", 300, 0.05),
            heartbeat_interval_s=_num("HEARTBEAT_INTERVAL_S", 60, 0.05),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            commands_enabled=commands_enabled,
            command_secret=secret,
            commands_db=os.environ.get("COMMANDS_DB")
            or os.path.join(os.path.dirname(queue_path) or ".", "commands.sqlite"),
        )
