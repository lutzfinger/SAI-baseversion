from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.control_plane.onepassword_notifier import post_onepassword_access_notice
from app.shared.config import get_settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Post a best-effort Slack notice before SAI requests 1Password access."
    )
    parser.add_argument(
        "--cwd",
        default=os.getcwd(),
        help="Working directory of the process that is about to request 1Password access.",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command about to be executed via op run.",
    )
    args = parser.parse_args()

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]

    settings = get_settings()
    result = post_onepassword_access_notice(
        settings=settings,
        command=command,
        cwd=Path(args.cwd).expanduser().resolve(),
    )
    payload = {
        "posted": result is not None,
        "channel": settings.slack_onepassword_channel,
        "result": result,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
