"""DAGWorkflow: Multi-layer DAG execution via Temporal child workflows.

Layers execute sequentially; nodes within each layer execute in parallel
using Temporal child workflows (not asyncio.gather).
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DAGNode:
    """A single node in the DAG."""
    id: str
    agent_type: str = "build"
    model_provider: str = ""
    model_id: str = ""
    prompt: str = ""
    upstream_ids: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


@dataclass
class DAGLayer:
    """A layer of nodes that can execute in parallel."""
    nodes: list[DAGNode] = field(default_factory=list)


@dataclass
class DAGParams:
    """Input parameters for DAGWorkflow."""
    run_id: str
    layers: list[DAGLayer] = field(default_factory=list)
    global_config: dict = field(default_factory=dict)


@dataclass
class DAGResult:
    """Output of DAGWorkflow."""
    run_id: str
    results: dict[str, Any] = field(default_factory=dict)
    status: str = "completed"


# ---------------------------------------------------------------------------
# DAGWorkflow
# ---------------------------------------------------------------------------

# Import at workflow-definition time (safe: only used for type hints / routing)
with workflow.unsafe.imports_passed_through():
    from app.workflows.activities import AgentNodeWorkflow


@workflow.defn
class DAGWorkflow:
    """Multi-layer DAG execution: layers run sequentially, nodes within each
    layer run in parallel as Temporal child workflows."""

    @workflow.run
    async def run(self, params: DAGParams) -> DAGResult:
        layer_results: dict[str, Any] = {}

        for layer_idx, layer in enumerate(params.layers):
            logger.info(
                "DAGWorkflow run=%s starting layer %d with %d nodes",
                params.run_id, layer_idx, len(layer.nodes),
            )

            # Start all child workflows in parallel within this layer
            child_handles: list[tuple[str, Any]] = []
            for node in layer.nodes:
                node_input = self._inject_upstream(node, layer_results, params)
                handle = await workflow.start_child_workflow(
                    AgentNodeWorkflow.run,
                    node_input,
                    id=f"agent-{params.run_id}-{node.id}",
                    retry_policy=RetryPolicy(maximum_attempts=3),
                    execution_timeout=timedelta(minutes=30),
                )
                child_handles.append((node.id, handle))

            # Wait for all children in this layer
            for node_id, handle in child_handles:
                result = await handle
                layer_results[node_id] = result
                logger.info(
                    "DAGWorkflow run=%s node %s completed: state=%s",
                    params.run_id, node_id, result.get("state", "unknown"),
                )

        return DAGResult(
            run_id=params.run_id,
            results=layer_results,
            status="completed",
        )

    @staticmethod
    def _inject_upstream(
        node: DAGNode,
        layer_results: dict[str, Any],
        params: DAGParams,
    ) -> dict:
        """Build the node_config dict for a child workflow, injecting outputs
        from upstream nodes that have already completed."""
        node_input: dict[str, Any] = {
            "node_id": node.id,
            "agent_type": node.agent_type,
            "model_provider": node.model_provider,
            "model_id": node.model_id,
            "prompt": node.prompt,
            "run_id": params.run_id,
            **node.extra,
            **params.global_config,
        }

        # Inject upstream results so the prompt can reference prior output
        if node.upstream_ids:
            upstream_data: dict[str, Any] = {}
            for uid in node.upstream_ids:
                if uid in layer_results:
                    upstream_data[uid] = layer_results[uid]
            node_input["upstream"] = upstream_data

        return node_input
