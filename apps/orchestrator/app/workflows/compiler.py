"""DAG Compiler: Converts React Flow JSON to Temporal Workflow execution plan."""

from collections import deque
from typing import Any


def compile_dag(dag_json: dict) -> list[list[dict]]:
    """Parse React Flow JSON, topologically sort nodes into execution layers.

    Returns list of layers. Each layer is a list of nodes that can execute in parallel.

    Example:
        Input: A → [B, C] → D
        Output: [[A], [B, C], [D]]

    Raises:
        ValueError: If DAG contains cycles.
    """
    nodes = {n["id"]: n for n in dag_json.get("nodes", [])}
    edges = dag_json.get("edges", [])

    # Build adjacency: source -> [targets]
    adj: dict[str, list[str]] = {nid: [] for nid in nodes}
    in_degree: dict[str, int] = {nid: 0 for nid in nodes}

    for edge in edges:
        source = edge["source"]
        target = edge["target"]
        adj[source].append(target)
        in_degree[target] += 1

    # Kahn's algorithm for topological sort with layer grouping
    layers: list[list[str]] = []
    queue = deque(nid for nid, deg in in_degree.items() if deg == 0)
    visited = 0

    while queue:
        layer_size = len(queue)
        layer = []
        for _ in range(layer_size):
            nid = queue.popleft()
            layer.append(nid)
            visited += 1
            for neighbor in adj[nid]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        layers.append(layer)

    if visited != len(nodes):
        raise ValueError("DAG contains a cycle")

    # Convert node IDs back to node configs
    return [[nodes[nid] for nid in layer] for layer in layers]
