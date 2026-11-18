"""Site mapping file: sites/<site_id>.yaml.

Maps canonical device names (the same in every villa) to this villa's Home Assistant
entity_ids. Validated against docs/site-schema.json (a copy ships inside this package).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator


class SiteFileError(Exception):
    """The site file is missing, unreadable or invalid. `problems` lists every issue."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@cache
def load_schema() -> dict[str, Any]:
    return json.loads(
        resources.files("villa_agent.schema").joinpath("site-schema.json").read_text("utf-8")
    )


@cache
def load_catalog() -> dict[str, Any]:
    return json.loads(
        resources.files("villa_agent.schema").joinpath("device-types.json").read_text("utf-8")
    )


@dataclass(frozen=True)
class DeviceType:
    name: str
    pattern: str
    domains: tuple[str, ...]
    device_classes: tuple[str, ...]
    unit: str | None
    commandable: bool

    @property
    def prefix(self) -> str | None:
        """'leak_' for 'leak_<room>', None for fixed names like 'power_main'."""
        return self.pattern.split("<")[0] if "<" in self.pattern else None

    def matches(self, canonical: str) -> bool:
        if self.prefix is None:
            return canonical == self.pattern
        rest = canonical[len(self.prefix) :]
        return canonical.startswith(self.prefix) and bool(NAME_PART.fullmatch(rest))


NAME_PART = re.compile(load_catalog()["name_part_pattern"])


@cache
def device_types() -> tuple[DeviceType, ...]:
    return tuple(
        DeviceType(
            name=name,
            pattern=d["pattern"],
            domains=tuple(d["domains"]),
            device_classes=tuple(d["device_classes"]),
            unit=d["unit"],
            commandable=d["commandable"],
        )
        for name, d in load_catalog()["types"].items()
    )


def type_of(canonical: str) -> DeviceType | None:
    """Return the device type for a canonical name, or None if it fits no type."""
    for t in device_types():
        if t.matches(canonical):
            return t
    return None


@dataclass(frozen=True)
class Device:
    canonical: str
    entity_id: str
    type: DeviceType
    critical: bool = False
    allow_commands: bool = False

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]


@dataclass(frozen=True)
class Site:
    site_id: str
    display_name: str
    timezone: str
    devices: dict[str, Device]  # canonical name -> device

    def by_entity_id(self) -> dict[str, Device]:
        return {d.entity_id: d for d in self.devices.values()}


def _friendly_schema_error(err: Any) -> str:
    path = ".".join(str(p) for p in err.absolute_path)
    if err.validator == "additionalProperties" and list(err.absolute_path) == ["devices"]:
        bad = sorted(set(err.instance) - _known_keys(err.instance))
        return f"devices: unknown canonical name(s) {', '.join(bad)}. Allowed: " + ", ".join(
            t.pattern for t in device_types()
        )
    if err.validator == "pattern" and path.endswith(".entity_id"):
        canonical = path.split(".")[1]
        t = type_of(canonical)
        if t and not any(str(err.instance).startswith(d + ".") for d in t.domains):
            return (
                f"{canonical}: entity '{err.instance}' has the wrong domain; "
                f"{t.pattern} needs {' or '.join(t.domains)}"
            )
        return f"{path}: '{err.instance}' is not a valid Home Assistant entity_id"
    if err.validator == "const" and path.endswith(".allow_commands"):
        return f"{path.split('.')[1]}: sensors cannot have allow_commands: true"
    return f"{path or '(file)'}: {err.message}"


def _known_keys(devices: dict[str, Any]) -> set[str]:
    return {k for k in devices if type_of(k) is not None}


def validate_data(data: Any, *, expected_site_id: str | None = None) -> list[str]:
    """Return a list of human-readable problems (empty list = valid)."""
    validator = Draft202012Validator(load_schema())
    problems = sorted(
        {_friendly_schema_error(e) for e in validator.iter_errors(data)},
    )
    if not problems and isinstance(data, dict):
        seen: dict[str, str] = {}
        for canonical, dev in data["devices"].items():
            eid = dev["entity_id"]
            if eid in seen:
                problems.append(f"{canonical}: entity '{eid}' is already mapped to {seen[eid]}")
            seen[eid] = canonical
        if expected_site_id and data["site_id"] != expected_site_id:
            problems.append(
                f"site_id '{data['site_id']}' does not match the file name '{expected_site_id}'"
            )
    return problems


def parse_site(data: dict[str, Any]) -> Site:
    devices: dict[str, Device] = {}
    for canonical, d in data["devices"].items():
        t = type_of(canonical)
        if t is None:  # cannot happen after schema validation, but never trust input
            raise SiteFileError([f"{canonical}: unknown canonical name"])
        devices[canonical] = Device(
            canonical=canonical,
            entity_id=d["entity_id"],
            type=t,
            critical=bool(d.get("critical", False)),
            allow_commands=bool(d.get("allow_commands", False)),
        )
    return Site(
        site_id=data["site_id"],
        display_name=data["display_name"],
        timezone=data["timezone"],
        devices=devices,
    )


def load_site(path: str | Path) -> Site:
    """Load and validate a site file. Raises SiteFileError with every problem found."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text("utf-8"))
    except FileNotFoundError:
        raise SiteFileError([f"file not found: {path}"]) from None
    except yaml.YAMLError as e:
        raise SiteFileError([f"not valid YAML: {e}"]) from None
    stem = path.name.removesuffix(".yaml").removesuffix(".draft")
    problems = validate_data(data, expected_site_id=stem)
    if problems:
        raise SiteFileError(problems)
    return parse_site(data)
