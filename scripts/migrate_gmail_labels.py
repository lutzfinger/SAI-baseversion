from __future__ import annotations

import argparse
import json
import sys

from app.connectors.gmail_auth import GmailOAuthAuthenticator
from app.connectors.gmail_labels import (
    GmailLabelConnector,
    is_taxonomy_classification_label,
    taxonomy_label_rename_target,
)
from app.control_plane.loaders import PolicyStore
from app.shared.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Rename legacy SAI taxonomy labels to L1/L2 and optionally delete "
            "all other user-created Gmail labels."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually rename/delete labels. Without this flag, the script is dry-run only.",
    )
    args = parser.parse_args()

    settings = get_settings()
    policy = PolicyStore(settings.policies_dir).load("email_tagging.yaml")
    authenticator = GmailOAuthAuthenticator(settings=settings, policy=policy)
    connector = GmailLabelConnector(authenticator=authenticator)

    user_labels = connector.list_user_labels()
    labels_by_name = {label["name"]: label for label in user_labels}

    rename_plan: list[dict[str, str]] = []
    delete_plan: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []

    for label in user_labels:
        name = label["name"]
        rename_target = taxonomy_label_rename_target(name)
        if rename_target is not None:
            existing_target = labels_by_name.get(rename_target)
            if existing_target is not None and existing_target["id"] != label["id"]:
                conflicts.append(
                    {
                        "from_id": label["id"],
                        "from_name": name,
                        "target_name": rename_target,
                        "existing_target_id": existing_target["id"],
                    }
                )
            else:
                rename_plan.append(
                    {
                        "id": label["id"],
                        "from_name": name,
                        "to_name": rename_target,
                    }
                )
        elif not is_taxonomy_classification_label(name):
            delete_plan.append({"id": label["id"], "name": name})

    payload = {
        "account": authenticator.auth_summary().get("account"),
        "apply": args.apply,
        "user_label_count": len(user_labels),
        "rename_count": len(rename_plan),
        "delete_count": len(delete_plan),
        "conflict_count": len(conflicts),
        "labels_to_rename": rename_plan,
        "labels_to_delete": delete_plan,
        "conflicts": conflicts,
    }

    if conflicts:
        print(json.dumps(payload, indent=2))
        print(
            "Refusing to modify Gmail labels because rename target conflicts exist.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.apply:
        for item in rename_plan:
            connector.rename_label(label_id=item["id"], new_name=item["to_name"])
        for item in delete_plan:
            connector.delete_label(label_id=item["id"])

    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
