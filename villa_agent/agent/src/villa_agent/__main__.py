"""Command-line entry point: `python -m villa_agent <command>`.

Commands:
  discover   read Home Assistant (read-only) and write sites/<site_id>.draft.yaml
  validate   check a site file against the schema and, optionally, the live Home Assistant
  run        run the agent (Prompt 2)

The Home Assistant token is ALWAYS read from the HA_TOKEN environment variable, never
from the command line (it would end up in your shell history) and never from YAML.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from villa_agent import __version__


def _token() -> str:
    token = os.environ.get("HA_TOKEN", "").strip()
    if not token:
        print(
            "HA_TOKEN is not set. Put the villa-agent long-lived token in the HA_TOKEN "
            "environment variable (never in a file in Git).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return token


async def _fetch_states(ha_url: str, token: str) -> list[dict]:
    from villa_agent.ha_ws import HAClient

    async with HAClient(ha_url, token) as ha:
        return await ha.get_states()


def cmd_discover(args: argparse.Namespace) -> int:
    from villa_agent.discover import entities_from_states, render_draft, suggest_mapping

    states = asyncio.run(_fetch_states(args.ha_url, _token()))
    entities = entities_from_states(states)
    mapping, unmatched = suggest_mapping(entities)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{args.site_id}.draft.yaml"
    out.write_text(
        render_draft(args.site_id, args.display_name or args.site_id, mapping, unmatched), "utf-8"
    )
    print(f"Found {len(entities)} entities, suggested {len(mapping)} devices.")
    print(f"Wrote {out}. Review it, then rename it to {args.site_id}.yaml")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from villa_agent.site import SiteFileError, load_site

    try:
        site = load_site(args.site_file)
    except SiteFileError as e:
        print(f"INVALID {args.site_file}:")
        for p in e.problems:
            print(f"  ✗ {p}")
        return 1
    print(f"Schema OK: {site.site_id}, {len(site.devices)} devices.")
    if not args.ha_url:
        print("(Add --ha-url to also check against your live Home Assistant.)")
        return 0

    from villa_agent.discover import check_against_ha, entities_from_states

    entities = entities_from_states(asyncio.run(_fetch_states(args.ha_url, _token())))
    errors, warnings = check_against_ha(site, entities)
    for w in warnings:
        print(f"  ! {w}")
    for err in errors:
        print(f"  ✗ {err}")
    if errors:
        print(f"INVALID: {len(errors)} problem(s) against Home Assistant.")
        return 1
    print("Live check OK: every mapped entity exists and has the expected domain.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from villa_agent.agent import main as run_main

    return run_main()


def cmd_autosite(args: argparse.Namespace) -> int:
    """Write a ready site file from Home Assistant (used by the HA app on start)."""
    import yaml

    from villa_agent.discover import auto_site, entities_from_states

    ha_url = os.environ.get("HA_URL", "").strip()
    if not ha_url:
        print("HA_URL is not set.", file=sys.stderr)
        return 2
    entities = entities_from_states(asyncio.run(_fetch_states(ha_url, _token())))
    data = auto_site(args.site_id, args.display_name or args.site_id, entities)
    if not data["devices"]:
        print(
            "No supported devices found in Home Assistant (temperature, humidity, doors, "
            "leaks, smoke, lights, plugs, air conditioners...).",
            file=sys.stderr,
        )
        return 3
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# Made automatically from Home Assistant for {args.site_id}.\n"
        "# To choose the devices yourself, copy this file to /config/villa_os/"
        f"{args.site_id}.yaml, edit it, and restart the app.\n"
    )
    out.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), "utf-8")
    counts: dict[str, int] = {}
    for canonical in data["devices"]:
        kind = canonical.split("_", 1)[0]
        counts[kind] = counts.get(kind, 0) + 1
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    print(f"Found {len(data['devices'])} devices: {summary}")
    return 0


def cmd_connection_env(args: argparse.Namespace) -> int:
    """Read a connection code on stdin, write the CA file, print shell exports."""
    from villa_agent.connection import ConnectionCodeError, decode, shell_exports

    try:
        c = decode(sys.stdin.read())
    except ConnectionCodeError as e:
        print(f"Connection code problem: {e}", file=sys.stderr)
        return 2
    Path(args.ca_out).write_text(c.ca.strip() + "\n", "utf-8")
    sys.stdout.write(shell_exports(c, args.ca_out))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="villa_agent", description="Villa OS agent")
    parser.add_argument("--version", action="version", version=f"villa_agent {__version__}")
    sub = parser.add_subparsers(dest="command")

    d = sub.add_parser("discover", help="suggest a site mapping from Home Assistant (read-only)")
    d.add_argument("--ha-url", required=True, help="e.g. http://homeassistant.local:8123")
    d.add_argument("--site-id", required=True, help="e.g. villa-0")
    d.add_argument("--display-name", help="human name of the property")
    d.add_argument("--out-dir", default="sites", help="where to write the draft (default: sites)")
    d.set_defaults(func=cmd_discover)

    v = sub.add_parser("validate", help="check a site file (schema + optional live HA check)")
    v.add_argument("site_file", help="e.g. sites/villa-0.yaml")
    v.add_argument("--ha-url", help="also check against this live Home Assistant")
    v.set_defaults(func=cmd_validate)

    a = sub.add_parser("autosite", help="write a ready site file from Home Assistant")
    a.add_argument("--site-id", required=True)
    a.add_argument("--display-name")
    a.add_argument("--out", required=True)
    a.set_defaults(func=cmd_autosite)

    ce = sub.add_parser("connection-env", help="decode a connection code (stdin) for run.sh")
    ce.add_argument("--ca-out", required=True)
    ce.set_defaults(func=cmd_connection_env)

    r = sub.add_parser("run", help="run the agent (configuration from environment variables)")
    r.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except OSError as e:
        print(f"Could not reach Home Assistant: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # last resort: clear message, no stack trace with secrets
        from villa_agent.ha_ws import HAAuthError

        if isinstance(e, HAAuthError):
            print("Home Assistant rejected the token in HA_TOKEN.", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
