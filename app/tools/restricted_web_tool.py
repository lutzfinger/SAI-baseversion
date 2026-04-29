"""Strict wrapper around the restricted web connector."""

from __future__ import annotations

from app.connectors.restricted_web import RestrictedWebConnector
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.web_models import RestrictedWebRequest, RestrictedWebResult


class RestrictedWebTool:
    """Read pages and submit simple forms under a tight allowlist policy."""

    def __init__(self, *, tool_id: str, connector: RestrictedWebConnector) -> None:
        self.tool_id = tool_id
        self.connector = connector

    def execute(
        self,
        *,
        request: RestrictedWebRequest,
    ) -> tuple[RestrictedWebResult, ToolExecutionRecord]:
        result = self.connector.perform(request)
        record = ToolExecutionRecord(
            tool_id=self.tool_id,
            tool_kind="restricted_web_operator",
            status=ToolExecutionStatus.COMPLETED,
            details={
                "action": result.action,
                "requested_url": result.requested_url,
                "final_url": result.final_url,
                "status_code": result.status_code,
                "content_type": result.content_type,
                "submitted_field_names": list(result.submitted_field_names),
                "redirect_count": result.redirect_count,
                "truncated": result.truncated,
            },
        )
        return result, record
