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
import { useLocaleStore } from "@/stores/localeStore";
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
  const focusNodeId = useWorkflowStore((s) => s.focusNodeId);
  const setFocusNode = useWorkflowStore((s) => s.setFocusNode);
  const mode = useWorkflowStore((s) => s.mode);
  const t = useLocaleStore((s) => s.t);

  const setSelectedRunNode = useRunStore((s) => s.setSelectedRunNode);
  const allEvents = useRunStore((s) => s.events);
  const addDynamicNode = useWorkflowStore((s) => s.addDynamicNode);
  const setSelectedEdge = useWorkflowStore((s) => s.setSelectedEdge);

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
      setSelectedRunNode(node.id);
    },
    [setSelectedNode, setSelectedRunNode]
  );

  // ---- Pane click: deselect ----
  const onPaneClick = useCallback(() => {
    setSelectedNode(null);
    setSelectedRunNode(null);
    setSelectedEdge(null);
  }, [setSelectedNode, setSelectedRunNode, setSelectedEdge]);

  // ---- Edge click: select edge for configuration ----
  const onEdgeClick = useCallback(
    (_event: React.MouseEvent, edge: WorkflowEdge) => {
      setSelectedEdge(edge.id);
      setSelectedRunNode(null);
    },
    [setSelectedEdge, setSelectedRunNode]
  );

  // ---- Focus node when focusNodeId changes (e.g. from TaskBoard click) ----
  useEffect(() => {
    if (!focusNodeId || !rfInstanceRef.current) return;
    const rf = rfInstanceRef.current;
    const node = staticNodes.find((n) => n.id === focusNodeId);
    if (!node) return;

    // Animate to center the node with some zoom
    rf.fitView({
      nodes: [{ id: focusNodeId }],
      padding: 0.5,
      duration: 400,
      maxZoom: 1.2,
    });

    // Clear the focus trigger so it doesn't re-trigger
    setFocusNode(null);
  }, [focusNodeId, staticNodes, setFocusNode]);

  // ---- Child nodes are added to workflowStore via addDynamicNode ----
  // No merge needed — staticNodes already contains them.

  return (
    <div className="w-full h-full relative">
      <ReactFlow
        nodes={staticNodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={mode === "auto" ? undefined : onEdgesChange}
        onConnect={mode === "auto" ? undefined : onConnect}
        onInit={setRfInstance}
        onDragOver={mode === "auto" ? undefined : onDragOver}
        onDrop={mode === "auto" ? undefined : onDrop}
        onNodeClick={onNodeClick}
        onPaneClick={onPaneClick}
        onEdgeClick={onEdgeClick}
        nodeTypes={nodeTypes}
        snapToGrid
        snapGrid={[16, 16]}
        fitView
        minZoom={0.2}
        maxZoom={2}
        nodesDraggable
        nodesConnectable={mode !== "auto"}
        deleteKeyCode={mode === "auto" ? null : undefined}
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
            <div className={`w-12 h-12 rounded-xl flex items-center justify-center mx-auto ${
              mode === "auto" ? "bg-blue-50" : "bg-blue-50"
            }`}>
              {mode === "auto" ? (
                <svg className="w-6 h-6 text-blue-400" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
                </svg>
              ) : (
                <svg className="w-6 h-6 text-blue-400" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
                </svg>
              )}
            </div>
            {mode === "auto" ? (
              <>
                <p className="text-gray-500 text-base font-medium">
                  {t("canvas.autoMode")}
                </p>
                <p className="text-gray-400 text-sm leading-relaxed">
                  {t("canvas.autoModeDesc")}
                </p>
              </>
            ) : (
              <>
                <p className="text-gray-500 text-base font-medium">
                  {t("canvas.manualMode")}
                </p>
                <p className="text-gray-400 text-sm leading-relaxed">
                  {t("canvas.manualModeDesc")}
                </p>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
