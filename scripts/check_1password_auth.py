from __future__ import annotations

import argparse
import json

from app.control_plane.onepassword_runtime import check_onepassword_auth


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail-fast 1Password CLI auth probe for SAI wrappers."
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=8,
        help="Maximum time to wait for the 1Password CLI auth probe.",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Optional 1Password env reference file to validate through `op run`.",
    )
    parser.add_argument(
        "--require-service-account",
        action="store_true",
        help="Require OP_SERVICE_ACCOUNT_TOKEN instead of allowing interactive auth.",
    )
    args = parser.parse_args()

    status = check_onepassword_auth(
        timeout_seconds=max(1, args.timeout_seconds),
        env_file=args.env_file,
        require_service_account=args.require_service_account,
    )
    print(json.dumps(status.model_dump(mode="json"), indent=2))
    return 0 if status.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
