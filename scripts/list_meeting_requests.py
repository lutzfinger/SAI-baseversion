from __future__ import annotations

import argparse
import json

from app.control_plane.runner import ControlPlane
from app.shared.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List meeting-request emails from a workflow run."
    )
    parser.add_argument(
        "run_id",
        nargs="?",
        default=None,
        help="Optional run ID. Defaults to the latest email-triage-gmail run.",
    )
    args = parser.parse_args()

    control_plane = ControlPlane(get_settings())
    run_id = args.run_id or _latest_gmail_run_id(control_plane)
    if run_id is None:
        raise SystemExit("No email-triage-gmail runs found yet.")

    detail = control_plane.get_run_detail(run_id)
    meeting_requests: list[dict[str, object]] = []
    for event in detail["events"]:
        if event["event_type"] != "worker.message.classified":
            continue
        payload = event["payload"]
        if not payload.get("meeting_request", False):
            continue
        meeting_requests.append(
            {
                "run_id": run_id,
                "message_id": payload["message_id"],
                "from_email": payload["from_email"],
                "subject": payload["subject"],
                "level1_classification": payload["level1_classification"],
                "level2_intent": payload["level2_intent"],
                "reason": payload["reason"],
            }
        )
    print(json.dumps(meeting_requests, indent=2))


def _latest_gmail_run_id(control_plane: ControlPlane) -> str | None:
    for run in control_plane.list_runs(limit=50):
        if run["workflow_id"] == "email-triage-gmail":
            return str(run["run_id"])
    return None


if __name__ == "__main__":
    main()
