from __future__ import annotations

import argparse
from pathlib import Path

from app.control_plane.schedule_gate import mark_slot_completed, select_due_slot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Track once-per-slot scheduled job completion.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    due = subparsers.add_parser("due", help="Return the next due slot key, if any.")
    due.add_argument("--state-file", type=Path, required=True)
    due.add_argument("--slot", action="append", required=True, dest="slots")

    mark = subparsers.add_parser("mark", help="Mark one slot key as completed.")
    mark.add_argument("--state-file", type=Path, required=True)
    mark.add_argument("--slot-key", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "due":
        decision = select_due_slot(state_path=args.state_file, slots=args.slots)
        if not decision.due or decision.slot_key is None:
            return 10
        print(decision.slot_key)
        return 0

    mark_slot_completed(state_path=args.state_file, slot_key=args.slot_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
