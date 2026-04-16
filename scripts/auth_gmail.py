from __future__ import annotations

import argparse
import json

from app.control_plane.runner import ControlPlane
from app.shared.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Gmail OAuth flow for one workflow."
    )
    parser.add_argument(
        "--workflow-id",
        default="newsletter-identification-gmail",
        help="Workflow to authenticate.",
    )
    args = parser.parse_args()

    control_plane = ControlPlane(get_settings())
    try:
        result = control_plane.authenticate_gmail(workflow_id=args.workflow_id)
        print(json.dumps(result, indent=2))
    finally:
        control_plane.close()


if __name__ == "__main__":
    main()
