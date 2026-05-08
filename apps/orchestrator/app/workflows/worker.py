"""Temporal Worker entry point."""

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from app.config import settings
from app.workflows.activities import (
    AgentNodeWorkflow,
    AgentNodeWorkflowV2,
    init_dependencies,
    start_agent_task,
    check_agent_status,
)
from app.workflows.dag_workflow import DAGWorkflow


async def run_worker():
    # Initialise module-level singletons for activities
    from app.sandbox.manager import SandboxManager
    from app.sandbox.checkpoint import GitCheckpointManager
    from app.sandbox.provision import SandboxProvisioner
    from app.streaming.publisher import StreamPublisher

    sandbox_mgr = SandboxManager(
        docker_url=settings.docker_socket,
        base_image=settings.sandbox_image,
    )
    checkpoint_mgr = GitCheckpointManager(sandbox_mgr)
    provisioner = SandboxProvisioner(sandbox_mgr)
    publisher = StreamPublisher()

    init_dependencies(sandbox_mgr, checkpoint_mgr, provisioner, publisher)

    # Connect to Temporal
    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
    )

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[AgentNodeWorkflow, AgentNodeWorkflowV2, DAGWorkflow],
        activities=[start_agent_task, check_agent_status],
    )

    print(f"Temporal Worker started on {settings.temporal_host}, queue: {settings.temporal_task_queue}")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(run_worker())
