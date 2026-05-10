"use client";

import { useCallback, useEffect, useRef, type DragEvent } from "react";
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
  const allEvents = useRunStore((s) => s.events);
  const addDynamicNode = useWorkflowStore((s) => s.addDynamicNode);

  // ---- Sync child_created events into workflowStore as real nodes ----
  useEffect(() => {
    const currentNodes = useWorkflowStore.getState().nodes;
    const existingIds = new Set(currentNodes.map((n) => n.id));

    for (const ev of allEvents) {
      if (ev.type !== "child_created") continue;
      const ce = ev as ChildCreatedEvent;
      const childId = ce.child_node_id;
      if (!childId || existingIds.has(childId)) continue;

      existingIds.add(childId);
      addDynamicNode(ce.node_id, {
        id: childId,
        type: (ce.child_type || "coder") as AgentNodeType,
        prompt: ce.child_prompt || "",
        model: ce.child_model || "",
      });
    }
  }, [allEvents, addDynamicNode]);

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

  // ---- Child nodes are added to workflowStore via addDynamicNode ----
  // No merge needed — staticNodes already contains them.

  return (
    <div className="w-full h-full relative">
      <ReactFlow
        nodes={staticNodes}
        edges={edges}
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
