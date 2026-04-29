"""Read daily provider costs for OpenAI and Gemini/Google Cloud."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, cast
from urllib import parse, request

from google.oauth2 import service_account
from googleapiclient.discovery import build  # type: ignore[import-untyped]

from app.shared.config import Settings
from app.shared.models import WorkflowToolDefinition
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.daily_cost_report_models import DailyProviderCost, ProviderCostLineItem


class OpenAIDailyCostReaderTool:
    """Read one day of OpenAI costs from the organization cost endpoint."""

    def __init__(self, *, tool_definition: WorkflowToolDefinition, settings: Settings) -> None:
        self.tool_definition = tool_definition
        self.settings = settings
        self.timeout_seconds = _resolve_timeout_seconds(tool_definition, default=30)

    def read_cost(
        self,
        *,
        started_at: datetime,
        ended_at: datetime,
    ) -> tuple[DailyProviderCost, ToolExecutionRecord]:
        api_key = (self.settings.openai_admin_api_key or "").strip()
        if not api_key:
            raise RuntimeError(
                "OpenAI daily cost reporting requires SAI_OPENAI_ADMIN_API_KEY "
                "or OPENAI_ADMIN_API_KEY."
            )

        endpoint = (self.settings.openai_org_costs_url or "").strip()
        if not endpoint:
            raise RuntimeError("OpenAI costs endpoint is not configured.")
        query = parse.urlencode(
            {
                "start_time": int(started_at.timestamp()),
                "bucket_width": "1d",
                "limit": 1,
            }
        )
        req = request.Request(
            f"{endpoint}?{query}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="GET",
        )
        with request.urlopen(req, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("OpenAI cost API returned a non-object response.")
        bucket = _first_bucket(payload)
        if bucket is None:
            raise RuntimeError("OpenAI cost API returned no buckets for the requested day.")
        line_items = _extract_openai_line_items(bucket)
        total_usd = _round_currency(sum(item.amount_usd for item in line_items))
        cost = DailyProviderCost(
            provider="openai",
            status="actual",
            source="openai_cost_api",
            started_at=started_at,
            ended_at=ended_at,
            amount_usd=total_usd,
            currency="USD",
            line_items=line_items,
        )
        record = ToolExecutionRecord(
            tool_id=self.tool_definition.tool_id,
            tool_kind=self.tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details={
                "endpoint": endpoint,
                "bucket_count": len(cast(list[Any], payload.get("data", [])))
                if isinstance(payload.get("data"), list)
                else 0,
                "line_item_count": len(line_items),
                "amount_usd": total_usd,
            },
        )
        return cost, record


class GeminiDailyCostReaderTool:
    """Read one day of Gemini cost from a configured Cloud Billing BigQuery export."""

    def __init__(self, *, tool_definition: WorkflowToolDefinition, settings: Settings) -> None:
        self.tool_definition = tool_definition
        self.settings = settings
        self.timeout_seconds = _resolve_timeout_seconds(tool_definition, default=30)

    def read_cost(
        self,
        *,
        started_at: datetime,
        ended_at: datetime,
    ) -> tuple[DailyProviderCost, ToolExecutionRecord]:
        project_id = (self.settings.gcp_billing_project_id or "").strip()
        dataset = (self.settings.gcp_billing_dataset or "").strip()
        table = (self.settings.gcp_billing_table or "").strip()
        if not project_id or not dataset or not table:
            raise RuntimeError(
                "Gemini daily cost reporting requires SAI_GCP_BILLING_PROJECT_ID, "
                "SAI_GCP_BILLING_DATASET, and SAI_GCP_BILLING_TABLE."
            )
        credentials = _load_google_credentials(self.settings)
        service_descriptions = _service_descriptions(self.tool_definition)
        query = (
            "SELECT service.description AS service_description, "
            "ROUND(SUM(cost), 6) AS gross_cost_usd, "
            "ANY_VALUE(currency) AS currency "
            f"FROM `{project_id}.{dataset}.{table}` "
            "WHERE usage_start_time >= @start_time "
            "AND usage_start_time < @end_time "
            "AND service.description IN UNNEST(@service_descriptions) "
            "GROUP BY service_description "
            "ORDER BY gross_cost_usd DESC"
        )
        service = build("bigquery", "v2", credentials=credentials, cache_discovery=False)
        response = cast(
            dict[str, Any],
            service.jobs()
            .query(
                projectId=project_id,
                body={
                    "query": query,
                    "useLegacySql": False,
                    "timeoutMs": self.timeout_seconds * 1000,
                    "parameterMode": "NAMED",
                    "queryParameters": [
                        {
                            "name": "start_time",
                            "parameterType": {"type": "TIMESTAMP"},
                            "parameterValue": {"value": started_at.isoformat()},
                        },
                        {
                            "name": "end_time",
                            "parameterType": {"type": "TIMESTAMP"},
                            "parameterValue": {"value": ended_at.isoformat()},
                        },
                        {
                            "name": "service_descriptions",
                            "parameterType": {
                                "type": "ARRAY",
                                "arrayType": {"type": "STRING"},
                            },
                            "parameterValue": {
                                "arrayValues": [
                                    {"value": description}
                                    for description in service_descriptions
                                ]
                            },
                        },
                    ],
                },
            )
            .execute()
        )
        line_items = _extract_bigquery_line_items(response)
        total_usd = _round_currency(sum(item.amount_usd for item in line_items))
        cost = DailyProviderCost(
            provider="gemini",
            status="actual",
            source="google_cloud_billing_export",
            started_at=started_at,
            ended_at=ended_at,
            amount_usd=total_usd,
            currency=(line_items[0].currency if line_items else "USD"),
            line_items=line_items,
            note="Gross usage cost from Cloud Billing export.",
        )
        record = ToolExecutionRecord(
            tool_id=self.tool_definition.tool_id,
            tool_kind=self.tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details={
                "billing_table": f"{project_id}.{dataset}.{table}",
                "service_descriptions": service_descriptions,
                "line_item_count": len(line_items),
                "amount_usd": total_usd,
            },
        )
        return cost, record


def _resolve_timeout_seconds(tool_definition: WorkflowToolDefinition, *, default: int) -> int:
    raw_value = tool_definition.config.get("timeout_seconds", default)
    try:
        timeout_seconds = int(raw_value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Tool {tool_definition.tool_id} timeout_seconds must be an integer."
        ) from error
    if timeout_seconds <= 0:
        raise ValueError(f"Tool {tool_definition.tool_id} timeout_seconds must be positive.")
    return timeout_seconds


def _first_bucket(payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    return first if isinstance(first, dict) else None


def _extract_openai_line_items(bucket: dict[str, Any]) -> list[ProviderCostLineItem]:
    results = bucket.get("results")
    if not isinstance(results, list):
        return []
    line_items: list[ProviderCostLineItem] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        amount_payload = item.get("amount")
        if not isinstance(amount_payload, dict):
            continue
        value = amount_payload.get("value")
        if value is None:
            continue
        currency = str(amount_payload.get("currency", "usd")).upper()
        line_item = str(item.get("line_item", "OpenAI")).strip() or "OpenAI"
        line_items.append(
            ProviderCostLineItem(
                label=line_item,
                amount_usd=_round_currency(float(value)),
                currency=currency,
            )
        )
    return line_items


def _load_google_credentials(settings: Settings) -> service_account.Credentials:
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    raw_service_account_json = (settings.gcp_billing_service_account_json or "").strip()
    if raw_service_account_json:
        try:
            service_account_info = json.loads(raw_service_account_json)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "Gemini daily cost reporting received invalid "
                "SAI_GCP_BILLING_SERVICE_ACCOUNT_JSON."
            ) from error
        if not isinstance(service_account_info, dict):
            raise RuntimeError(
                "Gemini daily cost reporting requires "
                "SAI_GCP_BILLING_SERVICE_ACCOUNT_JSON to contain a JSON object."
            )
        return cast(
            service_account.Credentials,
            service_account.Credentials.from_service_account_info(
                service_account_info,
                scopes=scopes,
            ),  # type: ignore[no-untyped-call]
        )
    if settings.gcp_billing_service_account_path is not None:
        path = Path(settings.gcp_billing_service_account_path).expanduser()
        return cast(
            service_account.Credentials,
            service_account.Credentials.from_service_account_file(
                str(path),
                scopes=scopes,
            ),  # type: ignore[no-untyped-call]
        )
    raise RuntimeError(
        "Gemini daily cost reporting requires "
        "SAI_GCP_BILLING_SERVICE_ACCOUNT_JSON, "
        "SAI_GCP_BILLING_SERVICE_ACCOUNT_PATH, "
        "or GOOGLE_APPLICATION_CREDENTIALS."
    )


def _service_descriptions(tool_definition: WorkflowToolDefinition) -> list[str]:
    raw_value = tool_definition.config.get("service_descriptions", ["Gemini API"])
    if not isinstance(raw_value, list):
        raise ValueError(
            f"Tool {tool_definition.tool_id} service_descriptions must be a list."
        )
    descriptions = [str(value).strip() for value in raw_value if str(value).strip()]
    if not descriptions:
        raise ValueError(
            f"Tool {tool_definition.tool_id} service_descriptions must not be empty."
        )
    return descriptions


def _extract_bigquery_line_items(payload: dict[str, Any]) -> list[ProviderCostLineItem]:
    schema = payload.get("schema", {})
    rows = payload.get("rows", [])
    if not isinstance(schema, dict) or not isinstance(rows, list):
        return []
    fields = schema.get("fields", [])
    if not isinstance(fields, list):
        return []
    field_names = [
        str(field.get("name", "")).strip()
        for field in fields
        if isinstance(field, dict)
    ]
    line_items: list[ProviderCostLineItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        values = row.get("f", [])
        if not isinstance(values, list):
            continue
        parsed = {
            name: _bigquery_cell_value(values[index])
            for index, name in enumerate(field_names)
            if index < len(values)
        }
        amount_value = parsed.get("gross_cost_usd")
        if amount_value is None:
            continue
        line_items.append(
            ProviderCostLineItem(
                label=str(parsed.get("service_description", "Gemini API")),
                amount_usd=_round_currency(float(amount_value)),
                currency=str(parsed.get("currency", "USD")).upper(),
            )
        )
    return line_items


def _bigquery_cell_value(cell: Any) -> Any:
    if not isinstance(cell, dict):
        return None
    value = cell.get("v")
    return value


def _round_currency(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))
