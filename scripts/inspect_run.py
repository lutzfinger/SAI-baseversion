from __future__ import annotations

import argparse
import json

from app.control_plane.runner import ControlPlane
from app.shared.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect a recorded workflow run.")
    parser.add_argument("run_id")
    args = parser.parse_args()

    control_plane = ControlPlane(get_settings())
    detail = control_plane.get_run_detail(args.run_id)
    print(json.dumps(detail, indent=2))


if __name__ == "__main__":
    main()
