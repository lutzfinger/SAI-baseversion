from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from uuid import uuid4

from langsmith import Client, traceable

from app.observability.langsmith import LangSmithTraceManager
from app.shared.config import get_settings


@traceable(name="langsmith_smoke_step", run_type="tool")
def _langsmith_smoke_step(payload: dict[str, str]) -> dict[str, str]:
    """Emit one tiny traceable step so LangSmith has something to record."""

    return {"status": "ok", "message": payload["message"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the LangSmith integration for this repo.")
    parser.add_argument(
        "--smoke-run",
        action="store_true",
        help="Create one tiny trace in the configured LangSmith project.",
    )
    args = parser.parse_args()

    settings = get_settings()
    manager = LangSmithTraceManager(settings)

    result: dict[str, object] = {
        "langsmith_enabled": settings.langsmith_enabled,
        "has_api_key": bool(settings.langsmith_api_key),
        "project": settings.langsmith_project,
        "endpoint": settings.langsmith_endpoint or "https://api.smith.langchain.com",
        "manager_enabled": manager.is_enabled(),
        "api_probe": "not_run",
        "smoke_run": "not_run",
    }

    if not manager.is_enabled():
        print(json.dumps(result, indent=2))
        return 1

    client = Client(
        api_url=settings.langsmith_endpoint,
        api_key=settings.langsmith_api_key,
    )

    try:
        next(client.list_projects(name=settings.langsmith_project, limit=1), None)
        result["api_probe"] = "ok"
    except Exception as exc:  # pragma: no cover - exercised in real envs
        result["api_probe"] = f"failed: {exc}"
        print(json.dumps(result, indent=2))
        return 1

    if args.smoke_run:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        smoke_run_id = f"langsmith_smoke_{timestamp}_{uuid4().hex[:8]}"
        try:
            with manager.workflow_trace(
                workflow_id="langsmith-smoke",
                run_id=smoke_run_id,
                redaction_config={},
                extra_metadata={"kind": "diagnostic_smoke_run"},
            ):
                smoke_result = _langsmith_smoke_step({"message": smoke_run_id})
            result["smoke_run"] = "ok"
            result["smoke_run_id"] = smoke_run_id
            result["smoke_result"] = smoke_result
        except Exception as exc:  # pragma: no cover - exercised in real envs
            result["smoke_run"] = f"failed: {exc}"
            print(json.dumps(result, indent=2))
            return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
