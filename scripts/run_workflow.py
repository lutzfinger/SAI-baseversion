from __future__ import annotations

import argparse
import json

from app.control_plane.runner import ControlPlane
from app.shared.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one starter workflow.")
    parser.add_argument(
        "--workflow-id",
        default="newsletter-identification-gmail",
        help="Workflow to run.",
    )
    parser.add_argument(
        "--query",
        default=None,
        help="Optional Gmail query override.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=None,
        help="Optional Gmail max-results override.",
    )
    args = parser.parse_args()

    connector_overrides: dict[str, object] = {}
    if args.query is not None:
        connector_overrides["query"] = args.query or None
    if args.max_results is not None:
        connector_overrides["max_results"] = args.max_results

    control_plane = ControlPlane(get_settings())
    try:
        result = control_plane.run_workflow(
            workflow_id=args.workflow_id,
            connector_overrides=connector_overrides or None,
        )
        print(json.dumps(result.model_dump(mode="json"), indent=2))
    finally:
        control_plane.close()


if __name__ == "__main__":
    main()
