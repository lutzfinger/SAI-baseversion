"""Install, reload, stop, and inspect always-on local SAI background services."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.control_plane.background_services import (
    all_service_statuses,
    build_background_service_specs,
    ensure_launch_agent,
    remove_retired_launch_agents,
    stop_launch_agent,
)
from app.shared.config import get_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("ensure", help="Write plists and (re)start the configured services.")
    subparsers.add_parser("status", help="Show launchctl + health status for configured services.")
    subparsers.add_parser("stop", help="Unload the configured background services.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    settings = get_settings()
    launch_agents_dir = Path.home() / "Library" / "LaunchAgents"
    specs = build_background_service_specs(settings)

    if args.command == "ensure":
        payload = {
            key: {
                "label": spec.label,
                "plist_path": str(ensure_launch_agent(spec, launch_agents_dir=launch_agents_dir)),
            }
            for key, spec in specs.items()
        }
        payload["retired"] = remove_retired_launch_agents(launch_agents_dir=launch_agents_dir)
        payload["status"] = all_service_statuses(settings)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "stop":
        payload = {
            key: {
                "label": spec.label,
                "plist_path": str(stop_launch_agent(spec, launch_agents_dir=launch_agents_dir)),
            }
            for key, spec in specs.items()
        }
        payload["retired"] = remove_retired_launch_agents(launch_agents_dir=launch_agents_dir)
        payload["status"] = all_service_statuses(settings)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    status_payload = all_service_statuses(settings)
    print(
        json.dumps(
            {
                "all_loaded": bool(status_payload)
                and all(item["loaded"] for item in status_payload.values()),
                "all_healthy": bool(status_payload)
                and all(item["healthy"] for item in status_payload.values()),
                "services": status_payload,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
