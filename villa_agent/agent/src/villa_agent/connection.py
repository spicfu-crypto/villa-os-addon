"""The villa "connection code": one line that carries everything the agent needs to reach
Villa OS (broker address, MQTT login, the broker CA and the villa's command secret).

Format: ``VOS1.`` + base64url(JSON), no padding. Villa OS prints it (add-site.sh); the
Home Assistant app has ONE field for it. The code is a secret: whoever has it can send
data as this villa. Issuing a new code invalidates the old one (new MQTT password).
"""

from __future__ import annotations

import base64
import json
import re
import shlex
from dataclasses import dataclass, field

PREFIX = "VOS1."
SITE_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class ConnectionCodeError(ValueError):
    pass


@dataclass(frozen=True)
class Connection:
    site_id: str
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    ca: str = field(repr=False)
    command_secret: str = field(default="", repr=False)
    name: str = ""


def encode(c: Connection) -> str:
    data = {
        "s": c.site_id,
        "h": c.host,
        "p": c.port,
        "u": c.username,
        "pw": c.password,
        "ca": c.ca,
        "cs": c.command_secret,
        "n": c.name,
    }
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
    return PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(code: str) -> Connection:
    # People paste with spaces, line breaks or quotes around it: forgive that.
    code = re.sub(r"\s+", "", code or "").strip("'\"")
    if not code.startswith(PREFIX):
        raise ConnectionCodeError(
            "this is not a Villa OS connection code (it must start with VOS1.)"
        )
    body = code[len(PREFIX) :]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, json.JSONDecodeError):
        raise ConnectionCodeError(
            "the connection code is incomplete: copy the whole line again"
        ) from None
    if not isinstance(data, dict):
        raise ConnectionCodeError("the connection code is damaged: copy it again")
    try:
        c = Connection(
            site_id=str(data["s"]),
            host=str(data["h"]),
            port=int(data["p"]),
            username=str(data["u"]),
            password=str(data["pw"]),
            ca=str(data["ca"]),
            command_secret=str(data.get("cs") or ""),
            name=str(data.get("n") or ""),
        )
    except (KeyError, TypeError, ValueError):
        raise ConnectionCodeError("the connection code is damaged: copy it again") from None
    problems = []
    if not SITE_ID.fullmatch(c.site_id):
        problems.append("site id")
    if not c.host or any(ch.isspace() for ch in c.host):
        problems.append("server address")
    if not 1 <= c.port <= 65535:
        problems.append("port")
    if not c.username or not c.password:
        problems.append("login")
    if "BEGIN CERTIFICATE" not in c.ca:
        problems.append("certificate")
    if problems:
        raise ConnectionCodeError(
            "the connection code is damaged (" + ", ".join(problems) + "): copy it again"
        )
    return c


def shell_exports(c: Connection, ca_file: str) -> str:
    """Lines for `eval` in the app's run.sh. Values are shell-quoted."""
    env = {
        "SITE_ID": c.site_id,
        "SITE_NAME": c.name or c.site_id,
        "MQTT_HOST": c.host,
        "MQTT_PORT": str(c.port),
        "MQTT_TLS": "true",
        "MQTT_CA_FILE": ca_file,
        "MQTT_USERNAME": c.username,
        "MQTT_PASSWORD": c.password,
    }
    if c.command_secret:
        env["COMMAND_SECRET"] = c.command_secret
    return "\n".join(f"export {k}={shlex.quote(v)}" for k, v in env.items()) + "\n"
