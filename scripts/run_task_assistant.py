"""CLI entrypoint for the interactive propose-first task assistant."""

from __future__ import annotations

import argparse
import json
import sys

from app.control_plane.runner import ControlPlane
from app.shared.config import get_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan a task first, open an approval record, and execute it only "
            "after explicit approval."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    propose = subparsers.add_parser("propose", help="Create a new approval-backed task plan.")
    propose.add_argument("task_text", help="Free-form task request for SAI.")
    propose.add_argument(
        "--context-line",
        action="append",
        default=[],
        help="Optional extra context line. May be passed multiple times.",
    )
    propose.add_argument("--requested-by", default="local-operator")
    propose.add_argument("--ask-in-slack", action="store_true")
    propose.add_argument("--slack-channel")

    approve = subparsers.add_parser("approve", help="Approve and execute a pending task plan.")
    approve.add_argument("request_id")
    approve.add_argument("--decided-by", default="local-operator")
    approve.add_argument("--reason")

    deny = subparsers.add_parser("deny", help="Deny a pending task plan.")
    deny.add_argument("request_id")
    deny.add_argument("--decided-by", default="local-operator")
    deny.add_argument("--reason")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    control_plane = ControlPlane(get_settings())
    try:
        if args.command == "propose":
            result = control_plane.propose_task_execution(
                task_text=args.task_text,
                requested_by=args.requested_by,
                context_lines=list(args.context_line),
                ask_in_slack=bool(args.ask_in_slack),
                slack_channel=args.slack_channel,
            )
        elif args.command == "approve":
            result = control_plane.decide_approval(
                request_id=args.request_id,
                approved=True,
                decided_by=args.decided_by,
                reason=args.reason,
            )
        else:
            result = control_plane.decide_approval(
                request_id=args.request_id,
                approved=False,
                decided_by=args.decided_by,
                reason=args.reason,
            )
    except Exception as error:
        print(
            f"Task assistant request failed: {error}",
            file=sys.stderr,
        )
        return 1
    finally:
        control_plane.close()

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
