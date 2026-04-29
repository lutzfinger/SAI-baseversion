from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.connectors.calendar_auth import CalendarOAuthAuthenticator
from app.control_plane.loaders import PolicyStore, WorkflowStore
from app.shared.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Authenticate the Calendar portion of a Google-backed SAI workflow."
    )
    parser.add_argument("--workflow-id", default="meeting-triage-daily")
    parser.add_argument("--calendar-token-path", default=None)
    args = parser.parse_args()

    settings = get_settings()
    workflow = WorkflowStore(settings.workflows_dir).load(f"{args.workflow_id}.yaml")
    policy = PolicyStore(settings.policies_dir).load(workflow.policy)

    if args.calendar_token_path:
        os.environ["SAI_MEETING_CALENDAR_TOKEN_PATH"] = str(
            Path(args.calendar_token_path).expanduser()
        )

    authenticator = CalendarOAuthAuthenticator(settings=settings, policy=policy)
    token_path = authenticator.authenticate_interactively()
    print(
        json.dumps(
            {
                "workflow_id": workflow.workflow_id,
                "calendar_token_path": str(token_path),
                "calendar_scopes": authenticator.calendar_policy.allowed_scopes,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
