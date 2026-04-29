from __future__ import annotations

import json

from app.control_plane.runner import ControlPlane
from app.shared.config import get_settings


def main() -> int:
    settings = get_settings()
    control_plane = ControlPlane(settings)
    try:
        result = control_plane.send_cornell_ta_registry_reminder()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("status") != "failed" else 1
    finally:
        control_plane.close()


if __name__ == "__main__":
    raise SystemExit(main())
