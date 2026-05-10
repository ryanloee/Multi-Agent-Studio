"use client";

import { useCallback, useMemo, useRef, type DragEvent } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  type ReactFlowInstance,
  type NodeMouseHandler,
} from "@xyflow/react";

import "@xyflow/react/dist/style.css";

import { useWorkflowStore } from "@/stores/workflowStore";
import { useRunStore } from "@/stores/runStore";
import { nodeTypes } from "@/components/canvas/nodeTypes";
import type { AgentNodeType, WorkflowNode, WorkflowEdge } from "@/types/workflow";
import { NODE_META } from "@/lib/constants";
import type { ChildCreatedEvent } from "@/types/events";

export default function FlowCanvas() {
  const staticNodes = useWorkflowStore((s) => s.nodes) ?? [];
  const edges = useWorkflowStore((s) => s.edges) ?? [];
  const onNodesChange = useWorkflowStore((s) => s.onNodesChange);
  const onEdgesChange = useWorkflowStore((s) => s.onEdgesChange);
  const onConnect = useWorkflowStore((s) => s.onConnect);
  const addNode = useWorkflowStore((s) => s.addNode);
  const setSelectedNode = useWorkflowStore((s) => s.setSelectedNode);

  const runStatus = useRunStore((s) => s.status);
  const setSelectedRunNode = useRunStore((s) => s.setSelectedRunNode);
  const parentChildMap = useRunStore((s) => s.parentChildMap);
  const allEvents = useRunStore((s) => s.events);

  // ---- React Flow instance ref (for screenToFlowPosition in onDrop) ----
  const rfInstanceRef = useRef<ReactFlowInstance<WorkflowNode, WorkflowEdge> | null>(null);
  const setRfInstance = useCallback((instance: ReactFlowInstance<WorkflowNode, WorkflowEdge>) => {
    rfInstanceRef.current = instance;
  }, []);

  // ---- Drag-over: allow drop ----
  const onDragOver = useCallback((event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
  }, []);

  // ---- Drop: create node from sidebar drag ----
  const onDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault();

      const nodeType = event.dataTransfer.getData("application/reactflow") as AgentNodeType | "";
      if (!nodeType || !NODE_META[nodeType]) return;

      // Convert screen coordinates to flow coordinates
      const rf = rfInstanceRef.current;
      if (!rf) return;
      const position = rf.screenToFlowPosition({
        x: event.clientX,
        y: event.clientY,
      });

      addNode(nodeType, position);
    },
    [addNode]
  );

  // ---- Node click: select node (+ set run node during active run) ----
  const onNodeClick: NodeMouseHandler = useCallback(
    (_event, node) => {
      setSelectedNode(node.id);
      if (runStatus === "running" || runStatus === "paused") {
        setSelectedRunNode(node.id);
      }
    },
    [setSelectedNode, runStatus, setSelectedRunNode]
  );

  // ---- Pane click: deselect ----
  const onPaneClick = useCallback(() => {
    setSelectedNode(null);
    setSelectedRunNode(null);
  }, [setSelectedNode, setSelectedRunNode]);

  // ---- Merge static nodes with dynamic child nodes from planner ----
  const { mergedNodes, mergedEdges } = useMemo(() => {
    const childEntries = Object.entries(parentChildMap);
    if (childEntries.length === 0) {
      return { mergedNodes: staticNodes, mergedEdges: edges };
    }

    // Build a lookup of child_created events to get type/prompt/model
    const childEventMap = new Map<string, ChildCreatedEvent>();
    for (const ev of allEvents) {
      if (ev.type === "child_created") {
        const ce = ev as ChildCreatedEvent;
        if (ce.child_node_id) childEventMap.set(ce.child_node_id, ce);
      }
    }

    const dynamicNodes: WorkflowNode[] = [];
    const dynamicEdges: WorkflowEdge[] = [];

    for (const [parentId, childIds] of childEntries) {
      const parentNode = staticNodes.find((n) => n.id === parentId);
      if (!parentNode) continue;

      const parentX = parentNode.position.x;
      const parentY = parentNode.position.y;
      const childCount = childIds.length;
      const spacing = 200;
      const totalWidth = (childCount - 1) * spacing;
      const startX = parentX - totalWidth / 2;

      for (let i = 0; i < childIds.length; i++) {
        const childId = childIds[i];
        const ce = childEventMap.get(childId);
        const childType = ce?.child_type || "coder";
        const childPrompt = ce?.child_prompt || "";

        dynamicNodes.push({
          id: childId,
          type: "child" as AgentNodeType,
          position: {
            x: startX + i * spacing,
            y: parentY + 120,
          },
          width: 180,
          height: 100,
          data: {
            label: `${childType} #${i + 1}`,
            agentType: childType as AgentNodeType,
            modelProvider: "",
            modelId: "",
            prompt: childPrompt,
            description: "",
            permissions: {},
            command: "",
            childType,
            childPrompt,
          },
        });

        dynamicEdges.push({
          id: `edge-${parentId}-${childId}`,
          source: parentId,
          target: childId,
          type: "smoothstep",
          animated: true,
          style: { stroke: "#22c55e", strokeWidth: 1.5 },
        });
      }
    }

    return {
      mergedNodes: [...staticNodes, ...dynamicNodes],
      mergedEdges: [...edges, ...dynamicEdges],
    };
  }, [staticNodes, edges, parentChildMap, allEvents]);

  return (
    <div className="w-full h-full relative">
      <ReactFlow
        nodes={mergedNodes}
        edges={mergedEdges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onInit={setRfInstance}
        onDragOver={onDragOver}
        onDrop={onDrop}
        onNodeClick={onNodeClick}
        onPaneClick={onPaneClick}
        nodeTypes={nodeTypes}
        snapToGrid
        snapGrid={[16, 16]}
        fitView
        minZoom={0.2}
        maxZoom={2}
      >
        <Background gap={16} size={1} />
        <Controls />
        <MiniMap
          nodeStrokeWidth={3}
          zoomable
          pannable
          className="!bg-gray-50 !border-gray-200"
        />
      </ReactFlow>

      {/* Empty canvas guide text */}
      {staticNodes.length === 0 && (
        <div className="absolute inset-0 flex items-center justify-center pointer-events-none select-none z-10">
          <div className="text-center space-y-3 max-w-sm px-4">
            <div className="w-12 h-12 rounded-xl bg-blue-50 flex items-center justify-center mx-auto">
              <svg className="w-6 h-6 text-blue-400" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
              </svg>
            </div>
            <p className="text-gray-500 text-base font-medium">
              从左侧节点库拖拽节点到此处
            </p>
            <p className="text-gray-400 text-sm leading-relaxed">
              先拖一个「规划器」作为起点，再拖「编码器」「审查器」等节点，用连线把它们串起来，就是一个自动化工作流。
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
