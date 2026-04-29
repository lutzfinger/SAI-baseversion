"""Shared LangGraph runtime resources for workflow workers.

The control plane stays responsible for run lifecycle, approvals, and audit
events. This module adds a reusable LangGraph execution substrate underneath
that layer: a SQLite-backed checkpointer plus standard runnable config helpers.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from app.shared.config import Settings


class GraphRuntimeManager:
    """Own the shared LangGraph checkpointer used by graph-backed workers."""

    runtime_name = "langgraph_stategraph"
    runnable_runtime = "langchain_runnables"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._connection = sqlite3.connect(
            settings.graph_checkpoint_path,
            check_same_thread=False,
        )
        self.checkpointer = SqliteSaver(self._connection)
        self.checkpointer.setup()

    def runtime_metadata(self) -> dict[str, Any]:
        """Return stable runtime metadata for artifacts, audit, and UI."""

        return {
            "workflow_engine": self.runtime_name,
            "node_runtime": self.runnable_runtime,
            "checkpoint_backend": "sqlite",
            "checkpoint_path": str(self.settings.graph_checkpoint_path),
        }

    def runnable_config(
        self,
        *,
        run_id: str,
        workflow_id: str,
        thread_suffix: str,
    ) -> dict[str, Any]:
        """Build the standard per-run config passed into LangChain runnables."""

        return {
            "run_name": f"{workflow_id}:{thread_suffix}",
            "tags": [workflow_id, self.runtime_name, self.settings.environment],
            "metadata": {
                "run_id": run_id,
                "workflow_id": workflow_id,
                "thread_suffix": thread_suffix,
                "environment": self.settings.environment,
            },
            "configurable": {
                "thread_id": f"{run_id}:{thread_suffix}",
            },
        }

    def close(self) -> None:
        """Close the checkpoint connection when a long-lived process exits."""

        self._connection.close()
