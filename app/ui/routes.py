"""Minimal API routes for the SAI starter repo."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.control_plane.runner import ControlPlane
from app.shared.config import Settings


class RunWorkflowRequest(BaseModel):
    source_override: str | None = None
    connector_overrides: dict[str, Any] = Field(default_factory=dict)


def build_router(settings: Settings, control_plane: ControlPlane | None = None) -> APIRouter:
    bound_control_plane = control_plane or ControlPlane(settings)
    router = APIRouter()

    @router.get("/")
    def root() -> dict[str, Any]:
        return {
            "app_name": settings.app_name,
            "status": bound_control_plane.get_status(),
            "workflows": bound_control_plane.list_workflows(),
        }

    @router.get("/api/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/api/status")
    def status() -> dict[str, Any]:
        return bound_control_plane.get_status()

    @router.get("/api/workflows")
    def workflows() -> list[dict[str, Any]]:
        return bound_control_plane.list_workflows()

    @router.post("/api/workflows/{workflow_id}/authenticate")
    def authenticate_workflow(workflow_id: str) -> dict[str, Any]:
        try:
            return bound_control_plane.authenticate_gmail(workflow_id=workflow_id)
        except Exception as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.post("/api/workflows/{workflow_id}/run")
    def run_workflow(workflow_id: str, request: RunWorkflowRequest | None = None) -> dict[str, Any]:
        try:
            result = bound_control_plane.run_workflow(
                workflow_id=workflow_id,
                source_override=request.source_override if request else None,
                connector_overrides=request.connector_overrides if request else None,
            )
        except Exception as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return result.model_dump(mode="json")

    @router.get("/api/runs")
    def runs(limit: int = 20) -> list[dict[str, Any]]:
        return bound_control_plane.list_runs(limit=limit)

    @router.get("/api/runs/{run_id}")
    def run(run_id: str) -> dict[str, Any]:
        try:
            return bound_control_plane.get_run_detail(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @router.get("/api/runs/{run_id}/events")
    def run_events(run_id: str) -> list[dict[str, Any]]:
        try:
            return bound_control_plane.get_run_events(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    return router
