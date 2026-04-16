"""FastAPI application entrypoint for local development.

This file keeps startup intentionally small: settings are loaded, runtime paths
are ensured, and the UI/API router is attached. That matches the project's
preference for boring, explicit code over hidden framework magic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI

from app.control_plane.runner import ControlPlane
from app.shared.config import Settings, get_settings
from app.ui.routes import build_router


def _build_lifespan(control_plane: ControlPlane) -> Any:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            control_plane.close()

    return lifespan


def build_app(settings: Settings | None = None) -> FastAPI:
    """Create the FastAPI app used by both tests and local execution."""

    configured_settings = settings or get_settings()
    configured_settings.ensure_runtime_paths()
    control_plane = ControlPlane(configured_settings)
    app = FastAPI(
        title=configured_settings.app_name,
        lifespan=_build_lifespan(control_plane),
    )
    app.include_router(build_router(configured_settings, control_plane=control_plane))
    return app


app = build_app()


def main() -> None:
    """Run the local control-plane API with Uvicorn."""

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
